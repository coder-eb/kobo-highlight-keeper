import os
from functools import partial

from calibre.gui2 import Dispatcher, choose_dir, error_dialog, info_dialog
from calibre.gui2.actions import InterfaceAction
from calibre.gui2.device import device_signals
from calibre.utils.filenames import ascii_filename
from qt.core import (
    QDialog, QDialogButtonBox, QIcon, QMenu, QPlainTextEdit, QTimer, QToolButton, QVBoxLayout,
)

from calibre_plugins.kobo_highlight_keeper import config as cfg
from calibre_plugins.kobo_highlight_keeper import store
from calibre_plugins.kobo_highlight_keeper.core.engine import summarize
from calibre_plugins.kobo_highlight_keeper.core.export import book_markdown
from calibre_plugins.kobo_highlight_keeper.core.viewer import feedback as viewer_feedback
from calibre_plugins.kobo_highlight_keeper.core.viewer import mirrored as viewer_mirrored
from calibre_plugins.kobo_highlight_keeper.devicejob import run_sync


def viewer_spine_offset(path, fmt):
    """How many pages calibre's viewer inserts before the book's own spine.

    When preparing a non-EPUB format (KEPUB included) that has a cover image,
    the viewer adds a generated 'titlepage.html' as the first spine item
    (calibre.srv.render_book.create_cover_page); EPUBs are left as they are.
    """
    if not path or fmt.upper() == 'EPUB':
        return 0
    from calibre.ebooks.oeb.polish.container import EpubContainer
    from calibre.ebooks.oeb.polish.cover import find_cover_image
    from calibre.utils.logging import default_log
    try:
        return 1 if find_cover_image(EpubContainer(path, default_log), strict=True) else 0
    except Exception:
        return 0


class ReportDialog(QDialog):
    def __init__(self, parent, text):
        super().__init__(parent)
        self.setWindowTitle('Kobo Highlight Keeper: sync history')
        self.resize(900, 560)
        lay = QVBoxLayout(self)
        view = QPlainTextEdit(self)
        view.setReadOnly(True)
        view.setPlainText(text)
        lay.addWidget(view)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class KoboHighlightKeeperAction(InterfaceAction):
    name = 'Kobo Highlight Keeper'
    action_spec = ('Kobo highlights', None, 'Keep Kobo highlights in calibre and restore them to the Kobo', None)
    popup_type = QToolButton.ToolButtonPopupMode.MenuButtonPopup
    action_type = 'current'

    def genesis(self):
        self._running = False
        self.qaction.setIcon(QIcon.ic('highlight.png'))
        self.menu = QMenu(self.gui)
        self.qaction.setMenu(self.menu)
        m = self.menu
        m.setToolTipsVisible(True)
        self._view = None
        self.qaction.triggered.connect(self.show_view)  # the button itself opens the view

        def item(key, text, tip, triggered, icon=None):
            ac = self.create_menu_action(m, key, text, icon=icon, description=tip, triggered=triggered)
            ac.setToolTip(tip)
            return ac

        # with the Kobo
        self.sync_all_action = item(
            'khk-sync-all', 'Sync with Kobo now',
            'Copy new and edited highlights from the Kobo into calibre, and put back any the Kobo has lost. '
            'Runs automatically when a Kobo is connected.',
            partial(self.sync, None, True), icon='sync.png')
        self.sync_sel_action = item(
            'khk-sync-sel', 'Sync selected books with Kobo',
            'Same as Sync with Kobo now, for the books selected in the library only.',
            self.sync_selected)
        m.addSeparator()
        # looking at highlights
        item('khk-view', 'View Kobo highlights…',
             'Read your Kobo highlights and notes book by book, grouped by chapter, with search.',
             self.show_view, icon='highlight.png')
        item('khk-browse', 'Open calibre’s annotations browser',
             "calibre's own annotations browser, set to show only Kobo highlights.",
             self.show_calibre_browser, icon='edit-find.png')
        m.addSeparator()
        # with the selected books
        self.export_action = item(
            'khk-export', 'Export highlights of selected books…',
            'Save the highlights and notes of the selected books as Markdown files, one per book.',
            self.export_selected, icon='save.png')
        self.undelete_action = item(
            'khk-undelete', 'Send back highlights deleted on the Kobo',
            'Highlights you delete on the Kobo stay in calibre (struck out). '
            'This puts them back on the Kobo for the selected books at the next sync.',
            self.undelete_selected)
        m.addSeparator()
        item('khk-report', 'Sync history',
             'What the recent syncs did: highlights copied, restored, repaired or removed.',
             self.show_report, icon='dialog_information.png')
        item('khk-config', 'Settings…', 'Automatic sync, calibre viewer, Kobo database backups.',
             lambda: self.interface_action_base_plugin.do_user_config(self.gui), icon='config.png')
        m.aboutToShow.connect(self._update_menu)
        self._synced_this_connection = False
        device_signals.device_connection_changed.connect(self._connection_changed)
        device_signals.device_metadata_available.connect(self._metadata_available)

    def show_view(self):
        from calibre_plugins.kobo_highlight_keeper.view import KoboHighlightsView
        if self._view is None:
            self._view = KoboHighlightsView(self.gui)
        self._view.reload(select_ids=self.gui.library_view.get_selected_ids())
        self._view.show()
        self._view.raise_()
        self._view.activateWindow()

    def show_calibre_browser(self):
        from calibre.gui2 import gprefs
        from qt.core import QComboBox
        gprefs['browse_annots_restrict_to_user'] = 'local:kobo'
        action = self.gui.iactions['Browse Annotations']
        action.show_browser()
        # already-open browser: switch its user filter too
        for box in action.browser.findChildren(QComboBox):
            row = box.findData('local:kobo')
            if row > -1:
                box.setCurrentIndex(row)

    def _update_menu(self):
        """Grey out what can't run right now, and say why."""
        connected = self.kobo() is not None
        selected = bool(self.gui.library_view.get_selected_ids())
        self.sync_all_action.setEnabled(connected)
        self.sync_all_action.setText('Sync with Kobo now' if connected else 'Sync with Kobo now (connect a Kobo)')
        self.sync_sel_action.setEnabled(connected and selected)
        self.sync_sel_action.setText('Sync selected books with Kobo' if connected and selected else
                                     'Sync selected books with Kobo (' +
                                     ('connect a Kobo' if not connected else 'select books') + ')')
        for ac, text in ((self.export_action, 'Export highlights of selected books…'),
                         (self.undelete_action, 'Send back highlights deleted on the Kobo')):
            ac.setEnabled(selected)
            ac.setText(text if selected else text.rstrip('…') + ' (select books)')

    # --- device ---------------------------------------------------------------
    def kobo(self):
        from calibre.devices.kobo.driver import KOBO
        dev = self.gui.device_manager.connected_device
        if isinstance(dev, KOBO) and getattr(dev, '_main_prefix', None):
            return dev
        return None

    def _connection_changed(self, connected):
        if not connected:
            self._synced_this_connection = False

    def _metadata_available(self):
        # calibre emits this after every booklist refresh (each send/delete);
        # auto-sync only on the first one per connection.
        if self._synced_this_connection or not cfg.prefs['auto_sync'] or self.kobo() is None:
            return
        self._synced_this_connection = True
        # Let calibre finish matching device books to the library first.
        QTimer.singleShot(250, partial(self.sync, None, False))

    def _tasks(self, only_ids):
        db = self.gui.current_db.new_api
        found = []
        for book in self.gui.memory_view.model().db or ():
            path = getattr(book, 'path', '') or ''
            if not path.lower().endswith('.epub'):
                continue
            book_id = getattr(book, 'application_id', None)
            if book_id is None and getattr(book, 'uuid', None):
                book_id = db.lookup_by_uuid(book.uuid)
            if book_id is None or (only_ids is not None and book_id not in only_ids):
                continue
            found.append((book_id, path))
        records = store.load_all_records(db, [i for i, _ in found])
        viewer = store.load_viewer_annotations(db, [i for i, _ in found])
        tasks = []
        for i, p in found:
            fmt = store.target_format(db, i)
            # Notes edited / highlights deleted in calibre's viewer go to the Kobo in this sync.
            recs, changed = viewer_feedback(records.get(i, []), viewer.get((i, fmt), []))
            if changed:
                store.save_records(db, i, recs)
                records[i] = recs
            lib = db.format_abspath(i, fmt)
            tasks.append({'key': i, 'title': db.field_for('title', i), 'path': p, 'records': records.get(i, []),
                          'library_path': lib, 'viewer_spine_offset': partial(viewer_spine_offset, lib, fmt)})
        return tasks

    def sync_selected(self):
        ids = set(self.gui.library_view.get_selected_ids())
        if not ids:
            return error_dialog(self.gui, 'No books selected', 'Select books in the library first.', show=True)
        self.sync(ids, True)

    def sync(self, only_ids=None, interactive=True):
        dev = self.kobo()
        if dev is None:
            if interactive:
                error_dialog(self.gui, 'No Kobo connected', 'Connect a Kobo reader first.', show=True)
            return
        if self._running:
            return
        tasks = self._tasks(only_ids)
        self._running = True
        self.gui.device_manager.create_job(
            run_sync, Dispatcher(partial(self._sync_done, interactive)),
            description='Sync Kobo annotations',
            args=[dev, tasks, cfg.backup_dir(), cfg.prefs['keep_backups']])
        self.gui.status_bar.show_message('Kobo annotations: syncing…')

    def _sync_done(self, interactive, job):
        self._running = False
        if job.failed:
            return self.gui.job_exception(job, dialog_title='Kobo annotation sync failed')
        out = job.result
        db = self.gui.current_db.new_api
        for r in out['results']:
            plan = r.get('plan')
            if plan is not None and plan.changed:
                store.save_records(db, r['key'], plan.all_records())
        self._refresh_viewer_mirrors(db, out['results'])
        if self._view is not None and self._view.isVisible():
            self._view.reload()
        unmatched = getattr(out['results'], 'unmatched', [])
        if unmatched:
            cfg.save_unmatched(unmatched)
        head, tot, detail = summarize(out['results'])
        if unmatched:
            detail += '\n    (saved to ' + cfg.UNMATCHED_FILE + ')'
        backups = ''.join(f'\nKobo database backed up to: {b}' for b in out['backups'])
        eventful = any(v for k, v in tot.items() if k != 'books') or bool(unmatched)
        if interactive or eventful:  # auto-sync runs after every device refresh
            cfg.save_report(f'{head}\n{backups}\n\n{detail}\n')
        msg = 'Kobo annotations: ' + head
        if tot['orphans'] or tot['errors']:
            msg += '  (see Kobo annotations → Show last sync report)'
        self.gui.status_bar.show_message(msg, 15000)
        if interactive:
            self.show_report()

    def _refresh_viewer_mirrors(self, db, results):
        ids = [r['key'] for r in results if r.get('plan') is not None]
        if not ids:
            return
        viewer = store.load_viewer_annotations(db, ids)
        for r in results:
            if r.get('plan') is None:
                continue
            fmt = store.target_format(db, r['key'])
            new = viewer_mirrored(r['plan'].all_records(), viewer.get((r['key'], fmt), []),
                                  enabled=cfg.prefs['show_in_viewer'])
            if new is not None:
                store.save_viewer_annotations(db, r['key'], fmt, new)

    # --- library ----------------------------------------------------------------
    def show_report(self):
        ReportDialog(self.gui, cfg.load_report()).exec()

    def export_selected(self):
        db = self.gui.current_db.new_api
        ids = self.gui.library_view.get_selected_ids()
        with_annots = [i for i in ids if any('kobo' in r for r in store.load_records(db, i))]
        if not with_annots:
            return info_dialog(self.gui, 'Nothing to export',
                               'None of the selected books have Kobo annotations.', show=True)
        dest = choose_dir(self.gui, 'khk-export-dir', 'Export Kobo annotations to')
        if not dest:
            return
        for i in with_annots:
            title = db.field_for('title', i)
            authors = ' & '.join(db.field_for('authors', i))
            md = book_markdown(title, authors, store.load_records(db, i))
            with open(os.path.join(dest, ascii_filename(f'{title} - {authors}')[:150] + '.md'), 'w',
                      encoding='utf-8') as f:
                f.write(md)
        info_dialog(self.gui, 'Exported', f'Wrote {len(with_annots)} Markdown file(s) to {dest}', show=True)

    def undelete_selected(self):
        db = self.gui.current_db.new_api
        n = sum(store.reactivate_deleted(db, i) for i in self.gui.library_view.get_selected_ids())
        where = 'now' if self.kobo() is not None else 'the next time the Kobo is connected'
        info_dialog(self.gui, 'Highlights to send back',
                    f'{n} highlight(s) deleted on the Kobo will be put back on it {where}.' if n else
                    'None of the selected books have highlights that were deleted on the Kobo.', show=True)
        if n and self.kobo() is not None:
            self.sync(None, True)
