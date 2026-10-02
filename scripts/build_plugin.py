#!/usr/bin/env python3
"""Package the calibre plugin: plugin/*.py + kobo_highlight_keeper/ as core/.

  python3 scripts/build_plugin.py   ->  dist/Kobo-Highlight-Keeper.zip
Install with: calibre-customize -a dist/Kobo-Highlight-Keeper.zip
"""

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'dist' / 'Kobo-Highlight-Keeper.zip'


def main():
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
