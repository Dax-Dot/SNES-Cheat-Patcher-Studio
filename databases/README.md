# Bundled databases

- `nointro_snes_db.json`: the supplied No-Intro export, used to identify ROM
  revisions by CRC32, SHA-256 and size.
- `CATALOG_CLEANUP_REPORT.json`: full traceability of the games and cheats that
  were excluded when the integrated catalog was built.

The runtime catalog that the application loads lives at
`src/snes_cheat_patcher/data/cheat_catalog.json` (single canonical copy, shipped
inside the package). It is the output of `tools/build_catalog.py`.

Application and catalog versions are independent. The application is currently
version 0.6.2, while the bundled cleaned catalog remains version 0.5.1 because
its validated contents have not changed.

The catalog never links games by name similarity. It only accepts a revision
when CRC32, SHA-256 and size all match the No-Intro entry and an exact ROM was
available during the catalog build. Every retained cheat passed that ROM's
detected mapper. Runtime validation is kept as a final safety check.

## Reproducing the catalog

`tools/build_catalog.py` takes the raw Game Genie database, the No-Intro database
and a directory containing exact No-Intro ROM revisions. ROMs are read only for
validation and are never copied into the project. The No-Intro database
(`nointro_snes_db.json`) is bundled here, but the **raw cheats and commercial
ROMs are not redistributed**.

```bash
PYTHONPATH=src python3 tools/build_catalog.py \
  YOUR_RAW_CHEATS.json nointro_snes_db.json \
  src/snes_cheat_patcher/data/cheat_catalog.json \
  databases/CATALOG_CLEANUP_REPORT.json \
  /path/to/your/no-intro-snes-romset
```
