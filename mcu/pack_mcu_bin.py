#!/usr/bin/env python3
"""Creality K2 Plus MCU binary packer and header/CRC-16 verifier."""

from __future__ import annotations

import argparse
import shutil
import struct
import sys
from pathlib import Path

SV_OFFSET = 0x200
SV_LEN = 12
CRC_OFFSET = 0x20C
LEN_OFFSET = 0x20E
HEADER_END = 0x212


def _build_ccitt_table() -> list[int]:
    table: list[int] = []
    for byte in range(256):
        val = byte << 8
        for _ in range(8):
            val = ((val << 1) ^ 0x1021) if (val & 0x8000) else (val << 1)
            val &= 0xFFFF
        table.append(val)
    return table


_CCITT_TABLE = _build_ccitt_table()


def crc16_ccitt(data: bytes) -> int:
    """Compute Creality legacy CRC-16 (poly 0x1021, init 0, MSB-first)."""
    crc = 0
    for b in data:
        crc = ((crc << 8) ^ _CCITT_TABLE[((crc >> 8) ^ b) & 0xFF]) & 0xFFFF
    return crc


def crc16_ibm(data: bytes) -> int:
    """Compute Creality new CRC-16 (poly 0x8005, init 0, MSB-first)."""
    crc = 0
    for b in data:
        crc ^= (b << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x8005) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def compute_crc16(data: bytes, mode: str) -> int:
    """Compute Creality MCU bootloader CRC-16 for mode 'ccitt' or 'ibm'."""
    if mode == "ccitt":
        return crc16_ccitt(data)
    if mode == "ibm":
        return crc16_ibm(data)
    raise ValueError(f"Unsupported CRC mode: {mode!r}")


def pack_mcu_bin(
    src_bin: Path,
    dst_bin: Path,
    *,
    crc_mode: str,
    expected_sv: str | None = None,
    max_size: int | None = None,
) -> tuple[int, int]:
    """Patch Creality CRC-16 and length fields into a compiled raw MCU .bin."""
    raw = bytearray(src_bin.read_bytes())
    if len(raw) < HEADER_END:
        raise ValueError(f"{src_bin} too small ({len(raw)} bytes < {HEADER_END})")
    if max_size is not None and len(raw) > max_size:
        raise ValueError(
            f"{src_bin} exceeds flash budget ({len(raw)} bytes > {max_size} bytes)"
        )

    sv_bytes = bytes(raw[SV_OFFSET : SV_OFFSET + SV_LEN])
    if expected_sv is not None:
        exp_bytes = expected_sv.encode("ascii")
        if len(exp_bytes) != SV_LEN:
            raise ValueError(
                f"Expected SV tag must be {SV_LEN} ASCII bytes, got {expected_sv!r}"
            )
        if sv_bytes != exp_bytes:
            raise ValueError(
                f"Unexpected SV header at 0x{SV_OFFSET:x}: {sv_bytes!r} != {exp_bytes!r}"
            )

    if bytes(raw[CRC_OFFSET:HEADER_END]) != b"\x00" * 6:
        raise ValueError(
            f"Expected zeroed CRC/len slot at 0x{CRC_OFFSET:x}..0x{HEADER_END:x}, "
            f"found {bytes(raw[CRC_OFFSET:HEADER_END]).hex()}"
        )

    crc = compute_crc16(bytes(raw), crc_mode)
    flen = len(raw)
    struct.pack_into("<HI", raw, CRC_OFFSET, crc, flen)

    dst_bin.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst_bin.with_suffix(dst_bin.suffix + ".tmp")
    tmp.write_bytes(bytes(raw))
    shutil.move(str(tmp), str(dst_bin))
    return crc, flen


def verify_packed_bin(bin_path: Path, *, crc_mode: str) -> tuple[str, int, int]:
    """Verify CRC-16 and embedded length inside an existing packed MCU .bin."""
    raw = bytearray(bin_path.read_bytes())
    if len(raw) < HEADER_END:
        raise ValueError(f"{bin_path} too small ({len(raw)} bytes)")
    stored_crc, stored_len = struct.unpack_from("<HI", raw, CRC_OFFSET)
    if stored_len != len(raw):
        raise ValueError(
            f"{bin_path}: header length {stored_len} != file size {len(raw)}"
        )
    sv = raw[SV_OFFSET : SV_OFFSET + SV_LEN].decode("ascii", errors="strict")
    raw[CRC_OFFSET:HEADER_END] = b"\x00" * 6
    computed = compute_crc16(bytes(raw), crc_mode)
    if computed != stored_crc:
        raise ValueError(
            f"{bin_path}: CRC16 mismatch ({crc_mode}): "
            f"computed 0x{computed:04x} != header 0x{stored_crc:04x}"
        )
    return sv, stored_crc, stored_len


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pack = sub.add_parser("pack", help="Patch CRC-16 and length into a raw .bin")
    p_pack.add_argument("--input", type=Path, required=True)
    p_pack.add_argument("--output", type=Path, required=True)
    p_pack.add_argument("--crc-mode", choices=("ccitt", "ibm"), required=True)
    p_pack.add_argument("--expected-sv", default=None)
    p_pack.add_argument("--max-size", type=lambda x: int(x, 0), default=None)

    p_ver = sub.add_parser("verify", help="Verify an already-packed .bin")
    p_ver.add_argument("--input", type=Path, required=True)
    p_ver.add_argument("--crc-mode", choices=("ccitt", "ibm"), required=True)

    args = parser.parse_args(argv)
    if args.cmd == "pack":
        crc, flen = pack_mcu_bin(
            args.input,
            args.output,
            crc_mode=args.crc_mode,
            expected_sv=args.expected_sv,
            max_size=args.max_size,
        )
        print(
            f"Packed {args.output.name}: crc=0x{crc:04x} ({args.crc_mode}) "
            f"len={flen} (0x{flen:x})"
        )
    else:
        sv, crc, flen = verify_packed_bin(args.input, crc_mode=args.crc_mode)
        print(
            f"Verified {args.input.name}: sv={sv!r} crc=0x{crc:04x} "
            f"len={flen} (0x{flen:x})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
