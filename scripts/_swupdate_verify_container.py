#!/usr/bin/env python3
"""Container-side ARMv7 verification using real Creality /sbin/swupdate & shell checks."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, "/src/k2_mainline/ota")
from cpio_crc import read_cpio_crc  # noqa: E402


def _corrupt_rootfs_payload(data: bytes) -> bytes:
    buf = bytearray(data)
    offset = 0
    while True:
        hdr = buf[offset : offset + 110]
        if hdr[:6] != b"070702":
            raise AssertionError("Expected CRC CPIO header while corrupting fixture")
        size = int(hdr[54:62], 16)
        namesize = int(hdr[94:102], 16)
        name = bytes(buf[offset + 110 : offset + 110 + namesize - 1])
        start = (offset + 110 + namesize + 3) & ~3
        if name == b"rootfs":
            buf[start + size // 2] ^= 0x01
            return bytes(buf)
        offset = (start + size + 3) & ~3


def _populate_dev_nodes(root: Path) -> None:
    dev_dir = root / "dev"
    dev_dir.mkdir(parents=True, exist_ok=True)
    for name, minor in (("null", 3), ("zero", 5), ("full", 7), ("random", 8), ("urandom", 9)):
        node = dev_dir / name
        if node.is_symlink() or (node.exists() and not stat.S_ISCHR(node.stat().st_mode)):
            node.unlink()
        if not node.exists():
            os.mknod(node, stat.S_IFCHR | 0o666, os.makedev(1, minor))
        node.chmod(0o666)


def _setup_slot_stubs(
    root: Path,
    *,
    alias_rootfs: bool = False,
    cross_alias_bootB_to_rootfsA: bool = False,
) -> None:
    (root / "dev/by-name").mkdir(parents=True, exist_ok=True)
    slot_map = (
        ("bootA", "mmcblk0p4", "179:4", "32768"),
        ("bootB", "mmcblk0p5", "179:5", "32768"),
        ("rootfsA", "mmcblk0p6", "179:6", "614400"),
        ("rootfsB", "mmcblk0p7", "179:7", "614400"),
    )
    for label, dev_name, dev_id, sz in slot_map:
        actual_dev = dev_name
        if alias_rootfs and label == "rootfsB":
            actual_dev = "mmcblk0p6"
        if cross_alias_bootB_to_rootfsA and label == "bootB":
            actual_dev = "mmcblk0p6"
        dev_path = root / "dev" / dev_name
        dev_path.write_bytes(b"untouched")
        link = root / "dev/by-name" / label
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(f"/dev/{actual_dev}")
        blk_dir = root / "sys/class/block" / dev_name
        blk_dir.mkdir(parents=True, exist_ok=True)
        (blk_dir / "size").write_text(sz + "\n", encoding="utf-8")
        (blk_dir / "dev").write_text(dev_id + "\n", encoding="utf-8")


def _write_uboot_env(root: Path, env_map: dict[str, str]) -> None:
    payload = b"".join(f"{k}={v}\x00".encode("ascii") for k, v in env_map.items()) + b"\x00"
    data = payload.ljust(0x20000 - 5, b"\x00")
    if len(data) != 0x20000 - 5:
        raise AssertionError("U-Boot env payload exceeds 128 KiB")
    crc = zlib.crc32(data) & 0xFFFFFFFF
    blob = struct.pack("<I", crc) + b"\x01" + data
    by_name = root / "dev/by-name"
    by_name.mkdir(parents=True, exist_ok=True)
    for name in ("env", "env-redund"):
        (by_name / name).write_bytes(blob)


def _read_uboot_env(root: Path) -> dict[str, str]:
    proc = subprocess.run(
        ["chroot", str(root), "/usr/sbin/fw_printenv"],
        capture_output=True,
        text=True,
        check=True,
    )
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def _install_bootenv_mocks(mocks: Path) -> None:
    # Remove any mock fw_printenv so real /usr/sbin/fw_printenv reads /dev/by-name/env*.
    fp_mock = mocks / "fw_printenv"
    if fp_mock.exists() or fp_mock.is_symlink():
        fp_mock.unlink()
    # Thin wrapper delegating to real /usr/sbin/fw_setenv with optional failure / readback corruption hooks.
    (mocks / "fw_setenv").write_text(
        '#!/bin/sh\n'
        'if [ -f /tmp/fw-setenv-fail ] && [ "$(cat /tmp/fw-setenv-fail)" = "1" ]; then\n'
        '  echo "simulated fw_setenv failure" >&2\n'
        '  exit 1\n'
        'fi\n'
        'if [ -f /tmp/fw-setenv-revert-fail ] && [ "$(cat /tmp/fw-setenv-revert-fail)" = "1" ] && grep -q "boot_partition bootA" "$2" 2>/dev/null; then\n'
        '  echo "simulated fw_setenv revert failure" >&2\n'
        '  exit 1\n'
        'fi\n'
        'if ! /usr/sbin/fw_setenv "$@"; then\n'
        '  exit 1\n'
        'fi\n'
        'if [ -f /tmp/fw-setenv-corrupt ] && [ "$(cat /tmp/fw-setenv-corrupt)" = "1" ] && grep -q "boot_partition bootB" "$2" 2>/dev/null; then\n'
        '  /usr/sbin/fw_setenv version CORRUPTED_READBACK\n'
        'fi\n'
        'exit 0\n',
        encoding="utf-8",
    )


def _reset_env_files(root: Path, overrides: dict[str, str | None] | None = None) -> None:
    defaults: dict[str, str | None] = {
        "fw-board": "CR0CN240110C10",
        "fw-version": "1.1.4.11",
        "fw-boot": "bootA",
        "fw-root": "rootfsA",
        "fw-swu-mode": "now_A_next_B",
        "fw-swu-next": None,
        "fw-setenv-fail": "0",
        "fw-setenv-corrupt": "0",
        "fw-setenv-revert-fail": "0",
        "sn-model": "F008",
        "sn-board": "CR0CN240319C13",
        "sn-struct": "0",
        "df-free-kib": "1048576",
    }
    if overrides:
        defaults.update(overrides)
    for k, v in defaults.items():
        p = root / "tmp" / k
        if v is None:
            if p.exists() or p.is_symlink():
                p.unlink()
        else:
            p.write_text(v + "\n", encoding="utf-8")

    env_map: dict[str, str] = {}
    key_map = (
        ("fw-board", "board"),
        ("fw-version", "version"),
        ("fw-boot", "boot_partition"),
        ("fw-root", "root_partition"),
        ("fw-swu-mode", "swu_mode"),
        ("fw-swu-next", "swu_next"),
    )
    for tmp_k, uboot_k in key_map:
        val = defaults.get(tmp_k)
        if val is not None:
            env_map[uboot_k] = val
    _write_uboot_env(root, env_map)


def _create_whiteout_or_char(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        path.unlink()
    try:
        os.mknod(path, stat.S_IFCHR | 0o000, os.makedev(0, 0))
    except PermissionError:
        # Docker overlayfs blocks unprivileged 0:0 whiteout creation inside upperdir; use char 1:5 node.
        os.mknod(path, stat.S_IFCHR | 0o000, os.makedev(1, 5))


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--stock-sha256", required=True)
    parser.add_argument("--rollback-version", default=None)
    args = parser.parse_args(argv)

    stage_dir = Path("/stage")
    pack_report = json.loads((stage_dir / "ota_pack_report.json").read_text(encoding="utf-8"))
    img_path = stage_dir / f"CR0CN240110C10_ota_img_V{args.version}.img"
    img_bytes = img_path.read_bytes()

    with tempfile.TemporaryDirectory(prefix="k2-swu-check-") as td:
        root = Path(td) / "root"
        root.mkdir(parents=True, exist_ok=True)
        subprocess.run(["tar", "-xf", "/src/stock-chroot.tar", "-C", str(root)], check=True)
        for rel in ("tmp", "var/lock", "dev/by-name", "mnt/UDISK/printer_data/config", "overlay/upper", "proc"):
            (root / rel).mkdir(parents=True, exist_ok=True)
        _populate_dev_nodes(root)
        (root / "tmp").chmod(0o1777)
        _setup_slot_stubs(root)
        (root / "proc/cmdline").write_text(
            "console=ttyS0,115200 root=/dev/by-name/rootfsA rootwait\n",
            encoding="utf-8",
        )

        # Install chroot mocks under /tmp/mocks (bootenv, identity, df).
        mocks = root / "tmp/mocks"
        mocks.mkdir(parents=True, exist_ok=True)
        _install_bootenv_mocks(mocks)
        (mocks / "get_sn_mac.sh").write_text(
            '#!/bin/sh\n'
            'case "$1" in model) cat /tmp/sn-model;; board) cat /tmp/sn-board;; structure_version) cat /tmp/sn-struct;; *) exit 1;; esac\n',
            encoding="utf-8",
        )
        (mocks / "df").write_text(
            '#!/bin/sh\nprintf "Filesystem 1024-blocks Used Available Capacity Mounted on\\n"\n'
            'printf "rootfs 2097152 1048576 %s 50%% /mnt/UDISK\\n" "$(cat /tmp/df-free-kib)"\n',
            encoding="utf-8",
        )
        for mock_bin in ("fw_setenv", "get_sn_mac.sh", "df"):
            (mocks / mock_bin).chmod(0o755)

        _reset_env_files(root)
        target_img = root / "tmp/update.img"
        mock_env = {**os.environ, "PATH": "/tmp/mocks:/bin:/usr/bin:/sbin:/usr/sbin"}

        def run_swupdate(
            mode_flag: str,
            payload: bytes,
            selection: str,
            expect_ok: bool,
            *,
            require_substr: str | None = None,
        ) -> dict[str, object]:
            target_img.write_bytes(payload)
            cmd = ["chroot", str(root), "/sbin/swupdate"]
            if mode_flag == "-c":
                cmd.append("-c")
            elif mode_flag != "-i":
                raise ValueError(f"Unsupported mode_flag: {mode_flag}")
            cmd.extend(["-i", "/tmp/update.img", "-e", f"stable,{selection}", "-v"])
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                env=mock_env,
            )
            combined = f"{proc.stdout}\n{proc.stderr}"
            if "Usage /sbin/swupdate" in combined:
                raise RuntimeError(f"swupdate CLI invocation rejected by getopt:\n{combined}")
            ok = (proc.returncode == 0) == expect_ok
            if not ok:
                raise RuntimeError(
                    f"swupdate {mode_flag} unexpected rc={proc.returncode} (expect_ok={expect_ok}) "
                    f"for {selection}:\n{combined}"
                )
            if require_substr is not None and require_substr not in combined:
                raise RuntimeError(
                    f"swupdate output missing expected reason {require_substr!r}:\n{combined}"
                )
            return {"exit_code": proc.returncode, "expected_ok": expect_ok}

        swu_results: dict[str, object] = {
            "now_A_next_B": run_swupdate("-c", img_bytes, "now_A_next_B", True),
            "now_B_next_A": run_swupdate("-c", img_bytes, "now_B_next_A", True),
            "corrupted_rootfs_rejected": run_swupdate("-c", _corrupt_rootfs_payload(img_bytes), "now_A_next_B", False),
        }

        if list((root / "mnt/UDISK").glob("k2-mainline-backup.*")):
            raise AssertionError("swupdate -c executed preinstall script during check")
        for lbl, part in (("boot", 4), ("boot", 5), ("rootfs", 6), ("rootfs", 7)):
            if (root / f"dev/mmcblk0p{part}").read_bytes() != b"untouched":
                raise AssertionError(f"swupdate -c wrote to {lbl} device stub mmcblk0p{part}")

        # 1. Real /sbin/swupdate -i MUST fail in preinstall before writing ANY slot when wrong slot selection is passed.
        stale_marker = root / "overlay/upper/etc/init.d/klipper"
        stale_marker.parent.mkdir(parents=True, exist_ok=True)
        stale_marker.write_text("active-override\n", encoding="utf-8")
        swu_results["wrong_slot_install_rejected"] = run_swupdate(
            "-i", img_bytes, "now_B_next_A", False, require_substr="K2 mainline preinstall"
        )
        for part in (4, 5, 6, 7):
            if (root / f"dev/mmcblk0p{part}").read_bytes() != b"untouched":
                raise AssertionError(f"Wrong-slot swupdate -i wrote to device stub mmcblk0p{part}")
        if not stale_marker.is_file() or stale_marker.read_text(encoding="utf-8") != "active-override\n":
            raise AssertionError("Wrong-slot swupdate -i mutated /overlay/upper")
        if list((root / "mnt/UDISK").glob("k2-mainline-backup.*")):
            raise AssertionError("Wrong-slot swupdate -i created backup before rejecting")

        # 2. Cross-pair alias bootB -> mmcblk0p6 (active rootfsA): valid selection MUST fail BEFORE writing kernel!
        _setup_slot_stubs(root, cross_alias_bootB_to_rootfsA=True)
        _reset_env_files(root)
        swu_results["cross_alias_install_rejected"] = run_swupdate(
            "-i", img_bytes, "now_A_next_B", False, require_substr="alias"
        )
        for part in (4, 5, 6, 7):
            if (root / f"dev/mmcblk0p{part}").read_bytes() != b"untouched":
                raise AssertionError(f"Cross-alias bootB->rootfsA swupdate -i wrote mmcblk0p{part}!")
        if list((root / "mnt/UDISK").glob("k2-mainline-backup.*")):
            raise AssertionError("Cross-alias swupdate -i created backup before rejecting")
        _setup_slot_stubs(root)

        # 2b. Undersized bootB partition (512 bytes) MUST be rejected by /sbin/swupdate -i BEFORE writing kernel!
        (root / "sys/class/block/mmcblk0p5/size").write_text("1\n", encoding="utf-8")
        _reset_env_files(root)
        swu_results["small_boot_partition_rejected"] = run_swupdate(
            "-i", img_bytes, "now_A_next_B", False, require_substr="Insufficient block size for bootB"
        )
        for part in (4, 5, 6, 7):
            if (root / f"dev/mmcblk0p{part}").read_bytes() != b"untouched":
                raise AssertionError(f"Small-boot swupdate -i wrote mmcblk0p{part}!")
        if list((root / "mnt/UDISK").glob("k2-mainline-backup.*")):
            raise AssertionError("Small-boot swupdate -i created backup before rejecting")
        _setup_slot_stubs(root)

        # 3. Failed fw_setenv inside real /sbin/swupdate -i MUST leave bootenv on old slot AND overlay untouched!
        (root / "mnt/UDISK/printer_data/config/printer.cfg").write_text("# user\n", encoding="utf-8")
        _reset_env_files(root, {"fw-setenv-fail": "1"})
        swu_results["bootenv_fail_preserves_overlay"] = run_swupdate(
            "-i", img_bytes, "now_A_next_B", False, require_substr="K2 mainline postinstall"
        )
        if not stale_marker.is_file() or stale_marker.read_text(encoding="utf-8") != "active-override\n":
            raise AssertionError("Failed fw_setenv mutated shared /overlay/upper!")
        failed_env = _read_uboot_env(root)
        if (
            failed_env.get("boot_partition") != "bootA"
            or failed_env.get("root_partition") != "rootfsA"
            or failed_env.get("version") != "1.1.4.11"
        ):
            raise AssertionError(f"Failed fw_setenv partially mutated bootenv state: {failed_env}")
        for bkp in (root / "mnt/UDISK").glob("k2-mainline-backup.*"):
            shutil.rmtree(bkp)

        # 3b. EIO while staging /overlay/upper/etc/init.d/mcu_update MUST restore overlay AND leave U-Boot on bootA!
        stale_mcu_update = root / "overlay/upper/etc/init.d/mcu_update"
        stale_mcu_update.write_text("stale-mcu-update\n", encoding="utf-8")
        whiteout = root / "overlay/upper/usr/bin/klipper_mcu"
        _create_whiteout_or_char(whiteout)
        mv_wrapper = mocks / "mv"
        mv_wrapper.write_text(
            '#!/bin/sh\n'
            'case "$*" in\n'
            '  *etc/init.d/mcu_update*) echo "simulated EIO on mcu_update" >&2; exit 1 ;;\n'
            'esac\n'
            'exec /bin/mv "$@"\n',
            encoding="utf-8",
        )
        mv_wrapper.chmod(0o755)
        try:
            _setup_slot_stubs(root)
            _reset_env_files(root)
            swu_results["overlay_eio_preserves_slot_and_overlay"] = run_swupdate(
                "-i", img_bytes, "now_A_next_B", False, require_substr="Failed to stage overlay shadow"
            )
            eio_env = _read_uboot_env(root)
            if (
                eio_env.get("boot_partition") != "bootA"
                or eio_env.get("root_partition") != "rootfsA"
                or eio_env.get("version") != "1.1.4.11"
                or "swu_next" in eio_env
            ):
                raise AssertionError(f"EIO in postinstall left mutated U-Boot env: {eio_env}")
            if (
                stale_marker.read_text(encoding="utf-8") != "active-override\n"
                or stale_mcu_update.read_text(encoding="utf-8") != "stale-mcu-update\n"
                or not whiteout.exists()
            ):
                raise AssertionError("EIO in postinstall failed to restore previously staged overlay items")
        finally:
            if mv_wrapper.exists():
                mv_wrapper.unlink()
            if stale_mcu_update.exists():
                stale_mcu_update.unlink()
            for stg in (root / "overlay").glob(".k2-ota-stage.*"):
                shutil.rmtree(stg, ignore_errors=True)
            for bkp in (root / "mnt/UDISK").glob("k2-mainline-backup.*"):
                shutil.rmtree(bkp)

        # 3c. U-Boot readback mismatch MUST revert slot to A AND restore overlay; failing revert emits CRITICAL.
        _setup_slot_stubs(root)
        _reset_env_files(root, {"fw-setenv-corrupt": "1", "fw-setenv-revert-fail": "0"})
        swu_results["readback_mismatch_reverts_cleanly"] = run_swupdate(
            "-i", img_bytes, "now_A_next_B", False, require_substr="reverted to bootA/rootfsA"
        )
        rev_env = _read_uboot_env(root)
        if (
            rev_env.get("boot_partition") != "bootA"
            or rev_env.get("root_partition") != "rootfsA"
            or rev_env.get("version") != "1.1.4.11"
            or "swu_next" in rev_env
        ):
            raise AssertionError(f"Readback mismatch revert failed to restore slot A: {rev_env}")
        if stale_marker.read_text(encoding="utf-8") != "active-override\n" or not whiteout.exists():
            raise AssertionError("Readback mismatch revert failed to restore staged overlay")
        for bkp in (root / "mnt/UDISK").glob("k2-mainline-backup.*"):
            shutil.rmtree(bkp)

        _setup_slot_stubs(root)
        _reset_env_files(root, {"fw-setenv-corrupt": "1", "fw-setenv-revert-fail": "1"})
        swu_results["readback_mismatch_revert_fail_critical"] = run_swupdate(
            "-i", img_bytes, "now_A_next_B", False, require_substr="CRITICAL:"
        )
        for stg in (root / "overlay").glob(".k2-ota-stage.*"):
            shutil.rmtree(stg, ignore_errors=True)
        for bkp in (root / "mnt/UDISK").glob("k2-mainline-backup.*"):
            shutil.rmtree(bkp)

        # 4. Valid full /sbin/swupdate -i MUST write target B only, commit bootenv, clean shadows, keep SSH key.
        keep_file = root / "overlay/upper/etc/dropbear/authorized_keys"
        keep_file.parent.mkdir(parents=True, exist_ok=True)
        keep_file.write_text("ssh-rsa TEST\n", encoding="utf-8")
        stale_tree_file = root / "overlay/upper/usr/share/klipper/klippy/old.py"
        stale_tree_file.parent.mkdir(parents=True, exist_ok=True)
        stale_tree_file.write_text("# stale\n", encoding="utf-8")
        stale_ota = root / "overlay/upper/usr/share/k2-ota/manifest.json"
        stale_ota.parent.mkdir(parents=True, exist_ok=True)
        stale_ota.write_text("{}\n", encoding="utf-8")
        whiteout = root / "overlay/upper/usr/bin/klipper_mcu"
        _create_whiteout_or_char(whiteout)

        _setup_slot_stubs(root)
        _reset_env_files(root, {"fw-setenv-fail": "0"})
        swu_results["valid_install_commits_env_and_cleans_overlay"] = run_swupdate("-i", img_bytes, "now_A_next_B", True)
        if (root / "dev/mmcblk0p4").read_bytes() != b"untouched" or (root / "dev/mmcblk0p6").read_bytes() != b"untouched":
            raise AssertionError("Valid now_A_next_B swupdate -i modified active slot A!")
        if (root / "dev/mmcblk0p5").read_bytes() == b"untouched" or (root / "dev/mmcblk0p7").read_bytes() == b"untouched":
            raise AssertionError("Valid now_A_next_B swupdate -i did not write target slot B!")
        committed_env = _read_uboot_env(root)
        if (
            committed_env.get("boot_partition") != "bootB"
            or committed_env.get("root_partition") != "rootfsB"
            or committed_env.get("version") != args.version
            or committed_env.get("swu_next") != "reboot"
            or "swu_mode" in committed_env
        ):
            raise AssertionError(f"Valid swupdate -i did not commit expected U-Boot env vars: {committed_env}")
        if stale_marker.exists() or stale_tree_file.exists() or whiteout.exists() or (root / "overlay/upper/usr/share/k2-ota").exists():
            raise AssertionError("Valid swupdate -i failed to clean allowlisted overlay shadows or whiteout")
        if keep_file.read_text(encoding="utf-8") != "ssh-rsa TEST\n":
            raise AssertionError("Valid swupdate -i deleted unrelated SSH key in overlay")

        for bkp in (root / "mnt/UDISK").glob("k2-mainline-backup.*"):
            shutil.rmtree(bkp)
        _setup_slot_stubs(root)
        _reset_env_files(root)

        # Verify pure-Python CPIO rejects both corruption and truncation (both mid-archive and 16-byte tail).
        cpio_rejections = {"corruption": False, "truncation_1024": False, "truncation_16": False}
        for mode, bad_data in (
            ("corruption", _corrupt_rootfs_payload(img_bytes)),
            ("truncation_1024", img_bytes[:-1024]),
            ("truncation_16", img_bytes[:-16]),
        ):
            try:
                read_cpio_crc(bad_data)
            except ValueError:
                cpio_rejections[mode] = True
            else:
                raise AssertionError(f"read_cpio_crc accepted {mode}")

        # Verify preinstall.sh and postinstall.sh syntax and edge cases inside stock BusyBox /bin/sh.
        entries = read_cpio_crc(img_bytes)
        (root / "tmp/preinstall.sh").write_bytes(entries["preinstall.sh"])
        (root / "tmp/postinstall.sh").write_bytes(entries["postinstall.sh"])
        subprocess.run(["chroot", str(root), "/bin/sh", "-n", "/tmp/preinstall.sh"], check=True)
        subprocess.run(["chroot", str(root), "/bin/sh", "-n", "/tmp/postinstall.sh"], check=True)

        def run_shell(
            script: str,
            arg: str,
            overrides: dict[str, str | None] | None = None,
            *,
            cmdline: str | None = None,
        ) -> int:
            _reset_env_files(root, overrides)
            if cmdline is not None:
                (root / "proc/cmdline").write_text(cmdline + "\n", encoding="utf-8")
            else:
                (root / "proc/cmdline").write_text(
                    "console=ttyS0,115200 root=/dev/by-name/rootfsA rootwait\n",
                    encoding="utf-8",
                )
            return subprocess.run(
                ["chroot", str(root), "/bin/sh", f"/tmp/{script}", arg],
                capture_output=True,
                env=mock_env,
            ).returncode

        preflight_checks: dict[str, object] = {}
        for k, bad_v in (
            ("fw-board", "WRONG"),
            ("fw-version", "1.1.5.5"),
            ("fw-boot", "bootB"),
            ("fw-root", "rootfsB"),
            ("sn-model", "F012"),
            ("sn-board", "CR0CN240319C14"),
            ("sn-struct", "1"),
            ("df-free-kib", "1024"),
        ):
            rc = run_shell("preinstall.sh", "now_A_next_B", {k: bad_v})
            if rc == 0:
                raise AssertionError(f"preinstall.sh accepted invalid {k}={bad_v}")
            preflight_checks[f"reject_{k}"] = rc

        if run_shell("preinstall.sh", "now_B_next_A") == 0:
            raise AssertionError("preinstall.sh accepted now_B_next_A when active root is rootfsA")
        preflight_checks["reject_wrong_slot_selection"] = True

        if run_shell("preinstall.sh", "now_A_next_B", cmdline="console=ttyS0 root=/dev/by-name/rootfsB") == 0:
            raise AssertionError("preinstall.sh accepted /proc/cmdline mismatch")
        preflight_checks["reject_cmdline_mismatch"] = True

        _setup_slot_stubs(root, alias_rootfs=True)
        if run_shell("preinstall.sh", "now_A_next_B") == 0:
            raise AssertionError("preinstall.sh accepted aliased rootfsA == rootfsB block devices")
        preflight_checks["reject_aliased_rootfs"] = True

        _setup_slot_stubs(root, cross_alias_bootB_to_rootfsA=True)
        if run_shell("preinstall.sh", "now_A_next_B") == 0:
            raise AssertionError("preinstall.sh accepted cross-pair alias bootB == rootfsA")
        preflight_checks["reject_cross_alias_boot_root"] = True
        _setup_slot_stubs(root)

        (root / "sys/class/block/mmcblk0p5/size").write_text("1\n", encoding="utf-8")
        if run_shell("preinstall.sh", "now_A_next_B") == 0:
            raise AssertionError("preinstall.sh accepted 1-sector bootB partition")
        preflight_checks["reject_small_boot_partition"] = True
        _setup_slot_stubs(root)

        (root / "sys/class/block/mmcblk0p5/dev").write_text("1x:2junk\n", encoding="utf-8")
        if run_shell("preinstall.sh", "now_A_next_B") == 0:
            raise AssertionError("preinstall.sh accepted malformed block dev id 1x:2junk")
        preflight_checks["reject_malformed_dev_id"] = True
        _setup_slot_stubs(root)

        preinstall_txt = entries["preinstall.sh"].decode("utf-8")
        want_k_sec = (len(entries["kernel"]) + 511) // 512
        want_r_sec = (len(entries["rootfs"]) + 511) // 512
        if (
            f'KERNEL_MIN_SECTORS="{want_k_sec}"' not in preinstall_txt
            or f'ROOTFS_MIN_SECTORS="{want_r_sec}"' not in preinstall_txt
        ):
            raise AssertionError("preinstall.sh missing exact rendered sector thresholds")
        preflight_checks["kernel_min_sectors"] = want_k_sec
        preflight_checks["rootfs_min_sectors"] = want_r_sec

        # Active printing check via /tmp/klippy_uds mock socket inside chroot.
        uds_script = (
            "import json, os, socket, sys\n"
            "p = '/tmp/klippy_uds'\n"
            "if os.path.exists(p): os.unlink(p)\n"
            "srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "srv.bind(p); srv.listen(1)\n"
            "open('/tmp/uds.ready', 'w').write('1')\n"
            "conn, _ = srv.accept()\n"
            "conn.recv(4096)\n"
            "conn.sendall(json.dumps({'id': 9101, 'result': {'status': {'print_stats': {'state': 'printing'}, 'idle_timeout': {'state': 'Printing'}}}}).encode() + b'\\x03')\n"
            "conn.close(); srv.close()\n"
        )
        (root / "tmp/uds_mock.py").write_text(uds_script, encoding="utf-8")
        uds_proc = subprocess.Popen(
            ["chroot", str(root), "/usr/share/klippy-env/bin/python", "-B", "/tmp/uds_mock.py"],
            env=mock_env,
        )
        try:
            while not (root / "tmp/uds.ready").exists():
                pass
            rc_print = run_shell("preinstall.sh", "now_A_next_B")
            if rc_print == 0:
                raise AssertionError("preinstall.sh accepted OTA while printer state == printing")
            preflight_checks["reject_active_printing"] = rc_print
        finally:
            if uds_proc.poll() is None:
                uds_proc.kill()
            uds_proc.wait()
            for tmpf in (root / "tmp/klippy_uds", root / "tmp/uds.ready", root / "tmp/uds_mock.py"):
                if tmpf.exists() or tmpf.is_symlink():
                    tmpf.unlink()

        if list((root / "mnt/UDISK").glob("k2-mainline-backup.*")):
            raise AssertionError("Rejected preinstall.sh created backup")

        # Upgrade from 1.1.4.919 allowed; verify preinstall leaves overlay intact until postinstall.
        stale_marker.parent.mkdir(parents=True, exist_ok=True)
        stale_marker.write_text("stale\n", encoding="utf-8")
        _create_whiteout_or_char(whiteout)
        if run_shell("preinstall.sh", "now_A_next_B", {"fw-version": "1.1.4.919"}) != 0:
            raise AssertionError("preinstall.sh rejected upgrade from 1.1.4.919")
        backups = list((root / "mnt/UDISK").glob("k2-mainline-backup.*"))
        if len(backups) != 1 or not (backups[0] / "config.tar.gz").is_file() or not (backups[0] / "overlay.tar.gz").is_file():
            raise AssertionError("preinstall.sh backup incomplete")
        if not stale_marker.is_file() or not whiteout.exists():
            raise AssertionError("preinstall.sh mutated /overlay/upper prematurely")
        if run_shell("postinstall.sh", "now_A_next_B", {"fw-version": "1.1.4.919"}) != 0:
            raise AssertionError("postinstall.sh failed on 1.1.4.919 upgrade")
        if stale_marker.exists() or whiteout.exists():
            raise AssertionError("postinstall.sh left shadows behind")
        preflight_checks["accept_1_1_4_11_and_919"] = True
        for bkp in backups:
            shutil.rmtree(bkp)

        # Reject symlinked overlay parent directory and directory in place of file shadow.
        (root / "overlay/upper/usr/share/klipper/klippy").symlink_to("/etc")
        if run_shell("preinstall.sh", "now_A_next_B") == 0:
            raise AssertionError("preinstall.sh accepted symlink in overlay parent path")
        (root / "overlay/upper/usr/share/klipper/klippy").unlink()
        preflight_checks["reject_overlay_symlink"] = True

        (root / "overlay/upper/usr/bin/klipper_mcu").mkdir(parents=True, exist_ok=True)
        if run_shell("preinstall.sh", "now_A_next_B") == 0:
            raise AssertionError("preinstall.sh accepted directory in place of klipper_mcu file shadow")
        shutil.rmtree(root / "overlay/upper/usr/bin/klipper_mcu")
        preflight_checks["reject_file_shadow_dir"] = True

        # Verify patched /etc/init.d/mcu_update (pinned by SHA to packed rootfs) with stateful mcu_util model:
        # real mcu_util -u verifies & auto-starts app immediately, so subsequent -c/-g fails once app is running!
        patched_mcu_src = stage_dir / "patched_mcu_update"
        if hashlib.sha256(patched_mcu_src.read_bytes()).hexdigest() != pack_report["patched_mcu_update_sha256"]:
            raise AssertionError("patched_mcu_update SHA mismatch vs packed SquashFS rootfs")
        patched_mcu_dst = root / "etc/init.d/mcu_update"
        patched_mcu_dst.write_bytes(patched_mcu_src.read_bytes())
        patched_mcu_dst.chmod(0o755)

        for dev in ("dev/ttyS2", "dev/ttyS3", "dev/ttyS5"):
            (root / dev).write_bytes(b"")
        (root / "usr/bin/mcu_util_485").write_text(
            '#!/bin/sh\necho "FORBIDDEN mcu_util_485 CALLED: $*" >> /tmp/mcu_485.trap\nexit 99\n',
            encoding="utf-8",
        )
        (root / "usr/bin/mcu_util_485").chmod(0o755)
        (root / "usr/bin/mcu_util").write_text(
            '#!/bin/sh\n'
            'tty=""; mode=""; fw=""\n'
            'while [ $# -gt 0 ]; do\n'
            '  case "$1" in\n'
            '    -i) tty="$2"; shift 2;;\n'
            '    -c) mode="c"; shift;;\n'
            '    -g) mode="g"; shift;;\n'
            '    -s) mode="s"; shift;;\n'
            '    -u) mode="u"; shift;;\n'
            '    -f) fw="$2"; shift 2;;\n'
            '    *) shift;;\n'
            '  esac\n'
            'done\n'
            'if [ "$tty" = "/dev/ttyS2" ]; then tag="mcu0"; elif [ "$tty" = "/dev/ttyS3" ]; then tag="noz0"; else exit 2; fi\n'
            'state=$(cat "/tmp/mcu_${tag}_state" 2>/dev/null || echo app)\n'
            'fail_c=$(cat "/tmp/mcu_${tag}_fail_c" 2>/dev/null || echo 0)\n'
            'if [ "$state" != "boot" ]; then exit 2; fi\n'
            'if [ "$mode" = "c" ] && [ "$fail_c" = "1" ]; then exit 2; fi\n'
            'if [ "$mode" = "c" ] && [ "$fail_c" = "once" ]; then\n'
            '  echo 0 > "/tmp/mcu_${tag}_fail_c"; exit 2\n'
            'fi\n'
            'if [ "$mode" = "c" ]; then exit 0; fi\n'
            'if [ "$mode" = "g" ]; then cat "/tmp/mcu_${tag}_ver"; exit 0; fi\n'
            'if [ "$mode" = "s" ]; then\n'
            '  echo "startup:$tag" >> /tmp/mcu_ops.log\n'
            '  echo "app" > "/tmp/mcu_${tag}_state"\n'
            '  exit 0\n'
            'fi\n'
            'if [ "$mode" = "u" ]; then\n'
            '  echo "update:$tag:$fw" >> /tmp/mcu_ops.log\n'
            '  basename "$fw" .bin > "/tmp/mcu_${tag}_ver"\n'
            '  echo "app" > "/tmp/mcu_${tag}_state"\n'
            '  exit 0\n'
            'fi\n'
            'exit 2\n',
            encoding="utf-8",
        )
        (root / "usr/bin/mcu_util").chmod(0o755)

        (root / "sys/class/gpio/gpio140").mkdir(parents=True, exist_ok=True)
        (root / "sys/class/gpio/gpio140/value").write_text("1\n", encoding="utf-8")
        (root / "usr/bin/mcu_reset.sh").write_text(
            '#!/bin/sh\n'
            'fail=$(cat /tmp/mcu_reset_fail 2>/dev/null || echo 0)\n'
            'if [ "$fail" = "1" ]; then exit 1; fi\n'
            'if [ "$1" = "enable" ]; then\n'
            '  echo "reset:enable" >> /tmp/mcu_ops.log\n'
            '  echo "0" > /sys/class/gpio/gpio140/value\n'
            '  exit 0\n'
            'fi\n'
            'if [ $# -eq 0 ]; then\n'
            '  echo "reset:cycle" >> /tmp/mcu_ops.log\n'
            '  echo "0" > /sys/class/gpio/gpio140/value\n'
            '  echo "boot" > /tmp/mcu_mcu0_state\n'
            '  echo "boot" > /tmp/mcu_noz0_state\n'
            '  exit 0\n'
            'fi\n'
            'exit 1\n',
            encoding="utf-8",
        )
        (root / "usr/bin/mcu_reset.sh").chmod(0o755)
        (root / "tmp/mcu_reset_fail").write_text("0\n", encoding="utf-8")

        fw_f008 = root / "usr/share/klipper/fw/F008"
        shutil.rmtree(fw_f008, ignore_errors=True)
        fw_f008.mkdir(parents=True, exist_ok=True)
        (fw_f008 / "mcu0_140_G32-mcu0_022_950.bin").write_bytes(b"main-950")
        (fw_f008 / "noz0_130_G30-noz0_021_950.bin").write_bytes(b"noz-950")

        # Case 1: Transition BOTH MCUs from _000 -> _950 starting from running app (requires mcu_reset.sh).
        (root / "tmp/mcu_mcu0_ver").write_text("mcu0_140_G32-mcu0_022_000\n", encoding="utf-8")
        (root / "tmp/mcu_noz0_ver").write_text("noz0_130_G30-noz0_021_000\n", encoding="utf-8")
        (root / "tmp/mcu_mcu0_state").write_text("app\n", encoding="utf-8")
        (root / "tmp/mcu_noz0_state").write_text("app\n", encoding="utf-8")
        (root / "tmp/mcu_mcu0_fail_c").write_text("0\n", encoding="utf-8")
        (root / "tmp/mcu_noz0_fail_c").write_text("0\n", encoding="utf-8")
        for p in (root / "tmp/mcu_ops.log", root / "tmp/mcu_485.trap", root / "tmp/.mcu_version"):
            if p.exists():
                p.unlink()
        proc_up = subprocess.run(
            ["chroot", str(root), "/bin/sh", "-c", ". /etc/init.d/mcu_update; start_service"],
            capture_output=True,
            text=True,
            env=mock_env,
        )
        if proc_up.returncode != 0:
            raise AssertionError(f"Patched mcu_update failed valid transition: {proc_up.stdout}\n{proc_up.stderr}")
        if (root / "tmp/mcu_485.trap").exists():
            raise AssertionError(f"FORBIDDEN mcu_util_485 invoked: {(root / 'tmp/mcu_485.trap').read_text()}")
        up_ops = (root / "tmp/mcu_ops.log").read_text(encoding="utf-8").splitlines()
        if up_ops != [
            "reset:enable",
            "reset:cycle",
            "update:mcu0:/usr/share/klipper/fw/F008/mcu0_140_G32-mcu0_022_950.bin",
            "update:noz0:/usr/share/klipper/fw/F008/noz0_130_G30-noz0_021_950.bin",
        ]:
            raise AssertionError(f"Unexpected ops during _000 -> _950 transition: {up_ops}")
        mcu_ver_json = json.loads((root / "tmp/.mcu_version").read_text(encoding="utf-8"))
        if (
            mcu_ver_json["mcu0"]["version"] != "mcu0_140_G32-mcu0_022_950"
            or mcu_ver_json["noz0"]["version"] != "noz0_130_G30-noz0_021_950"
            or (root / "tmp/mcu_mcu0_state").read_text(encoding="utf-8").strip() != "app"
            or (root / "tmp/mcu_noz0_state").read_text(encoding="utf-8").strip() != "app"
        ):
            raise AssertionError(f"Unexpected MCU state/version after transition: {mcu_ver_json}")

        # Case 2: Re-run already on _950 -> power-reset then startup_app only, zero flash updates.
        (root / "tmp/mcu_mcu0_state").write_text("app\n", encoding="utf-8")
        (root / "tmp/mcu_noz0_state").write_text("app\n", encoding="utf-8")
        (root / "tmp/mcu_ops.log").write_text("", encoding="utf-8")
        proc_idem = subprocess.run(
            ["chroot", str(root), "/bin/sh", "-c", ". /etc/init.d/mcu_update; start_service"],
            capture_output=True,
            text=True,
            env=mock_env,
        )
        if proc_idem.returncode != 0:
            raise AssertionError("Patched mcu_update failed on idempotent _950 startup")
        ops_lines = (root / "tmp/mcu_ops.log").read_text(encoding="utf-8").splitlines()
        if ops_lines != ["reset:enable", "reset:cycle", "startup:mcu0", "startup:noz0"]:
            raise AssertionError(f"Unexpected ops on idempotent startup: {ops_lines}")

        (root / "tmp/mcu_noz0_fail_c").write_text("once\n", encoding="utf-8")
        (root / "tmp/mcu_ops.log").write_text("", encoding="utf-8")
        subprocess.run(
            ["chroot", str(root), "/bin/sh", "-c", ". /etc/init.d/mcu_update; start_service"],
            capture_output=True,
            text=True,
            env=mock_env,
            check=True,
        )
        retry_ops = (root / "tmp/mcu_ops.log").read_text(encoding="utf-8").splitlines()
        two_resets = ["reset:enable", "reset:cycle"] * 2
        if retry_ops != [*two_resets, "startup:mcu0", "startup:noz0"]:
            raise AssertionError(f"Unexpected ops on transient nozzle probe failure: {retry_ops}")

        # Case 3: Nozzle handshake failure -> MUST abort BEFORE flashing MAIN AND never invoke mcu_util_485!
        (root / "tmp/mcu_mcu0_ver").write_text("mcu0_140_G32-mcu0_022_000\n", encoding="utf-8")
        (root / "tmp/mcu_noz0_ver").write_text("noz0_130_G30-noz0_021_000\n", encoding="utf-8")
        (root / "tmp/mcu_mcu0_state").write_text("app\n", encoding="utf-8")
        (root / "tmp/mcu_noz0_state").write_text("app\n", encoding="utf-8")
        (root / "tmp/mcu_noz0_fail_c").write_text("1\n", encoding="utf-8")
        (root / "tmp/mcu_ops.log").write_text("", encoding="utf-8")
        proc_fail_noz = subprocess.run(
            ["chroot", str(root), "/bin/sh", "-c", ". /etc/init.d/mcu_update; start_service"],
            capture_output=True,
            text=True,
            env=mock_env,
        )
        fail_noz_ops = (root / "tmp/mcu_ops.log").read_text(encoding="utf-8").splitlines()
        if proc_fail_noz.returncode == 0 or fail_noz_ops != two_resets:
            raise AssertionError(
                f"Patched mcu_update flashed or exited 0 despite nozzle handshake failure: "
                f"rc={proc_fail_noz.returncode} ops={fail_noz_ops!r}"
            )
        if (root / "tmp/mcu_485.trap").exists():
            raise AssertionError("Nozzle failure path invoked forbidden mcu_util_485!")

        # Case 4: Wrong hardware prefix on nozzle -> MUST abort BEFORE flashing either MCU!
        (root / "tmp/mcu_noz0_fail_c").write_text("0\n", encoding="utf-8")
        (root / "tmp/mcu_noz0_ver").write_text("bed0_100_G30-bed0_000_001\n", encoding="utf-8")
        (root / "tmp/mcu_mcu0_state").write_text("app\n", encoding="utf-8")
        (root / "tmp/mcu_noz0_state").write_text("app\n", encoding="utf-8")
        (root / "tmp/mcu_ops.log").write_text("", encoding="utf-8")
        proc_wrong_hw = subprocess.run(
            ["chroot", str(root), "/bin/sh", "-c", ". /etc/init.d/mcu_update; start_service"],
            capture_output=True,
            text=True,
            env=mock_env,
        )
        wrong_hw_ops = (root / "tmp/mcu_ops.log").read_text(encoding="utf-8").splitlines()
        if proc_wrong_hw.returncode == 0 or wrong_hw_ops != ["reset:enable", "reset:cycle"]:
            raise AssertionError(
                f"Patched mcu_update did not abort before flash on wrong hardware prefix: {wrong_hw_ops!r}"
            )

        # Case 5: Missing NOZZLE .bin -> MUST fail in Phase 1 BEFORE flashing MAIN!
        noz_bin_path = fw_f008 / "noz0_130_G30-noz0_021_950.bin"
        noz_bin_backup = noz_bin_path.read_bytes()
        (root / "tmp/mcu_mcu0_ver").write_text("mcu0_140_G32-mcu0_022_000\n", encoding="utf-8")
        (root / "tmp/mcu_noz0_ver").write_text("noz0_130_G30-noz0_021_000\n", encoding="utf-8")
        (root / "tmp/mcu_mcu0_state").write_text("app\n", encoding="utf-8")
        (root / "tmp/mcu_noz0_state").write_text("app\n", encoding="utf-8")
        (root / "tmp/mcu_ops.log").write_text("", encoding="utf-8")
        noz_bin_path.unlink()
        try:
            proc_missing_noz = subprocess.run(
                ["chroot", str(root), "/bin/sh", "-c", ". /etc/init.d/mcu_update; start_service"],
                capture_output=True,
                text=True,
                env=mock_env,
            )
            missing_noz_ops = (root / "tmp/mcu_ops.log").read_text(encoding="utf-8").splitlines()
            if proc_missing_noz.returncode == 0 or missing_noz_ops != ["reset:enable", "reset:cycle"]:
                raise AssertionError(
                    f"Patched mcu_update did not abort before MAIN flash on missing NOZZLE .bin: "
                    f"rc={proc_missing_noz.returncode} ops={missing_noz_ops!r}"
                )
            if (root / "tmp/mcu_485.trap").exists():
                raise AssertionError("Missing NOZZLE path invoked forbidden mcu_util_485!")
        finally:
            noz_bin_path.write_bytes(noz_bin_backup)

        # Case 6: mcu_reset failure -> MUST abort BEFORE any probe or flash!
        (root / "tmp/mcu_mcu0_state").write_text("app\n", encoding="utf-8")
        (root / "tmp/mcu_noz0_state").write_text("app\n", encoding="utf-8")
        (root / "tmp/mcu_reset_fail").write_text("1\n", encoding="utf-8")
        (root / "tmp/mcu_ops.log").write_text("", encoding="utf-8")
        try:
            proc_reset_fail = subprocess.run(
                ["chroot", str(root), "/bin/sh", "-c", ". /etc/init.d/mcu_update; start_service"],
                capture_output=True,
                text=True,
                env=mock_env,
            )
            reset_fail_ops = (root / "tmp/mcu_ops.log").read_text(encoding="utf-8")
            if proc_reset_fail.returncode == 0 or reset_fail_ops != "":
                raise AssertionError(
                    f"Patched mcu_update did not abort on mcu_reset failure: "
                    f"rc={proc_reset_fail.returncode} ops={reset_fail_ops!r}"
                )
            if (root / "tmp/mcu_485.trap").exists():
                raise AssertionError("Reset failure path invoked forbidden mcu_util_485!")
        finally:
            (root / "tmp/mcu_reset_fail").write_text("0\n", encoding="utf-8")

        # Verify compare_version logic compatibility directly (including missing/duplicate failure).
        fw_tmp = root / "tmp/fw_check"
        fw_tmp.mkdir(parents=True, exist_ok=True)
        mcu_checks: dict[str, str] = {}
        for installed, avail, expected in (
            ("mcu0_140_G32-mcu0_022_000", "mcu0_140_G32-mcu0_022_950.bin", "mcu0_140_G32-mcu0_022_950.bin"),
            ("mcu0_140_G32-mcu0_022_950", "mcu0_140_G32-mcu0_022_950.bin", ""),
            ("noz0_130_G30-noz0_021_000", "noz0_130_G30-noz0_021_950.bin", "noz0_130_G30-noz0_021_950.bin"),
            ("noz0_130_G30-noz0_021_950", "noz0_130_G30-noz0_021_950.bin", ""),
            ("cfs0_050_G32-cfs0_000_113", "cfs0_050_G32-cfs0_000_113.bin", ""),
        ):
            for p in fw_tmp.iterdir():
                p.unlink()
            (fw_tmp / avail).write_bytes(b"fixture")
            proc = subprocess.run(
                [
                    "chroot",
                    str(root),
                    "/bin/sh",
                    "-c",
                    '. /etc/init.d/mcu_update; write_log() { :; }; compare_version "$1" /tmp/fw_check false',
                    "sh",
                    installed,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            got = proc.stdout.strip()
            want = f"/tmp/fw_check/{expected}" if expected else ""
            if got != want:
                raise AssertionError(f"mcu_update compare_version({installed}) returned {got!r}, expected {want!r}")
            mcu_checks[installed] = got

        # Missing & duplicate .bin must make compare_version fail (non-zero, empty stdout).
        for p in fw_tmp.iterdir():
            p.unlink()
        proc_empty_fw = subprocess.run(
            [
                "chroot",
                str(root),
                "/bin/sh",
                "-c",
                '. /etc/init.d/mcu_update; write_log() { :; }; compare_version "$1" /tmp/fw_check false',
                "sh",
                "noz0_130_G30-noz0_021_000",
            ],
            capture_output=True,
            text=True,
        )
        if proc_empty_fw.returncode == 0 or proc_empty_fw.stdout.strip() != "":
            raise AssertionError(f"compare_version did not fail on missing .bin: rc={proc_empty_fw.returncode}")
        (fw_tmp / "mcu0_140_G32-mcu0_022_950.bin").write_bytes(b"a")
        (fw_tmp / "mcu0_140_G32-mcu0_022_951.bin").write_bytes(b"b")
        proc_dup_fw = subprocess.run(
            [
                "chroot",
                str(root),
                "/bin/sh",
                "-c",
                '. /etc/init.d/mcu_update; write_log() { :; }; compare_version "$1" /tmp/fw_check false',
                "sh",
                "mcu0_140_G32-mcu0_022_000",
            ],
            capture_output=True,
            text=True,
        )
        if proc_dup_fw.returncode == 0 or proc_dup_fw.stdout.strip() != "":
            raise AssertionError(f"compare_version did not fail on duplicate .bin: rc={proc_dup_fw.returncode}")

        # Verify rollback_postinstall.sh: incomplete/nested-broken backup rejected cleanly, complete backup restored.
        rollback_checks: dict[str, object] = {}
        rb_entries = None
        if args.rollback_version:
            rb_path = stage_dir / f"CR0CN240110C10_ota_img_V{args.rollback_version}.img"
            rb_bytes = rb_path.read_bytes()
            swu_results["rollback_now_A_next_B"] = run_swupdate("-c", rb_bytes, "now_A_next_B", True)
            rb_entries = read_cpio_crc(rb_bytes)
        rb_script_bytes = (
            rb_entries["rollback_postinstall.sh"]
            if rb_entries is not None
            else Path("/src/k2_mainline/ota/rollback_postinstall.sh.tmpl")
            .read_text(encoding="utf-8")
            .replace("@TARGET_VERSION@", "1.1.4.11")
            .encode("utf-8")
        )
        (root / "tmp/rollback_postinstall.sh").write_bytes(rb_script_bytes)
        subprocess.run(["chroot", str(root), "/bin/sh", "-n", "/tmp/rollback_postinstall.sh"], check=True)

        cfg_dir = root / "mnt/UDISK/printer_data/config"
        # Case A: Incomplete 1.1.4.11 backup (missing gcode_macro.cfg etc.) + .950 gcode_macro.cfg -> MUST reject backup
        # and remove overlapped .950 configs so stock ROM seeds a clean matching 1.1.4.11 set!
        (cfg_dir / "printer.cfg").write_text("# Version: v9.5.0\n[load_cell_probe]\n", encoding="utf-8")
        (cfg_dir / "gcode_macro.cfg").write_text("# 950 macro\nchamber_fan_target\n", encoding="utf-8")
        (cfg_dir / "k2_load_cell.cfg").write_text("# 950\n", encoding="utf-8")
        bad_src = Path(td) / "rb_incomplete_src/printer_data/config"
        bad_src.mkdir(parents=True, exist_ok=True)
        (bad_src / "printer.cfg").write_text("# Version: v1.1.4\n[include gcode_macro.cfg]\n[prtouch_v3]\n", encoding="utf-8")
        bad_bkp = root / "mnt/UDISK/k2-mainline-backup.BAD114"
        bad_bkp.mkdir(parents=True, exist_ok=True)
        (bad_bkp / "version.txt").write_text("1.1.4.11\n", encoding="utf-8")
        with tarfile.open(bad_bkp / "config.tar.gz", "w:gz") as tf:
            tf.add(bad_src.parent, arcname="printer_data")
        if run_shell("rollback_postinstall.sh", "now_A_next_B") != 0:
            raise AssertionError("rollback_postinstall.sh failed on incomplete backup fallback")
        for leftover in ("printer.cfg", "gcode_macro.cfg", "k2_load_cell.cfg"):
            if (cfg_dir / leftover).exists():
                raise AssertionError(f"rollback left mixed {leftover} behind after incomplete backup")
        rollback_checks["incomplete_backup_rejected_cleanly"] = True
        shutil.rmtree(bad_bkp)

        # Case A2: All 6 top-level files present, but gcode_macro.cfg has nested [include vendor_local.cfg] missing -> reject!
        (cfg_dir / "printer.cfg").write_text("# Version: v9.5.0\n[load_cell_probe]\n", encoding="utf-8")
        (cfg_dir / "gcode_macro.cfg").write_text("# 950 macro\n", encoding="utf-8")
        (cfg_dir / "k2_load_cell.cfg").write_text("# 950\n", encoding="utf-8")
        nested_bad_src = Path(td) / "rb_nested_bad_src/printer_data/config"
        nested_bad_src.mkdir(parents=True, exist_ok=True)
        (nested_bad_src / "printer.cfg").write_text(
            "# Version: v1.1.4\n"
            "[include gcode_macro.cfg]\n[include printer_params.cfg]\n"
            "[include sensorless.cfg]\n[include motor_control.cfg]\n[include box.cfg]\n"
            "[prtouch_v3]\n",
            encoding="utf-8",
        )
        (nested_bad_src / "gcode_macro.cfg").write_text(
            "[include vendor_local.cfg]\n[gcode_macro M106]\ngcode:\n  M107\n",
            encoding="utf-8",
        )
        for req_name in ("printer_params.cfg", "sensorless.cfg", "motor_control.cfg", "box.cfg"):
            (nested_bad_src / req_name).write_text(f"# stock 1.1.4.11 {req_name}\n", encoding="utf-8")
        nested_bad_bkp = root / "mnt/UDISK/k2-mainline-backup.NBAD114"
        nested_bad_bkp.mkdir(parents=True, exist_ok=True)
        (nested_bad_bkp / "version.txt").write_text("1.1.4.11\n", encoding="utf-8")
        with tarfile.open(nested_bad_bkp / "config.tar.gz", "w:gz") as tf:
            tf.add(nested_bad_src.parent, arcname="printer_data")
        if run_shell("rollback_postinstall.sh", "now_A_next_B") != 0:
            raise AssertionError("rollback_postinstall.sh failed on nested missing include fallback")
        for leftover in ("printer.cfg", "gcode_macro.cfg", "k2_load_cell.cfg"):
            if (cfg_dir / leftover).exists():
                raise AssertionError(f"rollback left mixed {leftover} after rejecting nested missing include")
        rollback_checks["nested_missing_include_rejected"] = True
        shutil.rmtree(nested_bad_bkp)

        # Case A3: Malformed INI line inside included [section] MUST be rejected by configparser validation!
        (cfg_dir / "printer.cfg").write_text("# Version: v9.5.0\n[load_cell_probe]\n", encoding="utf-8")
        (cfg_dir / "gcode_macro.cfg").write_text("# 950 macro\n", encoding="utf-8")
        (cfg_dir / "k2_load_cell.cfg").write_text("# 950\n", encoding="utf-8")
        ini_bad_src = Path(td) / "rb_ini_bad_src/printer_data/config"
        ini_bad_src.mkdir(parents=True, exist_ok=True)
        (ini_bad_src / "printer.cfg").write_text(
            "# Version: v1.1.4\n"
            "[include gcode_macro.cfg]\n[include printer_params.cfg]\n"
            "[include sensorless.cfg]\n[include motor_control.cfg]\n[include box.cfg]\n"
            "[prtouch_v3]\n",
            encoding="utf-8",
        )
        (ini_bad_src / "gcode_macro.cfg").write_text(
            "[include vendor_local.cfg]\n[gcode_macro M106]\ngcode:\n  M107\n",
            encoding="utf-8",
        )
        (ini_bad_src / "vendor_local.cfg").write_text("[gcode_macro BAD]\nnot_an_option\n", encoding="utf-8")
        for req_name in ("printer_params.cfg", "sensorless.cfg", "motor_control.cfg", "box.cfg"):
            (ini_bad_src / req_name).write_text(f"# stock 1.1.4.11 {req_name}\n", encoding="utf-8")
        ini_bad_bkp = root / "mnt/UDISK/k2-mainline-backup.INIBAD"
        ini_bad_bkp.mkdir(parents=True, exist_ok=True)
        (ini_bad_bkp / "version.txt").write_text("1.1.4.11\n", encoding="utf-8")
        with tarfile.open(ini_bad_bkp / "config.tar.gz", "w:gz") as tf:
            tf.add(ini_bad_src.parent, arcname="printer_data")
        if run_shell("rollback_postinstall.sh", "now_A_next_B") != 0:
            raise AssertionError("rollback_postinstall.sh failed on malformed INI fallback")
        for leftover in ("printer.cfg", "gcode_macro.cfg", "k2_load_cell.cfg", "vendor_local.cfg"):
            if (cfg_dir / leftover).exists():
                raise AssertionError(f"rollback left mixed {leftover} after rejecting malformed INI")
        rollback_checks["malformed_ini_backup_rejected"] = True
        shutil.rmtree(ini_bad_bkp)

        # Case B0: EIO during rollback overlay staging MUST leave U-Boot on bootA AND restore .950 configs/overlay!
        (cfg_dir / "printer.cfg").write_text("# Version: v9.5.0\n[load_cell_probe]\n", encoding="utf-8")
        (cfg_dir / "k2_load_cell.cfg").write_text("# 950\n", encoding="utf-8")
        stale_mcu_update.write_text("stale-mcu-rb\n", encoding="utf-8")
        mv_wrapper.write_text(
            '#!/bin/sh\n'
            'case "$*" in\n'
            '  *etc/init.d/mcu_update*) echo "simulated EIO during rollback" >&2; exit 1 ;;\n'
            'esac\n'
            'exec /bin/mv "$@"\n',
            encoding="utf-8",
        )
        mv_wrapper.chmod(0o755)
        try:
            if run_shell("rollback_postinstall.sh", "now_A_next_B") == 0:
                raise AssertionError("rollback_postinstall.sh exited 0 despite EIO in overlay staging")
            rb_eio_env = _read_uboot_env(root)
            if (
                rb_eio_env.get("boot_partition") != "bootA"
                or rb_eio_env.get("root_partition") != "rootfsA"
                or rb_eio_env.get("version") != "1.1.4.11"
            ):
                raise AssertionError(f"rollback EIO left mutated U-Boot env: {rb_eio_env}")
            if (
                (cfg_dir / "k2_load_cell.cfg").read_text(encoding="utf-8") != "# 950\n"
                or "v9.5.0" not in (cfg_dir / "printer.cfg").read_text(encoding="utf-8")
                or stale_mcu_update.read_text(encoding="utf-8") != "stale-mcu-rb\n"
            ):
                raise AssertionError("rollback EIO failed to restore configs or overlay")
            rollback_checks["rollback_eio_preserves_slot_and_configs"] = True
        finally:
            if mv_wrapper.exists():
                mv_wrapper.unlink()
            if stale_mcu_update.exists():
                stale_mcu_update.unlink()
            for stg in (root / "overlay").glob(".k2-ota-stage.*"):
                shutil.rmtree(stg, ignore_errors=True)
            for stg in (root / "mnt/UDISK").glob(".rollback-prev-cfg.*"):
                shutil.rmtree(stg, ignore_errors=True)

        # Case B: Complete 6-file verified 1.1.4.11 backup with valid nested include -> MUST restore all files cleanly.
        (cfg_dir / "printer.cfg").write_text("# Version: v9.5.0\n[load_cell_probe]\n", encoding="utf-8")
        (cfg_dir / "gcode_macro.cfg").write_text("# 950 macro\n", encoding="utf-8")
        (cfg_dir / "k2_load_cell.cfg").write_text("# 950\n", encoding="utf-8")
        stock_src = Path(td) / "rb_stock_src/printer_data/config"
        stock_src.mkdir(parents=True, exist_ok=True)
        (stock_src / "printer.cfg").write_text(
            "# Version: v1.1.4\n"
            "[include gcode_macro.cfg]\n[include printer_params.cfg]\n"
            "[include sensorless.cfg]\n[include motor_control.cfg]\n[include box.cfg]\n"
            "[prtouch_v3]\n",
            encoding="utf-8",
        )
        (stock_src / "gcode_macro.cfg").write_text(
            "# stock 1.1.4.11 gcode_macro.cfg\n[include vendor_local.cfg]\n",
            encoding="utf-8",
        )
        (stock_src / "vendor_local.cfg").write_text("# stock 1.1.4.11 vendor_local.cfg\n", encoding="utf-8")
        (stock_src / "printer.cfg.1").write_text("# stock extra backup file\n", encoding="utf-8")
        for req_name in ("printer_params.cfg", "sensorless.cfg", "motor_control.cfg", "box.cfg"):
            (stock_src / req_name).write_text(f"# stock 1.1.4.11 {req_name}\n", encoding="utf-8")
        rb_backup = root / "mnt/UDISK/k2-mainline-backup.RB1141"
        rb_backup.mkdir(parents=True, exist_ok=True)
        (rb_backup / "version.txt").write_text("1.1.4.11\n", encoding="utf-8")
        with tarfile.open(rb_backup / "config.tar.gz", "w:gz") as tf:
            tf.add(stock_src.parent, arcname="printer_data")

        # Case B1: fw_setenv failure during rollback MUST preserve pre-existing printer.cfg.1 & .950 configs!
        (cfg_dir / "printer.cfg").write_text("# Version: v9.5.0\n[load_cell_probe]\n", encoding="utf-8")
        (cfg_dir / "gcode_macro.cfg").write_text("# 950 macro\n", encoding="utf-8")
        (cfg_dir / "k2_load_cell.cfg").write_text("# 950\n", encoding="utf-8")
        (cfg_dir / "printer.cfg.1").write_text("# pre-rollback printer.cfg.1\n", encoding="utf-8")
        if run_shell("rollback_postinstall.sh", "now_A_next_B", {"fw-setenv-fail": "1"}) == 0:
            raise AssertionError("rollback_postinstall.sh exited 0 despite fw_setenv failure")
        if (cfg_dir / "printer.cfg.1").read_text(encoding="utf-8") != "# pre-rollback printer.cfg.1\n":
            raise AssertionError("rollback fw_setenv failure deleted or overwrote pre-existing printer.cfg.1!")
        if "v9.5.0" not in (cfg_dir / "printer.cfg").read_text(encoding="utf-8"):
            raise AssertionError("rollback fw_setenv failure did not restore .950 printer.cfg")
        rollback_checks["rollback_fw_fail_preserves_extra_files"] = True
        (cfg_dir / "printer.cfg.1").unlink()

        if run_shell("rollback_postinstall.sh", "now_A_next_B") != 0:
            raise AssertionError("rollback_postinstall.sh failed on complete backup")
        if (cfg_dir / "k2_load_cell.cfg").exists():
            raise AssertionError("rollback_postinstall.sh left k2_load_cell.cfg behind")
        for req_name in ("printer_params.cfg", "sensorless.cfg", "motor_control.cfg", "box.cfg", "vendor_local.cfg"):
            if (cfg_dir / req_name).read_text(encoding="utf-8") != f"# stock 1.1.4.11 {req_name}\n":
                raise AssertionError(f"rollback failed to restore stock {req_name}")
        if "[include vendor_local.cfg]" not in (cfg_dir / "gcode_macro.cfg").read_text(encoding="utf-8"):
            raise AssertionError("rollback failed to restore stock gcode_macro.cfg")
        if "[prtouch_v3]" not in (cfg_dir / "printer.cfg").read_text(encoding="utf-8"):
            raise AssertionError("rollback_postinstall.sh failed to restore stock 1.1.4.11 printer.cfg")
        rb_env = _read_uboot_env(root)
        if rb_env.get("version") != "1.1.4.11" or rb_env.get("boot_partition") != "bootB":
            raise AssertionError(f"rollback_postinstall.sh failed to commit version=1.1.4.11: {rb_env}")
        if not list((cfg_dir / "backups").glob("pre-rollback-*")):
            raise AssertionError("rollback_postinstall.sh did not archive .950 configs before rollback")
        rollback_checks["restored_from_1_1_4_11_backup"] = True

    report = {
        "passed": True,
        "swupdate_cli_checks": swu_results,
        "cpio_parser_rejections": cpio_rejections,
        "preflight_checks": preflight_checks,
        "mcu_update_checks": {
            "transition_main_and_nozzle": mcu_ver_json,
            "idempotent_ops": ops_lines,
            "retry_transient_probe_before_writes": True,
            "reject_nozzle_fail_without_485": True,
            "reject_wrong_hw_prefix": True,
            "reject_missing_nozzle_fw": True,
            "reject_reset_fail": True,
            "compare_version_fail_on_missing_or_dup": True,
            "compare_version": mcu_checks,
        },
        "mcu_update_compare_version": mcu_checks,
        "rollback_checks": rollback_checks,
    }
    (stage_dir / "swupdate_verify_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
