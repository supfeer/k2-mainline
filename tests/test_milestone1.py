"""Automated verification suite for k2_mainline Milestone 1."""

from __future__ import annotations

import filecmp
import json
import os
import re
import struct
import sys
import tempfile
import unittest
from pathlib import Path

K2_MAINLINE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = K2_MAINLINE_DIR.parent
sys.path.insert(0, str(K2_MAINLINE_DIR / "scripts"))
sys.path.insert(0, str(K2_MAINLINE_DIR / "mcu"))

from common import get_build_root, load_upstream_manifest, sha256_file  # noqa: E402
from pack_mcu_bin import HEADER_END, pack_mcu_bin, verify_packed_bin  # noqa: E402


def _read_elf32_arm_header(path: Path) -> tuple[int, int, int]:
    """Return (ei_class, ei_data, e_machine) from an ELF header."""
    data = path.read_bytes()
    if len(data) < 20 or data[:4] != b"\x7fELF":
        raise AssertionError(f"{path} is not a valid ELF file")
    ei_class = data[4]
    ei_data = data[5]
    e_machine = struct.unpack_from("<H", data, 18)[0]
    return ei_class, ei_data, e_machine


class Milestone1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_upstream_manifest()
        cls.build_root = get_build_root(cls.manifest)

    def test_external_cache_hashes(self) -> None:
        klip = self.manifest["klipper"]
        tarball = self.build_root / "cache" / klip["tarball_name"]
        self.assertTrue(tarball.is_file(), f"Missing Klipper tarball: {tarball}")
        self.assertEqual(sha256_file(tarball), klip["sha256"])
        self.assertEqual(klip["tarball_top_dir"], f"klipper-{klip['commit']}")

        lds = (K2_MAINLINE_DIR / "mcu" / "src" / "gd32" / "gd32_link.lds.S").read_text(encoding="utf-8")
        self.assertIn(
            "CONFIG_FLASH_SIZE - (CONFIG_FLASH_APPLICATION_ADDRESS - CONFIG_FLASH_BOOT_ADDRESS)",
            lds,
        )

        ota = self.manifest["stock_ota"]
        ota_path = self.build_root / "cache" / ota["name"]
        self.assertTrue(ota_path.is_file(), f"Missing stock OTA image: {ota_path}")
        self.assertEqual(sha256_file(ota_path), ota["sha256"])

        moon = self.manifest["moonraker"]
        moon_path = self.build_root / "cache" / moon["tarball_name"]
        self.assertTrue(moon_path.is_file(), f"Missing Moonraker tarball: {moon_path}")
        self.assertEqual(sha256_file(moon_path), moon["sha256"])

        for key in ("fluidd", "mainsail", "static_curl", "go2rtc"):
            cfg = self.manifest[key]
            p = self.build_root / "cache" / cfg["filename"]
            self.assertTrue(p.is_file(), f"Missing {key} archive: {p}")
            self.assertEqual(sha256_file(p), cfg["sha256"])

    def test_repo_cleanliness_and_ascii_code_policy(self) -> None:
        forbidden_suffixes = {".bin", ".so", ".elf", ".o", ".img", ".tar.gz", ".gz"}
        max_file_bytes = 512 * 1024
        for p in K2_MAINLINE_DIR.rglob("*"):
            if not p.is_file():
                continue
            self.assertNotIn(
                p.suffix.lower(),
                forbidden_suffixes,
                f"Binary/archive artifact found inside repo: {p}",
            )
            self.assertLessEqual(
                p.stat().st_size,
                max_file_bytes,
                f"Oversized file inside repo ({p.stat().st_size} bytes): {p}",
            )

        for code_dir in (
            K2_MAINLINE_DIR / "native",
            K2_MAINLINE_DIR / "mcu" / "src" / "gd32",
            K2_MAINLINE_DIR / "ota",
            K2_MAINLINE_DIR / "scripts",
            K2_MAINLINE_DIR / "services",
            K2_MAINLINE_DIR / "tests",
        ):
            for p in code_dir.rglob("*"):
                if not p.is_file():
                    continue
                non_ascii = [i for i, b in enumerate(p.read_bytes()) if b > 127]
                self.assertEqual(
                    non_ascii,
                    [],
                    f"Non-ASCII byte(s) in English code file {p} at offsets {non_ascii[:5]}",
                )

        # Verify k2_sync_config.py is strictly ASCII, and all comments/non-UI files in host/ are pure ASCII.
        sync_bytes = (K2_MAINLINE_DIR / "config" / "k2_sync_config.py").read_bytes()
        self.assertEqual([i for i, b in enumerate(sync_bytes) if b > 127], [])

        localized_ui_host_files = {"k2.py", "k2_print.py", "k2_mesh.py", "k2_cancel.py"}
        import io
        import tokenize

        for p in (K2_MAINLINE_DIR / "host").rglob("*.py"):
            text = p.read_text(encoding="utf-8")
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type == tokenize.COMMENT:
                    self.assertFalse(
                        any(ord(ch) > 127 for ch in tok.string),
                        f"Non-ASCII comment in {p}:{tok.start[0]}",
                    )
                elif any(ord(ch) > 127 for ch in tok.string):
                    self.assertIn(
                        p.name,
                        localized_ui_host_files,
                        f"Unexpected non-ASCII token outside localized UI dict in {p}:{tok.start[0]}",
                    )

        for cfg in (K2_MAINLINE_DIR / "config").glob("*.cfg"):
            for lineno, line in enumerate(cfg.read_text(encoding="utf-8").splitlines(), start=1):
                if not any(ord(ch) > 127 for ch in line):
                    continue
                stripped = line.lstrip()
                self.assertFalse(
                    stripped.startswith("#") or stripped.startswith(";"),
                    f"Non-ASCII comment line in {cfg.name}:{lineno}",
                )
                self.assertIn(
                    "'ru':",
                    line,
                    f"Unexpected non-ASCII config text outside localized UI map in {cfg.name}:{lineno}",
                )

    def test_pack_mcu_bin_roundtrip_and_stock_parity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            for mode in ("ccitt", "ibm"):
                raw = bytearray(b"\xaa" * 1024)
                sv = b"mcu0_022_950"
                raw[0x200 : 0x200 + len(sv)] = sv
                raw[0x20C:HEADER_END] = b"\x00" * 6
                src = tmp / f"raw_{mode}.bin"
                dst = tmp / f"packed_{mode}.bin"
                src.write_bytes(bytes(raw))

                crc, flen = pack_mcu_bin(
                    src,
                    dst,
                    crc_mode=mode,
                    expected_sv="mcu0_022_950",
                    max_size=2048,
                )
                got_sv, got_crc, got_len = verify_packed_bin(dst, crc_mode=mode)
                self.assertEqual(got_sv, "mcu0_022_950")
                self.assertEqual(got_crc, crc)
                self.assertEqual(got_len, flen)

                tampered = bytearray(dst.read_bytes())
                tampered[0x300] ^= 0x01
                dst.write_bytes(bytes(tampered))
                with self.assertRaises(ValueError):
                    verify_packed_bin(dst, crc_mode=mode)

        stock_dir = (
            REPO_ROOT
            / "printer_backups"
            / "K2Plus-855E-20260410_202129"
            / "unpacked"
            / "system-code"
            / "usr"
            / "share"
            / "klipper"
            / "fw"
            / "F008"
        )
        main_stock = stock_dir / "mcu0_140_G32-mcu0_022_000.bin"
        noz_stock = stock_dir / "noz0_130_G30-noz0_021_000.bin"
        if main_stock.is_file():
            sv, crc, flen = verify_packed_bin(main_stock, crc_mode="ccitt")
            self.assertEqual((sv, crc, flen), ("mcu0_022_000", 0x0EAE, 30948))
        if noz_stock.is_file():
            sv, crc, flen = verify_packed_bin(noz_stock, crc_mode="ibm")
            self.assertEqual((sv, crc, flen), ("noz0_021_000", 0x9B3B, 30872))

    def test_mcu_stage_upstream_isolation(self) -> None:
        clean_dir = self.build_root / "work" / self.manifest["klipper"]["extract_dir"]
        stage_dir = self.build_root / "work" / "mcu-stage"
        self.assertTrue((clean_dir / "klippy" / "klippy.py").is_file())
        self.assertTrue((stage_dir / "src" / "Kconfig").is_file())

        modified: set[str] = set()
        added: set[str] = set()
        deleted: set[str] = set()
        top_ignore = ["out", ".config", ".config.old", "__pycache__"]
        sub_ignore = ["__pycache__"]

        def walk_compare(cdir: Path, sdir: Path, rel: str = "") -> None:
            dcmp = filecmp.dircmp(
                str(cdir),
                str(sdir),
                ignore=top_ignore if not rel else sub_ignore,
            )
            for name in dcmp.diff_files:
                modified.add(f"{rel}/{name}".lstrip("/"))
            for name in dcmp.right_only:
                added.add(f"{rel}/{name}".lstrip("/"))
            for name in dcmp.left_only:
                deleted.add(f"{rel}/{name}".lstrip("/"))
            for sub, sub_cmp in dcmp.subdirs.items():
                walk_compare(cdir / sub, sdir / sub, f"{rel}/{sub}".lstrip("/"))

        walk_compare(clean_dir, stage_dir)

        self.assertEqual(deleted, set(), f"Unexpected deleted upstream paths: {deleted}")
        self.assertEqual(
            modified,
            {"src/Kconfig"},
            f"Upstream files modified outside staging src/Kconfig: {modified}",
        )
        self.assertEqual(
            added,
            {"src/gd32", "lib/gd32f30x"},
            f"Unexpected added top-level entries in mcu-stage: {added}",
        )

    def test_gd32_serial_irq_buffer_and_driver(self) -> None:
        gd32_dir = K2_MAINLINE_DIR / "mcu" / "src" / "gd32"
        makefile_text = (gd32_dir / "Makefile").read_text(encoding="utf-8")
        self.assertIn("gd32/serial_irq.c", makefile_text)
        self.assertNotIn("generic/serial_irq.c", makefile_text)

        serial_irq_text = (gd32_dir / "serial_irq.c").read_text(encoding="utf-8")
        self.assertIn("#define RX_BUFFER_SIZE 192", serial_irq_text)
        tx_match = re.search(r"#define\s+TX_BUFFER_SIZE\s+(\d+)", serial_irq_text)
        self.assertIsNotNone(tx_match, "Missing TX_BUFFER_SIZE definition in gd32/serial_irq.c")
        tx_buf_size = int(tx_match.group(1))
        self.assertGreaterEqual(tx_buf_size, 200)
        self.assertLessEqual(tx_buf_size, 255)

        serial_c_text = (gd32_dir / "serial.c").read_text(encoding="utf-8")
        self.assertIn("CTL0_FLAGS", serial_c_text)
        self.assertIn("USART_STAT0_ORERR", serial_c_text)

    def test_mcu_outputs_and_dictionaries(self) -> None:
        out_mcu = self.build_root / "out" / "mcu"
        specs = [
            (
                "mcu0_140_G32-mcu0_022_950.bin",
                "main_klipper.dict",
                "main_klipper.elf",
                "ccitt",
                "mcu0_022_950",
                "gd32f303xe",
                0x7D000,
                0x20010000,
                {
                    "config_transparent",
                    "transparent_send",
                    "config_fancheck",
                    "query_fancheck",
                    "query_gpio_status",
                },
                {"transparent_response", "fan_status", "gpio_status"},
            ),
            (
                "noz0_130_G30-noz0_021_950.bin",
                "nozzle_klipper.dict",
                "nozzle_klipper.elf",
                "ibm",
                "noz0_021_950",
                "gd32f303xb",
                0x1D000,
                0x20008000,
                {
                    "config_transparent",
                    "transparent_send",
                    "config_fancheck",
                    "query_fancheck",
                    "query_gpio_status",
                    "config_cs1237",
                    "config_lis2dw",
                    "config_trigger_analog",
                },
                {"transparent_response", "fan_status", "gpio_status"},
            ),
        ]

        for (
            bin_name,
            dict_name,
            elf_name,
            crc_mode,
            expected_sv,
            expected_mcu,
            max_flash,
            expected_sp,
            req_cmds,
            req_resps,
        ) in specs:
            bin_path = out_mcu / bin_name
            dict_path = out_mcu / dict_name
            elf_path = out_mcu / elf_name
            self.assertTrue(bin_path.is_file(), f"Missing MCU binary: {bin_path}")
            self.assertTrue(dict_path.is_file(), f"Missing MCU dict: {dict_path}")
            self.assertTrue(elf_path.is_file(), f"Missing MCU elf: {elf_path}")

            sv, _crc, flen = verify_packed_bin(bin_path, crc_mode=crc_mode)
            self.assertEqual(sv, expected_sv)
            self.assertLessEqual(flen, max_flash)

            raw = bin_path.read_bytes()
            init_sp, reset_vec = struct.unpack_from("<II", raw, 0)
            self.assertEqual(init_sp, expected_sp)
            self.assertEqual(reset_vec & 1, 1)
            self.assertGreater(reset_vec, 0x08003000)
            self.assertLess(reset_vec, 0x08003000 + flen)

            kd = json.loads(dict_path.read_text(encoding="utf-8"))
            cfg = kd["config"]
            self.assertEqual(cfg["MCU"], expected_mcu)
            self.assertEqual(cfg["CLOCK_FREQ"], 120000000)
            self.assertEqual(cfg["SERIAL_BAUD"], 230400)
            self.assertEqual(cfg["RESERVE_PINS_serial"], "PA2,PA3")
            self.assertEqual(cfg["STEPPER_STEP_BOTH_EDGE"], 1)

            cmd_names = {k.split()[0] for k in kd["commands"]}
            resp_names = {k.split()[0] for k in kd["responses"]}
            for name in req_cmds:
                self.assertIn(name, cmd_names)
            for name in req_resps:
                self.assertIn(name, resp_names)

    def test_native_armv7_outputs_and_report(self) -> None:
        out_native = self.build_root / "out" / "native"
        report_path = out_native / "build_native_report.json"
        self.assertTrue(report_path.is_file(), f"Missing native report: {report_path}")
        report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(report["python_version"], "3.9.12")
        self.assertEqual(report["python_check"]["import_test"], "passed")
        self.assertGreaterEqual(report["python_check"]["ast_parsed_files"], 170)

        abi = report["k2_hardware_abi"]
        self.assertEqual(abi["pull_message_size"], 516)
        self.assertEqual(abi["rgb_t_size"], 3)
        self.assertEqual(abi["crc8_check_hex"], "0xf4")
        self.assertGreater(abi["flushing_volume_black_to_white"], 0)
        self.assertEqual(
            abi["exported_symbols"],
            [
                "get_flushing_volume",
                "msgblock_485_check",
                "msgblock_485_crc8",
                "serial_485_queue_alloc",
                "serial_485_queue_exit",
                "serial_485_queue_extract_old",
                "serial_485_queue_free",
                "serial_485_queue_get_stats",
                "serial_485_queue_pull",
                "serial_485_queue_send",
            ],
        )

        for name in ("c_helper.so", "k2_hardware.so", "klipper_mcu"):
            art_path = out_native / name
            self.assertTrue(art_path.is_file(), f"Missing native artifact: {art_path}")
            ei_class, ei_data, e_machine = _read_elf32_arm_header(art_path)
            self.assertEqual((ei_class, ei_data, e_machine), (1, 1, 40))

            meta = report["artifacts"][name]
            self.assertEqual(sha256_file(art_path), meta["sha256"])
            max_glibc = meta["max_glibc"]
            if max_glibc != "none":
                major, minor = (int(x) for x in max_glibc.split("."))
                self.assertLessEqual((major, minor), (2, 29))
            if name == "klipper_mcu":
                self.assertEqual(meta["pt_interp"], "/lib/ld-linux-armhf.so.3")


if __name__ == "__main__":
    unittest.main()
