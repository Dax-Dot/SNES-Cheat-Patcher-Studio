"""Builds the integrated catalog from the source databases.

Usage:
    python tools/build_catalog.py CHEATS.json NOINTRO.json OUTPUT.json REPORT.json ROM_ROOT

ROM_ROOT must contain exact No-Intro ROM revisions. Games without a verified
ROM are omitted so every bundled cheat has passed the real mapper.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from snes_cheat_patcher.core import (  # noqa: E402
    PatchError,
    Region,
    RomInfo,
    classify_region,
    decode_snes_game_genie,
    inspect_rom,
    translate,
)


def normalize_code(text: str) -> str:
    code = decode_snes_game_genie(text)
    return f"{code.text[:4]}-{code.text[4:]}"


def static_rejection(codes: list[str]) -> str | None:
    for text in codes:
        try:
            code = decode_snes_game_genie(text)
        except PatchError as exc:
            return f"invalid code: {exc}"
        region = classify_region(code.bank, code.address)
        if region in (Region.WRAM, Region.LOWRAM, Region.IO, Region.SRAM):
            return f"{text} points to {region.value} (${code.cpu_address:06X})"
    return None


def index_roms(root: Path) -> dict[str, list[Path]]:
    by_name: dict[str, list[Path]] = {}
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".sfc", ".smc", ".fig", ".swc"}:
            by_name.setdefault(path.stem, []).append(path)
    return by_name


def find_verified_rom(
    rom: dict, candidates: list[Path],
) -> tuple[RomInfo | None, str]:
    errors: list[str] = []
    for path in candidates:
        try:
            info = inspect_rom(path)
        except (PatchError, OSError) as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        if (
            info.payload_size == int(rom["size"])
            and f"{info.payload_crc32:08X}" == str(rom["crc32"]).upper()
            and info.payload_sha256.lower() == str(rom["sha256"]).lower()
        ):
            return info, ""
    if errors:
        return None, "; ".join(errors[:3])
    return None, "no exact CRC32 + SHA-256 + size match"


def clean_mapper_codes(
    codes: list[str], info: RomInfo,
) -> tuple[list[str], str | None, int]:
    offsets: dict[int, int] = {}
    clean: list[str] = []
    duplicates = 0
    for text in codes:
        code = decode_snes_game_genie(text)
        try:
            candidates = translate(code.cpu_address, info)
        except PatchError as exc:
            return [], f"{text}: {exc}", duplicates
        if not candidates:
            return [], (
                f"{text}: address ${code.cpu_address:06X} does not map to a "
                f"static ROM offset for {info.mapper_label}"
            ), duplicates
        offset = candidates[0].file_offset
        previous = offsets.get(offset)
        if previous is not None and previous != code.value:
            return [], (
                f"{text}: conflicting values target file offset "
                f"0x{offset:06X}"
            ), duplicates
        if previous == code.value:
            duplicates += 1
            continue
        offsets[offset] = code.value
        clean.append(text)
    return clean, None, duplicates


def build(
    cheats_path: Path, nointro_path: Path, rom_root: Path,
) -> tuple[dict, dict]:
    cheats_source = json.loads(cheats_path.read_text(encoding="utf-8"))
    nointro_source = json.loads(nointro_path.read_text(encoding="utf-8"))
    nointro = nointro_source["by_crc"]

    games: dict[str, dict] = {}
    rejected: list[dict] = []
    excluded_unlinked: list[dict] = []
    duplicate_cheats = 0
    duplicate_codes = 0
    mapper_duplicate_codes = 0
    retained_cheats = 0
    retained_codes = 0
    mapper_rejected: list[dict] = []
    unverified_games: list[dict] = []
    verified_revisions = 0
    roms_by_name = index_roms(rom_root)

    for crc, source_game in cheats_source["by_crc"].items():
        rom = nointro.get(crc)
        if rom is None:
            excluded_unlinked.append({
                "crc32": crc,
                "title": source_game.get("title", ""),
                "reason": "CRC not present in the supplied No-Intro database",
            })
            continue
        if len(str(rom.get("sha256", ""))) != 64 or not str(rom.get("size", "")).isdigit():
            excluded_unlinked.append({
                "crc32": crc,
                "title": source_game.get("title", ""),
                "reason": "No-Intro entry without verifiable SHA-256/size",
            })
            continue
        info, verification_error = find_verified_rom(
            rom, roms_by_name.get(str(rom["name"]), []),
        )
        if info is None:
            unverified_games.append({
                "crc32": crc,
                "title": rom["name"],
                "reason": verification_error,
            })
            continue
        verified_revisions += 1

        clean_cheats: list[dict] = []
        seen_sets: set[tuple[str, ...]] = set()
        for source_cheat in source_game.get("cheats", []):
            codes: list[str] = []
            seen_codes: set[str] = set()
            malformed: str | None = None
            for raw in source_cheat.get("codes", []):
                try:
                    code = normalize_code(str(raw))
                except PatchError as exc:
                    malformed = str(exc)
                    break
                if code in seen_codes:
                    duplicate_codes += 1
                    continue
                seen_codes.add(code)
                codes.append(code)

            reason = malformed or static_rejection(codes)
            if not codes:
                reason = reason or "cheat without codes"
            if reason:
                rejected.append({
                    "crc32": crc,
                    "title": rom["name"],
                    "cheat": source_cheat.get("name", ""),
                    "reason": reason,
                })
                continue
            mapper_input_codes = codes
            codes, reason, mapper_duplicates = clean_mapper_codes(codes, info)
            mapper_duplicate_codes += mapper_duplicates
            if reason:
                mapper_rejected.append({
                    "crc32": crc,
                    "title": rom["name"],
                    "cheat": source_cheat.get("name", ""),
                    "codes": mapper_input_codes,
                    "reason": reason,
                })
                continue

            signature = tuple(sorted(codes))
            if signature in seen_sets:
                duplicate_cheats += 1
                continue
            seen_sets.add(signature)
            clean_cheats.append({
                "name": str(source_cheat.get("name", "")).strip() or "Unnamed cheat",
                "codes": codes,
            })
            retained_cheats += 1
            retained_codes += len(codes)

        if clean_cheats:
            games[crc] = {
                "name": rom["name"],
                "region": rom.get("region", ""),
                "languages": rom.get("languages", ""),
                "serial": rom.get("serial", ""),
                "size": int(rom["size"]),
                "sha256": rom["sha256"].lower(),
                "source_url": source_game.get("source_url", ""),
                "cheats": clean_cheats,
            }

    catalog = {
        "schema": 1,
        "version": "0.5.1",
        "sources": {
            "cheats": cheats_source.get("source", ""),
            "cheats_generated": cheats_source.get("generated", ""),
            "nointro": nointro_source.get("source_xml", ""),
            "nointro_generated": nointro_source.get("generated", ""),
        },
        "summary": {
            "games": len(games),
            "cheats": retained_cheats,
            "codes": retained_codes,
        },
        "games": games,
    }
    report = {
        "catalog_summary": catalog["summary"],
        "verified_rom_revisions": verified_revisions,
        "source_summary": cheats_source.get("summary", {}),
        "excluded_unlinked_games": excluded_unlinked,
        "excluded_no_crc_games": [
            {"title": game.get("title", ""), "reason": "no verifiable CRC32"}
            for game in cheats_source.get("no_crc", [])
        ],
        "rejected_cheats": rejected,
        "mapper_rejected_cheats": mapper_rejected,
        "unverified_games_omitted": unverified_games,
        "duplicates_removed": {
            "cheats": duplicate_cheats,
            "codes_within_cheat": duplicate_codes,
            "mapper_equivalent_codes": mapper_duplicate_codes,
        },
        "validation_note": (
            "Every bundled game revision was matched against an exact ROM by CRC32, "
            "SHA-256 and size. Every retained cheat was then validated against that "
            "ROM's detected mapper. Runtime validation remains enabled as a safety "
            "check. Game Genie codes do not include an expected original byte, so "
            "functional behavior still requires emulator or hardware testing."
        ),
    }
    return catalog, report


def main() -> None:
    if len(sys.argv) != 6:
        raise SystemExit(__doc__)
    catalog, report = build(
        Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[5]),
    )
    Path(sys.argv[3]).write_text(
        json.dumps(catalog, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    Path(sys.argv[4]).write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
