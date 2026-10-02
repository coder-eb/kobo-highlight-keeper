"""Reconcile one book's Kobo Bookmark rows with the records kept in calibre.

Pure logic: takes the device rows, the stored records and the book file that
is on the device; returns a Plan. Callers (the calibre plugin, the CLI, the
tests) apply the plan to calibre and to the device database.

Record format: a calibre annotation dict (stored with user_type='local',
user='kobo') with everything Kobo-specific under the 'kobo' key:

  {'type': 'highlight', 'uuid': <BookmarkID>, 'timestamp': <ISO UTC>,
   'highlighted_text': ..., 'notes': ..., 'style': {...},
   'kobo': {'v': 1, 'state': 'active'|'deleted_on_device'|'replaced',
            'raw': {<every Bookmark column>}, 'anchor': {...},
            'format': 'kepub'|'epub', 'volume_id': ..., 'file_sig': [size, mtime],
            'on_device': bool, 'history': [...]}}

Dogears (Kobo bookmarks without text) are stored as calibre 'bookmark'
annotations titled 'Kobo dogear <id>' with the same 'kobo' payload.

calibre is the main copy: an annotation deleted in calibre (calibre leaves a
{'removed': True, 'uuid'/'title'} marker) is deleted from the Kobo, and a
note edited in calibre is sent to the Kobo (if both sides edited, newer wins).
"""

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import anchor as A
from .kobodb import is_hidden

USER_TYPE, USER = 'local', 'kobo'
ACTIVE, DELETED, REPLACED = 'active', 'deleted_on_device', 'replaced'
# Clara BW only writes 0; colour models use the codes measured by
# Calibre-Web-NextGen (0 yellow, 1 pink, 2 blue, 3 green).
KOBO_COLORS = {0: 'yellow', 1: 'red', 2: 'blue', 3: 'green'}
LOCATION_COLS = ('StartContainerPath', 'StartOffset', 'EndContainerPath', 'EndOffset',
                 'ChapterProgress', 'ContentID')


@dataclass
class Plan:
    records: dict = field(default_factory=dict)        # key -> record (full new state)
    changed: set = field(default_factory=set)          # keys whose record changed
    to_insert: list = field(default_factory=list)      # Bookmark rows for the device
    to_repair: list = field(default_factory=list)      # (BookmarkID, location fields)
    to_update: list = field(default_factory=list)      # (BookmarkID, fields): notes edited in calibre
    to_delete: list = field(default_factory=list)      # BookmarkIDs deleted in calibre
    orphans: list = field(default_factory=list)        # records that could not be placed
    pending: list = field(default_factory=list)        # restores waiting for Kobo to index
    tombstones: list = field(default_factory=list)     # calibre's markers for deleted annotations
    log: list = field(default_factory=list)
    raw_before_update: dict = field(default_factory=dict)  # BookmarkID -> raw, to undo note pushes
    spots: dict = field(default_factory=dict)  # key -> (chapter, start, end) in spots_book, if known
    spots_book: object = None

    @property
    def device_writes(self):
        return bool(self.to_insert or self.to_repair or self.to_update or self.to_delete)

    def skip_device_writes(self, reason):
        """Drop planned Kobo writes; records keep their pre-sync device state."""
        if not self.device_writes:
            return
        for row in self.to_insert:
            rec = self.records.get(row_key(row))
            if rec:
                rec['kobo']['on_device'] = False
        for rec in self.records.values():
            before = self.raw_before_update.get(bookmark_id(rec))
            if before is not None:  # the Kobo still has the old note
                rec['kobo']['raw'] = before
        self.to_insert, self.to_repair, self.to_update, self.to_delete = [], [], [], []
        self.log.append('ERROR ' + reason)

    def all_records(self):
        """Everything to store back in calibre, including deletion markers."""
        return list(self.records.values()) + self.tombstones


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def kobo_ts(value):
    """Kobo dates ('2026-01-01T10:00:00.000', '...Z') as calibre ISO UTC."""
    if not value:
        return now_iso()
    v = str(value).strip().replace(' ', 'T')
    if v.endswith('Z'):
        v = v[:-1]
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return now_iso()
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def record_key(rec):
    return rec.get('uuid') or rec.get('title')


def row_key(row):
    """The record key a device row has (or would get) in calibre."""
    return f'Kobo dogear {row["BookmarkID"][:8]}' if is_dogear(row) else row['BookmarkID']


def kobo_now():
    """Timestamp in the format Kobo writes."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]


def set_chapter(rec, book):
    """Chapter path for calibre's annotation browser (groups by chapter)."""
    a = rec['kobo'].get('anchor')
    if a and a.get('href'):
        titles = book.toc_titles(book.find_href(a['href']) or a['href'], a.get('start', 0))
        if titles:
            rec['toc_family_titles'] = titles


def bookmark_id(rec):
    return rec['kobo']['raw'].get('BookmarkID')


def is_dogear(row):
    return (row.get('Type') or '').lower() == 'dogear' or not (row.get('Text') or '').strip()


STRIKEOUT = {'kind': 'decoration', 'type': 'builtin', 'which': 'strikeout'}


def color_style(row):
    return {'kind': 'color', 'type': 'builtin', 'which': KOBO_COLORS.get(row.get('Color') or 0, 'yellow')}


def apply_state_style(rec):
    """Highlights deleted on the Kobo show struck out in calibre's annotation browser."""
    if rec['type'] == 'highlight':
        rec['style'] = STRIKEOUT if rec['kobo']['state'] == DELETED else color_style(rec['kobo']['raw'])


def row_to_record(row, anchor, book_format, volume_id, file_sig):
    kobo = {
        'v': 1, 'state': ACTIVE, 'raw': dict(row), 'anchor': anchor,
        'format': book_format, 'volume_id': volume_id, 'file_sig': file_sig,
        'on_device': True, 'history': [],
    }
    ts = kobo_ts(row.get('DateModified') or row.get('DateCreated'))
    if is_dogear(row):
        return {'type': 'bookmark', 'title': f'Kobo dogear {row["BookmarkID"][:8]}',
                'timestamp': ts, 'pos_type': 'kobo', 'pos': row.get('StartContainerPath') or '',
                'kobo': kobo}
    return {'type': 'highlight', 'uuid': row['BookmarkID'], 'timestamp': ts,
            'highlighted_text': row.get('Text') or '', 'notes': row.get('Annotation') or '',
            'style': color_style(row), 'kobo': kobo}


def anchor_for_row(book, row):
    hit = A.resolve_row(book, row)
    text = row.get('Text') or ''
    if hit:
        ch, s, e = hit
        if is_dogear(row):
            return A.make_anchor(ch, s, s, dogear=True)
        if not text or A.normalize(ch.text[s:e]) == A.normalize(text):
            return A.make_anchor(ch, s, e, text)
    # Location doesn't point at the highlighted text (file changed): use text.
    return A.anchor_from_text(book, text) if text else None


def remember_spot(plan, key, book, row, anchor):
    """Note where a row is in ``book`` so calibre positions needn't search again."""
    hit = A.resolve_row(book, row)
    if hit and location_ok(book, row, anchor):
        plan.spots[key], plan.spots_book = hit, book


def location_ok(book, row, anchor=None):
    """Does the row's location still point at its own text in this file?"""
    hit = A.resolve_row(book, row)
    if not hit:
        return False
    ch, s, e = hit
    if is_dogear(row):
        # A dogear has no text: compare what follows it with its anchor.
        if not anchor or not anchor.get('exact'):
            return True
        here = A.normalize(ch.text[s:s + 4 * A.DOGEAR_SPAN])
        return here.startswith(anchor['exact'][:40])
    return A.normalize(ch.text[s:e]) == A.normalize(row.get('Text') or '')


LOCATION_KEYS = ('ContentID', 'StartContainerPath', 'StartOffset', 'EndContainerPath', 'EndOffset')
CONTENT_KEYS = ('Text', 'Annotation', 'DateModified', 'Color', 'Type')


def reconcile(get_book, book_format, volume_id, file_sig, device_rows, records,
              chapter_id_for, device_indexed=True):
    """Plan for one book.

    get_book        callable -> epubtext.Book of the file on the device. Only
                    called when a step needs the text (new, edited or missing
                    annotations, or the file changed), so an unchanged book
                    costs no file I/O at all.
    file_sig        [size, mtime] of that file; locations are re-checked only
                    when it differs from the one recorded at the last sync.
    device_rows     all Bookmark rows for volume_id (incl. hidden)
    records         stored records for this calibre book (any state)
    chapter_id_for  callable(book, href) -> Kobo chapter ContentID
    device_indexed  False when Kobo hasn't indexed the file yet (just sent)
    """
    tombstones = [r for r in records if r.get('removed')]
    records = [r for r in records if not r.get('removed') and 'kobo' in r]
    plan = Plan(records={record_key(r): copy.deepcopy(r) for r in records}, tombstones=tombstones)
    deleted_in_calibre = {record_key(t) for t in tombstones}
    by_bid = {bookmark_id(r): k for k, r in plan.records.items()}
    visible = {r['BookmarkID']: r for r in device_rows if not is_hidden(r)}
    hidden = {r['BookmarkID'] for r in device_rows if is_hidden(r)}
    to_validate = []

    # 1. Pull: device -> calibre.
    for bid, row in visible.items():
        if (row.get('Type') or '').lower() == 'markup':
            plan.log.append(f'skip handwritten markup {bid} (not supported)')
            continue
        key = by_bid.get(bid)
        if key is None and row_key(row) in deleted_in_calibre:
            plan.to_delete.append(bid)
            plan.log.append(f'deleted in calibre, removed from Kobo: {(row.get("Text") or "dogear")[:50]!r}')
            continue
        if key is None:
            book = get_book()
            anchor = anchor_for_row(book, row)
            rec = row_to_record(row, anchor, book_format, volume_id, file_sig)
            set_chapter(rec, book)
            remember_spot(plan, record_key(rec), book, row, anchor)
            k = record_key(rec)
            # Same passage re-highlighted after a wipe: the new row wins.
            for other in plan.records.values():
                ok = other['kobo']
                if ok['state'] == ACTIVE and bookmark_id(other) not in visible and \
                        other.get('highlighted_text') and \
                        A.normalize(other['highlighted_text']) == A.normalize(row.get('Text')):
                    ok['state'] = REPLACED
                    ok['history'].append({'at': now_iso(), 'event': 'replaced', 'by': bid})
                    plan.changed.add(record_key(other))
            plan.records[k] = rec
            plan.changed.add(k)
            plan.log.append(f'new from device: {(row.get("Text") or "dogear")[:50]!r}')
            continue
        rec = plan.records[key]
        kobo = rec['kobo']
        old = kobo['raw']
        changed = False
        if kobo['state'] != ACTIVE:
            kobo['state'] = ACTIVE  # present on device again: it is active
            kobo['history'].append({'at': now_iso(), 'event': 'reactivated'})
            changed = True
        if rec['type'] == 'highlight':
            synced, here = old.get('Annotation') or '', rec.get('notes') or ''
            on_kobo = row.get('Annotation') or ''
            if here != synced and (on_kobo == synced or
                                   kobo_ts(rec['timestamp']) > kobo_ts(row.get('DateModified'))):
                # Edited in calibre (and newer than any edit on the Kobo): send it.
                fields = {'Annotation': here or None, 'DateModified': kobo_now(),
                          'Type': 'note' if here else 'highlight'}
                plan.to_update.append((bid, fields))
                plan.raw_before_update[bid] = dict(old)
                row = dict(row, **fields)
                kobo['raw'] = dict(old, **fields)
                old = kobo['raw']
                plan.log.append(f'note edited in calibre, sent to Kobo: {(row.get("Text") or "")[:50]!r}')
                changed = True
        if any(row.get(c) != old.get(c) for c in CONTENT_KEYS):
            if rec['type'] == 'highlight':
                rec['highlighted_text'] = row.get('Text') or ''
                rec['notes'] = row.get('Annotation') or ''
            rec['timestamp'] = kobo_ts(row.get('DateModified') or row.get('DateCreated'))
            plan.log.append(f'updated from device: {(row.get("Text") or "")[:50]!r}')
            changed = True
        moved = any(row.get(c) != old.get(c) for c in LOCATION_KEYS)
        if moved or kobo.get('file_sig') != file_sig:
            to_validate.append(bid)  # checked (and refreshed or repaired) in step 4
            changed = True
        if changed or not kobo.get('on_device') or kobo.get('volume_id') != volume_id:
            kobo['raw'] = dict(row)
            kobo.update(on_device=True, volume_id=volume_id, file_sig=file_sig, format=book_format)
            plan.changed.add(key)

    # 2. Deletions on the device (no file access needed).
    prev_on = [k for k, r in plan.records.items()
               if r['kobo']['state'] == ACTIVE and r['kobo'].get('on_device')]
    missing = [k for k in prev_on if bookmark_id(plan.records[k]) not in visible]
    file_changed = any(plan.records[k]['kobo'].get('file_sig') not in (None, file_sig) for k in missing)
    wiped = bool(prev_on) and (len(missing) == len(prev_on) or file_changed)
    for k in missing:
        rec = plan.records[k]
        if bookmark_id(rec) in hidden or not wiped:
            rec['kobo']['state'] = DELETED
            rec['kobo']['on_device'] = False
            rec['kobo']['history'].append({'at': now_iso(), 'event': 'deleted_on_device'})
            plan.changed.add(k)
            plan.log.append(f'deleted on device (kept in calibre): {rec.get("highlighted_text", "")[:50]!r}')
    if wiped:
        plan.log.append(f'device lost {len(missing)} annotations (re-import/wipe): restoring')

    # 3. Restore: calibre -> device.
    for k, rec in plan.records.items():
        kobo = rec['kobo']
        if kobo['state'] != ACTIVE or bookmark_id(rec) in visible:
            continue
        if not device_indexed:
            plan.pending.append(k)
            continue
        book = get_book()
        row = dict(kobo['raw'])
        hit = A.resolve_row(book, row)
        if hit and location_ok(book, row, kobo.get('anchor')):
            # 1st choice: the original Kobo position still points at this
            # text (e.g. the same file re-sent): reuse it, no search at all.
            row.update(VolumeID=volume_id, ContentID=chapter_id_for(book, hit[0].href), Hidden='false')
            plan.to_insert.append(row)
            kobo['raw'] = row
            kobo.update(on_device=True, volume_id=volume_id, file_sig=file_sig, format=book_format)
            kobo['history'].append({'at': now_iso(), 'event': 'restored', 'method': 'same-position'})
            plan.spots[k], plan.spots_book = hit, book
            if 'toc_family_titles' not in rec:
                set_chapter(rec, book)
            plan.changed.add(k)
            plan.log.append(f'restore (same position): {(rec.get("highlighted_text") or "dogear")[:50]!r}')
            continue
        # 2nd: find the text (own chapter first, then the rest of the book).
        m = A.locate(book, kobo.get('anchor') or {}) if kobo.get('anchor') else None
        if m is None:
            plan.orphans.append(k)
            if kobo.get('on_device'):
                kobo['on_device'] = False
                plan.changed.add(k)
            plan.log.append(f'ORPHAN (text not found, not restored): {rec.get("highlighted_text", "")[:60]!r}')
            continue
        row = dict(kobo['raw'])
        row.update(A.to_kobo_location(book, m['chapter'], m['start'], m['end']))
        row.update(VolumeID=volume_id, ContentID=chapter_id_for(book, m['chapter'].href), Hidden='false')
        if m['method'] == 'fuzzy':
            row['Text'] = m['chapter'].text[m['start']:m['end']]
        plan.to_insert.append(row)
        kobo['raw'] = row
        kobo.update(on_device=True, volume_id=volume_id, file_sig=file_sig, format=book_format)
        kobo['history'].append({'at': now_iso(), 'event': 'restored', 'method': m['method']})
        set_chapter(rec, book)
        plan.spots[k], plan.spots_book = (m['chapter'], m['start'], m['end']), book
        plan.changed.add(k)
        plan.log.append(f'restore ({m["method"]}): {(rec.get("highlighted_text") or "dogear")[:50]!r}')

    # 4. Re-check rows whose location or file changed; refresh or repair.
    for bid in to_validate:
        book = get_book()
        row = visible[bid]
        key = by_bid[bid]
        rec = plan.records[key]
        if location_ok(book, row, rec['kobo'].get('anchor')):
            fresh = anchor_for_row(book, row)
            if fresh:
                rec['kobo']['anchor'] = fresh
                set_chapter(rec, book)
            remember_spot(plan, key, book, row, rec['kobo']['anchor'])
            continue
        m = A.locate(book, rec['kobo'].get('anchor') or {}) if rec['kobo'].get('anchor') else None
        if m is None:
            plan.orphans.append(key)
            plan.log.append(f'ORPHAN on device (location broken, text not found): {(row.get("Text") or "")[:60]!r}')
            continue
        fields = A.to_kobo_location(book, m['chapter'], m['start'], m['end'])
        fields['ContentID'] = chapter_id_for(book, m['chapter'].href)
        plan.to_repair.append((bid, fields))
        plan.spots[key], plan.spots_book = (m['chapter'], m['start'], m['end']), book
        rec['kobo']['raw'].update(fields)
        rec['kobo']['history'].append({'at': now_iso(), 'event': 'repaired', 'method': m['method']})
        plan.log.append(f'repair location ({m["method"]}): {(row.get("Text") or "")[:50]!r}')

    for k in plan.changed:
        apply_state_style(plan.records[k])
    return plan


def add_calibre_positions(plan, get_library_book, library_sig, viewer_spine_offset=lambda: 0):
    """Give records calibre-viewer positions in the library's copy of the book.

    calibre's annotation browser sorts by spine_index + start_cfi and "Open in
    viewer" jumps to them (and fails without them), so every highlight gets
    them. They're recomputed only for changed records or when the library
    file changed. When the passage can't be placed exactly, the record points
    at the start of its chapter (kobo.cfi_exact = False).
    """
    offset = None
    for k, rec in plan.records.items():
        kobo = rec['kobo']
        if kobo.get('cfi_sig') == library_sig and k not in plan.changed:
            continue
        book = get_library_book()
        if offset is None:
            offset = viewer_spine_offset()
        anchor = kobo.get('anchor') or {}
        spot = plan.spots.get(k) if plan.spots_book is book else None
        if spot:  # already know where it is in this very file
            pos = A.calibre_position_at(*spot)
        else:
            pos = A.calibre_position(book, anchor) if anchor.get('exact') or anchor.get('dogear') else None
        if pos:
            pos['spine_index'] += offset
        else:
            href = book.find_href(anchor.get('href') or '')
            pos = {'spine_index': (book.spine.index(href) if href else 0) + offset,
                   'spine_name': href or '', 'start_cfi': '/2', 'end_cfi': '/2'}
        kobo['cfi_exact'] = pos['start_cfi'] != '/2'
        if rec['type'] == 'highlight':
            rec.update(pos)
        elif kobo['cfi_exact']:
            rec['pos_type'] = 'epubcfi'
            rec['pos'] = f'epubcfi(/{2 * (pos["spine_index"] + 1)}{pos["start_cfi"]})'
        kobo['cfi_sig'] = library_sig
        plan.changed.add(k)


def apply_to_device(con, plan):
    from .kobodb import delete_bookmark, insert_bookmark, update_bookmark
    for row in plan.to_insert:
        insert_bookmark(con, row)
    for bid, fields in plan.to_repair + plan.to_update:
        update_bookmark(con, bid, fields)
    for bid in plan.to_delete:
        delete_bookmark(con, bid)

