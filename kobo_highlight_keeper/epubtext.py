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


@dataclass
class Chapter:
    href: str           # path inside the zip, e.g. 'OEBPS/Text/ch01.xhtml'
    spine_index: int
    text: str = ''
    runs: list = field(default_factory=list)

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

        body = root.find(f'{XHTML_NS}body')
        if body is None:
            body = root.find('body')
        if body is not None:
            body_index = list(root).index(body)
            _walk(body, f'/1/{2 * (body_index + 1)}', emit, self.is_kepub)
        ch.text = ''.join(parts)
        return ch


BLOCK_TAGS = {
    'p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote', 'section',
    'article', 'aside', 'header', 'footer', 'pre', 'tr', 'td', 'th', 'dt', 'dd',
    'figure', 'figcaption', 'table', 'ul', 'ol', 'hr',
}
SKIP_TAGS = {'script', 'style', 'head'}


def _walk(el, path, emit, kepub):
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
    if kepub and name == 'span' and 'koboSpan' in (el.get('class') or '') \
            and KOBO_SPAN_RE.match(el.get('id') or ''):
        emit(''.join(el.itertext()), 'span', span_id=el.get('id'))
    else:
        emit(el.text, loose, point_path=f'{path}/1')
        for i, child in enumerate(el):
            _walk(child, f'{path}/{2 * (i + 1)}', emit, kepub)
            emit(child.tail, loose, point_path=f'{path}/{2 * (i + 1) + 1}')
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
