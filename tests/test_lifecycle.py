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
        return plan, plan.all_records() if plan else records, backups

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


def calibre_now():
    """Timestamp in the format calibre's annotation edits use."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


@unittest.skipUnless(BOOKS.exists(), 'run harness/setup_sandbox.py first')
class TestCalibreIsMaster(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='khk-')
        self.dev = Device(Path(self.tmp) / 'KOBOeReader')
        self.src = glob.glob(str(BOOKS / '852' / '*.kepub'))[0]
        self.rel = 'Natsume Soseki/Botchan - Natsume Soseki.kepub.epub'
        self.path = self.dev.send(self.src, self.rel)
        self.dev.nickel('import')
        for text, note in HIGHLIGHTS['botchan']:
            self.dev.nickel('highlight', 'Botchan', '--text', text, *(['--note', note] if note else []))
        _, self.records, _ = self.dev.sync(self.path, [])

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def device_rows(self):
        con = kobodb.connect(self.dev.db)
        rows = {r['BookmarkID']: r for r in con.execute('SELECT * FROM Bookmark')}
        con.close()
        return rows

    def by_text(self, start):
        return next(r for r in self.records if (r.get('highlighted_text') or '').startswith(start))

    def test_delete_and_note_edit_in_calibre_reach_the_kobo(self):
        plain = self.by_text('From the time')
        noted = self.by_text('After my mother')
        # what calibre's annotation browser does on delete / note edit
        tomb = {'removed': True, 'uuid': plain['uuid'], 'type': 'highlight', 'timestamp': calibre_now()}
        noted = dict(noted, notes='edited in calibre', timestamp=calibre_now())
        plan, records, backups = self.dev.sync(self.path, [tomb, noted])
        self.assertEqual(plan.to_delete, [plain['uuid']])
        self.assertEqual(len(plan.to_update), 1)
        self.assertEqual(len(backups), 1)
        rows = self.device_rows()
        self.assertNotIn(plain['uuid'], rows, 'deleted in calibre -> deleted on the Kobo')
        self.assertEqual(rows[noted['uuid']]['Annotation'], 'edited in calibre')
        self.assertIn(tomb, plan.all_records(), "calibre's deletion marker is kept")
        self.dev.nickel('verify')
        # next sync: nothing comes back, nothing is written
        plan, _, backups = self.dev.sync(self.path, plan.all_records())
        self.assertFalse(plan.device_writes or plan.changed or backups)

    def test_note_conflicts_newer_edit_wins(self):
        noted = self.by_text('After my mother')
        con = sqlite3.connect(self.dev.db)
        con.execute("UPDATE Bookmark SET Annotation = 'edited on kobo', DateModified = '2020-01-01T00:00:00.000' "
                    'WHERE BookmarkID = ?', (noted['uuid'],))
        con.commit()
        con.close()
        # calibre edited later than the Kobo -> calibre wins
        plan, records, _ = self.dev.sync(self.path, [dict(noted, notes='edited in calibre', timestamp=calibre_now())])
        self.assertEqual(self.device_rows()[noted['uuid']]['Annotation'], 'edited in calibre')
        # the Kobo edits later (calibre untouched) -> the Kobo wins
        con = sqlite3.connect(self.dev.db)
        con.execute("UPDATE Bookmark SET Annotation = 'newer on kobo', DateModified = '2099-01-01T00:00:00.000' "
                    'WHERE BookmarkID = ?', (noted['uuid'],))
        con.commit()
        con.close()
        plan, records, _ = self.dev.sync(self.path, records)
        self.assertEqual(next(r for r in records if r.get('uuid') == noted['uuid'])['notes'], 'newer on kobo')
        self.assertFalse(plan.to_update)

    def test_chapter_titles(self):
        self.assertEqual(self.by_text('From the time')['toc_family_titles'], ['Botchan', 'Chapter 1'])

    def test_unmatched_books_are_reported(self):
        other = self.dev.send(glob.glob(str(BOOKS / '890' / '*.kepub'))[0], 'X/Reformatory.kepub.epub')
        self.dev.nickel('import')
        self.dev.nickel('highlight', 'Reformatory.kepub', '--text', 'That\u2019s how fast she was gone. Always.')
        con = kobodb.connect(self.dev.db)
        res = sync_books(con, self.dev.mount, [{'key': 1, 'title': 'Botchan', 'path': self.path,
                                                 'records': self.records}])
        con.close()
        self.assertEqual([(u['title'], u['count']) for u in res.unmatched], [('The Reformatory', 1)])
        self.assertIn('not in the library', summarize(res)[2])
        self.assertTrue(other.exists())

    def test_damaged_database_blocks_writes(self):
        from unittest import mock
        self.dev.calibre_delete(self.rel)
        self.path = self.dev.send(self.src, self.rel)
        self.dev.nickel('import')
        with mock.patch('kobo_highlight_keeper.engine.integrity_problem', return_value='page 3 is never used'):
            plan, records, backups = self.dev.sync(self.path, self.records)
        self.assertFalse(backups)
        self.assertEqual(self.dev.bookmark_count(self.rel), 0, 'nothing written to a damaged database')
        self.assertTrue(any(line.startswith('ERROR') for line in plan.log))
        # once the database is fine again, the restore happens
        plan, records, _ = self.dev.sync(self.path, records)
        self.assertEqual(self.dev.bookmark_count(self.rel), 2)
        self.dev.nickel('verify')


if __name__ == '__main__':
    unittest.main()
