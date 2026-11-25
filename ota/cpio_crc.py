"""Deterministic SVR4 CRC CPIO (070702) reader and writer for K2 SWUpdate .img packages."""

from __future__ import annotations

import contextlib
import mmap
import os
import stat
from pathlib import Path
from typing import Iterator, Mapping, Sequence

CPIO_CRC_MAGIC = b"070702"
HEADER_LEN = 110
TRAILER_NAME = "TRAILER!!!"
DEFAULT_MTIME = 1788566400
CPIO_BLOCK_SIZE = 512


def _pad4(offset: int) -> int:
    return (offset + 3) & ~3


def crc32_bytesum(data: bytes | memoryview) -> int:
    """Compute the SVR4 CPIO CRC checksum (unsigned 32-bit byte sum)."""
    return sum(data) & 0xFFFFFFFF


def _validate_flat_name(name: str) -> bytes:
    if not name or name in (".", "..", TRAILER_NAME) or "/" in name or "\\" in name:
        raise ValueError(f"Unsafe CPIO entry name: {name!r}")
    encoded = name.encode("ascii")
    if len(encoded) + 1 >= 128:
        raise ValueError(f"CPIO entry name too long: {name!r}")
    return encoded


def _format_header(
    *,
    ino: int,
    mode: int,
    nlink: int,
    mtime: int,
    filesize: int,
    namesize: int,
    checksum: int,
) -> bytes:
    fields = (
        ino,
        mode,
        0,  # uid
        0,  # gid
        nlink,
        mtime,
        filesize,
        0,  # devmajor
        0,  # devminor
        0,  # rdevmajor
        0,  # rdevminor
        namesize,
        checksum,
    )
    return CPIO_CRC_MAGIC + "".join(f"{val:08x}" for val in fields).encode("ascii")


def write_cpio_crc(
    path: Path,
    entries: Sequence[tuple[str, bytes, int]],
    *,
    mtime: int = DEFAULT_MTIME,
) -> None:
    """Write a deterministic SVR4 CRC CPIO archive padded to 512-byte blocks."""
    seen: set[str] = set()
    tmp_path = path.with_name(f".{path.name}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with tmp_path.open("wb") as fh:
            for index, (name, payload, mode_bits) in enumerate(entries, start=1):
                if name in seen:
                    raise ValueError(f"Duplicate CPIO entry: {name}")
                seen.add(name)
                name_bytes = _validate_flat_name(name) + b"\x00"
                mode = stat.S_IFREG | (mode_bits & 0o777)
                checksum = crc32_bytesum(payload)
                header = _format_header(
                    ino=index,
                    mode=mode,
                    nlink=1,
                    mtime=mtime,
                    filesize=len(payload),
                    namesize=len(name_bytes),
                    checksum=checksum,
                )
                fh.write(header)
                fh.write(name_bytes)
                written += len(header) + len(name_bytes)
                name_pad = _pad4(written) - written
                if name_pad:
                    fh.write(b"\x00" * name_pad)
                    written += name_pad
                fh.write(payload)
                written += len(payload)
                data_pad = _pad4(written) - written
                if data_pad:
                    fh.write(b"\x00" * data_pad)
                    written += data_pad

            trailer_bytes = TRAILER_NAME.encode("ascii") + b"\x00"
            trailer_hdr = _format_header(
                ino=0,
                mode=0,
                nlink=1,
                mtime=0,
                filesize=0,
                namesize=len(trailer_bytes),
                checksum=0,
            )
            fh.write(trailer_hdr)
            fh.write(trailer_bytes)
            written += len(trailer_hdr) + len(trailer_bytes)
            trailer_pad = _pad4(written) - written
            if trailer_pad:
                fh.write(b"\x00" * trailer_pad)
                written += trailer_pad
            block_rem = written % CPIO_BLOCK_SIZE
            if block_rem:
                fh.write(b"\x00" * (CPIO_BLOCK_SIZE - block_rem))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def read_cpio_crc(
    data: bytes | bytearray | memoryview,
    *,
    copy_payloads: bool = True,
) -> dict[str, bytes | memoryview]:
    """Parse and strictly validate a flat SVR4 CRC CPIO archive."""
    result: dict[str, bytes | memoryview] = {}
    view = memoryview(data)
    offset = 0
    content: memoryview | None = None
    try:
        if len(view) == 0 or len(view) % CPIO_BLOCK_SIZE != 0:
            raise ValueError(
                f"CPIO archive length {len(view)} is empty or not aligned to {CPIO_BLOCK_SIZE}-byte blocks"
            )
        while True:
            if offset + HEADER_LEN > len(view) or bytes(view[offset : offset + 6]) != CPIO_CRC_MAGIC:
                raise ValueError(f"Expected CRC CPIO header at offset {offset}")
            hdr_raw = bytes(view[offset + 6 : offset + HEADER_LEN])
            try:
                fields = [int(hdr_raw[n : n + 8], 16) for n in range(0, 104, 8)]
            except ValueError as exc:
                raise ValueError("Invalid hex field in CPIO header") from exc
            mode, nlink, size, namesize, checksum = (
                fields[1],
                fields[4],
                fields[6],
                fields[11],
                fields[12],
            )
            if not 0 < namesize < 128:
                raise ValueError(f"Invalid CPIO name size: {namesize}")
            name_start = offset + HEADER_LEN
            name_end = name_start + namesize
            if name_end > len(view):
                raise ValueError("Truncated CPIO filename")
            name_bytes = bytes(view[name_start:name_end])
            if not name_bytes.endswith(b"\x00"):
                raise ValueError("CPIO filename missing NUL terminator")
            try:
                name = name_bytes[:-1].decode("ascii")
            except UnicodeDecodeError as exc:
                raise ValueError("Non-ASCII CPIO filename") from exc
            data_start = _pad4(name_end)
            data_end = data_start + size
            if data_end > len(view):
                raise ValueError(f"Truncated CPIO payload for {name}")
            content = view[data_start:data_end]
            if crc32_bytesum(content) != checksum:
                raise ValueError(f"CPIO CRC mismatch for {name}")
            offset = _pad4(data_end)
            if name == TRAILER_NAME:
                if size != 0 or any(view[offset:]):
                    raise ValueError("Invalid CPIO trailer or non-zero trailing data")
                content.release()
                content = None
                break
            _validate_flat_name(name)
            if not stat.S_ISREG(mode) or nlink != 1:
                raise ValueError(f"Unsupported CPIO entry type or link count for {name}")
            if name in result:
                raise ValueError(f"Duplicate CPIO entry: {name}")
            result[name] = bytes(content) if copy_payloads else content
            if copy_payloads:
                content.release()
            content = None
    except BaseException:
        if content is not None:
            content.release()
        for value in result.values():
            if isinstance(value, memoryview):
                value.release()
        raise
    finally:
        view.release()
    return result


@contextlib.contextmanager
def mapped_cpio_crc(path: Path) -> Iterator[tuple[bytes, Mapping[str, memoryview]]]:
    """Memory-map a CRC CPIO file and yield validated zero-copy payload views."""
    with path.open("rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
            raise ValueError(f"CPIO path must be a non-empty regular file: {path}")
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            entries = read_cpio_crc(mm, copy_payloads=False)
            try:
                yield mm, entries
            finally:
                for value in entries.values():
                    if isinstance(value, memoryview):
                        value.release()
