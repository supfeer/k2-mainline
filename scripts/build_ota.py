#!/usr/bin/env python3
"""Build and verify the single-.img K2 Plus upstream Klipper v0.14.0 SWUpdate OTA package."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_native import BUILD_IMAGE, DOCKER_PLATFORM, docker_env, ensure_build_image  # noqa: E402
from common import (  # noqa: E402
    K2_MAINLINE_ROOT,
    ensure_stock_payload,
    get_build_root,
    load_upstream_manifest,
    recreate_dir,
    run_checked,
    sha256_file,
)
from verify_host_arm import (  # noqa: E402
    OTA_TOOLS_IMAGE,
    OTA_TOOLS_PLATFORM,
    compute_host_input_hashes,
    ensure_ota_tools_image,
    ensure_sanitized_stock_tar,
)

sys.path.insert(0, str(K2_MAINLINE_ROOT / "ota"))
from cpio_crc import mapped_cpio_crc  # noqa: E402

MAIN_FW_BIN = "mcu0_140_G32-mcu0_022_950.bin"
NOZZLE_FW_BIN = "noz0_130_G30-noz0_021_950.bin"
DEFAULT_OTA_VERSION = "1.1.4.950"
DEFAULT_ROLLBACK_VERSION = "1.1.4.951"


def _publish_atomic(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.tmp")
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise


def build_ota(
    *,
    version: str = DEFAULT_OTA_VERSION,
    with_rollback: bool = False,
) -> Path:
    manifest = load_upstream_manifest()
    build_root = get_build_root(manifest)

    out_host = build_root / "out" / "host"
    out_native = build_root / "out" / "native"
    out_mcu = build_root / "out" / "mcu"
    out_services = build_root / "out" / "services"
    out_ota = build_root / "out" / "ota"
    out_ota.mkdir(parents=True, exist_ok=True)

    # Verify prerequisites and pin input hashes from verify_host_arm_report.json.
    host_klippy = out_host / "klippy"
    inv_path = out_host / "inventory.json"
    host_verify_path = out_host / "verify_host_arm_report.json"
    svc_report_path = out_services / "build_services_report.json"
    svc_moon_inv = out_services / "moonraker" / "inventory.json"
    svc_curl = out_services / "bin" / "curl"
    svc_go2rtc = out_services / "bin" / "go2rtc"
    if not (host_klippy / "klippy.py").is_file() or not inv_path.is_file():
        raise FileNotFoundError(f"Missing assembled host tree in {out_host}; run assemble_host.py")
    if not host_verify_path.is_file():
        raise FileNotFoundError(f"Missing host verification report {host_verify_path}; run verify_host_arm.py")
    if (
        not svc_report_path.is_file()
        or not svc_moon_inv.is_file()
        or not svc_curl.is_file()
        or not svc_go2rtc.is_file()
    ):
        raise FileNotFoundError(f"Missing services artifacts in {out_services}; run build_services.py")
    svc_report = json.loads(svc_report_path.read_text(encoding="utf-8"))
    if not svc_report.get("passed"):
        raise RuntimeError("Refusing OTA build: build_services_report.json did not pass")
    host_verify = json.loads(host_verify_path.read_text(encoding="utf-8"))
    if not host_verify.get("passed"):
        raise RuntimeError("Refusing OTA build: verify_host_arm_report.json did not pass")

    klipper_mcu = out_native / "klipper_mcu"
    main_fw = out_mcu / MAIN_FW_BIN
    nozzle_fw = out_mcu / NOZZLE_FW_BIN
    for req in (klipper_mcu, main_fw, nozzle_fw):
        if not req.is_file():
            raise FileNotFoundError(f"Missing build artifact {req}")

    actual_inputs = compute_host_input_hashes(build_root)
    if host_verify.get("input_hashes") != actual_inputs:
        raise RuntimeError(
            "Host/config/MCU/native inputs changed after verify_host_arm.py; re-run verify_host_arm.py"
        )

    payload_dir = ensure_stock_payload(build_root, manifest)
    ota_stage = build_root / "work" / "ota-stage"
    recreate_dir(ota_stage)

    with docker_env() as env:
        ensure_ota_tools_image(env)
        ensure_build_image(env, rebuild=False)
        chroot_tar = ensure_sanitized_stock_tar(env, build_root, payload_dir / "rootfs")

        # Stage 1: Unpack stock rootfs, apply strict allowlist, pack SquashFS + CRC CPIO .img into ota-stage.
        pack_cmd = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--platform",
            OTA_TOOLS_PLATFORM,
            "-v",
            f"{K2_MAINLINE_ROOT}:/src/k2_mainline:ro",
            "-v",
            f"{payload_dir}:/stock:ro",
            "-v",
            f"{out_host}:/inputs/host_out:ro",
            "-v",
            f"{out_native}:/inputs/native:ro",
            "-v",
            f"{out_mcu}:/inputs/mcu:ro",
            "-v",
            f"{out_services}:/inputs/services:ro",
            "-v",
            f"{ota_stage}:/stage",
            OTA_TOOLS_IMAGE,
            "python3",
            "-B",
            "/src/k2_mainline/scripts/_ota_container_pack.py",
            "--version",
            version,
        ]
        if with_rollback:
            pack_cmd.extend(["--rollback-version", DEFAULT_ROLLBACK_VERSION])
        run_checked(pack_cmd, env=env)

        # Stage 2: Verify staged .img using real Creality ARMv7 /sbin/swupdate & shell checks BEFORE publishing.
        verify_cmd = [
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
            f"{chroot_tar}:/src/stock-chroot.tar:ro",
            "-v",
            f"{ota_stage}:/stage",
            BUILD_IMAGE,
            "python3",
            "-B",
            "/src/k2_mainline/scripts/_swupdate_verify_container.py",
            "--version",
            version,
            "--stock-sha256",
            manifest["stock_ota"]["sha256"],
        ]
        if with_rollback:
            verify_cmd.extend(["--rollback-version", DEFAULT_ROLLBACK_VERSION])
        run_checked(verify_cmd, env=env)

    pack_report_path = ota_stage / "ota_pack_report.json"
    swu_report_path = ota_stage / "swupdate_verify_report.json"
    pack_report = json.loads(pack_report_path.read_text(encoding="utf-8"))
    swu_report = json.loads(swu_report_path.read_text(encoding="utf-8"))

    staged_img = ota_stage / pack_report["image"]["filename"]
    with mapped_cpio_crc(staged_img) as (mm, entries):
        expected_entries = ["sw-description", "preinstall.sh", "postinstall.sh", "kernel", "rootfs", "cpio_item_md5"]
        if list(entries.keys()) != expected_entries:
            raise RuntimeError(f"Unexpected CPIO entry order: {list(entries.keys())}")
        if sha256_file(staged_img) != pack_report["image"]["sha256"]:
            raise RuntimeError("OTA image SHA-256 mismatch vs pack report")
        for line in bytes(entries["cpio_item_md5"]).decode("ascii").splitlines():
            digest_hex, name = line.split()
            if hashlib.md5(entries[name]).hexdigest() != digest_hex:
                raise RuntimeError(f"cpio_item_md5 mismatch for {name}")

    combined = {
        "schema_version": 2,
        "version": version,
        "stock_base_sha256": manifest["stock_ota"]["sha256"],
        "input_hashes": actual_inputs,
        "hardware_tested": False,
        "passed": bool(pack_report.get("passed") and swu_report.get("passed")),
        "pack": pack_report,
        "swupdate_verify": swu_report,
    }
    if not combined["passed"]:
        raise RuntimeError("Refusing to publish OTA image because verification did not pass")

    # Atomically publish verified artifacts from ota-stage to out/ota/.
    pub_img = out_ota / pack_report["image"]["filename"]
    _publish_atomic(staged_img, pub_img)
    if with_rollback and pack_report.get("rollback_image"):
        rb_staged = ota_stage / pack_report["rollback_image"]["filename"]
        _publish_atomic(rb_staged, out_ota / pack_report["rollback_image"]["filename"])
    _publish_atomic(pack_report_path, out_ota / "ota_pack_report.json")
    _publish_atomic(swu_report_path, out_ota / "swupdate_verify_report.json")

    final_report_path = out_ota / "ota_verification_report.json"
    tmp_final = final_report_path.with_name(f".{final_report_path.name}.tmp")
    tmp_final.write_text(json.dumps(combined, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_final, final_report_path)

    print(f"Single SWUpdate .img built & verified -> {pub_img} ({pub_img.stat().st_size} bytes)")
    return final_report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=DEFAULT_OTA_VERSION, help="OTA version string (default: 1.1.4.950)")
    parser.add_argument(
        "--with-rollback",
        action="store_true",
        help="Also generate a stock 1.1.4.11 rollback .img (V1.1.4.951)",
    )
    args = parser.parse_args(argv)
    build_ota(version=args.version, with_rollback=args.with_rollback)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
