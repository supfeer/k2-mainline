"""Shared filesystem, hashing, stock-OTA cache, and subprocess helpers for k2_mainline."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

K2_MAINLINE_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = K2_MAINLINE_ROOT / "upstream.json"


def load_upstream_manifest() -> dict[str, Any]:
    """Load and return the pinned upstream source manifest."""
    with MANIFEST_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def get_build_root(manifest: Mapping[str, Any] | None = None) -> Path:
    """Resolve external K2_BUILD_ROOT directory and ensure standard subdirs exist."""
    if manifest is None:
        manifest = load_upstream_manifest()
    raw = os.environ.get("K2_BUILD_ROOT", "").strip()
    root = Path(raw) if raw else Path(manifest["default_build_root"])
    for rel in ("cache", "work", "out/mcu", "out/native", "out/host", "out/services", "out/ota"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    return root


def sha256_file(path: Path) -> str:
    """Compute lowercase hexadecimal SHA-256 digest for a file."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str, label: str) -> None:
    """Verify file existence and exact SHA-256 digest match."""
    if not path.is_file():
        raise FileNotFoundError(f"Missing {label}: {path}")
    actual = sha256_file(path)
    if actual.lower() != expected.lower():
        raise RuntimeError(
            f"SHA-256 mismatch for {label} ({path}): "
            f"expected {expected}, got {actual}"
        )


def recreate_dir(path: Path) -> None:
    """Delete an existing directory tree if present and recreate it empty."""
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def run_checked(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """Run a command, echoing its invocation and raising on non-zero exit."""
    print("+", " ".join(str(part) for part in cmd), flush=True)
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    subprocess.run(
        [str(part) for part in cmd],
        cwd=str(cwd) if cwd else None,
        env=merged_env,
        check=True,
    )


def ensure_stock_payload(build_root: Path, manifest: Mapping[str, Any] | None = None) -> Path:
    """Verify pinned stock OTA .img and cache verified kernel/rootfs under work/stock-payload."""
    if manifest is None:
        manifest = load_upstream_manifest()
    ota_cfg = manifest["stock_ota"]
    stock_img = build_root / "cache" / ota_cfg["name"]
    verify_sha256(stock_img, ota_cfg["sha256"], "stock OTA image")

    ota_pkg_dir = str(K2_MAINLINE_ROOT / "ota")
    if ota_pkg_dir not in sys.path:
        sys.path.insert(0, ota_pkg_dir)
    from cpio_crc import mapped_cpio_crc  # noqa: E402

    payload_dir = build_root / "work" / "stock-payload"
    kernel_path = payload_dir / "kernel"
    rootfs_path = payload_dir / "rootfs"
    stamp_path = payload_dir / "stock-payload.json"

    if stamp_path.is_file() and kernel_path.is_file() and rootfs_path.is_file():
        try:
            stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
            if (
                stamp.get("stock_sha256") == ota_cfg["sha256"]
                and sha256_file(kernel_path) == stamp.get("kernel_sha256")
                and sha256_file(rootfs_path) == stamp.get("rootfs_sha256")
            ):
                return payload_dir
        except (ValueError, OSError):
            pass

    recreate_dir(payload_dir)
    with mapped_cpio_crc(stock_img) as (_mm, entries):
        expected_order = ["sw-description", "uboot", "kernel", "rootfs", "cpio_item_md5"]
        if list(entries.keys()) != expected_order:
            raise RuntimeError(f"Unexpected stock OTA CPIO layout: {list(entries.keys())}")
        md5_lines = bytes(entries["cpio_item_md5"]).decode("ascii").splitlines()
        for line in md5_lines:
            checksum, name = line.split()
            actual_md5 = hashlib.md5(entries[name]).hexdigest()
            if actual_md5 != checksum:
                raise RuntimeError(f"Stock OTA MD5 mismatch for {name}: {actual_md5} != {checksum}")
        kernel_bytes = bytes(entries["kernel"])
        rootfs_bytes = bytes(entries["rootfs"])
        kernel_path.write_bytes(kernel_bytes)
        rootfs_path.write_bytes(rootfs_bytes)
        stamp = {
            "stock_sha256": ota_cfg["sha256"],
            "kernel_sha256": hashlib.sha256(kernel_bytes).hexdigest(),
            "rootfs_sha256": hashlib.sha256(rootfs_bytes).hexdigest(),
        }
        stamp_path.write_text(json.dumps(stamp, indent=2) + "\n", encoding="utf-8")
    return payload_dir


def compute_tree_digest(tree_dir: Path) -> tuple[str, int]:
    """Compute a deterministic digest over sorted entries in a clean source subtree."""
    if not tree_dir.is_dir():
        raise FileNotFoundError(f"Missing directory for tree digest: {tree_dir}")
    h = hashlib.sha256()
    count = 0
    for item in sorted(tree_dir.rglob("*")):
        rel = item.relative_to(tree_dir).as_posix()
        if "__pycache__" in item.relative_to(tree_dir).parts or item.suffix in (".pyc", ".pyo"):
            raise RuntimeError(f"Unexpected bytecode artifact in clean upstream tree: {item}")
        if item.is_symlink():
            h.update(f"L\t{rel}\t{os.readlink(item)}\n".encode("utf-8"))
            count += 1
        elif item.is_file():
            h.update(f"F\t{rel}\t{sha256_file(item)}\n".encode("utf-8"))
            count += 1
        elif item.is_dir():
            h.update(f"D\t{rel}\n".encode("utf-8"))
        else:
            raise RuntimeError(f"Unexpected special file in clean upstream tree: {item}")
    return h.hexdigest(), count


def verified_klipper_stamp_path(work_dir: Path, manifest: Mapping[str, Any]) -> Path:
    """Return path to the extracted Klipper integrity stamp beside work/klipper-v0.14.0."""
    return work_dir / f"{manifest['klipper']['extract_dir']}.verified.json"


def write_klipper_stamp(work_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Compute and write the integrity stamp for the extracted upstream Klipper tree."""
    cfg = manifest["klipper"]
    dest = work_dir / cfg["extract_dir"]
    klippy_digest, klippy_count = compute_tree_digest(dest / "klippy")
    src_digest, src_count = compute_tree_digest(dest / "src")
    stamp = {
        "tag": cfg["tag"],
        "commit": cfg["commit"],
        "tarball_sha256": cfg["sha256"],
        "tarball_top_dir": cfg["tarball_top_dir"],
        "klippy_tree_sha256": klippy_digest,
        "klippy_file_count": klippy_count,
        "src_tree_sha256": src_digest,
        "src_file_count": src_count,
    }
    stamp_path = verified_klipper_stamp_path(work_dir, manifest)
    tmp = stamp_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(stamp, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, stamp_path)
    return stamp


def verify_extracted_klipper(work_dir: Path, cache_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Validate extracted Klipper klippy/ and src/ trees against tarball and saved digests."""
    cfg = manifest["klipper"]
    dest = work_dir / cfg["extract_dir"]
    stamp_path = verified_klipper_stamp_path(work_dir, manifest)
    verify_sha256(cache_dir / cfg["tarball_name"], cfg["sha256"], f"Klipper {cfg['tag']} tarball")
    if not stamp_path.is_file() or not (dest / "klippy" / "klippy.py").is_file() or not (dest / "src" / "Kconfig").is_file():
        raise RuntimeError(f"Missing verified Klipper extraction stamp or tree at {dest}")
    stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    if stamp.get("tarball_sha256") != cfg["sha256"] or stamp.get("commit") != cfg["commit"]:
        raise RuntimeError("Extracted Klipper stamp does not match pinned manifest")
    klippy_digest, klippy_count = compute_tree_digest(dest / "klippy")
    src_digest, src_count = compute_tree_digest(dest / "src")
    if (klippy_digest, klippy_count) != (stamp.get("klippy_tree_sha256"), stamp.get("klippy_file_count")):
        raise RuntimeError("Extracted upstream klippy tree failed integrity digest check")
    if (src_digest, src_count) != (stamp.get("src_tree_sha256"), stamp.get("src_file_count")):
        raise RuntimeError("Extracted upstream src tree failed integrity digest check")
    return stamp


def verified_moonraker_stamp_path(work_dir: Path, manifest: Mapping[str, Any]) -> Path:
    """Return path to the extracted Moonraker integrity stamp beside work/moonraker-v0.10.0."""
    return work_dir / f"{manifest['moonraker']['extract_dir']}.verified.json"


def write_moonraker_stamp(work_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Compute and write the integrity stamp for the extracted upstream Moonraker tree."""
    cfg = manifest["moonraker"]
    dest = work_dir / cfg["extract_dir"]
    pkg_digest, pkg_count = compute_tree_digest(dest / "moonraker")
    stamp = {
        "tag": cfg["tag"],
        "commit": cfg["commit"],
        "tarball_sha256": cfg["sha256"],
        "tarball_top_dir": cfg["tarball_top_dir"],
        "moonraker_tree_sha256": pkg_digest,
        "moonraker_file_count": pkg_count,
    }
    stamp_path = verified_moonraker_stamp_path(work_dir, manifest)
    tmp = stamp_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(stamp, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, stamp_path)
    return stamp


def verify_extracted_moonraker(work_dir: Path, cache_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Validate extracted Moonraker package tree against tarball and saved digest."""
    cfg = manifest["moonraker"]
    dest = work_dir / cfg["extract_dir"]
    stamp_path = verified_moonraker_stamp_path(work_dir, manifest)
    verify_sha256(cache_dir / cfg["tarball_name"], cfg["sha256"], f"Moonraker {cfg['tag']} tarball")
    if not stamp_path.is_file() or not (dest / "moonraker" / "server.py").is_file():
        raise RuntimeError(f"Missing verified Moonraker extraction stamp or tree at {dest}")
    stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    if stamp.get("tarball_sha256") != cfg["sha256"] or stamp.get("commit") != cfg["commit"]:
        raise RuntimeError("Extracted Moonraker stamp does not match pinned manifest")
    pkg_digest, pkg_count = compute_tree_digest(dest / "moonraker")
    if (pkg_digest, pkg_count) != (stamp.get("moonraker_tree_sha256"), stamp.get("moonraker_file_count")):
        raise RuntimeError("Extracted upstream moonraker tree failed integrity digest check")
    return stamp
