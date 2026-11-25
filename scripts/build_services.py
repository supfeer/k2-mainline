#!/usr/bin/env python3
"""Assemble verified Moonraker v0.10.0, ARMv7 Python deps, Fluidd, Mainsail, and static curl."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import struct
import subprocess
import tarfile
import zipfile
from pathlib import Path

from build_native import BUILD_IMAGE, DOCKER_PLATFORM, docker_env, ensure_build_image
from common import (
    K2_MAINLINE_ROOT,
    compute_tree_digest,
    ensure_stock_payload,
    get_build_root,
    load_upstream_manifest,
    recreate_dir,
    run_checked,
    sha256_file,
    verify_extracted_moonraker,
)
from fetch_sources import (
    ensure_fluidd_zip,
    ensure_go2rtc_binary,
    ensure_mainsail_zip,
    ensure_moonraker_tarball,
    ensure_static_curl_archive,
    extract_moonraker,
)
from verify_host_arm import ensure_ota_tools_image, ensure_sanitized_stock_tar

SERVICES_SRC_DIR = K2_MAINLINE_ROOT / "services"
MOONRAKER_VERSION_STRING = 'v0.10.0-k2'


def _copy_clean_tree(src: Path, dst: Path) -> dict[str, str]:
    files: dict[str, str] = {}
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
            raise RuntimeError(f"Unexpected non-regular file in upstream tree: {item}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        mode = 0o755 if (item.stat().st_mode & 0o111) else 0o644
        target.chmod(mode)
        files[rel.as_posix()] = sha256_file(target)
    return files


def assemble_moonraker(build_root: Path, manifest: dict) -> dict[str, object]:
    """Assemble clean Moonraker v0.10.0 tree plus OpenWrt provider with 0 upstream edits."""
    work_dir = build_root / "work"
    moon_stamp = verify_extracted_moonraker(work_dir, build_root / "cache", manifest)
    clean_pkg = work_dir / manifest["moonraker"]["extract_dir"] / "moonraker"
    if not (clean_pkg / "server.py").is_file():
        raise FileNotFoundError(f"Missing extracted Moonraker package at {clean_pkg}")

    out_moon = build_root / "out" / "services" / "moonraker"
    recreate_dir(out_moon)
    dst_pkg = out_moon / "moonraker"
    upstream_rel = _copy_clean_tree(clean_pkg, dst_pkg)

    inventory_files: dict[str, dict[str, object]] = {}
    upstream_casefold: dict[str, str] = {}
    for rel, digest in sorted(upstream_rel.items()):
        full_rel = f"moonraker/{rel}"
        upstream_casefold[full_rel.casefold()] = full_rel
        mode = stat.S_IMODE((dst_pkg / rel).stat().st_mode)
        inventory_files[full_rel] = {
            "origin": "upstream",
            "sha256": digest,
            "mode": mode,
        }

    # Add read-only package version metadata & OpenWrt procd component without touching upstream files.
    ver_rel = "moonraker/__version__.py"
    if ver_rel.casefold() in upstream_casefold:
        raise RuntimeError(f"File collides with upstream Moonraker tree: {ver_rel}")
    ver_dst = out_moon / ver_rel
    ver_dst.write_text(f'__version__ = "{MOONRAKER_VERSION_STRING}"\n', encoding="ascii")
    ver_dst.chmod(0o644)
    inventory_files[ver_rel] = {
        "origin": "k2_version",
        "sha256": sha256_file(ver_dst),
        "mode": 0o644,
    }

    provider_rel = "moonraker/components/k2_openwrt.py"
    if provider_rel.casefold() in upstream_casefold:
        raise RuntimeError(f"File collides with upstream Moonraker tree: {provider_rel}")
    provider_src = SERVICES_SRC_DIR / "k2_openwrt.py"
    provider_dst = out_moon / provider_rel
    shutil.copy2(provider_src, provider_dst)
    provider_dst.chmod(0o644)
    inventory_files[provider_rel] = {
        "origin": "k2_openwrt_provider",
        "sha256": sha256_file(provider_dst),
        "mode": 0o644,
    }

    for tmpl_name in ("moonraker.conf", "moonraker.asvc"):
        if tmpl_name.casefold() in upstream_casefold:
            raise RuntimeError(f"Template collides with upstream Moonraker tree: {tmpl_name}")
        src = SERVICES_SRC_DIR / tmpl_name
        dst = out_moon / tmpl_name
        shutil.copy2(src, dst)
        dst.chmod(0o644)
        inventory_files[tmpl_name] = {
            "origin": "k2_default_config",
            "sha256": sha256_file(dst),
            "mode": 0o644,
        }

    # Verify 0 modified upstream files and exact inventory coverage.
    for rel, expected_sha in sorted(upstream_rel.items()):
        actual = dst_pkg / rel
        if not actual.is_file() or sha256_file(actual) != expected_sha:
            raise RuntimeError(f"Upstream Moonraker file modified after assembly: {rel}")

    actual_files = {
        p.relative_to(out_moon).as_posix()
        for p in out_moon.rglob("*")
        if p.is_file()
    }
    if actual_files != set(inventory_files):
        raise RuntimeError(
            f"Moonraker inventory mismatch: {sorted(actual_files ^ set(inventory_files))}"
        )

    inventory = {
        "moonraker_tag": manifest["moonraker"]["tag"],
        "moonraker_commit": manifest["moonraker"]["commit"],
        "moonraker_tarball_sha256": moon_stamp["tarball_sha256"],
        "moonraker_source_tree_sha256": moon_stamp["moonraker_tree_sha256"],
        "version_string": MOONRAKER_VERSION_STRING,
        "core_policy": "unmodified",
        "modified_upstream_files": 0,
        "upstream_shadow_collisions": 0,
        "total_files": len(inventory_files),
        "files": inventory_files,
    }
    inv_path = out_moon / "inventory.json"
    inv_path.write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    return inventory


def extract_webui_zip(zip_path: Path, dst_dir: Path, expected_project: str, expected_version: str) -> dict[str, object]:
    """Safely extract Fluidd/Mainsail release zip and validate release_info.json."""
    recreate_dir(dst_dir)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            rel = info.filename.lstrip("./")
            if not rel:
                continue
            parts = Path(rel).parts
            if Path(rel).is_absolute() or ".." in parts:
                raise RuntimeError(f"Unsafe zip member path in {zip_path.name}: {info.filename!r}")
            unix_mode = (info.external_attr >> 16) & 0o170000
            if unix_mode == stat.S_IFLNK:
                raise RuntimeError(f"Refusing symlink in web UI zip {zip_path.name}: {info.filename!r}")
            out_path = dst_dir / rel
            if info.is_dir():
                out_path.mkdir(parents=True, exist_ok=True)
                out_path.chmod(0o755)
                continue
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src_fh, out_path.open("wb") as dst_fh:
                shutil.copyfileobj(src_fh, dst_fh)
            out_path.chmod(0o644)

    index_path = dst_dir / "index.html"
    rel_info_path = dst_dir / "release_info.json"
    if not index_path.is_file() or not rel_info_path.is_file():
        raise RuntimeError(f"Web UI archive {zip_path.name} missing index.html or release_info.json")
    rel_info = json.loads(rel_info_path.read_text(encoding="utf-8"))
    if rel_info.get("project_name") != expected_project or rel_info.get("version") != expected_version:
        raise RuntimeError(
            f"Unexpected release_info.json in {zip_path.name}: {rel_info!r} "
            f"(expected {expected_project} {expected_version})"
        )
    digest, file_count = compute_tree_digest(dst_dir)
    return {
        "project_name": expected_project,
        "version": expected_version,
        "file_count": file_count,
        "tree_sha256": digest,
    }


def _verify_and_write_static_armv7_elf(
    data: bytes, dst_bin: Path, label: str
) -> dict[str, object]:
    """Assert static ELF32 little-endian ARM layout and atomically write executable binary."""
    if len(data) < 52 or data[:4] != b"\x7fELF":
        raise RuntimeError(f"{label} ({dst_bin}) is not a valid ELF file")
    ei_class, ei_data = data[4], data[5]
    e_machine = struct.unpack_from("<H", data, 18)[0]
    if (ei_class, ei_data, e_machine) != (1, 1, 40):
        raise RuntimeError(
            f"Unexpected {label} ELF header {(ei_class, ei_data, e_machine)} != (1, 1, 40)"
        )
    e_phoff = struct.unpack_from("<I", data, 28)[0]
    e_phentsize, e_phnum = struct.unpack_from("<HH", data, 42)
    if e_phentsize != 32 or e_phoff + e_phnum * e_phentsize > len(data):
        raise RuntimeError(f"Invalid {label} program header table")
    for offset in range(e_phoff, e_phoff + e_phnum * e_phentsize, e_phentsize):
        segment_type, start, _, _, size = struct.unpack_from("<IIIII", data, offset)
        if segment_type == 3:  # PT_INTERP
            raise RuntimeError(f"Static {label} must not request an ELF interpreter")
        if segment_type == 2:  # PT_DYNAMIC is legal for static PIE relocations.
            if size % 8 or start + size > len(data):
                raise RuntimeError(f"Invalid {label} dynamic table")
            for entry in range(start, start + size, 8):
                tag = struct.unpack_from("<I", data, entry)[0]
                if tag == 0:  # DT_NULL
                    break
                if tag == 1:  # DT_NEEDED
                    raise RuntimeError(f"Static {label} must not depend on a shared library")

    dst_bin.parent.mkdir(parents=True, exist_ok=True)
    tmp_bin = dst_bin.with_name(f".{dst_bin.name}.tmp")
    tmp_bin.write_bytes(data)
    tmp_bin.chmod(0o755)
    os.replace(tmp_bin, dst_bin)

    return {
        "sha256": sha256_file(dst_bin),
        "bytes": len(data),
        "elf_header": [ei_class, ei_data, e_machine],
        "static": True,
    }


def extract_and_verify_static_curl(arc_path: Path, dst_bin: Path) -> dict[str, object]:
    """Safely extract static ARMv7 curl binary and assert static ELF32 little-endian ARM layout."""
    found_bytes: bytes | None = None
    with tarfile.open(arc_path, "r:xz") as tf:
        for member in tf.getmembers():
            rel = member.name.lstrip("./")
            parts = Path(rel).parts
            if Path(rel).is_absolute() or ".." in parts:
                raise RuntimeError(f"Unsafe tar member in {arc_path.name}: {member.name!r}")
            if member.isfile() and Path(rel).name == "curl":
                if found_bytes is not None:
                    raise RuntimeError(f"Multiple curl binaries inside {arc_path.name}")
                fh = tf.extractfile(member)
                if fh is None:
                    raise RuntimeError(f"Unable to extract {member.name} from {arc_path.name}")
                with fh:
                    found_bytes = fh.read()
    if not found_bytes:
        raise RuntimeError(f"No curl binary found inside {arc_path.name}")

    return _verify_and_write_static_armv7_elf(found_bytes, dst_bin, "curl")


def stage_and_verify_go2rtc(src_bin: Path, dst_bin: Path) -> dict[str, object]:
    """Verify static ARMv7 go2rtc binary and stage executable into out/services/bin/go2rtc."""
    return _verify_and_write_static_armv7_elf(src_bin.read_bytes(), dst_bin, "go2rtc")


def build_moonraker_deps_in_container(build_root: Path, manifest: dict, out_services: Path) -> dict[str, object]:
    """Install/cache ARMv7 Python 3.9 wheels & verify ELF ABI/shared libs against K2 stock rootfs."""
    reqs_path = SERVICES_SRC_DIR / "moonraker-requirements-py39.txt"
    reqs_sha = sha256_file(reqs_path)
    builder = K2_MAINLINE_ROOT / "scripts" / "_services_container_build.py"
    builder_sha = sha256_file(builder)
    report_path = out_services / "moonraker_env_report.json"
    site_dir = out_services / "moonraker-site-packages"
    if report_path.is_file() and site_dir.is_dir():
        try:
            existing = json.loads(report_path.read_text(encoding="utf-8"))
            if (
                existing.get("passed")
                and existing.get("requirements_sha256") == reqs_sha
                and existing.get("builder_sha256") == builder_sha
            ):
                print(f"Using cached ARMv7 Moonraker site-packages ({existing['file_count']} files)", flush=True)
                return existing
        except (ValueError, OSError):
            pass

    payload_dir = ensure_stock_payload(build_root, manifest)
    wheel_cache = build_root / "cache" / "pip-wheels"
    wheel_cache.mkdir(parents=True, exist_ok=True)
    with docker_env() as env:
        ensure_ota_tools_image(env)
        ensure_build_image(env, rebuild=False)
        chroot_tar = ensure_sanitized_stock_tar(env, build_root, payload_dir / "rootfs")
        run_checked(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                DOCKER_PLATFORM,
                "-v",
                f"{SERVICES_SRC_DIR}:/src/services:ro",
                "-v",
                f"{K2_MAINLINE_ROOT / 'scripts'}:/src/scripts:ro",
                "-v",
                f"{chroot_tar}:/src/stock-chroot.tar:ro",
                "-v",
                f"{wheel_cache}:/cache/pip-wheels",
                "-v",
                f"{out_services}:/out",
                BUILD_IMAGE,
                "python3",
                "-B",
                "/src/scripts/_services_container_build.py",
            ],
            env=env,
        )
    if not report_path.is_file():
        raise FileNotFoundError(f"Missing container report: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not report.get("passed"):
        raise RuntimeError(f"Moonraker ARMv7 dependency build failed: {report}")
    return report


def build_services() -> Path:
    manifest = load_upstream_manifest()
    build_root = get_build_root(manifest)
    cache_dir = build_root / "cache"
    work_dir = build_root / "work"

    moon_tarball = ensure_moonraker_tarball(cache_dir, manifest, allow_download=True)
    extract_moonraker(moon_tarball, work_dir, manifest, force=False)
    fluidd_zip = ensure_fluidd_zip(cache_dir, manifest, allow_download=True)
    mainsail_zip = ensure_mainsail_zip(cache_dir, manifest, allow_download=True)
    curl_arc = ensure_static_curl_archive(cache_dir, manifest, allow_download=True)
    go2rtc_bin = ensure_go2rtc_binary(cache_dir, manifest, allow_download=True)

    out_services = build_root / "out" / "services"
    out_services.mkdir(parents=True, exist_ok=True)

    moon_inv = assemble_moonraker(build_root, manifest)
    env_report = build_moonraker_deps_in_container(build_root, manifest, out_services)
    fluidd_info = extract_webui_zip(
        fluidd_zip, out_services / "fluidd", "fluidd", manifest["fluidd"]["tag"]
    )
    mainsail_info = extract_webui_zip(
        mainsail_zip, out_services / "mainsail", "mainsail", manifest["mainsail"]["tag"]
    )
    curl_info = extract_and_verify_static_curl(curl_arc, out_services / "bin" / "curl")
    go2rtc_info = stage_and_verify_go2rtc(go2rtc_bin, out_services / "bin" / "go2rtc")

    site_digest, site_count = compute_tree_digest(out_services / "moonraker-site-packages")
    report = {
        "passed": True,
        "moonraker": {
            "version": moon_inv["version_string"],
            "inventory_sha256": sha256_file(out_services / "moonraker" / "inventory.json"),
            "modified_upstream_files": moon_inv["modified_upstream_files"],
            "total_files": moon_inv["total_files"],
        },
        "moonraker_env": {
            "requirements_sha256": env_report["requirements_sha256"],
            "file_count": site_count,
            "tree_sha256": site_digest,
            "extension_modules": env_report["extension_modules"],
            "imported_versions": env_report["imported_versions"],
        },
        "fluidd": fluidd_info,
        "mainsail": mainsail_info,
        "static_curl": curl_info,
        "go2rtc": go2rtc_info,
        "nginx_conf_sha256": sha256_file(SERVICES_SRC_DIR / "nginx.conf"),
        "go2rtc_yaml_sha256": sha256_file(SERVICES_SRC_DIR / "go2rtc.yaml"),
        "init_go2rtc_sha256": sha256_file(SERVICES_SRC_DIR / "init_go2rtc"),
        "k2_h264_source_sha256": sha256_file(SERVICES_SRC_DIR / "k2_h264_source"),
    }
    report_path = out_services / "build_services_report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Services build OK -> {out_services}", flush=True)
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    build_services()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
