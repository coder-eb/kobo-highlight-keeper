"""Render stored records as Markdown (one section per book, grouped by chapter)."""

from .sync import ACTIVE


def _position(rec):
    a = rec['kobo'].get('anchor') or {}
    return (a.get('spine_index', 1 << 30), a.get('start', 0))


def book_markdown(title, authors, records, include_deleted=False):
    lines = [f'# {title}', '']
    if authors:
        lines += [f'*{authors}*', '']
    # 'kobo' is missing on calibre's markers for annotations deleted in calibre
    recs = [r for r in records if 'kobo' in r and (include_deleted or r['kobo']['state'] == ACTIVE)]
    chapter = None
    for rec in sorted(recs, key=_position):
        here = ' › '.join(rec.get('toc_family_titles') or [])
        if here and here != chapter:
            lines += [f'## {here}', '']
            chapter = here
        if rec['type'] == 'bookmark':
            lines += [f'- 🔖 Bookmark ({rec["timestamp"][:10]})', '']
            continue
        text = ' '.join((rec.get('highlighted_text') or '').split())
        lines.append(f'> {text}')
        if rec.get('notes'):
            lines += ['', f'**Note:** {rec["notes"]}']
        state = '' if rec['kobo']['state'] == ACTIVE else f' · {rec["kobo"]["state"]}'
        lines += ['', f'<sub>{rec["timestamp"][:10]}{state}</sub>', '']
    return '\n'.join(lines).rstrip() + '\n'
