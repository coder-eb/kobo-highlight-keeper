"""Run inside calibre:  calibre-debug -e harness/_build_library.py

Adds book copies to the *sandbox* library through calibre's own API.
(calibredb refuses to run at all while any calibre GUI is open, even for a
different library; the API opens only the sandbox library's metadata.db.)

Reads the job from $KHK_BUILD_JOB (JSON: {library, books: [{real_id, dir}]})
and writes {real_id: sandbox_id} to $KHK_BUILD_OUT.
"""

import json
import os
from pathlib import Path

from calibre.db.backend import DB
from calibre.db.cache import Cache
from calibre.ebooks.metadata.opf2 import OPF

job = json.loads(Path(os.environ['KHK_BUILD_JOB']).read_text())
library = job['library']
sandbox = os.environ['KHK_SANDBOX']
assert os.path.realpath(library).startswith(os.path.realpath(sandbox)), library

cache = Cache(DB(library))
cache.init()
added = {}
for entry in job['books']:
    d = Path(entry['dir'])
    with open(d / 'metadata.opf', 'rb') as f:
        mi = OPF(f, basedir=str(d)).to_book_metadata()
    if (d / 'cover.jpg').exists():
        mi.cover_data = ('jpg', (d / 'cover.jpg').read_bytes())
    fmts = {}
    for p in sorted(d.iterdir()):
        if p.suffix.lower() in ('.epub', '.kepub'):
            fmts[p.suffix[1:].upper()] = str(p)
    ids, _ = cache.add_books([(mi, fmts)], add_duplicates=True)
    added[entry['real_id']] = ids[0]
    print(f'  real #{entry["real_id"]} -> sandbox #{ids[0]}: {mi.title} [{", ".join(fmts)}]')
cache.close()

# Skip the first-run welcome wizard in the sandbox calibre (config dir is the
# sandbox's: CALIBRE_CONFIG_DIRECTORY is set by setup_sandbox.py).
from calibre.utils.config import config_dir, dynamic  # noqa: E402
assert os.path.realpath(config_dir).startswith(os.path.realpath(sandbox)), config_dir
dynamic.set('welcome_wizard_was_run', True)

Path(os.environ['KHK_BUILD_OUT']).write_text(json.dumps(added))
