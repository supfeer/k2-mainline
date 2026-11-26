#!/usr/bin/env python3
"""Build ARMv7 host binaries (c_helper.so, k2_hardware.so, klipper_mcu) in Debian Buster."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    K2_MAINLINE_ROOT,
    get_build_root,
    load_upstream_manifest,
    recreate_dir,
    run_checked,
)
from fetch_sources import ensure_klipper_tarball, extract_klipper  # noqa: E402

DOCKER_PLATFORM = "linux/arm/v7"
BASE_IMAGE = "arm32v7/python:3.9.12-buster"
BUILD_IMAGE = "k2-mainline-arm-build:buster"
DEFAULT_DOCKER_SOCK = Path.home() / ".docker" / "run" / "docker.sock"


@contextlib.contextmanager
def docker_env() -> Iterator[dict[str, str]]:
    """Provide Docker env with explicit desktop socket and empty DOCKER_CONFIG."""
    tmp_cfg = tempfile.mkdtemp(prefix="k2-docker-cfg-")
    try:
        env = os.environ.copy()
        if not env.get("DOCKER_HOST") and DEFAULT_DOCKER_SOCK.is_socket():
            env["DOCKER_HOST"] = f"unix://{DEFAULT_DOCKER_SOCK}"
        env["DOCKER_CONFIG"] = tmp_cfg
        yield env
    finally:
        for root, dirs, files in os.walk(tmp_cfg, topdown=False):
            for name in files:
                Path(root, name).unlink(missing_ok=True)
            for name in dirs:
                Path(root, name).rmdir()
        Path(tmp_cfg).rmdir()


def ensure_base_image(env: dict[str, str]) -> None:
    """Verify arm32v7/python:3.9.12-buster is present locally, pulling if missing."""
    probe = subprocess.run(
        ["docker", "image", "inspect", BASE_IMAGE],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if probe.returncode == 0:
        return
    run_checked(
        ["docker", "pull", "--platform", DOCKER_PLATFORM, BASE_IMAGE],
        env=env,
    )


def ensure_build_image(env: dict[str, str], *, rebuild: bool) -> None:
    """Build or reuse the ARMv7 Buster toolchain image."""
    ensure_base_image(env)
    if not rebuild:
        probe = subprocess.run(
            ["docker", "image", "inspect", BUILD_IMAGE],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if probe.returncode == 0:
            return

    dockerfile = K2_MAINLINE_ROOT / "docker" / "arm-build.Dockerfile"
    run_checked(
        [
            "docker",
            "build",
            "--platform",
            DOCKER_PLATFORM,
            "--pull=false",
            "-t",
            BUILD_IMAGE,
            "-f",
            str(dockerfile),
            str(K2_MAINLINE_ROOT / "docker"),
        ],
        env=env,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rebuild-image",
        action="store_true",
        help="Force rebuilding the Docker ARMv7 Buster build image",
    )
    args = parser.parse_args(argv)

    manifest = load_upstream_manifest()
    build_root = get_build_root(manifest)
    tarball = ensure_klipper_tarball(build_root / "cache", manifest, allow_download=True)
    clean_klipper = extract_klipper(tarball, build_root / "work", manifest, force=False)

    native_stage = build_root / "work" / "native-stage"
    recreate_dir(native_stage)
    out_native = build_root / "out" / "native"
    out_native.mkdir(parents=True, exist_ok=True)

    with docker_env() as env:
        ensure_build_image(env, rebuild=args.rebuild_image)
        run_checked(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                DOCKER_PLATFORM,
                "-v",
                f"{K2_MAINLINE_ROOT}:/src/k2_mainline:ro",
                "-v",
                f"{clean_klipper}:/src/klipper:ro",
                "-v",
                f"{native_stage}:/work",
                "-v",
                f"{out_native}:/out",
                BUILD_IMAGE,
                "python3",
                "-B",
                "/src/k2_mainline/scripts/_native_container_build.py",
            ],
            env=env,
        )

    report_path = out_native / "build_native_report.json"
    if not report_path.is_file():
        raise FileNotFoundError(f"Expected container report missing: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    for name in ("c_helper.so", "k2_hardware.so", "libk2_ota_shim.so", "klipper_mcu"):
        artifact = out_native / name
        if not artifact.is_file():
            raise FileNotFoundError(f"Missing expected output artifact: {artifact}")
        assert name in report["artifacts"]
    print(f"Native ARMv7 build OK -> {out_native}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
