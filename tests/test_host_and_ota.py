"""Automated verification suite for k2_mainline Milestones 2-5 (host, config, OTA)."""

from __future__ import annotations

import configparser
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import jinja2

K2_MAINLINE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(K2_MAINLINE_DIR / "scripts"))
sys.path.insert(0, str(K2_MAINLINE_DIR / "ota"))
sys.path.insert(0, str(K2_MAINLINE_DIR / "config"))

from common import (  # noqa: E402
    compute_tree_digest,
    get_build_root,
    load_upstream_manifest,
    sha256_file,
    verified_klipper_stamp_path,
)
from cpio_crc import mapped_cpio_crc, read_cpio_crc, write_cpio_crc  # noqa: E402
from fetch_sources import extract_klipper  # noqa: E402
from init_patches import (  # noqa: E402
    patch_app_init,
    patch_klipper_init,
    patch_klipper_mcu_init,
    patch_mcu_update_init,
    patch_moonraker_init,
)
sys.path.insert(0, str(K2_MAINLINE_DIR / "services"))
import k2_ota_update  # noqa: E402
import publish_github_release  # noqa: E402
import k2_sync_config  # noqa: E402
from sw_description import FORBIDDEN_TOKENS, render_sw_description  # noqa: E402


class HostAndConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_upstream_manifest()
        cls.build_root = get_build_root(cls.manifest)

    def test_host_inventory_and_zero_core_edits(self) -> None:
        out_host = self.build_root / "out" / "host"
        inv_path = out_host / "inventory.json"
        self.assertTrue(inv_path.is_file(), f"Missing host inventory: {inv_path}")
        inv = json.loads(inv_path.read_text(encoding="utf-8"))

        self.assertEqual(inv["core_policy"], "unmodified")
        self.assertEqual(inv["modified_upstream_files"], 0)
        self.assertEqual(inv["upstream_shadow_collisions"], 0)
        self.assertEqual(inv["klipper_tag"], self.manifest["klipper"]["tag"])
        self.assertEqual(inv["klipper_commit"], self.manifest["klipper"]["commit"])
        self.assertEqual(inv["klipper_tarball_sha256"], self.manifest["klipper"]["sha256"])
        self.assertEqual(inv["origin_counts"].get("k2_adapter"), 11)
        self.assertEqual(inv["origin_counts"].get("k2_driver"), 5)
        self.assertEqual(inv["origin_counts"].get("k2_stock_extra"), 11)

        clean_klippy = self.build_root / "work" / self.manifest["klipper"]["extract_dir"] / "klippy"
        assembled_klippy = out_host / "klippy"
        self.assertEqual((assembled_klippy / ".version").read_text(encoding="ascii"), "mainline-v0.14.0-k2\n")

        stamp_path = verified_klipper_stamp_path(self.build_root / "work", self.manifest)
        self.assertTrue(stamp_path.is_file(), f"Missing verified Klipper stamp: {stamp_path}")
        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
        self.assertEqual(inv["klippy_source_tree_sha256"], stamp["klippy_tree_sha256"])

        # Spot-check every upstream file matches clean extracted upstream byte-for-byte.
        for rel, entry in inv["files"].items():
            target = assembled_klippy / rel
            self.assertTrue(target.is_file(), f"Missing assembled file: {rel}")
            self.assertEqual(sha256_file(target), entry["sha256"])
            if entry["origin"] == "upstream":
                self.assertEqual(entry["sha256"], sha256_file(clean_klippy / rel))

        for dropped in ("extras/k2_lis2dw.py", "extras/k2_cfs_diagnostic.py", "extras/k2_feed_diagnostics.py"):
            self.assertNotIn(dropped, inv["files"])

    def test_clean_tarball_extraction_and_tamper_reextraction(self) -> None:
        tarball = self.build_root / "cache" / self.manifest["klipper"]["tarball_name"]
        with tempfile.TemporaryDirectory() as td:
            work_dir = Path(td) / "work"
            work_dir.mkdir(parents=True)
            dest = extract_klipper(tarball, work_dir, self.manifest, force=True)
            self.assertTrue((dest / "klippy" / "klippy.py").is_file())
            self.assertTrue((dest / "src" / "Kconfig").is_file())
            digest_orig, _ = compute_tree_digest(dest / "klippy")

            # Corrupt an extracted upstream file and verify extract_klipper re-extracts automatically.
            (dest / "klippy" / "klippy.py").write_text("# tampered\n", encoding="utf-8")
            dest2 = extract_klipper(tarball, work_dir, self.manifest, force=False)
            digest_restored, _ = compute_tree_digest(dest2 / "klippy")
            self.assertEqual(digest_restored, digest_orig)

    def test_arm_host_verification_report(self) -> None:
        report_path = self.build_root / "out" / "host" / "verify_host_arm_report.json"
        self.assertTrue(report_path.is_file(), f"Missing ARMv7 host verification report: {report_path}")
        report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertTrue(report.get("passed"))
        self.assertIn("input_hashes", report)
        self.assertEqual(report["machine"], "armv7l")
        target = report["target_runtime"]
        self.assertEqual(target["python_checks"]["import_test"], "passed")
        self.assertGreaterEqual(target["config_checks"]["objects_created"], 40)
        self.assertEqual(target["config_checks"]["kinematics"], "K2CoreXYKinematics")
        self.assertEqual(target["config_checks"]["z_align"], "NativeZAlign")
        self.assertTrue(target["config_checks"]["qmode_chamber_fan_restore_verified"])
        self.assertTrue(target["config_checks"]["qmode_m106_m107_m141_verified"])
        self.assertTrue(target["config_checks"]["load_cell_init_save_config_verified"])
        self.assertTrue(target["config_checks"]["m141_screen_compat_verified"])
        self.assertTrue(target["config_checks"]["cold_bed_start_print_verified"])
        self.assertTrue(target["config_checks"]["z_clearance_bottom_ref_verified"])
        self.assertTrue(target["config_checks"]["custom_macro_screen_compat_verified"])
        self.assertTrue(target["config_checks"]["g29_legacy_screen_params_verified"])
        self.assertTrue(target["config_checks"]["auto_load_cell_init_verified"])
        self.assertTrue(target["config_checks"]["g29_auto_save_mesh_verified"])

        endpoints = set(report["screen_checks"]["endpoints_verified"])
        for req_ep in (
            "shakehands",
            "get_mesh",
            "getBootLoaderVersion",
            "get_cx_fan_status",
            "pause_resume/check_continue_print_state",
            "pause_resume/set_print_first_layer",
        ):
            self.assertIn(req_ep, endpoints)
        self.assertEqual(report["driver_checks"]["crc8_hex"], "0xf4")
        self.assertEqual(report["driver_checks"]["flushing_volume_black_to_white"], 560)
        self.assertTrue(report["driver_checks"]["migrated_pid_verified"])
        self.assertTrue(report["driver_checks"]["legacy_load_cell_migration_verified"])
        self.assertTrue(report["driver_checks"]["rs485_non_idempotent_verified"])
        self.assertTrue(report["driver_checks"]["rs485_broadcast_log_quiet_verified"])
        self.assertTrue(report["driver_checks"]["cfs_lost_ack_no_upper_retry_verified"])
        self.assertTrue(report["driver_checks"]["cfs_gear_retry_retrude_timeout_blocked"])
        self.assertTrue(report["driver_checks"]["cfs_stage6_crc_latch_verified"])
        self.assertTrue(report["driver_checks"]["k2_cancel_mutex_deadline_verified"])
        self.assertTrue(report["driver_checks"]["z_align_idle_stop_verified"])

        svc = report["service_checks"]
        self.assertEqual(svc["api_version"], "1.5.0")
        self.assertEqual(svc["moonraker_version"], "v0.10.0-k2")
        self.assertEqual(svc["fluidd_version"], "v1.38.1")
        self.assertEqual(svc["mainsail_version"], "v2.19.0")
        self.assertIn("go2rtc version 1.9.15", svc["go2rtc_version"])
        self.assertTrue(svc["go2rtc_config_ok"])
        self.assertTrue(svc["go2rtc_init_ok"])
        self.assertTrue(svc["k2_h264_source_ok"])
        self.assertTrue(svc["k2_ota_proxy_verified"])
        self.assertTrue(svc["k2_ota_shim_libcurl_verified"])
        self.assertTrue(svc["k2_ota_crc64_parity_verified"])
        self.assertTrue(svc["nginx_config_ok"])
        self.assertTrue(svc["openwrt_provider_verified"])
        self.assertTrue(svc["lmdb_migration_verified"])
        self.assertTrue(svc["pillow_jpeg_roundtrip_verified"])

        moon_inv = json.loads(
            (self.build_root / "out" / "services" / "moonraker" / "inventory.json").read_text(encoding="utf-8")
        )
        self.assertEqual(moon_inv["modified_upstream_files"], 0)
        self.assertEqual(moon_inv["upstream_shadow_collisions"], 0)
        self.assertEqual(moon_inv["version_string"], "v0.10.0-k2")

    def test_config_macros_compile_and_no_duplicate_macros(self) -> None:
        cfg_dir = K2_MAINLINE_DIR / "config"
        env = jinja2.Environment("{%", "%}", "{", "}")
        macro_owners: dict[str, list[str]] = {}
        for cfg in sorted(cfg_dir.glob("*.cfg")):
            raw = cfg.read_text(encoding="utf-8")
            self.assertNotIn("{#", raw)
            self.assertNotIn("#}", raw)
            self.assertNotIn("\r", raw)
            parser = configparser.RawConfigParser(strict=False, inline_comment_prefixes=(";", "#"))
            parser.read_string(raw, source=str(cfg))
            for section in parser.sections():
                if section.startswith("gcode_macro "):
                    macro_owners.setdefault(section, []).append(cfg.name)
                for opt in ("gcode", "runout_gcode", "on_error_gcode"):
                    if parser.has_option(section, opt):
                        env.from_string(parser.get(section, opt))

        duplicates = {k: v for k, v in macro_owners.items() if len(v) > 1}
        self.assertEqual(duplicates, {}, f"Duplicate [gcode_macro ...] sections: {duplicates}")

        printer_txt = (cfg_dir / "printer.cfg").read_text(encoding="utf-8")
        self.assertIn("# Version: v9.5.3", printer_txt)
        self.assertIn("printer.pause_resume.is_paused", printer_txt)
        self.assertIn("[lis2dw]", printer_txt)
        self.assertIn("accel_chip: lis2dw", printer_txt)
        self.assertNotIn("SAVE_CONFIG", printer_txt)
        self.assertNotIn("k2_lis2dw", printer_txt)
        self.assertNotIn("[output_pin fan1]", printer_txt)
        self.assertIn("counts_per_gram: 100", printer_txt)
        self.assertIn("retry_tolerance: 0.04", printer_txt)
        self.assertIn("25,175", printer_txt)
        self.assertIn("325,175", printer_txt)
        self.assertIn("[screws_tilt_adjust]", printer_txt)
        self.assertIn("screw_thread: CW-M4", printer_txt)
        k2_print_txt = (cfg_dir / "k2_print.cfg").read_text(encoding="utf-8")
        self.assertIn("params.Z_TILT|default(0)|int", k2_print_txt)
        self.assertIn("K_MESH_DISCARD_PENDING PROFILE={mesh_profile}", k2_print_txt)
        self.assertIn("[gcode_macro SCREWS_TILT_CALCULATE]", k2_print_txt)
        gcode_txt = (cfg_dir / "gcode_macro.cfg").read_text(encoding="utf-8")
        self.assertIn("SHAPER_CALIBRATE {rawparams}\n\tCXSAVE_CONFIG", gcode_txt)
        lc_txt = (cfg_dir / "k2_load_cell.cfg").read_text(encoding="utf-8")
        self.assertNotIn("counts_per_gram:", lc_txt)
        self.assertNotIn("z_offset:", lc_txt)
        mr_conf = (K2_MAINLINE_DIR / "services" / "moonraker.conf").read_text(encoding="utf-8")
        self.assertNotIn("enable_inotify_warnings", mr_conf)
        self.assertIn("[webcam k2_camera]", mr_conf)
        self.assertIn("service: webrtc-go2rtc", mr_conf)
        self.assertIn("stream_url: /webcam/stream.html?src=k2", mr_conf)
        self.assertIn("snapshot_url: /webcam/api/frame.jpeg?src=k2", mr_conf)
        mr_asvc = (K2_MAINLINE_DIR / "services" / "moonraker.asvc").read_text(encoding="utf-8")
        self.assertIn("go2rtc", mr_asvc.splitlines())
        self.assertIn("k2_ota_proxy", mr_asvc.splitlines())
        nginx_conf = (K2_MAINLINE_DIR / "services" / "nginx.conf").read_text(encoding="utf-8")
        self.assertIn("proxy_pass http://go2rtc/;", nginx_conf)
        self.assertIn("proxy_pass http://webrtc_camera/call/;", nginx_conf)

    def test_sync_config_migration_preserves_calibration_and_pid(self) -> None:
        rom_dir = K2_MAINLINE_DIR / "config"
        with tempfile.TemporaryDirectory() as td:
            user_dir = Path(td) / "user_cfg"
            user_dir.mkdir(parents=True)
            # Pre-seed custom box.cfg even when printer.cfg is absent ("fresh" branch).
            (user_dir / "box.cfg").write_text("# pre-existing custom box\n", encoding="utf-8")
            self.assertEqual(k2_sync_config.sync_configs(rom_dir, user_dir), "fresh")
            self.assertEqual((user_dir / "box.cfg").read_text(encoding="utf-8"), "# pre-existing custom box\n")
            self.assertTrue((user_dir / "printer.cfg").is_file())
            self.assertEqual(k2_sync_config.sync_configs(rom_dir, user_dir), "unchanged")

            stock_cfg = (
                "# Version: v1.1.4\n[stepper_z]\nstep_pin: PB6\n\n"
                + k2_sync_config.AUTOSAVE_CANONICAL_PREAMBLE
                + "#*# [prtouch_v3]\n#*# z_offset = 0.0\n#*#\n"
                "#*# [temperature_sensor mcu_temp]\n#*# min_temp = 0\n#*#\n"
                "#*# [stepper_z]\n#*# position_endstop = 0.1\n#*#\n"
                "#*# [extruder]\n#*# control = pid\n#*# pid_kp = 50.0\n#*# pid_ki = 1.5\n#*# pid_kd = 45.0\n#*#\n"
                "#*# [heater_bed]\n#*# control = pid\n#*# pid_kp = 55.0\n#*# pid_ki = 0.25\n#*# pid_kd = 300.0\n#*#\n"
                "#*# [bed_mesh default]\n#*# version = 1\n#*# points =\n#*# \t  0.01, 0.02\n#*# \t  0.03, 0.04\n#*#\n"
                "#*# [bed_mesh broken_missing_version]\n#*# min_x = 10\n#*#\n"
                "#*# [unknown_legacy_section]\n#*# foo = bar\n#*#\n"
                "#*# [box]\n#*# cut_pos_x = -8.40\n#*#\n"
                "#*# [input_shaper]\n#*# shaper_type_x = mzv\n#*# shaper_freq_x = 46.2\n"
            )
            (user_dir / "printer.cfg").write_text(stock_cfg, encoding="utf-8")
            (user_dir / "k2_print.cfg").write_text("# outdated managed file\n", encoding="utf-8")
            (user_dir / "k2_variables.cfg").write_text("[Variables]\nk2_prime_row = 2\n", encoding="utf-8")

            res = k2_sync_config.sync_configs(rom_dir, user_dir)
            self.assertEqual(res, "upgraded:1.1.4->9.5.3")
            merged = (user_dir / "printer.cfg").read_text(encoding="utf-8")
            self.assertNotIn("#*# [prtouch_v3]", merged)
            self.assertNotIn("#*# [stepper_z]", merged)
            self.assertNotIn("#*# [unknown_legacy_section]", merged)
            self.assertNotIn("broken_missing_version", merged)
            self.assertIn("cut_pos_x = -8.40", merged)
            self.assertIn("shaper_freq_x = 46.2", merged)
            self.assertIn("#*# \t  0.01, 0.02", merged)
            self.assertIn("# pid_kp: 14.296", merged)
            self.assertIn("# pid_Kp: 37", merged)
            self.assertEqual((user_dir / "box.cfg").read_text(encoding="utf-8"), "# pre-existing custom box\n")
            self.assertEqual((user_dir / "k2_variables.cfg").read_text(encoding="utf-8"), "[Variables]\nk2_prime_row = 2\n")
            self.assertEqual((user_dir / "k2_print.cfg").read_text(encoding="utf-8"), (rom_dir / "k2_print.cfg").read_text(encoding="utf-8"))
            self.assertTrue(list((user_dir / "backups").glob("backup-1.1.4-*")))

            # Legacy k2_load_cell.cfg migration when SAVE_CONFIG has no [load_cell_probe].
            legacy_dir = Path(td) / "legacy_lc"
            legacy_dir.mkdir(parents=True)
            (legacy_dir / "printer.cfg").write_text(stock_cfg, encoding="utf-8")
            (legacy_dir / "k2_load_cell.cfg").write_text(
                "[load_cell_probe]\ncounts_per_gram: 100\nz_offset: 0\nreference_tare_counts: -1407220\n",
                encoding="utf-8",
            )
            self.assertEqual(k2_sync_config.sync_configs(rom_dir, legacy_dir), "upgraded:1.1.4->9.5.3")
            legacy_merged = (legacy_dir / "printer.cfg").read_text(encoding="utf-8")
            self.assertIn("#*# [load_cell_probe]", legacy_merged)
            self.assertIn("#*# reference_tare_counts = -1407220", legacy_merged)
            self.assertIn("# counts_per_gram: 100", legacy_merged)
            self.assertIn("# z_offset: 0", legacy_merged)
            self.assertEqual(
                (legacy_dir / "k2_load_cell.cfg").read_text(encoding="utf-8"),
                (rom_dir / "k2_load_cell.cfg").read_text(encoding="utf-8"),
            )

            # Out-of-range legacy reference_tare_counts must be ignored (no fabricated calibration).
            bad_lc_dir = Path(td) / "bad_legacy_lc"
            bad_lc_dir.mkdir(parents=True)
            (bad_lc_dir / "printer.cfg").write_text(stock_cfg, encoding="utf-8")
            (bad_lc_dir / "k2_load_cell.cfg").write_text(
                "[load_cell_probe]\nreference_tare_counts: 99999999\n",
                encoding="utf-8",
            )
            self.assertEqual(k2_sync_config.sync_configs(rom_dir, bad_lc_dir), "upgraded:1.1.4->9.5.3")
            self.assertNotIn("#*# [load_cell_probe]", (bad_lc_dir / "printer.cfg").read_text(encoding="utf-8"))

            # Existing SAVE_CONFIG [load_cell_probe] takes precedence over legacy k2_load_cell.cfg.
            prec_dir = Path(td) / "prec_lc"
            prec_dir.mkdir(parents=True)
            (prec_dir / "printer.cfg").write_text(
                stock_cfg + "#*#\n#*# [load_cell_probe]\n#*# reference_tare_counts = -555555\n",
                encoding="utf-8",
            )
            (prec_dir / "k2_load_cell.cfg").write_text(
                "[load_cell_probe]\nreference_tare_counts: -1407220\n",
                encoding="utf-8",
            )
            self.assertEqual(k2_sync_config.sync_configs(rom_dir, prec_dir), "upgraded:1.1.4->9.5.3")
            prec_merged = (prec_dir / "printer.cfg").read_text(encoding="utf-8")
            self.assertIn("#*# reference_tare_counts = -555555", prec_merged)
            self.assertNotIn("-1407220", prec_merged)

    def test_rs485_non_idempotent_no_retry_and_z_align_idle_stop(self) -> None:
        report_path = self.build_root / "out" / "host" / "verify_host_arm_report.json"
        self.assertTrue(report_path.is_file(), f"Missing ARMv7 host verification report: {report_path}")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertTrue(report["driver_checks"]["rs485_non_idempotent_verified"])
        self.assertTrue(report["driver_checks"]["z_align_idle_stop_verified"])


class OtaPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_upstream_manifest()
        cls.build_root = get_build_root(cls.manifest)

    def test_cpio_crc_roundtrip_and_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pkg = Path(td) / "test.img"
            items = [
                ("sw-description", b"software = {};\n", 0o644),
                ("preinstall.sh", b"#!/bin/sh\nexit 0\n", 0o755),
                ("payload", b"\x01\x02\x03\x04\x05" * 100, 0o644),
            ]
            write_cpio_crc(pkg, items)
            self.assertEqual(pkg.stat().st_size % 512, 0)

            parsed = read_cpio_crc(pkg.read_bytes())
            self.assertEqual(list(parsed.keys()), [n for n, _, _ in items])
            for name, content, _mode in items:
                self.assertEqual(parsed[name], content)

            with self.assertRaises(ValueError):
                write_cpio_crc(Path(td) / "bad.img", [("../escape", b"x", 0o644)])

            tampered = bytearray(pkg.read_bytes())
            tampered[150] ^= 0xFF
            with self.assertRaises(ValueError):
                read_cpio_crc(bytes(tampered))

            with self.assertRaises(ValueError):
                read_cpio_crc(pkg.read_bytes()[:-16])

    def test_sw_description_and_init_patches(self) -> None:
        desc = render_sw_description("1.1.4.950", "1.1.4.950")
        text = desc.decode("ascii").lower()
        self.assertNotIn("sha256", text)
        for forbidden in FORBIDDEN_TOKENS:
            self.assertNotIn(forbidden, text)
        self.assertIn("now_a_next_b", text)
        self.assertIn("now_b_next_a", text)
        self.assertIn('filename = "postinstall.sh";', text)
        self.assertIn('data = "now_a_next_b";', text)
        self.assertNotIn("bootenv", text)

        mcu_init = "start_service() {\n    procd_set_param command klipper_mcu -r\n}\n"
        self.assertIn("procd_set_param command /usr/bin/klipper_mcu -r", patch_klipper_mcu_init(mcu_init))

        klip_init = (
            "copy_config() {\n"
            "    if [ -s $PRINTER_CONFIG_DIR/printer.cfg ]; then\n"
            "        echo ok\n"
            "    else\n"
            '        cp -af "${CONFIG_PATH}"/*.cfg $PRINTER_CONFIG_DIR/\n'
            "        sync\n"
            "    fi\n"
            "}\nstart_service() {\n    mcu_reset\n    copy_config\n}\n"
        )
        patched = patch_klipper_init(klip_init)
        self.assertNotIn("\n    mcu_reset\n", patched)
        self.assertIn("k2_sync_config.py", patched)

        stock_mcu_update = (
            "#!/bin/sh /etc/rc.common\n"
            "PROG=/usr/bin/mcu_util\n"
            "PROG_485=/usr/bin/mcu_util_485\n"
            "compare_version() {\n  echo ok\n}\n"
            "auto_update() {\n  echo ok\n}\n"
        )
        patched_mcu_up = patch_mcu_update_init(stock_mcu_update)
        self.assertNotIn("mcu_util_485", patched_mcu_up)
        self.assertNotIn("extruder_update", patched_mcu_up)
        self.assertIn("mcu_reset.sh", patched_mcu_up)
        self.assertIn("reset_mcus_to_bootloader", patched_mcu_up)
        self.assertIn("mcu0_140_G32", patched_mcu_up)
        self.assertIn("noz0_130_G30", patched_mcu_up)

        stock_moon_init = (
            "#!/bin/sh /etc/rc.common\n"
            "START=56\n"
            "PROG=/usr/share/moonraker-env/bin/python\n"
            "CONF=/usr/share/moonraker/moonraker.conf\n"
        )
        patched_moon_init = patch_moonraker_init(stock_moon_init)
        self.assertIn("seed_if_missing", patched_moon_init)
        self.assertIn("USER_DATA=/mnt/UDISK/printer_data", patched_moon_init)
        self.assertIn("CONF=${USER_DATA}/config/moonraker.conf", patched_moon_init)
        self.assertIn("moonraker.asvc", patched_moon_init)
        self.assertIn("MOONRAKER_DIR=/usr/share/moonraker", patched_moon_init)
        self.assertIn(
            "command ${PROG} -B ${MOONRAKER_DIR}/moonraker/moonraker.py -d ${USER_DATA} -c ${CONF}",
            patched_moon_init,
        )

        stock_app_init = (
            "#!/bin/sh /etc/rc.common\n"
            "BIN_PATH=/usr/bin\n"
            "APP_SERVER=app-server\n"
            "start_app() {\n"
            "    local app=$1\n"
            "    procd_open_instance\n"
            "    procd_set_param env HOME=/root\n"
            "    procd_set_param command $app\n"
            "    procd_close_instance\n"
            "}\n"
        )
        patched_app = patch_app_init(stock_app_init)
        self.assertIn('if [ "$app" = "$BIN_PATH/$APP_SERVER" ]', patched_app)
        self.assertIn("LD_PRELOAD=/usr/lib/libk2_ota_shim.so", patched_app)
        self.assertEqual(patch_app_init(patched_app), patched_app)

    def test_github_ota_manifest_and_release_bundle(self) -> None:
        self.assertEqual(k2_ota_update.crc64_ecma_bytes(b"123456789"), 0x995DC9BBDF1939FA)
        self.assertTrue(k2_ota_update.is_newer_version("1.1.4.952", "1.1.4.950"))
        self.assertTrue(k2_ota_update.is_newer_version("1.1.4.950", "1.1.4.11"))
        self.assertFalse(k2_ota_update.is_newer_version("1.1.4.950", "1.1.4.950"))
        self.assertFalse(k2_ota_update.is_newer_version("1.1.4.950", "1.1.4.952"))

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            fake_img = tdp / "CR0CN240110C10_ota_img_V1.1.4.952.img"
            fake_img.write_bytes(b"TEST_K2_OTA_IMAGE_DATA" * 64)
            fake_report = {"version": "1.1.4.952", "passed": True}
            manifest_obj, manifest_path, sums_path = publish_github_release.build_release_bundle(
                img_path=fake_img,
                report=fake_report,
                repo="supfeer/k2-mainline",
                tag="v1.1.4.952",
                notes=["K2 Mainline v1.1.4.952"],
                out_dir=tdp / "bundle",
            )
            self.assertTrue(manifest_path.is_file())
            self.assertTrue(sums_path.is_file())
            self.assertEqual(manifest_obj["version"], "1.1.4.952")
            self.assertIn(manifest_obj["sha256"], sums_path.read_text(encoding="utf-8"))

            resp_sys = k2_ota_update.build_creality_firmware_response(
                {"items": [{"type": 2, "subType": "F008", "firmwareVersion": "1.1.4.950", "hardwareVersionNumber": "CR0CN240110C10"}]},
                manifest_obj,
                proxy_base_url="http://127.0.0.1:7129",
            )
            self.assertEqual(resp_sys["result"]["count"], 1)
            self.assertEqual(resp_sys["result"]["list"][0]["versionNumber"], "1.1.4.952")

            # Stale system_version.json (1.1.4.11) must not re-offer an already installed fw_printenv version.
            resp_stale = k2_ota_update.build_creality_firmware_response(
                {"items": [{"type": 2, "subType": "F008", "firmwareVersion": "1.1.4.11", "hardwareVersionNumber": "CR0CN240110C10"}]},
                manifest_obj,
                proxy_base_url="http://127.0.0.1:7129",
                fallback_current_version="1.1.4.952",
            )
            self.assertEqual(resp_stale["result"]["count"], 0)
            self.assertEqual(resp_stale["result"]["list"], [])

            resp_cfs = k2_ota_update.build_creality_firmware_response(
                {"items": [{"type": 27, "subType": "cfs", "firmwareVersion": "1.1.3", "hardwareVersionNumber": ""}]},
                manifest_obj,
                proxy_base_url="http://127.0.0.1:7129",
            )
            self.assertEqual(resp_cfs["result"]["count"], 0)
            self.assertEqual(resp_cfs["result"]["list"], [])

    def test_ota_image_and_verification_report(self) -> None:
        out_ota = self.build_root / "out" / "ota"
        report_path = out_ota / "ota_verification_report.json"
        self.assertTrue(report_path.is_file(), f"Missing OTA verification report: {report_path}")
        report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertTrue(report.get("passed"))
        self.assertEqual(report["version"], "1.1.4.950")
        self.assertEqual(report["stock_base_sha256"], self.manifest["stock_ota"]["sha256"])

        pack = report["pack"]
        self.assertTrue(pack["squashfs_roundtrip"])
        self.assertLess(pack["image"]["rootfs_bytes"], pack["rootfs_slot_limit_bytes"])
        self.assertEqual(
            pack["preserved_pins"]["usr/share/k2-ota/mcu_update.stock"],
            "fe2b542886c22d345e5f83dfcb9248749415647b4dfdf2fa57deae0fc9a2501e",
        )
        self.assertIn("usr/share/klipper/fw/cfs/cfs0_050_G32-cfs0_000_113.bin", pack["preserved_pins"])
        self.assertIn("usr/share/klipper/fw/cfs/cfs0_050_G30-cfs0_000_113.bin", pack["preserved_pins"])
        for cloud_rel in (
            "usr/bin/app-server",
            "usr/bin/master-server",
            "usr/bin/display-server",
            "usr/bin/web-server",
            "usr/bin/webrtc",
            "usr/bin/webrtc_local",
            "usr/bin/cam_app",
            "usr/share/k2-ota/app.init.stock",
        ):
            self.assertIn(cloud_rel, pack["preserved_pins"])
        changed_paths = set(pack["changed_rootfs_paths"])
        for go2rtc_rel in (
            "usr/bin/go2rtc",
            "usr/bin/k2_h264_source",
            "etc/go2rtc/go2rtc.yaml",
            "etc/init.d/go2rtc",
            "etc/rc.d/S98go2rtc",
            "usr/lib/libk2_ota_shim.so",
            "usr/bin/k2-ota-update",
            "etc/init.d/k2_ota_proxy",
            "etc/rc.d/S97k2_ota_proxy",
            "etc/init.d/app",
            "usr/share/k2-ota/app.init.stock",
        ):
            self.assertIn(go2rtc_rel, changed_paths)
        self.assertEqual(pack["klippy_bytecode_artifacts"], 0)
        self.assertGreater(pack["deleted_stock_klippy_pyc_count"], 0)

        img_path = out_ota / pack["image"]["filename"]
        self.assertTrue(img_path.is_file(), f"Missing OTA .img: {img_path}")
        self.assertEqual(sha256_file(img_path), pack["image"]["sha256"])

        with mapped_cpio_crc(img_path) as (_mm, entries):
            self.assertEqual(
                list(entries.keys()),
                ["sw-description", "preinstall.sh", "postinstall.sh", "kernel", "rootfs", "cpio_item_md5"],
            )
            desc_lower = bytes(entries["sw-description"]).decode("ascii").lower()
            for tok in FORBIDDEN_TOKENS:
                self.assertNotIn(tok, entries)
                self.assertNotIn(tok, desc_lower)
            pre_txt = bytes(entries["preinstall.sh"]).decode("utf-8")
            self.assertNotIn("@KERNEL_MIN_SECTORS@", pre_txt)
            self.assertNotIn("@ROOTFS_MIN_SECTORS@", pre_txt)
            want_k_sec = (len(entries["kernel"]) + 511) // 512
            want_r_sec = (len(entries["rootfs"]) + 511) // 512
            self.assertIn(f'KERNEL_MIN_SECTORS="{want_k_sec}"', pre_txt)
            self.assertIn(f'ROOTFS_MIN_SECTORS="{want_r_sec}"', pre_txt)
            self.assertNotIn(b">/dev/null 2>&1 || true", bytes(entries["postinstall.sh"]))
            for line in bytes(entries["cpio_item_md5"]).decode("ascii").splitlines():
                md5_hex, name = line.split()
                self.assertEqual(hashlib.md5(entries[name]).hexdigest(), md5_hex)

        swu = report["swupdate_verify"]
        self.assertEqual(swu["swupdate_cli_checks"]["now_A_next_B"]["exit_code"], 0)
        self.assertEqual(swu["swupdate_cli_checks"]["now_B_next_A"]["exit_code"], 0)
        self.assertNotEqual(swu["swupdate_cli_checks"]["corrupted_rootfs_rejected"]["exit_code"], 0)
        self.assertNotEqual(swu["swupdate_cli_checks"]["wrong_slot_install_rejected"]["exit_code"], 0)
        self.assertNotEqual(swu["swupdate_cli_checks"]["cross_alias_install_rejected"]["exit_code"], 0)
        self.assertNotEqual(swu["swupdate_cli_checks"]["small_boot_partition_rejected"]["exit_code"], 0)
        self.assertNotEqual(swu["swupdate_cli_checks"]["bootenv_fail_preserves_overlay"]["exit_code"], 0)
        self.assertNotEqual(swu["swupdate_cli_checks"]["overlay_eio_preserves_slot_and_overlay"]["exit_code"], 0)
        self.assertNotEqual(swu["swupdate_cli_checks"]["readback_mismatch_reverts_cleanly"]["exit_code"], 0)
        self.assertNotEqual(swu["swupdate_cli_checks"]["readback_mismatch_revert_fail_critical"]["exit_code"], 0)
        self.assertEqual(swu["swupdate_cli_checks"]["valid_install_commits_env_and_cleans_overlay"]["exit_code"], 0)
        self.assertTrue(all(swu["cpio_parser_rejections"].values()))
        self.assertTrue(swu["preflight_checks"]["accept_1_1_4_11_and_919"])
        self.assertTrue(swu["preflight_checks"]["reject_wrong_slot_selection"])
        self.assertTrue(swu["preflight_checks"]["reject_cmdline_mismatch"])
        self.assertTrue(swu["preflight_checks"]["reject_aliased_rootfs"])
        self.assertTrue(swu["mcu_update_checks"]["retry_transient_probe_before_writes"])
        self.assertTrue(swu["preflight_checks"]["reject_cross_alias_boot_root"])
        self.assertTrue(swu["preflight_checks"]["reject_small_boot_partition"])
        self.assertTrue(swu["preflight_checks"]["reject_malformed_dev_id"])
        self.assertEqual(swu["preflight_checks"]["kernel_min_sectors"], want_k_sec)
        self.assertEqual(swu["preflight_checks"]["rootfs_min_sectors"], want_r_sec)
        self.assertTrue(swu["preflight_checks"]["reject_overlay_symlink"])
        self.assertTrue(swu["mcu_update_checks"]["reject_nozzle_fail_without_485"])
        self.assertTrue(swu["mcu_update_checks"]["reject_wrong_hw_prefix"])
        self.assertTrue(swu["mcu_update_checks"]["reject_missing_nozzle_fw"])
        self.assertTrue(swu["mcu_update_checks"]["reject_reset_fail"])
        self.assertTrue(swu["mcu_update_checks"]["compare_version_fail_on_missing_or_dup"])
        self.assertTrue(swu["rollback_checks"]["incomplete_backup_rejected_cleanly"])
        self.assertTrue(swu["rollback_checks"]["nested_missing_include_rejected"])
        self.assertTrue(swu["rollback_checks"]["malformed_ini_backup_rejected"])
        self.assertTrue(swu["rollback_checks"]["rollback_eio_preserves_slot_and_configs"])
        self.assertTrue(swu["rollback_checks"]["rollback_fw_fail_preserves_extra_files"])
        self.assertTrue(swu["rollback_checks"]["restored_from_1_1_4_11_backup"])
        self.assertEqual(
            swu["mcu_update_compare_version"]["mcu0_140_G32-mcu0_022_000"],
            "/tmp/fw_check/mcu0_140_G32-mcu0_022_950.bin",
        )
        self.assertEqual(swu["mcu_update_compare_version"]["cfs0_050_G32-cfs0_000_113"], "")


if __name__ == "__main__":
    unittest.main()
