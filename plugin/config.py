import os

from calibre.utils.config import JSONConfig, config_dir
from qt.core import QCheckBox, QFormLayout, QLabel, QLineEdit, QSpinBox, QWidget

prefs = JSONConfig('plugins/Kobo Highlight Keeper')
prefs.defaults['auto_sync'] = True
prefs.defaults['backup_dir'] = ''
prefs.defaults['keep_backups'] = 20

DATA_DIR = os.path.join(config_dir, 'plugins', 'Kobo Highlight Keeper')
REPORT_FILE = os.path.join(DATA_DIR, 'sync-report.txt')
NO_REPORT = 'No sync has run yet.'


UNMATCHED_FILE = os.path.join(DATA_DIR, 'unmatched-books-annotations.json')


def save_unmatched(unmatched):
    """Raw Kobo rows of annotated books that aren't in the library, so nothing is lost.

    Merged by BookmarkID, so rows from earlier syncs are kept too.
    """
    import json
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(UNMATCHED_FILE, encoding='utf-8') as f:
            saved = json.load(f)
    except (FileNotFoundError, ValueError):
        saved = {}
    for u in unmatched:
        book = saved.setdefault(u['volume_id'], {'title': u['title'], 'annotations': {}})
        book['title'] = u['title']
        for row in u['rows']:
            book['annotations'][row['BookmarkID']] = row
    tmp = UNMATCHED_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(saved, f, indent=1, default=str)
    os.replace(tmp, UNMATCHED_FILE)


def backup_dir():
    return prefs['backup_dir'] or os.path.join(DATA_DIR, 'kobo-db-backups')


RUN_SEPARATOR = '\n' + '=' * 72 + '\n'
KEEP_RUNS = 30


def save_report(text):
    """Prepend this run to the report history (newest first, last 30 runs)."""
    import time
    os.makedirs(DATA_DIR, exist_ok=True)
    runs = [r for r in load_report().split(RUN_SEPARATOR) if r.strip() and r != NO_REPORT]
    runs.insert(0, time.strftime('%Y-%m-%d %H:%M:%S  ') + text.strip())
    with open(REPORT_FILE, 'w', encoding='utf-8') as f:
        f.write(RUN_SEPARATOR.join(runs[:KEEP_RUNS]) + '\n')


def load_report():
    try:
        with open(REPORT_FILE, encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        return NO_REPORT


class ConfigWidget(QWidget):
    def __init__(self):
        super().__init__()
        lay = QFormLayout(self)
        self.auto_sync = QCheckBox('Sync automatically whenever a Kobo is connected')
        self.auto_sync.setChecked(prefs['auto_sync'])
        lay.addRow(self.auto_sync)
        lay.addRow(QLabel(
            'Sync pulls new and changed annotations from the Kobo into calibre, and restores\n'
            'annotations the Kobo has lost (deleted/re-sent/re-converted books).\n'
            'Highlights you delete on the Kobo are kept in calibre but not pushed back.'))
        self.backup = QLineEdit(prefs['backup_dir'])
        self.backup.setPlaceholderText(os.path.join(DATA_DIR, 'kobo-db-backups'))
        lay.addRow('Kobo database backups:', self.backup)
        self.keep = QSpinBox()
        self.keep.setRange(1, 500)
        self.keep.setValue(prefs['keep_backups'])
        lay.addRow('Backups to keep:', self.keep)

    def save_settings(self):
        prefs['auto_sync'] = self.auto_sync.isChecked()
        prefs['backup_dir'] = self.backup.text().strip()
        prefs['keep_backups'] = self.keep.value()
