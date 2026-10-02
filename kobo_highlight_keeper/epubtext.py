"""Read EPUB / KEPUB books as chapter text with Kobo-native locators.

A chapter becomes one flat string plus a list of *runs*. Each run is a slice
of that string that maps back to one addressable node in the XHTML:

* KEPUB: a ``<span class="koboSpan" id="kobo.P.S">`` -- Kobo addresses
  highlights as ``span#kobo\\.P\\.S`` + a character offset inside the span.
* EPUB: a DOM text node -- Kobo addresses highlights as
  ``<href>#point(/1/<cfi steps>/<odd text index>:<offset>)``.

Pure stdlib so it runs both inside calibre and as a plain script.
"""

import html.entities
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

XHTML_NS = '{http://www.w3.org/1999/xhtml}'
OPF_NS = '{http://www.idpf.org/2007/opf}'
DC_NS = '{http://purl.org/dc/elements/1.1/}'
KOBO_SPAN_RE = re.compile(r'^kobo\.(\d+)\.(\d+)$')

# ASSUMPTION (unverified on hardware): Kobo's ADE-style epub #point() offsets
# count UTF-8 bytes (observed by valeriangalliat/kobo-highlights-to-calibre);
# koboSpan offsets count characters. Kept in one place so a device test can
# flip it.
EPUB_POINT_OFFSET_UNIT = 'bytes'
PARAGRAPH_SEP = '\n'


@dataclass
class Run:
    start: int          # offset in Chapter.text
    end: int
    kind: str           # 'span' (kepub) or 'text' (epub)
    span_id: str = ''   # kepub: 'kobo.12.3'
    point_path: str = ''  # epub: '/1/4/2/26/1'
    # for calibre viewer CFIs (built on demand, see Chapter.cfi_for):
    # the parent element of this text and which of its text nodes it is
    # (-1 = parent.text, i = tail of the parent's i-th child)
    cfi_el: object = None
    cfi_child: int = -1
    cfi_ok: bool = True


@dataclass
class Chapter:
    href: str           # path inside the zip, e.g. 'OEBPS/Text/ch01.xhtml'
    spine_index: int
    text: str = ''
    runs: list = field(default_factory=list)
    ids: dict = field(default_factory=dict)  # element id -> offset in text
    root: object = None

    def cfi_for(self, run, offset16):
        """calibre-viewer CFI for a UTF-16 offset into ``run`` (paths built lazily, cached)."""
        if not hasattr(self, '_cfi_cache'):
            counts = {}
            for el in self.root.iter():
                if el.get('id'):
                    counts[el.get('id')] = counts.get(el.get('id'), 0) + 1
            self._unique = {i for i, n in counts.items() if n == 1}
            self._parents = {c: p for p in self.root.iter() for c in p}
            self._cfi_cache = {}
        el = run.cfi_el
        # calibre counts a parent's direct text from its first child node
        first = '/1' if el.text or not len(el) else '/2' + _idspec(el[0], self._unique)
        return f'{self._path(el)}{first}:{self.cfi_base(run) + offset16}'

    @staticmethod
    def cfi_base(run):
        """UTF-16 length of the parent's direct text before this run's text node."""
        el = run.cfi_el
        if run.cfi_child < 0:
            return 0
        return u16(el.text or '') + sum(u16(el[i].tail or '') for i in range(run.cfi_child))

    def _path(self, el):
        cached = self._cfi_cache.get(el)
        if cached is None:
            parent = self._parents.get(el)
            if parent is self.root or parent is None:
                # calibre parses with an HTML5 parser, so <head> always exists
                # and <body> is always the 2nd element of <html>: '/2/4'
                cached = '/2' + _idspec(self.root, self._unique) + '/4' + _idspec(el, self._unique)
            else:
                cached = f'{self._path(parent)}/{2 * (list(parent).index(el) + 1)}{_idspec(el, self._unique)}'
            self._cfi_cache[el] = cached
        return cached

    def run_at(self, pos, prefer_end=False):
        """The run containing flat-text offset ``pos``."""
        best = None
        for r in self.runs:
            if r.start <= pos < r.end or (prefer_end and r.start < pos <= r.end):
                return r
            if r.start >= pos and best is None:
                best = r
        return best or (self.runs[-1] if self.runs else None)


class Book:
    def __init__(self, path):
        self.path = str(path)
        self.zf = zipfile.ZipFile(self.path)
        self.is_kepub = self._detect_kepub()
        container = ET.fromstring(self.zf.read('META-INF/container.xml'))
        rootfile = next(e for e in container.iter() if e.tag.endswith('rootfile'))
        self.opf_path = rootfile.get('full-path')
        self.opf_dir = posixpath.dirname(self.opf_path)
        self._parse_opf()
        self._chapters = {}

    def _detect_kepub(self):
        if self.path.lower().endswith(('.kepub', '.kepub.epub')):
            return True
        return any(b'koboSpan' in self.zf.read(n)
                   for n in self.zf.namelist()[:40] if n.endswith(('.xhtml', '.html', '.htm')))

    def _parse_opf(self):
        opf = ET.fromstring(self.zf.read(self.opf_path))
        self.title = (opf.findtext(f'.//{DC_NS}title') or '').strip()
        self.authors = [a.text.strip() for a in opf.iter(f'{DC_NS}creator') if a.text]
        manifest = {i.get('id'): i for i in opf.iter(f'{OPF_NS}item')}
        self.manifest = manifest
        spine_el = opf.find(f'{OPF_NS}spine')
        self.ncx_id = spine_el.get('toc') if spine_el is not None else None
        self._toc = None
        self.spine = []
        for ref in opf.iter(f'{OPF_NS}itemref'):
            item = manifest.get(ref.get('idref'))
            if item is None or 'html' not in (item.get('media-type') or ''):
                continue
            self.spine.append(posixpath.normpath(posixpath.join(self.opf_dir, item.get('href'))))

    def opf_relative(self, href):
        return posixpath.relpath(href, self.opf_dir) if self.opf_dir else href

    def chapter(self, href):
        if href not in self._chapters:
            self._chapters[href] = self._load_chapter(href)
        return self._chapters[href]

    def chapters(self):
        return [self.chapter(h) for h in self.spine]

    def find_href(self, suffix):
        """Resolve a chapter reference that may be OPF- or zip-relative."""
        suffix = suffix.lstrip('/')
        for h in self.spine:
            if h == suffix or h.endswith('/' + suffix) or self.opf_relative(h) == suffix:
                return h
        return None

    # --- table of contents ---------------------------------------------------
    def toc(self):
        """[(zip href, fragment, (title, ..., title))] in reading order (nav, else NCX)."""
        if self._toc is None:
            self._toc = []
            try:
                nav = next((i for i in self.manifest.values() if 'nav' in (i.get('properties') or '').split()), None)
                if nav is not None:
                    self._parse_nav(posixpath.normpath(posixpath.join(self.opf_dir, nav.get('href'))))
                elif self.ncx_id in self.manifest:
                    self._parse_ncx(posixpath.normpath(posixpath.join(self.opf_dir, self.manifest[self.ncx_id].get('href'))))
            except Exception:
                self._toc = []  # a broken TOC only costs chapter names
        return self._toc

    def _add_toc(self, base, src, path):
        target, _, frag = (src or '').partition('#')
        if target:
            href = posixpath.normpath(posixpath.join(posixpath.dirname(base), target))
            self._toc.append((href, frag, tuple(path)))

    def _parse_nav(self, href):
        root = parse_xhtml(self.zf.read(href))
        ops_type = '{http://www.idpf.org/2007/ops}type'
        navs = [n for n in root.iter() if n.tag.endswith('nav')]
        nav = next((n for n in navs if n.get(ops_type) == 'toc'), navs[0] if navs else None)

        def walk_ol(ol, path):
            for li in (c for c in ol if c.tag.endswith('li')):
                a = next((c for c in li if c.tag.endswith(('a', 'span'))), None)
                title = ' '.join(''.join(a.itertext()).split()) if a is not None else ''
                here = path + [title] if title else path
                if a is not None and a.get('href'):
                    self._add_toc(href, a.get('href'), here)
                for sub in (c for c in li if c.tag.endswith('ol')):
                    walk_ol(sub, here)
        if nav is not None:
            for ol in (c for c in nav if c.tag.endswith('ol')):
                walk_ol(ol, [])

    def _parse_ncx(self, href):
        root = ET.fromstring(self.zf.read(href))

        def walk(el, path):
            for np_ in (c for c in el if c.tag.endswith('navPoint')):
                label = next((t for t in np_.iter() if t.tag.endswith('text')), None)
                title = ' '.join((label.text or '').split()) if label is not None else ''
                here = path + [title] if title else path
                content = next((c for c in np_ if c.tag.endswith('content')), None)
                if content is not None:
                    self._add_toc(href, content.get('src'), here)
                walk(np_, here)
        nav_map = next((c for c in root if c.tag.endswith('navMap')), None)
        if nav_map is not None:
            walk(nav_map, [])

    def toc_titles(self, href, pos):
        """Chapter path (outermost first) for raw offset ``pos`` in spine item ``href``."""
        entries = self.toc()
        if not entries:
            return []
        here = [(i, e) for i, e in enumerate(entries) if e[0] == href]
        if here:
            ch = self.chapter(href)
            best = None
            for i, (_, frag, titles) in here:
                at = ch.ids.get(frag, 0) if frag else 0
                if at <= pos and (best is None or (at, i) >= best[0]):
                    best = ((at, i), titles)
            if best:
                return list(best[1])
        # No entry in this file (or none before pos): the chapter it continues.
        order = {h: n for n, h in enumerate(self.spine)}
        mine = order.get(href, -1)
        before = [(order.get(e[0], -1), i, e[2]) for i, e in enumerate(entries) if order.get(e[0], -1) < mine]
        return list(max(before)[2]) if before else []

    def _load_chapter(self, href):
        root = parse_xhtml(self.zf.read(href))
        ch = Chapter(href=href, spine_index=self.spine.index(href) if href in self.spine else -1)
        parts = []
        pos = 0

        def emit(text, kind, **kw):
            nonlocal pos
            if not text:
                return
            parts.append(text)
            if kind != 'gap':  # gaps are searchable but not addressable
                ch.runs.append(Run(pos, pos + len(text), kind, **kw))
            pos += len(text)

        def mark(el_id):
            ch.ids.setdefault(el_id, pos)

        ch.root = root
        body = root.find(f'{XHTML_NS}body')
        if body is None:
            body = root.find('body')
        if body is not None:
            body_index = list(root).index(body)
            _walk(body, f'/1/{2 * (body_index + 1)}', emit, self.is_kepub, mark)
        ch.text = ''.join(parts)
        return ch


BLOCK_TAGS = {
    'p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote', 'section',
    'article', 'aside', 'header', 'footer', 'pre', 'tr', 'td', 'th', 'dt', 'dd',
    'figure', 'figcaption', 'table', 'ul', 'ol', 'hr',
}
SKIP_TAGS = {'script', 'style', 'head'}


_CFI_ESCAPE = re.compile(r'([\[\],^();~@!])')


def _idspec(el, unique):
    """calibre adds an [id] assertion to steps for elements with a unique id."""
    i = el.get('id')
    if not i or i not in unique:
        return ''
    return '[' + _CFI_ESCAPE.sub(r'^\1', i) + ']'


def u16(text):
    """Length as JavaScript counts it (UTF-16 code units)."""
    return len(text.encode('utf-16-le')) // 2


def _walk(el, path, emit, kepub, mark):
    """Emit text in document order.

    EPUB: every DOM text node is a run addressed by a Kobo/CFI-style path
    (element children even, text nodes odd). KEPUB: each koboSpan is one run;
    any text outside spans (inter-span spaces) is an unaddressable gap.
    Block boundaries add a newline gap so words never fuse across paragraphs.
    """
    name = el.tag.rsplit('}', 1)[-1].lower()
    if name in SKIP_TAGS:
        return
    loose = 'gap' if kepub else 'text'
    if name in BLOCK_TAGS:
        emit(PARAGRAPH_SEP, 'gap')
    if el.get('id'):
        mark(el.get('id'))
    if kepub and name == 'span' and 'koboSpan' in (el.get('class') or '') \
            and KOBO_SPAN_RE.match(el.get('id') or ''):
        emit(''.join(el.itertext()), 'span', span_id=el.get('id'), cfi_el=el, cfi_ok=not len(el))
    else:
        emit(el.text, loose, point_path=f'{path}/1', cfi_el=el)
        for i, child in enumerate(el):
            _walk(child, f'{path}/{2 * (i + 1)}', emit, kepub, mark)
            emit(child.tail, loose, point_path=f'{path}/{2 * (i + 1) + 1}', cfi_el=el, cfi_child=i)
    if name in BLOCK_TAGS or name == 'br':
        emit(PARAGRAPH_SEP, 'gap')


_ENTITY_RE = re.compile(r'&([A-Za-z][A-Za-z0-9]*);')
_XML_ENTITIES = {'amp', 'lt', 'gt', 'quot', 'apos'}


def parse_xhtml(raw):
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        # Named HTML entities (&nbsp; ...) are not valid XML: resolve them.
        text = raw.decode('utf-8', 'replace')

        def sub(m):
            name = m.group(1)
            if name in _XML_ENTITIES:
                return m.group(0)
            cp = html.entities.name2codepoint.get(name)
            return chr(cp) if cp else m.group(0)
        return ET.fromstring(_ENTITY_RE.sub(sub, text).encode('utf-8'))


def run_offset_to_kobo(run, chapter_text, pos):
    """Offset of flat-text ``pos`` inside ``run`` in Kobo's unit."""
    chars = pos - run.start
    if run.kind == 'text' and EPUB_POINT_OFFSET_UNIT == 'bytes':
        return len(chapter_text[run.start:run.start + chars].encode('utf-8'))
    return chars


def kobo_offset_to_pos(run, chapter_text, offset):
    if run.kind == 'text' and EPUB_POINT_OFFSET_UNIT == 'bytes':
        raw = chapter_text[run.start:run.end].encode('utf-8')[:offset]
        return run.start + len(raw.decode('utf-8', 'ignore'))
    return run.start + min(offset, run.end - run.start)
