"""Check our calibre-viewer CFIs the way calibre's viewer would read them.

  calibre-debug -e harness/verify_cfi.py            (books in sandbox/source_books)

For random passages in every book: compute start/end CFIs with
kobo_highlight_keeper, then decode them with a line-by-line port of
calibre's viewer decoder (src/pyj/read_book/cfi.pyj: decode,
node_for_path_step, node_for_text_offset) on the chapter as calibre itself
parses it (the polish container, the same parser the viewer uses). The
decoded range must contain exactly the passage's text.
"""

import glob
import os
import random
import re
import sys

from calibre.ebooks.oeb.polish.container import Container
from calibre.ptempfile import TemporaryDirectory
from calibre.utils.logging import default_log
from calibre.utils.zipfile import ZipFile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from kobo_highlight_keeper import anchor as A  # noqa: E402
from kobo_highlight_keeper.epubtext import Book, u16  # noqa: E402

STEP = re.compile(r'^/(\d+)(\[[^\]]*\])?')
UNESCAPE = re.compile(r'\^(.)')


def is_el(node):
    return node[0] == 'el'


def child_nodes(el):
    """DOM childNodes for an lxml element: ('el', e) | ('other', e) | ('text', s, owner, which)."""
    out = []
    if el.text:
        out.append(('text', el.text, el, 'text'))
    for c in el:
        out.append(('el', c) if isinstance(c.tag, str) else ('other', c))
        if c.tail:
            out.append(('text', c.tail, c, 'tail'))
    return out


def same(a, b):
    return a[0] == b[0] and a[1:] == b[1:] if a[0] == 'text' else a[0] == b[0] and a[1] is b[1]


def node_at_index(nodes, target, iter_text):
    index = 0
    for n in nodes:
        if (iter_text and n[0] != 'text') or (not iter_text and not is_el(n)):
            continue
        if index == target:
            return n
        index += 1
    return None


def unique_ids(root):
    counts = {}
    for e in root.iter('*'):
        if e.get('id'):
            counts[e.get('id')] = counts.get(e.get('id'), 0) + 1
    return {i for i, c in counts.items() if c == 1}


def node_for_path_step(root, parent_nodes, target, assertion, unique):
    if assertion and assertion in unique:
        return ('el', next(e for e in root.iter('*') if e.get('id') == assertion))
    is_element = target % 2 == 0
    target //= 2
    if is_element and target > 0:
        target -= 1
    return node_at_index(parent_nodes, target, not is_element)


def decode(cfi, root, unique):
    """(text node, utf16 offset) the CFI points at, like calibre's decode()."""
    node, parent_nodes = ('doc',), [('el', root)]
    parent = None
    while True:
        m = STEP.match(cfi)
        if not m:
            break
        assertion = UNESCAPE.sub(r'\1', m.group(2)[1:-1]) if m.group(2) else None
        q = node_for_path_step(root, parent_nodes, int(m.group(1)), assertion, unique)
        if q is None:
            raise ValueError(f'no child for step in {cfi}')
        parent, node = (node[1] if node[0] == 'el' else None), q
        parent_nodes = child_nodes(q[1]) if is_el(q) else []
        cfi = cfi[m.end():]
    m = re.match(r'^:(\d+)$', cfi)
    if not m:
        raise ValueError(f'undecoded: {cfi}')
    offset = int(m.group(1))
    # node_for_text_offset(node.parentNode.childNodes, offset, node)
    owner = node[1].getparent() if is_el(node) else node[2] if node[3] == 'text' else node[2].getparent()
    seen, last = False, None
    for n in child_nodes(owner):
        if not seen:
            if same(n, node):
                seen = True
            else:
                continue
        if n[0] == 'text':
            ln = u16(n[1])
            if offset <= ln:
                return n, offset
            last, offset = n, offset - ln
    raise ValueError('offset out of range')


def py_index(text, off16):
    j, acc = 0, 0
    while acc < off16 and j < len(text):
        acc += u16(text[j])
        j += 1
    return j


def text_between(root, start, end):
    """All document text from (node, off) to (node, off), in document order."""
    flat = []

    def walk(el):
        if el.text:
            flat.append(('text', el.text, el, 'text'))
        for c in el:
            if isinstance(c.tag, str):
                walk(c)
            if c.tail:
                flat.append(('text', c.tail, c, 'tail'))
    walk(root)
    si = next(i for i, n in enumerate(flat) if same(n, start[0]))
    ei = next(i for i, n in enumerate(flat) if same(n, end[0]))
    if si == ei:
        t = flat[si][1]
        return t[py_index(t, start[1]):py_index(t, end[1])]
    parts = [flat[si][1][py_index(flat[si][1], start[1]):]]
    parts += [n[1] for n in flat[si + 1:ei]]
    parts.append(flat[ei][1][:py_index(flat[ei][1], end[1])])
    return ''.join(parts)


def main():
    rng = random.Random(5)
    files = sorted(glob.glob(os.path.join(ROOT, 'sandbox', 'source_books', '*', '*.*pub')))
    total_ok = total_bad = 0
    for f in files:
        book = Book(f)
        with TemporaryDirectory() as tdir:
            ZipFile(f).extractall(tdir)
            cont = Container(tdir, os.path.join(tdir, book.opf_path), default_log)
            parsed, uniques = {}, {}
            chs = [c for c in book.chapters() if len(c.text) > 2000]
            ok = bad = 0
            for _ in range(int(os.environ.get('KHK_SAMPLES', '400'))):
                ch = rng.choice(chs)
                r = rng.choice(ch.runs)
                s = rng.randrange(r.start, r.end)
                e = min(len(ch.text), s + rng.randrange(5, 300))
                an = A.make_anchor(ch, s, e)
                if not an['exact']:
                    continue
                pos = A.calibre_position(book, an)
                if pos is None:
                    bad += 1
                    print('  no position for', repr(an['exact'][:50]))
                    continue
                name = pos['spine_name']
                if name not in parsed:
                    parsed[name] = cont.parsed(name)
                    uniques[name] = unique_ids(parsed[name])
                root = parsed[name]
                try:
                    got = text_between(root, decode(pos['start_cfi'], root, uniques[name]),
                                       decode(pos['end_cfi'], root, uniques[name]))
                except Exception as ex:
                    got = f'<error {ex}>'
                if A.normalize(got) == an['exact']:
                    ok += 1
                else:
                    bad += 1
                    if bad <= 3:
                        from lxml import etree
                        el = root
                        for step in re.findall(r'/(\d+)', pos['start_cfi'].split(':')[0])[1:-1]:
                            el = [c for c in el if isinstance(c.tag, str)][int(step) // 2 - 1]
                        print('    calibre parent:', etree.tostring(el, encoding='unicode', with_tail=False)[:400])
                        print(f'  MISMATCH in {name} {pos["start_cfi"]} .. {pos["end_cfi"]}\n'
                              f'    want {an["exact"][:70]!r}\n    got  {A.normalize(got)[:70]!r}')
        print(f'{os.path.basename(f)[:45]:45} {ok} ok, {bad} bad')
        total_ok += ok
        total_bad += bad
    print(f'TOTAL {total_ok} ok, {total_bad} bad')


main()
