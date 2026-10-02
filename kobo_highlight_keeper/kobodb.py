"""Thin access layer for a Kobo's .kobo/KoboReader.sqlite.

Columns are discovered with PRAGMA instead of hard-coded, so rows copied
from one firmware can be written to another (unknown columns are dropped,
new columns keep their defaults).
"""

import shutil
import sqlite3
import time
from pathlib import Path

ONBOARD = 'file:///mnt/onboard/'


def _dict_row(cursor, row):
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


def connect(path):
    """Plain sqlite3 connection with dict rows (CLI / tests).

    Inside calibre use ``device.database_transaction(use_row_factory=True)``
    instead: same dict rows, plus calibre's kobo_db_lock and copy-back.
    """
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = _dict_row
    return con


def columns(con, table='Bookmark'):
    return [r['name'] for r in con.execute(f'PRAGMA table_info({table})')]


def sideloaded_books(con):
    """ContentType 6 rows for books Kobo has indexed from the filesystem."""
    rows = con.execute(
        "SELECT ContentID, Title, Attribution, MimeType, ReadStatus, ___PercentRead, "
        "DateLastRead, ___FileSize FROM content WHERE ContentType = 6 AND ContentID LIKE 'file:///%'")
    return {r['ContentID']: dict(r) for r in rows}


def bookmarks(con, volume_id):
    return [dict(r) for r in con.execute('SELECT * FROM Bookmark WHERE VolumeID = ?', (volume_id,))]


def bookmarks_by_volume(con):
    """Every Bookmark row in one query, grouped by VolumeID."""
    out = {}
    for r in con.execute('SELECT * FROM Bookmark'):
        out.setdefault(r['VolumeID'], []).append(dict(r))
    return out


def is_hidden(row):
    return str(row.get('Hidden')).strip().lower() in ('true', '1')


def chapter_content_ids(con, volume_id):
    return [r['ContentID'] for r in con.execute(
        'SELECT ContentID FROM content WHERE BookID = ? AND ContentType IN (9, 899)', (volume_id,))]


def chapter_content_id(con, volume_id, book, href):
    """ContentID Kobo uses for a chapter: looked up, else built from its known format."""
    rel = book.opf_relative(href)
    known = chapter_content_ids(con, volume_id)
    if book.is_kepub:
        # Bookmark rows reference the type-9 manifest row:
        # '/mnt/onboard/<file>.kepub.epub!<opf dir>!<path relative to opf>'
        built = f'{volume_id[len("file://"):]}!{book.opf_dir}!{rel}'
        if built in known:
            return built
        for cid in known:
            if cid.endswith('!' + rel) and not cid.startswith('file://'):
                return cid
        return built
    for cid in known:
        if cid.endswith(')' + href) or cid.endswith(')' + rel):
            return cid
    return f'{volume_id}#({book.spine.index(href) if href in book.spine else 0}){href}'


def volume_id_for(mount, file_path):
    rel = Path(file_path).resolve().relative_to(Path(mount).resolve()).as_posix()
    return ONBOARD + rel


def file_for_volume(mount, volume_id):
    if not volume_id.startswith(ONBOARD):
        return None
    return Path(mount) / volume_id[len(ONBOARD):]


def backup(db_path, dest_dir, keep=20):
    """Timestamped copy (with WAL/SHM companions) before any write."""
    db_path, dest_dir = Path(db_path), Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    target = dest_dir / f'KoboReader-{stamp}.sqlite'
    n = 1
    while target.exists():
        target = dest_dir / f'KoboReader-{stamp}-{n}.sqlite'
        n += 1
    shutil.copy2(db_path, target)
    for suffix in ('-wal', '-shm'):
        side = db_path.with_name(db_path.name + suffix)
        if side.exists():
            shutil.copy2(side, target.with_name(target.name + suffix))
    olds = sorted(dest_dir.glob('KoboReader-*.sqlite'))
    for old in olds[:-keep]:
        for p in (old, old.with_name(old.name + '-wal'), old.with_name(old.name + '-shm')):
            p.unlink(missing_ok=True)
    return target


def insert_bookmark(con, row):
    cols = [c for c in columns(con) if c in row]
    con.execute(
        f'INSERT OR REPLACE INTO Bookmark ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})',
        [row[c] for c in cols])


def delete_bookmark(con, bookmark_id):
    con.execute('DELETE FROM Bookmark WHERE BookmarkID = ?', (bookmark_id,))


def update_bookmark(con, bookmark_id, fields):
    cols = [c for c in columns(con) if c in fields and c != 'BookmarkID']
    con.execute(f'UPDATE Bookmark SET {", ".join(f"{c} = ?" for c in cols)} WHERE BookmarkID = ?',
                [fields[c] for c in cols] + [bookmark_id])
