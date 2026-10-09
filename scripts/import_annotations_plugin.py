"""Import every highlight from the Annotations plugin's column into Kobo Highlight Keeper.

Run with calibre closed and the plugin installed:

  calibre-debug -e scripts/import_annotations_plugin.py -- "/path/to/Calibre Library" [#column] [--dry-run]

The same as Kobo highlights → Import highlights from the Annotations plugin…
for all books, plus: books with only an ORIGINAL_KEPUB/ORIGINAL_EPUB are
placed using that file instead of being skipped. Prints a per-book report.
The column itself is not changed (remove it afterwards with
`calibredb remove_custom_column <label>` if you no longer need it).
"""

import os
import sys

from calibre.customize.ui import find_plugin
from calibre.library import db as open_library

find_plugin('Kobo Highlight Keeper')  # makes calibre_plugins.kobo_highlight_keeper importable
from calibre_plugins.kobo_highlight_keeper import store  # noqa: E402
from calibre_plugins.kobo_highlight_keeper.action import viewer_spine_offset  # noqa: E402
from calibre_plugins.kobo_highlight_keeper.config import prefs  # noqa: E402
from calibre_plugins.kobo_highlight_keeper.core.epubtext import Book  # noqa: E402
from calibre_plugins.kobo_highlight_keeper.core.legacy import LEGACY_MARKER, import_records, parse  # noqa: E402
from calibre_plugins.kobo_highlight_keeper.core.viewer import mirrored  # noqa: E402

args = [a for a in sys.argv[1:] if a != '--']
dry = '--dry-run' in args
args = [a for a in args if a != '--dry-run']
library = args[0]
db = open_library(library).new_api
field = args[1] if len(args) > 1 else None
if field is None:
    for f in db.field_metadata.custom_field_keys():
        if db.field_metadata[f]['datatype'] == 'comments' and any(
                LEGACY_MARKER in (db.field_for(f, i) or '') for i in db.search(f'{f}:true')):
            field = f
            break
if field is None:
    sys.exit('No Annotations plugin column found')
print(f'column: {field}{"  (dry run)" if dry else ""}')

totals = dict(old=0, imported=0, already=0, unplaced=0)
for book_id in sorted(db.search(f'{field}:true')):
    html = db.field_for(field, book_id) or ''
    if LEGACY_MARKER not in html:
        continue
    title = db.field_for('title', book_id)
    fmts = db.formats(book_id) or ()
    fmt = next((f for f in ('KEPUB', 'EPUB', 'ORIGINAL_KEPUB', 'ORIGINAL_EPUB') if f in fmts), None)
    items = parse(html)
    totals['old'] += len(items)
    if fmt is None:
        print(f'  {title}: {len(items)} old, NOT imported (no EPUB/KEPUB format: {fmts})')
        totals['unplaced'] += len(items)
        continue
    path = db.format_abspath(book_id, fmt)
    st = os.stat(path)
    existing = store.load_records(db, book_id)
    new, dupes, unplaced = import_records(
        Book(path), items, existing, 'epub' if 'EPUB' in fmt else 'kepub',
        spine_offset=viewer_spine_offset(path, fmt.replace('ORIGINAL_', '')),
        library_sig=[st.st_size, int(st.st_mtime)])
    totals['imported'] += len(new)
    totals['already'] += len(dupes)
    totals['unplaced'] += len(unplaced)
    note = '' if fmt in ('KEPUB', 'EPUB') else f'  (placed using {fmt})'
    print(f'  {title}: {len(items)} old -> {len(new)} imported, {len(dupes)} already present, '
          f'{len(unplaced)} not found{note}')
    for u in unplaced:
        print(f'      not found: {u["text"][:100]!r}')
    if dry or not new:
        continue
    records = existing + new
    store.save_records(db, book_id, records)
    target = store.target_format(db, book_id)
    viewer = store.load_viewer_annotations(db, [book_id]).get((book_id, target), [])
    m = mirrored(records, viewer, enabled=prefs['show_in_viewer'])
    if m is not None:
        store.save_viewer_annotations(db, book_id, target, m)
print('totals:', totals)
db.close()
