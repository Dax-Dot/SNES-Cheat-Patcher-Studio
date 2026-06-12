"""Loading and identification for the built-in No-Intro/Game Genie catalog."""
from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import json

from .core import PatchError, PatchPlan, RomInfo, make_patch_plan


@dataclass(frozen=True)
class CatalogCheat:
    index: int
    name: str
    codes: tuple[str, ...]


@dataclass(frozen=True)
class CatalogGame:
    crc32: str
    name: str
    region: str
    languages: str
    serial: str
    size: int
    sha256: str
    source_url: str
    cheats: tuple[CatalogCheat, ...]


@dataclass(frozen=True)
class CatalogMatch:
    game: CatalogGame | None
    reason: str

    @property
    def matched(self) -> bool:
        return self.game is not None


_CACHE: dict | None = None


def load_catalog() -> dict:
    global _CACHE
    if _CACHE is None:
        resource = files("snes_cheat_patcher").joinpath("data/cheat_catalog.json")
        _CACHE = json.loads(resource.read_text(encoding="utf-8"))
        if _CACHE.get("schema") != 1:
            raise PatchError("The built-in database uses an incompatible schema.")
    return _CACHE


def identify_rom(info: RomInfo) -> CatalogMatch:
    crc = f"{info.payload_crc32:08X}"
    raw = load_catalog()["games"].get(crc)
    if raw is None:
        return CatalogMatch(None, f"CRC32 {crc} is not in the built-in catalog.")
    if raw["sha256"].lower() != info.payload_sha256.lower():
        return CatalogMatch(
            None,
            f"CRC32 {crc} matches, but SHA-256 does not; rejected for safety.",
        )
    if int(raw["size"]) != info.payload_size:
        return CatalogMatch(None, "The size does not match the No-Intro revision.")
    cheats = tuple(
        CatalogCheat(i + 1, cheat["name"], tuple(cheat["codes"]))
        for i, cheat in enumerate(raw["cheats"])
    )
    return CatalogMatch(CatalogGame(
        crc32=crc,
        name=raw["name"],
        region=raw.get("region", ""),
        languages=raw.get("languages", ""),
        serial=raw.get("serial", ""),
        size=int(raw["size"]),
        sha256=raw["sha256"],
        source_url=raw.get("source_url", ""),
        cheats=cheats,
    ), "Exact match on CRC32 + SHA-256 + size.")


def plans_for_cheat(info: RomInfo, cheat: CatalogCheat) -> list[PatchPlan]:
    plans: list[PatchPlan] = []
    offsets: dict[int, int] = {}
    for code in cheat.codes:
        plan = make_patch_plan(info.path, code, rom_info=info)
        previous = offsets.get(plan.file_offset)
        if previous is not None and previous != plan.new_value:
            raise PatchError(
                f"{cheat.name}: two codes write different values at "
                f"0x{plan.file_offset:06X}."
            )
        if previous is None:
            offsets[plan.file_offset] = plan.new_value
            plans.append(plan)
    return plans


def validate_game_cheats(
    info: RomInfo, game: CatalogGame,
) -> tuple[list[tuple[CatalogCheat, list[PatchPlan]]], list[tuple[CatalogCheat, str]]]:
    valid: list[tuple[CatalogCheat, list[PatchPlan]]] = []
    rejected: list[tuple[CatalogCheat, str]] = []
    for cheat in game.cheats:
        try:
            valid.append((cheat, plans_for_cheat(info, cheat)))
        except (PatchError, OSError) as exc:
            rejected.append((cheat, str(exc)))
    return valid, rejected
