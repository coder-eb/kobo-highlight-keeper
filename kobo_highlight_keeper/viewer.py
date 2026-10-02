"""Mirror Kobo highlights into calibre's viewer annotations, and read back edits.

calibre's viewer shows only its own annotations (user_type='local',
user='viewer'). Active Kobo highlights with an exact calibre position are
copied there, tagged with MIRROR and sharing the Kobo BookmarkID as uuid.
Annotations the viewer made itself are never touched.

Edits flow back: when the viewer copy is newer than the record (a note
edited in the viewer, or the highlight deleted there, which leaves calibre's
{'removed': True, 'uuid'} marker), the record is updated, and the next sync
sends that to the Kobo like any edit made in calibre.
"""

import json

from .sync import ACTIVE, kobo_ts, now_iso

MIRROR = 'kobo_mirror'
MIRRORED_KEYS = ('type', 'uuid', 'timestamp', 'highlighted_text', 'notes', 'style', 'spine_index',
                 'spine_name', 'start_cfi', 'end_cfi', 'toc_family_titles')


def _is_live_highlight(r):
    return not r.get('removed') and 'kobo' in r and r['type'] == 'highlight'


def _live_highlights(records):
    return [r for r in records if _is_live_highlight(r)]


def feedback(records, viewer_annots):
    """Apply viewer edits/deletions of mirrored highlights to records: (records, changed)."""
    ours = {r['uuid'] for r in _live_highlights(records)}
    by_uuid = {a['uuid']: a for a in viewer_annots
               if a.get('type') == 'highlight' and a.get('uuid') in ours and (a.get(MIRROR) or a.get('removed'))}
    out, changed = [], False
    for r in records:
        v = by_uuid.get(r['uuid']) if _is_live_highlight(r) else None
        if v is None or r['kobo']['state'] != ACTIVE or kobo_ts(v['timestamp']) <= kobo_ts(r['timestamp']):
            out.append(r)
        elif v.get('removed'):  # deleted in the viewer
            out.append({'removed': True, 'uuid': r['uuid'], 'type': 'highlight', 'timestamp': v['timestamp']})
            changed = True
        elif (v.get('notes') or '') != (r.get('notes') or ''):  # note edited in the viewer
            out.append(dict(r, notes=v.get('notes') or '', timestamp=v['timestamp']))
            changed = True
        else:
            out.append(r)
    return out, changed


def mirrored(records, viewer_annots, enabled=True):
    """The viewer's annotation list with our mirrors refreshed, or None if unchanged."""
    live = {r['uuid']: r for r in _live_highlights(records)}
    record_uuids = set(live) | {r.get('uuid') for r in records if r.get('removed')}
    was_mirrored = {a['uuid'] for a in viewer_annots if a.get(MIRROR) and a.get('uuid')}
    is_ours = lambda a: a.get(MIRROR) or (a.get('removed') and a.get('uuid') in record_uuids)  # noqa: E731
    keep = [a for a in viewer_annots if not is_ours(a)]
    old = {a['uuid']: a for a in viewer_annots if is_ours(a) and a.get('uuid')}
    ours = []
    for uuid in sorted(record_uuids | was_mirrored):
        r = live.get(uuid)
        if enabled and r and r['kobo']['state'] == ACTIVE and r['kobo'].get('cfi_exact'):
            m = {k: r[k] for k in MIRRORED_KEYS if k in r}
            m[MIRROR] = True
            ours.append(m)
        elif uuid in was_mirrored or (uuid in old and not old[uuid].get('removed')):
            ours.append({'removed': True, 'uuid': uuid, 'type': 'highlight', 'timestamp': now_iso()})
        elif uuid in old:
            ours.append(old[uuid])  # keep an existing deletion marker as it is
    if _same(list(old.values()), ours):
        return None
    return keep + ours


def _same(a, b):
    def key(x):
        x = dict(x)
        if x.get('removed'):
            x.pop('timestamp', None)
        return json.dumps(x, sort_keys=True)
    return sorted(map(key, a)) == sorted(map(key, b))
