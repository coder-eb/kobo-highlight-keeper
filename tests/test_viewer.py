"""Mirroring Kobo highlights into calibre's viewer annotations."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kobo_highlight_keeper.sync import ACTIVE, DELETED  # noqa: E402
from kobo_highlight_keeper.viewer import MIRROR, feedback, mirrored  # noqa: E402

T0, T1, T2 = '2026-01-01T10:00:00.000Z', '2026-02-01T10:00:00.000Z', '2026-03-01T10:00:00+00:00'


def record(uuid, notes='', state=ACTIVE, exact=True):
    return {'type': 'highlight', 'uuid': uuid, 'timestamp': T1, 'highlighted_text': f'text {uuid}',
            'notes': notes, 'style': {'kind': 'color', 'type': 'builtin', 'which': 'yellow'},
            'spine_index': 3, 'spine_name': 'ch.xhtml', 'start_cfi': '/2/4/2/1:0', 'end_cfi': '/2/4/2/1:9',
            'kobo': {'state': state, 'cfi_exact': exact, 'raw': {'BookmarkID': uuid}}}


OWN = {'type': 'highlight', 'uuid': 'viewer-own', 'timestamp': T0, 'highlighted_text': 'mine',
       'spine_index': 1, 'start_cfi': '/2/4/1:0', 'end_cfi': '/2/4/1:4'}


class TestViewerMirror(unittest.TestCase):
    def test_mirrors_active_exact_highlights_and_keeps_own(self):
        recs = [record('a'), record('b', exact=False), record('c', state=DELETED)]
        out = mirrored(recs, [OWN])
        self.assertIn(OWN, out)
        mirrors = [a for a in out if a.get(MIRROR)]
        self.assertEqual([m['uuid'] for m in mirrors], ['a'])
        self.assertEqual(mirrors[0]['start_cfi'], '/2/4/2/1:0')
        self.assertNotIn('kobo', mirrors[0])
        self.assertIsNone(mirrored(recs, out), 'second pass: nothing to write')

    def test_removed_or_disabled_mirrors_become_deletion_markers(self):
        out = mirrored([record('a')], [OWN])
        gone = mirrored([record('a', state=DELETED)], out)
        self.assertTrue(any(a.get('removed') and a['uuid'] == 'a' for a in gone))
        off = mirrored([record('a')], out, enabled=False)
        self.assertTrue(any(a.get('removed') and a['uuid'] == 'a' for a in off))
        self.assertIn(OWN, off)

    def test_viewer_note_edit_and_delete_flow_back(self):
        recs = [record('a', notes='old'), record('b')]
        viewer = mirrored(recs, [])
        edited = [dict(a, notes='edited in viewer', timestamp=T2) if a['uuid'] == 'a' else a for a in viewer]
        recs2, changed = feedback(recs, edited)
        self.assertTrue(changed)
        self.assertEqual(next(r for r in recs2 if r.get('uuid') == 'a')['notes'], 'edited in viewer')
        deleted = [{'removed': True, 'uuid': 'b', 'type': 'highlight', 'timestamp': T2} if a['uuid'] == 'b' else a
                   for a in viewer]
        recs3, changed = feedback(recs, deleted)
        self.assertTrue(changed)
        self.assertTrue(next(r for r in recs3 if r.get('uuid') == 'b').get('removed'))

    def test_older_viewer_copy_is_ignored(self):
        recs = [record('a', notes='newer on kobo')]
        stale = [dict(mirrored([record('a', notes='old')], [])[0], timestamp=T0)]
        self.assertFalse(feedback(recs, stale)[1])


if __name__ == '__main__':
    unittest.main()
