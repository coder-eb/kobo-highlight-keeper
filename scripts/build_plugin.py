#!/usr/bin/env python3
"""Package the calibre plugin: plugin/*.py + kobo_highlight_keeper/ as core/.

  python3 scripts/build_plugin.py              ->  dist/Kobo-Highlight-Keeper.zip
  python3 scripts/build_plugin.py --version    prints the plugin version (e.g. 0.1.0)

Install with: calibre-customize -a dist/Kobo-Highlight-Keeper.zip
The version is KoboHighlightKeeperPlugin.version in plugin/__init__.py.
"""

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'dist' / 'Kobo-Highlight-Keeper.zip'


def plugin_version():
    src = (ROOT / 'plugin' / '__init__.py').read_text()
    m = re.search(r'^\s*version\s*=\s*\((\d+),\s*(\d+),\s*(\d+)\)', src, re.M)
    if not m:
        sys.exit('version = (x, y, z) not found in plugin/__init__.py')
    return '.'.join(m.groups())


def main():
    if '--version' in sys.argv:
        print(plugin_version())
        return
    OUT.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUT, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('plugin-import-name-kobo_highlight_keeper.txt', '')
        for f in sorted((ROOT / 'plugin').glob('*.py')):
            z.write(f, f.name)
        for f in sorted((ROOT / 'kobo_highlight_keeper').glob('*.py')):
            z.write(f, f'core/{f.name}')
    print(OUT)


if __name__ == '__main__':
    main()
