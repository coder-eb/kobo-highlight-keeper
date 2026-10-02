#!/usr/bin/env python3
"""A stand-in for the Kobo firmware ("Nickel") acting on the simulated device.

It deliberately does NOT import the plugin's location code: highlights are
created and verified with its own small parser, so a passing `verify` means
the plugin wrote rows that point at the right text by an independent check.

Behaviour that is OBSERVED elsewhere vs ASSUMED here is marked inline.

Commands (BOOK = case-insensitive substring of title or file name):
  import                         index new/replaced book files (what Nickel does after eject)
  read BOOK --percent 40         mark as currently reading
  highlight BOOK --text "..."    [--note "..."] [--nth 1]
  dogear BOOK --text "..."       bookmark the page where the text starts
  delete-highlight BOOK --text "..."   the reader deletes a highlight on the device
  verify [BOOK]                  check every visible highlight points at its own Text
  show                           list books and annotations
"""

import argparse
import posixpath
import re
import sqlite3
import sys
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from sandbox_env import KOBO_DB, KOBO_DIR

ONBOARD = 'file:///mnt/onboard/'
SPAN = re.compile(r'<span[^>]*\bclass="koboSpan"[^>]*\bid="(kobo\.\d+\.\d+)"[^>]*>(.*?)</span>', re.S)
TAG = re.compile(r'<[^>]+>')


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]


def db():
    con = sqlite3.connect(KOBO_DB)
    con.row_factory = sqlite3.Row
    return con


def book_files():
    return sorted(p for p in KOBO_DIR.rglob('*') if p.suffix.lower() == '.epub' and '.kobo' not in p.parts)


def volume_id(path):
    return ONBOARD + path.relative_to(KOBO_DIR).as_posix()


def unescape(s):
    import html
    return html.unescape(TAG.sub('', s))


class EBook:
    def __init__(self, path):
        self.path = path
        self.zf = zipfile.ZipFile(path)
        self.kepub = path.name.lower().endswith('.kepub.epub')
        c = ET.fromstring(self.zf.read('META-INF/container.xml'))
        self.opf = next(e for e in c.iter() if e.tag.endswith('rootfile')).get('full-path')
        self.opf_dir = posixpath.dirname(self.opf)
        opf = ET.fromstring(self.zf.read(self.opf))
        ns = '{http://www.idpf.org/2007/opf}'
        dc = '{http://purl.org/dc/elements/1.1/}'
        self.title = (opf.findtext(f'.//{dc}title') or path.stem).strip()
        self.author = ' & '.join(a.text for a in opf.iter(f'{dc}creator') if a.text)
        items = {i.get('id'): i for i in opf.iter(f'{ns}item')}
        self.spine = []
        for r in opf.iter(f'{ns}itemref'):
            it = items.get(r.get('idref'))
            if it is not None and 'html' in (it.get('media-type') or ''):
                self.spine.append(posixpath.normpath(posixpath.join(self.opf_dir, it.get('href'))))

    def rel(self, href):
        return posixpath.relpath(href, self.opf_dir) if self.opf_dir else href

    def chapter_id(self, vid, i, href):
        if self.kepub:
            return f'{vid[len("file://"):]}!{self.opf_dir}!{self.rel(href)}'
        return f'{vid}#({i}){href}'

    # -- independent text model: list of (locator, text) chars -------------
    def kepub_spans(self, href):
        raw = self.zf.read(href).decode('utf-8')
        return [(sid, unescape(body)) for sid, body in SPAN.findall(raw)]

    def epub_nodes(self, href):
        root = ET.fromstring(self.zf.read(href))
        body = next(e for e in root if e.tag.endswith('body'))
        out = []

        def walk(el, path):
            out.append((f'{path}/1', el.text or ''))
            for i, ch in enumerate(el):
                walk(ch, f'{path}/{2 * (i + 1)}')
                out.append((f'{path}/{2 * (i + 1) + 1}', ch.tail or ''))
        walk(body, f'/1/{2 * (list(root).index(body) + 1)}')
        return out

    def units(self, href):
        return self.kepub_spans(href) if self.kepub else self.epub_nodes(href)

    def find(self, phrase, nth=1):
        """(chapter index, href, start(loc, off), end(loc, off)) ignoring whitespace."""
        target = re.sub(r'\s+', '', phrase)
        seen = 0
        for ci, href in enumerate(self.spine):
            chars = []
            for loc, text in self.units(href):
                for off, c in enumerate(text):
                    if not c.isspace():
                        chars.append((loc, off, c))
            flat = ''.join(c for _, _, c in chars)
            i = flat.find(target)
            while i >= 0:
                seen += 1
                if seen == nth:
                    s, e = chars[i], chars[i + len(target) - 1]
                    return ci, href, (s[0], s[1]), (e[0], e[1] + 1), i / max(1, len(flat))
                i = flat.find(target, i + 1)
        raise SystemExit(f'text not found in {self.title}: {phrase!r}')

    def text_at(self, href, start, end):
        """Text between two (loc, offset) points, by this module's own parser."""
        units = self.units(href)
        locs = [u[0] for u in units]
        if start[0] not in locs or end[0] not in locs:
            return None
        a, b = locs.index(start[0]), locs.index(end[0])
        if b < a:
            return None
        if a == b:
            return units[a][1][start[1]:end[1]]
        parts = [units[a][1][start[1]:]] + [u[1] for u in units[a + 1:b]] + [units[b][1][:end[1]]]
        return ''.join(parts)


def epub_off(text_node, chars):  # ASSUMED: epub #point offsets are UTF-8 bytes
    return len(text_node[:chars].encode('utf-8'))


def epub_chars(text_node, nbytes):
    return len(text_node.encode('utf-8')[:nbytes].decode('utf-8', 'ignore'))


def kobo_path(eb, href, loc, off, units=None):
    if eb.kepub:
        return 'span#' + loc.replace('.', '\\.'), off
    text = dict(units or eb.units(href))[loc]
    b = epub_off(text, off)
    return f'{href}#point({loc}:{b})', b


def parse_path(eb, href, path, off):
    if eb.kepub:
        m = re.search(r'span#(kobo(?:\\?\.\d+){2})', path or '')
        return (m.group(1).replace('\\', ''), off) if m else None
    m = re.match(r'^(.*)#point\(([/\d]+):(\d+)\)$', path or '')
    if not m:
        return None
    text = dict(eb.units(href)).get(m.group(2))
    return (m.group(2), epub_chars(text, int(m.group(3)))) if text is not None else None


def pick_book(con, query):
    q = query.lower()
    hits = [p for p in book_files() if q in p.name.lower()]
    if not hits:
        for r in con.execute("SELECT ContentID, Title FROM content WHERE ContentType = 6"):
            if q in (r['Title'] or '').lower() and r['ContentID'].startswith(ONBOARD):
                hits.append(KOBO_DIR / r['ContentID'][len(ONBOARD):])
    if len(hits) != 1:
        raise SystemExit(f'{query!r} matches {len(hits)} books: {[h.name for h in hits]}')
    return hits[0]


# --- commands -----------------------------------------------------------------

def cmd_import(args):
    con = db()
    files = {volume_id(p): p for p in book_files()}
    known = {r['ContentID']: r for r in con.execute(
        "SELECT ContentID, ___FileSize FROM content WHERE ContentType = 6 AND ContentID LIKE 'file:///%'")}
    for vid in set(known) - set(files):
        # OBSERVED (calibre delete_via_sql does the same): a book removed from
        # the filesystem loses its rows, including its Bookmarks.
        con.execute('DELETE FROM content WHERE ContentID = ? OR BookID = ?', (vid, vid))
        con.execute('DELETE FROM Bookmark WHERE VolumeID = ?', (vid,))
        print(f'removed missing book {vid}')
    for vid, path in files.items():
        eb = EBook(path)
        size = path.stat().st_size
        chapter_ids = {eb.chapter_id(vid, i, h) for i, h in enumerate(eb.spine)}
        existing = {r[0] for r in con.execute(
            'SELECT ContentID FROM content WHERE BookID = ? AND ContentType = 9', (vid,))}
        if vid in known and existing == chapter_ids:
            continue
        if vid in known and args.wipe_on_change:
            # ASSUMED alternative: firmware treats a changed file as a new book.
            con.execute('DELETE FROM Bookmark WHERE VolumeID = ?', (vid,))
        # ASSUMED default (consistent with Calibre-Web-NextGen's OBSERVED notes):
        # a re-indexed book keeps its Bookmark rows, which may now point at
        # chapters/spans that no longer exist.
        con.execute('DELETE FROM content WHERE BookID = ?', (vid,))
        con.execute(
            'INSERT OR REPLACE INTO content (ContentID, ContentType, MimeType, BookTitle, Title, '
            'Attribution, ___FileSize, ___PercentRead, ReadStatus, IsDownloaded, Accessibility, '
            '___UserID, ___SyncTime, DateLastRead, ChapterIDBookmarked, ___NumPages) '
            "VALUES (?, 6, ?, NULL, ?, ?, ?, 0, 0, 'true', -1, '', ?, NULL, NULL, -1)",
            (vid, 'application/x-kobo-epub+zip' if eb.kepub else 'application/epub+zip',
             eb.title, eb.author, size, now()))
        for i, href in enumerate(eb.spine):
            con.execute(
                'INSERT INTO content (ContentID, ContentType, MimeType, BookID, BookTitle, Title, '
                "VolumeIndex, ___UserID, ___FileSize, ___PercentRead, ReadStatus, IsDownloaded) "
                "VALUES (?, 9, ?, ?, ?, ?, ?, '', 0, 0, 0, 'true')",
                (eb.chapter_id(vid, i, href), 'application/xhtml+xml', vid, eb.title,
                 posixpath.basename(href), i))
            if eb.kepub:  # kepub TOC rows
                con.execute(
                    'INSERT INTO content (ContentID, ContentType, MimeType, BookID, BookTitle, Title, '
                    "VolumeIndex, ___UserID, ___FileSize, ___PercentRead, ReadStatus, IsDownloaded) "
                    "VALUES (?, 899, 'application/x-kobo-epub+zip', ?, ?, ?, ?, '', 0, 0, 0, 'true')",
                    ('file://' + eb.chapter_id(vid, i, href) + '-1', vid, eb.title,
                     posixpath.basename(href), i))
        print(f'indexed {path.name} ({len(eb.spine)} chapters)')
    con.commit()


def cmd_read(args):
    con = db()
    vid = volume_id(pick_book(con, args.book))
    con.execute("UPDATE content SET ReadStatus = 1, ___PercentRead = ?, DateLastRead = ? "
                "WHERE ContentID = ?", (args.percent, now(), vid))
    con.commit()
    print(f'reading {vid} at {args.percent}%')


def cmd_highlight(args, dogear=False):
    con = db()
    path = pick_book(con, args.book)
    vid, eb = volume_id(path), EBook(path)
    if not con.execute('SELECT 1 FROM content WHERE ContentID = ?', (vid,)).fetchone():
        raise SystemExit('book not indexed yet: run `import` first (eject/reconnect)')
    ci, href, s, e, prog = eb.find(args.text, args.nth)
    units = eb.units(href)
    sp, so = kobo_path(eb, href, *s, units)
    ep, eo = kobo_path(eb, href, *e, units)
    if dogear:
        ep, eo = sp, so
    t = now()
    row = {
        'BookmarkID': str(uuid.uuid4()), 'VolumeID': vid, 'ContentID': eb.chapter_id(vid, ci, href),
        'StartContainerPath': sp, 'StartContainerChildIndex': -99, 'StartOffset': so,
        'EndContainerPath': ep, 'EndContainerChildIndex': -99, 'EndOffset': eo,
        'Text': None if dogear else args.text, 'Annotation': getattr(args, 'note', None),
        'DateCreated': t, 'DateModified': t, 'ChapterProgress': round(prog, 4), 'Hidden': 'false',
        'Version': '', 'Type': 'dogear' if dogear else ('note' if getattr(args, 'note', None) else 'highlight'),
        'Color': 0, 'Published': 'false',
    }
    cols = ', '.join(row)
    con.execute(f'INSERT INTO Bookmark ({cols}) VALUES ({", ".join("?" * len(row))})', list(row.values()))
    con.commit()
    print(f'{row["Type"]} {row["BookmarkID"][:8]} in {path.name}: {sp}:{so} -> {ep}:{eo}')


def cmd_delete_highlight(args):
    con = db()
    vid = volume_id(pick_book(con, args.book))
    target = re.sub(r'\s+', ' ', args.text).strip()
    rows = [r for r in con.execute('SELECT BookmarkID, Text FROM Bookmark WHERE VolumeID = ?', (vid,))
            if re.sub(r'\s+', ' ', r['Text'] or '').strip() == target]
    if not rows:
        raise SystemExit('no such highlight')
    # ASSUMED: for sideloaded books the row is deleted outright.
    con.execute('DELETE FROM Bookmark WHERE BookmarkID = ?', (rows[0]['BookmarkID'],))
    con.commit()
    print(f'deleted {rows[0]["BookmarkID"][:8]}')


def chapter_href(eb, content_id):
    if eb.kepub:
        parts = content_id.split('!', 2)
        rel = '/'.join(p for p in parts[1:] if p)
    else:
        rel = content_id.split(')', 1)[1] if ')' in content_id else ''
    for h in eb.spine:
        if h == rel or h.endswith('/' + rel):
            return h
    return None


def cmd_verify(args):
    con = db()
    paths = [pick_book(con, args.book)] if args.book else book_files()
    bad = good = 0
    for path in paths:
        vid, eb = volume_id(path), EBook(path)
        for r in con.execute("SELECT * FROM Bookmark WHERE VolumeID = ? AND Hidden != 'true'", (vid,)):
            href = chapter_href(eb, r['ContentID'])
            ok, got = False, None
            if href:
                s = parse_path(eb, href, r['StartContainerPath'], r['StartOffset'])
                e = parse_path(eb, href, r['EndContainerPath'], r['EndOffset'])
                if s and e:
                    if r['Type'] == 'dogear':
                        ok = True
                    else:
                        got = eb.text_at(href, s, e)
                        ok = got is not None and re.sub(r'\s+', '', got) == re.sub(r'\s+', '', r['Text'] or '')
            good += ok
            bad += not ok
            mark = 'OK  ' if ok else 'BAD '
            print(f'{mark}{path.name[:40]:40} {r["Type"] or "":9} {(r["Text"] or "<dogear>")[:50]!r}'
                  + ('' if ok else f'  -> device shows {got!r:.60}'))
    print(f'verify: {good} ok, {bad} bad')
    sys.exit(1 if bad else 0)


def cmd_show(args):
    con = db()
    for b in con.execute("SELECT ContentID, Title, ReadStatus, ___PercentRead, ___FileSize FROM content "
                         "WHERE ContentType = 6 ORDER BY Title"):
        n = con.execute('SELECT count(*) FROM Bookmark WHERE VolumeID = ?', (b['ContentID'],)).fetchone()[0]
        print(f'{b["Title"][:40]:40} status={b["ReadStatus"]} {b["___PercentRead"]}% '
              f'size={b["___FileSize"]} bookmarks={n}')
    unindexed = [p.name for p in book_files()
                 if not con.execute('SELECT 1 FROM content WHERE ContentID = ?', (volume_id(p),)).fetchone()]
    if unindexed:
        print('not indexed yet:', unindexed)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('import')
    p.add_argument('--wipe-on-change', action='store_true')
    p = sub.add_parser('read')
    p.add_argument('book')
    p.add_argument('--percent', type=int, default=25)
    for name in ('highlight', 'dogear'):
        p = sub.add_parser(name)
        p.add_argument('book')
        p.add_argument('--text', required=True)
        p.add_argument('--nth', type=int, default=1)
        if name == 'highlight':
            p.add_argument('--note')
    p = sub.add_parser('delete-highlight')
    p.add_argument('book')
    p.add_argument('--text', required=True)
    p = sub.add_parser('verify')
    p.add_argument('book', nargs='?')
    sub.add_parser('show')
    args = ap.parse_args()
    {'import': cmd_import, 'read': cmd_read, 'highlight': cmd_highlight,
     'dogear': lambda a: cmd_highlight(a, dogear=True), 'delete-highlight': cmd_delete_highlight,
     'verify': cmd_verify, 'show': cmd_show}[args.cmd](args)


if __name__ == '__main__':
    main()
