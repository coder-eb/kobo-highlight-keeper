# Kobo Highlight Keeper

A calibre plugin that keeps your Kobo highlights, notes and bookmarks (dogears)
in calibre and puts them back on the Kobo when the device loses them: when you
delete and re-send a book, re-convert it to kepub, or edit it.

calibre is the main copy. The Kobo holds a working copy that can be rebuilt.

## What it does

When a Kobo is connected (or when you pick **Kobo annotations → Sync now**),
the plugin does four things for every book on the device that's in your
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
| Connect, nothing changed (the everyday case) | 0.05 s, no writes |
| Restore 5 wiped books | 0.20 s |
| First sync of 750 highlights (one-off) | 0.9 s |

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
- **Normal calibre open?** `run_gui.py` refuses to start while it's running,
  unless you pass `--alongside`. The lock and socket are isolated, but a full
  run alongside an open calibre hasn't been verified. The non-GUI steps
  (`setup_sandbox.py`, the tests) are safe to run any time.
- **Kobo.** The simulated device is a folder with a real-firmware schema
  (`harness/schema/nickel-176.sql`, DB version 176). calibre's own **Kobo
  driver** is attached to it through "connect to folder as device" (Clara
  BW). Sending, kepubify and deleting all run the real driver code.
- **Kobo firmware.** `harness/fake_nickel.py` stands in for the firmware: it
  indexes books after "eject", creates highlights, deletes them, and checks
  with its own parser that every highlight points at its text.

### What the simulation can't prove

These depend on the real Kobo firmware and are marked ASSUMED in the code:

- that the Kobo shows rows written while it was connected (same as Kobo
  Utilities / kobo2calibre writes, but not checked here)
- epub `#point(...)` offsets being in UTF-8 bytes (kepub span offsets are
  characters)
- what the Kobo does with existing rows when an indexed file is replaced
  (`fake_nickel.py import --wipe-on-change` simulates the harsher case)

A one-book check on the real device (with the automatic DB backup) settles
all three.

## Credits / references

- calibre KoboTouch driver: chapter `ContentID` formats; `delete_via_sql`
  wiping `Bookmark` rows
- Kobo Utilities (davidfor, jlarres): chapter row formats, device-job pattern
- kobo2calibre (degiz, MIT): sample kepub Bookmark rows
- Calibre-Web-NextGen (GPL-3): colour codes, text-quote re-anchoring idea,
  firmware observations
- kobodeck: Nickel schema dump (DB version 176)
