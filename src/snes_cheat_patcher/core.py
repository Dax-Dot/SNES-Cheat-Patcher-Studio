"""Engine that turns SNES Game Genie codes into static ROM patches.

Design principles:

* The CPU -> physical offset translation is decided by the cartridge **topology**
  (LoROM / HiROM / ExLoROM / ExHiROM), not by the commercial chip name.
* The **coprocessor chip** (SA-1, Super FX, S-DD1, SPC7110, ...) is detected from
  the header chipset byte and only affects labelling and special cases
  (e.g. the SA-1 Super MMC mapping).
* Addresses pointing at work RAM, low RAM or hardware registers are **rejected**:
  they cannot be turned into a static patch.
* Correct *mirroring* is supported even for non-power-of-two ROMs.
* The original ROM is never overwritten (atomic write via a temporary file).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import hashlib
import os
import shutil
import tempfile
import zlib


class PatchError(Exception):
    """Raised when a code or ROM cannot be patched safely."""


# --------------------------------------------------------------------------- #
#  Data models
# --------------------------------------------------------------------------- #
class Topology(str, Enum):
    LOROM = "LoROM"
    HIROM = "HiROM"
    EXLOROM = "ExLoROM"
    EXHIROM = "ExHiROM"
    UNKNOWN = "Unknown"


class ExtendedLayout(str, Enum):
    NORMAL = "Normal"
    BIG_FIRST = "Big-first"
    SMALL_FIRST = "Small-first"


class Chip(str, Enum):
    NONE = ""
    DSP = "DSP"
    SUPERFX = "Super FX"
    OBC1 = "OBC1"
    SA1 = "SA-1"
    SDD1 = "S-DD1"
    SRTC = "S-RTC"
    SPC7110 = "SPC7110"
    CX4 = "C4"
    OTHER = "Other coprocessor"
    CUSTOM = "Custom"


class Region(str, Enum):
    ROM = "ROM"
    WRAM = "WRAM"
    LOWRAM = "Low RAM (mirror)"
    IO = "Hardware registers"
    SRAM = "SRAM"
    UNKNOWN = "Unknown"


@dataclass(frozen=True)
class GameGenieCode:
    text: str
    cpu_address: int
    value: int

    @property
    def bank(self) -> int:
        return (self.cpu_address >> 16) & 0xFF

    @property
    def address(self) -> int:
        return self.cpu_address & 0xFFFF


@dataclass(frozen=True)
class RomInfo:
    path: Path
    size: int
    copier_header: int
    header_offset: int          # offset (incl. copier) of the internal header
    topology: Topology
    chip: Chip
    map_mode: int
    chipset_byte: int
    rom_size_byte: int
    sram_size_byte: int
    title: str
    checksum: int
    checksum_complement: int
    confidence: int
    extended_layout: ExtendedLayout
    sha256: str
    crc32: int
    payload_sha256: str
    payload_crc32: int

    @property
    def payload_size(self) -> int:
        return self.size - self.copier_header

    @property
    def mapper_label(self) -> str:
        if self.chip not in (Chip.NONE,):
            return f"{self.topology.value} · {self.chip.value}"
        return self.topology.value


@dataclass(frozen=True)
class Candidate:
    file_offset: int
    note: str
    primary: bool = True


@dataclass(frozen=True)
class PatchPlan:
    code: GameGenieCode
    rom: RomInfo
    file_offset: int
    old_value: int
    new_value: int
    mapping_note: str

    @property
    def is_noop(self) -> bool:
        return self.old_value == self.new_value


# --------------------------------------------------------------------------- #
#  Game Genie decoding
# --------------------------------------------------------------------------- #
GG_ALPHABET = "DF4709156BC8A23E"


def normalize_code(text: str) -> str:
    code = "".join(ch for ch in text.upper() if ch not in "- \t\r\n")
    if len(code) != 8:
        raise PatchError(
            f"A SNES Game Genie code must be 8 characters; got {len(code)}."
        )
    invalid = sorted({ch for ch in code if ch not in GG_ALPHABET})
    if invalid:
        raise PatchError(f"Invalid characters in the code: {', '.join(invalid)}")
    return code


def decode_snes_game_genie(text: str) -> GameGenieCode:
    code = normalize_code(text)
    n = [GG_ALPHABET.index(ch) for ch in code]
    address = (
        ((n[4] & 0x3) << 22)
        | ((n[5] & 0xC) << 18)
        | ((n[6] & 0x3) << 18)
        | ((n[7] & 0xC) << 14)
        | (n[2] << 12)
        | ((n[7] & 0x3) << 10)
        | ((n[4] & 0xC) << 6)
        | (n[3] << 4)
        | ((n[5] & 0x3) << 2)
        | ((n[6] & 0xC) >> 2)
    )
    return GameGenieCode(code, address, (n[0] << 4) | n[1])


def encode_snes_game_genie(cpu_address: int, value: int) -> str:
    """Inverse of :func:`decode_snes_game_genie` (useful for tests and the UI)."""
    if not 0 <= cpu_address <= 0xFFFFFF:
        raise PatchError("The CPU address must be between $000000 and $FFFFFF.")
    if not 0 <= value <= 0xFF:
        raise PatchError("The value must be between $00 and $FF.")
    a = cpu_address
    n = [0] * 8
    n[0] = (value >> 4) & 0xF
    n[1] = value & 0xF
    n[2] = (a >> 12) & 0xF
    n[3] = (a >> 4) & 0xF
    n[4] = ((a >> 22) & 0x3) | (((a >> 6) & 0xC))
    n[5] = (((a >> 18) & 0xC)) | ((a >> 2) & 0x3)
    n[6] = (((a >> 18) & 0x3)) | (((a << 2) & 0xC))
    n[7] = (((a >> 14) & 0xC)) | ((a >> 10) & 0x3)
    return "".join(GG_ALPHABET[v & 0xF] for v in n)


# --------------------------------------------------------------------------- #
#  ROM inspection
# --------------------------------------------------------------------------- #
def _ascii_title(data: bytes) -> tuple[str, int]:
    score = 0
    chars: list[str] = []
    for byte in data[:21]:
        if byte == 0:
            chars.append(" ")
        elif 32 <= byte <= 126:
            chars.append(chr(byte))
            score += 1
        else:
            chars.append("?")
            score -= 2
    return "".join(chars).strip(), score


def _header_score(data: bytes, offset: int) -> tuple[int, dict]:
    if offset < 0 or offset + 0x20 > len(data):
        return -999, {}
    h = data[offset:offset + 0x20]
    title, title_score = _ascii_title(h[:21])
    map_mode = h[0x15]
    chipset = h[0x16]
    rom_size = h[0x17]
    sram_size = h[0x18]
    complement = int.from_bytes(h[0x1C:0x1E], "little")
    checksum = int.from_bytes(h[0x1E:0x20], "little")
    score = title_score
    if (checksum ^ complement) == 0xFFFF and checksum != 0:
        score += 24
    if map_mode & 0x0F in {0, 1, 2, 3, 5, 9, 10}:
        score += 6
    if map_mode & 0xE0 == 0x20:  # valid map modes live in 0x20-0x3F
        score += 4
    reset_vector_pos = offset + 0x3C
    reset = 0
    if reset_vector_pos + 2 <= len(data):
        reset = int.from_bytes(data[reset_vector_pos:reset_vector_pos + 2], "little")
        if reset >= 0x8000:
            score += 8
    return score, {
        "title": title,
        "map_mode": map_mode,
        "chipset": chipset,
        "rom_size": rom_size,
        "sram_size": sram_size,
        "checksum": checksum,
        "complement": complement,
        "reset": reset,
    }


def _topology_from_location(header_unheadered: int) -> Topology:
    base = header_unheadered & 0xFFFFFF
    return Topology.HIROM if (base & 0xFFFF) >= 0xC000 else Topology.LOROM


def _chip_from_chipset(chipset: int, map_mode: int, topology: Topology) -> Chip:
    identifier = (chipset << 8) | map_mode
    if identifier == 0x5535:
        return Chip.SRTC
    if identifier in (0xF53A, 0xF93A):
        return Chip.SPC7110
    if identifier == 0x2530:
        return Chip.OBC1
    if identifier in (0x3423, 0x3523):
        return Chip.SA1
    if identifier in {
        0x1320, 0x1420, 0x1520, 0x1A20,
        0x1330, 0x1430, 0x1530, 0x1A30,
    }:
        return Chip.SUPERFX
    if identifier in (0x4332, 0x4532):
        return Chip.SDD1
    if identifier == 0xF320:
        return Chip.CX4
    if chipset in (0x03, 0x05):
        return Chip.DSP
    if (chipset & 0x0F) >= 0x03:
        return Chip.OTHER
    return Chip.NONE


def _looks_like_bsx_header(data: bytes, offset: int) -> bool:
    if offset < 0 or offset + 0x20 > len(data):
        return False
    h = data[offset:offset + 0x20]
    normal_bank = h[0x18] in (0x20, 0x21, 0x30, 0x31)
    if h[0x1A] not in (0x33, 0xFF) or not normal_bank:
        return False
    if h[0x15] and (h[0x15] & 0x83) != 0x80:
        return False
    month, day = h[0x16], h[0x17]
    return (
        (month == 0 and day == 0)
        or (month == 0xFF and day == 0xFF)
        or ((month & 0x0F) == 0 and 1 <= (month >> 4) <= 12)
    )


def _reject_special_container(data: bytes, copier: int) -> None:
    payload = data[copier:]
    if payload.startswith(b"BANDAI SFC-ADX"):
        raise PatchError(
            "Detected a Sufami Turbo/ADX image. Its multicart map cannot be "
            "safely converted to a static patch."
        )
    if payload[0x7FC0:0x7FC0 + 21].startswith(b"Satellaview BS-X"):
        raise PatchError("Detected the Satellaview BS-X BIOS, not a conventional ROM.")
    for base in (0x7FC0, 0xFFC0):
        if _looks_like_bsx_header(payload, base):
            raise PatchError(
                "Detected Satellaview/BS-X software with an unsupported special map."
            )
    if len(payload) > 0x7FDA and (
        payload[0x7FB2] == 0x5A
        and payload[0x7FB5] != 0x20
        and payload[0x7FDA] == 0x33
    ):
        raise PatchError("Detected a BS-X cartridge with an unsupported special map.")
    if len(payload) > 0xFFDA and (
        payload[0xFFB2] == 0x5A
        and payload[0xFFB5] != 0x20
        and payload[0xFFDA] == 0x33
    ):
        raise PatchError("Detected a BS-X cartridge with an unsupported special map.")


def _canonical_payload(payload: bytes, layout: ExtendedLayout) -> bytes:
    if layout is not ExtendedLayout.SMALL_FIRST:
        return payload
    tail = len(payload) - 0x400000
    if tail <= 0:
        return payload
    return payload[tail:] + payload[:tail]


def inspect_rom(path: str | Path) -> RomInfo:
    p = Path(path)
    if not p.is_file():
        raise PatchError(f"The ROM does not exist: {p}")
    data = p.read_bytes()
    if len(data) < 0x8000:
        raise PatchError("The file is too small to be a valid SNES ROM.")

    candidates = []
    size_remainder = len(data) % 0x8000
    if size_remainder not in (0, 512):
        raise PatchError(
            "The size is not aligned to 32 KiB SNES blocks. The image may be "
            "truncated, interleaved or contain data that is not part of the ROM."
        )
    for copier in (0, 512):
        for base in (0x7FC0, 0xFFC0, 0x407FC0, 0x40FFC0):
            score, fields = _header_score(data, base + copier)
            if fields:
                expected_copier = 512 if size_remainder == 512 else 0
                score += 12 if copier == expected_copier else -12
                candidates.append((score, copier, base + copier, fields))
    if not candidates:
        raise PatchError("Could not locate any SNES header.")
    payload_size = len(data) - (512 if size_remainder == 512 else 0)
    score, copier, header_offset, fields = max(
        candidates,
        key=lambda item: (
            item[0],
            payload_size > 0x400000 and item[2] - item[1] >= 0x400000,
        ),
    )
    if score < 10:
        raise PatchError(
            "Could not identify an SNES header with enough confidence. "
            "Is the file really an uncompressed SNES ROM?"
        )
    if int(fields["reset"]) < 0x8000:
        raise PatchError(
            "The RESET vector of the detected header does not point to ROM. The "
            "image may be interleaved, damaged or not a compatible SNES ROM."
        )
    _reject_special_container(data, copier)
    if str(fields["title"]).startswith((
        "SOUND NOVEL-TCOOL",
        "DERBY STALLION 96",
        "ADD-ON BASE CASSETE",
        "WANDERERS FROM YS",
        "THOROUGHBRED BREEDER3",
        "RPG-TCOOL 2",
    )):
        raise PatchError(
            "The detected cartridge uses a special map that does not yet have a "
            "verifiable static translation."
        )

    map_mode = int(fields["map_mode"])
    chipset = int(fields["chipset"])
    topology = _topology_from_location(header_offset - copier)
    chip = _chip_from_chipset(chipset, map_mode, topology)
    extended_layout = ExtendedLayout.NORMAL
    if payload_size > 0x400000 and chip not in {
        Chip.SUPERFX, Chip.SA1, Chip.SDD1, Chip.SPC7110,
    }:
        topology = (
            Topology.EXHIROM
            if ((header_offset - copier) & 0xFFFF) >= 0xC000
            else Topology.EXLOROM
        )
        extended_layout = (
            ExtendedLayout.BIG_FIRST
            if header_offset - copier >= 0x400000
            else ExtendedLayout.SMALL_FIRST
        )
        if topology is Topology.EXLOROM and payload_size < 0x600000:
            raise PatchError(
                "The image looks like ExLoROM but is smaller than 6 MiB. That layout "
                "does not provide a complete physical map and is rejected to avoid corruption."
            )
    digest = hashlib.sha256(data).hexdigest()
    crc = zlib.crc32(data) & 0xFFFFFFFF
    payload_data = _canonical_payload(data[copier:], extended_layout)
    return RomInfo(
        path=p,
        size=len(data),
        copier_header=copier,
        header_offset=header_offset,
        topology=topology,
        chip=chip,
        map_mode=map_mode,
        chipset_byte=chipset,
        rom_size_byte=int(fields["rom_size"]),
        sram_size_byte=int(fields["sram_size"]),
        title=str(fields["title"]),
        checksum=int(fields["checksum"]),
        checksum_complement=int(fields["complement"]),
        confidence=score,
        extended_layout=extended_layout,
        sha256=digest,
        crc32=crc,
        payload_sha256=hashlib.sha256(payload_data).hexdigest(),
        payload_crc32=zlib.crc32(payload_data) & 0xFFFFFFFF,
    )


# --------------------------------------------------------------------------- #
#  Memory region classification
# --------------------------------------------------------------------------- #
def classify_region(bank: int, addr: int) -> Region:
    if bank in (0x7E, 0x7F):
        return Region.WRAM
    if (bank <= 0x3F or 0x80 <= bank <= 0xBF):
        if addr < 0x2000:
            return Region.LOWRAM
        if 0x2000 <= addr < 0x6000:
            return Region.IO
        if 0x6000 <= addr < 0x8000:
            # Usually cartridge/SRAM space in many mappings.
            return Region.SRAM
    return Region.ROM


# --------------------------------------------------------------------------- #
#  Mirroring
# --------------------------------------------------------------------------- #
def mirror_offset(offset: int, payload: int) -> int | None:
    """Folds a physical offset into the real ROM size.

    The SNES mirrors the ROM in power-of-two blocks. For non-power-of-two ROMs,
    the final block repeats until it fills the base block.
    """
    if payload <= 0:
        return None
    if offset < 0:
        return None
    if offset < payload:
        return offset

    # Algorithm used by bsnes/Snes9x. Not equivalent to offset % power_of_two
    # when the ROM has a non-power-of-two tail.
    mask = 1 << (offset.bit_length() - 1)
    if payload <= (offset & mask):
        return mirror_offset(offset - mask, payload)
    tail = mirror_offset(offset - mask, payload - mask)
    return None if tail is None else mask + tail


# --------------------------------------------------------------------------- #
#  CPU -> physical offset translation
# --------------------------------------------------------------------------- #
def _lorom_offset(bank: int, addr: int) -> int | None:
    if addr < 0x8000 and not (0x40 <= bank <= 0x6F or 0xC0 <= bank <= 0xFF):
        return None
    return ((bank & 0x7F) << 15) | (addr & 0x7FFF)


def _hirom_offset(bank: int, addr: int) -> int | None:
    if addr < 0x8000 and not (0x40 <= bank <= 0x7D or 0xC0 <= bank <= 0xFF):
        return None
    return ((bank & 0x3F) << 16) | addr


def _window_offset(
    bank: int,
    addr: int,
    *,
    bank_start: int,
    size: int,
    base: int = 0,
    bank_size: int = 0x10000,
    addr_mask: int = 0xFFFF,
) -> int | None:
    if size <= 0:
        return None
    logical = (bank - bank_start) * bank_size + (addr & addr_mask)
    mirrored = mirror_offset(logical, size)
    return None if mirrored is None else base + mirrored


def _exlorom_offset(bank: int, addr: int, payload: int) -> int | None:
    if 0x00 <= bank <= 0x3F and addr >= 0x8000:
        return _window_offset(
            bank, addr, bank_start=0x00, size=payload - 0x400000,
            base=0x400000, bank_size=0x8000, addr_mask=0x7FFF,
        )
    if 0x40 <= bank <= 0x7D:
        return _window_offset(
            bank, addr, bank_start=0x40, size=payload - 0x600000,
            base=0x600000, bank_size=0x8000, addr_mask=0x7FFF,
        )
    if 0x80 <= bank <= 0xBF and addr >= 0x8000:
        return _window_offset(
            bank, addr, bank_start=0x80, size=min(payload, 0x400000),
            bank_size=0x8000, addr_mask=0x7FFF,
        )
    if 0xC0 <= bank <= 0xFF:
        return _window_offset(
            bank, addr, bank_start=0xC0, size=min(payload, 0x400000),
            base=0x200000, bank_size=0x8000, addr_mask=0x7FFF,
        )
    return None


def _exhirom_offset(bank: int, addr: int, payload: int) -> int | None:
    upper_size = payload - 0x400000
    if 0x00 <= bank <= 0x3F and addr >= 0x8000:
        return _window_offset(
            bank, addr, bank_start=0x00, size=upper_size, base=0x400000,
        )
    if 0x40 <= bank <= 0x7D:
        return _window_offset(
            bank, addr, bank_start=0x40, size=upper_size, base=0x400000,
        )
    if 0x80 <= bank <= 0xBF and addr >= 0x8000:
        return _window_offset(
            bank, addr, bank_start=0x80, size=min(payload, 0x400000),
        )
    if 0xC0 <= bank <= 0xFF:
        return _window_offset(
            bank, addr, bank_start=0xC0, size=min(payload, 0x400000),
        )
    return None


def _sa1_offset(bank: int, addr: int) -> tuple[int, str] | None:
    """Static SA-1 windows of the initial map.

    Banks C0-FF and the remaps enabled by $2220-$2223 are dynamic; they cannot be
    safely converted into a single file patch.
    """
    note = "SA-1: static LoROM window"
    if (0x00 <= bank <= 0x3F or 0x80 <= bank <= 0xBF) and addr >= 0x8000:
        return (bank & 0x3F) * 0x8000 + (addr & 0x7FFF), note
    return None


def _superfx_offset(bank: int, addr: int, payload: int) -> tuple[int, str] | None:
    low_size = min(payload, 0x200000)
    if (0x00 <= bank <= 0x3F or 0x80 <= bank <= 0xBF) and addr >= 0x8000:
        off = _window_offset(
            bank & 0x7F, addr, bank_start=0x00, size=low_size,
            bank_size=0x8000, addr_mask=0x7FFF,
        )
        return None if off is None else (off, "Super FX: LoROM window")
    if 0x40 <= bank <= 0x5F:
        off = _window_offset(bank, addr, bank_start=0x40, size=low_size)
        return None if off is None else (off, "Super FX: HiROM window $40-$5F")
    upper_end = 0xDF if payload <= 0x200000 else 0xFF
    if 0xC0 <= bank <= upper_end:
        off = _window_offset(bank, addr, bank_start=0xC0, size=payload)
        return None if off is None else (off, "Super FX: high HiROM window")
    return None


def _sdd1_offset(bank: int, addr: int, payload: int) -> tuple[int, str] | None:
    if (0x00 <= bank <= 0x3F or 0x80 <= bank <= 0xBF) and addr >= 0x8000:
        off = _window_offset(
            bank & 0x7F, addr, bank_start=0x00, size=payload,
            bank_size=0x8000, addr_mask=0x7FFF,
        )
        return None if off is None else (off, "S-DD1: static LoROM window")
    if 0x60 <= bank <= 0x6F or (0x70 <= bank <= 0x7D and addr >= 0x8000):
        off = _window_offset(bank, addr, bank_start=0x60, size=payload)
        return None if off is None else (off, "S-DD1: static HiROM window")
    return None


def _spc7110_offset(bank: int, addr: int, info: RomInfo) -> tuple[int, str] | None:
    payload = info.payload_size
    if 0x00 <= bank <= 0x0F and addr >= 0x8000:
        off = _window_offset(bank, addr, bank_start=0x00, size=payload)
        return None if off is None else (off, "SPC7110: low HiROM window")
    if 0x80 <= bank <= 0x8F and addr >= 0x8000:
        off = _window_offset(bank, addr, bank_start=0x80, size=payload)
        return None if off is None else (off, "SPC7110: HiROM mirror")
    if 0xC0 <= bank <= 0xCF:
        off = _window_offset(bank, addr, bank_start=0xC0, size=payload)
        return None if off is None else (off, "SPC7110: HiROM window C0-CF")
    if info.rom_size_byte >= 13 and payload > 0x600000 and 0x40 <= bank <= 0x4F:
        off = _window_offset(
            bank, addr, bank_start=0x40, size=payload - 0x600000, base=0x600000,
        )
        return None if off is None else (off, "SPC7110: extended ROM $40-$4F")
    return None


def _dsp_io_window(bank: int, addr: int, info: RomInfo) -> bool:
    if info.chip is not Chip.DSP:
        return False
    if info.chipset_byte == 0x03:
        if info.map_mode == 0x30:
            return (0x30 <= bank <= 0x3F or 0xB0 <= bank <= 0xBF) and addr >= 0x8000
        if info.topology is Topology.HIROM:
            return (bank <= 0x1F or 0x80 <= bank <= 0x9F) and 0x6000 <= addr < 0x8000
        if info.payload_size > 0x100000:
            return (0x60 <= bank <= 0x6F or 0xE0 <= bank <= 0xEF) and addr < 0x8000
        return (0x20 <= bank <= 0x3F or 0xA0 <= bank <= 0xBF) and addr >= 0x8000
    if info.chipset_byte == 0x05 and info.map_mode == 0x20:
        return (
            (0x20 <= bank <= 0x3F or 0xA0 <= bank <= 0xBF)
            and (0x6000 <= addr < 0x7000 or 0x8000 <= addr < 0xC000)
        )
    return (0x20 <= bank <= 0x3F or 0xA0 <= bank <= 0xBF) and addr >= 0x8000


def _lorom_sram_window(bank: int, addr: int, info: RomInfo) -> bool:
    if info.topology not in (Topology.LOROM, Topology.EXLOROM):
        return False
    if info.chip in (Chip.SA1, Chip.SUPERFX, Chip.SDD1):
        return False
    high = 0x7FFF if info.rom_size_byte > 11 or info.sram_size_byte > 5 else 0xFFFF
    if 0x70 <= bank <= 0x7D and addr <= high:
        return True
    return info.sram_size_byte > 0 and 0xF0 <= bank <= 0xFF and addr <= high


def translate(address: int, info: RomInfo) -> list[Candidate]:
    """Returns the candidate physical offsets for a CPU address.

    Raises :class:`PatchError` if the address cannot be ROM (RAM/registers).
    Returns an empty list if the address does not map to ROM in this topology.
    """
    bank = (address >> 16) & 0xFF
    addr = address & 0xFFFF

    if info.chip in (Chip.OTHER, Chip.CUSTOM):
        raise PatchError(
            f"The cartridge uses a coprocessor that is not safely supported "
            f"(chipset 0x{info.chipset_byte:02X}, map mode 0x{info.map_mode:02X})."
        )
    if info.title.startswith((
        "SOUND NOVEL-TCOOL",
        "DERBY STALLION 96",
        "ADD-ON BASE CASSETE",
        "WANDERERS FROM YS",
        "THOROUGHBRED BREEDER3",
        "RPG-TCOOL 2",
    )):
        raise PatchError(
            "This cartridge uses a special/multicart map that cannot yet be "
            "safely converted to a static patch."
        )

    region = classify_region(bank, addr)
    if region is Region.WRAM:
        raise PatchError(
            f"${address:06X} points to work RAM (WRAM $7E/$7F). "
            "Codes targeting RAM cannot be converted into a static patch."
        )
    if region is Region.LOWRAM:
        raise PatchError(
            f"${address:06X} points to the low-RAM mirror ($0000-$1FFF). "
            "It is not ROM; it cannot be patched statically."
        )
    if region is Region.IO:
        raise PatchError(
            f"${address:06X} points to hardware registers ($2000-$5FFF). "
            "It is not ROM; it cannot be patched statically."
        )
    if _dsp_io_window(bank, addr, info):
        raise PatchError(
            f"${address:06X} points to the {info.chip.value} I/O window. "
            "It is not ROM and cannot be patched statically."
        )
    if _lorom_sram_window(bank, addr, info):
        raise PatchError(
            f"${address:06X} points to cartridge SRAM according to its header. "
            "It is not ROM and cannot be patched statically."
        )
    if info.chip is Chip.SA1 and 0xC0 <= bank <= 0xFF:
        raise PatchError(
            f"${address:06X} uses a dynamic SA-1 Super MMC window. "
            "There is no single safe ROM offset."
        )
    if info.chip is Chip.SDD1 and 0xC0 <= bank <= 0xFF:
        raise PatchError(
            f"${address:06X} uses a dynamic S-DD1 window. "
            "There is no single safe ROM offset."
        )
    if info.chip is Chip.SPC7110 and (bank == 0x50 or 0xD0 <= bank <= 0xFF):
        raise PatchError(
            f"${address:06X} uses DRAM or a dynamic SPC7110 window. "
            "There is no single safe ROM offset."
        )

    # The fourth field indicates the mapper already applied its window mirroring.
    raw: list[tuple[int, str, bool, bool]] = []

    if info.chip is Chip.SA1:
        res = _sa1_offset(bank, addr)
        if res is not None:
            raw.append((res[0], f"{res[1]}: bank {bank:02X} ${addr:04X}", True, False))
    elif info.chip is Chip.SUPERFX:
        res = _superfx_offset(bank, addr, info.payload_size)
        if res is not None:
            raw.append((res[0], f"{res[1]}: bank {bank:02X} ${addr:04X}", True, True))
    elif info.chip is Chip.SDD1:
        res = _sdd1_offset(bank, addr, info.payload_size)
        if res is not None:
            raw.append((res[0], f"{res[1]}: bank {bank:02X} ${addr:04X}", True, True))
    elif info.chip is Chip.SPC7110:
        res = _spc7110_offset(bank, addr, info)
        if res is not None:
            raw.append((res[0], f"{res[1]}: bank {bank:02X} ${addr:04X}", True, True))
    elif info.topology is Topology.LOROM:
        off = _lorom_offset(bank, addr)
        if off is not None:
            raw.append((off, f"{info.topology.value}: bank {bank:02X} ${addr:04X}", True, False))
    elif info.topology is Topology.EXLOROM:
        off = _exlorom_offset(bank, addr, info.payload_size)
        if off is not None:
            raw.append((off, f"ExLoROM: bank {bank:02X} ${addr:04X}", True, True))
    elif info.topology is Topology.HIROM:
        off = _hirom_offset(bank, addr)
        if off is not None:
            raw.append((off, f"HiROM: bank {bank:02X} ${addr:04X}", True, False))
    elif info.topology is Topology.EXHIROM:
        off = _exhirom_offset(bank, addr, info.payload_size)
        if off is not None:
            raw.append((off, f"ExHiROM: bank {bank:02X} ${addr:04X}", True, True))
    else:
        return []

    # If detection confidence is low, also offer the alternative topology
    # (LoROM <-> HiROM) as a secondary candidate.
    if info.confidence < 24 and info.chip is Chip.NONE:
        if info.topology is Topology.LOROM:
            alt = _hirom_offset(bank, addr)
            if alt is not None:
                raw.append((alt, f"HiROM alternative: bank {bank:02X} ${addr:04X}", False, False))
        elif info.topology is Topology.HIROM:
            alt = _lorom_offset(bank, addr)
            if alt is not None:
                raw.append((alt, f"LoROM alternative: bank {bank:02X} ${addr:04X}", False, False))

    payload = info.payload_size
    out: list[Candidate] = []
    seen: set[int] = set()
    for offset, note, primary, already_mirrored in raw:
        mirrored = offset if already_mirrored else mirror_offset(offset, payload)
        if mirrored is None:
            continue
        if mirrored != offset:
            note += " (mirror)"
        physical = mirrored
        if info.extended_layout is ExtendedLayout.SMALL_FIRST:
            tail = payload - 0x400000
            physical = tail + mirrored if mirrored < 0x400000 else mirrored - 0x400000
            note += " (small-first layout)"
        file_offset = physical + info.copier_header
        if 0 <= file_offset < info.size and file_offset not in seen:
            seen.add(file_offset)
            out.append(Candidate(file_offset, note, primary))
    out.sort(key=lambda c: (not c.primary, c.file_offset))
    return out


# --------------------------------------------------------------------------- #
#  Building and applying plans
# --------------------------------------------------------------------------- #
def make_patch_plan(
    rom_path: str | Path,
    code_text: str,
    expected: int | None = None,
    *,
    candidate_index: int = 0,
    rom_info: RomInfo | None = None,
) -> PatchPlan:
    info = rom_info if rom_info is not None else inspect_rom(rom_path)
    code = decode_snes_game_genie(code_text)
    candidates = translate(code.cpu_address, info)
    if not candidates:
        raise PatchError(
            f"Address ${code.cpu_address:06X} does not map to ROM with the detected "
            f"topology ({info.topology.value})."
        )
    if candidate_index < 0 or candidate_index >= len(candidates):
        raise PatchError("Candidate index out of range.")
    chosen = candidates[candidate_index]

    with info.path.open("rb") as f:
        f.seek(chosen.file_offset)
        old = f.read(1)
    if len(old) != 1:
        raise PatchError("Could not read the target byte.")
    old_value = old[0]
    if expected is not None and old_value != expected:
        raise PatchError(
            f"The original byte is ${old_value:02X}, but ${expected:02X} was expected. "
            "The ROM or revision probably does not match."
        )
    return PatchPlan(code, info, chosen.file_offset, old_value, code.value, chosen.note)


def patch_candidates(rom_path: str | Path, code_text: str, *, rom_info: RomInfo | None = None) -> list[Candidate]:
    """List of candidates for a code (so the UI can offer a choice)."""
    info = rom_info if rom_info is not None else inspect_rom(rom_path)
    code = decode_snes_game_genie(code_text)
    return translate(code.cpu_address, info)


# --------------------------------------------------------------------------- #
#  Internal checksum
# --------------------------------------------------------------------------- #
def compute_internal_checksum(payload: bytes, *, chip: Chip = Chip.NONE) -> int:
    """Computes the SNES internal checksum (byte sum with block mirroring)."""
    n = len(payload)
    if n == 0:
        return 0
    if chip is Chip.SPC7110:
        total = sum(payload)
        if n == 0x300000:
            total *= 2
        return total & 0xFFFF
    if n & 0x7FFF:
        return sum(payload) & 0xFFFF
    def mirror_sum(start: int, length: int, mask: int) -> int:
        while mask and not (length & mask):
            mask >>= 1
        part1 = sum(payload[start:start + mask])
        next_length = length - mask
        if not next_length:
            return part1
        part2 = mirror_sum(start + mask, next_length, mask >> 1)
        while next_length < mask:
            next_length += next_length
            part2 += part2
        return part1 + part2

    return mirror_sum(0, n, 0x800000) & 0xFFFF


def _apply_checksum_to_buffer(buf: bytearray, info: RomInfo) -> None:
    payload = bytearray(buf[info.copier_header:])
    header = info.header_offset - info.copier_header
    payload[header + 0x1C:header + 0x1E] = b"\xFF\xFF"
    payload[header + 0x1E:header + 0x20] = b"\x00\x00"
    canonical = _canonical_payload(bytes(payload), info.extended_layout)
    checksum = compute_internal_checksum(canonical, chip=info.chip)
    complement = checksum ^ 0xFFFF
    h = info.header_offset
    buf[h + 0x1C:h + 0x1E] = complement.to_bytes(2, "little")
    buf[h + 0x1E:h + 0x20] = checksum.to_bytes(2, "little")


def _validate_same_source(plans: list[PatchPlan]) -> RomInfo:
    if not plans:
        raise PatchError("There are no patches to apply.")
    source = plans[0].rom.path.resolve()
    if any(plan.rom.path.resolve() != source for plan in plans):
        raise PatchError("All plans must belong to the same ROM.")
    reference = plans[0].rom
    if any(
        plan.rom.size != reference.size
        or plan.rom.sha256 != reference.sha256
        or plan.rom.topology is not reference.topology
        or plan.rom.chip is not reference.chip
        for plan in plans
    ):
        raise PatchError("The plans do not belong to the same ROM revision and analysis.")
    try:
        current = source.read_bytes()
    except OSError as exc:
        raise PatchError(f"Could not re-read the source ROM: {exc}") from exc
    if len(current) != reference.size or hashlib.sha256(current).hexdigest() != reference.sha256:
        raise PatchError(
            "The source ROM changed after it was analyzed. "
            "Reload it and rebuild the code list."
        )
    # Conflicts: same offset with a different value.
    by_offset: dict[int, int] = {}
    for plan in plans:
        if plan.file_offset in by_offset:
            if by_offset[plan.file_offset] != plan.new_value:
                raise PatchError(
                    f"Conflict: two codes write different values at 0x{plan.file_offset:06X}."
                )
            raise PatchError(
                f"Two codes write the same value at 0x{plan.file_offset:06X}. "
                "Remove the duplicate."
            )
        by_offset[plan.file_offset] = plan.new_value
    return plans[0].rom


def _atomic_write_bytes(output_path: str | Path, data: bytes) -> Path:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=out.name + ".", suffix=".tmp", dir=out.parent, delete=False
    ) as tmp:
        temp_path = Path(tmp.name)
    try:
        with temp_path.open("r+b") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, out)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return out


def apply_plans(
    plans: list[PatchPlan],
    output_path: str | Path,
    *,
    overwrite: bool = False,
    fix_checksum: bool = False,
    progress=None,
) -> Path:
    """Creates a patched ROM atomically. Never touches the original."""
    info = _validate_same_source(plans)
    source = info.path.resolve()
    out = Path(output_path)
    if out.resolve() == source:
        raise PatchError("The output cannot be the source ROM.")
    if out.exists() and not overwrite:
        raise PatchError(f"The output file already exists: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)

    if progress:
        progress(0.0, "Copying ROM…")

    with tempfile.NamedTemporaryFile(prefix=out.name + ".", suffix=".tmp", dir=out.parent, delete=False) as tmp:
        temp_path = Path(tmp.name)
    try:
        shutil.copyfile(source, temp_path)
        buf = bytearray(temp_path.read_bytes())
        total = len(plans)
        for i, plan in enumerate(plans, 1):
            current = buf[plan.file_offset]
            if current != plan.old_value:
                raise PatchError(
                    f"The byte at 0x{plan.file_offset:06X} is ${current:02X}, "
                    f"expected ${plan.old_value:02X}; operation cancelled."
                )
            buf[plan.file_offset] = plan.new_value
            if progress:
                progress(0.2 + 0.6 * i / total, f"Applying {i}/{total}…")
        if fix_checksum:
            if progress:
                progress(0.85, "Recalculating checksum…")
            _apply_checksum_to_buffer(buf, info)
        with temp_path.open("r+b") as f:
            f.write(buf)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, out)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    if progress:
        progress(1.0, "Done.")
    return out
