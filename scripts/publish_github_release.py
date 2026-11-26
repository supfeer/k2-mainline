#!/usr/bin/env python3
"""Generate manifest.json + SHA256SUMS and publish verified K2 Mainline OTA to GitHub Releases."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))
from common import (  # noqa: E402
    get_build_root,
    load_upstream_manifest,
    sha256_file,
)
from k2_ota_update import (  # noqa: E402
    DEFAULT_BOARD,
    DEFAULT_MODEL,
    DEFAULT_REPO,
    crc64_ecma_file,
    validate_manifest,
)


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def load_verified_ota(out_ota: Path) -> tuple[Path, dict[str, Any]]:
    """Load ota_verification_report.json and verify the staged .img matches it."""
    report_path = out_ota / "ota_verification_report.json"
    if not report_path.is_file():
        raise FileNotFoundError(
            f"Missing OTA verification report: {report_path}; run 'make ota' first"
        )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not report.get("passed"):
        raise RuntimeError(f"Refusing to publish unverified OTA report: {report_path}")

    img_meta = report["pack"]["image"]
    img_path = out_ota / img_meta["filename"]
    if not img_path.is_file():
        raise FileNotFoundError(f"Missing verified OTA image: {img_path}")

    actual_sha = sha256_file(img_path)
    if actual_sha != img_meta["sha256"]:
        raise RuntimeError(
            f"OTA image SHA-256 mismatch: {actual_sha} != {img_meta['sha256']}"
        )
    if img_path.stat().st_size != img_meta["bytes"]:
        raise RuntimeError(
            f"OTA image size mismatch: {img_path.stat().st_size} != {img_meta['bytes']}"
        )
    return img_path, report


def build_release_bundle(
    *,
    img_path: Path,
    report: dict[str, Any],
    repo: str,
    tag: str,
    notes: list[str],
    out_dir: Path,
) -> tuple[dict[str, Any], Path, Path]:
    """Generate validated manifest.json and SHA256SUMS alongside the verified .img."""
    version = str(report["version"]).strip()
    img_sha256 = sha256_file(img_path)
    img_md5 = md5_file(img_path)
    img_crc64 = str(crc64_ecma_file(img_path))
    img_size = img_path.stat().st_size

    download_url = f"https://github.com/{repo}/releases/download/{tag}/{img_path.name}"
    manifest_raw: dict[str, Any] = {
        "schema_version": 1,
        "version": version,
        "tag": tag,
        "repo": repo,
        "board": DEFAULT_BOARD,
        "model": DEFAULT_MODEL,
        "physical_board": "CR0CN240319C13",
        "filename": img_path.name,
        "size": img_size,
        "sha256": img_sha256,
        "md5": img_md5,
        "crc64ecma": img_crc64,
        "download_url": download_url,
        "min_base_versions": ["1.1.4.11", "1.1.4.9xx"],
        "release_notes": notes,
        "published_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    validate_manifest(manifest_raw, allow_http=False)

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest_raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    sums_path = out_dir / "SHA256SUMS"
    manifest_sha = sha256_file(manifest_path)
    sums_path.write_text(
        f"{img_sha256}  {img_path.name}\n{manifest_sha}  manifest.json\n",
        encoding="utf-8",
    )
    return manifest_raw, manifest_path, sums_path


def _gh_env(gh_user: str | None) -> dict[str, str]:
    env = os.environ.copy()
    if gh_user and not env.get("GH_TOKEN"):
        token = subprocess.check_output(
            ["gh", "auth", "token", "--user", gh_user],
            text=True,
        ).strip()
        if not token:
            raise RuntimeError(f"Empty token returned for gh user {gh_user!r}")
        env["GH_TOKEN"] = token
    return env


def publish_to_github(
    *,
    repo: str,
    tag: str,
    title: str,
    notes_markdown: str,
    assets: list[Path],
    gh_user: str | None,
    prerelease: bool,
    clobber: bool,
) -> None:
    if not shutil.which("gh"):
        raise RuntimeError("GitHub CLI 'gh' not found in PATH")
    env = _gh_env(gh_user)

    exists = (
        subprocess.run(
            ["gh", "release", "view", tag, "--repo", repo],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )

    if exists:
        if not clobber:
            raise RuntimeError(
                f"GitHub release {tag} already exists in {repo}; pass --clobber to update assets"
            )
        subprocess.run(
            ["gh", "release", "upload", tag, *[str(p) for p in assets], "--repo", repo, "--clobber"],
            env=env,
            check=True,
        )
        subprocess.run(
            ["gh", "release", "edit", tag, "--repo", repo, "--title", title, "--notes", notes_markdown],
            env=env,
            check=True,
        )
    else:
        cmd = [
            "gh",
            "release",
            "create",
            tag,
            *[str(p) for p in assets],
            "--repo",
            repo,
            "--title",
            title,
            "--notes",
            notes_markdown,
        ]
        if prerelease:
            cmd.append("--prerelease")
        subprocess.run(cmd, env=env, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"Target GitHub repo (default: {DEFAULT_REPO})")
    parser.add_argument("--tag", default="", help="Release tag (default: v<version>)")
    parser.add_argument("--gh-user", default="supfeer", help="GitHub CLI user for GH_TOKEN lookup")
    parser.add_argument("--note", action="append", default=[], help="Release note line (repeatable)")
    parser.add_argument("--prerelease", action="store_true", help="Mark release as prerelease")
    parser.add_argument("--clobber", action="store_true", help="Update existing release assets if tag exists")
    parser.add_argument("--dry-run", action="store_true", help="Generate manifest.json and SHA256SUMS without publishing")
    args = parser.parse_args(argv)

    upstream = load_upstream_manifest()
    build_root = get_build_root(upstream)
    out_ota = build_root / "out" / "ota"
    img_path, report = load_verified_ota(out_ota)

    version = str(report["version"]).strip()
    tag = args.tag.strip() or f"v{version}"
    notes = args.note or [
        f"K2 Mainline v{version}",
        "\n1. Upstream Klipper v0.14.0 & Moonraker v0.10.0",
        "\n2. Fluidd v1.38.1, Mainsail v2.19.0, go2rtc v1.9.15",
        "\n3. Native K2 Plus screen OTA updates via GitHub Releases",
    ]
    bundle_dir = out_ota / f"release-{tag}"
    manifest_obj, manifest_path, sums_path = build_release_bundle(
        img_path=img_path,
        report=report,
        repo=args.repo,
        tag=tag,
        notes=notes,
        out_dir=bundle_dir,
    )

    print(f"Release bundle ready in {bundle_dir}:")
    print(f"  image   : {img_path.name} ({manifest_obj['size']} bytes)")
    print(f"  sha256  : {manifest_obj['sha256']}")
    print(f"  md5     : {manifest_obj['md5']}")
    print(f"  crc64   : {manifest_obj['crc64ecma']}")
    print(f"  manifest: {manifest_path}")
    print(f"  sums    : {sums_path}")

    if args.dry_run:
        print("Dry run complete; skipping GitHub Release upload.")
        return 0

    notes_md = "\n".join(
        [
            f"## K2 Plus Mainline OTA `{tag}`",
            "",
            f"- **Version:** `{version}`",
            f"- **Board / Model:** `{DEFAULT_BOARD}` / `{DEFAULT_MODEL}`",
            f"- **Image:** `{img_path.name}` ({manifest_obj['size']} bytes)",
            f"- **SHA-256:** `{manifest_obj['sha256']}`",
            f"- **CRC64-ECMA:** `{manifest_obj['crc64ecma']}`",
            "",
            "### Changes",
            *[f"- {line.strip()}" for line in notes if line.strip()],
        ]
    )
    publish_to_github(
        repo=args.repo,
        tag=tag,
        title=f"K2 Mainline {tag}",
        notes_markdown=notes_md,
        assets=[img_path, manifest_path, sums_path],
        gh_user=args.gh_user,
        prerelease=args.prerelease,
        clobber=args.clobber,
    )
    print(f"Published GitHub release {tag} to https://github.com/{args.repo}/releases/tag/{tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
