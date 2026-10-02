"""A reading-focused view of the Kobo highlights stored in calibre."""

import html
from datetime import datetime

from calibre.gui2 import question_dialog
from calibre.gui2.widgets2 import Dialog
from qt.core import (
    QAbstractItemView, QApplication, QCheckBox, QDialogButtonBox, QHBoxLayout, QIcon, QLabel, QLineEdit,
    QInputDialog, QListWidget, QListWidgetItem, QPalette, QPixmap, QSize, QSplitter, Qt, QTextBrowser, QTimer,
    QVBoxLayout,
)

from calibre_plugins.kobo_highlight_keeper import store
from calibre_plugins.kobo_highlight_keeper.core.export import book_markdown
from calibre_plugins.kobo_highlight_keeper.core.sync import ACTIVE, DELETED, USER, USER_TYPE, apply_state_style

ACCENT = {'yellow': '#e3b505', 'red': '#d9436b', 'blue': '#3b82c4', 'green': '#3fa34d'}


def _position(rec):
    a = rec['kobo'].get('anchor') or {}
    return (a.get('spine_index', rec.get('spine_index', 1 << 30)), a.get('start', 0))


def _now():
    from datetime import timezone
    return datetime.now(timezone.utc).isoformat()


def _date(ts):
    try:
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone()
        return f'{dt.day} {dt:%b %Y}'
    except Exception:
        return ''


class KoboHighlightsView(Dialog):
    def __init__(self, gui, on_change=None):
        self.gui = gui
        self.on_change = on_change  # (book_id, records): e.g. refresh the calibre viewer copies
        self.db = gui.current_db.new_api
        self.books = {}
        Dialog.__init__(self, 'Kobo Highlights', 'khk-highlights-view', parent=gui)
        self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint)

    def sizeHint(self):
        return QSize(1100, 750)

    # --- layout ---------------------------------------------------------------
    def setup_ui(self):
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit(self)
        self.search.setPlaceholderText('Search highlights and notes in all books')
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        top.addWidget(self.search, 1)
        self.show_deleted = QCheckBox('Show highlights deleted on the Kobo', self)
        self.show_deleted.toggled.connect(self.refresh)
        top.addWidget(self.show_deleted)
        lay.addLayout(top)

        split = QSplitter(self)
        self.book_list = QListWidget(self)
        self.book_list.setIconSize(QSize(36, 54))
        self.book_list.setSpacing(2)
        self.book_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.book_list.currentItemChanged.connect(self.show_current)
        split.addWidget(self.book_list)
        self.page = QTextBrowser(self)
        self.page.setOpenLinks(False)
        self.page.anchorClicked.connect(self.link_clicked)
        self.page.document().setDocumentMargin(18)
        split.addWidget(self.page)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([300, 800])
        lay.addWidget(split, 1)

        bottom = QHBoxLayout()
        self.status = QLabel(self)
        bottom.addWidget(self.status, 1)
        self.bb.setStandardButtons(QDialogButtonBox.StandardButton.Close)
        self.copy_button = self.bb.addButton('Copy book’s highlights', QDialogButtonBox.ButtonRole.ActionRole)
        self.copy_button.setIcon(QIcon.ic('edit-copy.png'))
        self.copy_button.clicked.connect(self.copy_book)
        bottom.addWidget(self.bb)
        lay.addLayout(bottom)

    # --- data -----------------------------------------------------------------
    def reload(self, select_ids=()):
        self.books = {}
        for a in self.db.all_annotations(restrict_to_user=(USER_TYPE, USER)):
            rec = a['annotation']
            if 'kobo' in rec:
                self.books.setdefault(a['book_id'], []).append(rec)
        self.refresh(select_ids=select_ids)

    def visible_records(self, book_id):
        q = self.search.text().strip().lower()
        out = []
        for rec in self.books.get(book_id, ()):
            state = rec['kobo']['state']
            if state != ACTIVE and not (state == DELETED and self.show_deleted.isChecked()):
                continue
            if q and q not in (rec.get('highlighted_text') or '').lower() and q not in (rec.get('notes') or '').lower():
                continue
            out.append(rec)
        return sorted(out, key=_position)

    def refresh(self, *args, select_ids=()):
        current = self.current_book_id()
        self.book_list.blockSignals(True)
        self.book_list.clear()
        rows = []
        for book_id in self.books:
            recs = self.visible_records(book_id)
            if recs:
                latest = max(r['timestamp'] for r in recs)
                rows.append((latest, book_id, recs))
        rows.sort(reverse=True)  # most recently highlighted first
        for _, book_id, recs in rows:
            title = self.db.field_for('title', book_id)
            authors = ' & '.join(self.db.field_for('authors', book_id))
            n = sum(1 for r in recs if r['type'] == 'highlight')
            item = QListWidgetItem(f'{title}\n{authors} · {n} highlight{"s" if n != 1 else ""}')
            item.setData(Qt.ItemDataRole.UserRole, book_id)
            item.setIcon(self.cover_icon(book_id))
            item.setToolTip(title)
            self.book_list.addItem(item)
        self.book_list.blockSignals(False)
        wanted = [i for i in (*select_ids, current) if i is not None]
        for row in range(self.book_list.count()):
            if self.book_list.item(row).data(Qt.ItemDataRole.UserRole) in wanted:
                self.book_list.setCurrentRow(row)
                break
        else:
            if self.book_list.count():
                self.book_list.setCurrentRow(0)
        total = sum(len(self.visible_records(b)) for b in self.books)
        self.status.setText(f'{total} highlights in {self.book_list.count()} books'
                            + (f' matching “{self.search.text().strip()}”' if self.search.text().strip() else ''))
        self.show_current()

    def cover_icon(self, book_id):
        pm = QPixmap()
        data = self.db.cover(book_id)
        if data and pm.loadFromData(data):
            return QIcon(pm.scaled(QSize(72, 108), Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation))
        return QIcon.ic('book.png')

    def current_book_id(self):
        item = self.book_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    # --- rendering ------------------------------------------------------------
    def show_current(self, *args):
        book_id = self.current_book_id()
        if book_id is None:
            self.page.setHtml('<p style="margin-top:40px" align="center">No Kobo highlights to show.</p>')
            self.copy_button.setEnabled(False)
            return
        self.copy_button.setEnabled(True)
        self.page.setHtml(self.book_html(book_id))

    def book_html(self, book_id):
        pal = self.page.palette()
        text = pal.color(QPalette.ColorRole.Text).name()
        muted = pal.color(QPalette.ColorRole.PlaceholderText).name()
        card = pal.color(QPalette.ColorRole.AlternateBase).name()
        link = pal.color(QPalette.ColorRole.Link).name()
        q = self.search.text().strip()
        recs = self.visible_records(book_id)
        highlights = [r for r in recs if r['type'] == 'highlight']
        notes = sum(1 for r in highlights if r.get('notes'))
        marks = len(recs) - len(highlights)
        title = html.escape(self.db.field_for('title', book_id))
        authors = html.escape(' & '.join(self.db.field_for('authors', book_id)))
        summary = [f'{len(highlights)} highlight{"s" if len(highlights) != 1 else ""}']
        if notes:
            summary.append(f'{notes} note{"s" if notes != 1 else ""}')
        if marks:
            summary.append(f'{marks} bookmark{"s" if marks != 1 else ""}')
        out = [f'<html><body style="color:{text}">',
               f'<div style="font-size:20pt; font-weight:600">{title}</div>',
               f'<div style="color:{muted}; margin-bottom:6px">{authors} · {" · ".join(summary)}</div>']
        chapter = None
        for rec in recs:
            here = ' › '.join(rec.get('toc_family_titles') or [])
            if here and here != chapter:
                out.append(f'<div style="margin-top:22px; margin-bottom:4px; font-size:11pt; font-weight:600; '
                           f'color:{muted}">{html.escape(here).upper()}</div>')
                chapter = here
            out.append(self.card_html(book_id, rec, q, card, muted, link))
        out.append('</body></html>')
        return ''.join(out)

    def card_html(self, book_id, rec, q, card, muted, link):
        deleted = rec['kobo']['state'] == DELETED
        date = _date(rec['timestamp'])
        key = rec['kobo']['raw'].get('BookmarkID')

        def a(kind, label):
            return f'<a href="{kind}:{book_id}:{key}" style="color:{link}; text-decoration:none">{label}</a>'

        open_link = a('open', 'Open in viewer')
        if rec['type'] == 'bookmark':
            return (f'<p style="color:{muted}; margin-left:6px">🔖 Bookmark · {date}'
                    + (f' · {open_link}' if rec.get('pos_type') == 'epubcfi' else '')
                    + f' · {a("delete", "Delete")}</p>')
        bar = muted if deleted else ACCENT.get((rec.get('style') or {}).get('which'), ACCENT['yellow'])
        quote = self.mark(rec.get('highlighted_text') or '', q)
        if deleted:
            quote = f'<s>{quote}</s>'
        note = ''
        if rec.get('notes'):
            note = (f'<div style="margin-top:10px; margin-left:14px">'
                    f'<span style="color:{muted}; font-size:8pt; font-weight:600">NOTE</span><br>'
                    f'<i>{self.mark(rec["notes"], q)}</i></div>')
        meta = [date]
        if deleted:
            meta += ['deleted on the Kobo', a('putback', 'Put back on the Kobo')]
        meta += [open_link, a('copy', 'Copy'), a('note', 'Edit note' if rec.get('notes') else 'Add note'),
                 a('delete', 'Delete')]
        return (f'<table width="100%" cellspacing="0" cellpadding="0" style="margin-top:10px"><tr>'
                f'<td bgcolor="{bar}" width="4"></td>'
                f'<td bgcolor="{card}" style="padding:12px 14px">'
                f'<div style="font-size:13pt; line-height:140%">{quote}</div>{note}'
                f'<div style="color:{muted}; font-size:9pt; margin-top:8px">{" · ".join(meta)}</div>'
                f'</td></tr></table>')

    @staticmethod
    def mark(text, q):
        text = ' '.join(text.split())
        if not q:
            return html.escape(text)
        out, low, i = [], text.lower(), 0
        ql = q.lower()
        while True:
            j = low.find(ql, i)
            if j < 0:
                out.append(html.escape(text[i:]))
                return ''.join(out)
            out.append(html.escape(text[i:j]))
            out.append(f'<span style="background-color:#ffe066; color:#000">{html.escape(text[j:j + len(q)])}</span>')
            i = j + len(q)

    # --- actions --------------------------------------------------------------
    def find(self, book_id, key):
        return next((r for r in self.books.get(book_id, ()) if r['kobo']['raw'].get('BookmarkID') == key), None)

    def link_clicked(self, url):
        kind, _, rest = url.toString().partition(':')
        book_id, _, key = rest.partition(':')
        book_id = int(book_id)
        rec = self.find(book_id, key)
        if rec is None:
            return
        if kind == 'open':
            fmt = store.target_format(self.db, book_id)
            if rec['type'] == 'bookmark':
                cfi = rec.get('pos')
            else:
                cfi = f'epubcfi(/{2 * (rec.get("spine_index", 0) + 1)}{rec.get("start_cfi", "/2")})'
            self.gui.iactions['View'].view_format_by_id(book_id, fmt, open_at=cfi)
        elif kind == 'copy':
            QApplication.clipboard().setText(self.plain(book_id, [rec]))
            self.status.setText('Copied to the clipboard')
        elif kind == 'delete':
            what = 'bookmark' if rec['type'] == 'bookmark' else 'highlight'
            text = ' '.join((rec.get('highlighted_text') or '').split())[:120]
            if question_dialog(self, f'Delete {what}?',
                               f'Delete this {what} from calibre and from the Kobo?'
                               + (f'<br><br><i>{html.escape(text)}</i>' if text else '')
                               + '<br><br>The Kobo is updated at the next <b>Sync with Kobo now</b>.'):
                self.delete(book_id, key)
        elif kind == 'note':
            text, ok = QInputDialog.getMultiLineText(self, 'Note', 'Note for this highlight:', rec.get('notes') or '')
            if ok and text.strip() != (rec.get('notes') or '').strip():
                self.edit_note(book_id, key, text.strip())
        elif kind == 'putback':
            self.put_back(book_id, key)

    # --- changes (calibre now, the Kobo at the next full sync) ------------------
    def delete(self, book_id, key):
        def tombstone(r):
            mark = {'removed': True, 'type': r['type'], 'timestamp': _now()}
            mark['uuid' if r['type'] == 'highlight' else 'title'] = r.get('uuid') or r.get('title')
            return mark
        self.change(book_id, key, tombstone, 'Deleted')

    def edit_note(self, book_id, key, text):
        self.change(book_id, key, lambda r: dict(r, notes=text, timestamp=_now()),
                    'Note saved' if text else 'Note removed')

    def put_back(self, book_id, key):
        def reactivate(r):
            r['kobo'].update(state=ACTIVE, on_device=False)
            apply_state_style(r)
            return r
        self.change(book_id, key, reactivate, 'Will be put back')

    def change(self, book_id, key, fn, done):
        records = []
        for r in store.load_records(self.db, book_id):  # includes calibre's deletion markers
            if 'kobo' in r and r['kobo']['raw'].get('BookmarkID') == key:
                r = fn(r)
            records.append(r)
        store.save_records(self.db, book_id, records)
        if self.on_change is not None:
            self.on_change(book_id, records)
        scroll = self.page.verticalScrollBar().value()
        self.reload(select_ids=[book_id])
        QTimer.singleShot(0, lambda: self.page.verticalScrollBar().setValue(scroll))
        self.status.setText(f'{done}; the Kobo is updated at the next Sync with Kobo now')

    def plain(self, book_id, recs):
        title = self.db.field_for('title', book_id)
        authors = ' & '.join(self.db.field_for('authors', book_id))
        return book_markdown(title, authors, recs)

    def copy_book(self):
        book_id = self.current_book_id()
        if book_id is not None:
            QApplication.clipboard().setText(self.plain(book_id, self.visible_records(book_id)))
            self.status.setText('Copied this book’s highlights to the clipboard (Markdown)')
