import os

from calibre.utils.config import JSONConfig, config_dir
from qt.core import QCheckBox, QComboBox, QFormLayout, QLabel, QLineEdit, QSpinBox, QWidget

prefs = JSONConfig('plugins/Kobo Highlight Keeper')
# What happens when a Kobo is connected: 'copy' (copy highlights from the
# Kobo, never write to it), 'sync' (both ways) or 'off'.
prefs.defaults['on_connect'] = 'copy'
ON_CONNECT = (
    ('copy', 'Copy new highlights from the Kobo (don’t change the Kobo)'),
    ('sync', 'Sync both ways (also put back highlights the Kobo lost)'),
    ('off', 'Do nothing'),
)
prefs.defaults['backup_dir'] = ''
prefs.defaults['keep_backups'] = 20
prefs.defaults['show_in_viewer'] = True

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
        self.on_connect = QComboBox(self)
        for value, text in ON_CONNECT:
            self.on_connect.addItem(text, value)
        self.on_connect.setCurrentIndex(max(0, self.on_connect.findData(prefs['on_connect'])))
        lay.addRow('When a Kobo is connected:', self.on_connect)
        self.show_in_viewer = QCheckBox("Show Kobo highlights in calibre's viewer")
        self.show_in_viewer.setChecked(prefs['show_in_viewer'])
        self.show_in_viewer.setToolTip('Notes edited or highlights deleted in the viewer are sent to the Kobo too.')
        lay.addRow(self.show_in_viewer)
        lay.addRow(QLabel(
            'Copying never writes to the Kobo: highlights it lost, and notes or deletions made in\n'
            'calibre, wait until you choose Kobo highlights → Sync with Kobo now.\n'
            'Highlights you delete on the Kobo are kept in calibre but not put back.'))
        self.backup = QLineEdit(prefs['backup_dir'])
        self.backup.setPlaceholderText(os.path.join(DATA_DIR, 'kobo-db-backups'))
        lay.addRow('Kobo database backups:', self.backup)
        self.keep = QSpinBox()
        self.keep.setRange(1, 500)
        self.keep.setValue(prefs['keep_backups'])
        lay.addRow('Backups to keep:', self.keep)

    def save_settings(self):
        prefs['on_connect'] = self.on_connect.currentData()
        prefs['show_in_viewer'] = self.show_in_viewer.isChecked()
        prefs['backup_dir'] = self.backup.text().strip()
        prefs['keep_backups'] = self.keep.value()
