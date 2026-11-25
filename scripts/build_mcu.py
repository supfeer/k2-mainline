#!/usr/bin/env python3
"""Build MAIN (gd32f303xe) and NOZZLE (gd32f303xb) MCU firmwares against Klipper v0.14.0."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcu"))

from common import (  # noqa: E402
    K2_MAINLINE_ROOT,
    get_build_root,
    load_upstream_manifest,
    recreate_dir,
    run_checked,
)
from fetch_sources import ensure_klipper_tarball, extract_klipper  # noqa: E402
from pack_mcu_bin import pack_mcu_bin, verify_packed_bin  # noqa: E402


@dataclass(frozen=True)
class McuTarget:
    name: str
    config_rel: str
    out_bin: str
    out_elf: str
    out_dict: str
    crc_mode: str
    expected_sv: str
    expected_mcu: str
    flash_start: int
    flash_budget: int
    ram_start: int
    ram_end: int
    required_commands: tuple[str, ...]
    required_responses: tuple[str, ...]


TARGETS: dict[str, McuTarget] = {
    "main": McuTarget(
        name="main",
        config_rel="mcu/configs/main_gd32f303xe.config",
        out_bin="mcu0_140_G32-mcu0_022_950.bin",
        out_elf="main_klipper.elf",
        out_dict="main_klipper.dict",
        crc_mode="ccitt",
        expected_sv="mcu0_022_950",
        expected_mcu="gd32f303xe",
        flash_start=0x08003000,
        flash_budget=0x00080000 - 0x3000,
        ram_start=0x20000000,
        ram_end=0x20010000,
        required_commands=(
            "config_transparent",
            "transparent_send",
            "config_fancheck",
            "query_fancheck",
            "query_gpio_status",
        ),
        required_responses=(
            "transparent_response",
            "fan_status",
            "gpio_status",
        ),
    ),
    "nozzle": McuTarget(
        name="nozzle",
        config_rel="mcu/configs/nozzle_gd32f303xb.config",
        out_bin="noz0_130_G30-noz0_021_950.bin",
        out_elf="nozzle_klipper.elf",
        out_dict="nozzle_klipper.dict",
        crc_mode="ibm",
        expected_sv="noz0_021_950",
        expected_mcu="gd32f303xb",
        flash_start=0x08003000,
        flash_budget=0x00020000 - 0x3000,
        ram_start=0x20000000,
        ram_end=0x20008000,
        required_commands=(
            "config_transparent",
            "transparent_send",
            "config_fancheck",
            "query_fancheck",
            "query_gpio_status",
            "config_cs1237",
            "config_lis2dw",
            "config_trigger_analog",
        ),
        required_responses=(
            "transparent_response",
            "fan_status",
            "gpio_status",
        ),
    ),
}


def resolve_arm_toolchain() -> tuple[str, dict[str, str]]:
    """Locate arm-none-eabi-gcc preferring pinned GCC 9 bundle or PATH."""
    candidates = []
    override = os.environ.get("ARM_NONE_EABI_BIN", "").strip()
    if override:
        candidates.append(Path(override))
    candidates.append(
        Path.home()
        / ".cache/creality-helper/reverse/gcc-arm-none-eabi-9-2019-q4-major/bin"
    )
    existing_which = shutil.which("arm-none-eabi-gcc")
    if existing_which:
        candidates.append(Path(existing_which).parent)

    for bin_dir in candidates:
        gcc = bin_dir / "arm-none-eabi-gcc"
        if gcc.is_file():
            env = {
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            }
            return str(gcc), env

    raise FileNotFoundError(
        "Unable to locate arm-none-eabi-gcc in ARM_NONE_EABI_BIN, "
        "~/.cache/creality-helper/reverse/gcc-arm-none-eabi-9-2019-q4-major/bin, or PATH"
    )


def prepare_mcu_stage(clean_klipper: Path, stage_dir: Path) -> None:
    """Create a fresh MCU staging tree combining clean Klipper v0.14.0 with src/gd32."""
    recreate_dir(stage_dir)
    for item in clean_klipper.iterdir():
        dest = stage_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dest, symlinks=True)
        else:
            shutil.copy2(item, dest)

    # Overlay GD32 HAL and CMSIS/StdPeriph library without modifying upstream logic.
    shutil.copytree(
        K2_MAINLINE_ROOT / "mcu" / "src" / "gd32",
        stage_dir / "src" / "gd32",
        dirs_exist_ok=True,
    )
    shutil.copytree(
        K2_MAINLINE_ROOT / "mcu" / "lib" / "gd32f30x",
        stage_dir / "lib" / "gd32f30x",
        dirs_exist_ok=True,
    )

    kconfig_path = stage_dir / "src" / "Kconfig"
    orig = kconfig_path.read_text(encoding="utf-8")
    stm32_choice = '    config MACH_STM32\n        bool "STMicroelectronics STM32"'
    gd32_choice = f'{stm32_choice}\n    config MACH_GD32\n        bool "GigaDevice GD32"'
    if stm32_choice not in orig:
        raise RuntimeError(f"Expected anchor missing in {kconfig_path}")
    patched = orig.replace(stm32_choice, gd32_choice, 1)

    stm32_source = 'source "src/stm32/Kconfig"'
    gd32_source = f'{stm32_source}\nsource "src/gd32/Kconfig"'
    if stm32_source not in patched:
        raise RuntimeError(f"Expected source anchor missing in {kconfig_path}")
    patched = patched.replace(stm32_source, gd32_source, 1)
    kconfig_path.write_text(patched, encoding="utf-8")


def _parse_nm_symbols(nm_output: str) -> dict[str, int]:
    syms: dict[str, int] = {}
    for line in nm_output.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            try:
                syms[parts[2]] = int(parts[0], 16)
            except ValueError:
                continue
    return syms


def validate_elf_and_dict(
    target: McuTarget,
    elf_path: Path,
    bin_path: Path,
    dict_path: Path,
    env: dict[str, str],
) -> None:
    """Validate linker symbols, RAM/Flash limits, vector table, and klipper.dict."""
    merged_env = os.environ.copy()
    merged_env.update(env)

    nm_out = subprocess.check_output(
        ["arm-none-eabi-nm", str(elf_path)],
        env=merged_env,
        text=True,
    )
    syms = _parse_nm_symbols(nm_out)

    vt_start = syms.get("_text_vectortable_start")
    bss_end = syms.get("_bss_end")
    stack_start = syms.get("_stack_start")
    stack_end = syms.get("_stack_end")
    if None in (vt_start, bss_end, stack_start, stack_end):
        raise RuntimeError(f"Missing expected linker symbols in {elf_path}")

    assert vt_start == target.flash_start, (
        f"{target.name}: vector table at 0x{vt_start:x} != 0x{target.flash_start:x}"
    )
    assert target.ram_start <= bss_end <= stack_start, (
        f"{target.name}: RAM overflow _bss_end=0x{bss_end:x} > _stack_start=0x{stack_start:x}"
    )
    assert stack_end == target.ram_end, (
        f"{target.name}: _stack_end=0x{stack_end:x} != expected RAM end 0x{target.ram_end:x}"
    )

    raw = bin_path.read_bytes()
    init_sp, reset_vec = struct.unpack_from("<II", raw, 0)
    assert init_sp == stack_end, (
        f"{target.name}: vector[0] SP 0x{init_sp:x} != _stack_end 0x{stack_end:x}"
    )
    flash_end = target.flash_start + len(raw)
    assert (reset_vec & 1) == 1 and target.flash_start < reset_vec < flash_end, (
        f"{target.name}: invalid ResetHandler vector 0x{reset_vec:x}"
    )

    kd = json.loads(dict_path.read_text(encoding="utf-8"))
    cfg = kd.get("config", {})
    assert cfg.get("MCU") == target.expected_mcu, (
        f"{target.name}: dict MCU={cfg.get('MCU')!r} != {target.expected_mcu!r}"
    )
    assert cfg.get("CLOCK_FREQ") == 120000000, f"{target.name}: CLOCK_FREQ={cfg.get('CLOCK_FREQ')}"
    assert cfg.get("SERIAL_BAUD") == 230400, f"{target.name}: SERIAL_BAUD={cfg.get('SERIAL_BAUD')}"
    assert cfg.get("RESERVE_PINS_serial") == "PA2,PA3", (
        f"{target.name}: RESERVE_PINS_serial={cfg.get('RESERVE_PINS_serial')!r}"
    )
    assert cfg.get("STEPPER_STEP_BOTH_EDGE") == 1, (
        f"{target.name}: STEPPER_STEP_BOTH_EDGE missing"
    )

    cmds = kd.get("commands", {})
    resps = kd.get("responses", {})
    for prefix in target.required_commands:
        if not any(k == prefix or k.startswith(prefix + " ") for k in cmds):
            raise AssertionError(f"{target.name}: missing MCU command {prefix!r} in klipper.dict")
    for prefix in target.required_responses:
        if not any(k == prefix or k.startswith(prefix + " ") for k in resps):
            raise AssertionError(f"{target.name}: missing MCU response {prefix!r} in klipper.dict")

    size_out = subprocess.check_output(
        ["arm-none-eabi-size", str(elf_path)],
        env=merged_env,
        text=True,
    ).strip()
    print(f"  [{target.name}] {size_out.splitlines()[-1]}")


def build_target(
    target: McuTarget,
    stage_dir: Path,
    out_mcu_dir: Path,
    env: dict[str, str],
    *,
    jobs: int,
    verbose: bool,
) -> dict[str, str | int]:
    """Configure, compile, pack, and verify a single GD32 MCU firmware target."""
    print(f"=== Building MCU target: {target.name} ({target.expected_mcu}) ===", flush=True)
    out_dir = stage_dir / "out"
    recreate_dir(out_dir)

    cfg_src = K2_MAINLINE_ROOT / target.config_rel
    cfg_dst = stage_dir / ".config"
    shutil.copyfile(cfg_src, cfg_dst)

    make_vars = ["PYTHON=python3"]
    if verbose:
        make_vars.append("V=1")

    run_checked(["make", *make_vars, "olddefconfig"], cwd=stage_dir, env=env)
    # Remove any board-link generated during olddefconfig so the main build
    # creates out/board from the finalized .config without timestamp races.
    recreate_dir(out_dir)
    resolved_cfg = cfg_dst.read_text(encoding="utf-8")
    if target.crc_mode == "ibm":
        assert "CONFIG_USE_NEW_CRC16=y" in resolved_cfg
    else:
        assert "CONFIG_USE_NEW_CRC16=y" not in resolved_cfg
    assert "CONFIG_FLASH_APPLICATION_ADDRESS=0x8003000" in resolved_cfg

    run_checked(["make", f"-j{jobs}", *make_vars], cwd=stage_dir, env=env)

    raw_bin = out_dir / "klipper.bin"
    raw_elf = out_dir / "klipper.elf"
    raw_dict = out_dir / "klipper.dict"

    dest_bin = out_mcu_dir / target.out_bin
    dest_elf = out_mcu_dir / target.out_elf
    dest_dict = out_mcu_dir / target.out_dict

    crc, flen = pack_mcu_bin(
        raw_bin,
        dest_bin,
        crc_mode=target.crc_mode,
        expected_sv=target.expected_sv,
        max_size=target.flash_budget,
    )
    sv, verified_crc, verified_len = verify_packed_bin(dest_bin, crc_mode=target.crc_mode)
    assert sv == target.expected_sv and verified_crc == crc and verified_len == flen

    shutil.copy2(raw_elf, dest_elf)
    shutil.copy2(raw_dict, dest_dict)

    validate_elf_and_dict(target, dest_elf, dest_bin, dest_dict, env)
    margin = target.flash_budget - flen
    print(
        f"  [{target.name}] OK: {dest_bin.name} ({flen} bytes, "
        f"{margin} bytes free of {target.flash_budget}), sv={sv!r}, crc=0x{crc:04x}"
    )
    return {
        "target": target.name,
        "mcu": target.expected_mcu,
        "bin": str(dest_bin),
        "dict": str(dest_dict),
        "elf": str(dest_elf),
        "size_bytes": flen,
        "flash_margin_bytes": margin,
        "crc16": f"0x{crc:04x}",
        "sv": sv,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        choices=("all", "main", "nozzle"),
        default="all",
        help="Select which MCU target(s) to build",
    )
    parser.add_argument("-j", "--jobs", type=int, default=max(1, os.cpu_count() or 4))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    manifest = load_upstream_manifest()
    build_root = get_build_root(manifest)
    tarball = ensure_klipper_tarball(build_root / "cache", manifest, allow_download=True)
    clean_klipper = extract_klipper(tarball, build_root / "work", manifest, force=False)

    gcc_path, env = resolve_arm_toolchain()
    gcc_ver = subprocess.check_output([gcc_path, "--version"], text=True).splitlines()[0]
    print(f"Using toolchain: {gcc_path} ({gcc_ver})", flush=True)

    stage_dir = build_root / "work" / "mcu-stage"
    prepare_mcu_stage(clean_klipper, stage_dir)

    out_mcu_dir = build_root / "out" / "mcu"
    out_mcu_dir.mkdir(parents=True, exist_ok=True)

    selected = list(TARGETS.values()) if args.target == "all" else [TARGETS[args.target]]
    summaries = [
        build_target(t, stage_dir, out_mcu_dir, env, jobs=args.jobs, verbose=args.verbose)
        for t in selected
    ]
    print(json.dumps(summaries, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
