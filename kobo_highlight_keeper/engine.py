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
    """
    indexed_books = kobodb.sideloaded_books(conn)
    results = []
    for t in tasks:
        path = Path(t['path'])
        vid = kobodb.volume_id_for(mount, path)
        try:
            book = Book(path)
        except Exception as e:  # unreadable / DRM / not an epub
            results.append({'key': t['key'], 'title': t.get('title'), 'error': f'{path.name}: {e}'})
            continue
        rows = kobodb.bookmarks(conn, vid)
        if not rows and not t['records']:
            continue
        indexed = vid in indexed_books and bool(kobodb.chapter_content_ids(conn, vid))

        def chapter_id_for(href, vid=vid, book=book):
            return kobodb.chapter_content_id(conn, vid, book, href)

        plan = reconcile(book, 'kepub' if book.is_kepub else 'epub', vid, path.stat().st_size,
                         rows, t['records'], chapter_id_for, device_indexed=indexed)
        results.append({'key': t['key'], 'title': t.get('title') or book.title, 'plan': plan})

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
