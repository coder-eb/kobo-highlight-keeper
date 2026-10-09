"""Importing highlights stored by the calibre Annotations plugin."""

import glob
import html
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kobo_highlight_keeper import anchor as A  # noqa: E402
from kobo_highlight_keeper import legacy  # noqa: E402
from kobo_highlight_keeper.epubtext import Book  # noqa: E402

BOOKS = ROOT / 'sandbox' / 'source_books'


def legacy_html(items):
    """HTML in the Annotations plugin's format: [(text, note, uts)]."""
    out = ['<html><head></head><body><div class="user_annotations" style="margin:0">']
    for i, (text, note, uts) in enumerate(items):
        out.append(
            f'<div class="annotation" genre="" hash="h{i}" location_sort="{i:08d}" reader="KoboTouch">'
            '<table cellpadding="0" width="100%"><tbody><tr>'
            '<td class="location" style="text-align:left">1 · Chapter</td>'
            f'<td class="timestamp" style="text-align:right" uts="{uts}">Sat Feb  7 16:07:00 2026</td>'
            '</tr></tbody></table>'
            f'<p class="highlight" style="margin:0;">{html.escape(text)}</p>'
            + (f'<p class="note" style="margin:0;">{html.escape(note)}<br/>second line</p>' if note else '')
            + '</div>')
    out.append('</div></body></html>')
    return ''.join(out)


def joined_paragraphs(book):
    """Two consecutive paragraphs run together without a space, as the old plugin stored them."""
    ch = max(book.chapters(), key=lambda c: len(c.text))
    paras = [p.strip() for p in ch.text.split('\n') if len(p.strip()) > 60]
    return paras[3][-40:].strip() + paras[4][:40].strip()


@unittest.skipUnless(BOOKS.exists(), 'run harness/setup_sandbox.py first')
class TestLegacyImport(unittest.TestCase):
    def setUp(self):
        self.book = Book(glob.glob(str(BOOKS / '852' / '*.kepub'))[0])

    def test_parse(self):
        items = legacy.parse(legacy_html([('Plain & simple', '', '1770460620.0'),
                                          ('With a note', 'my note', '1770460667.0')]))
        self.assertEqual([i['text'] for i in items], ['Plain & simple', 'With a note'])
        self.assertEqual(items[1]['note'], 'my note\nsecond line')
        self.assertEqual(items[0]['location'], '1 · Chapter')
        self.assertEqual(items[0]['uts'], '1770460620.0')

    def test_import(self):
        plain = 'From the time I was a boy the reckless streak that runs in my family has brought me nothing but trouble.'
        noted = 'After my mother died, I went on living with the old man and my brother.'
        joined = joined_paragraphs(self.book)
        missing = 'This sentence is not anywhere in Botchan at all, so it must not be imported.'
        items = legacy.parse(legacy_html([(plain, '', 1770460620), (noted, 'family', 1770460667),
                                          (joined, '', 1770460700), (missing, '', 1770460800)]))
        existing = [{'type': 'highlight', 'highlighted_text': plain, 'kobo': {}}]
        new, dupes, unplaced = legacy.import_records(self.book, items, existing, 'kepub', spine_offset=1)
        self.assertEqual([d['text'] for d in dupes], [plain])
        self.assertEqual([u['text'] for u in unplaced], [missing])
        self.assertEqual(len(new), 2)
        by_text = {A.normalize(r['highlighted_text'])[:20]: r for r in new}
        n = by_text[A.normalize(noted)[:20]]
        self.assertEqual(n['notes'], 'family\nsecond line')
        self.assertEqual(n['kobo']['raw']['Type'], 'note')
        self.assertEqual(n['timestamp'][:10], '2026-02-07')
        self.assertFalse(n['kobo']['on_device'], 'goes onto the Kobo at the next full sync')
        self.assertEqual(n['toc_family_titles'], ['Botchan', 'Chapter 1'])
        self.assertTrue(n['kobo']['cfi_exact'] and n['start_cfi'])
        self.assertEqual(n['spine_index'], self.book.spine.index(n['spine_name']) + 1)
        j = next(r for r in new if r is not n)
        self.assertEqual(j['kobo']['history'][-1]['method'], 'fuzzy')
        self.assertIn('\n', j['highlighted_text'], "the book's own text keeps the paragraph break")


if __name__ == '__main__':
    unittest.main()
