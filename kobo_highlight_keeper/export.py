"""Render stored records as Markdown (one section per book)."""

from .sync import ACTIVE


def _position(rec):
    a = rec['kobo'].get('anchor') or {}
    return (a.get('spine_index', 1 << 30), a.get('start', 0))


def book_markdown(title, authors, records, include_deleted=False):
    lines = [f'# {title}', '']
    if authors:
        lines += [f'*{authors}*', '']
    recs = [r for r in records if include_deleted or r['kobo']['state'] == ACTIVE]
    for rec in sorted(recs, key=_position):
        if rec['type'] == 'bookmark':
            lines.append(f'- 🔖 Bookmark ({rec["timestamp"][:10]})')
            continue
        text = ' '.join((rec.get('highlighted_text') or '').split())
        lines.append(f'> {text}')
        if rec.get('notes'):
            lines += ['', f'**Note:** {rec["notes"]}']
        state = '' if rec['kobo']['state'] == ACTIVE else f' · {rec["kobo"]["state"]}'
        lines += ['', f'<sub>{rec["timestamp"][:10]}{state}</sub>', '']
    return '\n'.join(lines).rstrip() + '\n'
