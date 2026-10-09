# Kobo Highlight Keeper

A calibre plugin that keeps your Kobo highlights, notes and bookmarks (dogears)
in calibre and puts them back on the Kobo when the device loses them: when you
delete and re-send a book, re-convert it to kepub, or edit it.

calibre is the main copy. The Kobo holds a working copy that can be rebuilt.

## What it does

When you pick **Kobo highlights → Sync with Kobo now** (or on connect, if set to sync both ways),
the plugin does the following for every book on the device that's in your
library:

1. **Pull.** New and edited highlights, notes and dogears go into calibre's
   own annotations store (`user=kobo`). You can browse and search them in
   **Browse annotations**.
2. **Detect deletions.** If you deleted a few highlights on the Kobo, they're
   marked *deleted on device* in calibre. They're kept, and not pushed back.
   If *all* of a book's highlights vanished, or the file changed, that's a
   re-import wipe, so they're restored.
3. **Restore.** Highlights calibre has but the Kobo doesn't are written back
   to `KoboReader.sqlite`. Each one is located in the file that's actually on
   the device, in this order (the same cascade as the W3C Web Annotation
   model and Hypothesis):
   1. its original Kobo position, if that still points at the same text
      (the usual case: the same book re-sent), reused unchanged
   2. an exact text search in its own chapter, using the surrounding words
      and its old position to choose between repeats
   3. the rest of the book (chapters split or renamed)
   4. a fuzzy match for edited text: short pieces of the passage vote for
      likely spots, which are then checked with an alignment. It tolerates
      edits anywhere in the passage and needs at least 32 characters and 75%
      similarity (85% under 80 characters).
   5. otherwise it's reported as an **orphan** and never placed in the wrong
      spot

   In stress tests on three real books: 0 false matches in 3,000 cross-book
   searches, and 566 of 568 randomly edited passages placed correctly.
4. **Repair.** Highlights still on the Kobo whose location no longer points
   at their text (span ids shifted after re-conversion) are moved to the
   right place.
5. **Changes made in calibre.** calibre is the main copy:
   - a highlight you delete in calibre (e.g. in **Browse annotations**) is
     deleted from the Kobo and doesn't come back
   - a note you edit in calibre is sent to the Kobo; if the note was also
     edited on the Kobo since the last sync, the newer edit wins

Each annotation also gets its chapter path from the book's table of
contents (e.g. *Part I › Chapter 1*). calibre uses it in the annotations
browser's **Export** (grouped by chapter), and so does the plugin's own
Markdown export.

Each annotation also gets a **calibre viewer position** (`spine_index` +
`start_cfi`/`end_cfi`) in the library's copy of the book, which is your KEPUB
if there is one, else the EPUB. That lets calibre's annotations browser list
highlights in reading order, and lets **Open in viewer** jump to the passage.
Positions follow calibre's own CFI rules (`src/pyj/read_book/cfi.pyj`):
- **Text offsets** count a parent's direct text only. A passage starting
  right after an inline element is written as the end of that element's
  text, which is the same spot, to avoid calibre's ambiguous form.
- **Chapter numbers** include the cover page calibre's viewer inserts before
  KEPUBs (`calibre.srv.render_book.create_cover_page`).

**In calibre's viewer** (setting, on by default): calibre's viewer only
shows its own annotation set, so active Kobo highlights are mirrored into it,
tagged `kobo_mirror` and sharing the Kobo highlight's ID.
- Highlights you made in the viewer yourself are never touched.
- A note edited or a highlight deleted *in the viewer* on a mirrored
  highlight goes back to the Kobo record, and to the Kobo on the next sync.
- Turning the setting off removes the mirrors.
- Dogears aren't mirrored.

`calibre-debug -e harness/verify_cfi.py` checks positions with a port of
calibre's decoder on books as calibre parses them: 1,982 of 1,982 decoded
to the exact passage. The GUI scenario checks chapter numbers against
calibre's real viewer preparation (`render_for_viewer`).

**Safety:**
- Before writing, the Kobo database is checked with SQLite's
  `quick_check`. If it's damaged, nothing is written and the report says so.
- Books on the Kobo that have annotations but aren't in the library (or
  aren't matched to it) are listed in the report. Their raw annotation rows
  are saved to `<calibre config>/plugins/Kobo Highlight Keeper/unmatched-books-annotations.json`.

A just-sent book has to be indexed by the Kobo before highlights can be
written for it. So the sequence is: send → eject → let the Kobo import it →
reconnect, and the restore happens automatically on that next connect.

Before any write, the device database is backed up to
`<calibre config>/plugins/Kobo Highlight Keeper/kobo-db-backups/`
(last 20 copies are kept).

### Storage format

Each annotation is a normal calibre annotation, plus a `kobo` key that holds:

- the original Kobo `Bookmark` row, every column, unchanged
- a location-independent *anchor*: chapter, normalized text, about 48
  characters of text before and after, and its offset

See `kobo_highlight_keeper/sync.py` for the full format.

## Performance

The automatic sync runs once per connection. It reads the whole device with
two queries and calibre with one, and a book file is opened only when one of
its annotations is new, edited or missing, or the file changed (tracked by
size and modification time). calibre is written only when something actually
changed. `python3 harness/bench.py` (200 books on the device, 30 with 25
highlights each):

| Case | Time |
|---|---|
| Connect, nothing changed (the everyday case; copy-only or full) | 0.07 s, no writes |
| Copy-only connect after 5 books were wiped | 0.26 s, Kobo untouched |
| Restore 5 wiped books | 0.28 s |
| First sync of 750 highlights (one-off, incl. calibre positions) | 1.5 s |

Times are on a Mac SSD; over USB the first two stay small because they
barely touch book files.

## Layout

```
kobo_highlight_keeper/   pure-Python core (no calibre imports): book text model,
                         anchors, Kobo DB access, sync rules, Markdown export
plugin/                  calibre GUI plugin (bundles the core as core/)
scripts/build_plugin.py  -> dist/Kobo-Highlight-Keeper.zip
harness/                 sandbox + simulated Kobo + scripted GUI test
tests/                   unit + lifecycle tests
```

## Releases

Download the zip from the repo's **Releases** page and install it in calibre
(Preferences → Plugins → **Load plugin from file**).

To publish a release: bump `version` in `plugin/__init__.py`, commit, then
tag and push:

```bash
git tag v0.2.0 && git push origin v0.2.0
```

`.github/workflows/release.yml` checks the tag matches the plugin version,
runs the tests, builds `Kobo-Highlight-Keeper-<version>.zip` and publishes
the release with the commit list since the previous tag.

On GitHub the tests that need copies of real books skip themselves, because
the sandbox isn't in the repo. Run the full suite locally before tagging.

## Build and install

```bash
python3 scripts/build_plugin.py
calibre-customize -a dist/Kobo-Highlight-Keeper.zip   # or Preferences → Plugins → Load from file
```

## Testing without a real Kobo

Everything runs in `sandbox/` (gitignored), separate from the real library
and config:

```bash
python3 harness/setup_sandbox.py --force   # new library with copies of 3 real books,
                                           # empty simulated Kobo Clara BW, separate config
python3 -m unittest discover -s tests -v   # anchors, fuzzy matching, full lifecycle
python3 harness/bench.py                   # sync timings on a 200-book simulated Kobo
python3 harness/run_gui.py                 # scripted run of the real calibre GUI, offscreen,
                                           # screenshots in sandbox/screenshots/
```

- **Library.** Book files are *copied* from `~/Calibre Library`, and its
  `metadata.db` is never opened. The sandbox calibre uses its own config,
  cache and temp dirs (`CALIBRE_*_DIRECTORY`), plus its own single-instance
  lock and GUI socket (`harness/launch_gui.py`).
- **Running next to your normal calibre is fine.** The full GUI scenario has
  run alongside an open calibre without affecting it.
- **No real Kobo can be touched.** The sandbox calibre has USB/MTP device
  detection switched off, and `run_gui.py` refuses to start while a drive
  containing a Kobo database is mounted.
- **Kobo.** The simulated device is a folder with a real-firmware schema
  (`harness/schema/nickel-176.sql`, DB version 176). calibre's own **Kobo
  driver** is attached to it through "connect to folder as device" (Clara
  BW). Sending, kepubify and deleting all run the real driver code.
- **Kobo firmware.** `harness/fake_nickel.py` stands in for the firmware: it
  indexes books after "eject", creates highlights, deletes them, and checks
  with its own parser that every highlight points at its text.

### Checked on a real Kobo

On a Kobo Clara BW, deleting a highlighted book from the Kobo and re-sending
it brought its highlights back. That confirms the Kobo shows highlight rows
written while it's connected.

Still unconfirmed on a real device (marked ASSUMED in the code):

- epub `#point(...)` offsets being in UTF-8 bytes (kepub span offsets are
  characters). This only matters for books sent as plain EPUB rather than
  KEPUB.
- what the Kobo does with existing rows when an indexed file is replaced in
  place, e.g. after editing a book and re-sending it without deleting it
  first. `fake_nickel.py import --wipe-on-change` simulates the harsher case.

## Moving from the Annotations plugin

The menu item imports for the selected books. To import every book at once,
with calibre closed:

```bash
calibre-debug -e scripts/import_annotations_plugin.py -- "/path/to/Calibre Library" [--dry-run]
```

It also places highlights for books that only have an ORIGINAL_KEPUB or
ORIGINAL_EPUB. Running it again adds nothing. Afterwards the old column can
be removed with `calibredb remove_custom_column <label>`.

## When a Kobo is connected

Set in **Settings…**:

- **Copy new highlights from the Kobo** (default). Nothing is written to the
  Kobo automatically. If changes are waiting (highlights the Kobo lost, notes
  or deletions made in calibre), the status bar says how many, and **Sync
  with Kobo now** writes them.
- **Sync both ways.** Writes happen automatically on connect.
- **Do nothing.**

The status bar and sync history show only what happened in that sync. Use
the views below to look at existing highlights.

## Menu

| Item | What it does |
|---|---|
| **Sync with Kobo now** | Both ways: copies from the Kobo, and writes to it (restores, repairs, notes and deletions made in calibre). Greyed out without a Kobo. |
| **Sync selected books with Kobo** | The same, limited to the books selected in the library. |
| **Copy highlights from Kobo only** | Copies new and edited highlights into calibre and never writes to the Kobo. Changes for the Kobo wait for the next **Sync with Kobo now**. |
| **View Kobo highlights…** | The plugin's own reading view; also opens when you click the toolbar button. Books on the left (most recently highlighted first, with covers). The chosen book's highlights on the right, grouped by chapter, with notes and dates. Each card has **Open in viewer**, **Copy**, **Edit note / Add note** and **Delete**. Highlights deleted on the Kobo also have **Put back on the Kobo**. Edits change calibre at once (and calibre's viewer copies); the Kobo is updated at the next **Sync with Kobo now**. Searches highlights and notes across all books; highlights deleted on the Kobo can be shown. |
| **Open calibre's annotations browser** | calibre's own browser, set to show only Kobo highlights. calibre remembers that filter; change *User* back to see others. |
| **Export highlights of selected books…** | Markdown files, one per book, grouped by chapter. |
| **Import highlights from the Annotations plugin…** | For the selected books: brings in highlights the older Annotations plugin saved in its custom column. Each one is placed by finding its text in the book; ones that can't be found, or are already present, are skipped and listed. Asks first; the old column is left unchanged. Imported highlights go onto the Kobo at the next **Sync with Kobo now** (matched, not duplicated, if the Kobo still has them). |
| **Send back highlights deleted on the Kobo** | Highlights you deleted on the Kobo stay in calibre, struck out. This puts them back on the Kobo for the selected books. |
| **Sync history** | What the last 30 syncs did. |
| **Settings…** | Automatic sync, calibre viewer copies, Kobo database backups. |

## Credits / references

- calibre KoboTouch driver: chapter `ContentID` formats; `delete_via_sql`
  wiping `Bookmark` rows
- Kobo Utilities (davidfor, jlarres): chapter row formats, device-job pattern
- kobo2calibre (degiz, MIT): sample kepub Bookmark rows
- Calibre-Web-NextGen (GPL-3): colour codes, text-quote re-anchoring idea,
  firmware observations
- kobodeck: Nickel schema dump (DB version 176)
