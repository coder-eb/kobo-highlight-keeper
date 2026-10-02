#!/usr/bin/env python3
"""Time the sync engine on a large simulated Kobo.

  python3 harness/bench.py [--books 200] [--annotated 30] [--per-book 25]

Builds a temp device with copies of the sandbox books, highlights some of
them, then times the cases that matter:
  first sync      everything is new
  no-change sync  what runs on every connect when nothing happened
  restore         5 books were deleted and re-sent (Kobo wiped their rows)
"""

import argparse
import random
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'harness'))

from kobo_highlight_keeper import anchor as A, kobodb  # noqa: E402
from kobo_highlight_keeper.engine import sync_books  # noqa: E402
from kobo_highlight_keeper.epubtext import Book  # noqa: E402
from setup_sandbox import kobo_schema_sql  # noqa: E402

SOURCES = sorted((ROOT / 'sandbox' / 'source_books').glob('*/*.kepub'))


def index_book(con, mount, path):
    """Minimal Nickel indexing: book row + chapter rows."""
    vid = kobodb.volume_id_for(mount, path)
    b = Book(path)
    con.execute("INSERT INTO content (ContentID, ContentType, MimeType, Title, ___FileSize, ___UserID, "
                "IsDownloaded, ReadStatus, ___PercentRead) VALUES (?, 6, 'application/x-kobo-epub+zip', ?, ?, '', "
                "'true', 0, 0)", (vid, b.title, path.stat().st_size))
    for i, h in enumerate(b.spine):
        con.execute("INSERT INTO content (ContentID, ContentType, MimeType, BookID, Title, VolumeIndex, ___UserID, "
                    "___FileSize, ReadStatus, ___PercentRead, IsDownloaded) VALUES (?, 9, 'application/xhtml+xml', "
                    "?, ?, ?, '', 0, 0, 0, 'true')",
                    (f'{vid[len("file://"):]}!{b.opf_dir}!{b.opf_relative(h)}', vid, h, i))
    return vid, b


def highlight(con, vid, b, rng):
    ch = rng.choice([c for c in b.chapters() if len(c.text) > 3000])
    r = rng.choice(ch.runs)
    s = rng.randrange(r.start, r.end)
    e = min(len(ch.text), s + rng.randrange(20, 200))
    loc = A.to_kobo_location(b, ch, s, e)
    row = dict(loc, BookmarkID=str(uuid.uuid4()), VolumeID=vid,
               ContentID=f'{vid[len("file://"):]}!{b.opf_dir}!{b.opf_relative(ch.href)}',
               StartContainerChildIndex=-99, EndContainerChildIndex=-99, Text=ch.text[s:e],
               DateCreated='2026-09-01T10:00:00.000', DateModified='2026-09-01T10:00:00.000',
               Hidden='false', Type='highlight', Color=0)
    kobodb.insert_bookmark(con, row)


def timed(label, fn):
    t = time.perf_counter()
    out = fn()
    print(f'  {label:<38} {time.perf_counter() - t:7.2f} s')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--books', type=int, default=200)
    ap.add_argument('--annotated', type=int, default=30)
    ap.add_argument('--per-book', type=int, default=25)
    args = ap.parse_args()
    if not SOURCES:
        sys.exit('run harness/setup_sandbox.py first')
    rng = random.Random(7)
    tmp = Path(tempfile.mkdtemp(prefix='khk-bench-'))
    mount = tmp / 'KOBOeReader'
    (mount / '.kobo').mkdir(parents=True)
    db = mount / '.kobo' / 'KoboReader.sqlite'
    con = kobodb.connect(db)
    con.executescript(kobo_schema_sql())
    paths = []
    for i in range(args.books):
        p = mount / f'Author {i:03d}' / f'Book {i:03d}.kepub.epub'
        p.parent.mkdir()
        shutil.copyfile(SOURCES[i % len(SOURCES)], p)
        paths.append(p)
    annotated = paths[:args.annotated]
    for p in paths:
        vid, b = index_book(con, mount, p)
        if p in annotated:
            for _ in range(args.per_book):
                highlight(con, vid, b, rng)
    con.commit()
    print(f'{args.books} books on device, {args.annotated} with {args.per_book} highlights each '
          f'({args.annotated * args.per_book} total)')

    store = {str(p): [] for p in paths}

    def run(write=True):
        c = kobodb.connect(db)
        tasks = [{'key': str(p), 'title': p.name, 'path': p, 'records': store[str(p)], 'library_path': p}
                 for p in paths]
        res = sync_books(c, mount, tasks, write=write)
        c.commit()
        c.close()
        writes = 0
        for r in res:
            if r.get('plan') is not None:
                if r['plan'].changed:
                    store[r['key']] = r['plan'].all_records()
                    writes += 1
        return writes

    w = timed('first copy from Kobo (all new)', lambda: run(False))
    print(f'  {"":<38} calibre writes for {w} books')
    w = timed('copy-only connect, nothing changed', lambda: run(False))
    print(f'  {"":<38} calibre writes for {w} books')
    w = timed('full sync, nothing changed', run)
    print(f'  {"":<38} calibre writes for {w} books')
    con = sqlite3.connect(db)
    for p in annotated[:5]:
        con.execute('DELETE FROM Bookmark WHERE VolumeID = ?', (kobodb.volume_id_for(mount, p),))
    con.commit()
    con.close()
    w = timed('copy-only connect after 5 wiped books', lambda: run(False))
    print(f'  {"":<38} calibre writes for {w} books (changes held back)')
    w = timed('full sync: restore the 5 wiped books', run)
    print(f'  {"":<38} calibre writes for {w} books')
    shutil.rmtree(tmp)


if __name__ == '__main__':
    main()
