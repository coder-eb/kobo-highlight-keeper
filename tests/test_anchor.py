"""Anchor / location tests against real books copied into the sandbox.

Run:  python3 -m unittest discover -s tests -v
(needs `python3 harness/setup_sandbox.py` once for the book fixtures)
"""

import glob
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kobo_highlight_keeper import anchor as A  # noqa: E402
from kobo_highlight_keeper.epubtext import Book  # noqa: E402

BOOKS = ROOT / 'sandbox' / 'source_books'
N = 60


def books(pattern):
    return [Book(f) for f in sorted(glob.glob(str(BOOKS / pattern)))]


def chapter_row(book, ch, loc):
    if book.is_kepub:
        cid = f'/mnt/onboard/x.kepub.epub!{book.opf_dir}!{book.opf_relative(ch.href)}'
    else:
        cid = f'file:///mnt/onboard/x.epub#({ch.spine_index}){ch.href}'
    return dict(loc, ContentID=cid)


@unittest.skipUnless(BOOKS.exists(), 'run harness/setup_sandbox.py first')
class TestAnchors(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(42)

    def sample(self, book):
        """A random selection that contains at least one visible character."""
        chs = [c for c in book.chapters() if len(c.text) > 2000]
        while True:
            ch = self.rng.choice(chs)
            r = self.rng.choice(ch.runs)
            s = self.rng.randrange(r.start, r.end)
            e = min(len(ch.text), s + self.rng.randrange(3, 250))
            if A.normalize(ch.text[s:e]).strip():
                return ch, s, e

    def test_kobo_location_roundtrip(self):
        for b in books('*/*.kepub') + books('*/*.epub'):
            for _ in range(N):
                ch, s, e = self.sample(b)
                row = chapter_row(b, ch, A.to_kobo_location(b, ch, s, e))
                got = A.resolve_row(b, row)
                self.assertIsNotNone(got, b.title)
                self.assertEqual(A.normalize(ch.text[got[1]:got[2]]), A.normalize(ch.text[s:e]))

    def test_locate_same_file(self):
        for b in books('*/*.kepub') + books('*/*.epub'):
            for _ in range(N):
                ch, s, e = self.sample(b)
                an = A.make_anchor(ch, s, e)
                m = A.locate(b, an)
                self.assertEqual(m['method'], 'exact')
                self.assertEqual(m['chapter'].href, ch.href)
                self.assertEqual(A.normalize(ch.text[m['start']:m['end']]), an['exact'])

    def test_locate_across_formats(self):
        """A highlight made on the KEPUB is found in the EPUB and vice versa."""
        for d in ('890', '859'):
            ep, = books(f'{d}/*.epub')
            kp, = books(f'{d}/*.kepub')
            for src, dst in ((kp, ep), (ep, kp)):
                found = 0
                for _ in range(N):
                    ch, s, e = self.sample(src)
                    an = A.make_anchor(ch, s, e)
                    m = A.locate(dst, an)
                    if m and A.normalize(m['chapter'].text[m['start']:m['end']]) == an['exact']:
                        found += 1
                self.assertGreaterEqual(found / N, 0.95, f'{src.title}: {found}/{N}')

    def test_edited_chapter_reanchors(self):
        """Text inserted before a highlight shifts kobo span ids; the anchor survives."""
        b, = books('852/*.kepub')
        ch, s, e = self.sample(b)
        an = A.make_anchor(ch, s, e)
        ch.text = 'A NEW PARAGRAPH ADDED BY AN EDIT.\n' + ch.text
        ch._norm = None
        shift = len('A NEW PARAGRAPH ADDED BY AN EDIT.\n')
        for r in ch.runs:
            r.start += shift
            r.end += shift
        m = A.locate(b, an)
        self.assertEqual(A.normalize(ch.text[m['start']:m['end']]), an['exact'])

    def test_fuzzy_when_middle_edited(self):
        b, = books('852/*.kepub')
        ch = max(b.chapters(), key=lambda c: len(c.text))
        an = A.make_anchor(ch, 1000, 1200)
        an = dict(an, exact=an['exact'][:90] + ' typo fixed ' + an['exact'][100:])
        m = A.locate(b, an)
        self.assertIsNotNone(m)
        self.assertEqual(m['method'], 'fuzzy')

    def test_unfindable_text_is_orphan(self):
        b, = books('852/*.kepub')
        an = {'href': b.spine[3], 'exact': 'this sentence is not in the book at all, anywhere',
              'prefix': '', 'suffix': '', 'start': 0, 'chapter_len': 1}
        self.assertIsNone(A.locate(b, an))


if __name__ == '__main__':
    unittest.main()
