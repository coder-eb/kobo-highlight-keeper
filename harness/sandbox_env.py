"""Paths and environment for the isolated test sandbox.

Everything the harness writes lives under ``sandbox/``. The real calibre
library and config are only ever *read* (copied from), never written to.
"""

import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SANDBOX = PROJECT / 'sandbox'

CONFIG_DIR = SANDBOX / 'config'
CACHE_DIR = SANDBOX / 'cache'
TEMP_DIR = SANDBOX / 'tmp'
RUN_DIR = SANDBOX / 'run'
LIBRARY_DIR = SANDBOX / 'library'
# the simulated Kobo's "mounted volume" (tests point it at a temp dir)
KOBO_DIR = Path(os.environ.get('KHK_KOBO_DIR') or SANDBOX / 'kobo')
SOURCE_DIR = SANDBOX / 'source_books'  # read-only copies of real books
SCREENSHOT_DIR = SANDBOX / 'screenshots'

KOBO_DB = KOBO_DIR / '.kobo' / 'KoboReader.sqlite'

REAL_CONFIG_DIR = Path.home() / 'Library' / 'Preferences' / 'calibre'
REAL_LIBRARY_DIR = Path.home() / 'Calibre Library'

CALIBRE_BIN = Path('/Applications/calibre.app/Contents/MacOS')

# Simulated device identity: a Clara BW (model id ending in 391), matching
# the user's real reader. Serial is fake so it never collides with the real
# device's per-device settings.
KOBO_SERIAL = 'N365000000000'
KOBO_FIRMWARE = '4.45.23646'
KOBO_MODEL_ID = '00000000-0000-0000-0000-000000000391'
KOBO_DBVERSION = 176


def assert_sandboxed(path):
    """Refuse to touch anything outside the sandbox."""
    p = Path(path).resolve()
    if p != SANDBOX and SANDBOX not in p.parents:
        raise RuntimeError(f'Refusing to write outside sandbox: {p}')
    if REAL_LIBRARY_DIR.resolve() in p.parents or REAL_CONFIG_DIR.resolve() in p.parents:
        raise RuntimeError(f'Refusing to write to real calibre data: {p}')
    return p


def calibre_env():
    """Environment that points every calibre process at the sandbox."""
    env = dict(os.environ)
    env['CALIBRE_CONFIG_DIRECTORY'] = str(CONFIG_DIR)
    env['CALIBRE_CACHE_DIRECTORY'] = str(CACHE_DIR)
    env['CALIBRE_TEMP_DIR'] = str(TEMP_DIR)
    env['KHK_SANDBOX'] = str(SANDBOX)
    return env
