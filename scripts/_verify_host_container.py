#!/usr/bin/env python3
"""Container-side ARMv7 verification: Buster Python 3.9.12 + target K2 OpenWrt chroot."""

from __future__ import annotations

import ast
import configparser
import json
import logging
import os
import platform
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace

import jinja2

KLIPPY_DIR = Path("/src/klippy")
K2_CONFIG_DIR = Path("/src/k2_mainline/config")
K2_SERVICES_SRC = Path("/src/k2_mainline/services")
MCU_DIR = Path("/src/mcu")
SERVICES_DIR = Path("/src/services")
STOCK_ROOTFS_TAR = Path("/src/stock-chroot.tar")
OUT_DIR = Path("/out")
REPORT_PATH = OUT_DIR / "verify_host_arm_report.json"

REQUIRED_SECTIONS = (
    "mcu",
    "mcu nozzle_mcu",
    "mcu rpi",
    "k2",
    "k2_display",
    "k2_print",
    "k2_mesh",
    "k2_cancel",
    "lis2dw",
    "resonance_tester",
    "load_cell_probe",
    "z_align",
    "motor_control",
    "box",
    "filament_rack",
    "serial_485 serial485",
    "fan_feedback",
    "belt_mdl mdlx",
    "belt_mdl mdly",
    "load_ai",
    "bl24c16f",
    "custom_macro",
    "auto_addr",
    "bed_mesh",
    "virtual_sdcard",
    "pause_resume",
    "exclude_object",
    "save_variables",
    "homing_override",
)

SCREEN_ENDPOINTS = (
    "info",
    "objects/list",
    "objects/query",
    "objects/subscribe",
    "gcode/script",
    "gcode/subscribe_output",
    "shakehands",
    "get_mesh",
    "getBootLoaderVersion",
    "get_cx_fan_status",
    "pause_resume/check_continue_print_state",
    "pause_resume/cancel_continue_print",
    "pause_resume/set_print_first_layer",
    "zdown_force_stop",
)


def _check_ast_only(klippy_dir: Path) -> dict[str, object]:
    py_files = sorted(klippy_dir.rglob("*.py"))
    for py_path in py_files:
        ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
    return {"ast_parsed_files": len(py_files)}


def _check_ast_and_import_test(klippy_dir: Path, python_exe: str) -> dict[str, object]:
    ast_info = _check_ast_only(klippy_dir)
    subprocess.run(
        [python_exe, "-B", str(klippy_dir / "klippy.py"), "--import-test"],
        check=True,
    )
    return {**ast_info, "import_test": "passed"}


def _check_jinja_templates(cfg_dir: Path) -> dict[str, object]:
    env = jinja2.Environment("{%", "%}", "{", "}")
    compiled: list[str] = []
    for cfg_path in sorted(cfg_dir.glob("*.cfg")):
        raw = cfg_path.read_text(encoding="utf-8")
        if "{#" in raw or "#}" in raw:
            raise AssertionError(f"Forbidden Jinja comment syntax in {cfg_path.name}")
        parser = configparser.RawConfigParser(strict=False, inline_comment_prefixes=(";", "#"))
        parser.read_string(raw, source=str(cfg_path))
        for section in parser.sections():
            for opt in ("gcode", "runout_gcode", "on_error_gcode"):
                if parser.has_option(section, opt):
                    env.from_string(parser.get(section, opt))
                    compiled.append(f"{cfg_path.name}:{section}:{opt}")
    return {"compiled_templates": len(compiled)}


def _check_printer_config_and_endpoints(
    klippy_dir: Path, cfg_root: Path, mcu_dir: Path, tmp_root: Path
) -> tuple[dict[str, object], dict[str, object]]:
    import numpy  # noqa: F401

    cfg_dir = tmp_root / "printer_data" / "config"
    gcodes_dir = tmp_root / "printer_data" / "gcodes"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    gcodes_dir.mkdir(parents=True, exist_ok=True)
    for cfg in cfg_root.glob("*.cfg"):
        shutil.copy2(cfg, cfg_dir / cfg.name)
    (cfg_dir / "k2_variables.cfg").write_text("[Variables]\n", encoding="utf-8")

    # On K2 OpenWrt rootfs (/etc/openwrt_release present), base_info.base_dir == '/mnt/UDISK'.
    for sub in ("creality/userdata/box", "creality/userdata/config"):
        Path("/mnt/UDISK", sub).mkdir(parents=True, exist_ok=True)
    Path("/mnt/UDISK/creality/userdata/box/material_database.json").write_text(
        json.dumps({"result": {"list": []}}), encoding="utf-8"
    )
    Path("/mnt/UDISK/creality/userdata/config/user_print_refer.json").write_text("{}", encoding="utf-8")
    Path("/mnt/UDISK/creality/userdata/config/system_config.json").write_text("{}", encoding="utf-8")
    os.environ["HOME"] = str(tmp_root)

    sys.path.insert(0, str(klippy_dir))
    import klippy  # noqa: E402
    import msgproto  # noqa: E402
    import reactor  # noqa: E402
    from webhooks import WebRequest, WebRequestError  # noqa: E402

    class ConfigOnlyPrinter(klippy.Printer):
        config_failure = None

        def _connect(self, eventtime):
            try:
                self._read_config()
            except BaseException:
                self.config_failure = sys.exc_info()
            finally:
                self.reactor.end()

    loop = reactor.Reactor()
    devnull_fd = os.open("/dev/null", os.O_RDONLY)
    printer = None
    try:
        start_args = {
            "config_file": str(cfg_dir / "printer.cfg"),
            "debuginput": "/dev/null",
            "gcode_fd": devnull_fd,
            "start_reason": "startup",
            "software_version": "mainline-v0.14.0-k2",
        }
        printer = ConfigOnlyPrinter(loop, None, start_args)
        loop.run()
        if printer.config_failure is not None:
            _, err, tb = printer.config_failure
            raise err.with_traceback(tb)

        for section_name in REQUIRED_SECTIONS:
            if printer.lookup_object(section_name, None) is None:
                raise AssertionError(f"Expected Klipper object not loaded: {section_name}")

        kin = printer.lookup_object("toolhead").get_kinematics()
        if type(kin).__name__ != "K2CoreXYKinematics":
            raise AssertionError(f"Unexpected kinematics class: {type(kin).__name__}")

        z_align = printer.lookup_object("z_align")
        if type(z_align).__name__ != "NativeZAlign":
            raise AssertionError(f"Expected NativeZAlign, got {type(z_align).__name__}")
        z_align.stop()
        if printer.is_shutdown() or z_align.force_stop_flag:
            raise AssertionError("Idle z_align.stop() triggered shutdown or latched force_stop_flag")

        # Verify single-owner PA0 chamber_fan + bridged output_pin fan1.
        fan1 = printer.lookup_object("output_pin fan1")
        cfan = printer.lookup_object("temperature_fan chamber_fan")
        gcode = printer.lookup_object("gcode")
        fan1_gcmd = gcode.create_gcode_command("SET_PIN", "SET_PIN", {"PIN": "fan1", "VALUE": "127.5"})
        fan1.cmd_SET_PIN(fan1_gcmd)
        if not fan1.manual_override or fan1.get_status(0.0)["value"] != 0.5:
            raise AssertionError(f"Unexpected ChamberFanBridge manual state: {fan1.get_status(0.0)}")
        cfan.set_tf_speed(1.0, 0.9)
        if not fan1.manual_override or fan1.get_status(0.0)["value"] != 0.5:
            raise AssertionError("Thermostat callback overwrote manual fan1 speed")
        cfan.set_temp(35.0)
        if fan1.manual_override:
            raise AssertionError("SET_TEMPERATURE_FAN_TARGET did not restore thermostat ownership")

        # Verify Qmode -> Qmode_exit restores chamber_fan thermostat instead of leaving manual override.
        fan0 = printer.lookup_object("output_pin fan0")
        fan2 = printer.lookup_object("output_pin fan2")
        pstats = printer.lookup_object("print_stats")
        toolhead_obj = printer.lookup_object("toolhead")
        prev_state = pstats.state
        prev_fan0_th = fan0.gcrq.toolhead
        prev_fan2_th = fan2.gcrq.toolhead
        prev_cfan_th = cfan.fan.gcrq.toolhead
        prev_cfan_req = cfan.fan.last_req_value
        prev_cfan_tgt = cfan.target_temp
        prev_handlers = gcode.gcode_handlers
        prev_is_ready = gcode.is_printer_ready
        qmode_errors: list[str] = []

        class _QmodeErrCapture(logging.Handler):
            def emit(self, record):
                if record.levelno >= logging.ERROR:
                    qmode_errors.append(record.getMessage())

        err_handler = _QmodeErrCapture()
        logging.getLogger().addHandler(err_handler)
        try:
            gcode.is_printer_ready = True
            gcode.gcode_handlers = gcode.ready_gcode_handlers
            pstats.state = "printing"
            fan0.gcrq.toolhead = toolhead_obj
            fan2.gcrq.toolhead = toolhead_obj
            cfan.fan.gcrq.toolhead = None
            cfan.set_temp(35.0)
            cfan.fan.last_req_value = 1.0
            gcode.run_script_from_command("Qmode")
            if not fan1.manual_override or fan1.get_status(0.0)["value"] >= 0.9:
                raise AssertionError(f"Qmode failed to throttle fan1: {fan1.get_status(0.0)}")
            gcode.run_script_from_command("Qmode_exit")
            if fan1.manual_override or cfan.target_temp != 35.0:
                raise AssertionError(
                    f"Qmode_exit trapped chamber_fan in manual mode: "
                    f"manual_override={fan1.manual_override}, target_temp={cfan.target_temp}"
                )

            # Verify fan1 starting below threshold + M106 P1 S255 inside Qmode restores full speed on Qmode_exit.
            cfan.set_temp(0.0)
            cfan.fan.last_req_value = 0.0
            gcode.run_script_from_command("SET_PIN PIN=fan1 VALUE=0")
            gcode.run_script_from_command("Qmode")
            if fan1.get_status(0.0)["value"] != 0.0:
                raise AssertionError(f"Unexpected initial fan1 value in Qmode: {fan1.get_status(0.0)}")
            gcode.run_script_from_command("M106 P1 S255")
            throttled_val = fan1.get_status(0.0)["value"]
            if not (0.3 < throttled_val < 0.95):
                raise AssertionError(f"M106 P1 S255 inside Qmode did not throttle fan1: {throttled_val}")
            gcode.run_script_from_command("Qmode_exit")
            restored_val = fan1.get_status(0.0)["value"]
            if restored_val < 0.99:
                raise AssertionError(f"Qmode_exit did not restore M106 P1 S255 fan1 speed: {restored_val}")

            # Verify M107 P1 inside Qmode clears throttled state and keeps fan1 off on Qmode_exit.
            gcode.run_script_from_command("Qmode")
            gcode.run_script_from_command("M106 P1 S255")
            gcode.run_script_from_command("M107 P1")
            gcode.run_script_from_command("Qmode_exit")
            if fan1.get_status(0.0)["value"] != 0.0:
                raise AssertionError(f"Qmode_exit revived fan1 after M107 P1: {fan1.get_status(0.0)}")

            # Verify M141 S28 inside Qmode restores thermostat target 28.0 on Qmode_exit.
            gcode.run_script_from_command("Qmode")
            gcode.run_script_from_command("M141 S28")
            gcode.run_script_from_command("Qmode_exit")
            if fan1.manual_override or cfan.target_temp != 28.0:
                raise AssertionError(
                    f"Qmode_exit failed to preserve M141 target 28.0: "
                    f"manual_override={fan1.manual_override}, target_temp={cfan.target_temp}"
                )

            if qmode_errors:
                raise AssertionError(f"Unexpected logging ERROR during Qmode test: {qmode_errors}")
        finally:
            logging.getLogger().removeHandler(err_handler)
            gcode.gcode_handlers = prev_handlers
            gcode.is_printer_ready = prev_is_ready
            pstats.state = prev_state
            fan0.gcrq.toolhead = prev_fan0_th
            fan2.gcrq.toolhead = prev_fan2_th
            cfan.fan.gcrq.toolhead = prev_cfan_th
            cfan.set_temp(prev_cfan_tgt)
            cfan.fan.last_req_value = prev_cfan_req

        # Verify K_LOAD_CELL_INIT state check works before initial calibration while unloaded_probe blocks.
        k2_obj = printer.lookup_object("k2")
        motor_obj = printer.lookup_object("motor_control")
        motor_obj.is_ready = True
        if printer.lookup_object("load_cell_probe")._load_cell.is_calibrated():
            raise AssertionError("Expected fresh default printer.cfg to require initial calibration")
        toolhead_obj = printer.lookup_object("toolhead")
        prev_pause = toolhead_obj.can_pause
        toolhead_obj.can_pause = False
        try:
            _ = k2_obj._check_init_state(gcode.error)
            try:
                k2_obj.unloaded_probe(gcode.error)
            except Exception:
                pass
            else:
                raise AssertionError("unloaded_probe unexpectedly passed while uncalibrated")
        finally:
            toolhead_obj.can_pause = prev_pause

        # Verify core c440x status objects produce valid get_status() dicts without crashing.
        for status_obj_name in (
            "gcode", "webhooks", "configfile", "mcu", "mcu nozzle_mcu", "mcu rpi",
            "gcode_move", "print_stats", "virtual_sdcard", "pause_resume",
            "k2_print", "k2_display", "fan_feedback", "output_pin fan1",
            "temperature_fan chamber_fan", "heater_bed", "extruder", "bed_mesh",
            "z_align", "motor_control", "load_cell_probe", "box", "toolhead",
        ):
            obj = printer.lookup_object(status_obj_name)
            st = obj.get_status(0.0)
            json.dumps(st)

        webhooks = printer.lookup_object("webhooks")
        missing_ep = [ep for ep in SCREEN_ENDPOINTS if ep not in webhooks._endpoints]
        if missing_ep:
            raise AssertionError(f"Missing screen webhook endpoints: {missing_ep}")

        req_fan = WebRequest(None, b'{"id": 776, "method": "get_cx_fan_status", "params": {}}')
        webhooks._endpoints["get_cx_fan_status"](req_fan)
        res_fan = req_fan.finish()["result"]
        if not isinstance(res_fan, dict) or printer.is_shutdown():
            raise AssertionError(f"get_cx_fan_status failed or shut down printer: {res_fan}")

        req_sh = WebRequest(None, json.dumps({"id": 777, "method": "shakehands", "params": {}}).encode())
        webhooks._endpoints["shakehands"](req_sh)
        res_sh = req_sh.finish()["result"]
        if res_sh["shakehands_id"] != 777 or not isinstance(res_sh["curtime"], float):
            raise AssertionError(f"Unexpected shakehands response: {res_sh}")

        req_mesh = WebRequest(None, b'{"id": 778, "method": "get_mesh"}')
        webhooks._endpoints["get_mesh"](req_mesh)
        res_mesh = req_mesh.finish()["result"]
        if res_mesh != {"probed_matrix": [[]]}:
            raise AssertionError(f"Unexpected get_mesh response: {res_mesh}")

        req_rec = WebRequest(
            None,
            b'{"id": 779, "method": "pause_resume/check_continue_print_state"}',
        )
        webhooks._endpoints["pause_resume/check_continue_print_state"](req_rec)
        res_rec = req_rec.finish()["result"]
        if res_rec != {"file_state": False, "eeprom_state": False, "supported": False}:
            raise AssertionError(f"Unexpected recovery status response: {res_rec}")

        req_cancel = WebRequest(None, b'{"id": 780, "method": "pause_resume/cancel_continue_print"}')
        try:
            webhooks._endpoints["pause_resume/cancel_continue_print"](req_cancel)
        except WebRequestError as exc:
            if not str(exc):
                raise AssertionError("Empty rejection error from pause_resume/cancel_continue_print")
        else:
            raise AssertionError("pause_resume/cancel_continue_print did not raise WebRequestError")

        req_first = WebRequest(None, b'{"id": 7801, "method": "pause_resume/set_print_first_layer"}')
        webhooks._endpoints["pause_resume/set_print_first_layer"](req_first)
        if req_first.finish()["result"] != {"state": "success"}:
            raise AssertionError("Unexpected pause_resume/set_print_first_layer response")
        vsd_status = printer.lookup_object("virtual_sdcard").get_status(0.0)
        if vsd_status.get("first_layer_stop") is not False:
            raise AssertionError(f"Missing virtual_sdcard.first_layer_stop: {vsd_status}")

        mcu_ver_path = Path("/tmp/.mcu_version")
        mcu_ver_path.write_text(
            json.dumps({"mcu0": {"version": "mcu0_140_G32-mcu0_022_950"}}), encoding="utf-8"
        )
        try:
            req_bl = WebRequest(None, b'{"id": 781, "method": "getBootLoaderVersion"}')
            webhooks._endpoints["getBootLoaderVersion"](req_bl)
            res_bl = req_bl.finish()["result"]
            if res_bl.get("software_version") != "mcu0_140_G32-mcu0_022_950":
                raise AssertionError(f"Unexpected getBootLoaderVersion from /tmp/.mcu_version: {res_bl}")
        finally:
            mcu_ver_path.unlink()
        req_bl_fb = WebRequest(None, b'{"id": 782, "method": "getBootLoaderVersion"}')
        webhooks._endpoints["getBootLoaderVersion"](req_bl_fb)
        if req_bl_fb.finish()["result"].get("software_version") != "mainline-v0.14.0-k2":
            raise AssertionError("Unexpected getBootLoaderVersion fallback")

        # Verify MCU command encoding against compiled GD32 dictionaries.
        main_mp = msgproto.MessageParser()
        main_mp.process_identify((mcu_dir / "main_klipper.dict").read_bytes(), decompress=False)
        nozzle_mp = msgproto.MessageParser()
        nozzle_mp.process_identify((mcu_dir / "nozzle_klipper.dict").read_bytes(), decompress=False)
        _ = main_mp.create_command("config_transparent oid=0")
        _ = main_mp.create_command(
            "config_fancheck oid=1 fan_num=1 "
            "fan0_pin=PC6 pull_up0=1 fan1_pin=PC6 pull_up1=1 "
            "fan2_pin=PC6 pull_up2=1 fan3_pin=PC6 pull_up3=1 "
            "fan4_pin=PC6 pull_up4=1"
        )
        _ = main_mp.create_command("query_gpio_status oid=0 gpio_pin=PA9")
        _ = nozzle_mp.create_command("config_lis2dw oid=2 bus_oid=0 bus_oid_type=spi lis_chip_type=LIS2DW")
        _ = nozzle_mp.create_command("config_cs1237 oid=3 config=12 dout_pin=PB14 sclk_pin=PB13")
        _ = nozzle_mp.create_command("config_trigger_analog oid=4 sos_filter_oid=5")

        handlers = sorted(printer.lookup_object("gcode").ready_gcode_handlers)
        for cmd in (
            "START_PRINT",
            "END_PRINT",
            "K_PRIME_LINE",
            "K_LOAD_CELL_INIT",
            "Z_ALIGN_ENDSTOPS",
            "K_QUERY_MOTORS",
            "BOX_SELECT",
            "CXSAVE_CONFIG",
            "T1",
            "T2",
            "T3",
            "T4",
        ):
            if cmd not in handlers:
                raise AssertionError(f"Missing required G-code command: {cmd}")

        # End-to-end _K_ENSURE_LOAD_CELL_CALIBRATED (auto K_LOAD_CELL_INIT at bottom_reference + CXSAVE_CONFIG).
        lc_probe = printer.lookup_object("load_cell_probe")
        cell = lc_probe._load_cell
        cfgfile = printer.lookup_object("configfile")
        orig_cfg_bytes_pre = (cfg_dir / "printer.cfg").read_bytes()
        prev_h_lc = gcode.gcode_handlers
        prev_r_lc = gcode.is_printer_ready
        prev_z_lim_lc = kin.limits[2]
        prev_pos_z_lc = toolhead_obj.commanded_pos[2]
        prev_wait_lc = toolhead_obj.wait_moves
        prev_avg_lc = cell.avg_counts
        prev_restart_lc = gcode.request_restart
        prev_pause_lc = toolhead_obj.can_pause
        prev_mono_lc = loop.monotonic
        try:
            gcode.is_printer_ready = True
            gcode.gcode_handlers = gcode.ready_gcode_handlers
            motor_obj.is_ready = True
            toolhead_obj.can_pause = False
            toolhead_obj.wait_moves = lambda: None
            kin.limits[2] = (-1.0, 350.0)
            toolhead_obj.commanded_pos[2] = 350.84
            # K_PREPARE_PROBE must still block while uncalibrated (defense-in-depth).
            try:
                gcode.run_script_from_command("K_PREPARE_PROBE")
            except Exception:
                pass
            else:
                raise AssertionError("K_PREPARE_PROBE unexpectedly passed while uncalibrated")

            auto_clock = [100.0]

            def _fast_mono():
                auto_clock[0] += 0.25
                return auto_clock[0]

            avg_calls = [0]

            def _mock_avg(num_samples=None):
                avg_calls[0] += 1
                return -1407220.0

            auto_restarts: list[str] = []
            loop.monotonic = _fast_mono
            cell.avg_counts = _mock_avg
            gcode.request_restart = lambda reason: auto_restarts.append(reason)

            gcode.run_script_from_command("_K_ENSURE_LOAD_CELL_CALIBRATED")
            if auto_restarts:
                raise AssertionError(f"CXSAVE_CONFIG unexpectedly requested restart: {auto_restarts}")
            if not cell.is_calibrated() or cell.reference_tare_counts != -1407220:
                raise AssertionError(
                    f"_K_ENSURE_LOAD_CELL_CALIBRATED failed: cal={cell.is_calibrated()}, ref={cell.reference_tare_counts}"
                )
            if cfgfile.autosave.save_config_pending or cfgfile.autosave.status_save_pending:
                raise AssertionError("CXSAVE_CONFIG left save_config_pending dirty")
            disk_auto_cfg = (cfg_dir / "printer.cfg").read_text(encoding="utf-8")
            if "#*# [load_cell_probe]" not in disk_auto_cfg or "#*# reference_tare_counts = -1407220" not in disk_auto_cfg:
                raise AssertionError("CXSAVE_CONFIG did not persist [load_cell_probe] to printer.cfg")

            # Second invocation when already calibrated must be a no-op.
            calls_before = avg_calls[0]
            gcode.run_script_from_command("_K_ENSURE_LOAD_CELL_CALIBRATED")
            if avg_calls[0] != calls_before:
                raise AssertionError("_K_ENSURE_LOAD_CELL_CALIBRATED re-ran init when already calibrated")

            # Reload saved printer.cfg in a fresh ConfigOnlyPrinter instance.
            loop_auto = reactor.Reactor()
            devnull_auto = os.open("/dev/null", os.O_RDONLY)
            printer_auto = None
            try:
                printer_auto = ConfigOnlyPrinter(
                    loop_auto,
                    None,
                    {
                        "config_file": str(cfg_dir / "printer.cfg"),
                        "debuginput": "/dev/null",
                        "gcode_fd": devnull_auto,
                        "start_reason": "startup",
                        "software_version": "mainline-v0.14.0-k2",
                    },
                )
                loop_auto.run()
                if printer_auto.config_failure is not None:
                    _, err_a, tb_a = printer_auto.config_failure
                    raise err_a.with_traceback(tb_a)
                auto_cell = printer_auto.lookup_object("load_cell_probe")._load_cell
                if not auto_cell.is_calibrated() or auto_cell.reference_tare_counts != -1407220:
                    raise AssertionError(
                        f"Auto-calibrated load_cell did not survive reload: {auto_cell.reference_tare_counts}"
                    )
            finally:
                if printer_auto is not None:
                    aio_a = printer_auto.lookup_object("aio_executor", None)
                    if aio_a is not None:
                        aio_a._handle_disconnect()
                loop_auto.finalize()
                os.close(devnull_auto)
        finally:
            loop.monotonic = prev_mono_lc
            toolhead_obj.can_pause = prev_pause_lc
            toolhead_obj.wait_moves = prev_wait_lc
            cell.avg_counts = prev_avg_lc
            gcode.request_restart = prev_restart_lc
            kin.limits[2] = prev_z_lim_lc
            toolhead_obj.commanded_pos[2] = prev_pos_z_lc
            gcode.gcode_handlers = prev_h_lc
            gcode.is_printer_ready = prev_r_lc
            (cfg_dir / "printer.cfg").write_bytes(orig_cfg_bytes_pre)
            for bkp_cfg in cfg_dir.glob("printer-*.cfg"):
                bkp_cfg.unlink()
            cell.reference_tare_counts = None
            cell.tare_counts = None
            if cfgfile.autosave.fileconfig.has_section("load_cell_probe"):
                cfgfile.autosave.fileconfig.remove_section("load_cell_probe")
            cfgfile.autosave.status_save_pending = {}
            cfgfile.autosave.save_config_pending = False

        # End-to-end K_LOAD_CELL_INIT + real upstream SAVE_CONFIG + config reload check.
        orig_wait_moves = toolhead_obj.wait_moves
        orig_avg_counts = cell.avg_counts
        orig_req_restart = gcode.request_restart
        orig_can_pause = toolhead_obj.can_pause
        restart_reasons: list[str] = []
        try:
            toolhead_obj.can_pause = False
            toolhead_obj.wait_moves = lambda: None
            cell.avg_counts = lambda num_samples=None: -123456.0
            gcode.request_restart = lambda reason: restart_reasons.append(reason)
            init_gcmd = gcode.create_gcode_command(
                "K_LOAD_CELL_INIT", "K_LOAD_CELL_INIT", {"STABLE_TIME": "0"}
            )
            k2_obj.cmd_load_cell_init(init_gcmd)
            if not cell.is_calibrated() or cell.reference_tare_counts != -123456 or cell.counts_per_gram != 100.0:
                raise AssertionError(
                    f"K_LOAD_CELL_INIT failed to calibrate: is_calibrated={cell.is_calibrated()}, "
                    f"ref={cell.reference_tare_counts}, cpg={cell.counts_per_gram}"
                )
            cfgfile.autosave.cmd_SAVE_CONFIG(gcode.create_gcode_command("SAVE_CONFIG", "SAVE_CONFIG", {}))
            if restart_reasons != ["restart"]:
                raise AssertionError(f"SAVE_CONFIG did not trigger expected restart: {restart_reasons}")
        finally:
            toolhead_obj.wait_moves = orig_wait_moves
            cell.avg_counts = orig_avg_counts
            gcode.request_restart = orig_req_restart
            toolhead_obj.can_pause = orig_can_pause

        loop_saved = reactor.Reactor()
        devnull_saved = os.open("/dev/null", os.O_RDONLY)
        printer_saved = None
        try:
            printer_saved = ConfigOnlyPrinter(
                loop_saved,
                None,
                {
                    "config_file": str(cfg_dir / "printer.cfg"),
                    "debuginput": "/dev/null",
                    "gcode_fd": devnull_saved,
                    "start_reason": "startup",
                    "software_version": "mainline-v0.14.0-k2",
                },
            )
            loop_saved.run()
            if printer_saved.config_failure is not None:
                _, err_s, tb_s = printer_saved.config_failure
                raise err_s.with_traceback(tb_s)
            saved_cell = printer_saved.lookup_object("load_cell_probe")._load_cell
            if not saved_cell.is_calibrated() or saved_cell.reference_tare_counts != -123456:
                raise AssertionError(
                    f"Saved calibration did not survive reload: is_calibrated={saved_cell.is_calibrated()}, "
                    f"ref={saved_cell.reference_tare_counts}"
                )
        finally:
            if printer_saved is not None:
                aio_s = printer_saved.lookup_object("aio_executor", None)
                if aio_s is not None:
                    aio_s._handle_disconnect()
            loop_saved.finalize()
            os.close(devnull_saved)

        # Verify standalone G29 auto-save in _K_START_READY (SAVE_MESH=0 leaves pending; SAVE_MESH=1 persists via CXSAVE_CONFIG).
        prev_h_mesh = gcode.gcode_handlers
        prev_r_mesh = gcode.is_printer_ready
        prev_restart_mesh = gcode.request_restart
        orig_cfg_bytes_mesh = (cfg_dir / "printer.cfg").read_bytes()
        try:
            gcode.is_printer_ready = True
            gcode.gcode_handlers = gcode.ready_gcode_handlers
            mesh_restarts: list[str] = []
            gcode.request_restart = lambda reason: mesh_restarts.append(reason)
            cfgfile.set("bed_mesh k2_load_cell", "version", "1")
            cfgfile.set("bed_mesh k2_load_cell", "points", "\n  0.000000, 0.010000\n  0.020000, 0.030000")
            cfgfile.set("bed_mesh k2_load_cell", "x_count", "2")
            cfgfile.set("bed_mesh k2_load_cell", "y_count", "2")
            cfgfile.set("bed_mesh k2_load_cell", "mesh_x_pps", "2")
            cfgfile.set("bed_mesh k2_load_cell", "mesh_y_pps", "2")
            cfgfile.set("bed_mesh k2_load_cell", "algo", "bicubic")
            cfgfile.set("bed_mesh k2_load_cell", "tension", "0.2")
            cfgfile.set("bed_mesh k2_load_cell", "min_x", "5.0")
            cfgfile.set("bed_mesh k2_load_cell", "max_x", "345.0")
            cfgfile.set("bed_mesh k2_load_cell", "min_y", "5.0")
            cfgfile.set("bed_mesh k2_load_cell", "max_y", "345.0")
            if not cfgfile.autosave.save_config_pending:
                raise AssertionError("Expected save_config_pending after staging bed_mesh k2_load_cell")

            gcode.run_script_from_command("_K_START_READY EXTRUDER_TEMP=220 PREPARE_ONLY=1 SAVE_MESH=0")
            if not cfgfile.autosave.save_config_pending:
                raise AssertionError("_K_START_READY SAVE_MESH=0 unexpectedly cleared save_config_pending")

            cfgfile.set("bed_mesh k2_adaptive", "version", "1")
            gcode.run_script_from_command("K_MESH_DISCARD_PENDING PROFILE=k2_adaptive")
            if "bed_mesh k2_adaptive" in cfgfile.autosave.status_save_pending:
                raise AssertionError("K_MESH_DISCARD_PENDING left bed_mesh k2_adaptive in status_save_pending")
            if cfgfile.autosave.fileconfig.has_section("bed_mesh k2_adaptive"):
                raise AssertionError("K_MESH_DISCARD_PENDING left bed_mesh k2_adaptive in autosave fileconfig")
            if not cfgfile.autosave.save_config_pending:
                raise AssertionError("K_MESH_DISCARD_PENDING cleared unrelated pending bed_mesh k2_load_cell")

            gcode.run_script_from_command("_K_START_READY EXTRUDER_TEMP=220 PREPARE_ONLY=1 SAVE_MESH=1")
            if mesh_restarts:
                raise AssertionError(f"_K_START_READY SAVE_MESH=1 unexpectedly requested restart: {mesh_restarts}")
            if cfgfile.autosave.save_config_pending:
                raise AssertionError("_K_START_READY SAVE_MESH=1 left save_config_pending True")
            mesh_cfg_disk = (cfg_dir / "printer.cfg").read_text(encoding="utf-8")
            if "#*# [bed_mesh k2_load_cell]" not in mesh_cfg_disk or "#*# [load_cell_probe]" not in mesh_cfg_disk:
                raise AssertionError("Standalone G29 CXSAVE_CONFIG did not persist bed_mesh k2_load_cell and load_cell_probe")

            loop_mesh = reactor.Reactor()
            devnull_mesh = os.open("/dev/null", os.O_RDONLY)
            printer_mesh = None
            try:
                printer_mesh = ConfigOnlyPrinter(
                    loop_mesh,
                    None,
                    {
                        "config_file": str(cfg_dir / "printer.cfg"),
                        "debuginput": "/dev/null",
                        "gcode_fd": devnull_mesh,
                        "start_reason": "startup",
                        "software_version": "mainline-v0.14.0-k2",
                    },
                )
                loop_mesh.run()
                if printer_mesh.config_failure is not None:
                    _, err_m, tb_m = printer_mesh.config_failure
                    raise err_m.with_traceback(tb_m)
                mesh_settings = printer_mesh.lookup_object("configfile").get_status(0.0)["settings"]
                if "bed_mesh k2_load_cell" not in mesh_settings:
                    raise AssertionError("Saved bed_mesh k2_load_cell missing after reload")
            finally:
                if printer_mesh is not None:
                    aio_m = printer_mesh.lookup_object("aio_executor", None)
                    if aio_m is not None:
                        aio_m._handle_disconnect()
                loop_mesh.finalize()
                os.close(devnull_mesh)
        finally:
            gcode.request_restart = prev_restart_mesh
            gcode.gcode_handlers = prev_h_mesh
            gcode.is_printer_ready = prev_r_mesh
            (cfg_dir / "printer.cfg").write_bytes(orig_cfg_bytes_mesh)
            if cfgfile.autosave.fileconfig.has_section("bed_mesh k2_load_cell"):
                cfgfile.autosave.fileconfig.remove_section("bed_mesh k2_load_cell")
            cfgfile.autosave.status_save_pending = {}
            cfgfile.autosave.save_config_pending = False

        # Verify screen M141 bare/empty syntax, BED_TEMP=0 cold plate, K_Z_CLEARANCE at bottom_reference, and CUSTOM_MACRO.
        prev_handlers_s4 = gcode.gcode_handlers
        prev_ready_s4 = gcode.is_printer_ready
        prev_cfan_tgt_s4 = cfan.target_temp
        prev_z_lim_s4 = kin.limits[2]
        prev_pos_z_s4 = toolhead_obj.commanded_pos[2]
        prev_wait_s4 = toolhead_obj.wait_moves
        cm_obj = printer.lookup_object("custom_macro")
        prev_cm_run = cm_obj.gcode.run_script_from_command
        prev_cm_state = (
            cm_obj.default_bed_temp,
            cm_obj.default_extruder_temp,
            cm_obj.extruder_temp,
            cm_obj.bed_temp,
            cm_obj.g28_ext_temp,
            cm_obj.leveling_calibration,
        )
        orig_start_print = gcode.ready_gcode_handlers["START_PRINT"]
        try:
            gcode.is_printer_ready = True
            gcode.gcode_handlers = gcode.ready_gcode_handlers

            g29_calls: list[dict[str, str]] = []
            gcode.ready_gcode_handlers["START_PRINT"] = (
                lambda gcmd: g29_calls.append(dict(gcode._get_extended_params(gcmd).get_command_parameters()))
            )
            gcode.run_script_from_command("G29 BED_TEMP80")
            gcode.run_script_from_command("G29 BED_TEMP=80 EXTRUDER_TEMP=220")
            gcode.run_script_from_command("G29")
            expected_g29 = [
                {"BED_TEMP": "80", "SOAK": "0", "CALIBRATE": "1", "PREPARE_ONLY": "1"},
                {"BED_TEMP": "80", "EXTRUDER_TEMP": "220", "SOAK": "0", "CALIBRATE": "1", "PREPARE_ONLY": "1"},
                {"SOAK": "0", "CALIBRATE": "1", "PREPARE_ONLY": "1"},
            ]
            if g29_calls != expected_g29:
                raise AssertionError(f"G29 screen parameter normalization mismatch: got={g29_calls}, expected={expected_g29}")
            gcode.ready_gcode_handlers["START_PRINT"] = orig_start_print

            coarse_home = printer.lookup_object("gcode_macro _K_HOME_Z_ONLY").template
            accept_home = printer.lookup_object("gcode_macro _K_ACCEPT_Z_HOME").template
            home_settings = {
                "k2_print": {"language": "en"},
                "load_cell_probe": {"z_offset": 0.0, "sample_retract_dist": 2.0},
            }
            optical_frame = {
                "configfile": {"settings": home_settings},
                "gcode_macro K_HOME_SETTINGS": {"errors": {"en": {"contact": "Invalid contact"}}},
                "load_cell_probe": {"is_last_tap_valid": True, "last_probe_position": [175, 175, -0.675, 0]},
                "toolhead": {"position": SimpleNamespace(z=2.28)},
            }
            home_script = coarse_home.render({"printer": optical_frame})
            if "SAMPLE_RETRACT_DIST=3.0" not in home_script or "_K_ACCEPT_Z_HOME RETRACT=3.0" not in home_script:
                raise AssertionError("Coarse homing must use the same clearance for the tap and acceptance")
            accept_script = accept_home.render({"printer": optical_frame, "params": {"RETRACT": "3.0"}})
            if "SET_HOMED=Z" not in accept_script:
                raise AssertionError("Coarse homing did not accept a valid negative-frame contact")

            gcode.run_script_from_command("M141")
            gcode.run_script_from_command("M141 S")
            if cfan.target_temp != 35.0:
                raise AssertionError(f"Empty M141 S did not set default cooling target 35.0: {cfan.target_temp}")
            try:
                gcode.run_script_from_command("M141 S-5")
            except Exception:
                pass
            else:
                raise AssertionError("M141 S-5 unexpectedly passed")

            gcode.run_script_from_command("K_PRINT_PARAMETERS BED_TEMP=0 EXTRUDER_TEMP=210")
            if cm_obj.default_bed_temp != 0.0 or cm_obj.default_extruder_temp != 210.0:
                raise AssertionError("K_PRINT_PARAMETERS failed for cold bed BED_TEMP=0")

            kin.limits[2] = (-1.0, 350.0)
            toolhead_obj.commanded_pos[2] = 350.84
            toolhead_obj.wait_moves = lambda: None
            gcode.run_script_from_command("K_Z_CLEARANCE")
            if toolhead_obj.commanded_pos[2] != 350.84:
                raise AssertionError(f"K_Z_CLEARANCE moved Z unexpectedly from bottom_reference: {toolhead_obj.commanded_pos[2]}")

            cm_cmds: list[str] = []
            cm_obj.gcode.run_script_from_command = lambda cmd: cm_cmds.append(cmd)
            cm_obj.cmd_CX_ROUGH_G28(
                gcode.create_gcode_command("CX_ROUGH_G28", "CX_ROUGH_G28", {"EXTRUDER_TEMP": "220", "BED_TEMP": "60"})
            )
            cm_obj.cmd_CX_NOZZLE_CLEAR(gcode.create_gcode_command("CX_NOZZLE_CLEAR", "CX_NOZZLE_CLEAR", {}))
            cm_obj.cmd_CX_PRINT_LEVELING_CALIBRATION(
                gcode.create_gcode_command("CX_PRINT_LEVELING_CALIBRATION", "CX_PRINT_LEVELING_CALIBRATION", {})
            )
            cm_obj.cmd_CX_PRINT_DRAW_ONE_LINE(
                gcode.create_gcode_command("CX_PRINT_DRAW_ONE_LINE", "CX_PRINT_DRAW_ONE_LINE", {})
            )
            joined_cm = "\n".join(cm_cmds)
            if (
                "G28" not in cm_cmds
                or "NOZZLE_CLEAR" not in cm_cmds
                or "G29 BED_TEMP=60 EXTRUDER_TEMP=220" not in cm_cmds
                or "M109 S220" not in cm_cmds
                or "CHECK_BED_MESH" in joined_cm
                or "ACCEL_TO_DECEL" in joined_cm
            ):
                raise AssertionError(f"Unexpected CUSTOM_MACRO commands: {cm_cmds}")
        finally:
            gcode.ready_gcode_handlers["START_PRINT"] = orig_start_print
            cm_obj.gcode.run_script_from_command = prev_cm_run
            (
                cm_obj.default_bed_temp,
                cm_obj.default_extruder_temp,
                cm_obj.extruder_temp,
                cm_obj.bed_temp,
                cm_obj.g28_ext_temp,
                cm_obj.leveling_calibration,
            ) = prev_cm_state
            kin.limits[2] = prev_z_lim_s4
            toolhead_obj.commanded_pos[2] = prev_pos_z_s4
            toolhead_obj.wait_moves = prev_wait_s4
            cfan.set_temp(prev_cfan_tgt_s4)
            gcode.gcode_handlers = prev_handlers_s4
            gcode.is_printer_ready = prev_ready_s4

        return (
            {
                "objects_created": len(printer.objects),
                "kinematics": type(kin).__name__,
                "z_align": type(z_align).__name__,
                "numpy_version": numpy.__version__,
                "registered_commands_count": len(handlers),
                "qmode_chamber_fan_restore_verified": True,
                "qmode_m106_m107_m141_verified": True,
                "load_cell_init_save_config_verified": True,
                "m141_screen_compat_verified": True,
                "cold_bed_start_print_verified": True,
                "z_clearance_bottom_ref_verified": True,
                "custom_macro_screen_compat_verified": True,
                "g29_legacy_screen_params_verified": True,
                "auto_load_cell_init_verified": True,
                "g29_auto_save_mesh_verified": True,
            },
            {
                "endpoints_verified": list(SCREEN_ENDPOINTS),
                "shakehands_keys": sorted(res_sh.keys()),
                "mesh_empty_shape": res_mesh,
                "recovery_status": res_rec,
                "bootloader_response": res_bl,
            },
        )
    finally:
        if printer is not None:
            aio = printer.lookup_object("aio_executor", None)
            if aio is not None:
                aio._handle_disconnect()
        loop.finalize()
        os.close(devnull_fd)


def _check_driver_functional(cfg_dir: Path, tmp_root: Path) -> dict[str, object]:
    from extras import box_wrapper, k2, k2_mesh, k2_native, motor_control  # noqa: E402
    import gcode  # noqa: E402

    ffi, lib = k2_native.get_ffi()
    crc8 = int(lib.msgblock_485_crc8(b"123456789", 9))
    if crc8 != 0xF4:
        raise AssertionError(f"msgblock_485_crc8 mismatch: 0x{crc8:02x}")
    src_rgb = ffi.new("rgb_t *", {"r": 0, "g": 0, "b": 0})[0]
    dst_rgb = ffi.new("rgb_t *", {"r": 255, "g": 255, "b": 255})[0]
    vol = int(lib.get_flushing_volume(src_rgb, dst_rgb))
    if vol <= 0:
        raise AssertionError(f"Invalid flushing volume: {vol}")

    # motor_control protection decoder
    payload = bytearray([0xF7, 0x81, 11, 0, 0x0C, 0x05, 0, 0, 0, 0, 0, 0, 0])
    crc = 0
    for byte in payload[2:]:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ (0x07 if crc & 0x80 else 0)) & 0xFF
    payload.append(crc)
    bits = motor_control.decode_protection_response(bytes(payload), 0x81)
    if bits[0] != "1" or bits[2] != "1" or len(bits) != 32:
        raise AssertionError(f"Unexpected protection bits: {bits}")
    try:
        motor_control.decode_protection_response(bytes(payload[:-1] + bytes([crc ^ 1])), 0x81)
    except ValueError:
        pass
    else:
        raise AssertionError("decode_protection_response accepted bad CRC")

    # k2_mesh outliers
    grid = [
        SimpleNamespace(
            bed_x=float(x),
            bed_y=float(y),
            bed_z=0.1 * x + 0.05 * y + (1.0 if (x, y) == (1, 1) else 0.0),
        )
        for y in range(3)
        for x in range(3)
    ]
    out = k2_mesh.outliers(grid, 0.3)
    if len(out) != 1 or out[0][1] != 4:
        raise AssertionError(f"Unexpected outlier detection result: {out}")

    # k2 load cell helpers
    tare_samples = [[i / 1280.0, 0.0, -100000 + (i % 7)] for i in range(400)]
    tw = k2.probe_tare_window(tare_samples, 1280.0, (-8388608, 8388607))
    if not (-100010 < tw["mean_raw"] < -99990):
        raise AssertionError(f"Unexpected tare window: {tw}")

    desc_samples = [[0.0 + 0.002 * i, 5.0 * i, 0.0] for i in range(80)]
    ds = k2.descent_samples(desc_samples, 0.158, lambda t: 1.0 - t)
    if len(ds) < 6 or ds[0][1] >= ds[-1][1]:
        raise AssertionError("Unexpected descent_samples output ordering")
    sig, noise = k2.tap_fit_quality(ds, contact=0.95, slope=-50.0)
    if sig <= 0 or noise < 0:
        raise AssertionError(f"Invalid tap_fit_quality: {(sig, noise)}")

    # box_wrapper slot persistence + material DB cache + BoxError inheritance
    slot_file = tmp_root / "last_slot.json"
    box_wrapper._save_last_slot("T2C", str(slot_file))
    if box_wrapper._load_last_slot(str(slot_file)) != "T2C":
        raise AssertionError("CFS _save_last_slot/_load_last_slot roundtrip failed")
    db_file = tmp_root / "mat_db.json"
    db_file.write_text('{"result": {"list": ["PLA"]}}', encoding="utf-8")
    with db_file.open("r") as s1:
        d1 = box_wrapper._material_database(s1)
    with db_file.open("r") as s2:
        d2 = box_wrapper._material_database(s2)
    if d1 is not d2:
        raise AssertionError("_material_database failed to return cached object")
    if not issubclass(box_wrapper.BoxError, gcode.CommandError):
        raise AssertionError("BoxError must inherit from gcode.CommandError")
    if not box_wrapper.key_exists({"a": [{"b": 1}]}, "b"):
        raise AssertionError("key_exists helper failed")

    # k2_sync_config checks including all-obsolete [stepper_z] pruning
    sys.path.insert(0, str(cfg_dir))
    import k2_sync_config  # noqa: E402

    sync_root = tmp_root / "sync_test"
    fresh_dir = sync_root / "fresh"
    res_fresh = k2_sync_config.sync_configs(cfg_dir, fresh_dir)
    if res_fresh != "fresh" or not (fresh_dir / "printer.cfg").is_file():
        raise AssertionError(f"k2_sync_config fresh failed: {res_fresh}")

    upgrade_dir = sync_root / "printer_data" / "config"
    upgrade_dir.mkdir(parents=True, exist_ok=True)
    for cfg in cfg_dir.glob("*.cfg"):
        if cfg.name != "printer.cfg":
            shutil.copy2(cfg, upgrade_dir / cfg.name)
    custom_box = "# user box config\n" + (cfg_dir / "box.cfg").read_text(encoding="utf-8")
    (upgrade_dir / "box.cfg").write_text(custom_box, encoding="utf-8")
    (upgrade_dir / "k2_variables.cfg").write_text("[Variables]\nk2_prime_row = 3\n", encoding="utf-8")
    stock_printer = (
        "# Version: v1.1.4\n"
        "[stepper_z]\nstep_pin: PB6\n\n"
        "#*# <---------------------- SAVE_CONFIG ---------------------->\n"
        "#*# DO NOT EDIT THIS BLOCK OR BELOW. The contents are auto-generated.\n"
        "#*#\n"
        "#*# [prtouch_v3]\n"
        "#*# z_offset = 0.0\n"
        "#*#\n"
        "#*# [temperature_sensor mcu_temp]\n"
        "#*# min_temp = 0\n"
        "#*#\n"
        "#*# [stepper_z]\n"
        "#*# position_endstop = 0.12\n"
        "#*#\n"
        "#*# [extruder]\n"
        "#*# control = pid\n"
        "#*# pid_kp = 50.0\n"
        "#*# pid_ki = 1.5\n"
        "#*# pid_kd = 45.0\n"
        "#*#\n"
        "#*# [heater_bed]\n"
        "#*# control = pid\n"
        "#*# pid_kp = 55.0\n"
        "#*# pid_ki = 0.25\n"
        "#*# pid_kd = 300.0\n"
        "#*#\n"
        "#*# [bed_mesh default]\n"
        "#*# version = 1\n"
        "#*# points =\n"
        "#*# \t  0.01, 0.02\n"
        "#*# \t  0.03, 0.04\n"
        "#*# x_count = 2\n"
        "#*# y_count = 2\n"
        "#*# mesh_x_pps = 2\n"
        "#*# mesh_y_pps = 2\n"
        "#*# algo = lagrange\n"
        "#*# tension = 0.2\n"
        "#*# min_x = 10.0\n"
        "#*# max_x = 100.0\n"
        "#*# min_y = 10.0\n"
        "#*# max_y = 100.0\n"
        "#*#\n"
        "#*# [box]\n"
        "#*# cut_pos_x = -7.10\n"
        "#*#\n"
        "#*# [auto_addr]\n"
        "#*# mb_addr_table_uniids = 0x64\n"
        "#*#\n"
        "#*# [load_cell_probe]\n"
        "#*# reference_tare_counts = -1407220\n"
    )
    (upgrade_dir / "printer.cfg").write_text(stock_printer, encoding="utf-8")
    res_up = k2_sync_config.sync_configs(cfg_dir, upgrade_dir)
    if res_up != "upgraded:1.1.4->9.5.3":
        raise AssertionError(f"Unexpected upgrade result: {res_up}")
    merged_text = (upgrade_dir / "printer.cfg").read_text(encoding="utf-8")
    marker = "#*# <---------------------- SAVE_CONFIG ---------------------->"
    save_tail = merged_text[merged_text.index(marker) :]
    for forbidden in ("#*# [prtouch_v3]", "#*# [temperature_sensor mcu_temp]", "#*# [stepper_z]"):
        if forbidden in merged_text:
            raise AssertionError(f"Obsolete SAVE_CONFIG token survived: {forbidden}")
    if "position_endstop" in save_tail:
        raise AssertionError("Obsolete SAVE_CONFIG token survived: position_endstop")
    for required in ("#*# [box]", "cut_pos_x = -7.10", "#*# [auto_addr]", "reference_tare_counts = -1407220"):
        if required not in merged_text:
            raise AssertionError(f"Expected SAVE_CONFIG token lost: {required}")
    if (upgrade_dir / "box.cfg").read_text(encoding="utf-8") != custom_box:
        raise AssertionError("k2_sync_config overwrote user box.cfg")
    if "k2_prime_row = 3" not in (upgrade_dir / "k2_variables.cfg").read_text(encoding="utf-8"):
        raise AssertionError("k2_sync_config modified k2_variables.cfg")
    if not list((upgrade_dir / "backups").glob("backup-1.1.4-*")):
        raise AssertionError("k2_sync_config failed to back up old config")
    res_idem = k2_sync_config.sync_configs(cfg_dir, upgrade_dir)
    if res_idem != "unchanged":
        raise AssertionError(f"Expected unchanged on second sync, got {res_idem}")

    # Load migrated printer.cfg through real upstream Klipper ConfigAutoSave and verify effective settings!
    import klippy  # noqa: E402
    import reactor  # noqa: E402

    class MigratedCheckPrinter(klippy.Printer):
        config_failure = None

        def _connect(self, eventtime):
            try:
                self._read_config()
            except BaseException:
                self.config_failure = sys.exc_info()
            finally:
                self.reactor.end()

    loop2 = reactor.Reactor()
    devnull2 = os.open("/dev/null", os.O_RDONLY)
    printer2 = None
    migrated_pid_verified = False
    try:
        os.environ["HOME"] = str(sync_root)
        printer2 = MigratedCheckPrinter(
            loop2,
            None,
            {
                "config_file": str(upgrade_dir / "printer.cfg"),
                "debuginput": "/dev/null",
                "gcode_fd": devnull2,
                "start_reason": "startup",
                "software_version": "mainline-v0.14.0-k2",
            },
        )
        loop2.run()
        if printer2.config_failure is not None:
            _, err2, tb2 = printer2.config_failure
            raise err2.with_traceback(tb2)
        settings = printer2.lookup_object("configfile").get_status(0.0)["settings"]
        if settings["extruder"]["pid_kp"] != 50.0:
            raise AssertionError(f"Saved extruder pid_kp shadowed: {settings['extruder']['pid_kp']}")
        if settings["heater_bed"]["pid_kp"] != 55.0:
            raise AssertionError(f"Saved heater_bed pid_kp shadowed: {settings['heater_bed']['pid_kp']}")
        if settings["load_cell_probe"]["reference_tare_counts"] != -1407220:
            raise AssertionError(
                f"Saved reference_tare_counts lost: {settings['load_cell_probe'].get('reference_tare_counts')}"
            )
        if "bed_mesh default" not in settings or "prtouch_v3" in settings:
            raise AssertionError("Migrated bed_mesh default missing or prtouch_v3 present")
        migrated_pid_verified = True
    finally:
        if printer2 is not None:
            aio2 = printer2.lookup_object("aio_executor", None)
            if aio2 is not None:
                aio2._handle_disconnect()
        loop2.finalize()
        os.close(devnull2)

    # Verify legacy k2_load_cell.cfg reference_tare_counts migration when SAVE_CONFIG lacked [load_cell_probe].
    legacy_dir = sync_root / "legacy_lc" / "printer_data" / "config"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    for cfg in cfg_dir.glob("*.cfg"):
        if cfg.name != "printer.cfg":
            shutil.copy2(cfg, legacy_dir / cfg.name)
    (legacy_dir / "k2_variables.cfg").write_text("[Variables]\n", encoding="utf-8")
    (legacy_dir / "k2_load_cell.cfg").write_text(
        "[load_cell_probe]\ncounts_per_gram: 100\nz_offset: 0\nreference_tare_counts: -1407220\n",
        encoding="utf-8",
    )
    stock_no_lc = "\n".join(
        line for line in stock_printer.splitlines()
        if "#*# [load_cell_probe]" not in line and "reference_tare_counts" not in line
    ) + "\n"
    (legacy_dir / "printer.cfg").write_text(stock_no_lc, encoding="utf-8")
    if k2_sync_config.sync_configs(cfg_dir, legacy_dir) != "upgraded:1.1.4->9.5.3":
        raise AssertionError("Legacy k2_load_cell migration did not run expected upgrade")

    loop3 = reactor.Reactor()
    devnull3 = os.open("/dev/null", os.O_RDONLY)
    printer3 = None
    legacy_load_cell_migration_verified = False
    try:
        os.environ["HOME"] = str(sync_root / "legacy_lc")
        printer3 = MigratedCheckPrinter(
            loop3,
            None,
            {
                "config_file": str(legacy_dir / "printer.cfg"),
                "debuginput": "/dev/null",
                "gcode_fd": devnull3,
                "start_reason": "startup",
                "software_version": "mainline-v0.14.0-k2",
            },
        )
        loop3.run()
        if printer3.config_failure is not None:
            _, err3, tb3 = printer3.config_failure
            raise err3.with_traceback(tb3)
        migrated_cell = printer3.lookup_object("load_cell_probe")._load_cell
        if not migrated_cell.is_calibrated() or migrated_cell.reference_tare_counts != -1407220:
            raise AssertionError(
                f"Legacy k2_load_cell migration failed to calibrate: {migrated_cell.reference_tare_counts}"
            )
        legacy_load_cell_migration_verified = True
    finally:
        if printer3 is not None:
            aio3 = printer3.lookup_object("aio_executor", None)
            if aio3 is not None:
                aio3._handle_disconnect()
        loop3.finalize()
        os.close(devnull3)
        os.environ["HOME"] = str(sync_root)

    # Verify non-idempotent RS-485 retries_en=False executes only a single attempt & Z_ALIGN idle stop is safe.
    import threading  # noqa: E402
    from extras import k2_cancel, serial_485_wrapper, z_align  # noqa: E402

    s485 = object.__new__(serial_485_wrapper.Serialhdl_485)
    s485.lock = threading.Lock()
    s485.mutex = threading.Lock()
    s485.send_notifications = {}
    s485.pending_notifications = {}
    comp = SimpleNamespace(test=lambda: False, complete=lambda _v: None)
    s485.pending_notifications["#addr_1#cmd_5"] = [comp]
    s485.is_connected = lambda: True
    sends = []

    def _fake_wait_ack(cmd, notifications, timeout):
        sends.append(cmd)
        return None

    s485.raw_send_wait_ack = _fake_wait_ack
    rs_records: list[logging.LogRecord] = []

    class _RsHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            rs_records.append(record)

    rs_handler = _RsHandler()
    logging.getLogger().addHandler(rs_handler)
    try:
        res_rs = s485.get_response(b"\xf7\x01\x04\xff\x05\x00", 1.0, "#addr_1", "#cmd_5", retries_en=False)
        if res_rs is not None or len(sends) != 1:
            raise AssertionError(f"Non-idempotent RS-485 retried unexpectedly: sends={len(sends)}")
        if not any(
            r.levelno >= logging.WARNING and "non-idempotent RS-485 command timed out without retry" in r.getMessage()
            for r in rs_records
        ):
            raise AssertionError("Addressed non-idempotent RS-485 timeout did not log warning")

        rs_records.clear()
        sends.clear()
        s485.pending_notifications["#addr_254#cmd_161"] = [comp]
        res_bcast = s485.get_response(b"\xfe\x05\x00\xa1\xfe\xfe", 0.3, "#addr_254", "#cmd_161", retries_en=False)
        if res_bcast is not None or len(sends) != 1:
            raise AssertionError(f"Broadcast RS-485 poll failed expectations: res={res_bcast}, sends={len(sends)}")
        if any(r.levelno >= logging.INFO for r in rs_records):
            raise AssertionError(f"Broadcast 0xfe RS-485 poll emitted INFO/WARNING log noise: {[r.getMessage() for r in rs_records if r.levelno >= logging.INFO]}")
    finally:
        logging.getLogger().removeHandler(rs_handler)

    # Full BoxAction.box_extrude_material() lost-ACK path: single EXTRUDE_PROCESS attempt, ZERO RETRUDE_PROCESS.
    act = object.__new__(box_wrapper.BoxAction)
    act.parse = box_wrapper.ParseData()
    act.box_state = SimpleNamespace(
        get_Tnn_content=lambda tnn: {"addr": 1, "num": "A"},
        get_Tn_data=lambda *a, **kw: None,
        modify_Tn_data=lambda *a, **kw: None,
    )
    act.box_save = SimpleNamespace(last_cmd=None, last_cmd_tmp=None, error=None, recode_err=lambda *a, **kw: None)
    act.boxcfg = SimpleNamespace(version=1, safe_pos_y=345.0)
    act.remain_material = SimpleNamespace(type="PLA")
    act.print_stats = SimpleNamespace(state="standby")
    act.toolhead = SimpleNamespace(
        get_position=lambda: [100.0, 200.0, 0.0],
        get_extruder=lambda: SimpleNamespace(get_heater=lambda: SimpleNamespace(can_extrude=True)),
    )
    act.printer = SimpleNamespace(lookup_object=lambda name, default=None: SimpleNamespace(gcode_metadata=None))
    act.gcode = SimpleNamespace(run_script_from_command=lambda *_a, **_kw: None)
    act.reactor = SimpleNamespace(monotonic=lambda: 0.0, pause=lambda _t: None)
    act.check_connect = lambda _addr: False
    act.go_to_extrude_pos = lambda: None
    act.is_material_available = lambda _tnn: True
    act.get_flush_velocity = lambda _a, _b: ([360], 360)
    act.get_flush_temp = lambda _a, _b: (220, 220, 220)
    act.quickly_wait_heating = lambda _t: None
    act.timeout_process = lambda _addr: None
    act.extrude_process_ret_state = None
    act._last_send_status = None
    sent_cmds: list[bytes] = []

    def _fake_cmd_send(data_send, timeout, retries_en=True):
        cmd_b = data_send[3:4]
        sent_cmds.append(cmd_b)
        if cmd_b in (box_wrapper.Box_cmd["EXTRUDE_PROCESS"], box_wrapper.Box_cmd["RETRUDE_PROCESS"]):
            if retries_en:
                raise AssertionError("Non-idempotent CFS command sent with retries_en=True")
            return None
        return bytes([0xF7, data_send[0], 3, 0x00, cmd_b[0], 0x00])

    act._serial = SimpleNamespace(cmd_send_data_with_response=_fake_cmd_send)
    if act.box_extrude_material("T1A") is not False:
        raise AssertionError("box_extrude_material unexpectedly succeeded on lost EXTRUDE_PROCESS ACK")
    extrude_cnt = sent_cmds.count(box_wrapper.Box_cmd["EXTRUDE_PROCESS"])
    retrude_cnt = sent_cmds.count(box_wrapper.Box_cmd["RETRUDE_PROCESS"])
    if extrude_cnt != 1 or retrude_cnt != 0:
        raise AssertionError(
            f"Upper-layer CFS retry triggered after lost ACK: extrude={extrude_cnt}, retrude={retrude_cnt}"
        )

    # auto_retry_extruder_gear: if box_retrude_material() hits TIMEOUT, MUST return False with ZERO EXTRUDE_PROCESS!
    act_gear = object.__new__(box_wrapper.BoxAction)
    act_gear.parse = box_wrapper.ParseData()
    act_gear.box_state = SimpleNamespace(
        get_Tnn_content=lambda tnn: {"addr": 1, "num": "A"},
        get_Tn_data=lambda *a, **kw: None,
        get_Tn_inner_data=lambda *a, **kw: 0x01,
        modify_Tn_data=lambda *a, **kw: None,
    )
    act_gear.box_save = SimpleNamespace(
        last_cmd=None, last_cmd_tmp=None, error=None, error_tnn={}, recode_err=lambda *a, **kw: None
    )
    act_gear.boxcfg = SimpleNamespace(version=1, safe_pos_y=345.0, Tn_extrude_temp=220)
    act_gear.remain_material = SimpleNamespace(type="PLA")
    act_gear.print_stats = SimpleNamespace(state="printing")
    act_gear.toolhead = act.toolhead
    act_gear.printer = act.printer
    act_gear.gcode = act.gcode
    act_gear.reactor = act.reactor
    act_gear.is_shutdown = False
    act_gear.check_connect = lambda addr: addr == 1
    act_gear.get_filament_sensor_detect = lambda: False
    act_gear.communication_get_filament_sensor_state = lambda *a, **kw: None
    act_gear.move_to_cut = lambda: True
    act_gear.go_to_extrude_pos = lambda: None
    act_gear.retrude_process_clear_flag = lambda: None
    act_gear.timeout_process = lambda _addr: None
    act_gear._last_send_status = "EXTRUDE_ERR8"
    gear_cmds: list[bytes] = []

    def _fake_cmd_send_gear(data_send, timeout, retries_en=True):
        cmd_b = data_send[3:4]
        gear_cmds.append(cmd_b)
        if cmd_b in (box_wrapper.Box_cmd["EXTRUDE_PROCESS"], box_wrapper.Box_cmd["RETRUDE_PROCESS"]):
            if retries_en:
                raise AssertionError("Non-idempotent gear-retry CFS command sent with retries_en=True")
            return None
        return bytes([0xF7, data_send[0], 3, 0x00, cmd_b[0], 0x00])

    act_gear._serial = SimpleNamespace(cmd_send_data_with_response=_fake_cmd_send_gear)
    gear_res = act_gear.extrude_process_auto_retry_process("T1A", "auto_retry_extruder_gear", [0, 0, 0, 0])
    gear_retrude_cnt = gear_cmds.count(box_wrapper.Box_cmd["RETRUDE_PROCESS"])
    gear_extrude_cnt = gear_cmds.count(box_wrapper.Box_cmd["EXTRUDE_PROCESS"])
    if gear_res is not False or gear_retrude_cnt != 1 or gear_extrude_cnt != 0:
        raise AssertionError(
            f"auto_retry_extruder_gear proceeded after RETRUDE_PROCESS timeout: "
            f"res={gear_res}, retrude={gear_retrude_cnt}, extrude={gear_extrude_cnt}"
        )

    # Subsequent retry entry while transport is uncertain ('TIMEOUT') MUST fail before cutting or retruding.
    gear_cmds.clear()
    if act_gear.extrude_process_auto_retry_process("T1A", "auto_retry_extruder_gear", [0, 0, 0, 0]) is not False or gear_cmds:
        raise AssertionError(f"auto_retry_extruder_gear executed commands after uncertain transport: {gear_cmds}")

    # stage=6 CRC_ERR latched even if stage=5 later responds OK -> _is_transport_uncertain() MUST stay True!
    act_latch = object.__new__(box_wrapper.BoxAction)
    act_latch.parse = box_wrapper.ParseData()
    act_latch.box_state = SimpleNamespace(
        get_Tnn_content=lambda tnn: {"addr": 1, "num": "A"},
        get_Tn_data=lambda *a, **kw: None,
        modify_Tn_data=lambda *a, **kw: None,
    )
    act_latch.box_save = SimpleNamespace(
        last_cmd=None, last_cmd_tmp=None, error=None, error_tnn={}, recode_err=lambda *a, **kw: None
    )
    act_latch.boxcfg = SimpleNamespace(version=1, safe_pos_y=345.0)
    act_latch.print_stats = SimpleNamespace(state="printing")
    act_latch.gcode = SimpleNamespace(run_script_from_command=lambda *_a, **_kw: None)
    act_latch.timeout_process = lambda _addr: None
    act_latch.disable_filament_sensor = lambda: None
    act_latch.filament_sensor_enable = True
    act_latch.get_filament_sensor_detect = lambda: False
    act_latch.extrude_tnn = "T1A"
    act_latch.extrude_process_ret_state = None
    act_latch.extrude_process_stage7_flag = False
    act_latch.extrude_process_stage7_ret = {"ret": None, "extrude_process_ret_state": None}
    act_latch._last_send_status = None
    act_latch._transport_uncertain_latched = False
    latch_stages: list[int] = []

    def _fake_cmd_send_latch(data_send, timeout, retries_en=True):
        cmd_b = data_send[3:4]
        if cmd_b == box_wrapper.Box_cmd["EXTRUDE_PROCESS"]:
            if retries_en:
                raise AssertionError("Non-idempotent stage poll sent with retries_en=True")
            st_byte = data_send[5] if len(data_send) > 5 else -1
            latch_stages.append(st_byte)
            status_code = 0x02 if st_byte == 6 else 0x00
            return bytes([0xF7, data_send[0], 3, status_code, cmd_b[0], 0x00])
        return bytes([0xF7, data_send[0], 3, 0x00, cmd_b[0], 0x00])

    act_latch._serial = SimpleNamespace(cmd_send_data_with_response=_fake_cmd_send_latch)
    act_latch.reactor = SimpleNamespace(
        monotonic=lambda: 0.0,
        pause=lambda _t: act_latch.extrude_process_stage7() if 6 not in latch_stages else None,
    )
    part_res = act_latch.box_extrude_material_part("T1A", [0, 0, 0, 0])
    if part_res is not False or not act_latch._is_transport_uncertain():
        raise AssertionError(
            f"stage=6 CRC_ERR failed to preserve uncertainty: part_res={part_res}, uncertain={act_latch._is_transport_uncertain()}"
        )
    # Even if a follow-up stage=5 poll returns 'OK', latched uncertainty remains True for caller emergency guard!
    poll_ok = act_latch.communication_extrude_process(1, "A", stage=5, report_err=False)
    if poll_ok is not True or not act_latch._is_transport_uncertain():
        raise AssertionError("Follow-up stage=5 OK cleared latched uncertainty after non-idempotent stage=6 CRC_ERR!")

    # K2Cancel.finish() mutex deadline check: must time out without blocking in __enter__.
    fake_clock = [0.0]

    class _FakeReactor:
        def monotonic(self):
            return fake_clock[0]

        def pause(self, waketime):
            fake_clock[0] = max(fake_clock[0] + 0.5, waketime)
            return fake_clock[0]

    class _BusyMutex:
        def test(self):
            return True

        def __enter__(self):
            raise AssertionError("K2Cancel.finish() entered held mutex past deadline")

        def __exit__(self, *args):
            return False

    cancel_obj = object.__new__(k2_cancel.K2Cancel)
    cancel_obj.reactor = _FakeReactor()
    cancel_obj.printer = SimpleNamespace(is_shutdown=lambda: False)
    cancel_obj.owner = None
    cancel_obj.requested = True
    cancel_obj.sd = SimpleNamespace(is_active=lambda: False, do_cancel=lambda: None)
    cancel_obj.gcode = SimpleNamespace(get_mutex=lambda: _BusyMutex())
    cancel_obj.finish(0.0)
    if cancel_obj.requested:
        raise AssertionError("K2Cancel.finish() left requested=True after mutex deadline")

    dummy_align = object.__new__(z_align.NativeZAlign)
    dummy_align.active = False
    dummy_align.force_stop_flag = False
    shutdowns = []
    dummy_align.printer = SimpleNamespace(invoke_shutdown=lambda msg: shutdowns.append(msg))
    dummy_align.messages = {"z_bottom_stop": "stopped"}
    dummy_align.stop()
    if dummy_align.force_stop_flag or shutdowns:
        raise AssertionError("Idle NativeZAlign.stop() mutated force_stop_flag or shut down")

    # Verify Serial_485_Wrapper sleeps on empty queue and wakes on add_send_data.
    timer_updates = []
    dummy_485 = object.__new__(serial_485_wrapper.Serial_485_Wrapper)
    dummy_485.send_list = []
    dummy_485.send_timer = object()
    dummy_485.reactor = SimpleNamespace(
        NEVER=1e18,
        NOW=0.0,
        monotonic=lambda: 10.0,
        update_timer=lambda timer, when: timer_updates.append((timer, when)),
    )
    dummy_485._serial = SimpleNamespace(is_connected=lambda: True, get_response=lambda *a: None)
    if dummy_485.send_queue_process(10.0) != dummy_485.reactor.NEVER:
        raise AssertionError("Empty Serial_485_Wrapper queue did not return reactor.NEVER")
    dummy_485.add_send_data(b"\x01", 0.5, "#addr_1", "#cmd_1", True)
    if not timer_updates or timer_updates[-1] != (dummy_485.send_timer, dummy_485.reactor.NOW):
        raise AssertionError(f"add_send_data did not wake send_timer: {timer_updates}")

    # Verify K2 load-cell idle sleep stops sensor when idle and wakes on external add_client.
    import extras.k2 as k2_mod  # noqa: E402
    fake_now = [0.0]
    sensor_cbs = []

    class _FakeBatchBulk:
        def __init__(self):
            self.is_started = False
            self.client_cbs = sensor_cbs

        def _start(self):
            self.is_started = True

    fake_batch = _FakeBatchBulk()
    fake_sensor = SimpleNamespace(
        batch_bulk=fake_batch,
        get_mcu=lambda: SimpleNamespace(is_fileoutput=lambda: False),
        add_client=lambda cb: (sensor_cbs.append(cb), setattr(fake_batch, "is_started", True)),
    )
    track_cb = lambda msg: True
    cell_cbs = [track_cb]
    fake_clients = SimpleNamespace(
        client_cbs=cell_cbs,
        add_client=lambda cb: cell_cbs.append(cb),
    )
    fake_cell = SimpleNamespace(
        sensor=fake_sensor,
        clients=fake_clients,
        _track_force=track_cb,
        _sensor_data_event=lambda msg: True,
    )
    fake_probe = SimpleNamespace(_load_cell=fake_cell)
    dummy_k2 = object.__new__(k2_mod.K2)
    dummy_k2.printer = SimpleNamespace(
        lookup_object=lambda name, default=None: fake_probe if name == "load_cell_probe" else default,
        get_reactor=lambda: SimpleNamespace(monotonic=lambda: fake_now[0]),
    )
    dummy_k2.prepare_load_cell_idle_sleep()
    fake_sensor.add_client(fake_cell._sensor_data_event)
    if not fake_cell._sensor_data_event({"data": []}):
        raise AssertionError("Load cell entered sleep before idle timeout")
    fake_now[0] = k2_mod.LOAD_CELL_IDLE_TIMEOUT + 1.0
    if fake_cell._sensor_data_event({"data": []}) is not False:
        raise AssertionError("Load cell did not enter sleep after idle timeout")
    sensor_cbs.clear()
    fake_batch.is_started = False
    ext_cb = lambda msg: True
    fake_cell.clients.add_client(ext_cb)
    if not fake_batch.is_started or fake_cell._sensor_data_event not in sensor_cbs:
        raise AssertionError("External load-cell client did not wake CS1237 sensor")

    # Verify Box.stats does not poll MEASURING_WHEEL while idle (non-printing).
    wheel_queries = []
    orig_qmw = box_wrapper._query_measuring_wheel
    orig_wf = box_wrapper._watch_feed
    try:
        box_wrapper._query_measuring_wheel = lambda r, act, addr: wheel_queries.append(addr)
        box_wrapper._watch_feed = lambda b, et, cmd, content: None
        dummy_box = object.__new__(box_wrapper.MultiColorMeterialBoxWrapper)
        dummy_box.reactor = SimpleNamespace()
        dummy_box.box_action = SimpleNamespace(
            box_save=SimpleNamespace(last_cmd="T1A"),
            Tn_action_flag=False,
            print_stats=SimpleNamespace(state="standby"),
        )
        dummy_box.box_state = SimpleNamespace(
            get_Tnn_content=lambda cmd: {"addr": 1},
            get_Tn_data=lambda addr, key: "connect" if key == "state" else -10.0,
        )
        dummy_box.stats(1.0)
        if wheel_queries:
            raise AssertionError(f"Box.stats polled MEASURING_WHEEL while standby: {wheel_queries}")
        dummy_box.box_action.print_stats.state = "printing"
        dummy_box.stats(2.0)
        if wheel_queries != [1]:
            raise AssertionError(f"Box.stats did not poll MEASURING_WHEEL while printing: {wheel_queries}")
    finally:
        box_wrapper._query_measuring_wheel = orig_qmw
        box_wrapper._watch_feed = orig_wf

    return {
        "crc8_hex": f"0x{crc8:02x}",
        "flushing_volume_black_to_white": vol,
        "motor_decode_bits": len(bits),
        "mesh_outlier_index": out[0][1],
        "migrated_pid_verified": migrated_pid_verified,
        "legacy_load_cell_migration_verified": legacy_load_cell_migration_verified,
        "rs485_non_idempotent_verified": True,
        "rs485_broadcast_log_quiet_verified": True,
        "rs485_event_driven_queue_verified": True,
        "load_cell_idle_sleep_verified": True,
        "box_idle_wheel_poll_suppressed": True,
        "cfs_lost_ack_no_upper_retry_verified": True,
        "cfs_gear_retry_retrude_timeout_blocked": True,
        "cfs_stage6_crc_latch_verified": True,
        "k2_cancel_mutex_deadline_verified": True,
        "z_align_idle_stop_verified": True,
        "sync_config": {"fresh": res_fresh, "upgrade": res_up, "idempotent": res_idem},
    }


def _run_inner_target() -> int:
    verify_dir = Path("/tmp/verify")
    out_report = verify_dir / "target_report.json"
    py_checks = _check_ast_and_import_test(verify_dir / "klippy", sys.executable)
    jinja_checks = _check_jinja_templates(verify_dir / "config")
    with tempfile.TemporaryDirectory(prefix="k2-inner-") as td:
        tmp_root = Path(td)
        config_info, screen_info = _check_printer_config_and_endpoints(
            verify_dir / "klippy", verify_dir / "config", verify_dir / "mcu", tmp_root
        )
        driver_info = _check_driver_functional(verify_dir / "config", tmp_root)
    result = {
        "python_version": platform.python_version(),
        "machine": platform.machine(),
        "python_checks": py_checks,
        "jinja_checks": jinja_checks,
        "config_checks": config_info,
        "screen_checks": screen_info,
        "driver_checks": driver_info,
    }
    out_report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0


_SERVICES_CHROOT_SCRIPT = r"""
import asyncio
import importlib
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "/usr/share/moonraker")

import moonraker.__version__ as mver
from moonraker import server as mserver
from moonraker.components import database as mdb
from moonraker.components import k2_openwrt
from moonraker.components.machine import Machine

if mserver.API_VERSION != (1, 5, 0):
    raise AssertionError(f"Unexpected Moonraker API_VERSION: {mserver.API_VERSION}")
if getattr(mver, "__version__", None) != "v0.10.0-k2":
    raise AssertionError(f"Unexpected Moonraker __version__: {getattr(mver, '__version__', None)}")

imported = {}
for name in (
    "tornado",
    "serial",
    "PIL",
    "streaming_form_data",
    "yaml",
    "distro",
    "inotify_simple",
    "libnacl",
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
    mod = importlib.import_module(name)
    mod_file = getattr(mod, "__file__", "") or ""
    if not mod_file.startswith("/usr/share/moonraker-env/lib/python3.9/site-packages"):
        raise AssertionError(f"Dependency {name} resolved outside moonraker-env site-packages: {mod_file}")
    top = importlib.import_module(name.split(".")[0])
    imported[name] = str(getattr(top, "__version__", getattr(top, "version", "ok")))

import lmdb
from PIL import Image
buf = io.BytesIO()
Image.new("RGB", (24, 24), (200, 100, 50)).save(buf, format="JPEG")
buf.seek(0)
with Image.open(buf) as im:
    im.load()
    if im.size != (24, 24):
        raise AssertionError(f"Pillow JPEG roundtrip size mismatch: {im.size}")

# Test OpenWrtProcdProvider inside chroot with isolated fake /etc/init.d & ubus
class _Err(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status

class _FakeEventLoop:
    def create_task(self, coro):
        return asyncio.get_event_loop().create_task(coro)

class _FakeServer:
    def __init__(self):
        self.error = _Err
        self.events = []
        self.klippy_connection = type(
            "KConn",
            (),
            {"remote_methods": {"shutdown_machine": None, "reboot_machine": None}, "unit_name": "klipper"},
        )()
        self.machine = None
        self.loop = _FakeEventLoop()

    def lookup_component(self, name):
        if name == "machine":
            return self.machine
        if name == "klippy_connection":
            return self.klippy_connection
        raise KeyError(name)

    def load_component(self, config, name):
        return object()

    def send_event(self, ev, payload=None):
        self.events.append((ev, payload))

    def get_event_loop(self):
        return self.loop

class _FakeConfig:
    def __init__(self, srv):
        self._srv = srv
    def get_server(self):
        return self._srv

    def get(self, name, default):
        return default

async def _test_provider():
    with tempfile.TemporaryDirectory(prefix="k2-procd-") as td:
        root = Path(td)
        init_dir = root / "init.d"
        init_dir.mkdir()
        ops_log = root / "ops.log"
        for svc in ("klipper", "klipper_mcu", "moonraker", "nginx", "go2rtc"):
            sc = init_dir / svc
            sc.write_text(f"#!/bin/sh\necho {svc}:$1 >> {ops_log}\nexit 0\n", encoding="utf-8")
            sc.chmod(0o755)
        (init_dir / "nonexec").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (init_dir / "sym").symlink_to(init_dir / "klipper")

        ubus_bin = root / "ubus"
        ubus_bin.write_text(
            '#!/bin/sh\n'
            'echo \'{"klipper":{"instances":{"i1":{"running":true}}},"klipper_mcu":{"instances":{"i1":{"running":true}}},"moonraker":{"instances":{"i1":{"running":true}}},"nginx":{"instances":{"i1":{"running":false}}},"go2rtc":{"instances":{"i1":{"running":true}}}}\'\n',
            encoding="utf-8",
        )
        ubus_bin.chmod(0o755)
        reboot_bin = root / "reboot"
        reboot_bin.write_text(f"#!/bin/sh\necho REBOOT >> {ops_log}\n", encoding="utf-8")
        reboot_bin.chmod(0o755)
        power_bin = root / "poweroff"
        power_bin.write_text(f"#!/bin/sh\necho POWEROFF >> {ops_log}\n", encoding="utf-8")
        power_bin.chmod(0o755)

        srv = _FakeServer()
        mach = type(
            "Mach",
            (),
            {"_allowed_services": ["klipper", "nginx", "nonexec", "sym", "../escape"], "system_info": {"provider": "none", "service_state": {}}, "sys_provider": None, "provider_type": "none"},
        )()
        srv.machine = mach
        prov = k2_openwrt.load_component(_FakeConfig(srv))
        prov._init_dir = init_dir
        prov._ubus_bin = str(ubus_bin)
        prov._reboot_bin = str(reboot_bin)
        prov._poweroff_bin = str(power_bin)
        if mach.provider_type != "openwrt_procd" or mach.system_info["provider"] != "openwrt_procd":
            raise AssertionError("OpenWrt provider was not attached to Machine")
        if srv.klippy_connection.remote_methods["shutdown_machine"] != prov.shutdown:
            raise AssertionError("shutdown_machine remote method not updated")
        if srv.klippy_connection.remote_methods["reboot_machine"] != prov.reboot:
            raise AssertionError("reboot_machine remote method not updated")

        await prov.initialize()
        if prov.available_services.get("klipper") != {"active_state": "active", "sub_state": "running"}:
            raise AssertionError(f"Unexpected klipper state: {prov.available_services}")
        if prov.available_services.get("nginx") != {"active_state": "inactive", "sub_state": "dead"}:
            raise AssertionError(f"Unexpected nginx state: {prov.available_services}")
        if prov.available_services.get("go2rtc") != {"active_state": "active", "sub_state": "running"}:
            raise AssertionError(f"Unexpected go2rtc state: {prov.available_services}")
        for forbidden_svc in ("nonexec", "sym", "../escape"):
            if forbidden_svc in prov.available_services:
                raise AssertionError(f"Unsafe/non-exec service accepted: {forbidden_svc}")

        info_m = await prov.extract_service_info("moonraker", 4321)
        if info_m.get("manager") != "openwrt_procd" or info_m.get("unit_name") != "moonraker":
            raise AssertionError(f"Unexpected service_info: {info_m}")

        await prov.do_service_action("restart", "klipper")
        if not any(ev == "machine:service_state_changed" and "klipper" in p for ev, p in srv.events):
            raise AssertionError(f"Missing service_state_changed event: {srv.events}")

        for bad_act, bad_svc in (("reload", "klipper"), ("restart", "../etc/passwd"), ("stop", "moonraker"), ("restart", "unknownsvc")):
            try:
                await prov.do_service_action(bad_act, bad_svc)
            except _Err:
                pass
            else:
                raise AssertionError(f"Expected rejection for ({bad_act}, {bad_svc})")

        spawned = []
        prov._spawn_detached = lambda cmd: spawned.append(cmd)
        await prov.do_service_action("restart", "moonraker")
        await prov.reboot()
        await prov.shutdown()
        if len(spawned) != 3 or "moonraker restart" not in spawned[0][2] or str(reboot_bin) not in spawned[1][2] or str(power_bin) not in spawned[2][2]:
            raise AssertionError(f"Unexpected detached commands: {spawned}")

        if "klipper:restart" not in ops_log.read_text(encoding="utf-8"):
            raise AssertionError("Missing klipper:restart execution log")

asyncio.run(_test_provider())

# Exercise the same database startup path used by Moonraker.
with tempfile.TemporaryDirectory(prefix="k2-lmdb-") as td:
    db_dir = Path(td)
    env = lmdb.open(str(db_dir), max_dbs=8, map_size=1048576, lock=False)
    expected = {"test": 1}
    with env.begin(write=True) as txn:
        sub = env.open_db(b"fluidd", txn=txn)
        txn.put(b"console", mdb.encode_record(expected), db=sub)
    env.close()
    mdb_before = (db_dir / "data.mdb").read_bytes()
    server = _FakeServer()
    server.add_log_rollover_item = lambda *args: None
    server.loop.asyncio_loop = asyncio.new_event_loop()
    lmdb_open = lmdb.open
    try:
        # QEMU user mode cannot provide the robust mutex used by stock LMDB.
        # This database fixture has a single owner for its entire lifetime.
        with patch.object(lmdb, "open", side_effect=lambda *args, **kwargs: lmdb_open(*args, **kwargs, lock=False)):
            for _ in range(2):
                provider = mdb.SqliteProvider(_FakeConfig(server), db_dir / mdb.SQL_DB_FILENAME)
                try:
                    got = provider.get_item(provider.sync_conn, "fluidd", "console")
                    if got != expected:
                        raise AssertionError(f"LMDB migration missed entry: {got}")
                finally:
                    provider.sync_conn.close()
    finally:
        server.loop.asyncio_loop.close()
    if (db_dir / "data.mdb").read_bytes() != mdb_before:
        raise AssertionError("LMDB migration mutated original data.mdb")

curl_ver = subprocess.check_output(["/usr/bin/curl", "--version"], text=True).splitlines()[0]
if "curl 8." not in curl_ver:
    raise AssertionError(f"Unexpected curl version: {curl_ver}")
go2rtc_ver = subprocess.check_output(["/usr/bin/go2rtc", "--version"], text=True).splitlines()[0].strip()
if "go2rtc version 1.9.15" not in go2rtc_ver:
    raise AssertionError(f"Unexpected go2rtc version: {go2rtc_ver}")
subprocess.run(["/bin/sh", "-n", "/etc/init.d/go2rtc"], check=True)
import configparser
import yaml
go2rtc_cfg = yaml.safe_load(Path("/etc/go2rtc/go2rtc.yaml").read_text(encoding="utf-8"))
if (
    not isinstance(go2rtc_cfg, dict)
    or go2rtc_cfg.get("api", {}).get("listen") != ":1984"
    or go2rtc_cfg.get("rtsp", {}).get("listen") != ":8554"
    or go2rtc_cfg.get("webrtc", {}).get("listen") != ":8555"
    or go2rtc_cfg.get("webrtc", {}).get("ice_servers") != []
):
    raise AssertionError(f"Unexpected go2rtc.yaml settings: {go2rtc_cfg}")
k2_streams = go2rtc_cfg.get("streams", {}).get("k2")
if k2_streams != ["exec:/usr/bin/k2_h264_source h264", "exec:/usr/bin/k2_h264_source mjpeg"]:
    raise AssertionError(f"Unexpected go2rtc streams.k2: {k2_streams}")
import ast, socket as _sock, threading as _thr
h264_src_code = Path("/usr/bin/k2_h264_source").read_text(encoding="utf-8")
ast.parse(h264_src_code, filename="/usr/bin/k2_h264_source")
h264_ns = {}
exec(compile(h264_src_code, "/usr/bin/k2_h264_source", "exec"), h264_ns)
with tempfile.TemporaryDirectory(prefix="k2-h264-") as _td:
    _uds = os.path.join(_td, "h264.sock")
    _srv = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
    _srv.bind(_uds)
    _srv.listen(1)
    def _feed():
        _c, _ = _srv.accept()
        _c.sendall(b"\x00\x00\x00\x01\x41junk_pframe\x00\x00\x00\x01\x67sps_body\x00\x00\x00\x01\x68pps")
        _c.close()
        _srv.close()
    _t = _thr.Thread(target=_feed)
    _t.start()
    _rfd, _wfd = os.pipe()
    try:
        h264_ns["stream_sps_aligned"](_wfd, _uds)
    finally:
        os.close(_wfd)
    _t.join(timeout=5.0)
    _got = os.read(_rfd, 4096)
    os.close(_rfd)
    if _got != b"\x00\x00\x00\x01\x67sps_body\x00\x00\x00\x01\x68pps":
        raise AssertionError(f"k2_h264_source SPS sync mismatch: {_got!r}")
mr_cfg = configparser.RawConfigParser(strict=False)
mr_cfg.read("/usr/share/moonraker/moonraker.conf", encoding="utf-8")
if (
    not mr_cfg.has_section("webcam k2_camera")
    or mr_cfg.get("webcam k2_camera", "service") != "webrtc-go2rtc"
    or mr_cfg.get("webcam k2_camera", "stream_url") != "/webcam/stream.html?src=k2"
    or mr_cfg.get("webcam k2_camera", "snapshot_url") != "/webcam/api/frame.jpeg?src=k2"
):
    raise AssertionError("Missing or invalid [webcam k2_camera] in moonraker.conf")
fluidd_rel = json.loads(Path("/usr/share/fluidd/release_info.json").read_text(encoding="utf-8"))
mainsail_rel = json.loads(Path("/usr/share/mainsail/release_info.json").read_text(encoding="utf-8"))
subprocess.run(["/usr/sbin/nginx", "-t", "-c", "/etc/nginx/nginx.conf", "-g", "daemon off;"], check=True)

result = {
    "api_version": mserver.API_VERSION_STRING if hasattr(mserver, "API_VERSION_STRING") else ".".join(map(str, mserver.API_VERSION)),
    "moonraker_version": mver.__version__,
    "tornado_version": imported["tornado"],
    "pillow_version": imported["PIL"],
    "curl_version": curl_ver,
    "go2rtc_version": go2rtc_ver,
    "go2rtc_config_ok": True,
    "go2rtc_init_ok": True,
    "k2_h264_source_ok": True,
    "fluidd_version": fluidd_rel["version"],
    "mainsail_version": mainsail_rel["version"],
    "nginx_config_ok": True,
    "openwrt_provider_verified": True,
    "lmdb_migration_verified": True,
    "pillow_jpeg_roundtrip_verified": True,
    "sqlite_version": sqlite3.sqlite_version,
}
Path("/tmp/verify/services_report.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
"""


def main() -> int:
    if "--inner" in sys.argv[1:]:
        return _run_inner_target()

    if sys.version_info[:2] != (3, 9) or struct.calcsize("P") != 4:
        raise RuntimeError(
            f"Expected 32-bit Python 3.9, got {platform.python_version()} ({platform.machine()})"
        )
    report: dict[str, object] = {
        "python_version": platform.python_version(),
        "machine": platform.machine(),
        "passed": False,
    }
    try:
        report["buster_python_checks"] = _check_ast_only(KLIPPY_DIR)
        chroot_root = Path(tempfile.mkdtemp(prefix="k2-chroot-"))
        try:
            subprocess.run(["tar", "-xf", str(STOCK_ROOTFS_TAR), "-C", str(chroot_root)], check=True)
            dev_dir = chroot_root / "dev"
            dev_dir.mkdir(parents=True, exist_ok=True)
            for name, minor in (("null", 3), ("zero", 5), ("full", 7), ("random", 8), ("urandom", 9)):
                node = dev_dir / name
                if node.is_symlink() or (node.exists() and not stat.S_ISCHR(node.stat().st_mode)):
                    node.unlink()
                if not node.exists():
                    os.mknod(node, stat.S_IFCHR | 0o666, os.makedev(1, minor))
                node.chmod(0o666)
            verify_dir = chroot_root / "tmp" / "verify"
            verify_dir.mkdir(parents=True, exist_ok=True)
            (chroot_root / "tmp").chmod(0o1777)
            shutil.copytree(KLIPPY_DIR, verify_dir / "klippy")
            shutil.copytree(K2_CONFIG_DIR, verify_dir / "config")
            shutil.copytree(MCU_DIR, verify_dir / "mcu")
            shutil.copy2(__file__, verify_dir / "self.py")

            # Stage updated Moonraker v0.10.0, site-packages, WebUIs, nginx.conf, and static curl into target chroot.
            for src_dir, rel_dst in (
                (SERVICES_DIR / "moonraker", "usr/share/moonraker"),
                (SERVICES_DIR / "moonraker-site-packages", "usr/share/moonraker-env/lib/python3.9/site-packages"),
                (SERVICES_DIR / "fluidd", "usr/share/fluidd"),
                (SERVICES_DIR / "mainsail", "usr/share/mainsail"),
            ):
                dst_dir = chroot_root / rel_dst
                if dst_dir.exists():
                    shutil.rmtree(dst_dir)
                shutil.copytree(src_dir, dst_dir)
            shutil.copy2(K2_SERVICES_SRC / "nginx.conf", chroot_root / "etc/nginx/nginx.conf")
            (chroot_root / "var/log/nginx").mkdir(parents=True, exist_ok=True)
            (chroot_root / "var/lib/nginx").mkdir(parents=True, exist_ok=True)
            (chroot_root / "var/run").mkdir(parents=True, exist_ok=True)
            curl_dst = chroot_root / "usr/bin/curl"
            shutil.copy2(SERVICES_DIR / "bin/curl", curl_dst)
            curl_dst.chmod(0o755)
            go2rtc_dst = chroot_root / "usr/bin/go2rtc"
            shutil.copy2(SERVICES_DIR / "bin/go2rtc", go2rtc_dst)
            go2rtc_dst.chmod(0o755)
            h264_src_dst = chroot_root / "usr/bin/k2_h264_source"
            shutil.copy2(K2_SERVICES_SRC / "k2_h264_source", h264_src_dst)
            h264_src_dst.chmod(0o755)
            (chroot_root / "etc/go2rtc").mkdir(parents=True, exist_ok=True)
            shutil.copy2(K2_SERVICES_SRC / "go2rtc.yaml", chroot_root / "etc/go2rtc/go2rtc.yaml")
            go2rtc_init_dst = chroot_root / "etc/init.d/go2rtc"
            shutil.copy2(K2_SERVICES_SRC / "init_go2rtc", go2rtc_init_dst)
            go2rtc_init_dst.chmod(0o755)

            subprocess.run(
                [
                    "chroot",
                    str(chroot_root),
                    "/usr/share/klippy-env/bin/python",
                    "-B",
                    "/tmp/verify/self.py",
                    "--inner",
                ],
                check=True,
            )
            (verify_dir / "check_services.py").write_text(_SERVICES_CHROOT_SCRIPT, encoding="utf-8")
            subprocess.run(
                [
                    "chroot",
                    str(chroot_root),
                    "/usr/share/moonraker-env/bin/python",
                    "-B",
                    "/tmp/verify/check_services.py",
                ],
                check=True,
            )
            inner = json.loads((verify_dir / "target_report.json").read_text(encoding="utf-8"))
            svc_checks = json.loads((verify_dir / "services_report.json").read_text(encoding="utf-8"))
            inner["service_checks"] = svc_checks
            report["target_runtime"] = inner
            report["config_checks"] = inner["config_checks"]
            report["screen_checks"] = inner["screen_checks"]
            report["driver_checks"] = inner["driver_checks"]
            report["jinja_checks"] = inner["jinja_checks"]
            report["service_checks"] = svc_checks
            report["passed"] = True
        finally:
            shutil.rmtree(chroot_root, ignore_errors=True)
    except Exception as exc:
        report["failure"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
        REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        raise
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
