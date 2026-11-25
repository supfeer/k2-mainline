"""Assemble the clean upstream Klipper v0.14.0 host tree plus K2 modules outside the repo."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
from pathlib import Path

from common import (
    get_build_root,
    load_upstream_manifest,
    recreate_dir,
    sha256_file,
    verify_extracted_klipper,
)

K2_MAINLINE_ROOT = Path(__file__).resolve().parent.parent
HOST_SRC_DIR = K2_MAINLINE_ROOT / "host"
VERSION_STRING = "mainline-v0.14.0-k2\n"

K2_ADAPTERS = {
    "extras/auto_addr.py",
    "extras/k2.py",
    "extras/k2_cancel.py",
    "extras/k2_display.py",
    "extras/k2_mesh.py",
    "extras/k2_native.py",
    "extras/k2_print.py",
    "extras/k2_transport.py",
    "extras/motor_control.py",
    "extras/z_align.py",
    "kinematics/k2_corexy.py",
}

K2_DRIVERS = {
    "extras/box_wrapper.py",
    "extras/fan_feedback.py",
    "extras/filament_rack_wrapper.py",
    "extras/motor_control_wrapper.py",
    "extras/serial_485_wrapper.py",
}

K2_STOCK_EXTRAS = {
    "extras/auto_addr_wrapper.py",
    "extras/base_info.py",
    "extras/belt_mdl.py",
    "extras/bl24c16f.py",
    "extras/box.py",
    "extras/custom_macro.py",
    "extras/filament_rack.py",
    "extras/load_ai.py",
    "extras/metadata.py",
    "extras/photograph.py",
    "extras/serial_485.py",
}


def _copy_clean_upstream_tree(src: Path, dst: Path) -> dict[str, str]:
    upstream_files: dict[str, str] = {}
    recreate_dir(dst)
    for item in sorted(src.rglob("*")):
        rel = item.relative_to(src)
        if "__pycache__" in rel.parts or item.suffix in (".pyc", ".pyo"):
            continue
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not item.is_file() or item.is_symlink():
            raise RuntimeError(f"Unexpected non-regular file in upstream klippy: {item}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        mode = 0o755 if (item.stat().st_mode & 0o111) else 0o644
        target.chmod(mode)
        upstream_files[rel.as_posix()] = sha256_file(target)
    return upstream_files


def _classify_k2_rel(rel_posix: str) -> str:
    if rel_posix in K2_ADAPTERS:
        return "k2_adapter"
    if rel_posix in K2_DRIVERS:
        return "k2_driver"
    if rel_posix in K2_STOCK_EXTRAS:
        return "k2_stock_extra"
    raise RuntimeError(f"Unclassified K2 host file: {rel_posix}")


def assemble_host() -> Path:
    manifest = load_upstream_manifest()
    build_root = get_build_root(manifest)
    klipper_stamp = verify_extracted_klipper(build_root / "work", build_root / "cache", manifest)
    clean_klippy = build_root / "work" / manifest["klipper"]["extract_dir"] / "klippy"
    if not (clean_klippy / "klippy.py").is_file():
        raise FileNotFoundError(
            f"Missing extracted upstream klippy tree ({clean_klippy}); run fetch_sources.py --extract"
        )

    out_native = build_root / "out" / "native"
    c_helper = out_native / "c_helper.so"
    k2_hardware = out_native / "k2_hardware.so"
    for req in (c_helper, k2_hardware):
        if not req.is_file():
            raise FileNotFoundError(f"Missing native artifact {req}; run build_native.py first")

    out_host = build_root / "out" / "host"
    out_host.mkdir(parents=True, exist_ok=True)
    dst_klippy = out_host / "klippy"

    upstream_files = _copy_clean_upstream_tree(clean_klippy, dst_klippy)
    upstream_casefold = {rel.casefold(): rel for rel in upstream_files}

    inventory_files: dict[str, dict[str, object]] = {}
    for rel, digest in sorted(upstream_files.items()):
        mode = stat.S_IMODE((dst_klippy / rel).stat().st_mode)
        inventory_files[rel] = {"origin": "upstream", "sha256": digest, "mode": mode}

    # Install compiled ARMv7 shared libraries without overwriting upstream Python sources.
    native_targets = [
        (c_helper, "chelper/c_helper.so", "upstream_native"),
        (k2_hardware, "extras/k2_hardware.so", "k2_native"),
    ]
    for src_bin, rel_posix, origin in native_targets:
        if rel_posix.casefold() in upstream_casefold:
            raise RuntimeError(f"Native target collides with upstream file: {rel_posix}")
        dest = dst_klippy / rel_posix
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_bin, dest)
        dest.chmod(0o755)
        inventory_files[rel_posix] = {
            "origin": origin,
            "sha256": sha256_file(dest),
            "mode": 0o755,
        }

    # Install K2 host adapters, ported drivers, and stock pure-Python extras.
    expected_k2 = K2_ADAPTERS | K2_DRIVERS | K2_STOCK_EXTRAS
    seen_k2: set[str] = set()
    for subdir in ("extras", "kinematics"):
        src_dir = HOST_SRC_DIR / subdir
        if not src_dir.is_dir():
            raise FileNotFoundError(f"Missing K2 host source directory: {src_dir}")
        for item in sorted(src_dir.iterdir()):
            if not item.is_file() or item.suffix != ".py":
                raise RuntimeError(f"Unexpected entry in {src_dir}: {item}")
            rel_posix = f"{subdir}/{item.name}"
            if rel_posix.casefold() in upstream_casefold:
                raise RuntimeError(
                    f"K2 host file shadows upstream file: {rel_posix} vs {upstream_casefold[rel_posix.casefold()]}"
                )
            origin = _classify_k2_rel(rel_posix)
            dest = dst_klippy / rel_posix
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest)
            dest.chmod(0o644)
            inventory_files[rel_posix] = {
                "origin": origin,
                "sha256": sha256_file(dest),
                "mode": 0o644,
            }
            seen_k2.add(rel_posix)

    if seen_k2 != expected_k2:
        missing = sorted(expected_k2 - seen_k2)
        extra = sorted(seen_k2 - expected_k2)
        raise RuntimeError(f"K2 host module set mismatch: missing={missing}, extra={extra}")

    version_rel = ".version"
    if version_rel.casefold() in upstream_casefold:
        raise RuntimeError(".version exists unexpectedly in upstream klippy tarball")
    version_path = dst_klippy / version_rel
    version_path.write_text(VERSION_STRING, encoding="ascii")
    version_path.chmod(0o644)
    inventory_files[version_rel] = {
        "origin": "version",
        "sha256": sha256_file(version_path),
        "mode": 0o644,
    }

    # Independent verification pass: 0 modified or shadowed upstream files.
    modified_upstream: list[str] = []
    for rel, expected_sha in sorted(upstream_files.items()):
        actual_path = dst_klippy / rel
        if not actual_path.is_file() or sha256_file(actual_path) != expected_sha:
            modified_upstream.append(rel)
    if modified_upstream:
        raise RuntimeError(f"Upstream files modified after assembly: {modified_upstream}")

    # Verify exact file set in dst_klippy matches inventory_files.
    actual_rel_files = {
        p.relative_to(dst_klippy).as_posix()
        for p in dst_klippy.rglob("*")
        if p.is_file()
    }
    if actual_rel_files != set(inventory_files):
        raise RuntimeError(
            f"Assembled file set mismatch: diff={sorted(actual_rel_files ^ set(inventory_files))}"
        )

    origin_counts: dict[str, int] = {}
    for entry in inventory_files.values():
        org = str(entry["origin"])
        origin_counts[org] = origin_counts.get(org, 0) + 1

    inventory = {
        "klipper_tag": manifest["klipper"]["tag"],
        "klipper_commit": manifest["klipper"]["commit"],
        "klipper_tarball_sha256": klipper_stamp["tarball_sha256"],
        "klippy_source_tree_sha256": klipper_stamp["klippy_tree_sha256"],
        "version_string": VERSION_STRING.strip(),
        "core_policy": "unmodified",
        "modified_upstream_files": 0,
        "upstream_shadow_collisions": 0,
        "origin_counts": origin_counts,
        "total_files": len(inventory_files),
        "files": inventory_files,
    }
    inv_path = out_host / "inventory.json"
    inv_path.write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    print(f"Assembled host klippy tree: {out_host} ({len(inventory_files)} files, 0 upstream edits)")
    return inv_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    assemble_host()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
