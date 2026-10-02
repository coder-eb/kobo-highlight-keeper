#!/usr/bin/env python3
"""Rebuild the sandbox and run the scripted GUI scenario with screenshots.

  python3 harness/run_gui.py            # offscreen (no window on screen)
  python3 harness/run_gui.py --visible  # show the sandbox calibre window

Outputs: sandbox/screenshots/*.png, sandbox/run/scenario.log, result.json.
Exit code 0 only if every scenario check passed.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from sandbox_env import CALIBRE_BIN, PROJECT, RUN_DIR, calibre_env


def run(cmd, env=None, **kw):
    print('$', ' '.join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], env=env, check=True, **kw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--visible', action='store_true')
    ap.add_argument('--timeout', type=int, default=900)
    args = ap.parse_args()

    kobos = [str(v) for v in Path('/Volumes').glob('*') if (v / '.kobo').is_dir()]
    if kobos:
        sys.exit(f'A real Kobo is mounted ({", ".join(kobos)}); eject it before running the GUI scenario.')

    harness = PROJECT / 'harness'
    run([sys.executable, harness / 'setup_sandbox.py', '--force'])
    run([sys.executable, PROJECT / 'scripts' / 'build_plugin.py'])
    env = calibre_env()
    run([CALIBRE_BIN / 'calibre-customize', '-a', PROJECT / 'dist' / 'Kobo-Highlight-Keeper.zip'], env=env)
    run([CALIBRE_BIN / 'calibre-customize', '-b', harness / 'gui_plugin'], env=env)

    env['KHK_SCENARIO'] = 'full'
    if not args.visible:
        env['QT_QPA_PLATFORM'] = 'offscreen'
    log = RUN_DIR / 'calibre-stdout.log'
    with open(log, 'w') as out:
        proc = subprocess.run([str(CALIBRE_BIN / 'calibre-debug'), '-e', str(harness / 'launch_gui.py')],
                              env=env, stdout=out, stderr=subprocess.STDOUT, timeout=args.timeout)
    print(f'calibre exited with {proc.returncode}; log: {log}')
    result = RUN_DIR / 'result.json'
    if not result.exists():
        sys.exit('scenario did not finish (no result.json)')
    failures = json.loads(result.read_text())['failures']
    print(open(RUN_DIR / 'scenario.log').read())
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    os.chdir(PROJECT)
    main()
