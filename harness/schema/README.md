# Kobo database schema (test fixture)

`nickel-176.sql` is the `KoboReader.sqlite` schema (database version 176) as
dumped from Kobo firmware, taken from
[fkaduk/kobodeck](https://github.com/fkaduk/kobodeck/blob/main/testdata/nickel-schema-176.sql)
(AGPL-3.0). It's used only to build the simulated Kobo in `harness/`, and is
not shipped in the plugin.

`harness/setup_sandbox.py` lowercases one column (`DbVersion.version`) when
loading it, because calibre's Kobo driver reads it by that name.
