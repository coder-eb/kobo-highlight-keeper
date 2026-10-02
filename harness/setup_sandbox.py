#!/usr/bin/env python3
"""Build (or rebuild) the isolated test sandbox.

Creates:
  sandbox/config/        a separate calibre config dir (only the KoboTouch
                         driver prefs are copied from the real config)
  sandbox/library/       a new calibre library with copies of a few real books
  sandbox/kobo/          an empty simulated Kobo Clara BW volume

The real library is only read: files are copied with shutil.copy2 and the
real metadata.db is never opened.

Usage:
  python3 harness/setup_sandbox.py [--books 890 859 852] [--force]
"""

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys

from sandbox_env import (
    CACHE_DIR, CALIBRE_BIN, CONFIG_DIR, KOBO_DB, KOBO_DBVERSION, KOBO_DIR,
    KOBO_FIRMWARE, KOBO_MODEL_ID, KOBO_SERIAL, LIBRARY_DIR, PROJECT,
    REAL_CONFIG_DIR, REAL_LIBRARY_DIR, RUN_DIR, SANDBOX, SCREENSHOT_DIR,
    SOURCE_DIR, TEMP_DIR, assert_sandboxed, calibre_env,
)

DEFAULT_BOOKS = [890, 859, 852]
SCHEMA = PROJECT / 'harness' / 'schema' / 'nickel-176.sql'


def kobo_schema_sql():
    """The firmware schema dump, with DbVersion's column named as calibre reads it.

    calibre's driver runs `SELECT version FROM dbversion` and reads the
    'version' key; SQLite reports the *declared* name, so the column must be
    lowercase as on real devices (the dump capitalised it).
    """
    return SCHEMA.read_text().replace(
        'CREATE TABLE DbVersion (Version INTEGER NOT NULL, PRIMARY KEY (Version));',
        'CREATE TABLE DbVersion (version INTEGER NOT NULL, PRIMARY KEY (version));')


def find_real_book_dir(book_id):
    """Locate '<Author>/<Title> (<id>)' in the real library by folder name."""
    pat = re.compile(rf'\({book_id}\)$')
    for author_dir in REAL_LIBRARY_DIR.iterdir():
        if not author_dir.is_dir():
            continue
        for book_dir in author_dir.iterdir():
            if pat.search(book_dir.name):
                return book_dir
    raise SystemExit(f'Book id {book_id} not found in {REAL_LIBRARY_DIR}')


def calibre_debug_script(script, **extra_env):
    env = calibre_env()
    env.update(extra_env)
    cmd = [str(CALIBRE_BIN / 'calibre-debug'), '-e', str(PROJECT / 'harness' / script)]
    out = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if out.returncode:
        raise SystemExit(f'{script} failed:\n{out.stdout}\n{out.stderr}')
    return out.stdout


def setup_config():
    assert_sandboxed(CONFIG_DIR)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Same Kobo driver behaviour as the real setup (kepubify, save template,
    # replace-existing override, collections) -- copied, never linked.
    src = REAL_CONFIG_DIR / 'device_drivers_KOBOTOUCH.py.json'
    prefs = json.loads(src.read_text())
    # Collections come from a custom column that the sandbox library lacks.
    prefs['collections_columns'] = ''
    # Sandbox only: send "The No. 1 Ladies' Detective Agency" as a plain EPUB
    # (no kepubify) so both formats go through the real driver.
    prefs['template_for_kepubify'] = "program: contains($title, 'Ladies', 'false', 'true')"
    (CONFIG_DIR / 'device_drivers_KOBOTOUCH.py.json').write_text(json.dumps(prefs, indent=2))
    # Point the sandbox GUI at the sandbox library and skip the welcome wizard.
    (CONFIG_DIR / 'global.py.json').write_text(json.dumps({
        'library_path': str(LIBRARY_DIR),
        'installation_uuid': 'khk-sandbox',
    }, indent=2))
    (CONFIG_DIR / 'gui.json').write_text(json.dumps({
        'library_usage_stats': {str(LIBRARY_DIR): 1},
    }, indent=2))


def setup_library(book_ids):
    assert_sandboxed(LIBRARY_DIR)
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    job = {'library': str(LIBRARY_DIR), 'books': []}
    for real_id in book_ids:
        src_dir = find_real_book_dir(real_id)
        dest = assert_sandboxed(SOURCE_DIR / str(real_id))
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src_dir, dest)  # read-only copy of the real files
        job['books'].append({'real_id': real_id, 'dir': str(dest)})
    job_file = assert_sandboxed(TEMP_DIR / 'build_job.json')
    out_file = assert_sandboxed(TEMP_DIR / 'build_out.json')
    job_file.write_text(json.dumps(job))
    print(calibre_debug_script('_build_library.py', KHK_BUILD_JOB=str(job_file),
                               KHK_BUILD_OUT=str(out_file)), end='')
    return {int(k): v for k, v in json.loads(out_file.read_text()).items()}


def setup_kobo():
    assert_sandboxed(KOBO_DIR)
    kobo_meta = KOBO_DIR / '.kobo'
    kobo_meta.mkdir(parents=True, exist_ok=True)
    (KOBO_DIR / '.kobo-images').mkdir(exist_ok=True)
    # .kobo/version: serial,?,firmware,?,?,model-id (driver reads [0], [2], [-1])
    (kobo_meta / 'version').write_text(
        f'{KOBO_SERIAL},4.1.15,{KOBO_FIRMWARE},4.1.15,4.1.15,{KOBO_MODEL_ID}\n')
    (kobo_meta / 'Kobo').mkdir(exist_ok=True)
    (kobo_meta / 'Kobo' / 'Kobo eReader.conf').write_text('[ApplicationPreferences]\n')
    con = sqlite3.connect(assert_sandboxed(KOBO_DB))
    con.executescript(kobo_schema_sql())
    con.execute('INSERT INTO DbVersion (version) VALUES (?)', (KOBO_DBVERSION,))
    con.commit()
    con.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--books', type=int, nargs='+', default=DEFAULT_BOOKS,
                    help='real-library book ids to copy into the sandbox')
    ap.add_argument('--force', action='store_true', help='delete and rebuild an existing sandbox')
    args = ap.parse_args()

    if SANDBOX.exists():
        if not args.force:
            sys.exit(f'{SANDBOX} exists; pass --force to rebuild it')
        shutil.rmtree(assert_sandboxed(SANDBOX))
    for d in (CACHE_DIR, TEMP_DIR, RUN_DIR, SCREENSHOT_DIR):
        assert_sandboxed(d).mkdir(parents=True, exist_ok=True)

    print('config ...'); setup_config()
    print('library ...'); added = setup_library(args.books)
    print('kobo ...'); setup_kobo()
    (SANDBOX / 'books.json').write_text(json.dumps(added, indent=2))
    print(f'done: {SANDBOX}')


if __name__ == '__main__':
    main()
