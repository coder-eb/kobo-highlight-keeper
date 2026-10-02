"""Scripted end-to-end scenario inside the sandbox calibre GUI.

Runs only when $KHK_SCENARIO is set. Each step is a generator yield: the
runner polls the yielded condition, so calibre's event loop keeps running
(device jobs, dialogs) while the scenario waits. Screenshots are taken with
QWidget.grab(), so no macOS screen-recording permission is needed.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import time
import traceback

from calibre.gui2.actions import InterfaceAction
from qt.core import QApplication, QPoint, QTimer

SANDBOX = os.environ.get('KHK_SANDBOX', '')
KOBO = os.path.join(SANDBOX, 'kobo')
SHOTS = os.path.join(SANDBOX, 'screenshots')
LOG = os.path.join(SANDBOX, 'run', 'scenario.log')
NICKEL = os.path.join(os.path.dirname(SANDBOX), 'harness', 'fake_nickel.py')
PLUGIN = 'Kobo Highlight Keeper'

REFORMATORY = [
    ('His hope that she would say something to him died before it was fully awake.', None),
    ('That’s how fast she was gone. Always.', 'the ghost of his mother'),
    ('“I’m sorry, Miss Anne. Robbie got locked up at the Reformatory today.”', None),
]
BOTCHAN = [
    ('From the time I was a boy the reckless streak that runs in my family has brought me nothing but trouble.', None),
    ('After my mother died, I went on living with the old man and my brother.', 'family after Mother'),
]
LADIES = [
    ('detective agency in Africa, at the foot of Kgale Hill. These were its assets: a tiny white van', None),
    ('Mma Ramotswe—the only lady private detective in Botswana—brewed redbush tea.', 'the premise'),
]


def log(*args):
    line = time.strftime('%H:%M:%S ') + ' '.join(str(a) for a in args)
    print(line, flush=True)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


class Until:
    def __init__(self, pred, timeout, what):
        self.pred, self.deadline, self.what = pred, time.monotonic() + timeout, what


class HarnessAction(InterfaceAction):
    name = 'KHK Test Harness'
    action_spec = ('KHK harness', None, None, None)
    dont_add_to = frozenset({'toolbar', 'toolbar-device', 'context-menu', 'context-menu-device',
                             'menubar', 'menubar-device'})

    def genesis(self):
        pass

    def initialization_complete(self):
        if os.environ.get('KHK_SCENARIO'):
            self.gen = scenario(self)
            self.failures = []
            self.timer = QTimer(self.gui)
            self.timer.setInterval(300)
            self.timer.timeout.connect(self.poll)
            self.waiting = None
            QTimer.singleShot(1500, self.advance)

    # --- runner -------------------------------------------------------------
    def advance(self):
        try:
            self.waiting = next(self.gen)
            self.timer.start()
        except StopIteration:
            self.finish()
        except Exception:
            log('SCENARIO ERROR\n' + traceback.format_exc())
            self.failures.append('exception')
            self.finish()

    def poll(self):
        w = self.waiting
        try:
            done = w.pred()
        except Exception:
            done = False
        if done:
            self.timer.stop()
            self.advance()
        elif time.monotonic() > w.deadline:
            self.timer.stop()
            log(f'TIMEOUT waiting for: {w.what}')
            self.failures.append(f'timeout: {w.what}')
            for i, win in enumerate(x for x in QApplication.topLevelWidgets() if x.isVisible()):
                log(f'  visible window {i}: {type(win).__name__} {win.windowTitle()!r}')
                self.shot(f'zz-timeout-window-{i}', win)
            self.finish()

    def finish(self):
        log('RESULT:', 'PASS' if not self.failures else f'FAIL {self.failures}')
        with open(os.path.join(SANDBOX, 'run', 'result.json'), 'w') as f:
            json.dump({'failures': self.failures}, f)
        QTimer.singleShot(500, lambda: self.gui.quit(confirm_quit=False))

    # --- helpers --------------------------------------------------------------
    def check(self, ok, what):
        log(('PASS ' if ok else 'FAIL ') + what)
        if not ok:
            self.failures.append(what)

    def shot(self, name, widget=None):
        QApplication.processEvents()
        (widget or self.gui).grab().save(os.path.join(SHOTS, name + '.png'))
        log(f'screenshot {name}.png')

    def window(self, title_part):
        for w in QApplication.topLevelWidgets():
            if w.isVisible() and title_part.lower() in (w.windowTitle() or '').lower():
                return w
        return None

    def idle(self):
        jm = self.gui.job_manager
        busy = jm.unfinished_jobs() if hasattr(jm, 'unfinished_jobs') else jm.has_jobs()
        return not busy and not self.khk._running

    def connected(self):
        return self.gui.device_manager.is_device_connected

    @property
    def khk(self):
        return self.gui.iactions[PLUGIN]

    def connect_kobo(self):
        from calibre.devices.kobo.driver import KOBOTOUCH
        md = next(m for m in KOBOTOUCH.model_metadata() if m.model_name == 'Clara BW')
        log('connect simulated Kobo Clara BW at', KOBO)
        self.gui.connect_to_folder_named(KOBO, md)

    def eject(self):
        log('eject')
        self.gui.device_manager.umount_device()

    def nickel(self, *args):
        out = subprocess.run([shutil.which('python3') or '/usr/bin/python3', NICKEL, *args],
                             capture_output=True, text=True, env=dict(os.environ, KHK_KOBO_DIR=KOBO))
        log(f'nickel {" ".join(args)[:70]}: rc={out.returncode}\n    ' +
            (out.stdout + out.stderr).strip().replace('\n', '\n    '))
        return out

    def device_books(self):
        return list(self.gui.memory_view.model().db or ())

    def bookmark_rows(self, title_part):
        con = sqlite3.connect(os.path.join(KOBO, '.kobo', 'KoboReader.sqlite'))
        n = con.execute(
            'SELECT count(*) FROM Bookmark b JOIN content c ON c.ContentID = b.VolumeID '
            "WHERE c.ContentType = 6 AND c.Title LIKE ? AND b.Hidden != 'true'",
            (f'%{title_part}%',)).fetchone()[0]
        con.close()
        return n

    def calibre_records(self, book_id, state=None):
        from calibre_plugins.kobo_highlight_keeper import store
        recs = store.load_records(self.gui.current_db.new_api, book_id)
        return [r for r in recs if state is None or r['kobo']['state'] == state]

    def view(self, where):
        self.gui.location_manager._location_selected(where)


def wait(h, pred, timeout, what):
    return Until(pred, timeout, what)


def settle(h, what, timeout=120):
    """Wait until calibre and the plugin are idle (twice in a row)."""
    state = {'n': 0}

    def pred():
        state['n'] = state['n'] + 1 if h.idle() else 0
        return state['n'] >= 4
    return Until(pred, timeout, what)


def scenario(h):
    books = json.load(open(os.path.join(SANDBOX, 'books.json')))
    REF, LAD, BOT = books['890'], books['859'], books['852']
    log('=== scenario start; sandbox books', books)
    h.shot('01-library-before')

    # 1. connect an empty Kobo
    h.connect_kobo()
    yield wait(h, h.connected, 60, 'device connected')
    yield settle(h, 'initial sync after connect')
    h.check(h.khk.kobo() is not None, 'plugin sees the simulated Kobo as a Kobo device')
    m = h.khk.menu
    m.popup(h.gui.mapToGlobal(QPoint(300, 120)))
    yield wait(h, lambda: m.isVisible(), 5, 'menu shown')
    h.shot('02-plugin-menu', m)
    m.hide()

    # 2. send books the normal way (driver kepubifies; Ladies forced to EPUB)
    log('send Reformatory + Botchan (KEPUB) and No. 1 Ladies (EPUB)')
    h.gui.sync_to_device(None, False, send_ids=[REF, BOT])
    yield settle(h, 'send kepubs')
    h.gui.sync_to_device(None, False, specific_format='EPUB', send_ids=[LAD])
    yield settle(h, 'send epub')
    files = sorted(os.path.relpath(os.path.join(d, f), KOBO) for d, _, fs in os.walk(KOBO)
                   for f in fs if f.endswith('.epub'))
    log('files on device:', files)
    h.check(any(f.endswith('.kepub.epub') and 'Reformatory' in f for f in files), 'Reformatory sent as kepub')
    h.check(any(f.endswith('.epub') and not f.endswith('.kepub.epub') and 'Ladies' in f for f in files),
            'No. 1 Ladies sent as plain epub')
    h.view('main')
    yield settle(h, 'device view')
    h.shot('03-device-view-after-send')
    h.view('library')

    # 3. eject, "read" on the Kobo, reconnect -> auto sync pulls everything
    h.eject()
    yield wait(h, lambda: not h.connected(), 30, 'ejected')
    h.nickel('import')
    h.nickel('read', 'Reformatory', '--percent', '35')
    for text, note in REFORMATORY:
        h.nickel('highlight', 'Reformatory', '--text', text, *(['--note', note] if note else []))
    for text, note in BOTCHAN:
        h.nickel('highlight', 'Botchan', '--text', text, *(['--note', note] if note else []))
    h.nickel('dogear', 'Botchan', '--text', 'Better to go. I told the custodian')
    for text, note in LADIES:
        h.nickel('highlight', 'Ladies', '--text', text, *(['--note', note] if note else []))
    h.nickel('show')
    h.connect_kobo()
    yield wait(h, h.connected, 60, 'reconnected')
    yield settle(h, 'auto sync after reading')
    h.check(len(h.calibre_records(REF)) == 3, 'Reformatory: 3 highlights pulled into calibre')
    h.check(len(h.calibre_records(BOT)) == 3, 'Botchan: 2 highlights + 1 dogear pulled into calibre')
    h.check(len(h.calibre_records(LAD)) == 2, 'No. 1 Ladies (epub): 2 highlights pulled into calibre')
    h.shot('04-status-after-auto-sync')

    h.gui.iactions['Browse Annotations'].show_browser()
    yield wait(h, lambda: h.window('annotations') is not None, 15, 'annotations browser')
    yield Until(lambda: True, 1, 'render')
    br = h.window('annotations')
    h.shot('05-calibre-annotations-browser', br)
    br.close()

    # 4. delete Reformatory from the Kobo via calibre: the driver wipes Bookmarks
    ref_path = next(b.path for b in h.device_books() if 'Reformatory' in (b.title or ''))
    log('calibre deletes from device:', ref_path)
    before = h.bookmark_rows('Reformatory')
    h.gui.remove_paths([ref_path])
    yield settle(h, 'delete from device')
    h.check(before == 3 and h.bookmark_rows('Reformatory') == 0,
            f'calibre delete wiped the Kobo highlights ({before} -> {h.bookmark_rows("Reformatory")})')

    # 5. re-send it; the auto sync must wait until the Kobo has indexed it
    h.gui.sync_to_device(None, False, send_ids=[REF])
    yield settle(h, 're-send')
    yield settle(h, 'auto sync after re-send')
    h.check(h.bookmark_rows('Reformatory') == 0, 'nothing written before the Kobo indexed the book')

    # 6. eject -> Kobo indexes -> reconnect -> restored automatically
    h.eject()
    yield wait(h, lambda: not h.connected(), 30, 'ejected')
    h.nickel('import')
    h.connect_kobo()
    yield wait(h, h.connected, 60, 'reconnected')
    yield settle(h, 'auto restore')
    h.check(h.bookmark_rows('Reformatory') == 3, 'Reformatory highlights restored to the Kobo')
    out = h.nickel('verify')
    h.check(out.returncode == 0, 'independent check: every highlight on the Kobo points at its text')
    backups = os.listdir(os.path.join(SANDBOX, 'config', 'plugins', PLUGIN, 'kobo-db-backups'))
    h.check(len(backups) >= 1, f'Kobo database backed up before writing ({len(backups)} backup)')

    # 7. a highlight deleted on the Kobo stays deleted
    h.eject()
    yield wait(h, lambda: not h.connected(), 30, 'ejected')
    h.nickel('delete-highlight', 'Botchan', '--text', BOTCHAN[0][0])
    h.connect_kobo()
    yield wait(h, h.connected, 60, 'reconnected')
    yield settle(h, 'sync after device deletion')
    h.check(h.bookmark_rows('Botchan') == 2, 'deleted highlight not pushed back to the Kobo')
    deleted = h.calibre_records(BOT, 'deleted_on_device')
    h.check(len(deleted) == 1, 'kept in calibre, marked deleted on device')
    h.check(bool(deleted) and deleted[0]['style'].get('which') == 'strikeout',
            'deleted-on-device highlight shown struck out in calibre')
    h.gui.iactions['Browse Annotations'].show_browser()
    yield wait(h, lambda: h.window('annotations') is not None, 15, 'annotations browser')
    br = h.window('annotations')
    if hasattr(br, 'refresh'):
        br.refresh()
    yield Until(lambda: True, 1, 'render')
    h.shot('06-annotations-browser-after-device-delete', br)
    br.close()

    # 8. manual sync from the menu shows the report
    QTimer.singleShot(0, lambda: h.khk.sync(None, True))
    yield wait(h, lambda: h.window('sync history') is not None, 60, 'report dialog')
    yield Until(lambda: True, 1, 'render')
    dlg = h.window('sync history')
    h.shot('07-sync-history', dlg)
    dlg.reject()
    h.view('main')
    yield settle(h, 'device view')
    h.shot('08-device-view-final')
    h.view('library')
    log('=== scenario end')
