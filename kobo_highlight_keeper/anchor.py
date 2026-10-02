"""Format-independent anchors for Kobo highlights.

An anchor stores *what* was highlighted (normalized text plus a little
context) and *roughly where* (chapter + offset), never a Kobo location. At
restore time ``locate`` finds the passage in whatever file is on the device
and ``to_kobo_location`` produces fresh Kobo coordinates for it.

The matching follows the W3C TextQuoteSelector idea (exact + prefix +
suffix), which is also what Calibre-Web-NextGen's re-anchoring does.
"""

import re
from bisect import bisect_right
from difflib import SequenceMatcher

from .epubtext import kobo_offset_to_pos, run_offset_to_kobo, u16

CONTEXT = 48
DOGEAR_SPAN = 80

_QUOTES = str.maketrans({
    '‘': "'", '’': "'", '‚': "'", '‛': "'",
    '“': '"', '”': '"', '„': '"', '‟': '"',
    ' ': ' ', ' ': ' ', ' ': ' ',
    '‐': '-', '‑': '-', '‒': '-', '–': '-', '—': '-', '―': '-',
    '­': '',  # soft hyphen (Kobo hyphenation / kepubify)
})


_SOFT = '\x00'  # soft hyphens become this (same length), then are dropped
_SPACES = {c: ' ' for c in map(chr, range(0x3000)) if c.isspace()}
_FOLD = str.maketrans({**{k: v for k, v in _QUOTES.items() if v != ''}, **_SPACES, 0xAD: _SOFT})
# Only places where offsets shift: leading blanks, runs of 2+ spaces, soft hyphens.
_SHIFTS = re.compile(r'^[ \x00]+|[ \x00]*\x00[ \x00]*| {2,}')


class NormMap:
    """norm index -> raw index, stored as breakpoints instead of one int per char."""

    __slots__ = ('starts', 'deltas', 'n', 'raw_len')

    def __init__(self, starts, deltas, n, raw_len):
        self.starts, self.deltas, self.n, self.raw_len = starts, deltas, n, raw_len

    def __getitem__(self, i):
        if i >= self.n:
            return self.raw_len
        return i + self.deltas[bisect_right(self.starts, i) - 1]

    def __len__(self):
        return self.n + 1


class ListMap(list):
    pass


def normalize_with_map(text):
    """Normalized text + map from each normalized char to its raw index.

    Quotes/dashes unified, soft hyphens dropped, whitespace runs collapsed to
    one space, lowercased. Built from large slices with C-level str methods;
    the map only records where offsets shift.
    """
    t = text.translate(_FOLD)
    lowered = t.lower()
    if len(lowered) != len(t):  # rare: a char that lowercases to two chars
        return _normalize_charwise(t)
    pieces, starts, deltas = [], [0], [0]
    n = last = 0
    for m in _SHIFTS.finditer(lowered):
        a, b = m.span()
        pieces.append(lowered[last:a])
        n += a - last
        seg = m.group()
        if ' ' in seg and a > 0:  # keep one space, mapped to the first blank
            starts.append(n)
            deltas.append(a + seg.index(' ') - n)
            pieces.append(' ')
            n += 1
        starts.append(n)
        deltas.append(b - n)
        last = b
    pieces.append(lowered[last:])
    n += len(lowered) - last
    norm = ''.join(pieces)
    if norm.endswith(' '):
        norm = norm[:-1]
        n -= 1
    return norm, NormMap(starts, deltas, n, len(text))


def _normalize_charwise(t):
    out, idx = [], ListMap()
    prev_space = True
    for i, ch in enumerate(t):
        if ch == _SOFT:
            continue
        if ch.isspace():
            if prev_space:
                continue
            ch, prev_space = ' ', True
        else:
            prev_space = False
        for c in ch.lower():
            out.append(c)
            idx.append(i)
    if out and out[-1] == ' ':
        out.pop(), idx.pop()
    idx.append(len(t))
    return ''.join(out), idx


def normalize(text):
    return normalize_with_map(text or '')[0]


def chapter_norm(chapter):
    """Cached (normalized text, index map) for a chapter."""
    cached = getattr(chapter, '_norm', None)
    if cached is None:
        cached = chapter._norm = normalize_with_map(chapter.text)
    return cached


# --- Kobo location <-> chapter position ------------------------------------

_KEPUB_PATH = re.compile(r'span#(kobo(?:\\?\.\d+){2})')
_EPUB_POINT = re.compile(r'^(?P<href>[^#]*)#point\((?P<path>[/\d]+):(?P<off>\d+)\)$')


def chapter_href_from_row(book, row):
    """Which spine item a Kobo Bookmark row lives in."""
    cid = row.get('ContentID') or ''
    if book.is_kepub and '!' in cid:
        # '<book>.kepub.epub!<opf dir>!<path relative to opf dir>'
        _, opf_dir, rel = (cid.split('!', 2) + ['', ''])[:3]
        return book.find_href('/'.join(p for p in (opf_dir, rel) if p))
    m = _EPUB_POINT.match(row.get('StartContainerPath') or '')
    if m:
        return book.find_href(m.group('href'))
    if '#(' in cid:  # '<book>#(<n>)<path>'
        return book.find_href(cid.split(')', 1)[1])
    return None


def kobo_point_to_pos(chapter, container_path, offset):
    """Chapter text position for one Kobo (path, offset) pair, or None."""
    m = _KEPUB_PATH.search(container_path or '')
    if m:
        span_id = m.group(1).replace('\\', '')
        for r in chapter.runs:
            if r.span_id == span_id:
                return kobo_offset_to_pos(r, chapter.text, offset)
        return None
    m = _EPUB_POINT.match(container_path or '')
    if m:
        for r in chapter.runs:
            if r.point_path == m.group('path'):
                return kobo_offset_to_pos(r, chapter.text, offset)
    return None


def resolve_row(book, row):
    """(chapter, start, end) a Kobo row points at in ``book``, or None."""
    href = chapter_href_from_row(book, row)
    if not href:
        return None
    ch = book.chapter(href)
    s = kobo_point_to_pos(ch, row.get('StartContainerPath'), row.get('StartOffset') or 0)
    e = kobo_point_to_pos(ch, row.get('EndContainerPath'), row.get('EndOffset') or 0)
    if s is None:
        return None
    if e is None or e < s:
        e = s
    return ch, s, e


# --- anchors ------------------------------------------------------------------

def make_anchor(chapter, start, end, highlighted_text=None, dogear=False):
    """Anchor for raw chapter positions [start, end).

    Only a window around the passage is normalized (not the whole chapter),
    so saving a highlight is cheap. 'start' and 'chapter_len' are raw offsets.
    """
    text = chapter.text
    span_end = start + 4 * DOGEAR_SPAN if dogear else end
    pad = 8 * CONTEXT
    while True:
        ws, we = max(0, start - pad), min(len(text), span_end + pad)
        norm, idx = normalize_with_map(text[ws:we])
        ns = _raw_to_norm(idx, start - ws)
        ne = _raw_to_norm(idx, min(span_end, we) - ws)
        enough_before = ns >= CONTEXT or ws == 0
        enough_after = len(norm) - ne >= CONTEXT or we == len(text)
        if (enough_before and enough_after) or pad > len(text):
            break
        pad *= 4  # mostly whitespace around it: widen the window
    if dogear:
        ne = min(len(norm), ns + DOGEAR_SPAN)
    while ns < ne and norm[ns] == ' ':
        ns += 1
    while ne > ns and norm[ne - 1] == ' ':
        ne -= 1
    exact = norm[ns:ne]
    if highlighted_text and normalize(highlighted_text) and normalize(highlighted_text) != exact:
        # The device's Text is the ground truth of what the reader saw.
        exact = normalize(highlighted_text)
    return {
        'href': chapter.href,
        'spine_index': chapter.spine_index,
        'exact': exact,
        'prefix': norm[max(0, ns - CONTEXT):ns],
        'suffix': norm[ne:ne + CONTEXT],
        'start': start,
        'chapter_len': len(text),
        'dogear': dogear,
    }


def anchor_from_text(book, text):
    """Fallback when a row's location can't be resolved: search by text."""
    target = normalize(text)
    if not target:
        return None
    for ch in book.chapters():
        norm, idx = chapter_norm(ch)
        i = norm.find(target)
        if i >= 0:
            return make_anchor(ch, idx[i], idx[i + len(target)])
    return None


def _raw_to_norm(idx, raw_pos):
    # first normalized char whose raw index >= raw_pos
    lo, hi = 0, len(idx) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if idx[mid] < raw_pos:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _context_score(norm, s, e, anchor):
    score = 0
    pre, suf = anchor.get('prefix') or '', anchor.get('suffix') or ''
    got_pre, got_suf = norm[max(0, s - len(pre)):s], norm[e:e + len(suf)]
    score += _common_suffix(pre, got_pre) + _common_prefix(suf, got_suf)
    return score


def _common_prefix(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _common_suffix(a, b):
    return _common_prefix(a[::-1], b[::-1])


class _LazyChapters:
    """Iterable of chapters in a given order, parsed only when reached (and cached by Book)."""

    def __init__(self, book, hrefs):
        self.book, self.hrefs = book, hrefs

    def __iter__(self):
        return (self.book.chapter(h) for h in self.hrefs)


def locate(book, anchor):
    """Find an anchor in ``book``.

    Returns dict(chapter, start, end, method) with raw chapter positions, or
    None when the passage can't be found (the caller reports an orphan and
    never guesses).
    """
    exact = anchor.get('exact') or ''
    if not exact:
        return None
    home = book.find_href(anchor.get('href') or '')
    # Own chapter first; the others are only parsed if it isn't found there.
    order = ([home] if home else []) + [h for h in book.spine if h != home]
    chapters = _LazyChapters(book, order)

    best = None
    for ch in chapters:
        norm, idx = chapter_norm(ch)
        i = norm.find(exact)
        while i >= 0:
            e = i + len(exact)
            score = _context_score(norm, i, e, anchor) * 10
            if ch.href == home:
                score += 5
                # tie-break on distance from the original relative position
                if anchor.get('chapter_len'):
                    rel_then = anchor['start'] / max(1, anchor['chapter_len'])
                    score -= abs(idx[i] / max(1, len(ch.text)) - rel_then)
            cand = (score, ch, idx[i], idx[e], 'exact' if ch.href == home else 'moved')
            if best is None or cand[0] > best[0]:
                best = cand
            i = norm.find(exact, i + 1)
        if best and ch.href == home:
            break  # found in its own chapter: don't read the rest of the book
    if best:
        _, ch, s, e, method = best
        if anchor.get('dogear'):
            e = s
        return {'chapter': ch, 'start': s, 'end': e, 'method': method}

    # Fuzzy: the passage was edited. Seed-and-verify approximate matching.
    best = None
    for ch in chapters:
        norm, idx = chapter_norm(ch)
        hit = fuzzy_find(norm, exact)
        if hit:
            s, e, ratio = hit
            score = ratio * 100 + _context_score(norm, s, e, anchor) + (5 if ch.href == home else 0)
            if best is None or score > best[0]:
                best = (score, ch, idx[s], idx[e])
            if ch.href == home:
                break
    if best:
        _, ch, s, e = best
        if anchor.get('dogear'):
            e = s
        return {'chapter': ch, 'start': s, 'end': e, 'method': 'fuzzy'}
    return None


FUZZY_MIN_LEN = 32
FUZZY_MIN_RATIO = 0.75


def fuzzy_find(norm, exact):
    """Best approximate occurrence of ``exact`` in ``norm``: (start, end, ratio) or None.

    1. Seeds: ~9 short pieces of the passage are looked up with str.find; each
       hit votes for where the passage would start.
    2. Verify: the top candidate spots are aligned with difflib, giving exact
       boundaries and a similarity ratio.
    Tolerates edits anywhere in the passage (start, middle or end).
    """
    n = len(exact)
    if n < FUZZY_MIN_LEN:
        return None  # too short to place safely without an exact match
    # Short passages share too many common phrases with other text: be stricter.
    min_ratio = FUZZY_MIN_RATIO if n >= 80 else 0.85
    k = 8 if n < 48 else 12
    step = max(1, (n - k) // 8)
    votes = {}
    for off in range(0, n - k + 1, step):
        seed = exact[off:off + k]
        i, hits = norm.find(seed), 0
        while i >= 0 and hits < 20:
            bucket = (i - off) // 16
            votes[bucket] = votes.get(bucket, 0) + 1
            i, hits = norm.find(seed, i + 1), hits + 1
    candidates = sorted((v, b) for b, v in votes.items() if v >= 2)[-3:]
    best = None
    for _, bucket in candidates:
        c = bucket * 16
        a, b = max(0, c - n // 2 - 16), min(len(norm), c + n + n // 2 + 16)
        blocks = [m for m in SequenceMatcher(None, exact, norm[a:b], autojunk=False).get_matching_blocks()
                  if m.size]
        if not blocks:
            continue
        # Keep blocks on the same alignment as the largest one; stray short
        # matches of common letters elsewhere in the window are dropped.
        main = max(blocks, key=lambda m: m.size)
        diag, slack = main.b - main.a, max(12, n // 4)
        blocks = [m for m in blocks if m.size >= 3 and abs((m.b - m.a) - diag) <= slack]
        s, e = a + blocks[0].b, a + blocks[-1].b + blocks[-1].size
        if not (0.5 * n <= e - s <= 1.5 * n):
            continue
        ratio = SequenceMatcher(None, exact, norm[s:e], autojunk=False).ratio()
        if ratio >= min_ratio and (best is None or ratio > best[2]):
            best = (s, e, ratio)
    return best


# --- back to Kobo coordinates ---------------------------------------------

def _run_for(chapter, pos, at_end):
    runs = chapter.runs
    if not runs:
        return None
    if at_end:
        for r in reversed(runs):
            if r.start < pos <= r.end:
                return r
        prev = [r for r in runs if r.end <= pos]
        return prev[-1] if prev else runs[0]
    for r in runs:
        if r.start <= pos < r.end:
            return r
    nxt = [r for r in runs if r.start >= pos]
    return nxt[0] if nxt else runs[-1]


def container_path(book, chapter, run):
    if run.kind == 'span':
        return 'span#' + run.span_id.replace('.', '\\.')
    return f'{chapter.href}#point({run.point_path}'


def to_kobo_location(book, chapter, start, end):
    """Kobo Bookmark location columns for chapter positions [start, end)."""
    rs = _run_for(chapter, start, at_end=False)
    re_ = _run_for(chapter, max(end, start), at_end=end > start)
    s_pos = max(start, rs.start)
    e_pos = min(max(end, start), re_.end) if end > start else s_pos
    s_off = run_offset_to_kobo(rs, chapter.text, s_pos)
    e_off = run_offset_to_kobo(re_, chapter.text, e_pos)
    loc = {
        'StartContainerPath': container_path(book, chapter, rs),
        'StartOffset': s_off,
        'EndContainerPath': container_path(book, chapter, re_),
        'EndOffset': e_off,
        'ChapterProgress': round(start / max(1, len(chapter.text)), 6),
    }
    if rs.kind == 'text':
        loc['StartContainerPath'] += f':{s_off})'
        loc['EndContainerPath'] += f':{e_off})'
    return loc


# --- calibre viewer positions --------------------------------------------

def calibre_cfi(chapter, pos, at_end=False):
    """calibre-viewer CFI (within the spine item) for a chapter position, or None."""
    run = _run_for(chapter, pos, at_end)
    if run is None or not run.cfi_ok or run.cfi_el is None:
        return None
    p = min(max(pos, run.start), run.end)
    if not at_end and p == run.start and chapter.cfi_base(run) > 0:
        # calibre counts only a parent's direct text, so "start of the text
        # after an inline element" encodes the same as "end of the text
        # before it" and decodes to the latter. Use the identical caret
        # position "end of the text inside that element" instead.
        i = chapter.runs.index(run)
        if i > 0 and chapter.runs[i - 1].end == run.start and chapter.runs[i - 1].cfi_ok:
            return calibre_cfi(chapter, run.start, at_end=True)
    return chapter.cfi_for(run, u16(chapter.text[run.start:p]))


def calibre_position_at(ch, start, end):
    """calibre-viewer position for known chapter offsets, or None."""
    s = calibre_cfi(ch, start)
    e = calibre_cfi(ch, end, at_end=True) if end > start else s
    if not s or not e:
        return None
    return {'spine_index': ch.spine_index, 'spine_name': ch.href, 'start_cfi': s, 'end_cfi': e}


def calibre_position(book, anchor):
    """{'spine_index', 'spine_name', 'start_cfi', 'end_cfi'} for an anchor in ``book``, or None."""
    m = locate(book, anchor)
    return calibre_position_at(m['chapter'], m['start'], m['end']) if m else None
