"""Sync a set of device books against their stored records.

Shared by the calibre plugin (inside a device job, on the driver's own DB
connection) and by the command line / test harness (plain sqlite3).
"""

import filecmp
from pathlib import Path

from . import kobodb
from .epubtext import Book
from .sync import ACTIVE, DELETED, add_calibre_positions, apply_to_device, reconcile


def sync_books(conn, mount, tasks, backup=None):
    """tasks: [{'key': <calibre id>, 'title': str, 'path': <file on device>, 'records': [...],
              optional 'library_path': <calibre's copy, for viewer positions>,
              optional 'viewer_spine_offset': callable -> int}]

    Returns [{'key', 'title', 'plan'|'error'}]. Device rows are written on
    ``conn`` (the caller commits); ``backup()`` runs once before any write.

    Cost: two queries for the whole device; a book file is opened only when
    one of its annotations is new, edited, missing or its file changed.
    """
    indexed_books = kobodb.sideloaded_books(conn)
    rows_by_volume = kobodb.bookmarks_by_volume(conn)
    results = []
    for t in tasks:
        path = Path(t['path'])
        vid = kobodb.volume_id_for(mount, path)
        rows = rows_by_volume.get(vid, [])
        if not rows and not t['records']:
            continue
        try:
            st = path.stat()
        except OSError as e:
            results.append({'key': t['key'], 'title': t.get('title'), 'error': f'{path.name}: {e}'})
            continue
        opened = {}

        def get_book(path=path, opened=opened):
            if 'book' not in opened:
                opened['book'] = Book(path)
            return opened['book']

        def chapter_id_for(book, href, vid=vid):
            return kobodb.chapter_content_id(conn, vid, book, href)

        fmt = 'kepub' if path.name.lower().endswith('.kepub.epub') else 'epub'
        try:
            plan = reconcile(get_book, fmt, vid, [st.st_size, int(st.st_mtime)], rows, t['records'],
                             chapter_id_for, device_indexed=vid in indexed_books)
        except Exception as e:  # unreadable / DRM / not an epub
            results.append({'key': t['key'], 'title': t.get('title'), 'error': f'{path.name}: {e}'})
            continue
        plan.opened_file = 'book' in opened
        if t.get('library_path'):
            try:
                add_library_positions(plan, t, path, opened)
            except Exception as e:  # positions are a convenience; never fail the sync
                plan.log.append(f'calibre viewer positions skipped: {e}')
        results.append({'key': t['key'], 'title': t.get('title') or path.name, 'plan': plan})

    matched = {kobodb.volume_id_for(mount, t['path']) for t in tasks}
    unmatched = unmatched_volumes(conn, rows_by_volume, matched)

    if any(r.get('plan') and r['plan'].device_writes for r in results):
        problem = integrity_problem(conn)
        if problem:
            # Never write into a damaged database; calibre still gets the pull.
            for r in results:
                if r.get('plan'):
                    r['plan'].skip_device_writes(f'Kobo database failed its integrity check ({problem}); '
                                                 'nothing was written to the Kobo')
        else:
            if backup:
                backup()
            for r in results:
                if r.get('plan'):
                    apply_to_device(conn, r['plan'])
    return SyncResult(results, unmatched)


def add_library_positions(plan, task, device_path, device_opened):
    lib = Path(task['library_path'])
    st = lib.stat()
    opened = {}

    def get_library_book():
        if 'book' not in opened:
            # The library copy is often the very file that was sent (a KEPUB
            # sent as-is): then reuse the already-parsed device book.
            if 'book' in device_opened and st.st_size == device_path.stat().st_size \
                    and filecmp.cmp(lib, device_path, shallow=False):
                opened['book'] = device_opened['book']
            else:
                opened['book'] = Book(lib)
        return opened['book']

    add_calibre_positions(plan, get_library_book, [st.st_size, int(st.st_mtime)],
                          task.get('viewer_spine_offset') or (lambda: 0))


class SyncResult(list):
    """The per-book results, plus books on the Kobo with annotations that
    weren't part of the sync (not in the library, or not matched to it)."""

    def __init__(self, results, unmatched):
        super().__init__(results)
        self.unmatched = unmatched


def integrity_problem(conn):
    """'' when SQLite's quick_check passes, else its first complaint."""
    try:
        row = next(iter(conn.execute('PRAGMA quick_check')))
        value = row['quick_check'] if isinstance(row, dict) else row[0]
    except Exception as e:
        return str(e)
    return '' if value == 'ok' else str(value)


def unmatched_volumes(conn, rows_by_volume, matched):
    """[{'volume_id', 'title', 'count', 'rows'}] for annotated books not synced."""
    out = []
    for vid, rows in rows_by_volume.items():
        visible = [r for r in rows if not kobodb.is_hidden(r)]
        if vid in matched or not visible:
            continue
        title = next(iter(conn.execute('SELECT Title FROM content WHERE ContentID = ?', (vid,))), None)
        out.append({'volume_id': vid, 'title': (title or {}).get('Title') or vid,
                    'count': len(visible), 'rows': visible})
    return sorted(out, key=lambda u: u['title'].lower())


def summarize(results):
    tot = dict(books=0, new=0, restored=0, repaired=0, deleted=0, orphans=0, pending=0, errors=0,
               notes_sent=0, removed=0)
    lines = []
    for r in results:
        if r.get('error'):
            tot['errors'] += 1
            lines.append(f'! {r["title"] or r["key"]}: {r["error"]}')
            continue
        p = r['plan']
        tot['books'] += 1
        tot['restored'] += len(p.to_insert)
        tot['repaired'] += len(p.to_repair)
        tot['orphans'] += len(p.orphans)
        tot['pending'] += len(p.pending)
        tot['new'] += sum(1 for line in p.log if line.startswith('new from device'))
        tot['deleted'] += sum(1 for line in p.log if line.startswith('deleted on device'))
        tot['notes_sent'] += len(p.to_update)
        tot['removed'] += len(p.to_delete)
        tot['errors'] += sum(1 for line in p.log if line.startswith('ERROR'))
        active = sum(1 for rec in p.records.values() if rec['kobo']['state'] == ACTIVE)
        deleted = sum(1 for rec in p.records.values() if rec['kobo']['state'] == DELETED)
        lines.append(f'{r["title"]}: {active} active, {deleted} deleted on device'
                     + (f', {len(p.pending)} waiting for Kobo to index the book' if p.pending else ''))
        lines.extend('    ' + line for line in p.log)
    head = (f'{tot["books"]} books: {tot["new"]} new from Kobo, {tot["restored"]} restored to Kobo, '
            f'{tot["repaired"]} repaired, {tot["deleted"]} deleted on device, '
            f'{tot["notes_sent"]} notes sent to Kobo, {tot["removed"]} removed from Kobo, '
            f'{tot["orphans"]} orphaned, {tot["pending"]} pending, {tot["errors"]} errors')
    unmatched = getattr(results, 'unmatched', [])
    if unmatched:
        lines.append(f'\n{len(unmatched)} book(s) on the Kobo have annotations but are not in the library '
                     '(or not matched to it); their annotations were saved to the backup file:')
        lines.extend(f'    {u["title"]}: {u["count"]}' for u in unmatched)
    return head, tot, '\n'.join(lines)
