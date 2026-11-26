#!/usr/bin/env python3
"""Container-side SquashFS rootfs patcher and CRC CPIO SWUpdate .img builder."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/src/k2_mainline/ota")
from cpio_crc import crc32_bytesum, mapped_cpio_crc, write_cpio_crc  # noqa: E402
from init_patches import (  # noqa: E402
    patch_app_init,
    patch_klipper_init,
    patch_klipper_mcu_init,
    patch_mcu_update_init,
    patch_moonraker_init,
)
from sw_description import FORBIDDEN_TOKENS, render_sw_description  # noqa: E402

MCU_UPDATE_STOCK_SHA = "fe2b542886c22d345e5f83dfcb9248749415647b4dfdf2fa57deae0fc9a2501e"
ROOTFS_SLOT_MAX_BYTES = 307200 * 1024
MKFS_TIME = "1788566400"

MAIN_FW_OLD = "usr/share/klipper/fw/F008/mcu0_140_G32-mcu0_022_000.bin"
MAIN_FW_NEW = "usr/share/klipper/fw/F008/mcu0_140_G32-mcu0_022_950.bin"
NOZ_FW_OLD = "usr/share/klipper/fw/F008/noz0_130_G30-noz0_021_000.bin"
NOZ_FW_NEW = "usr/share/klipper/fw/F008/noz0_130_G30-noz0_021_950.bin"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _read_xattrs(path: Path) -> dict[str, str]:
    try:
        names = sorted(os.listxattr(path, follow_symlinks=False))
    except (AttributeError, OSError):
        return {}
    attrs: dict[str, str] = {}
    for name in names:
        try:
            attrs[name] = os.getxattr(path, name, follow_symlinks=False).hex()
        except OSError:
            continue
    return attrs


def tree_state(root: Path) -> dict[str, list[object]]:
    raw_items: list[tuple[str, Path, os.stat_result]] = []
    inode_groups: dict[tuple[int, int], list[str]] = {}
    for item in sorted(root.rglob("*")):
        info = item.lstat()
        rel = item.relative_to(root).as_posix()
        raw_items.append((rel, item, info))
        if not stat.S_ISDIR(info.st_mode) and info.st_nlink > 1:
            inode_groups.setdefault((info.st_dev, info.st_ino), []).append(rel)

    entries: dict[str, list[object]] = {}
    for rel, item, info in raw_items:
        content: object
        if stat.S_ISLNK(info.st_mode):
            content = os.readlink(item)
        elif stat.S_ISREG(info.st_mode):
            content = _sha256_file(item)
        elif stat.S_ISDIR(info.st_mode):
            content = 0
        else:
            content = info.st_rdev
        xattrs = _read_xattrs(item)
        hardlink_peers: list[str] = []
        if not stat.S_ISDIR(info.st_mode) and info.st_nlink > 1:
            hardlink_peers = sorted(inode_groups[(info.st_dev, info.st_ino)])
        entries[rel] = [
            info.st_mode,
            info.st_uid,
            info.st_gid,
            content,
            xattrs,
            hardlink_peers,
        ]
    return entries


def _install_file(src: Path, dst: Path, mode: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    dst.chmod(mode)
    os.chown(dst, 0, 0)


def _render_preinstall(
    template: str,
    *,
    kernel_bytes: bytes,
    rootfs_bytes: bytes,
) -> bytes:
    kernel_min_sectors = (len(kernel_bytes) + 511) // 512
    rootfs_min_sectors = (len(rootfs_bytes) + 511) // 512
    rendered = (
        template
        .replace("@KERNEL_MIN_SECTORS@", str(kernel_min_sectors))
        .replace("@ROOTFS_MIN_SECTORS@", str(rootfs_min_sectors))
    )
    if "@KERNEL_MIN_SECTORS@" in rendered or "@ROOTFS_MIN_SECTORS@" in rendered:
        raise AssertionError("Unrendered sector placeholder in preinstall.sh")
    return rendered.encode("utf-8")


def _pack_image(
    *,
    out_img: Path,
    pkg_version: str,
    target_version: str,
    preinstall_template: str,
    postinstall_script: str,
    postinstall_bytes: bytes,
    kernel_bytes: bytes,
    rootfs_path: Path,
) -> dict[str, object]:
    sw_desc = render_sw_description(
        pkg_version,
        target_version,
        postinstall_script=postinstall_script,
    )
    rootfs_bytes = rootfs_path.read_bytes()
    preinstall_bytes = _render_preinstall(
        preinstall_template,
        kernel_bytes=kernel_bytes,
        rootfs_bytes=rootfs_bytes,
    )
    payloads = [
        ("sw-description", sw_desc, 0o644),
        ("preinstall.sh", preinstall_bytes, 0o755),
        (postinstall_script, postinstall_bytes, 0o755),
        ("kernel", kernel_bytes, 0o644),
        ("rootfs", rootfs_bytes, 0o644),
    ]
    md5_text = "".join(
        f"{hashlib.md5(data).hexdigest()}  {name}\n"
        for name, data, _mode in payloads
    ).encode("ascii")
    payloads.append(("cpio_item_md5", md5_text, 0o644))

    write_cpio_crc(out_img, payloads)
    with mapped_cpio_crc(out_img) as (mm, parsed):
        if list(parsed.keys()) != [name for name, _d, _m in payloads]:
            raise AssertionError("CPIO entry order mismatch after write")
        for name, expected_data, _m in payloads:
            if bytes(parsed[name]) != expected_data:
                raise AssertionError(f"CPIO roundtrip payload mismatch: {name}")
        desc_lower = sw_desc.decode("ascii").lower()
        for tok in FORBIDDEN_TOKENS:
            if tok in parsed or tok in desc_lower:
                raise AssertionError(f"Forbidden token {tok!r} present in OTA image")
        img_sha = hashlib.sha256(mm).hexdigest()

    return {
        "filename": out_img.name,
        "sha256": img_sha,
        "bytes": out_img.stat().st_size,
        "package_version": pkg_version,
        "target_version": target_version,
        "rootfs_bytes": len(rootfs_bytes),
        "rootfs_sha256": _sha256_bytes(rootfs_bytes),
        "kernel_sha256": _sha256_bytes(kernel_bytes),
        "sw_description_sha256": _sha256_bytes(sw_desc),
        "preinstall_sha256": _sha256_bytes(preinstall_bytes),
        "postinstall_sha256": _sha256_bytes(postinstall_bytes),
        "postinstall_script": postinstall_script,
        "cpio_entries": [name for name, _d, _m in payloads],
        "cpio_crc_values": {name: f"0x{crc32_bytesum(data):08x}" for name, data, _m in payloads},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--rollback-version", default=None)
    args = parser.parse_args(argv)

    # Work inside container native filesystem (/tmp) for full Linux POSIX inode/ownership fidelity.
    root = Path("/tmp/ota-root")
    if root.exists():
        shutil.rmtree(root)
    subprocess.run(["unsquashfs", "-no-progress", "-d", str(root), "/stock/rootfs"], check=True)
    before = tree_state(root)

    # Verify untampered stock mcu_update, CFS 1.1.3, and peripheral FWs.
    mcu_update_path = root / "etc/init.d/mcu_update"
    orig_mcu_update_text = mcu_update_path.read_text(encoding="utf-8")
    actual_mcu_update_sha = _sha256_file(mcu_update_path)
    if actual_mcu_update_sha != MCU_UPDATE_STOCK_SHA:
        raise RuntimeError(
            f"Stock etc/init.d/mcu_update SHA mismatch: {actual_mcu_update_sha} != {MCU_UPDATE_STOCK_SHA}"
        )

    preserved_fw_prefixes = (
        "usr/share/klipper/fw/cfs/",
        "usr/share/klipper/fw/F008/belt/",
        "usr/share/klipper/fw/F008/motor/",
        "usr/share/klipper/fw/F008/rfid/",
        "usr/share/klipper/fw/F008/mot0_",
        "usr/share/klipper/fw/F008/mot1_",
        "usr/share/klipper/fw/F008/mot2_",
        "usr/share/klipper/fw/F008/mcu0_130_",
    )
    preserved_pins = {
        rel: entry[3]
        for rel, entry in before.items()
        if rel.startswith(preserved_fw_prefixes)
    }
    if not any("cfs0_000_113.bin" in p for p in preserved_pins):
        raise RuntimeError("Stock CFS 1.1.3 firmware missing from stock rootfs")

    preserved_cloud_paths = (
        "usr/bin/app-server",
        "usr/bin/master-server",
        "usr/bin/display-server",
        "usr/bin/web-server",
        "usr/bin/upgrade-server",
        "usr/bin/Monitor",
        "usr/bin/webrtc",
        "usr/bin/webrtc_local",
        "usr/bin/cam_app",
        "usr/bin/mdns",
        "etc/init.d/webrtc",
        "etc/init.d/mdns",
        "etc/init.d/device_manager",
    )
    for rel in preserved_cloud_paths:
        if not (root / rel).is_file():
            raise RuntimeError(f"Expected Creality Cloud/screen file missing from stock rootfs: {rel}")
        preserved_pins[rel] = before[rel][3]

    allowed_changes: set[str] = set()

    # 1. Replace usr/share/klipper/klippy strictly from verified inventory.json (no extra files or .pyc).
    inv_path = Path("/inputs/host_out/inventory.json")
    inv = json.loads(inv_path.read_text(encoding="utf-8"))
    src_klippy = Path("/inputs/host_out/klippy")
    dst_klippy = root / "usr/share/klipper/klippy"
    if not dst_klippy.is_dir() or dst_klippy.is_symlink():
        raise RuntimeError("Missing stock usr/share/klipper/klippy directory")
    for p in before:
        if p == "usr/share/klipper/klippy" or p.startswith("usr/share/klipper/klippy/"):
            allowed_changes.add(p)
    shutil.rmtree(dst_klippy)
    dst_klippy.mkdir(parents=True)
    dst_klippy.chmod(0o755)
    os.chown(dst_klippy, 0, 0)
    allowed_changes.add("usr/share/klipper/klippy")

    for rel, meta in sorted(inv["files"].items()):
        rel_path = Path(rel)
        if "__pycache__" in rel_path.parts or rel_path.suffix in (".pyc", ".pyo"):
            raise RuntimeError(f"Refusing bytecode artifact in host inventory: {rel}")
        src = src_klippy / rel
        if not src.is_file() or src.is_symlink():
            raise RuntimeError(f"Missing verified host file from inventory: {src}")
        if _sha256_file(src) != meta["sha256"]:
            raise RuntimeError(f"Host file SHA mismatch vs inventory.json: {rel}")
        dst = dst_klippy / rel
        for parent in reversed(rel_path.parents):
            if str(parent) in ("", "."):
                continue
            pdir = dst_klippy / parent
            pdir.mkdir(parents=True, exist_ok=True)
            pdir.chmod(0o755)
            os.chown(pdir, 0, 0)
            allowed_changes.add(pdir.relative_to(root).as_posix())
        mode = int(meta["mode"])
        _install_file(src, dst, mode)
        allowed_changes.add(dst.relative_to(root).as_posix())

    # 2. Replace /usr/bin/klipper_mcu.
    dst_linux_mcu = root / "usr/bin/klipper_mcu"
    if not dst_linux_mcu.is_file():
        raise RuntimeError("Missing stock usr/bin/klipper_mcu")
    _install_file(Path("/inputs/native/klipper_mcu"), dst_linux_mcu, 0o755)
    allowed_changes.add("usr/bin/klipper_mcu")

    # 3. Preserve verbatim stock mcu_update reference and patch init.d scripts safely.
    ref_dir = root / "usr/share/k2-ota"
    ref_dir.mkdir(parents=True, exist_ok=True)
    ref_dir.chmod(0o755)
    os.chown(ref_dir, 0, 0)
    stock_ref = ref_dir / "mcu_update.stock"
    _install_file(mcu_update_path, stock_ref, 0o644)
    allowed_changes.add("usr/share/k2-ota")
    allowed_changes.add("usr/share/k2-ota/mcu_update.stock")
    preserved_pins["usr/share/k2-ota/mcu_update.stock"] = MCU_UPDATE_STOCK_SHA

    patched_mcu_update_text = patch_mcu_update_init(orig_mcu_update_text)
    if "mcu_util_485" in patched_mcu_update_text:
        raise RuntimeError("Patched etc/init.d/mcu_update still references mcu_util_485")
    mcu_update_path.write_text(patched_mcu_update_text, encoding="utf-8")
    mcu_update_path.chmod(0o755)
    os.chown(mcu_update_path, 0, 0)
    allowed_changes.add("etc/init.d/mcu_update")

    kmcu_init = root / "etc/init.d/klipper_mcu"
    kmcu_init.write_text(patch_klipper_mcu_init(kmcu_init.read_text(encoding="utf-8")), encoding="utf-8")
    kmcu_init.chmod(0o755)
    os.chown(kmcu_init, 0, 0)
    allowed_changes.add("etc/init.d/klipper_mcu")

    k_init = root / "etc/init.d/klipper"
    k_init.write_text(patch_klipper_init(k_init.read_text(encoding="utf-8")), encoding="utf-8")
    k_init.chmod(0o755)
    os.chown(k_init, 0, 0)
    allowed_changes.add("etc/init.d/klipper")

    # 4. Replace MAIN and NOZZLE MCU firmwares only.
    for old_rel, new_rel, src_name in (
        (MAIN_FW_OLD, MAIN_FW_NEW, "mcu0_140_G32-mcu0_022_950.bin"),
        (NOZ_FW_OLD, NOZ_FW_NEW, "noz0_130_G30-noz0_021_950.bin"),
    ):
        old_path = root / old_rel
        new_path = root / new_rel
        if not old_path.is_file():
            raise RuntimeError(f"Missing stock firmware file: {old_rel}")
        old_path.unlink()
        _install_file(Path("/inputs/mcu") / src_name, new_path, 0o755)
        allowed_changes.add(old_rel)
        allowed_changes.add(new_rel)

    # 5. Install clean F008_CR0CN240319C13 configs and k2_sync_config.py.
    cfg_src_dir = Path("/src/k2_mainline/config")
    cfg_dst_dir = root / "usr/share/klipper/config/F008_CR0CN240319C13"
    if not cfg_dst_dir.is_dir():
        raise RuntimeError(f"Missing target config dir in rootfs: {cfg_dst_dir}")
    for cfg_file in sorted(cfg_src_dir.glob("*.cfg")):
        dst = cfg_dst_dir / cfg_file.name
        _install_file(cfg_file, dst, 0o644)
        allowed_changes.add(dst.relative_to(root).as_posix())
    sync_dst = root / "usr/share/klipper/config/k2_sync_config.py"
    _install_file(cfg_src_dir / "k2_sync_config.py", sync_dst, 0o755)
    allowed_changes.add(sync_dst.relative_to(root).as_posix())

    # 5b. Install upstream Moonraker v0.10.0, ARMv7 site-packages, Fluidd v1.38.1, Mainsail v2.19.0, nginx.conf, static curl.
    svc_in = Path("/inputs/services")
    moon_inv = json.loads((svc_in / "moonraker" / "inventory.json").read_text(encoding="utf-8"))
    src_moon = svc_in / "moonraker"
    dst_moon = root / "usr/share/moonraker"
    if not dst_moon.is_dir() or dst_moon.is_symlink():
        raise RuntimeError("Missing stock usr/share/moonraker directory")
    for p in before:
        if p == "usr/share/moonraker" or p.startswith("usr/share/moonraker/"):
            allowed_changes.add(p)
    shutil.rmtree(dst_moon)
    dst_moon.mkdir(parents=True)
    dst_moon.chmod(0o755)
    os.chown(dst_moon, 0, 0)
    allowed_changes.add("usr/share/moonraker")

    for rel, meta in sorted(moon_inv["files"].items()):
        rel_path = Path(rel)
        if "__pycache__" in rel_path.parts or rel_path.suffix in (".pyc", ".pyo"):
            raise RuntimeError(f"Refusing bytecode artifact in moonraker inventory: {rel}")
        src = src_moon / rel
        if not src.is_file() or src.is_symlink() or _sha256_file(src) != meta["sha256"]:
            raise RuntimeError(f"Verified moonraker file mismatch: {rel}")
        dst = dst_moon / rel
        for parent in reversed(rel_path.parents):
            if str(parent) in ("", "."):
                continue
            pdir = dst_moon / parent
            pdir.mkdir(parents=True, exist_ok=True)
            pdir.chmod(0o755)
            os.chown(pdir, 0, 0)
            allowed_changes.add(pdir.relative_to(root).as_posix())
        _install_file(src, dst, int(meta["mode"]))
        allowed_changes.add(dst.relative_to(root).as_posix())

    src_site = svc_in / "moonraker-site-packages"
    dst_site = root / "usr/share/moonraker-env/lib/python3.9/site-packages"
    if not dst_site.is_dir() or dst_site.is_symlink():
        raise RuntimeError("Missing stock usr/share/moonraker-env/lib/python3.9/site-packages directory")
    for p in before:
        if p == "usr/share/moonraker-env/lib/python3.9/site-packages" or p.startswith(
            "usr/share/moonraker-env/lib/python3.9/site-packages/"
        ):
            allowed_changes.add(p)
    shutil.rmtree(dst_site)
    dst_site.mkdir(parents=True)
    dst_site.chmod(0o755)
    os.chown(dst_site, 0, 0)
    allowed_changes.add("usr/share/moonraker-env/lib/python3.9/site-packages")

    for item in sorted(src_site.rglob("*")):
        rel = item.relative_to(src_site)
        if "__pycache__" in rel.parts or item.suffix in (".pyc", ".pyo"):
            raise RuntimeError(f"Forbidden bytecode in moonraker-site-packages: {rel}")
        if item.is_symlink():
            raise RuntimeError(f"Forbidden symlink in moonraker-site-packages: {rel}")
        dst = dst_site / rel
        if item.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            dst.chmod(0o755)
            os.chown(dst, 0, 0)
        elif item.is_file():
            mode = 0o755 if (".so" in item.name or (item.stat().st_mode & 0o111)) else 0o644
            _install_file(item, dst, mode)
        else:
            raise RuntimeError(f"Unsupported entry in moonraker-site-packages: {rel}")
        allowed_changes.add(dst.relative_to(root).as_posix())

    moon_init = root / "etc/init.d/moonraker"
    moon_init.write_text(patch_moonraker_init(moon_init.read_text(encoding="utf-8")), encoding="utf-8")
    moon_init.chmod(0o755)
    os.chown(moon_init, 0, 0)
    allowed_changes.add("etc/init.d/moonraker")

    # Install Fluidd v1.38.1 (replacing stock v1.30.0) and Mainsail v2.19.0.
    for ui_name, require_existing in (("fluidd", True), ("mainsail", False)):
        src_ui = svc_in / ui_name
        dst_ui = root / "usr/share" / ui_name
        if require_existing and (not dst_ui.is_dir() or dst_ui.is_symlink()):
            raise RuntimeError(f"Missing stock usr/share/{ui_name} directory")
        if not require_existing and dst_ui.exists():
            raise RuntimeError(f"Unexpected existing usr/share/{ui_name} in stock rootfs")
        prefix = f"usr/share/{ui_name}/"
        for p in before:
            if p == f"usr/share/{ui_name}" or p.startswith(prefix):
                allowed_changes.add(p)
        if dst_ui.exists():
            shutil.rmtree(dst_ui)
        dst_ui.mkdir(parents=True)
        dst_ui.chmod(0o755)
        os.chown(dst_ui, 0, 0)
        allowed_changes.add(f"usr/share/{ui_name}")
        for item in sorted(src_ui.rglob("*")):
            if item.is_symlink():
                raise RuntimeError(f"Unexpected symlink in {ui_name}: {item}")
            dst = dst_ui / item.relative_to(src_ui)
            if item.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
                dst.chmod(0o755)
                os.chown(dst, 0, 0)
            elif item.is_file():
                _install_file(item, dst, 0o644)
            else:
                raise RuntimeError(f"Unsupported entry in {ui_name}: {item}")
            allowed_changes.add(dst.relative_to(root).as_posix())

    nginx_dst = root / "etc/nginx/nginx.conf"
    if not nginx_dst.is_file():
        raise RuntimeError("Missing stock etc/nginx/nginx.conf")
    _install_file(Path("/src/k2_mainline/services/nginx.conf"), nginx_dst, 0o644)
    allowed_changes.add("etc/nginx/nginx.conf")

    curl_dst = root / "usr/bin/curl"
    if curl_dst.exists() or curl_dst.is_symlink():
        raise RuntimeError("Unexpected existing usr/bin/curl in stock rootfs")
    _install_file(svc_in / "bin" / "curl", curl_dst, 0o755)
    allowed_changes.add("usr/bin/curl")

    # Install go2rtc v1.9.15 camera gateway alongside stock cam_app / webrtc / webrtc_local.
    go2rtc_dst = root / "usr/bin/go2rtc"
    if go2rtc_dst.exists() or go2rtc_dst.is_symlink():
        raise RuntimeError("Unexpected existing usr/bin/go2rtc in stock rootfs")
    _install_file(svc_in / "bin" / "go2rtc", go2rtc_dst, 0o755)
    allowed_changes.add("usr/bin/go2rtc")

    h264_src_dst = root / "usr/bin/k2_h264_source"
    if h264_src_dst.exists() or h264_src_dst.is_symlink():
        raise RuntimeError("Unexpected existing usr/bin/k2_h264_source in stock rootfs")
    _install_file(Path("/src/k2_mainline/services/k2_h264_source"), h264_src_dst, 0o755)
    allowed_changes.add("usr/bin/k2_h264_source")

    go2rtc_cfg_dir = root / "etc/go2rtc"
    if go2rtc_cfg_dir.exists() or go2rtc_cfg_dir.is_symlink():
        raise RuntimeError("Unexpected existing etc/go2rtc in stock rootfs")
    go2rtc_cfg_dir.mkdir(parents=True)
    go2rtc_cfg_dir.chmod(0o755)
    os.chown(go2rtc_cfg_dir, 0, 0)
    allowed_changes.add("etc/go2rtc")

    go2rtc_cfg_dst = go2rtc_cfg_dir / "go2rtc.yaml"
    _install_file(Path("/src/k2_mainline/services/go2rtc.yaml"), go2rtc_cfg_dst, 0o644)
    allowed_changes.add("etc/go2rtc/go2rtc.yaml")

    go2rtc_init_dst = root / "etc/init.d/go2rtc"
    if go2rtc_init_dst.exists() or go2rtc_init_dst.is_symlink():
        raise RuntimeError("Unexpected existing etc/init.d/go2rtc in stock rootfs")
    _install_file(Path("/src/k2_mainline/services/init_go2rtc"), go2rtc_init_dst, 0o755)
    allowed_changes.add("etc/init.d/go2rtc")

    go2rtc_rc_dst = root / "etc/rc.d/S98go2rtc"
    if go2rtc_rc_dst.exists() or go2rtc_rc_dst.is_symlink():
        raise RuntimeError("Unexpected existing etc/rc.d/S98go2rtc in stock rootfs")
    go2rtc_rc_dst.symlink_to("../init.d/go2rtc")
    os.lchown(go2rtc_rc_dst, 0, 0)
    allowed_changes.add("etc/rc.d/S98go2rtc")

    # Install GitHub Releases OTA updater, loopback screen proxy, and app-server LD_PRELOAD shim.
    ota_shim_dst = root / "usr/lib/libk2_ota_shim.so"
    if ota_shim_dst.exists() or ota_shim_dst.is_symlink():
        raise RuntimeError("Unexpected existing usr/lib/libk2_ota_shim.so in stock rootfs")
    _install_file(Path("/inputs/native/libk2_ota_shim.so"), ota_shim_dst, 0o755)
    allowed_changes.add("usr/lib/libk2_ota_shim.so")

    ota_cli_dst = root / "usr/bin/k2-ota-update"
    if ota_cli_dst.exists() or ota_cli_dst.is_symlink():
        raise RuntimeError("Unexpected existing usr/bin/k2-ota-update in stock rootfs")
    _install_file(Path("/src/k2_mainline/services/k2_ota_update.py"), ota_cli_dst, 0o755)
    allowed_changes.add("usr/bin/k2-ota-update")

    ota_init_dst = root / "etc/init.d/k2_ota_proxy"
    if ota_init_dst.exists() or ota_init_dst.is_symlink():
        raise RuntimeError("Unexpected existing etc/init.d/k2_ota_proxy in stock rootfs")
    _install_file(Path("/src/k2_mainline/services/init_k2_ota_proxy"), ota_init_dst, 0o755)
    allowed_changes.add("etc/init.d/k2_ota_proxy")

    ota_rc_dst = root / "etc/rc.d/S97k2_ota_proxy"
    if ota_rc_dst.exists() or ota_rc_dst.is_symlink():
        raise RuntimeError("Unexpected existing etc/rc.d/S97k2_ota_proxy in stock rootfs")
    ota_rc_dst.symlink_to("../init.d/k2_ota_proxy")
    os.lchown(ota_rc_dst, 0, 0)
    allowed_changes.add("etc/rc.d/S97k2_ota_proxy")

    app_init = root / "etc/init.d/app"
    if not app_init.is_file():
        raise RuntimeError("Missing stock etc/init.d/app")
    app_init_stock_sha = before["etc/init.d/app"][3]
    app_init_ref = ref_dir / "app.init.stock"
    _install_file(app_init, app_init_ref, 0o644)
    allowed_changes.add("usr/share/k2-ota/app.init.stock")
    preserved_pins["usr/share/k2-ota/app.init.stock"] = app_init_stock_sha

    app_init.write_text(patch_app_init(app_init.read_text(encoding="utf-8")), encoding="utf-8")
    app_init.chmod(0o755)
    os.chown(app_init, 0, 0)
    allowed_changes.add("etc/init.d/app")

    # 6. Verify strict rootfs change set & preserved components.
    after = tree_state(root)
    actual_changed = {
        name for name in (before.keys() | after.keys())
        if before.get(name) != after.get(name)
    }
    unexpected = actual_changed - allowed_changes
    if unexpected:
        raise RuntimeError(f"Unexpected rootfs modifications outside allowlist: {sorted(unexpected)[:20]}")
    for rel, expected_sha in preserved_pins.items():
        if after.get(rel, [None, None, None, None])[3] != expected_sha:
            raise RuntimeError(f"Preserved stock file mutated unexpectedly: {rel}")

    out_dir = Path("/stage")
    out_dir.mkdir(parents=True, exist_ok=True)
    # Write SHA-pinned copy of patched etc/init.d/mcu_update for ARMv7 chroot verification.
    patched_copy = out_dir / "patched_mcu_update"
    patched_copy.write_bytes(mcu_update_path.read_bytes())

    # 7. Pack SquashFS & perform full roundtrip comparison inside container FS.
    custom_rootfs = Path("/tmp/custom.rootfs")
    if custom_rootfs.exists():
        custom_rootfs.unlink()
    subprocess.run(
        [
            "mksquashfs",
            str(root),
            str(custom_rootfs),
            "-noappend",
            "-comp",
            "gzip",
            "-b",
            "262144",
            "-no-progress",
            "-mkfs-time",
            MKFS_TIME,
        ],
        check=True,
    )
    rootfs_size = custom_rootfs.stat().st_size
    if rootfs_size >= ROOTFS_SLOT_MAX_BYTES:
        raise RuntimeError(f"SquashFS exceeds 300 MiB partition: {rootfs_size} >= {ROOTFS_SLOT_MAX_BYTES}")

    roundtrip_dir = Path("/tmp/ota-roundtrip")
    if roundtrip_dir.exists():
        shutil.rmtree(roundtrip_dir)
    subprocess.run(
        ["unsquashfs", "-no-progress", "-d", str(roundtrip_dir), str(custom_rootfs)],
        check=True,
    )
    if tree_state(roundtrip_dir) != after:
        raise RuntimeError("SquashFS roundtrip unpack differed from staged rootfs tree")
    shutil.rmtree(roundtrip_dir)
    shutil.rmtree(root)

    # 8. Build SWUpdate CRC CPIO .img package(s) into staging directory.
    preinstall_template = Path("/src/k2_mainline/ota/preinstall.sh.tmpl").read_text(encoding="utf-8")
    postinstall_template = Path("/src/k2_mainline/ota/postinstall.sh.tmpl").read_text(encoding="utf-8")
    postinstall_rendered = postinstall_template.replace("@TARGET_VERSION@", args.version)
    if "@TARGET_VERSION@" in postinstall_rendered:
        raise AssertionError("Failed to substitute @TARGET_VERSION@ in postinstall.sh")
    postinstall_bytes = postinstall_rendered.encode("utf-8")
    kernel_bytes = Path("/stock/kernel").read_bytes()
    main_img = out_dir / f"CR0CN240110C10_ota_img_V{args.version}.img"
    img_info = _pack_image(
        out_img=main_img,
        pkg_version=args.version,
        target_version=args.version,
        preinstall_template=preinstall_template,
        postinstall_script="postinstall.sh",
        postinstall_bytes=postinstall_bytes,
        kernel_bytes=kernel_bytes,
        rootfs_path=custom_rootfs,
    )

    rollback_info = None
    if args.rollback_version:
        rb_template = Path("/src/k2_mainline/ota/rollback_postinstall.sh.tmpl").read_text(encoding="utf-8")
        rb_rendered = rb_template.replace("@TARGET_VERSION@", "1.1.4.11")
        if "@TARGET_VERSION@" in rb_rendered:
            raise AssertionError("Failed to substitute @TARGET_VERSION@ in rollback_postinstall.sh")
        rb_post = rb_rendered.encode("utf-8")
        rb_img = out_dir / f"CR0CN240110C10_ota_img_V{args.rollback_version}.img"
        rollback_info = _pack_image(
            out_img=rb_img,
            pkg_version=args.rollback_version,
            target_version="1.1.4.11",
            preinstall_template=preinstall_template,
            postinstall_script="rollback_postinstall.sh",
            postinstall_bytes=rb_post,
            kernel_bytes=kernel_bytes,
            rootfs_path=Path("/stock/rootfs"),
        )

    installed_klippy_pyc = [
        p for p in after
        if (p == "usr/share/klipper/klippy" or p.startswith("usr/share/klipper/klippy/"))
        and (p.endswith((".pyc", ".pyo")) or "__pycache__" in Path(p).parts)
    ]
    if installed_klippy_pyc:
        raise RuntimeError(f"Unexpected bytecode artifacts inside packed klippy tree: {installed_klippy_pyc[:10]}")
    deleted_pyc_count = sum(
        1 for p in actual_changed
        if p not in after and p.startswith("usr/share/klipper/klippy/") and p.endswith((".pyc", ".pyo"))
    )

    report = {
        "passed": True,
        "stock_entries_count": len(before),
        "changed_rootfs_paths_count": len(actual_changed),
        "changed_rootfs_paths": sorted(actual_changed),
        "klippy_bytecode_artifacts": 0,
        "deleted_stock_klippy_pyc_count": deleted_pyc_count,
        "patched_mcu_update_sha256": after["etc/init.d/mcu_update"][3],
        "squashfs_roundtrip": True,
        "rootfs_slot_limit_bytes": ROOTFS_SLOT_MAX_BYTES,
        "preserved_pins": preserved_pins,
        "image": img_info,
        "rollback_image": rollback_info,
    }
    (out_dir / "ota_pack_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
