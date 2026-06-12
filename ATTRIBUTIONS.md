# Attributions

SNES Cheat Patcher Studio does not include SNES ROMs, BIOS files, save files, or
copyrighted game assets.

## Game-Genie-Good-Guy

Parts of the SNES Game Genie decoding logic were adapted from
[Mte90/Game-Genie-Good-Guy](https://github.com/Mte90/Game-Genie-Good-Guy/),
a GPL-3.0-licensed project.

SNES Cheat Patcher Studio reimplements and extends that functionality in Python
with its own ROM inspection, mapper-aware address translation, static patch
validation, ROM-revision identification, conflict handling, checksum repair,
tests, catalog integration, and Tkinter interface.

Game-Genie-Good-Guy remains copyright its respective authors and contributors.
Its original source code is available from the project linked above.

## Cheat data

Bundled Game Genie codes and descriptions are attributed to
[GameHacking.org](https://gamehacking.org/). This project does not claim
ownership of those codes or descriptions.

Catalog entries are filtered so that codes targeting RAM, I/O, SRAM, or
unsupported dynamic mapper windows are not presented as static ROM patches.

## ROM metadata

ROM-identification metadata is attributed to
[No-Intro](https://no-intro.org/) and its
[DAT-o-MATIC](https://datomatic.no-intro.org/) service.

The bundled metadata contains names, hashes, sizes, and cartridge information;
it does not contain ROM data.

## License

SNES Cheat Patcher Studio is distributed under GPL-3.0-or-later. See
[LICENSE](LICENSE), [COPYRIGHT](COPYRIGHT), and the notices above for third-party
credits and copyright information.
