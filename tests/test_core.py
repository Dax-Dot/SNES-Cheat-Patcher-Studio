"""Engine tests with synthetic ROMs (no commercial ROMs required)."""
from __future__ import annotations

import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from snes_cheat_patcher import core
from snes_cheat_patcher.core import (
    Chip,
    ExtendedLayout,
    PatchError,
    Topology,
    apply_plans,
    decode_snes_game_genie,
    encode_snes_game_genie,
    inspect_rom,
    make_patch_plan,
    mirror_offset,
    translate,
)


def build_rom(
    *,
    size: int,
    map_mode: int,
    chipset: int = 0x00,
    header_base: int | None = None,
    copier: bool = False,
    title: str = "TEST ROM",
    fill: int = 0x55,
    rom_size_byte: int | None = None,
    sram_size_byte: int = 0,
) -> bytes:
    """Builds a minimal but valid SNES ROM (header with a coherent checksum)."""
    data = bytearray(bytes([fill]) * size)
    if header_base is None:
        low = map_mode & 0x0F
        header_base = 0xFFC0 if low in (1, 5, 9, 10) else 0x7FC0
    o = header_base
    data[o:o + 21] = title.ljust(21)[:21].encode("ascii")
    data[o + 0x15] = map_mode
    data[o + 0x16] = chipset
    if rom_size_byte is None:
        rom_size_byte = max(0, (size - 1).bit_length() - 10)
    data[o + 0x17] = rom_size_byte
    data[o + 0x18] = sram_size_byte
    data[o + 0x3C:o + 0x3C + 2] = (0x8000).to_bytes(2, "little")  # reset vector
    checksum = 0x4321
    data[o + 0x1C:o + 0x1E] = (checksum ^ 0xFFFF).to_bytes(2, "little")
    data[o + 0x1E:o + 0x20] = checksum.to_bytes(2, "little")
    out = bytes(data)
    if copier:
        out = b"\x00" * 512 + out
    return out


def write_tmp(data: bytes, suffix: str = ".sfc") -> Path:
    fd = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    fd.write(data)
    fd.close()
    return Path(fd.name)


def find_code(pred) -> str:
    rng = random.Random(12345)
    for _ in range(2_000_000):
        s = "".join(rng.choice(core.GG_ALPHABET) for _ in range(8))
        c = decode_snes_game_genie(s)
        if pred(c):
            return s
    raise AssertionError("no code matching the predicate was found")


class DecodeTests(unittest.TestCase):
    def test_kirby_reference_code(self):
        c = decode_snes_game_genie("C285-D4C7")
        self.assertEqual(c.cpu_address, 0x08BC7A)
        self.assertEqual(c.value, 0xAD)

    def test_encode_decode_roundtrip(self):
        rng = random.Random(1)
        for _ in range(5000):
            addr = rng.randint(0, 0xFFFFFF)
            val = rng.randint(0, 0xFF)
            code = encode_snes_game_genie(addr, val)
            back = decode_snes_game_genie(code)
            self.assertEqual(back.cpu_address, addr)
            self.assertEqual(back.value, val)

    def test_invalid_codes(self):
        with self.assertRaises(PatchError):
            decode_snes_game_genie("SHORT")
        with self.assertRaises(PatchError):
            decode_snes_game_genie("ZZZZZZZZ")  # Z is not in the alphabet


class DetectionTests(unittest.TestCase):
    def test_lorom(self):
        info = inspect_rom(write_tmp(build_rom(size=0x80000, map_mode=0x20)))
        self.assertEqual(info.topology, Topology.LOROM)
        self.assertEqual(info.chip, Chip.NONE)

    def test_hirom(self):
        info = inspect_rom(write_tmp(build_rom(size=0x100000, map_mode=0x21)))
        self.assertEqual(info.topology, Topology.HIROM)

    def test_exhirom(self):
        rom = build_rom(size=0x500000, map_mode=0x25, header_base=0x40FFC0)
        info = inspect_rom(write_tmp(rom))
        self.assertEqual(info.topology, Topology.EXHIROM)
        self.assertEqual(info.extended_layout, ExtendedLayout.BIG_FIRST)

    def test_standard_header_large_rom_is_small_first_extended(self):
        info = inspect_rom(write_tmp(build_rom(
            size=0x500000, map_mode=0x21, header_base=0xFFC0
        )))
        self.assertEqual(info.topology, Topology.EXHIROM)
        self.assertEqual(info.extended_layout, ExtendedLayout.SMALL_FIRST)

    def test_special_chips_over_four_mib_are_not_generic_extended(self):
        info = inspect_rom(write_tmp(build_rom(
            size=0x800000, map_mode=0x3A, chipset=0xF5,
            header_base=0xFFC0, rom_size_byte=13,
        )))
        self.assertEqual(info.topology, Topology.HIROM)
        self.assertEqual(info.extended_layout, ExtendedLayout.NORMAL)

    def test_invalid_reset_vector_is_rejected(self):
        rom = bytearray(build_rom(size=0x80000, map_mode=0x20))
        rom[0x7FFC:0x7FFE] = b"\x00\x00"
        with self.assertRaises(PatchError):
            inspect_rom(write_tmp(bytes(rom)))

    def test_unaligned_image_is_rejected(self):
        with self.assertRaises(PatchError):
            inspect_rom(write_tmp(build_rom(size=0x80000, map_mode=0x20) + b"x"))

    def test_exlorom_under_six_mib_is_rejected(self):
        with self.assertRaises(PatchError):
            inspect_rom(write_tmp(build_rom(
                size=0x500000, map_mode=0x22, header_base=0x407FC0
            )))

    def test_sufami_container_is_rejected(self):
        rom = bytearray(build_rom(size=0x80000, map_mode=0x20))
        rom[:14] = b"BANDAI SFC-ADX"
        with self.assertRaises(PatchError):
            inspect_rom(write_tmp(bytes(rom)))

    def test_known_special_title_is_rejected_during_inspection(self):
        with self.assertRaises(PatchError):
            inspect_rom(write_tmp(build_rom(
                size=0x80000, map_mode=0x20, title="WANDERERS FROM YS"
            )))

    def test_sa1_chip(self):
        info = inspect_rom(write_tmp(build_rom(size=0x100000, map_mode=0x23, chipset=0x34)))
        self.assertEqual(info.chip, Chip.SA1)

    def test_superfx_chip_is_lorom(self):
        info = inspect_rom(write_tmp(build_rom(size=0x100000, map_mode=0x20, chipset=0x13)))
        self.assertEqual(info.topology, Topology.LOROM)
        self.assertEqual(info.chip, Chip.SUPERFX)

    def test_special_chip_identifiers(self):
        sdd1 = inspect_rom(write_tmp(build_rom(size=0x400000, map_mode=0x32, chipset=0x43)))
        self.assertEqual(sdd1.chip, Chip.SDD1)
        spc = inspect_rom(write_tmp(build_rom(
            size=0x800000, map_mode=0x3A, chipset=0xF5, header_base=0xFFC0
        )))
        self.assertEqual(spc.chip, Chip.SPC7110)
        c4 = inspect_rom(write_tmp(build_rom(size=0x200000, map_mode=0x20, chipset=0xF3)))
        self.assertEqual(c4.chip, Chip.CX4)

    def test_copier_header(self):
        info = inspect_rom(write_tmp(build_rom(size=0x80000, map_mode=0x20, copier=True)))
        self.assertEqual(info.copier_header, 512)
        self.assertEqual(info.topology, Topology.LOROM)


class RegionRejectionTests(unittest.TestCase):
    def setUp(self):
        self.info = inspect_rom(write_tmp(build_rom(size=0x80000, map_mode=0x20)))

    def test_wram_rejected(self):
        with self.assertRaises(PatchError):
            translate(0x7E0000, self.info)
        with self.assertRaises(PatchError):
            translate(0x7F1234, self.info)

    def test_lowram_mirror_rejected(self):
        with self.assertRaises(PatchError):
            translate(0x001234, self.info)   # banco 00, $1234 = RAM baja
        with self.assertRaises(PatchError):
            translate(0x801FFF, self.info)

    def test_io_registers_rejected(self):
        with self.assertRaises(PatchError):
            translate(0x002100, self.info)   # APU/PPU regs
        with self.assertRaises(PatchError):
            translate(0x004016, self.info)


class MappingTests(unittest.TestCase):
    def test_sampled_static_mappers_never_return_out_of_file_offsets(self):
        infos = [
            inspect_rom(write_tmp(build_rom(size=0x180000, map_mode=0x20))),
            inspect_rom(write_tmp(build_rom(size=0x300000, map_mode=0x21))),
            inspect_rom(write_tmp(build_rom(
                size=0x700000, map_mode=0x22, header_base=0x407FC0
            ))),
            inspect_rom(write_tmp(build_rom(
                size=0x500000, map_mode=0x25, header_base=0x40FFC0
            ))),
            inspect_rom(write_tmp(build_rom(
                size=0x400000, map_mode=0x23, chipset=0x34
            ))),
            inspect_rom(write_tmp(build_rom(
                size=0x200000, map_mode=0x20, chipset=0x13
            ))),
            inspect_rom(write_tmp(build_rom(
                size=0x400000, map_mode=0x32, chipset=0x43
            ))),
            inspect_rom(write_tmp(build_rom(
                size=0x800000, map_mode=0x3A, chipset=0xF5,
                header_base=0xFFC0, rom_size_byte=13,
            ))),
        ]
        for info in infos:
            for bank in range(256):
                for addr in (0x6000, 0x7FFF, 0x8000, 0xFFFF):
                    address = (bank << 16) | addr
                    try:
                        candidates = translate(address, info)
                    except PatchError:
                        continue
                    for candidate in candidates:
                        self.assertGreaterEqual(candidate.file_offset, 0)
                        self.assertLess(candidate.file_offset, info.size)

    def test_lorom_basic(self):
        info = inspect_rom(write_tmp(build_rom(size=0x80000, map_mode=0x20)))
        c = translate(0x018000, info)
        self.assertEqual(c[0].file_offset, 0x8000)
        # banco $81 es espejo de $01
        self.assertEqual(translate(0x818000, info)[0].file_offset, 0x8000)

    def test_hirom_basic(self):
        info = inspect_rom(write_tmp(build_rom(size=0x100000, map_mode=0x21)))
        self.assertEqual(translate(0xC00000, info)[0].file_offset, 0x0)
        self.assertEqual(translate(0x008000, info)[0].file_offset, 0x8000)

    def test_sa1_kirby_offset(self):
        info = inspect_rom(write_tmp(build_rom(size=0x100000, map_mode=0x23, chipset=0x34)))
        c = translate(0x08BC7A, info)
        self.assertEqual(c[0].file_offset, 0x043C7A)

    def test_sa1_upper_banks_are_initial_mirror(self):
        info = inspect_rom(write_tmp(build_rom(size=0x400000, map_mode=0x23, chipset=0x34)))
        # The initial map installs the same LoROM window in $00-$3F and $80-$BF.
        self.assertEqual(translate(0x808000, info)[0].file_offset, 0x000000)
        self.assertEqual(translate(0x008000, info)[0].file_offset, 0x000000)

    def test_sa1_dynamic_window_rejected(self):
        info = inspect_rom(write_tmp(build_rom(size=0x400000, map_mode=0x23, chipset=0x34)))
        with self.assertRaises(PatchError):
            translate(0xC00000, info)

    def test_non_power_of_two_mirroring(self):
        info = inspect_rom(write_tmp(build_rom(size=0x180000, map_mode=0x20)))  # 1.5 MB
        # $308000 -> logical 0x180000 -> correct mirror to 0x100000.
        c = translate(0x308000, info)
        self.assertEqual(c[0].file_offset, 0x100000)

    def test_mirror_offset_helper(self):
        self.assertEqual(mirror_offset(0x0C0000, 0x0C0000), 0x080000)
        self.assertEqual(mirror_offset(0x180000, 0x180000), 0x100000)
        self.assertEqual(mirror_offset(0x300000, 0x300000), 0x200000)
        self.assertEqual(mirror_offset(0x600000, 0x600000), 0x400000)
        self.assertEqual(mirror_offset(0x100000, 0x100000), 0x0)  # pow2
        self.assertEqual(mirror_offset(0x1234, 0x80000), 0x1234)

    def test_mirror_matches_reference_algorithm(self):
        def reference(size: int, position: int) -> int:
            if position < size:
                return position
            mask = 1 << (position.bit_length() - 1)
            if size <= (position & mask):
                return reference(size, position - mask)
            return mask + reference(size - mask, position - mask)

        for size in (0x0C0000, 0x140000, 0x180000, 0x280000, 0x300000, 0x500000, 0x600000):
            for position in range(0, 0x800000, 0x1000):
                self.assertEqual(mirror_offset(position, size), reference(size, position))

    def test_extended_maps(self):
        exhi = inspect_rom(write_tmp(build_rom(
            size=0x500000, map_mode=0x25, header_base=0x40FFC0
        )))
        self.assertEqual(translate(0x008000, exhi)[0].file_offset, 0x408000)
        self.assertEqual(translate(0x808000, exhi)[0].file_offset, 0x008000)
        self.assertEqual(translate(0x400000, exhi)[0].file_offset, 0x400000)
        self.assertEqual(translate(0xC00000, exhi)[0].file_offset, 0x000000)

        exlo = inspect_rom(write_tmp(build_rom(
            size=0x700000, map_mode=0x22, header_base=0x407FC0
        )))
        self.assertEqual(exlo.topology, Topology.EXLOROM)
        self.assertEqual(translate(0x008000, exlo)[0].file_offset, 0x400000)
        self.assertEqual(translate(0x808000, exlo)[0].file_offset, 0x000000)
        self.assertEqual(translate(0x400000, exlo)[0].file_offset, 0x600000)
        self.assertEqual(translate(0xC00000, exlo)[0].file_offset, 0x200000)

    def test_small_first_exhirom_maps_canonical_offsets_to_physical_file(self):
        info = inspect_rom(write_tmp(build_rom(
            size=0x500000, map_mode=0x21, header_base=0xFFC0
        )))
        # In small-first layout, the 1 MiB tail appears at the start of the file.
        self.assertEqual(translate(0xC00000, info)[0].file_offset, 0x100000)
        self.assertEqual(translate(0x008000, info)[0].file_offset, 0x008000)

    def test_small_first_exlorom_maps_canonical_offsets_to_physical_file(self):
        info = inspect_rom(write_tmp(build_rom(
            size=0x700000, map_mode=0x20, header_base=0x7FC0
        )))
        self.assertEqual(info.topology, Topology.EXLOROM)
        self.assertEqual(info.extended_layout, ExtendedLayout.SMALL_FIRST)
        self.assertEqual(translate(0x808000, info)[0].file_offset, 0x300000)
        self.assertEqual(translate(0x008000, info)[0].file_offset, 0x000000)

    def test_superfx_windows(self):
        info = inspect_rom(write_tmp(build_rom(
            size=0x100000, map_mode=0x20, chipset=0x13
        )))
        self.assertEqual(translate(0x410000, info)[0].file_offset, 0x010000)
        self.assertEqual(translate(0xC10000, info)[0].file_offset, 0x010000)

    def test_sdd1_static_and_dynamic_windows(self):
        info = inspect_rom(write_tmp(build_rom(
            size=0x400000, map_mode=0x32, chipset=0x43
        )))
        self.assertEqual(translate(0x600000, info)[0].file_offset, 0x000000)
        self.assertEqual(translate(0x708000, info)[0].file_offset, 0x108000)
        with self.assertRaises(PatchError):
            translate(0xC00000, info)

    def test_spc7110_static_and_dynamic_windows(self):
        info = inspect_rom(write_tmp(build_rom(
            size=0x800000, map_mode=0x3A, chipset=0xF5, header_base=0xFFC0
        )))
        self.assertEqual(translate(0x400000, info)[0].file_offset, 0x600000)
        self.assertEqual(translate(0xC10000, info)[0].file_offset, 0x010000)
        with self.assertRaises(PatchError):
            translate(0xD00000, info)

    def test_spc7110_extended_window_requires_header_size_flag(self):
        info = inspect_rom(write_tmp(build_rom(
            size=0x800000, map_mode=0x3A, chipset=0xF5,
            header_base=0xFFC0, rom_size_byte=12,
        )))
        self.assertEqual(translate(0x400000, info), [])

    def test_dsp_io_window_rejected(self):
        info = inspect_rom(write_tmp(build_rom(
            size=0x100000, map_mode=0x20, chipset=0x03
        )))
        with self.assertRaises(PatchError):
            translate(0x208000, info)

    def test_lorom_sram_window_rejected(self):
        info = inspect_rom(write_tmp(build_rom(
            size=0x80000, map_mode=0x20, sram_size_byte=5
        )))
        with self.assertRaises(PatchError):
            translate(0x708000, info)


class PatchPipelineTests(unittest.TestCase):
    def setUp(self):
        self.rom_path = write_tmp(build_rom(size=0x80000, map_mode=0x20, fill=0x55))
        # Code that lands in bank $00, $8000-$80FF.
        self.code = find_code(lambda c: (c.cpu_address >> 16) == 0 and 0x8000 <= (c.cpu_address & 0xFFFF) < 0x8100)

    def test_make_plan_and_apply(self):
        plan = make_patch_plan(self.rom_path, self.code)
        self.assertEqual(plan.old_value, 0x55)
        out = write_tmp(b"", suffix=".sfc")
        out.unlink()
        apply_plans([plan], out)
        data = out.read_bytes()
        self.assertEqual(data[plan.file_offset], plan.new_value)
        # original intacto
        self.assertEqual(self.rom_path.read_bytes()[plan.file_offset], 0x55)

    def test_expected_byte_mismatch(self):
        with self.assertRaises(PatchError):
            make_patch_plan(self.rom_path, self.code, expected=0x99)

    def test_expected_byte_match(self):
        make_patch_plan(self.rom_path, self.code, expected=0x55)  # no lanza

    def test_output_cannot_be_source(self):
        plan = make_patch_plan(self.rom_path, self.code)
        with self.assertRaises(PatchError):
            apply_plans([plan], self.rom_path, overwrite=True)

    def test_conflict_detection(self):
        plan = make_patch_plan(self.rom_path, self.code)
        other = core.PatchPlan(plan.code, plan.rom, plan.file_offset, plan.old_value,
                               (plan.new_value + 1) & 0xFF, "x")
        with self.assertRaises(PatchError):
            apply_plans([plan, other], write_tmp(b"", suffix=".sfc"), overwrite=True)

    def test_source_change_after_analysis_is_rejected(self):
        plan = make_patch_plan(self.rom_path, self.code)
        data = bytearray(self.rom_path.read_bytes())
        data[0x200] ^= 0xFF
        self.rom_path.write_bytes(data)
        out = write_tmp(b"", suffix=".sfc")
        out.unlink()
        with self.assertRaises(PatchError):
            apply_plans([plan], out, overwrite=True)


class ChecksumTests(unittest.TestCase):
    def test_power_of_two(self):
        payload = bytes([0x01]) * 0x10000
        self.assertEqual(core.compute_internal_checksum(payload), (0x10000) & 0xFFFF)

    def test_non_power_of_two_runs(self):
        payload = bytes([0x02]) * 0x180000
        # We only verify that the value is stable and 16-bit, not exact hardware output.
        v = core.compute_internal_checksum(payload)
        self.assertTrue(0 <= v <= 0xFFFF)

    def test_matches_reference_recursive_algorithm(self):
        def reference(data: bytes, start: int, length: int, mask: int = 0x800000) -> int:
            while mask and not (length & mask):
                mask >>= 1
            part1 = sum(data[start:start + mask])
            next_length = length - mask
            if not next_length:
                return part1
            part2 = reference(data, start + mask, next_length, mask >> 1)
            while next_length < mask:
                next_length *= 2
                part2 *= 2
            return part1 + part2

        rng = random.Random(99)
        for size in (0x8000, 0x18000, 0x28000, 0x50000, 0x78000):
            payload = rng.randbytes(size)
            self.assertEqual(
                core.compute_internal_checksum(payload),
                reference(payload, 0, len(payload)) & 0xFFFF,
            )

    def test_checksum_recalculation_is_stable_despite_old_header_values(self):
        path = write_tmp(build_rom(size=0x80000, map_mode=0x20, fill=0x11))
        info = inspect_rom(path)
        first = bytearray(path.read_bytes())
        core._apply_checksum_to_buffer(first, info)
        first_checksum = first[info.header_offset + 0x1C:info.header_offset + 0x20]
        first[info.header_offset + 0x1C:info.header_offset + 0x20] = b"\x12\x34\x56\x78"
        core._apply_checksum_to_buffer(first, info)
        self.assertEqual(
            first[info.header_offset + 0x1C:info.header_offset + 0x20],
            first_checksum,
        )

    def test_small_first_checksum_uses_canonical_order(self):
        raw = bytearray(build_rom(
            size=0x500000, map_mode=0x21, header_base=0xFFC0, fill=0
        ))
        raw[0x100000:0x500000] = b"\x01" * 0x400000
        path = write_tmp(bytes(raw))
        info = inspect_rom(path)
        buf = bytearray(path.read_bytes())
        core._apply_checksum_to_buffer(buf, info)

        normalized = bytearray(raw)
        normalized[0xFFDC:0xFFE0] = b"\xFF\xFF\x00\x00"
        canonical = bytes(normalized[0x100000:] + normalized[:0x100000])
        expected = core.compute_internal_checksum(canonical)
        self.assertEqual(
            int.from_bytes(buf[0xFFDE:0xFFE0], "little"),
            expected,
        )

    def test_spc7110_three_megabyte_rule(self):
        payload = b"\x01" + bytes(0x300000 - 1)
        self.assertEqual(core.compute_internal_checksum(payload, chip=Chip.SPC7110), 2)


class OptionalRealRomTest(unittest.TestCase):
    ROM = Path(__file__).parents[1] / "testdata" / "Kirby's Dream Land 3 (USA).sfc"

    @unittest.skipUnless(ROM.exists(), "real ROM not included")
    def test_kirby_real(self):
        info = inspect_rom(self.ROM)
        self.assertEqual(info.chip, Chip.SA1)
        plan = make_patch_plan(self.ROM, "C285-D4C7", expected=0x8D)
        self.assertEqual(plan.file_offset, 0x043C7A)


if __name__ == "__main__":
    unittest.main(verbosity=2)
