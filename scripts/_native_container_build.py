#!/usr/bin/env python3
"""Containerized ARMv7 native builder and ABI/Python-3.9.12 verifier."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SRC_KLIPPER = Path("/src/klipper")
SRC_MAINLINE = Path("/src/k2_mainline")
WORK_KLIPPER = Path("/work/klipper")
OUT_DIR = Path("/out")

EXPECTED_K2_HW_EXPORTS = sorted(
    [
        "serial_485_queue_alloc",
        "serial_485_queue_exit",
        "serial_485_queue_free",
        "serial_485_queue_send",
        "serial_485_queue_pull",
        "serial_485_queue_get_stats",
        "serial_485_queue_extract_old",
        "msgblock_485_crc8",
        "msgblock_485_check",
        "get_flushing_volume",
    ]
)

K2_HW_CDEF = """
    struct pull_message {
        int len;
        uint8_t msg[512];
    };
    typedef struct {
        uint8_t r, g, b;
    } rgb_t;
    struct serial_485_queue *serial_485_queue_alloc(int serial_fd, char serial_fd_type);
    void serial_485_queue_exit(struct serial_485_queue *sq);
    void serial_485_queue_free(struct serial_485_queue *sq);
    void serial_485_queue_send(struct serial_485_queue *sq, uint8_t *msg, int len);
    void serial_485_queue_pull(struct serial_485_queue *sq, struct pull_message *pqm);
    void serial_485_queue_get_stats(struct serial_485_queue *sq, char *buf, int len);
    int serial_485_queue_extract_old(struct serial_485_queue *sq, int sentq,
                                     struct pull_message *p, int max);
    uint8_t msgblock_485_crc8(const uint8_t *data, uint32_t len);
    int msgblock_485_check(uint8_t *need_sync, uint8_t *buf, int buf_len);
    int get_flushing_volume(const rgb_t source, const rgb_t target);
"""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def run(cmd: list[str], *, cwd: Path | None = None) -> str:
    print("+", " ".join(cmd), flush=True)
    res = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if res.stdout:
        print(res.stdout, end="", flush=True)
    return res.stdout


def build_c_helper() -> Path:
    """Compile Klipper's upstream chelper/c_helper.so using Python 3.9 + CFFI."""
    print("=== Building upstream c_helper.so ===", flush=True)
    klippy_dir = str(WORK_KLIPPER / "klippy")
    if klippy_dir not in sys.path:
        sys.path.insert(0, klippy_dir)
    import chelper  # noqa: E402

    chelper.get_ffi()
    built = WORK_KLIPPER / "klippy" / "chelper" / "c_helper.so"
    if not built.is_file():
        raise FileNotFoundError(f"c_helper.so was not produced at {built}")
    dest = OUT_DIR / "c_helper.so"
    shutil.copy2(built, dest)
    return dest


def build_and_verify_k2_hardware() -> tuple[Path, dict]:
    """Compile k2_hardware.so and verify CFFI ABI layout + symbol exports."""
    print("=== Building k2_hardware.so ===", flush=True)
    native_dir = SRC_MAINLINE / "native"
    chelper_dir = WORK_KLIPPER / "klippy" / "chelper"
    out_so = OUT_DIR / "k2_hardware.so"

    run(
        [
            "gcc",
            "-Wall",
            "-O2",
            "-ffp-contract=off",
            "-fPIC",
            "-shared",
            f"-I{chelper_dir}",
            f"-I{native_dir}",
            "-Wl,--no-undefined",
            f"-Wl,--version-script={native_dir / 'exports.map'}",
            str(native_dir / "msgblock_485.c"),
            str(native_dir / "serial_485_queue.c"),
            str(native_dir / "filament_change.c"),
            str(chelper_dir / "pyhelper.c"),
            "-lm",
            "-pthread",
            "-o",
            str(out_so),
        ]
    )

    nm_out = subprocess.check_output(
        ["nm", "-D", "--defined-only", str(out_so)], text=True
    )
    exported: list[str] = []
    for line in nm_out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] in ("T", "D", "R", "B"):
            exported.append(parts[2])
    exported_sorted = sorted(exported)
    assert exported_sorted == EXPECTED_K2_HW_EXPORTS, (
        f"Unexpected exports in k2_hardware.so: {exported_sorted} != {EXPECTED_K2_HW_EXPORTS}"
    )

    import cffi  # noqa: E402

    ffi = cffi.FFI()
    ffi.cdef(K2_HW_CDEF)
    lib = ffi.dlopen(str(out_so))

    pull_sz = ffi.sizeof("struct pull_message")
    rgb_sz = ffi.sizeof("rgb_t")
    crc_val = lib.msgblock_485_crc8(b"123456789", 9)
    src_rgb = ffi.new("rgb_t *", {"r": 0, "g": 0, "b": 0})[0]
    dst_rgb = ffi.new("rgb_t *", {"r": 255, "g": 255, "b": 255})[0]
    flush_vol = lib.get_flushing_volume(src_rgb, dst_rgb)

    assert pull_sz == 516, f"sizeof(struct pull_message)={pull_sz} != 516"
    assert rgb_sz == 3, f"sizeof(rgb_t)={rgb_sz} != 3"
    assert crc_val == 0xF4, f"msgblock_485_crc8('123456789')=0x{crc_val:02x} != 0xf4"
    assert isinstance(flush_vol, int) and flush_vol > 0, f"get_flushing_volume={flush_vol}"

    return out_so, {
        "exported_symbols": exported_sorted,
        "pull_message_size": pull_sz,
        "rgb_t_size": rgb_sz,
        "crc8_check_hex": f"0x{crc_val:02x}",
        "flushing_volume_black_to_white": flush_vol,
    }


def build_klipper_mcu() -> Path:
    """Compile upstream Klipper Linux host MCU daemon (klipper_mcu)."""
    print("=== Building klipper_mcu (src/linux) ===", flush=True)
    cfg_src = SRC_MAINLINE / "mcu" / "configs" / "linux_armv7.config"
    cfg_dst = WORK_KLIPPER / ".config"
    shutil.copyfile(cfg_src, cfg_dst)

    run(["make", "PYTHON=python3", "olddefconfig"], cwd=WORK_KLIPPER)
    out_dir = WORK_KLIPPER / "out"
    if out_dir.exists():
        shutil.rmtree(out_dir)

    jobs = str(max(1, os.cpu_count() or 4))
    run(["make", f"-j{jobs}", "PYTHON=python3"], cwd=WORK_KLIPPER)

    built = WORK_KLIPPER / "out" / "klipper.elf"
    if not built.is_file():
        raise FileNotFoundError(f"Linux MCU ELF missing at {built}")
    dest = OUT_DIR / "klipper_mcu"
    shutil.copy2(built, dest)
    os.chmod(dest, 0o755)
    return dest


def check_elf_abi(path: Path, *, require_interp: str | None) -> dict:
    """Gate ELF architecture, PT_INTERP, and GLIBC <= 2.29 symbol requirements."""
    hdr = subprocess.check_output(["readelf", "-h", str(path)], text=True)
    assert "Class:                             ELF32" in hdr, f"{path}: not ELF32:\n{hdr}"
    assert "little endian" in hdr, f"{path}: not little endian:\n{hdr}"
    assert "Machine:                           ARM" in hdr, f"{path}: not ARM:\n{hdr}"

    interp_found = None
    phdr = subprocess.check_output(["readelf", "-l", str(path)], text=True)
    m_interp = re.search(r"Requesting program interpreter:\s+([^\]]+)\]", phdr)
    if m_interp:
        interp_found = m_interp.group(1).strip()
    if require_interp is not None:
        assert interp_found == require_interp, (
            f"{path}: PT_INTERP={interp_found!r} != {require_interp!r}"
        )

    ver_out = subprocess.check_output(["readelf", "-V", str(path)], text=True)
    versions: list[tuple[int, int]] = []
    for m in re.finditer(r"GLIBC_(\d+)\.(\d+)(?:\.(\d+))?", ver_out):
        ver = (int(m.group(1)), int(m.group(2)))
        versions.append(ver)
        assert ver <= (2, 29), (
            f"{path}: requires GLIBC_{ver[0]}.{ver[1]} > GLIBC_2.29"
        )

    max_ver = max(versions) if versions else None
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "pt_interp": interp_found,
        "max_glibc": f"{max_ver[0]}.{max_ver[1]}" if max_ver else "none",
    }


def verify_python_39_klippy() -> dict:
    """Verify every Klipper v0.14.0 Python file parses cleanly and passes --import-test."""
    print("=== Verifying Klipper v0.14.0 on Python 3.9.12 ===", flush=True)
    klippy_dir = WORK_KLIPPER / "klippy"
    py_files = sorted(klippy_dir.rglob("*.py"))
    assert py_files, f"No .py files found under {klippy_dir}"
    for py_path in py_files:
        ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))

    run([sys.executable, str(klippy_dir / "klippy.py"), "--import-test"], cwd=WORK_KLIPPER)
    return {
        "ast_parsed_files": len(py_files),
        "import_test": "passed",
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if WORK_KLIPPER.exists():
        shutil.rmtree(WORK_KLIPPER)
    shutil.copytree(SRC_KLIPPER, WORK_KLIPPER, symlinks=True)

    c_helper = build_c_helper()
    k2_hw, abi_info = build_and_verify_k2_hardware()
    klipper_mcu = build_klipper_mcu()

    elfs = {
        "c_helper.so": check_elf_abi(c_helper, require_interp=None),
        "k2_hardware.so": check_elf_abi(k2_hw, require_interp=None),
        "klipper_mcu": check_elf_abi(
            klipper_mcu, require_interp="/lib/ld-linux-armhf.so.3"
        ),
    }
    py_info = verify_python_39_klippy()

    gcc_ver = subprocess.check_output(["gcc", "--version"], text=True).splitlines()[0]
    ldd_ver = subprocess.check_output(["ldd", "--version"], text=True).splitlines()[0]

    report = {
        "python_version": sys.version.split()[0],
        "gcc_version": gcc_ver,
        "glibc_container": ldd_ver,
        "python_check": py_info,
        "k2_hardware_abi": abi_info,
        "artifacts": elfs,
    }
    report_path = OUT_DIR / "build_native_report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
