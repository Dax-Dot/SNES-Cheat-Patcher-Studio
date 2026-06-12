"""Integrated catalog tests without commercial ROMs."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))  # allow importing test_core helpers

from snes_cheat_patcher import catalog
from snes_cheat_patcher.catalog import CatalogCheat, identify_rom, plans_for_cheat
from snes_cheat_patcher.core import (
    PatchError, Region, classify_region, decode_snes_game_genie, inspect_rom,
)
from test_core import build_rom


def write_tmp(data: bytes) -> Path:
    file = tempfile.NamedTemporaryFile(delete=False, suffix=".sfc")
    file.write(data)
    file.close()
    return Path(file.name)


class CatalogTests(unittest.TestCase):
    def tearDown(self):
        catalog._CACHE = None

    def test_bundled_catalog_shape_and_codes(self):
        data = catalog.load_catalog()
        self.assertEqual(data["schema"], 1)
        self.assertEqual(data["version"], "0.5.1")
        self.assertEqual(data["summary"]["games"], 967)
        self.assertEqual(data["summary"]["cheats"], 11988)
        self.assertEqual(data["summary"]["codes"], 19038)
        for game in data["games"].values():
            self.assertEqual(len(game["sha256"]), 64)
            signatures = set()
            for cheat in game["cheats"]:
                self.assertTrue(cheat["codes"])
                signature = tuple(sorted(cheat["codes"]))
                self.assertNotIn(signature, signatures)
                signatures.add(signature)
                for code in cheat["codes"]:
                    self.assertRegex(code, r"^[DF4709156BC8A23E]{4}-[DF4709156BC8A23E]{4}$")
                    decoded = decode_snes_game_genie(code)
                    self.assertNotIn(
                        classify_region(decoded.bank, decoded.address),
                        (Region.WRAM, Region.LOWRAM, Region.IO, Region.SRAM),
                    )

    def test_identification_requires_crc_sha_and_size(self):
        path = write_tmp(build_rom(size=0x80000, map_mode=0x20))
        info = inspect_rom(path)
        crc = f"{info.payload_crc32:08X}"
        catalog._CACHE = {
            "schema": 1,
            "games": {
                crc: {
                    "name": "Synthetic",
                    "region": "Test",
                    "languages": "En",
                    "serial": "",
                    "size": info.payload_size,
                    "sha256": info.payload_sha256,
                    "source_url": "",
                    "cheats": [
                        {"name": "First", "codes": ["DF47-0915"]},
                        {"name": "Second", "codes": ["D047-0915"]},
                        {"name": "Third", "codes": ["D947-0915"]},
                    ],
                }
            },
        }
        match = identify_rom(info)
        self.assertTrue(match.matched)
        self.assertEqual(match.game.name, "Synthetic")
        self.assertEqual([cheat.index for cheat in match.game.cheats], [1, 2, 3])

        catalog._CACHE["games"][crc]["sha256"] = "0" * 64
        self.assertFalse(identify_rom(info).matched)

    def test_runtime_mapper_validation_rejects_ram_code(self):
        path = write_tmp(build_rom(size=0x80000, map_mode=0x20))
        info = inspect_rom(path)
        cheat = CatalogCheat(1, "RAM", ("DDDD-DDDD",))
        with self.assertRaises(PatchError):
            plans_for_cheat(info, cheat)


if __name__ == "__main__":
    unittest.main(verbosity=2)
