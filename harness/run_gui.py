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

from sandbox_env import CALIBRE_BIN, PROJECT, RUN_DIR, calibre_env


def run(cmd, env=None, **kw):
    print('$', ' '.join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], env=env, check=True, **kw)


def real_calibre_running():
    """PIDs of calibre GUIs that are not this sandbox's."""
    out = subprocess.run(['pgrep', '-fl', 'calibre.app/Contents/MacOS/calibre'], capture_output=True, text=True)
    return [line for line in out.stdout.splitlines()
            if str(PROJECT) not in line and 'calibre-parallel' not in line and line.rstrip().endswith('/calibre')]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--visible', action='store_true')
    ap.add_argument('--timeout', type=int, default=900)
    ap.add_argument('--alongside', action='store_true',
                    help='run even if your normal calibre is open (lock/socket are isolated, '
                         'but a full run alongside it has not been verified)')
    args = ap.parse_args()

    running = real_calibre_running()
    if running and not args.alongside:
        sys.exit('Your normal calibre is running; quit it first or pass --alongside:\n  '
                 + '\n  '.join(running))

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
