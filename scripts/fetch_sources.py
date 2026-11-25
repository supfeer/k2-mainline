#!/usr/bin/env python3
"""Download/verify pinned upstream Klipper tarball and stock OTA image into K2_BUILD_ROOT."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    K2_MAINLINE_ROOT,
    get_build_root,
    load_upstream_manifest,
    recreate_dir,
    verify_extracted_klipper,
    verify_extracted_moonraker,
    verify_sha256,
    write_klipper_stamp,
    write_moonraker_stamp,
)


def download_file(url: str, dest: Path) -> None:
    """Stream a remote file to a temporary sibling path before atomic rename."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    print(f"Downloading {url} -> {dest}", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "k2-mainline-fetch/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as out:
        shutil.copyfileobj(resp, out, length=1024 * 1024)
    os.replace(tmp, dest)


def ensure_klipper_tarball(
    cache_dir: Path, manifest: dict, *, allow_download: bool
) -> Path:
    """Ensure the pinned Klipper tarball exists in cache and matches SHA-256."""
    cfg = manifest["klipper"]
    tarball = cache_dir / cfg["tarball_name"]
    if not tarball.is_file():
        if not allow_download:
            raise FileNotFoundError(
                f"Klipper tarball missing from cache ({tarball}) and download disabled"
            )
        download_file(cfg["tarball_url"], tarball)
    verify_sha256(tarball, cfg["sha256"], f"Klipper {cfg['tag']} tarball")
    return tarball


def _ensure_cached_download(
    cache_dir: Path,
    *,
    filename: str,
    url: str,
    sha256: str,
    label: str,
    allow_download: bool,
) -> Path:
    dest = cache_dir / filename
    if not dest.is_file():
        if not allow_download:
            raise FileNotFoundError(
                f"{label} missing from cache ({dest}) and download disabled"
            )
        download_file(url, dest)
    verify_sha256(dest, sha256, label)
    return dest


def ensure_moonraker_tarball(
    cache_dir: Path, manifest: dict, *, allow_download: bool
) -> Path:
    """Ensure the pinned Moonraker tarball exists in cache and matches SHA-256."""
    cfg = manifest["moonraker"]
    return _ensure_cached_download(
        cache_dir,
        filename=cfg["tarball_name"],
        url=cfg["tarball_url"],
        sha256=cfg["sha256"],
        label=f"Moonraker {cfg['tag']} tarball",
        allow_download=allow_download,
    )


def ensure_fluidd_zip(
    cache_dir: Path, manifest: dict, *, allow_download: bool
) -> Path:
    """Ensure the pinned Fluidd zip release exists in cache and matches SHA-256."""
    cfg = manifest["fluidd"]
    return _ensure_cached_download(
        cache_dir,
        filename=cfg["filename"],
        url=cfg["url"],
        sha256=cfg["sha256"],
        label=f"Fluidd {cfg['tag']} zip",
        allow_download=allow_download,
    )


def ensure_mainsail_zip(
    cache_dir: Path, manifest: dict, *, allow_download: bool
) -> Path:
    """Ensure the pinned Mainsail zip release exists in cache and matches SHA-256."""
    cfg = manifest["mainsail"]
    return _ensure_cached_download(
        cache_dir,
        filename=cfg["filename"],
        url=cfg["url"],
        sha256=cfg["sha256"],
        label=f"Mainsail {cfg['tag']} zip",
        allow_download=allow_download,
    )


def ensure_static_curl_archive(
    cache_dir: Path, manifest: dict, *, allow_download: bool
) -> Path:
    """Ensure the pinned static curl archive exists in cache and matches SHA-256."""
    cfg = manifest["static_curl"]
    return _ensure_cached_download(
        cache_dir,
        filename=cfg["filename"],
        url=cfg["url"],
        sha256=cfg["sha256"],
        label=f"Static curl {cfg['tag']} archive",
        allow_download=allow_download,
    )


def ensure_go2rtc_binary(
    cache_dir: Path, manifest: dict, *, allow_download: bool
) -> Path:
    """Ensure the pinned static ARMv7 go2rtc binary exists in cache and matches SHA-256."""
    cfg = manifest["go2rtc"]
    return _ensure_cached_download(
        cache_dir,
        filename=cfg["filename"],
        url=cfg["url"],
        sha256=cfg["sha256"],
        label=f"go2rtc {cfg['tag']} binary",
        allow_download=allow_download,
    )


def ensure_stock_ota(cache_dir: Path, manifest: dict) -> Path:
    """Ensure the base stock OTA .img exists in cache and matches SHA-256."""
    cfg = manifest["stock_ota"]
    cached = cache_dir / cfg["name"]
    if not cached.is_file():
        fallback = (K2_MAINLINE_ROOT / cfg["fallback_repo_rel"]).resolve()
        if fallback.is_file():
            print(f"Copying stock OTA from {fallback} -> {cached}", flush=True)
            tmp = cached.with_suffix(cached.suffix + ".tmp")
            shutil.copy2(fallback, tmp)
            os.replace(tmp, cached)
        else:
            raise FileNotFoundError(
                f"Stock OTA base image not found in cache ({cached}) "
                f"or repo fallback ({fallback})"
            )
    verify_sha256(cached, cfg["sha256"], f"Stock OTA {cfg['name']}")
    return cached


def _safe_extract_tar_stripped(tarball: Path, target_dir: Path, strip_prefix: str) -> None:
    """Safely extract tar members stripping the top-level directory prefix."""
    tmp_parent = target_dir.parent
    staging = Path(tempfile.mkdtemp(prefix=f".{target_dir.name}-", dir=str(tmp_parent)))
    try:
        prefix = strip_prefix.rstrip("/") + "/"
        with tarfile.open(tarball, "r:gz") as tf:
            for member in tf.getmembers():
                rel = member.name.lstrip("./")
                if rel == strip_prefix.rstrip("/"):
                    continue
                if not rel.startswith(prefix):
                    raise RuntimeError(
                        f"Unexpected tar member outside {prefix!r}: {member.name!r}"
                    )
                stripped = rel[len(prefix):]
                if not stripped:
                    continue
                parts = Path(stripped).parts
                if Path(stripped).is_absolute() or ".." in parts:
                    raise RuntimeError(f"Unsafe tar member path: {member.name!r}")
                out_path = staging / stripped
                if member.isdir():
                    out_path.mkdir(parents=True, exist_ok=True)
                elif member.issym():
                    link_parts = Path(member.linkname).parts
                    if os.path.isabs(member.linkname) or ".." in link_parts:
                        raise RuntimeError(
                            f"Unsafe symlink target in tarball: {member.name!r} -> {member.linkname!r}"
                        )
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    os.symlink(member.linkname, out_path)
                elif member.islnk():
                    raise RuntimeError(f"Unsupported hardlink in tarball: {member.name!r}")
                elif member.isfile():
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    src_fh = tf.extractfile(member)
                    if src_fh is None:
                        raise RuntimeError(f"Failed to read tar member: {member.name!r}")
                    with src_fh, out_path.open("wb") as dst_fh:
                        shutil.copyfileobj(src_fh, dst_fh)
                    os.chmod(out_path, member.mode & 0o777)
        recreate_dir(target_dir)
        # Move staging into target_dir after clearing empty target_dir.
        target_dir.rmdir()
        os.replace(staging, target_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def extract_klipper(tarball: Path, work_dir: Path, manifest: dict, *, force: bool) -> Path:
    """Extract clean Klipper tree into K2_BUILD_ROOT/work if needed."""
    cfg = manifest["klipper"]
    dest = work_dir / cfg["extract_dir"]
    sentinel = dest / "src" / "Kconfig"
    if not force:
        try:
            verify_extracted_klipper(work_dir, tarball.parent, manifest)
            print(f"Using verified clean Klipper tree: {dest}", flush=True)
            return dest
        except Exception:
            pass
    print(f"Extracting {tarball.name} -> {dest}", flush=True)
    _safe_extract_tar_stripped(tarball, dest, cfg["tarball_top_dir"])
    if not sentinel.is_file():
        raise RuntimeError(f"Extracted Klipper tree missing {sentinel}")
    write_klipper_stamp(work_dir, manifest)
    return dest


def extract_moonraker(tarball: Path, work_dir: Path, manifest: dict, *, force: bool) -> Path:
    """Extract clean Moonraker tree into K2_BUILD_ROOT/work if needed."""
    cfg = manifest["moonraker"]
    dest = work_dir / cfg["extract_dir"]
    sentinel = dest / "moonraker" / "server.py"
    if not force:
        try:
            verify_extracted_moonraker(work_dir, tarball.parent, manifest)
            print(f"Using verified clean Moonraker tree: {dest}", flush=True)
            return dest
        except Exception:
            pass
    print(f"Extracting {tarball.name} -> {dest}", flush=True)
    _safe_extract_tar_stripped(tarball, dest, cfg["tarball_top_dir"])
    if not sentinel.is_file():
        raise RuntimeError(f"Extracted Moonraker tree missing {sentinel}")
    write_moonraker_stamp(work_dir, manifest)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--extract",
        action="store_true",
        help="Extract clean Klipper and Moonraker source trees into $K2_BUILD_ROOT/work",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-extract clean source trees even if already present",
    )
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="Fail instead of downloading if an upstream archive is absent from cache",
    )
    args = parser.parse_args(argv)

    manifest = load_upstream_manifest()
    build_root = get_build_root(manifest)
    cache_dir = build_root / "cache"
    work_dir = build_root / "work"

    allow_dl = not args.no_download
    tarball = ensure_klipper_tarball(cache_dir, manifest, allow_download=allow_dl)
    moon_tarball = ensure_moonraker_tarball(cache_dir, manifest, allow_download=allow_dl)
    fluidd_zip = ensure_fluidd_zip(cache_dir, manifest, allow_download=allow_dl)
    mainsail_zip = ensure_mainsail_zip(cache_dir, manifest, allow_download=allow_dl)
    curl_arc = ensure_static_curl_archive(cache_dir, manifest, allow_download=allow_dl)
    go2rtc_bin = ensure_go2rtc_binary(cache_dir, manifest, allow_download=allow_dl)
    ota = ensure_stock_ota(cache_dir, manifest)

    extracted = None
    moon_extracted = None
    if args.extract or args.force:
        extracted = extract_klipper(tarball, work_dir, manifest, force=args.force)
        moon_extracted = extract_moonraker(moon_tarball, work_dir, manifest, force=args.force)

    print("Verified external sources:")
    print(f"  K2_BUILD_ROOT  : {build_root}")
    print(f"  Klipper tarball: {tarball} ({manifest['klipper']['sha256']})")
    print(f"  Moonraker      : {moon_tarball} ({manifest['moonraker']['sha256']})")
    print(f"  Fluidd         : {fluidd_zip} ({manifest['fluidd']['sha256']})")
    print(f"  Mainsail       : {mainsail_zip} ({manifest['mainsail']['sha256']})")
    print(f"  Static curl    : {curl_arc} ({manifest['static_curl']['sha256']})")
    print(f"  go2rtc         : {go2rtc_bin} ({manifest['go2rtc']['sha256']})")
    print(f"  Stock OTA      : {ota} ({manifest['stock_ota']['sha256']})")
    if extracted is not None:
        print(f"  Clean Klipper  : {extracted}")
    if moon_extracted is not None:
        print(f"  Clean Moonraker: {moon_extracted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
