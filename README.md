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
   to `KoboReader.sqlite`. The plugin finds the highlighted text again in the
   file that's actually on the device, so it works across kepub/epub and after
   edits. If the text can't be found, the highlight is reported as an
   **orphan** and never placed in the wrong spot.
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
python3 -m unittest discover -s tests -v   # anchors + full lifecycle against the fake Kobo
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
