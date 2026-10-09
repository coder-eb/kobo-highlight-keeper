"""Import highlights stored by the calibre "Annotations" plugin (GRiker/davidfor).

That plugin keeps a book's annotations as HTML in a custom column:

  <div class="annotation" hash="…" location_sort="…" reader="KoboTouch">
    <table><tr><td class="location">7 · Medicine</td>
               <td class="timestamp" uts="1770460620.0">…</td></tr></table>
    <p class="highlight">highlighted text</p>
    <p class="note">optional note</p>
  </div>

It records no position in the book, so each highlight is placed by finding its
text in the book file (the same matching used for restores). Highlights that
can't be found are reported, never imported at a guessed position.
"""

import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser

from . import anchor as A
from .sync import ACTIVE, now_iso, record_key, row_to_record, set_chapter

LEGACY_MARKER = 'class="user_annotations"'


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items, self.cur, self.field, self.depth = [], None, None, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get('class') or ''
        if tag == 'div' and cls == 'annotation':
            self.cur = {'text': '', 'note': '', 'location': '', 'uts': None, 'hash': a.get('hash'),
                        'reader': a.get('reader') or ''}
            return
        if self.cur is None:
            return
        if tag == 'br':
            if self.field:
                self.cur[self._key()] += '\n'
            return
        if self.field:
            self.depth += 1  # an element nested inside the field (e.g. <i>)
        elif cls in ('highlight', 'note', 'location'):
            self.field, self.depth = cls, 1
        elif cls == 'timestamp':
            self.cur['uts'] = a.get('uts')

    def handle_endtag(self, tag):
        if tag == 'br':
            return
        if self.field:
            self.depth -= 1
            if self.depth == 0:
                self.field = None
        elif tag == 'div' and self.cur is not None:
            self.items.append(self.cur)
            self.cur = None

    def handle_data(self, data):
        if self.cur is not None and self.field:
            self.cur[self._key()] += data

    def _key(self):
        return 'text' if self.field == 'highlight' else self.field


def parse(html):
    """[{'text', 'note', 'location', 'uts', 'hash', 'reader'}] in the column's order."""
    p = _Parser()
    p.feed(html or '')
    p.close()
    return [dict(i, text=i['text'].strip(), note=i['note'].strip(), location=i['location'].strip())
            for i in p.items if i['text'].strip()]


def _kobo_date(uts):
    try:
        dt = datetime.fromtimestamp(float(uts), timezone.utc)
    except (TypeError, ValueError):
        dt = datetime.now(timezone.utc)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]


def import_records(book, items, existing, book_format, spine_offset=0, library_sig=None):
    """Records for legacy highlights not already present.

    Returns (new_records, skipped_duplicates, unplaced_items). New records are
    'active' and not yet on the Kobo, so the next full sync puts them there (or
    matches them to the Kobo's own copy if it still has them).
    """
    have = {A.normalize(r.get('highlighted_text')) for r in existing
            if 'kobo' in r and r.get('highlighted_text')}
    new, dupes, unplaced = [], [], []
    for it in items:
        norm = A.normalize(it['text'])
        if norm in have:
            dupes.append(it)
            continue
        text, method = it['text'], 'exact'
        anch = A.anchor_from_text(book, text)
        if anch is None:
            # The old plugin dropped paragraph breaks ("Christ.It's") and list
            # separators; fuzzy matching finds those, and the book's own text
            # is used for the highlight.
            m = A.locate(book, {'exact': A.normalize(text), 'href': '', 'prefix': '', 'suffix': ''})
            if m is None:
                unplaced.append(it)
                continue
            text, method = m['chapter'].text[m['start']:m['end']], 'fuzzy'
            anch = A.make_anchor(m['chapter'], m['start'], m['end'])
        m = A.locate(book, anch)
        when = _kobo_date(it['uts'])
        bid = str(uuid.uuid4())
        raw = {'BookmarkID': bid, 'Text': text, 'Annotation': it['note'] or None,
               'DateCreated': when, 'DateModified': when, 'Type': 'note' if it['note'] else 'highlight',
               'Hidden': 'false', 'Color': 0, 'StartContainerChildIndex': -99, 'EndContainerChildIndex': -99,
               'ChapterProgress': 0}
        raw.update(A.to_kobo_location(book, m['chapter'], m['start'], m['end']))
        rec = row_to_record(raw, anch, book_format, None, None)
        rec['kobo'].update(on_device=False, state=ACTIVE)
        rec['kobo']['history'].append({'at': now_iso(), 'event': 'imported from the Annotations plugin',
                                       'hash': it['hash'], 'method': method})
        set_chapter(rec, book)
        # calibre viewer position in this (library) file, as add_calibre_positions does
        pos = A.calibre_position_at(m['chapter'], m['start'], m['end'])
        if pos:
            pos['spine_index'] += spine_offset
            rec.update(pos)
            rec['kobo'].update(cfi_exact=True, cfi_sig=library_sig)
        have.add(norm)
        new.append(rec)
    assert len({record_key(r) for r in new}) == len(new)
    return new, dupes, unplaced
