# Changelog

Notable user-facing changes are documented here.

## 0.6.2 - 2026-06-12

- Updated project identity and public documentation for the first GitHub release.
- Added multiplatform automated checks for Windows, Linux, and macOS.
- Improved package metadata and Windows portable-build preparation.
- Kept the patching engine and bundled cheat catalog unchanged.
- Clarified attribution for SNES Game Genie decoding logic adapted from
  Mte90/Game-Genie-Good-Guy.

## 0.6.1 - 2026-06-11

- Completed the English interface and messages.
- Prevented ROM or patch-queue changes while an output ROM is being written.
- Improved recovery from unexpected save errors.
- Fixed conflict replacement and Live Log refresh behavior.
- Added the complete GPL-3.0-or-later license text.

## 0.6.0 - 2026-06-11

- Added SNES-themed application branding, window icons, and header logo.
- Added an **About** dialog with version, credits, license, and legal-use notes.
- Added acknowledgement of Mte90's Game-Genie-Good-Guy project.

## 0.5.9 - 2026-06-11

- Simplified source-code launching across Windows, Linux, and macOS.
- Cleaned generated files and obsolete development artifacts from the project.
- Prepared a reproducible Windows portable build.

## 0.5.8 - 2026-06-11

- Removed **Select all** to avoid activating mutually exclusive cheats.
- Fixed `run_gui.py` so it always loads the code bundled with the project.

## 0.5.7 - 2026-06-11

- Renamed the status panel to **Live Log**.
- Replaced accumulating messages with a concise summary of the current ROM,
  active cheats, and latest relevant status.
- Kept checksum repair automatic without showing an unnecessary option.

## 0.5.6 - 2026-06-11

- Made checkboxes the direct source of the active patch selection.
- Added conflict prompts when cheats write different values to the same ROM
  location.
- Renamed the final action to **Apply cheats and save ROM...**.

## 0.5.5 - 2026-06-11

- Fixed blank space above the cheat list after changing ROMs.
- Cleared previous-game information from the Live Log.
- Improved checkbox size, window scaling, and bottom status-bar visibility.

## 0.5.4 - 2026-06-11

- Placed the cheat browser and Live Log side by side in a resizable layout.
- Improved windowed, maximized, and HiDPI behavior.

## 0.5.3 - 2026-06-11

- Replaced simulated list indicators with real Tkinter checkboxes.
- Added reliable mouse-wheel scrolling and scroll-to-top behavior.

## 0.5.2 - 2026-06-11

- Added the selected-cheat counter.
- Added selected cheat numbers to suggested output filenames.
- Increased the visible cheat-list area.

## 0.5.1 - 2026-06-11

- Rebuilt and revalidated the integrated catalog against exact ROM revisions.
- Removed malformed, unsafe, duplicate, and mapper-incompatible entries.
- Improved cheat numbering and catalog presentation.

## 0.5.0 - 2026-06-10

- Renamed the project to **SNES Cheat Patcher Studio**.
- Changed the application to create patched ROM copies directly.
- Focused the interface on the integrated cheat catalog.

## Earlier development - 0.3.1 to 0.4.0

- Added exact ROM identification and the first integrated catalog.
- Added LoROM, HiROM, ExLoROM, ExHiROM, and supported coprocessor mappings.
- Added conservative rejection of unsafe or dynamic address ranges.
- Added checksum repair, copier-header support, atomic writes, and extensive
  synthetic regression tests.
