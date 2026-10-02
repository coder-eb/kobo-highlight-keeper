"""End-to-end lifecycle against the simulated Kobo, without the calibre GUI.

Each step does what the real actors do:
  * "calibre"  -- copies files onto the device and deletes Bookmark rows on
                  delete, exactly like the KoboTouch driver's delete_via_sql
  * "Nickel"   -- harness/fake_nickel.py (indexing, highlighting, deleting)
  * the plugin -- kobo_highlight_keeper.engine.sync_books
and every restore is checked with fake_nickel's independent `verify`.
"""

import glob
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kobo_highlight_keeper import kobodb  # noqa: E402
from kobo_highlight_keeper.engine import sync_books, summarize  # noqa: E402
from kobo_highlight_keeper.sync import ACTIVE, DELETED  # noqa: E402

sys.path.insert(0, str(ROOT / 'harness'))
from setup_sandbox import kobo_schema_sql  # noqa: E402

BOOKS = ROOT / 'sandbox' / 'source_books'

HIGHLIGHTS = {
    'botchan': [
        ('From the time I was a boy the reckless streak that runs in my family has brought me nothing but trouble.', None),
        ('After my mother died, I went on living with the old man and my brother.', 'the family after Mother'),
    ],
    'ladies': [
        ('detective agency in Africa, at the foot of Kgale Hill. These were its assets: a tiny white van', None),
        ('Mma Ramotswe—the only lady private detective in Botswana—brewed redbush tea.', 'the premise'),
    ],
}


class Device:
    def __init__(self, root):
        self.mount = Path(root)
        (self.mount / '.kobo').mkdir(parents=True)
        self.db = self.mount / '.kobo' / 'KoboReader.sqlite'
        con = sqlite3.connect(self.db)
        con.executescript(kobo_schema_sql())
        con.execute('INSERT INTO DbVersion VALUES (176)')
        con.commit()
        con.close()
        self.env = dict(os.environ, KHK_KOBO_DIR=str(self.mount))

    def nickel(self, *args, check=True):
        out = subprocess.run([sys.executable, str(ROOT / 'harness' / 'fake_nickel.py'), *args],
                             env=self.env, capture_output=True, text=True)
        if check and out.returncode:
            raise AssertionError(f'fake_nickel {args} failed:\n{out.stdout}\n{out.stderr}')
        return out

    def send(self, src, rel):  # calibre upload: copy file onto the device
        dest = self.mount / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        return dest

    def calibre_delete(self, rel):  # KoboTouch.delete_via_sql + file removal
        vid = kobodb.ONBOARD + rel
        con = sqlite3.connect(self.db)
        con.execute('DELETE FROM Bookmark WHERE VolumeID = ?', (vid,))
        con.execute('DELETE FROM content WHERE ContentID = ? OR BookID = ?', (vid, vid))
        con.commit()
        con.close()
        (self.mount / rel).unlink()

    def sync(self, path, records):
        con = kobodb.connect(self.db)
        backups = []
        res = sync_books(con, self.mount, [{'key': 1, 'title': path.name, 'path': path, 'records': records}],
                         backup=lambda: backups.append(kobodb.backup(self.db, self.mount.parent / 'backups')))
        con.commit()
        con.close()
        print('\n' + summarize(res)[2])
        plan = res[0]['plan'] if res else None
        return plan, list(plan.records.values()) if plan else records, backups

    def bookmark_count(self, rel):
        con = sqlite3.connect(self.db)
        n = con.execute('SELECT count(*) FROM Bookmark WHERE VolumeID = ?', (kobodb.ONBOARD + rel,)).fetchone()[0]
        con.close()
        return n


def shift_kepub_spans(src, dest):
    """Simulate an edit + re-kepubify: new first paragraph, every span id shifts."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dest, 'w') as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.endswith('.xhtml') and b'koboSpan' in data:
                s = data.decode('utf-8')
                s = re.sub(r'id="kobo\.(\d+)\.(\d+)"', lambda m: f'id="kobo.{int(m.group(1)) + 1}.{m.group(2)}"', s)
                s = re.sub(r'(<div id="book-inner">)',
                           r'\1<p><span class="koboSpan" id="kobo.1.1">Editor note added later.</span></p>', s, count=1)
                data = s.encode('utf-8')
            zout.writestr(item, data)


@unittest.skipUnless(BOOKS.exists(), 'run harness/setup_sandbox.py first')
class TestLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='khk-')
        self.dev = Device(Path(self.tmp) / 'KOBOeReader')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def highlight_all(self, name, rel):
        for text, note in HIGHLIGHTS[name]:
            args = ['highlight', Path(rel).name, '--text', text]
            if note:
                args += ['--note', note]
            self.dev.nickel(*args)

    def test_kepub_full_lifecycle(self):
        src = glob.glob(str(BOOKS / '852' / '*.kepub'))[0]
        rel = 'Natsume Soseki/Botchan - Natsume Soseki.kepub.epub'
        path = self.dev.send(src, rel)
        self.dev.nickel('import')
        self.highlight_all('botchan', rel)
        self.dev.nickel('dogear', 'Botchan', '--text', 'Better to go. I told the custodian')

        # connect #1: everything is pulled into "calibre"
        plan, records, backups = self.dev.sync(path, [])
        self.assertEqual(len(records), 3)
        self.assertFalse(backups, 'pull-only sync must not write to the device')
        ids = {r['kobo']['raw']['BookmarkID'] for r in records}

        # calibre deletes the book and re-sends it: the driver wipes Bookmarks
        self.dev.calibre_delete(rel)
        path = self.dev.send(src, rel)
        self.assertEqual(self.dev.bookmark_count(rel), 0)

        # connect #2, before Kobo has indexed the new copy: restore waits
        plan, records, _ = self.dev.sync(path, records)
        self.assertEqual(len(plan.pending), 3)
        self.assertEqual(self.dev.bookmark_count(rel), 0)

        # eject -> Kobo indexes -> connect #3: restored, same ids, verified
        self.dev.nickel('import')
        plan, records, backups = self.dev.sync(path, records)
        self.assertEqual(len(plan.to_insert), 3)
        self.assertTrue(all('same position' in line for line in plan.log if line.startswith('restore')))
        self.assertEqual(len(backups), 1)
        self.assertEqual({r['kobo']['raw']['BookmarkID'] for r in records}, ids)
        self.dev.nickel('verify')

        # the reader deletes one highlight on the device: kept in calibre as
        # deleted, never pushed back
        self.dev.nickel('delete-highlight', 'Botchan', '--text', HIGHLIGHTS['botchan'][0][0])
        plan, records, backups = self.dev.sync(path, records)
        states = sorted(r['kobo']['state'] for r in records)
        self.assertEqual(states, [ACTIVE, ACTIVE, DELETED])
        self.assertFalse(plan.to_insert)
        plan, records, _ = self.dev.sync(path, records)
        self.assertFalse(plan.to_insert, 'a deleted highlight must stay deleted')

        # the book is edited and re-converted; span ids shift; calibre replaces
        # the file in place (keeps the rows), Kobo re-indexes
        edited = Path(self.tmp) / 'edited.kepub'
        shift_kepub_spans(src, edited)
        self.dev.send(edited, rel)
        self.dev.nickel('import')
        self.assertNotEqual(self.dev.nickel('verify', check=False).returncode, 0,
                            'shifted spans should break the old locations')
        plan, records, _ = self.dev.sync(path, records)
        self.assertEqual(len(plan.to_repair), 2)
        self.dev.nickel('verify')

        # and a full wipe of the edited book restores into the new layout
        self.dev.calibre_delete(rel)
        path = self.dev.send(edited, rel)
        self.dev.nickel('import')
        plan, records, _ = self.dev.sync(path, records)
        self.assertEqual(len(plan.to_insert), 2)
        self.dev.nickel('verify')

    def test_epub_wipe_and_restore(self):
        src = glob.glob(str(BOOKS / '859' / '*.epub'))[0]
        rel = "Alexander McCall Smith/The No. 1 Ladies' Detective Agency - Alexander McCall Smith.epub"
        path = self.dev.send(src, rel)
        self.dev.nickel('import')
        self.highlight_all('ladies', rel)
        plan, records, _ = self.dev.sync(path, [])
        self.assertEqual(len(records), 2)
        self.dev.calibre_delete(rel)
        path = self.dev.send(src, rel)
        self.dev.nickel('import')
        plan, records, _ = self.dev.sync(path, records)
        self.assertEqual(len(plan.to_insert), 2)
        self.dev.nickel('verify')

    def test_highlight_moves_from_epub_to_kepub(self):
        """Highlighted on the plain EPUB, later the KEPUB is sent instead."""
        ep = glob.glob(str(BOOKS / '859' / '*.epub'))[0]
        kp = glob.glob(str(BOOKS / '859' / '*.kepub'))[0]
        rel_e = "A/Ladies.epub"
        rel_k = "A/Ladies.kepub.epub"
        path = self.dev.send(ep, rel_e)
        self.dev.nickel('import')
        self.highlight_all('ladies', rel_e)
        _, records, _ = self.dev.sync(path, [])
        self.dev.calibre_delete(rel_e)
        path = self.dev.send(kp, rel_k)
        self.dev.nickel('import')
        plan, records, _ = self.dev.sync(path, records)
        self.assertEqual(len(plan.to_insert), 2)
        self.dev.nickel('verify')


if __name__ == '__main__':
    unittest.main()
