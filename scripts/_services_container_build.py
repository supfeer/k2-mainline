#!/usr/bin/env python3
"""Build and ELF-audit Moonraker v0.10.0 Python 3.9 dependencies inside ARMv7 Buster."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tarfile
import csv
from pathlib import Path

REQS_PATH = Path("/src/services/moonraker-requirements-py39.txt")
STOCK_CHROOT_TAR = Path("/src/stock-chroot.tar")
WHEEL_CACHE_DIR = Path("/cache/pip-wheels")
OUT_DIR = Path("/out")
SITE_PACKAGES_DIR = OUT_DIR / "moonraker-site-packages"
REPORT_PATH = OUT_DIR / "moonraker_env_report.json"

MAX_GLIBC = (2, 29)
PIL_CONFIG_SETTINGS = [
    "-C", "jpeg=enable",
    "-C", "zlib=enable",
    "-C", "tiff=disable",
    "-C", "freetype=disable",
    "-C", "lcms=disable",
    "-C", "webp=disable",
    "-C", "jpeg2000=disable",
    "-C", "imagequant=disable",
    "-C", "raqm=disable",
    "-C", "xcb=disable",
    "-C", "avif=disable",
]
CORE_RUNTIME_LIBS = {
    "libc.so.6",
    "libm.so.6",
    "libpthread.so.0",
    "libdl.so.2",
    "librt.so.1",
    "ld-linux-armhf.so.3",
    "libgcc_s.so.1",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    merged = os.environ.copy()
    if env:
        merged.update(env)
    subprocess.run(cmd, check=True, env=merged)


def collect_target_shared_libs(chroot_tar: Path) -> set[str]:
    """Collect shared library sonames present in K2 stock rootfs /lib and /usr/lib."""
    libs: set[str] = set(CORE_RUNTIME_LIBS)
    with tarfile.open(chroot_tar, "r:*") as tf:
        for member in tf.getmembers():
            rel = member.name.lstrip("./")
            if not (rel.startswith("lib/") or rel.startswith("usr/lib/")):
                continue
            base = Path(rel).name
            if ".so" in base:
                libs.add(base)
    return libs


def ensure_build_venv() -> Path:
    """Create a temporary build virtualenv with modern pip/setuptools/wheel for PEP 517 flags."""
    venv_dir = Path("/tmp/moonraker-build-venv")
    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    run([sys.executable, "-m", "venv", str(venv_dir)])
    pip_bin = venv_dir / "bin" / "pip"
    run([str(pip_bin), "install", "--upgrade", "pip>=24.0", "setuptools>=68.0", "wheel"])
    return pip_bin


def build_wheelhouse_and_install(pip_bin: Path) -> None:
    """Build/cache ARMv7 Python 3.9 wheels and install cleanly into SITE_PACKAGES_DIR."""
    WHEEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if SITE_PACKAGES_DIR.exists():
        shutil.rmtree(SITE_PACKAGES_DIR)
    SITE_PACKAGES_DIR.mkdir(parents=True, exist_ok=True)

    # Try the local wheelhouse first.
    install_cmd = [
        str(pip_bin),
        "install",
        "--no-index",
        "--find-links",
        str(WHEEL_CACHE_DIR),
        "--target",
        str(SITE_PACKAGES_DIR),
        "--no-compile",
        "--upgrade",
        "-r",
        str(REQS_PATH),
    ]
    try:
        run(install_cmd)
    except subprocess.CalledProcessError:
        pinned = dict(line.split("==") for line in REQS_PATH.read_text().splitlines() if line)
        # pip forwards backend options only to explicit requirements.
        for name, settings, env in (
            ("pillow", PIL_CONFIG_SETTINGS, None),
            ("pyyaml", [], {"PYYAML_FORCE_LIBYAML": "0"}),
        ):
            version = pinned[name]
            if not any(WHEEL_CACHE_DIR.glob(f"{name}-{version}-*.whl")):
                run([
                    str(pip_bin), "wheel", "--wheel-dir", str(WHEEL_CACHE_DIR),
                    "--no-binary", name, "--no-deps", *settings, f"{name}=={version}",
                ], env=env)
        run([
            str(pip_bin),
            "wheel",
            "--wheel-dir",
            str(WHEEL_CACHE_DIR),
            "--find-links",
            str(WHEEL_CACHE_DIR),
            "-r",
            str(REQS_PATH),
        ])
        run(install_cmd)

    bin_dir = SITE_PACKAGES_DIR / "bin"
    if bin_dir.exists():
        shutil.rmtree(bin_dir)

    for cache_dir in sorted(SITE_PACKAGES_DIR.rglob("__pycache__"), reverse=True):
        shutil.rmtree(cache_dir)
    for pyc in sorted(SITE_PACKAGES_DIR.rglob("*")):
        if pyc.is_file() and pyc.suffix in (".pyc", ".pyo"):
            pyc.unlink()


def audit_extension_sos(target_libs: set[str]) -> list[dict[str, object]]:
    """Verify every .so extension is ARMv7 ELF32, <= GLIBC_2.29, and uses target libs only."""
    so_reports: list[dict[str, object]] = []
    for so_path in sorted(SITE_PACKAGES_DIR.rglob("*.so*")):
        if not so_path.is_file() or so_path.is_symlink():
            continue
        hdr = subprocess.check_output(["readelf", "-h", str(so_path)], text=True)
        assert "Class:                             ELF32" in hdr, f"{so_path}: not ELF32"
        assert "little endian" in hdr, f"{so_path}: not little endian"
        assert "Machine:                           ARM" in hdr, f"{so_path}: not ARM"

        dyn_out = subprocess.check_output(["readelf", "-d", str(so_path)], text=True)
        needed = re.findall(r"\(NEEDED\)\s+Shared library:\s+\[([^\]]+)\]", dyn_out)
        missing_libs = [lib for lib in needed if lib not in target_libs]
        if missing_libs:
            raise AssertionError(
                f"{so_path} depends on library(ies) missing from K2 rootfs: {missing_libs}"
            )

        ver_out = subprocess.check_output(["readelf", "-V", str(so_path)], text=True)
        versions: list[tuple[int, int]] = []
        for match in re.finditer(r"GLIBC_(\d+)\.(\d+)(?:\.(\d+))?", ver_out):
            ver = (int(match.group(1)), int(match.group(2)))
            versions.append(ver)
            assert ver <= MAX_GLIBC, (
                f"{so_path} requires GLIBC_{ver[0]}.{ver[1]} > GLIBC_{MAX_GLIBC[0]}.{MAX_GLIBC[1]}"
            )
        max_ver = max(versions) if versions else None
        so_reports.append(
            {
                "relpath": so_path.relative_to(SITE_PACKAGES_DIR).as_posix(),
                "sha256": sha256_file(so_path),
                "needed": needed,
                "max_glibc": f"{max_ver[0]}.{max_ver[1]}" if max_ver else "none",
            }
        )
    return so_reports


def normalize_extension_suffixes() -> None:
    """Use the extension suffix recognized by OpenWrt Python 3.9."""
    build_suffix = sysconfig.get_config_var("EXT_SUFFIX")
    target_suffix = ".cpython-39.so"
    renames = {}
    for path in SITE_PACKAGES_DIR.rglob(f"*{build_suffix}"):
        target = path.with_name(path.name[:-len(build_suffix)] + target_suffix)
        if target == path:
            continue
        if target.exists():
            raise RuntimeError(f"Duplicate Python extension: {target}")
        path.rename(target)
        renames[path.relative_to(SITE_PACKAGES_DIR).as_posix()] = target.relative_to(SITE_PACKAGES_DIR).as_posix()
    for record in SITE_PACKAGES_DIR.glob("*.dist-info/RECORD"):
        with record.open(newline="", encoding="utf-8") as source:
            rows = list(csv.reader(source))
        if any(row and row[0] in renames for row in rows):
            with record.open("w", newline="", encoding="utf-8") as target:
                csv.writer(target).writerows(
                    [renames.get(row[0], row[0]), *row[1:]] if row else row for row in rows
                )


def smoke_import_packages() -> dict[str, str]:
    """Verify all required Moonraker v0.10.0 dependencies import cleanly from staged dir."""
    # libnacl uses the printer's system libsodium and is exercised in the target chroot.
    check_code = r"""
import importlib
import io
import json
import sys

sys.path.insert(0, sys.argv[1])
versions = {}
for mod_name in (
    "tornado",
    "serial",
    "PIL",
    "streaming_form_data",
    "yaml",
    "distro",
    "inotify_simple",
    "paho.mqtt.client",
    "zeroconf",
    "preprocess_cancellation",
    "jinja2",
    "markupsafe",
    "dbus_fast",
    "apprise",
    "ldap3",
    "periphery",
    "importlib_metadata",
    "typing_extensions",
):
    mod = importlib.import_module(mod_name)
    mod_file = getattr(mod, "__file__", "") or ""
    if not mod_file.startswith(sys.argv[1]):
        raise AssertionError(f"{mod_name} did not load from staged site-packages: {mod_file}")
    top = importlib.import_module(mod_name.split(".")[0])
    versions[mod_name] = str(getattr(top, "__version__", getattr(top, "version", "ok")))

from PIL import Image
buf = io.BytesIO()
Image.new("RGB", (16, 16), (12, 34, 56)).save(buf, format="JPEG")
buf.seek(0)
with Image.open(buf) as im:
    im.load()
    if im.size != (16, 16):
        raise AssertionError(f"Unexpected Pillow JPEG roundtrip size: {im.size}")

print(json.dumps(versions, sort_keys=True))
"""
    raw = subprocess.check_output(
        [sys.executable, "-B", "-S", "-c", check_code, str(SITE_PACKAGES_DIR)],
        text=True,
    )
    # Remove any non-staged path fallback: re-run without -S so stdlib site works cleanly too.
    raw = subprocess.check_output(
        [sys.executable, "-B", "-c", check_code, str(SITE_PACKAGES_DIR)],
        text=True,
    )
    return json.loads(raw)


def main() -> int:
    if sys.version_info[:2] != (3, 9):
        raise RuntimeError(f"Expected Python 3.9 in container, got {sys.version}")
    if not REQS_PATH.is_file():
        raise FileNotFoundError(f"Missing requirements file: {REQS_PATH}")
    if not STOCK_CHROOT_TAR.is_file():
        raise FileNotFoundError(f"Missing stock chroot tarball: {STOCK_CHROOT_TAR}")

    target_libs = collect_target_shared_libs(STOCK_CHROOT_TAR)
    pip_bin = ensure_build_venv()
    build_wheelhouse_and_install(pip_bin)
    pkg_versions = smoke_import_packages()
    normalize_extension_suffixes()
    so_reports = audit_extension_sos(target_libs)

    file_count = 0
    total_bytes = 0
    for item in SITE_PACKAGES_DIR.rglob("*"):
        if item.is_file():
            if item.suffix in (".pyc", ".pyo") or "__pycache__" in item.parts:
                raise RuntimeError(f"Forbidden bytecode in staged site-packages: {item}")
            file_count += 1
            total_bytes += item.stat().st_size

    report = {
        "passed": True,
        "python_version": sys.version.split()[0],
        "requirements_sha256": sha256_file(REQS_PATH),
        "builder_sha256": sha256_file(Path(__file__)),
        "file_count": file_count,
        "total_bytes": total_bytes,
        "extension_modules": so_reports,
        "imported_versions": pkg_versions,
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
