#!/usr/bin/env python3
"""Run ARMv7 Python 3.9 verification on assembled k2_mainline host and config."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_native import BUILD_IMAGE, DOCKER_PLATFORM, docker_env, ensure_build_image  # noqa: E402
from common import (  # noqa: E402
    K2_MAINLINE_ROOT,
    ensure_stock_payload,
    get_build_root,
    load_upstream_manifest,
    run_checked,
    sha256_file,
)

OTA_TOOLS_IMAGE = "k2-mainline-ota-tools:alpine"
OTA_TOOLS_PLATFORM = "linux/arm64"


def ensure_ota_tools_image(env: dict[str, str], *, rebuild: bool = False) -> None:
    """Build or reuse the Alpine SquashFS tooling image."""
    if not rebuild:
        probe = subprocess.run(
            ["docker", "image", "inspect", OTA_TOOLS_IMAGE],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if probe.returncode == 0:
            return
    dockerfile = K2_MAINLINE_ROOT / "docker" / "ota-tools.Dockerfile"
    run_checked(
        [
            "docker",
            "build",
            "--platform",
            OTA_TOOLS_PLATFORM,
            "--pull=false",
            "-t",
            OTA_TOOLS_IMAGE,
            "-f",
            str(dockerfile),
            str(K2_MAINLINE_ROOT / "docker"),
        ],
        env=env,
    )


def ensure_sanitized_stock_tar(
    env: dict[str, str],
    build_root: Path,
    rootfs_img: Path,
) -> Path:
    """Cache sanitized stock rootfs tarball in work/ for clean Linux container chroot."""
    work_dir = build_root / "work"
    tar_path = work_dir / "stock-chroot-base.tar"
    stamp = work_dir / "stock-chroot-base.json"
    rootfs_sha = sha256_file(rootfs_img)

    if tar_path.is_file() and stamp.is_file():
        try:
            meta = json.loads(stamp.read_text(encoding="utf-8"))
            if meta.get("rootfs_sha256") == rootfs_sha and sha256_file(tar_path) == meta.get("tar_sha256"):
                return tar_path
        except (ValueError, OSError):
            pass

    if tar_path.exists():
        tar_path.unlink()
    if stamp.exists():
        stamp.unlink()

    unpack_script = """
import os, shutil, stat, subprocess, tarfile
from pathlib import Path

root = Path('/tmp/stock-root')
if root.exists():
    shutil.rmtree(root)
subprocess.run(['unsquashfs', '-no-progress', '-d', str(root), '/stock/rootfs'], check=True)
safe_chr = {'dev/null': (1, 3), 'dev/zero': (1, 5), 'dev/full': (1, 7), 'dev/random': (1, 8), 'dev/urandom': (1, 9)}
for p in list(root.rglob('*')):
    try:
        st = p.lstat()
    except FileNotFoundError:
        continue
    rel = p.relative_to(root).as_posix()
    if stat.S_ISBLK(st.st_mode) or (stat.S_ISCHR(st.st_mode) and safe_chr.get(rel) != (os.major(st.st_rdev), os.minor(st.st_rdev))):
        p.unlink()
tmp_tar = Path('/work/.stock-chroot-base.tar.tmp')
with tarfile.open(tmp_tar, 'w') as tf:
    tf.add(root, arcname='.')
os.replace(tmp_tar, '/work/stock-chroot-base.tar')
shutil.rmtree(root)
"""
    run_checked(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--platform",
            OTA_TOOLS_PLATFORM,
            "-v",
            f"{rootfs_img.parent}:/stock:ro",
            "-v",
            f"{work_dir}:/work",
            OTA_TOOLS_IMAGE,
            "python3",
            "-B",
            "-c",
            unpack_script,
        ],
        env=env,
    )
    stamp.write_text(
        json.dumps(
            {"rootfs_sha256": rootfs_sha, "tar_sha256": sha256_file(tar_path)},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return tar_path


def compute_host_input_hashes(build_root: Path) -> dict[str, object]:
    """Return deterministic SHA-256 snapshot of all host/config/MCU/native/services inputs."""
    out_host = build_root / "out" / "host"
    out_mcu = build_root / "out" / "mcu"
    out_native = build_root / "out" / "native"
    out_services = build_root / "out" / "services"
    cfg_dir = K2_MAINLINE_ROOT / "config"
    svc_src_dir = K2_MAINLINE_ROOT / "services"
    return {
        "inventory_sha256": sha256_file(out_host / "inventory.json"),
        "configs": {
            p.name: sha256_file(p)
            for p in sorted(cfg_dir.iterdir())
            if p.is_file() and (p.suffix == ".cfg" or p.name == "k2_sync_config.py")
        },
        "mcu": {
            p.name: sha256_file(p)
            for p in sorted(out_mcu.iterdir())
            if p.is_file() and p.suffix in (".bin", ".dict")
        },
        "native": {
            p.name: sha256_file(p)
            for p in sorted(out_native.iterdir())
            if p.is_file() and (p.suffix == ".so" or p.name == "klipper_mcu")
        },
        "services": {
            "build_services_report.json": sha256_file(out_services / "build_services_report.json"),
            **{
                p.name: sha256_file(p)
                for p in sorted(svc_src_dir.iterdir())
                if p.is_file()
            },
        },
    }


def verify_host_arm() -> Path:
    manifest = load_upstream_manifest()
    build_root = get_build_root(manifest)
    out_host = build_root / "out" / "host"
    klippy_dir = out_host / "klippy"
    inv_path = out_host / "inventory.json"
    out_mcu = build_root / "out" / "mcu"
    out_native = build_root / "out" / "native"
    out_services = build_root / "out" / "services"

    if not (klippy_dir / "klippy.py").is_file() or not inv_path.is_file():
        raise FileNotFoundError(
            f"Missing assembled host tree in {out_host}; run assemble_host.py first"
        )
    if not (out_mcu / "main_klipper.dict").is_file() or not (out_mcu / "nozzle_klipper.dict").is_file():
        raise FileNotFoundError(
            f"Missing MCU dictionaries in {out_mcu}; run build_mcu.py first"
        )
    if not (out_native / "libk2_ota_shim.so").is_file():
        raise FileNotFoundError(
            f"Missing native OTA shim in {out_native}; run build_native.py first"
        )
    if not (out_services / "build_services_report.json").is_file():
        raise FileNotFoundError(
            f"Missing services build report in {out_services}; run build_services.py first"
        )

    input_hashes = compute_host_input_hashes(build_root)
    payload_dir = ensure_stock_payload(build_root, manifest)
    report_path = out_host / "verify_host_arm_report.json"
    if report_path.exists():
        report_path.unlink()

    with docker_env() as env:
        ensure_ota_tools_image(env)
        ensure_build_image(env, rebuild=False)
        chroot_tar = ensure_sanitized_stock_tar(env, build_root, payload_dir / "rootfs")
        run_checked(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--platform",
                DOCKER_PLATFORM,
                "-v",
                f"{K2_MAINLINE_ROOT}:/src/k2_mainline:ro",
                "-v",
                f"{klippy_dir}:/src/klippy:ro",
                "-v",
                f"{out_mcu}:/src/mcu:ro",
                "-v",
                f"{out_native}:/src/native:ro",
                "-v",
                f"{out_services}:/src/services:ro",
                "-v",
                f"{chroot_tar}:/src/stock-chroot.tar:ro",
                "-v",
                f"{out_host}:/out",
                BUILD_IMAGE,
                "python3",
                "-B",
                "/src/k2_mainline/scripts/_verify_host_container.py",
            ],
            env=env,
        )

    if not report_path.is_file():
        raise FileNotFoundError(f"Expected host verification report missing: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not report.get("passed"):
        raise RuntimeError(f"ARMv7 host verification failed: {report}")
    report["input_hashes"] = input_hashes
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        f"ARMv7 verification PASSED (Buster {report['python_version']} + "
        f"K2 target {report['target_runtime']['python_version']}) -> {report_path}"
    )
    return report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    verify_host_arm()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
