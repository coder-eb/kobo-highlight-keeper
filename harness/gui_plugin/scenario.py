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

OWN_VIEWER_HIGHLIGHT = {
    'type': 'highlight', 'uuid': 'made-in-the-viewer', 'timestamp': '2026-01-01T10:00:00.000Z',
    'highlighted_text': 'a highlight made in calibre\u2019s viewer', 'spine_index': 5,
    'spine_name': 'x.xhtml', 'start_cfi': '/2/4/2/1:0', 'end_cfi': '/2/4/2/1:10'}

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

    def viewer_spine(self, path):
        """Spine of the book as calibre's viewer prepares it (render_for_viewer)."""
        import tempfile
        from calibre.srv.render_book import render_for_viewer
        out = tempfile.mkdtemp(dir=os.path.join(SANDBOX, 'tmp'))
        render_for_viewer(path, out, 'scenario')
        with open(os.path.join(out, 'calibre-book-manifest.json')) as f:
            return json.load(f)['spine']

    def viewer_annotations(self, book_id):
        db = self.gui.current_db.new_api
        return [a['annotation'] for a in db.all_annotations(restrict_to_user=('local', 'viewer'),
                                                             restrict_to_book_ids={book_id})]

    def status_text(self):
        return self.gui.status_bar.currentMessage() or ''

    def kobo_note(self, text_start):
        con = sqlite3.connect(os.path.join(KOBO, '.kobo', 'KoboReader.sqlite'))
        row = con.execute('SELECT Annotation FROM Bookmark WHERE Text LIKE ?', (text_start + '%',)).fetchone()
        con.close()
        return row[0] if row else None

    def calibre_records(self, book_id, state=None):
        from calibre_plugins.kobo_highlight_keeper import store
        recs = [r for r in store.load_records(self.gui.current_db.new_api, book_id) if 'kobo' in r]
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
    db = h.gui.current_db.new_api
    db.merge_annotations_for_book(REF, 'KEPUB', [OWN_VIEWER_HIGHLIGHT], user_type='local', user='viewer')
    h.shot('01-library-before')
    m = h.khk.menu
    m.popup(h.gui.mapToGlobal(QPoint(300, 120)))
    yield wait(h, lambda: m.isVisible(), 5, 'menu shown')
    h.shot('01b-menu-without-kobo', m)
    h.check(not h.khk.sync_all_action.isEnabled(), 'without a Kobo, sync is greyed out')
    m.hide()

    # 1. connect an empty Kobo
    h.connect_kobo()
    yield wait(h, h.connected, 60, 'device connected')
    yield settle(h, 'initial sync after connect')
    h.check(h.khk.kobo() is not None, 'plugin sees the simulated Kobo as a Kobo device')
    m = h.khk.menu
    m.popup(h.gui.mapToGlobal(QPoint(300, 120)))
    yield wait(h, lambda: m.isVisible(), 5, 'menu shown')
    h.shot('02-plugin-menu', m)
    h.check(h.khk.sync_all_action.isEnabled() and h.khk.copy_action.isEnabled(),
            'with a Kobo, sync and copy-only are available')
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
    # calibre viewer positions: present, and the spine index points at the
    # same chapter in the book as calibre's viewer actually prepares it
    db = h.gui.current_db.new_api
    for book_id in (REF, BOT, LAD):
        fmt = 'KEPUB' if 'KEPUB' in db.formats(book_id) else 'EPUB'
        viewer_spine = h.viewer_spine(db.format_abspath(book_id, fmt))
        highlights = [r for r in h.calibre_records(book_id) if r['type'] == 'highlight']
        ok = all(r.get('start_cfi') and r['kobo'].get('cfi_exact') and
                 viewer_spine[r['spine_index']] == r['spine_name'] for r in highlights)
        h.check(ok and highlights, f'{fmt} book {book_id}: viewer positions point at the right chapters '
                                   f'(viewer spine has {len(viewer_spine)} items)')
    vr = h.viewer_annotations(REF)
    mirrors = [a for a in vr if a.get('kobo_mirror') and not a.get('removed')]
    h.check(len(mirrors) == 3 and all(m.get('start_cfi') for m in mirrors),
            "Reformatory's 3 Kobo highlights are in calibre's viewer annotations")
    h.check(any(a.get('uuid') == 'made-in-the-viewer' for a in vr), 'own viewer highlight untouched')

    h.khk.show_calibre_browser()
    yield wait(h, lambda: h.window('annotations') is not None, 15, 'annotations browser')
    yield Until(lambda: True, 1, 'render')
    br = h.window('annotations')
    h.shot('05-calibre-annotations-browser', br)
    br.close()

    # the plugin's own view
    h.gui.library_view.select_rows({REF})
    h.khk.show_view()
    yield wait(h, lambda: h.khk._view is not None and h.khk._view.isVisible(), 15, 'Kobo Highlights view')
    yield Until(lambda: True, 1, 'render')
    v = h.khk._view
    h.check(v.current_book_id() == REF and v.book_list.count() == 3, 'view opens on the selected book, lists 3 books')
    h.shot('05b-kobo-highlights-view', v)
    v.search.setText('mother')
    yield Until(lambda: True, 1, 'render')
    h.check(v.book_list.count() == 2 and '2 highlights in 2 books' in v.status.text(),
            'search matches highlights and notes ("mother": a Botchan highlight, a Reformatory note)')
    h.shot('05c-kobo-highlights-search', v)
    v.search.clear()
    v.close()

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
    yield settle(h, 'automatic copy on connect')
    h.check(h.bookmark_rows('Reformatory') == 0, 'automatic copy-only sync did not write to the Kobo')
    h.check('waiting to be written' in h.status_text(), 'status bar says changes are waiting: '
            + h.status_text()[:120])
    h.shot('06z-status-changes-waiting')
    QTimer.singleShot(0, lambda: h.khk.sync(None, False))  # Kobo highlights → Sync with Kobo now
    yield settle(h, 'sync with Kobo now')
    h.check(h.bookmark_rows('Reformatory') == 3, 'Sync with Kobo now restored the highlights')
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
    h.gui.library_view.select_rows({BOT})
    h.khk.show_view()
    yield wait(h, lambda: h.khk._view.isVisible(), 15, 'view')
    h.khk._view.show_deleted.setChecked(True)
    yield Until(lambda: True, 1, 'render')
    h.shot('06a-view-deleted-on-kobo', h.khk._view)
    h.khk._view.show_deleted.setChecked(False)
    h.khk._view.close()
    h.gui.iactions['Browse Annotations'].show_browser()
    yield wait(h, lambda: h.window('annotations') is not None, 15, 'annotations browser')
    br = h.window('annotations')
    if hasattr(br, 'refresh'):
        br.refresh()
    yield Until(lambda: True, 1, 'render')
    h.shot('06-annotations-browser-after-device-delete', br)
    br.close()

    # 8. calibre is the main copy: delete a highlight and edit a note in
    #    calibre (the same calls calibre's annotation browser makes)
    db = h.gui.current_db.new_api
    ann = list(db.all_annotations(restrict_to_user=('local', 'kobo'), restrict_to_book_ids={REF}))
    to_delete = next(a for a in ann if (a['annotation'].get('highlighted_text') or '').startswith('His hope'))
    to_edit = next(a for a in ann if (a['annotation'].get('highlighted_text') or '').startswith('That'))
    db.delete_annotations([to_delete['id']])
    db.update_annotations({to_edit['id']: dict(to_edit['annotation'], notes='Edited in calibre')})
    log('calibre: deleted one Reformatory highlight, edited one note')
    # ... and in calibre's viewer: edit a note on one mirrored highlight, delete another
    later = '2099-01-01T00:00:00+00:00'
    sorry = next(a for a in h.viewer_annotations(REF) if (a.get('highlighted_text') or '').startswith('\u201cI\u2019m sorry'))
    db.merge_annotations_for_book(REF, 'KEPUB', [dict(sorry, notes='Edited in the viewer', timestamp=later)],
                                  user_type='local', user='viewer')
    mother = next(a for a in h.viewer_annotations(BOT) if (a.get('highlighted_text') or '').startswith('After my mother'))
    db.merge_annotations_for_book(BOT, 'KEPUB', [{'removed': True, 'uuid': mother['uuid'], 'type': 'highlight',
                                                  'timestamp': later}], user_type='local', user='viewer')
    log('viewer: edited one note, deleted one highlight')

    # 9. manual sync from the menu sends both and shows the report
    QTimer.singleShot(0, lambda: h.khk.sync(None, True))
    yield wait(h, lambda: h.window('sync history') is not None, 60, 'report dialog')
    yield Until(lambda: True, 1, 'render')
    dlg = h.window('sync history')
    h.shot('07-sync-history', dlg)
    dlg.reject()
    h.check(h.bookmark_rows('Reformatory') == 2, 'highlight deleted in calibre was deleted on the Kobo')
    h.check(h.kobo_note('That') == 'Edited in calibre', 'note edited in calibre was sent to the Kobo')
    h.check(h.kobo_note('\u201cI\u2019m sorry') == 'Edited in the viewer', 'note edited in the viewer was sent to the Kobo')
    h.check(h.bookmark_rows('Botchan') == 1, 'highlight deleted in the viewer was deleted on the Kobo (dogear stays)')
    live = [a for a in h.viewer_annotations(REF) if a.get('kobo_mirror') and not a.get('removed')]
    h.check(len(live) == 2 and all(a['highlighted_text'].startswith(('That\u2019s how', '\u201cI\u2019m sorry'))
                                   for a in live),
            'viewer mirrors follow deletions made in calibre')
    out = h.nickel('verify')
    h.check(out.returncode == 0, 'independent check after calibre-side changes')
    QTimer.singleShot(0, lambda: h.khk.sync(None, False))
    yield settle(h, 'second sync after calibre edits')
    h.check(h.bookmark_rows('Reformatory') == 2 and len(h.calibre_records(REF)) == 2,
            'deleted highlight did not come back on the next sync')
    h.check(all(r.get('toc_family_titles') for r in h.calibre_records(REF)),
            'highlights carry chapter titles')
    h.gui.iactions['Browse Annotations'].show_browser()
    yield wait(h, lambda: h.window('annotations') is not None, 15, 'annotations browser')
    br = h.window('annotations')
    if hasattr(br, 'refresh'):
        br.refresh()
    yield Until(lambda: True, 1, 'render')
    h.shot('07b-annotations-browser-chapters', br)
    br.close()
    # 10. act on highlights from the plugin's own view
    h.gui.library_view.select_rows({REF})
    h.khk.show_view()
    yield wait(h, lambda: h.khk._view.isVisible(), 15, 'view')
    v = h.khk._view

    def key(book, start):
        return next(r['kobo']['raw']['BookmarkID'] for r in v.books[book]
                    if (r.get('highlighted_text') or '').startswith(start))
    v.edit_note(REF, key(REF, '\u201cI\u2019m sorry'), 'Edited in the view')
    v.delete(REF, key(REF, 'That\u2019s how'))
    v.show_deleted.setChecked(True)
    v.put_back(BOT, key(BOT, 'From the time'))
    v.show_deleted.setChecked(False)
    yield Until(lambda: True, 1, 'render')
    h.gui.library_view.select_rows({REF})
    v.reload(select_ids=[REF])
    yield Until(lambda: True, 1, 'render')
    h.shot('09-view-after-edits', v)
    v.close()
    QTimer.singleShot(0, lambda: h.khk.sync(None, False))
    yield settle(h, 'sync after edits in the view')
    h.check(h.kobo_note('\u201cI\u2019m sorry') == 'Edited in the view', 'note edited in the view reached the Kobo')
    h.check(h.bookmark_rows('Reformatory') == 1, 'highlight deleted in the view was deleted on the Kobo')
    h.check(h.bookmark_rows('Botchan') == 2, 'highlight put back from the view is on the Kobo again')
    out = h.nickel('verify')
    h.check(out.returncode == 0, 'independent check after edits in the view')

    # 11. import highlights saved by the older Annotations plugin
    import html as _html
    from calibre_plugins.kobo_highlight_keeper.core.epubtext import Book as _Book
    lib_book = _Book(db.format_abspath(LAD, 'KEPUB'))
    ch = max(lib_book.chapters(), key=lambda c: len(c.text))
    paras = [p.strip() for p in ch.text.split('\n') if len(p.strip()) > 80]
    old = [(paras[5][:120], ''), (paras[8][:150], 'a note from the old plugin'),
           (paras[10][-40:].strip() + paras[11][:40].strip(), ''),  # paragraphs run together, as it stored them
           (LADIES[1][0], '')]                                      # already in Kobo Highlight Keeper
    old_html = '<div class="user_annotations">' + ''.join(
        f'<div class="annotation" hash="h{i}" reader="KoboTouch"><table><tr><td class="location">x</td>'
        f'<td class="timestamp" uts="1770460620"></td></tr></table><p class="highlight">{_html.escape(t)}</p>'
        + (f'<p class="note">{_html.escape(nt)}</p>' if nt else '') + '</div>'
        for i, (t, nt) in enumerate(old)) + '</div>'
    db.set_field('#mm_annotations', {LAD: old_html})
    h.gui.library_view.select_rows({LAD})
    n = h.khk.import_legacy(confirm=False)
    h.check(n == 3, f'imported 3 highlights from the Annotations plugin column (got {n}); 1 duplicate skipped')
    imported = [r for r in h.calibre_records(LAD) if r['kobo']['history']
                and r['kobo']['history'][-1]['event'].startswith('imported')]
    h.check(len(imported) == 3 and all(r.get('start_cfi') and r.get('toc_family_titles') for r in imported),
            'imported highlights have calibre viewer positions and chapters')
    QTimer.singleShot(0, lambda: h.khk.sync(None, False))
    yield settle(h, 'sync after import')
    h.check(h.bookmark_rows('Ladies') == 5, 'imported highlights were put on the Kobo (2 + 3)')
    out = h.nickel('verify', 'Ladies')
    h.check(out.returncode == 0, 'independent check: imported highlights sit on the right words on the Kobo')

    h.view('main')
    yield settle(h, 'device view')
    h.shot('08-device-view-final')
    h.view('library')
    log('=== scenario end')
