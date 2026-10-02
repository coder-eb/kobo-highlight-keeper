"""Kobo records <-> calibre's annotations table (user_type='local', user='kobo')."""

from calibre.utils.date import EPOCH
from calibre.utils.iso8601 import parse_iso8601

from calibre_plugins.kobo_highlight_keeper.core.sync import ACTIVE, DELETED, USER, USER_TYPE, apply_state_style


def _ours(a):
    """Our records, plus calibre's markers for ones deleted in calibre."""
    annot = a['annotation']
    return a['user_type'] == USER_TYPE and a['user'] == USER and ('kobo' in annot or annot.get('removed'))


def load_records(db, book_id):
    return [a['annotation'] for a in db.all_annotations_for_book(book_id) if _ours(a)]


def load_all_records(db, book_ids):
    """Kobo records for many books in a single query: {book_id: [records]}."""
    out = {}
    for a in db.all_annotations(restrict_to_user=(USER_TYPE, USER), restrict_to_book_ids=set(book_ids)):
        if _ours(a):
            out.setdefault(a['book_id'], []).append(a['annotation'])
    return out


def target_format(db, book_id):
    fmts = db.formats(book_id) or ()
    for f in ('KEPUB', 'EPUB'):
        if f in fmts:
            return f
    return fmts[0] if fmts else 'EPUB'


def save_records(db, book_id, records):
    """Replace this book's Kobo records (all kept under a single format)."""
    fmt = target_format(db, book_id)
    stale = {a['format'] for a in db.all_annotations_for_book(book_id) if _ours(a) and a['format'] != fmt}
    for other in stale:
        db.set_annotations_for_book(book_id, other, [], user_type=USER_TYPE, user=USER)
    alist = [(r, (parse_iso8601(r['timestamp'], assume_utc=True) - EPOCH).total_seconds()) for r in records]
    db.set_annotations_for_book(book_id, fmt, alist, user_type=USER_TYPE, user=USER)


def reactivate_deleted(db, book_id):
    """Undo 'deleted on device': the next sync restores them to the Kobo."""
    records = load_records(db, book_id)
    n = 0
    for r in records:
        if 'kobo' in r and r['kobo']['state'] == DELETED:
            r['kobo']['state'] = ACTIVE
            r['kobo']['on_device'] = False
            apply_state_style(r)
            n += 1
    if n:
        save_records(db, book_id, records)
    return n
