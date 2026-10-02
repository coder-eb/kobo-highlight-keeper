"""Sync a set of device books against their stored records.

Shared by the calibre plugin (inside a device job, on the driver's own DB
connection) and by the command line / test harness (plain sqlite3).
"""

from pathlib import Path

from . import kobodb
from .epubtext import Book
from .sync import ACTIVE, DELETED, apply_to_device, reconcile


def sync_books(conn, mount, tasks, backup=None):
    """tasks: [{'key': <calibre id>, 'title': str, 'path': <file on device>, 'records': [...]}]

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
        results.append({'key': t['key'], 'title': t.get('title') or path.name, 'plan': plan})

    if backup and any(r.get('plan') and r['plan'].device_writes for r in results):
        backup()
    for r in results:
        if r.get('plan'):
            apply_to_device(conn, r['plan'])
    return results


def summarize(results):
    tot = dict(books=0, new=0, restored=0, repaired=0, deleted=0, orphans=0, pending=0, errors=0)
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
        active = sum(1 for rec in p.records.values() if rec['kobo']['state'] == ACTIVE)
        deleted = sum(1 for rec in p.records.values() if rec['kobo']['state'] == DELETED)
        lines.append(f'{r["title"]}: {active} active, {deleted} deleted on device'
                     + (f', {len(p.pending)} waiting for Kobo to index the book' if p.pending else ''))
        lines.extend('    ' + line for line in p.log)
    head = (f'{tot["books"]} books: {tot["new"]} new from Kobo, {tot["restored"]} restored to Kobo, '
            f'{tot["repaired"]} repaired, {tot["deleted"]} deleted on device, '
            f'{tot["orphans"]} orphaned, {tot["pending"]} pending, {tot["errors"]} errors')
    return head, tot, '\n'.join(lines)
