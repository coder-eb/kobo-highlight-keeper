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
            'format': 'kepub'|'epub', 'volume_id': ..., 'file_size': ...,
            'on_device': bool, 'last_seen': <ISO>, 'history': [...]}}

Dogears (Kobo bookmarks without text) are stored as calibre 'bookmark'
annotations titled 'Kobo dogear <id>' with the same 'kobo' payload.
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
    orphans: list = field(default_factory=list)        # records that could not be placed
    pending: list = field(default_factory=list)        # restores waiting for Kobo to index
    log: list = field(default_factory=list)

    @property
    def device_writes(self):
        return bool(self.to_insert or self.to_repair)


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


def row_to_record(row, anchor, book_format, volume_id, file_size):
    kobo = {
        'v': 1, 'state': ACTIVE, 'raw': dict(row), 'anchor': anchor,
        'format': book_format, 'volume_id': volume_id, 'file_size': file_size,
        'on_device': True, 'last_seen': now_iso(), 'history': [],
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


def reconcile(book, book_format, volume_id, file_size, device_rows, records,
              chapter_id_for, device_indexed=True):
    """Plan for one book.

    book            epubtext.Book of the file currently on the device
    device_rows     all Bookmark rows for volume_id (incl. hidden)
    records         stored records for this calibre book (any state)
    chapter_id_for  callable(href) -> Kobo chapter ContentID
    device_indexed  False when Kobo hasn't indexed the file yet (just sent)
    """
    plan = Plan(records={record_key(r): copy.deepcopy(r) for r in records})
    by_bid = {bookmark_id(r): k for k, r in plan.records.items()}
    visible = {r['BookmarkID']: r for r in device_rows if not is_hidden(r)}
    hidden = {r['BookmarkID'] for r in device_rows if is_hidden(r)}

    # 1. Pull: device -> calibre.
    for bid, row in visible.items():
        if (row.get('Type') or '').lower() == 'markup':
            plan.log.append(f'skip handwritten markup {bid} (not supported)')
            continue
        key = by_bid.get(bid)
        if key is None:
            anchor = anchor_for_row(book, row)
            rec = row_to_record(row, anchor, book_format, volume_id, file_size)
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
        if kobo['state'] != ACTIVE:
            kobo['state'] = ACTIVE  # present on device again: it is active
            kobo['history'].append({'at': now_iso(), 'event': 'reactivated'})
        newer = kobo_ts(row.get('DateModified')) > kobo_ts(old.get('DateModified'))
        if newer or row.get('Annotation') != old.get('Annotation') or row.get('Text') != old.get('Text'):
            kobo['raw'] = dict(row)
            if rec['type'] == 'highlight':
                rec['highlighted_text'] = row.get('Text') or ''
                rec['notes'] = row.get('Annotation') or ''
            rec['timestamp'] = kobo_ts(row.get('DateModified') or row.get('DateCreated'))
            plan.log.append(f'updated from device: {(row.get("Text") or "")[:50]!r}')
        if location_ok(book, row, kobo.get('anchor')):
            fresh = anchor_for_row(book, row)
            if fresh:
                kobo['anchor'] = fresh
        kobo.update(on_device=True, last_seen=now_iso(), volume_id=volume_id,
                    file_size=file_size, format=book_format)
        plan.changed.add(key)

    # 2. Deletions on the device.
    prev_on = [k for k, r in plan.records.items()
               if r['kobo']['state'] == ACTIVE and r['kobo'].get('on_device')]
    missing = [k for k in prev_on if bookmark_id(plan.records[k]) not in visible]
    file_changed = any(plan.records[k]['kobo'].get('file_size') not in (None, file_size)
                       for k in prev_on)
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
        m = A.locate(book, kobo.get('anchor') or {}) if kobo.get('anchor') else None
        if m is None:
            plan.orphans.append(k)
            kobo['on_device'] = False
            plan.changed.add(k)
            plan.log.append(f'ORPHAN (text not found, not restored): {rec.get("highlighted_text", "")[:60]!r}')
            continue
        row = dict(kobo['raw'])
        row.update(A.to_kobo_location(book, m['chapter'], m['start'], m['end']))
        row.update(VolumeID=volume_id, ContentID=chapter_id_for(m['chapter'].href), Hidden='false')
        if m['method'] == 'fuzzy':
            row['Text'] = m['chapter'].text[m['start']:m['end']]
        plan.to_insert.append(row)
        kobo['raw'] = row
        kobo.update(on_device=True, volume_id=volume_id, file_size=file_size, format=book_format)
        kobo['history'].append({'at': now_iso(), 'event': 'restored', 'method': m['method']})
        plan.changed.add(k)
        plan.log.append(f'restore ({m["method"]}): {(rec.get("highlighted_text") or "dogear")[:50]!r}')

    # 4. Repair rows still on the device whose location no longer matches.
    for bid, row in visible.items():
        key = by_bid.get(bid)
        if key is None or location_ok(book, row, plan.records[key]['kobo'].get('anchor')):
            continue
        rec = plan.records[key]
        m = A.locate(book, rec['kobo'].get('anchor') or {}) if rec['kobo'].get('anchor') else None
        if m is None:
            plan.orphans.append(key)
            plan.log.append(f'ORPHAN on device (location broken, text not found): {(row.get("Text") or "")[:60]!r}')
            continue
        fields = A.to_kobo_location(book, m['chapter'], m['start'], m['end'])
        fields['ContentID'] = chapter_id_for(m['chapter'].href)
        plan.to_repair.append((bid, fields))
        rec['kobo']['raw'].update(fields)
        rec['kobo']['history'].append({'at': now_iso(), 'event': 'repaired', 'method': m['method']})
        plan.changed.add(key)
        plan.log.append(f'repair location ({m["method"]}): {(row.get("Text") or "")[:50]!r}')

    for k in plan.changed:
        apply_state_style(plan.records[k])
    return plan


def apply_to_device(con, plan):
    from .kobodb import insert_bookmark, update_bookmark
    for row in plan.to_insert:
        insert_bookmark(con, row)
    for bid, fields in plan.to_repair:
        update_bookmark(con, bid, fields)

