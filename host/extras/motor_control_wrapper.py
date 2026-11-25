"""Recovered Motor_Control methods; lifecycle and remaining methods are incomplete."""

from ctypes import c_double
from numbers import Number
from typing import Tuple
MOTOR_NAME_TO_NUM = {'x': 1, 'y': 2, 'z': 3, 'z1': 4, 'e': 5}
motor_error_dict = {"0": ["key781", "ERR_NUM_ENCODER_MUTATION", "\u7f16\u7801\u5668\u8df3\u53d8"], "1": ["key782", "ERR_NUM_ENCODER_READ_ERR", "\u7f16\u7801\u5668\u8bfb\u53d6\u9519\u8bef"], "2": ["key783", "ERR_NUM_SOFTWARE_PEAK_CURRENTOVER_LIMIT", "\u8f6f\u4ef6\u89e6\u53d1\u5cf0\u503c\u8fc7\u6d41"], "3": ["key784", "ERR_NUM_SOFTWARE_CONTINUE_CURRENT_OVER_LIMIT", "\u8f6f\u4ef6\u89e6\u53d1\u6301\u7eed\u8fc7\u6d41"], "4": ["key785", "ERR_NUM_HARDWARE_CURRENT_OVER_LIMIT", "\u786c\u4ef6\u89e6\u53d1\u8fc7\u6d41"], "5": ["key786", "ERR_NUM_SPEED_FDB_CONTINUE_OVER_LIMIT", "\u901f\u5ea6\u6301\u7eed\u8d85\u9650"], "6": ["key787", "ERR_NUM_POS_FDB_OVER_LIMIT", "\u4f4d\u7f6e\u8d85\u9650"], "7": ["key788", "ERR_NUM_POS_CMD_MUTATION", "\u4f4d\u7f6e\u6307\u4ee4\u8df3\u53d8"], "8": ["key789", "ERR_NUM_EXCESSIVE_POS_TRACKING_ERROR", "\u4f4d\u7f6e\u8ddf\u8e2a\u8bef\u5dee\u8fc7\u5927"], "9": ["key790", "ERR_NUM_MOTOR_CHECK_ValueAbnormal", "\u7535\u963b\u7535\u611f\u503c\u5f02\u5e38"], "10": ["key791", "ERR_NUM_MOTOR_CHECK_UnStable", "\u9636\u8dc3\u54cd\u5e94\u8fd8\u6ca1\u7a33\u5b9a"], "11": ["key792", "ERR_NUM_MOTOR_CHECK_MotorPhaseNotConnected", "\u7535\u673a\u6ca1\u6709\u8fde\u63a5"], "12": ["key793", "ERR_NUM_MOTOR_CHECK_MotorPhaseResistanceDifferent", "\u4e24\u76f8\u7535\u963b\u504f\u5dee\u8fc7\u5927(\u7535\u673a\u7ebf\u5708\u77ed\u8def)"], "13": ["key794", "ERR_NUM_MOTOR_CHECK_OtherErr", "\u7535\u963b\u68c0\u67e5\u4e2d\u7684\u5176\u4ed6\u9519\u8bef"]}
motor_warning_dict = {"0": ["key795", "ERR_NUM_COMMUNICATION_ERR", "\u901a\u8baf\u9519\u8bef"], "1": ["key796", "ERR_NUM_MOTOR_POWER_SUPPLY_TOO_LOW", "\u7535\u6e90\u7535\u538b\u8fc7\u4f4e"], "2": ["key797", "ERR_NUM_MCU_OVER_HEATING", "MCU\u6e29\u5ea6\u8fc7\u9ad8"]}

import logging
import math
import struct
from types import MethodType
import mcu
from extras.box_wrapper import ParseData

Motor_Addr = {
    "0": b"\x80", "1": b"\x81", "2": b"\x82", "3": b"\x83",
    "4": b"\x84", "5": b"\x85", "6": b"\x86", "7": b"\x87",
    "8": b"\x88", "9": b"\xff",
}
Motor_cmd = {
    "reboot": b"\x01", "encoder_calibrate_official": b"\x03",
    "elec_offset_calibrate": b"\x04", "boot": b"\x0b",
    "read485_addr": b"\x0e", "set485_addr": b"\x0e",
    "control": b"\x05", "protection": b"\x0c", "get": b"\x08",
    "flash_param": b"\x07", "sys_param": b"\x06", "systemid": b"\x0d",
    "version": b"\x0f", "stall_mode": b"\x11",
}
TIMEOUT_MAX = 3600
TIMEOUT_LONG_TIME_MS = 2000
TIMEOUT_SHORT_TIME_MS = 50
MOTOR_NUM = [1, 2, 3, 4]


TIMEOUT_LONGER_TIME_MS = 60000

MOTOR_NUM_TO_IO_ADDR = {1: '1', 2: '2', 3: '3', 4: '4', 5: '5'}


MOTOR_NUM_TO_NAME = {1: 'x', 2: 'y', 3: 'z', 4: 'z1', 5: 'e'}


key_cut_pos_err = '!! {"code":"key855", "msg":"cut position error", "values": [%.2f, %.2f, %.2f]}'
key_cut_filament_err = '!! {"code":"key854", "msg":"the presence of filament when cutting detected", "values": []}'
key_cutter_not_exist = '!! {"code":"key866", "msg":"no cutter", "values": []}'

class Motor_Control:
    motor_reboot_help = "MOTOR_REBOOT NUM=1 \u56fa\u4ef6\u7a0b\u5e8f\u91cd\u65b0\u542f\u52a8"
    motor_encoder_calibrate_official_help = "MOTOR_ENCODER_CALIBRATE_OFFICIAL NUM=1 \u7f16\u7801\u5668\u78c1\u573a\u4e00\u952e\u6821\u51c6"
    motor_elec_offset_calibrate_help = "MOTOR_ELEC_OFFSET_CALIBRATE NUM=1 \u7535\u96f6\u4f4d\u4e00\u952e\u6821\u51c6"
    motor_boot_help = "MOTOR_BOOT NUM=1 \u4f7f\u7cfb\u7edf\u8fdb\u5165boot\u6a21\u5f0f"
    motor_read485_addr_help = "MOTOR_READ_ADDR NUM=1 \u8bfb\u53d6\u7535\u673a\u5730\u5740"
    motor_set485_addr_help = "MOTOR_SET_ADDR NUM=1 \u8bbe\u7f6e\u7535\u673a\u5730\u5740"
    motor_control_help = "MOTOR_CONTROL NUM=1 DATA=4 \u7535\u673a\u63a7\u5236\u4f7f\u80fd\u76f8\u5173\u6307\u4ee4"
    motor_get_help = "MOTOR_GET NUM=1 DATA=1 \u6253\u5370\u7cfb\u7edf\u6240\u652f\u6301\u53d8\u91cf"
    motor_flash_param_help = "MOTOR_FLASH_PARAM NUM=1 DATA=1 \u8bbe\u7f6e\u7cfb\u7edfflash\u53c2\u6570\u53d8\u91cf\u7684\u8bfb\u5199"
    motor_sys_param_help = "MOTOR_SYS_PARAM NUM=1 DATA=1 ID=14 PARAMS=1.4 PARAMS_TYPE=float \u8bbe\u7f6e\u64cd\u4f5c\u7cfb\u7edf\u53c2\u6570\u53d8\u91cf\u7684\u8bfb\u5199"
    motor_version_help = "MOTOR_VERSION NUM=1 DATA=0 \u67e5\u8be2\u56fa\u4ef6\u7248\u672c\u53f7"
    motor_auto_check_protection_help = "MOTOR_AUTO_CHECK_PROTECTION NUM=1 DATA=11 \u67e5\u8be2\u9519\u8bef\u7801\u548c\u8b66\u544a\u7801"
    motor_check_protection_after_home_help = "MOTOR_CHECK_PROTECTION_AFTER_HOME DATA=11 \u5f52\u4f4d\u540e\u68c0\u6d4b4\u4e2a\u7535\u673a\u9519\u8bef\u7801"
    motor_clear_err_warn_code_help = "MOTOR_CLEAR_ERR_WARN_CODE NUM=0 DATA=5 DATA\u662f\u6e05\u9664\u9519\u8bef\u7801\u548c\u8b66\u544a\u7801\u7684\u53c2\u6570"
    motort_stall_mode_help = "MOTOR_STALL_MODE DATA=2 \u6539\u53d8stall\u7ba1\u811a\u6a21\u5f0f 1\u9650\u4f4d\u5835\u8f6c\u4fe1\u53f7 2\u7d27\u6025\u4fdd\u62a4\u8f93\u51fa"
    motor_check_cut_pos_help = "MOTOR_CHECK_CUT_POS \u901a\u8fc7x\u7535\u673a tall\u5f15\u811a\u7535\u6d41\u5b9a\u4f4d\u5207\u5200\u4f4d\u7f6e"
    motor_read_all_pin_io_status_help = "MOTOR_READ_ALL_PIN_IO \u83b7\u53d64\u4e2a\u7535\u673adir\u548cstep\u5f15\u811aIO\u72b6\u6001"
    motor_read_pin_io_help = "MOTOR_READ_PIN_IO PIN=PB15 \u8bfb\u53d6\u5355\u4e2aPIN\u811aIO\u72b6\u6001"
    motor_elec_offset_calibrate_all_help = "MOTOR_ELEC_OFFSET_CALIBRATE_ALL  4\u4e2a\u7535\u673a\u7535\u96f6\u4f4d\u4e00\u952e\u6821\u51c6"
    cmd_MOTOR_EXTRUDER_RETRY_PROCESS_help = "cmd_MOTOR_EXTRUDER_RETRY_PROCESS_help  E\u8f74\u7535\u673a\u62a5\u9519\u81ea\u52a8\u91cd\u8bd5\u5904\u7406"
    def bytes_to_int(self, bytes_obj, is_data_obj=False):
        if is_data_obj:
            return [int.from_bytes(bytes_obj, byteorder="big")]
        return int.from_bytes(bytes_obj, byteorder="big")

    def get_status(self, eventime):
        result = {}
        result["motor_ready"] = self.is_ready
        result["is_homing"] = self.is_homing
        cut = {}
        cut["state"] = bool(self.box_action.cut.cut_present)
        cut["pos_x"] = self.box_action.boxcfg.cut_pos_x
        result["cut"] = cut
        return result

    def _build_config(self):
        self.nozzle_mcu.add_config_cmd("config_transparent oid=%d" % self.oid)

    def data_pack(self, addr, cmd, data):
        packet = self.printer.lookup_object("auto_addr").data_pack(addr, cmd, data)
        return (bytes([packet.head])
                + bytes([packet.slave_addr])
                + bytes([packet.length])
                + bytes([packet.status])
                + bytes([packet.function_code])
                + bytes(int(item) for item in packet.data)
                + bytes([packet.crc]))

    def result_to_value(self, result, id_params_type):
        if not result:
            return None
        value = None
        try:
            data = result[5:result[2] - 3 + 5]
            if data and len(data) == 1:
                data = [b"\x00" + data]
            if data and id_params_type == "float":
                value = struct.unpack("f", data)[0]
            elif data and id_params_type == "uint16_t":
                value = struct.unpack("<H", data)[0]
            elif data and id_params_type == "uint32_t":
                value = struct.unpack(">I", data)[0]
            elif data and id_params_type == "int16_t":
                value = struct.unpack("<h", data)[0]
            elif data and id_params_type == "int32_t":
                value = struct.unpack(">I", data)[0]
        except Exception as error:
            logging.error("result:%s err:%s" % (str(result), str(error)))
            logging.exception(error)
        return value

    @staticmethod
    def stats_query_string(name):
        def decorator(func):
            def wrapper(self, *args, **kwargs):
                result = func(self, *args, **kwargs)
                return f"{name}={getattr(self, '_stats_motor_get_value')!r} "
            return wrapper
        return decorator

    def data_pack_extruder(self, addr, cmd, data, id_params_type=""):
        original_value = None
        if id_params_type == "float":
            original_value = data[-1]
            data[-1] = int(data[-1])
        packet = self.printer.lookup_object("auto_addr").data_pack(addr, cmd, data)
        types_16 = ["uint16_t", "int16_t"]
        types_32 = ["uint32_t", "int32_t"]
        encoded = []
        if id_params_type in types_16 and len(bytes([data[-1]])) == 1:
            packet.length += 1
            encoded = packet.data[-1].to_bytes(2, byteorder="little")
        elif id_params_type in types_32 and len(bytes([data[-1]])) == 1:
            packet.length += 3
            encoded = packet.data[-1].to_bytes(4, byteorder="big")
        elif id_params_type in types_32 and len(bytes([data[-1]])) == 2:
            packet.length += 2
            encoded = packet.data[-1].to_bytes(4, byteorder="big")
        elif id_params_type in types_32 and len(bytes([data[-1]])) == 3:
            packet.length += 1
            encoded = packet.data[-1].to_bytes(4, byteorder="big")
        if id_params_type == "float":
            packet.length += 3
            encoded = struct.pack("f", float(original_value))
        if id_params_type == "float" and original_value is not None:
            packet.data[-1] = original_value
        data_send = (bytes([packet.head])
                     + bytes([packet.slave_addr])
                     + bytes([packet.length])
                     + bytes([packet.status])
                     + bytes([packet.function_code])
                     + bytes(int(item) for item in packet.data[0:-1])
                     + encoded
                     + bytes([packet.crc]))
        from extras import k2_native as chelper
        ffi_main, ffi_lib = chelper.get_ffi()
        crc = ffi_lib.msgblock_485_crc8(data_send[2:-1], len(data_send[2:-1]))
        logging.info("data_send[2:-1]:%s len:%s crc:%s bytes_crc:%s" %
                     (str(data_send[2:-1]), len(data_send[2:-1]), crc, bytes([crc])))
        return data_send[0:-1] + bytes([crc])

    def motor_reboot(self, gcmd):
        if not self.validate_num_params(gcmd):
            return
        if self.motor_extruder_reboot(gcmd):
            return
        num = gcmd.get_int("NUM", None, minval=1, maxval=10)
        if num:
            addr = Motor_Addr.get(str(num))
            cmd = Motor_cmd.get("reboot")
            data = b"\x00"
            send = self.box_action.motor_send_data
            try:
                send(addr, cmd, TIMEOUT_MAX, data=data, retries_en=False)
            finally:
                del send

    def motor_boot(self, gcmd):
        if not self.validate_num_params(gcmd):
            return
        if self.motor_extruder_boot(gcmd):
            return
        num = gcmd.get_int("NUM", None, minval=1, maxval=10)
        if num:
            addr = Motor_Addr.get(str(num))
            cmd = Motor_cmd.get("boot")
            data = b"\x00"
            send = self.box_action.motor_send_data
            try:
                send(addr, cmd, TIMEOUT_MAX, data=data, retries_en=False)
            finally:
                del send

    def motor_set485_addr(self, gcmd):
        if not self.validate_num_params(gcmd):
            return
        if self.motor_extruder_set485_addr(gcmd):
            return
        num = gcmd.get_int("NUM", None, minval=1, maxval=10)
        if num:
            addr = Motor_Addr.get(str(num))
            cmd = Motor_cmd.get("set485_addr")
            data = b"\x01"
            self.box_action.motor_send_data(addr, cmd, 0.05,
                                            data=data, retries_en=False)

    def transparent_send_gcmd(self, gcmd):
        data = gcmd.get("DATA", "")
        data_obj = bytes.fromhex(data)
        from extras.k2_transport import TransparentQuery
        lookup = lambda *args, **kwargs: TransparentQuery(self.nozzle_mcu, *args, **kwargs)
        try:
            command = lookup(
                "transparent_send oid=%c write=%*s timeout_ms=%u",
                "transparent_response oid=%c read=%*s",
                oid=self.oid, cq=None)
        finally:
            del lookup
        try:
            self.cmd_transparent_send = command
        finally:
            del command
        convert = self.bytes_to_int
        try:
            get_addr = Motor_Addr.get
            try:
                address_data = get_addr(str(9))
            finally:
                del get_addr
            try:
                addr = convert(address_data)
            finally:
                # Outer cleanup still owns the local when argument lookup fails.
                convert = None
                del address_data
        finally:
            del convert
        convert = self.bytes_to_int
        try:
            cmd = convert(Motor_cmd.get("set485_addr"))
        finally:
            del convert
        convert = self.bytes_to_int
        try:
            value = convert(b"\x01", is_data_obj=True)
        finally:
            del convert
        data_send = self.data_pack(addr, cmd, value)
        caught_error = None
        try:
            data_send = data_obj
            logging.info("data_send:%s", data_send)
            send = self.cmd_transparent_send.send
            try:
                result = send([self.oid, data_send, TIMEOUT_LONG_TIME_MS])
            finally:
                del send
            respond = self.gcode.respond_info
            try:
                response_values = (str(data_send), str(result))
                try:
                    message = "data_send:%s \u5e7f\u64ad\u8fd4\u56de:%s" % response_values
                finally:
                    del response_values
                respond(message)
            finally:
                del respond
        except Exception as error:
            caught_error = error
            logging.exception(error)
        respond = self.gcode.respond_info
        try:
            respond("%s \u53d1\u9001\u7ed3\u675f" % data)
        finally:
            del respond

    def motor_read485_addr(self, gcmd):
        if not self.validate_num_params(gcmd):
            return
        if gcmd.get("EXTRUDER", ""):
            return self.motor_extruder_read485_addr(gcmd)
        num = gcmd.get_int("NUM", None, minval=1, maxval=10)
        if num:
            get_addr = Motor_Addr.get
            addr = get_addr(str(num))
            cmd = Motor_cmd.get("read485_addr")
            data = b"\x02"
            result = self.box_action.motor_send_data(
                addr, cmd, 0.05, data=data, retries_en=False)
            if result:
                result_data = result[5:result[2] - 3 + 5]
                respond = self.gcode.respond_info
                respond("get motor addr:%s" % result_data)
                return result_data

    def motor_get(self, gcmd):
        if not self.validate_num_params(gcmd):
            return
        if not self.validate_data_params(gcmd):
            return
        if self.motor_extruder_get(gcmd):
            return
        num = gcmd.get_int("NUM", None, minval=1, maxval=10)
        data = gcmd.get_int("DATA", None, minval=1, maxval=12)
        if num:
            get_addr = Motor_Addr.get
            addr = get_addr(str(num))
            cmd = Motor_cmd.get("get")
            data_byte = self.parse.parse_num_to_byte(data)
            send = self.box_action.motor_send_data
            result = send(addr, cmd, TIMEOUT_MAX, data=data_byte, retries_en=False)
            if result:
                result_data = result[5:result[2] - 3 + 5]
                value = ""
                if (data == 1 or data == 2 or data == 3 or data == 4
                        or data == 5 or data == 6 or data == 7 or data == 8
                        or data == 9 or data == 10 or data == 17 or data == 18):
                    value = struct.unpack("<f", result_data)[0]
                elif (data == 11 or data == 12 or data == 13 or data == 14
                      or data == 15 or data == 16):
                    value = struct.unpack("<I", result_data)[0]
                respond = self.gcode.respond_info
                response_values = (data_byte, result_data, value)
                respond("\u7cfb\u7edf\u6240\u652f\u6301\u53d8\u91cf\u5f53\u524d\u503c%s: result:%s value:%s" % response_values)

    def motor_control(self, gcmd):
        if not self.validate_num_params(gcmd):
            return
        if not self.validate_data_params(gcmd):
            return
        if self.motor_extruder_control(gcmd):
            return
        num = gcmd.get_int('NUM', None, minval=1, maxval=10)
        data = gcmd.get_int('DATA', None, minval=1, maxval=12)
        if num:
            get_addr = Motor_Addr.get
            key = str(num)
            addr = get_addr(key)
            del key, get_addr
            cmd = Motor_cmd.get('control')
            data_byte = self.parse.parse_num_to_byte(data)
            result = self.box_action.motor_send_data(
                addr, cmd, 0.05, data=data_byte, retries_en=False)
            if result:
                result_data = result[5:result[2] - 3 + 5]
                if data == 4:
                    level = controller = pwm = '\u672a\u77e5'
                    if result_data[0] == 0:
                        level = '\u4f4e'
                    elif result_data[0] == 1:
                        level = '\u9ad8'
                    if result_data[1] == 0:
                        controller = '\u5173\u95ed'
                    elif result_data[1] == 1:
                        controller = '\u5f00\u542f'
                    if result_data[2] == 0:
                        pwm = '\u5173\u95ed'
                    elif result_data[2] == 1:
                        pwm = '\u5f00\u542f'
                    respond = self.gcode.respond_info
                    values = (result_data, level, controller, pwm)
                    message = '\u7535\u673a\u63a7\u5236\u4f7f\u80fd\u72b6\u6001:%s EN\u811a\u7535\u5e73\u5f53\u524d:%s, \u63a7\u5236\u5668\u5f53\u524d:%s, pwm\u8f93\u51fa\u5f53\u524d:%s' % values
                    del values
                    response = respond(message)
                    del message, respond, response

    def motort_stall_mode(self, gcmd):
        if not self.validate_data_params(gcmd):
            return
        data = gcmd.get_int('DATA', 2, minval=1, maxval=10)
        if data == 1:
            self.is_homing = True
        cmd = Motor_cmd.get('stall_mode')
        data_byte = self.parse.parse_num_to_byte(data)
        for num in MOTOR_NUM[:2]:
            get_addr = Motor_Addr.get
            key = str(num)
            addr = get_addr(key)
            del key, get_addr
            result = self.box_action.motor_send_data(
                addr, cmd, 0.05, data=data_byte, retries_en=False)
            if result:
                result_data = result[5:result[2] - 3 + 5]
                respond = self.gcode.respond_info
                values = (num, data_byte, result_data)
                message = 'stall mode change motor8%s: send:%s recv:%s' % values
                del values
                response = respond(message)
                del message, respond, response
        pause = self.reactor.pause
        monotonic = self.reactor.monotonic
        now = monotonic()
        del monotonic
        deadline = now + 0.05
        del now
        paused = pause(deadline)
        del deadline, pause, paused
        if data == 2:
            pause = self.reactor.pause
            monotonic = self.reactor.monotonic
            now = monotonic()
            del monotonic
            deadline = now + 0.05
            del now
            paused = pause(deadline)
            del deadline, pause, paused
            self.is_homing = False

    def validate_num_params(self, gcmd):
        allowed = [index for index in range(10)]
        value = None
        try:
            getter = gcmd.get_int
            try:
                value = getter("NUM", None, minval=0, maxval=10)
            finally:
                del getter
            if value is None or value not in allowed:
                respond = gcmd.respond_info
                try:
                    values = (value, str(allowed))
                    message = "NUM:%s must be in %s" % values
                    del values
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond
                return False
            return True
        except Exception as error:
            logging.exception(error)
            respond = gcmd.respond_info
            try:
                values = (value, str(allowed))
                message = "NUM:%s must be in %s" % values
                del values
                try:
                    respond(message)
                finally:
                    del message
            finally:
                del respond
            return False

    def validate_data_params(self, gcmd):
        allowed = [index for index in range(16)]
        try:
            getter = gcmd.get_int
            try:
                value = getter("DATA", None, minval=0, maxval=15)
            finally:
                del getter
            if value is None or value not in allowed:
                respond = gcmd.respond_info
                try:
                    values = str(allowed)
                    message = "DATA must be in %s" % values
                    del values
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond
                return False
            return True
        except Exception as error:
            logging.exception(error)
            respond = gcmd.respond_info
            try:
                values = str(allowed)
                message = "DATA must be in %s" % values
                del values
                try:
                    respond(message)
                finally:
                    del message
            finally:
                del respond
            return False

    def validate_id_params(self, gcmd):
        allowed = [index for index in range(512)]
        try:
            getter = gcmd.get_int
            try:
                value = getter("ID", None, minval=0, maxval=512)
            finally:
                del getter
            if value is None or value not in allowed:
                respond = gcmd.respond_info
                try:
                    values = str(allowed)
                    message = "ID must be in %s" % values
                    del values
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond
                return False
            return True
        except Exception as error:
            logging.exception(error)
            respond = gcmd.respond_info
            try:
                values = str(allowed)
                message = "ID must be in %s" % values
                del values
                try:
                    respond(message)
                finally:
                    del message
            finally:
                del respond
            return False

    def validate_param_type_params(self, gcmd):
        allowed = ["float", "int16_t", "int32_t", "uint16_t", "uint32_t"]
        try:
            getter = gcmd.get
            try:
                value = getter("PARAMS_TYPE", None)
            finally:
                del getter
            if value is None or value not in allowed:
                respond = gcmd.respond_info
                try:
                    values = str(allowed)
                    message = "PARAMS_TYPE must be in %s" % values
                    del values
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond
                return False
            return True
        except Exception as error:
            logging.exception(error)
            respond = gcmd.respond_info
            try:
                values = str(allowed)
                message = "PARAMS_TYPE must be in %s" % values
                del values
                try:
                    respond(message)
                finally:
                    del message
            finally:
                del respond
            return False

    def validate_param(self, gcmd):
        try:
            value = gcmd.get("PARAMS", None)
            if value is None:
                gcmd.respond_info("PARAMS is None")
                return False
            return True
        except Exception as error:
            logging.exception(error)
            gcmd.respond_info("PARAMS is None")
            return False

    def motor_extruder_boot(self, gcmd):
        if not gcmd.get("EXTRUDER", ""):
            return False
        num = gcmd.get_int("NUM", 0, minval=0, maxval=10)
        convert = self.bytes_to_int
        try:
            lookup = Motor_Addr.get
            try:
                key = str(num)
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                addr = convert(argument)
            finally:
                del argument
        finally:
            del convert
        convert = self.bytes_to_int
        try:
            lookup = Motor_cmd.get
            try:
                key = 'boot'
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                cmd = convert(argument)
            finally:
                del argument
        finally:
            del convert
        data = self.bytes_to_int(b"\x00", is_data_obj=True)
        data_send = self.data_pack(addr, cmd, data)
        try:
            logging.info("data_send:%s", data_send)
            send = self.cmd_transparent_send.send
            try:
                arguments = [self.oid, data_send, TIMEOUT_SHORT_TIME_MS]
                try:
                    params = send(arguments)
                finally:
                    del arguments
            finally:
                del send
            respond = self.gcode.respond_info
            try:
                arguments = (str(data_send), str(params))
                try:
                    message = 'data_send:%s result:%s' % arguments
                finally:
                    del arguments
                try:
                    respond(message)
                finally:
                    del message
            finally:
                del respond
            if params.get("read"):
                result = params.get("read")[:-1]
                motor_status = result[5:result[2] - 3 + 5]
                respond = self.gcode.respond_info
                try:
                    arguments = motor_status
                    try:
                        message = 'get recv:%s' % arguments
                    finally:
                        del arguments
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond
        except Exception as err:
            logging.exception(err)
        return True

    def motor_extruder_reboot(self, gcmd):
        if not gcmd.get("EXTRUDER", ""):
            return False
        num = gcmd.get_int("NUM", 0, minval=0, maxval=10)
        convert = self.bytes_to_int
        try:
            lookup = Motor_Addr.get
            try:
                key = str(num)
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                addr = convert(argument)
            finally:
                del argument
        finally:
            del convert
        convert = self.bytes_to_int
        try:
            lookup = Motor_cmd.get
            try:
                key = 'reboot'
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                cmd = convert(argument)
            finally:
                del argument
        finally:
            del convert
        data = self.bytes_to_int(b"\x00", is_data_obj=True)
        data_send = self.data_pack(addr, cmd, data)
        try:
            logging.info("data_send:%s", data_send)
            send = self.cmd_transparent_send.send
            try:
                arguments = [self.oid, data_send, TIMEOUT_SHORT_TIME_MS]
                try:
                    params = send(arguments)
                finally:
                    del arguments
            finally:
                del send
            respond = self.gcode.respond_info
            try:
                arguments = (str(data_send), str(params))
                try:
                    message = 'data_send:%s result:%s' % arguments
                finally:
                    del arguments
                try:
                    respond(message)
                finally:
                    del message
            finally:
                del respond
            if params.get("read"):
                result = params.get("read")[:-1]
                motor_status = result[5:result[2] - 3 + 5]
                respond = self.gcode.respond_info
                try:
                    arguments = motor_status
                    try:
                        message = 'get recv:%s' % arguments
                    finally:
                        del arguments
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond
        except Exception as err:
            logging.exception(err)
        return True

    def motor_extruder_set485_addr(self, gcmd):
        if not gcmd.get("EXTRUDER", ""):
            return False
        num = gcmd.get_int("NUM", 0, minval=0, maxval=10)
        convert = self.bytes_to_int
        try:
            lookup = Motor_Addr.get
            try:
                key = str(num)
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                addr = convert(argument)
            finally:
                del argument
        finally:
            del convert
        convert = self.bytes_to_int
        try:
            lookup = Motor_cmd.get
            try:
                key = 'set485_addr'
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                cmd = convert(argument)
            finally:
                del argument
        finally:
            del convert
        data = self.bytes_to_int(b"\x01", is_data_obj=True)
        data_send = self.data_pack(addr, cmd, data)
        try:
            logging.info("data_send:%s", data_send)
            send = self.cmd_transparent_send.send
            try:
                arguments = [self.oid, data_send, TIMEOUT_LONG_TIME_MS]
                try:
                    params = send(arguments)
                finally:
                    del arguments
            finally:
                del send
            respond = self.gcode.respond_info
            try:
                arguments = (str(data_send), str(params))
                try:
                    message = 'data_send:%s result:%s' % arguments
                finally:
                    del arguments
                try:
                    respond(message)
                finally:
                    del message
            finally:
                del respond
        except Exception as err:
            logging.exception(err)
        return True

    def motor_extruder_read485_addr(self, gcmd):
        num = gcmd.get_int("NUM", 0, minval=0, maxval=10)
        convert = self.bytes_to_int
        try:
            lookup = Motor_Addr.get
            try:
                key = str(num)
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                addr = convert(argument)
            finally:
                del argument
        finally:
            del convert
        convert = self.bytes_to_int
        try:
            lookup = Motor_cmd.get
            try:
                key = 'read485_addr'
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                cmd = convert(argument)
            finally:
                del argument
        finally:
            del convert
        data = self.bytes_to_int(b"\x02", is_data_obj=True)
        data_send = self.data_pack(addr, cmd, data)
        try:
            logging.info("data_send:%s", data_send)
            send = self.cmd_transparent_send.send
            try:
                arguments = [self.oid, data_send, TIMEOUT_LONG_TIME_MS]
                try:
                    params = send(arguments)
                finally:
                    del arguments
            finally:
                del send
            respond = self.gcode.respond_info
            try:
                arguments = (str(data_send), str(params))
                try:
                    message = 'data_send:%s result:%s' % arguments
                finally:
                    del arguments
                try:
                    respond(message)
                finally:
                    del message
            finally:
                del respond
            if params.get("read"):
                result = params.get("read")[:-1]
                motor_status = result[5:result[2] - 3 + 5]
                respond = self.gcode.respond_info
                try:
                    arguments = motor_status
                    try:
                        message = 'get motor extruder addr:%s' % arguments
                    finally:
                        del arguments
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond
                return motor_status
        except Exception as err:
            logging.exception(err)
        return False

    def motor_extruder_control(self, gcmd):
        if not gcmd.get("EXTRUDER", ""):
            return False
        num = gcmd.get_int("NUM", 0, minval=0, maxval=10)
        value = gcmd.get_int("DATA", None, minval=1, maxval=12)
        data_byte = self.parse.parse_num_to_byte(value)
        convert = self.bytes_to_int
        try:
            lookup = Motor_Addr.get
            try:
                key = str(num)
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                addr = convert(argument)
            finally:
                del argument
        finally:
            del convert
        convert = self.bytes_to_int
        try:
            lookup = Motor_cmd.get
            try:
                key = 'control'
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                cmd = convert(argument)
            finally:
                del argument
        finally:
            del convert
        data = self.bytes_to_int(data_byte, is_data_obj=True)
        data_send = self.data_pack(addr, cmd, data)
        try:
            logging.info("data_send:%s", data_send)
            send = self.cmd_transparent_send.send
            try:
                arguments = [self.oid, data_send, TIMEOUT_SHORT_TIME_MS]
                try:
                    params = send(arguments)
                finally:
                    del arguments
            finally:
                del send
            respond = self.gcode.respond_info
            try:
                arguments = (str(data_send), str(params))
                try:
                    message = 'data_send:%s result:%s' % arguments
                finally:
                    del arguments
                try:
                    respond(message)
                finally:
                    del message
            finally:
                del respond
            if params.get("read"):
                result = params.get("read")[:-1]
                motor_status = result[5:result[2] - 3 + 5]
                if value == 4:
                    en_pin_state = "\u672a\u77e5"
                    control_switch_state = "\u672a\u77e5"
                    pwm_state = "\u672a\u77e5"
                    if motor_status[0] == 0:
                        en_pin_state = "\u4f4e"
                    elif motor_status[0] == 1:
                        en_pin_state = "\u9ad8"
                    if motor_status[1] == 0:
                        control_switch_state = "\u5173\u95ed"
                    elif motor_status[1] == 1:
                        control_switch_state = "\u5f00\u542f"
                    if motor_status[2] == 0:
                        pwm_state = "\u5173\u95ed"
                    elif motor_status[2] == 1:
                        pwm_state = "\u5f00\u542f"
                    respond = self.gcode.respond_info
                    try:
                        arguments = (motor_status, en_pin_state, control_switch_state, pwm_state)
                        try:
                            message = '\u7535\u673a\u63a7\u5236\u4f7f\u80fd\u72b6\u6001:%s EN\u811a\u7535\u5e73\u5f53\u524d:%s, \u63a7\u5236\u5668\u5f53\u524d:%s, pwm\u8f93\u51fa\u5f53\u524d:%s' % arguments
                        finally:
                            del arguments
                        try:
                            respond(message)
                        finally:
                            del message
                    finally:
                        del respond
        except Exception as err:
            logging.exception(err)
        return True

    def motor_extruder_get(self, gcmd):
        if not gcmd.get("EXTRUDER", ""):
            return False
        num = gcmd.get_int("NUM", 0, minval=0, maxval=10)
        value = gcmd.get_int("DATA", None, minval=1, maxval=12)
        data_byte = self.parse.parse_num_to_byte(value)
        convert = self.bytes_to_int
        try:
            lookup = Motor_Addr.get
            try:
                key = str(num)
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                addr = convert(argument)
            finally:
                del argument
        finally:
            del convert
        convert = self.bytes_to_int
        try:
            lookup = Motor_cmd.get
            try:
                key = 'get'
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                cmd = convert(argument)
            finally:
                del argument
        finally:
            del convert
        data = self.bytes_to_int(data_byte, is_data_obj=True)
        data_send = self.data_pack(addr, cmd, data)
        try:
            send = self.cmd_transparent_send.send
            try:
                arguments = [self.oid, data_send, TIMEOUT_SHORT_TIME_MS]
                try:
                    params = send(arguments)
                finally:
                    del arguments
            finally:
                del send
            if params.get("read"):
                result = params.get("read")[:-1]
                motor_status = result[5:result[2] - 3 + 5]
                data = value
                value = ""
                if data == 1 or data == 2 or data == 3 or data == 4 or data == 5 or data == 6 or data == 7 or data == 8 or data == 9 or data == 10 or data == 17 or data == 18:
                    self._stats_motor_get_value = value = struct.unpack("<f", motor_status)[0]
                elif data == 11 or data == 12 or data == 13 or data == 14 or data == 15 or data == 16:
                    self._stats_motor_get_value = value = struct.unpack("<I", motor_status)[0]
        except Exception as err:
            logging.exception(err)
        return True

    def motor_pin_init(self):
        self.motor_x_dir = None
        self.motor_x_step = None
        self.motor_x_stall = None
        self.motor_y_dir = None
        self.motor_y_step = None
        self.motor_y_stall = None
        self.motor_z_dir = None
        self.motor_z_step = None
        self.motor_z_stall = None
        self.motor_z1_dir = None
        self.motor_z1_step = None
        self.motor_z1_stall = None
        self.motor_e_stall = None

    def motor_x_init(self, config):
        lookup = self.printer.lookup_object
        try:
            ppins = lookup("pins")
        finally:
            del lookup
        self.motor_x_stall = config.get("motor_x_stall")
        self.motor_x_dir_info = config.getlist("motor_x_dir")
        self.motor_x_step_info = config.getlist("motor_x_step")
        setup = ppins.setup_pin
        try:
            pin = self.motor_x_dir_info[0]
            try:
                channel = setup("digital_out", pin)
            finally:
                del pin
        finally:
            del setup
        try:
            self.motor_x_dir = channel
        finally:
            del channel
        setup = ppins.setup_pin
        try:
            pin = self.motor_x_step_info[0]
            try:
                channel = setup("digital_out", pin)
            finally:
                del pin
        finally:
            del setup
        try:
            self.motor_x_step = channel
        finally:
            del channel
        duration = self.motor_x_dir.setup_max_duration
        try:
            ignored = duration(0.0)
        finally:
            del duration
        del ignored
        duration = self.motor_x_step.setup_max_duration
        try:
            ignored = duration(0.0)
        finally:
            del duration
        del ignored

    def motor_y_init(self, config):
        lookup = self.printer.lookup_object
        try:
            ppins = lookup("pins")
        finally:
            del lookup
        self.motor_y_stall = config.get("motor_y_stall")
        self.motor_y_dir_info = config.getlist("motor_y_dir")
        self.motor_y_step_info = config.getlist("motor_y_step")
        setup = ppins.setup_pin
        try:
            pin = self.motor_y_dir_info[0]
            try:
                channel = setup("digital_out", pin)
            finally:
                del pin
        finally:
            del setup
        try:
            self.motor_y_dir = channel
        finally:
            del channel
        setup = ppins.setup_pin
        try:
            pin = self.motor_y_step_info[0]
            try:
                channel = setup("digital_out", pin)
            finally:
                del pin
        finally:
            del setup
        try:
            self.motor_y_step = channel
        finally:
            del channel
        duration = self.motor_y_dir.setup_max_duration
        try:
            ignored = duration(0.0)
        finally:
            del duration
        del ignored
        duration = self.motor_y_step.setup_max_duration
        try:
            ignored = duration(0.0)
        finally:
            del duration
        del ignored

    def motor_z_init(self, config):
        lookup = self.printer.lookup_object
        try:
            ppins = lookup("pins")
        finally:
            del lookup
        self.motor_z_stall = config.get("motor_z_stall")
        self.motor_z_dir_info = config.getlist("motor_z_dir")
        self.motor_z_step_info = config.getlist("motor_z_step")
        setup = ppins.setup_pin
        try:
            pin = self.motor_z_dir_info[0]
            try:
                channel = setup("digital_out", pin)
            finally:
                del pin
        finally:
            del setup
        try:
            self.motor_z_dir = channel
        finally:
            del channel
        setup = ppins.setup_pin
        try:
            pin = self.motor_z_step_info[0]
            try:
                channel = setup("digital_out", pin)
            finally:
                del pin
        finally:
            del setup
        try:
            self.motor_z_step = channel
        finally:
            del channel
        duration = self.motor_z_dir.setup_max_duration
        try:
            ignored = duration(0.0)
        finally:
            del duration
        del ignored
        duration = self.motor_z_step.setup_max_duration
        try:
            ignored = duration(0.0)
        finally:
            del duration
        del ignored

    def motor_z1_init(self, config):
        lookup = self.printer.lookup_object
        try:
            ppins = lookup("pins")
        finally:
            del lookup
        self.motor_z1_stall = config.get("motor_z1_stall")
        self.motor_z1_dir_info = config.getlist("motor_z1_dir")
        self.motor_z1_step_info = config.getlist("motor_z1_step")
        setup = ppins.setup_pin
        try:
            pin = self.motor_z1_dir_info[0]
            try:
                channel = setup("digital_out", pin)
            finally:
                del pin
        finally:
            del setup
        try:
            self.motor_z1_dir = channel
        finally:
            del channel
        setup = ppins.setup_pin
        try:
            pin = self.motor_z1_step_info[0]
            try:
                channel = setup("digital_out", pin)
            finally:
                del pin
        finally:
            del setup
        try:
            self.motor_z1_step = channel
        finally:
            del channel
        duration = self.motor_z1_dir.setup_max_duration
        try:
            ignored = duration(0.0)
        finally:
            del duration
        del ignored
        duration = self.motor_z1_step.setup_max_duration
        try:
            ignored = duration(0.0)
        finally:
            del duration
        del ignored

    def motor_e_init(self, config):
        self.motor_e_stall = config.get("motor_e_stall")

    def motor_extruder_elec_offset_calibrate(self, gcmd):
        if not gcmd.get("EXTRUDER", ""):
            return False
        num = gcmd.get_int("NUM", 0, minval=0, maxval=10)
        convert = self.bytes_to_int
        try:
            lookup = Motor_Addr.get
            try:
                key = str(num)
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                addr = convert(argument)
            finally:
                del argument
        finally:
            del convert
        convert = self.bytes_to_int
        try:
            lookup = Motor_cmd.get
            try:
                key = 'elec_offset_calibrate'
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                cmd = convert(argument)
            finally:
                del argument
        finally:
            del convert
        data = self.bytes_to_int(b"\x00", is_data_obj=True)
        data_send = self.data_pack(addr, cmd, data)
        try:
            logging.info("data_send:%s", data_send)
            send = self.cmd_transparent_send.send
            try:
                arguments = [self.oid, data_send, TIMEOUT_LONGER_TIME_MS]
                try:
                    params = send(arguments)
                finally:
                    del arguments
            finally:
                del send
            respond = self.gcode.respond_info
            try:
                arguments = (str(data_send), str(params))
                try:
                    message = 'data_send:%s result:%s' % arguments
                finally:
                    del arguments
                try:
                    respond(message)
                finally:
                    del message
            finally:
                del respond
            status = ""
            if params.get("read"):
                result = params.get("read")[:-1]
                state = ""
                status = result[5:result[2] - 3 + 5]
                first_byte = status[0]
                if first_byte == 0:
                    state = "\u6821\u51c6\u5931\u8d25"
                elif first_byte == 1:
                    state = "\u6821\u51c6\u6210\u529f"
                else:
                    state = "\u672a\u77e5\u7684\u72b6\u6001\u8fd4\u56de\u503c"
                bytes_2_5 = status[1:5]
                deg = struct.unpack("<f", bytes_2_5)[0]
                bytes_6_9 = status[5:9]
                pulse = struct.unpack("<f", bytes_6_9)[0]
                respond = self.gcode.respond_info
                try:
                    arguments = (state, deg, pulse)
                    try:
                        message = "\u7535\u96f6\u4f4d\u4e00\u952e\u6821\u51c6 status:%s, \u6821\u51c6\u7ed3\u679c\u7684\u6700\u5927\u504f\u5dee:%s\u00b0, \u6821\u51c6\u7ed3\u679c\u5747\u503c:%spulse" % arguments
                    finally:
                        del arguments
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond
        except Exception as err:
            logging.exception(err)
        return True

    def motor_extruder_version(self, gcmd):
        if not gcmd.get("EXTRUDER", ""):
            return False
        num = gcmd.get_int("NUM", 0, minval=0, maxval=10)
        convert = self.bytes_to_int
        try:
            lookup = Motor_Addr.get
            try:
                key = str(num)
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                addr = convert(argument)
            finally:
                del argument
        finally:
            del convert
        convert = self.bytes_to_int
        try:
            lookup = Motor_cmd.get
            try:
                key = 'version'
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                cmd = convert(argument)
            finally:
                del argument
        finally:
            del convert
        data = self.bytes_to_int(b"\x00", is_data_obj=True)
        data_send = self.data_pack(addr, cmd, data)
        try:
            logging.info("data_send:%s", data_send)
            send = self.cmd_transparent_send.send
            try:
                arguments = [self.oid, data_send, TIMEOUT_SHORT_TIME_MS]
                try:
                    params = send(arguments)
                finally:
                    del arguments
            finally:
                del send
            respond = self.gcode.respond_info
            try:
                arguments = (str(data_send), str(params))
                try:
                    message = 'data_send:%s result:%s' % arguments
                finally:
                    del arguments
                try:
                    respond(message)
                finally:
                    del message
            finally:
                del respond
            info = 0
            if params.get("read"):
                ret = {"fireware_ver": "", "cmd_system_ver": "", "protection_ver": "", "flash_param_ver": "", "logger_ver": ""}
                result = params.get("read")[:-1]
                motor_status = result[5:result[2] - 3 + 5]
                arguments = (motor_status[0], motor_status[1], motor_status[2], motor_status[3])
                try:
                    ret["fireware_ver"] = "%s.%s.%s.%s" % arguments
                finally:
                    del arguments
                arguments = (motor_status[4], motor_status[5])
                try:
                    ret["cmd_system_ver"] = "%s.%s" % arguments
                finally:
                    del arguments
                arguments = (motor_status[6], motor_status[7])
                try:
                    ret["protection_ver"] = "%s.%s" % arguments
                finally:
                    del arguments
                arguments = (motor_status[8], motor_status[9])
                try:
                    ret["flash_param_ver"] = "%s.%s" % arguments
                finally:
                    del arguments
                arguments = (motor_status[10], motor_status[11])
                try:
                    ret["logger_ver"] = "%s.%s" % arguments
                finally:
                    del arguments
                respond = self.gcode.respond_info
                try:
                    arguments = str(ret)
                    try:
                        message = "\u83b7\u53d6E\u8f74\u7535\u673a\u7248\u672c\u53f7\u6570\u636e: recv:%s" % arguments
                    finally:
                        del arguments
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond
        except Exception as err:
            logging.exception(err)
        return True

    def motor_extruder_encoder_calibrate_official(self, gcmd):
        if not gcmd.get("EXTRUDER", ""):
            return False
        num = gcmd.get_int("NUM", 0, minval=0, maxval=10)
        convert = self.bytes_to_int
        try:
            lookup = Motor_Addr.get
            try:
                key = str(num)
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                addr = convert(argument)
            finally:
                del argument
        finally:
            del convert
        convert = self.bytes_to_int
        try:
            lookup = Motor_cmd.get
            try:
                key = 'encoder_calibrate_official'
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                cmd = convert(argument)
            finally:
                del argument
        finally:
            del convert
        data = self.bytes_to_int(b"\x00", is_data_obj=True)
        data_send = self.data_pack(addr, cmd, data)
        try:
            logging.info("data_send:%s", data_send)
            send = self.cmd_transparent_send.send
            try:
                arguments = [self.oid, data_send, TIMEOUT_LONGER_TIME_MS]
                try:
                    params = send(arguments)
                finally:
                    del arguments
            finally:
                del send
            respond = self.gcode.respond_info
            try:
                arguments = (str(data_send), str(params))
                try:
                    message = 'data_send:%s result:%s' % arguments
                finally:
                    del arguments
                try:
                    respond(message)
                finally:
                    del message
            finally:
                del respond
            if params.get("read"):
                result = params.get("read")[:-1]
                state = ""
                status = result[5:result[2] - 3 + 5]
                first_byte = status[0]
                if first_byte == 12:
                    state = "\u6821\u51c6\u6210\u529f"
                elif first_byte == 0:
                    state = "\u6ca1\u6709\u8fd0\u884c\u6821\u51c6"
                elif first_byte == 4:
                    state = "\u6b63\u5728\u8fdb\u884c\u81ea\u6821\u51c6"
                elif first_byte == 8:
                    state = "\u8fd0\u884c\u6821\u51c6\u5931\u8d25"
                else:
                    state = "\u672a\u77e5\u7684\u72b6\u6001\u8fd4\u56de\u503c"
                respond = self.gcode.respond_info
                try:
                    arguments = state
                    try:
                        message = "\u7f16\u7801\u5668\u78c1\u573a\u4e00\u952e\u6821\u51c6 status:%s" % arguments
                    finally:
                        del arguments
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond
        except Exception as err:
            logging.exception(err)
        return True

    def motor_extruder_protection(self, gcmd):
        if not gcmd.get("EXTRUDER", ""):
            return False
        num = gcmd.get_int("NUM", 0, minval=0, maxval=10)
        value = gcmd.get_int("DATA", None, minval=1, maxval=12)
        data_byte = self.parse.parse_num_to_byte(value)
        convert = self.bytes_to_int
        try:
            lookup = Motor_Addr.get
            try:
                key = str(num)
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                addr = convert(argument)
            finally:
                del argument
        finally:
            del convert
        convert = self.bytes_to_int
        try:
            lookup = Motor_cmd.get
            try:
                key = 'protection'
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                cmd = convert(argument)
            finally:
                del argument
        finally:
            del convert
        data = self.bytes_to_int(data_byte, is_data_obj=True)
        data_send = self.data_pack(addr, cmd, data)
        try:
            logging.info("data_send:%s", data_send)
            send = self.cmd_transparent_send.send
            try:
                arguments = [self.oid, data_send, TIMEOUT_SHORT_TIME_MS]
                try:
                    params = send(arguments)
                finally:
                    del arguments
            finally:
                del send
            respond = self.gcode.respond_info
            try:
                arguments = (str(data_send), str(params))
                try:
                    message = 'data_send:%s result:%s' % arguments
                finally:
                    del arguments
                try:
                    respond(message)
                finally:
                    del message
            finally:
                del respond
            if params.get("read"):
                result = params.get("read")[:-1]
                motor_status = result[5:result[2] - 3 + 5]
                respond = self.gcode.respond_info
                try:
                    arguments = motor_status
                    try:
                        message = 'get recv:%s' % arguments
                    finally:
                        del arguments
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond
        except Exception as err:
            logging.exception(err)
        return True

    def force_stop(self):
        monotonic = self.reactor.monotonic
        try:
            eventtime = monotonic()
        finally:
            del monotonic
        get_mcu = self.enable_pin_pa9.get_mcu
        try:
            mcu = get_mcu()
        finally:
            del get_mcu
        estimated_print_time = mcu.estimated_print_time
        del mcu
        try:
            print_time = estimated_print_time(eventtime)
        finally:
            del estimated_print_time
        set_digital = self.enable_pin_pa9.set_digital
        try:
            set_digital(print_time + 0.1, 0)
        finally:
            del set_digital
        respond_info = self.gcode.respond_info
        try:
            respond_info('force stop,set !PA9 value=0')
        finally:
            del respond_info

    def web_force_stop(self, web_request):
        monotonic = self.reactor.monotonic
        try:
            eventtime = monotonic()
        finally:
            del monotonic
        get_mcu = self.enable_pin_pa9.get_mcu
        try:
            mcu = get_mcu()
        finally:
            del get_mcu
        estimated_print_time = mcu.estimated_print_time
        del mcu
        try:
            print_time = estimated_print_time(eventtime)
        finally:
            del estimated_print_time
        set_digital = self.enable_pin_pa9.set_digital
        try:
            set_digital(print_time + 0.1, 0)
        finally:
            del set_digital
        respond_info = self.gcode.respond_info
        try:
            respond_info('force stop,set !PA9 value=0')
        finally:
            del respond_info
        send = web_request.send
        try:
            send({'result': 'success'})
        finally:
            del send
    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.motor_closed_loop = config.getlist('motor_closed_loop')
        self.motor_params_table = {}
        self.motor_pin_init()
        self.motor_init(config)
        self.overcurrent_switch = config.getint('overcurrent_switch', 0)
        self.switch = config.getint('switch')
        self.retries = config.getint('retries', 4)
        self.cut_pos_offset = config.getfloat('cut_pos_offset', 0.4)
        self.gcode = self.printer.lookup_object('gcode')
        self.box_action = self.printer.lookup_object('box').box_action
        self.parse = ParseData()
        self.motor_warning_code = {'1': [], '2': [], '3': [], '4': [], '5': []}
        self.motor_error_code = {'1': [], '2': [], '3': [], '4': [], '5': []}
        self.is_ready = False
        self.is_homing = False
        self.cut_state = False
        self.is_check_cut_pos_start = False
        self.auto_retry = True
        self.wait_auto_retry_end = False
        method = mcu.get_printer_mcu
        try:
            self.mcu = method(self.printer, 'mcu')
        finally:
            del method
        method = mcu.get_printer_mcu
        try:
            self.nozzle_mcu = method(self.printer, 'nozzle_mcu')
        finally:
            del method
        self.oid = self.nozzle_mcu.create_oid()
        method = self.nozzle_mcu.register_config_callback
        try:
            method(self._build_config)
        finally:
            del method
        self._stats_motor_get_value = ''
        self.motor_params_init = False
        method = self.gcode.register_command
        try:
            method('MOTOR_REBOOT', self.motor_reboot, desc=self.motor_reboot_help)
        finally:
            del method
        method = self.gcode.register_command
        try:
            method('MOTOR_ENCODER_CALIBRATE_OFFICIAL', self.motor_encoder_calibrate_official, desc=self.motor_encoder_calibrate_official_help)
        finally:
            del method
        method = self.gcode.register_command
        try:
            method('MOTOR_ELEC_OFFSET_CALIBRATE', self.motor_elec_offset_calibrate, desc=self.motor_elec_offset_calibrate_help)
        finally:
            del method
        method = self.gcode.register_command
        try:
            method('MOTOR_BOOT', self.motor_boot, desc=self.motor_boot_help)
        finally:
            del method
        method = self.gcode.register_command
        try:
            method('MOTOR_READ_ADDR', self.motor_read485_addr, desc=self.motor_read485_addr_help)
        finally:
            del method
        method = self.gcode.register_command
        try:
            method('MOTOR_SET_ADDR', self.motor_set485_addr, desc=self.motor_set485_addr_help)
        finally:
            del method
        method = self.gcode.register_command
        try:
            method('MOTOR_CONTROL', self.motor_control, desc=self.motor_control_help)
        finally:
            del method
        method = self.gcode.register_command
        try:
            method('MOTOR_GET', self.motor_get, desc=self.motor_get_help)
        finally:
            del method
        method = self.gcode.register_command
        try:
            method('MOTOR_FLASH_PARAM', self.motor_flash_param, desc=self.motor_flash_param_help)
        finally:
            del method
        method = self.gcode.register_command
        try:
            method('MOTOR_SYS_PARAM', self.motor_sys_param, desc=self.motor_sys_param_help)
        finally:
            del method
        method = self.gcode.register_command
        try:
            method('MOTOR_VERSION', self.motor_version, desc=self.motor_version_help)
        finally:
            del method
        method = self.gcode.register_command
        try:
            method('MOTOR_AUTO_CHECK_PROTECTION', self.motor_auto_check_protection, desc=self.motor_auto_check_protection_help)
        finally:
            del method
        method = self.gcode.register_command
        try:
            method('MOTOR_CHECK_PROTECTION_AFTER_HOME', self.motor_check_protection_after_home, desc=self.motor_check_protection_after_home_help)
        finally:
            del method
        method = self.gcode.register_command
        try:
            method('MOTOR_CLEAR_ERR_WARN_CODE', self.motor_clear_err_warn_code, desc=self.motor_clear_err_warn_code_help)
        finally:
            del method
        method = self.gcode.register_command
        try:
            method('MOTOR_STALL_MODE', self.motort_stall_mode, desc=self.motort_stall_mode_help)
        finally:
            del method
        method = self.gcode.register_command
        try:
            method('MOTOR_CHECK_CUT_POS', self.motor_check_cut_pos, desc=self.motor_check_cut_pos_help)
        finally:
            del method
        method = self.gcode.register_command
        try:
            method('MOTOR_READ_ALL_PIN_IO', self.motor_read_all_pin_io_status, desc=self.motor_read_all_pin_io_status_help)
        finally:
            del method
        method = self.gcode.register_command
        try:
            method('MOTOR_READ_PIN_IO', self.motor_read_pin_io, desc=self.motor_read_pin_io_help)
        finally:
            del method
        method = self.gcode.register_command
        try:
            method('TRANSPARENT_SEND', self.transparent_send_gcmd)
        finally:
            del method
        method = self.gcode.register_command
        try:
            method('MOTOR_ELEC_OFFSET_CALIBRATE_ALL', self.motor_elec_offset_calibrate_all, desc=self.motor_elec_offset_calibrate_all_help)
        finally:
            del method
        method = self.gcode.register_command
        try:
            method('MOTOR_EXTRUDER_RETRY_PROCESS', self.cmd_MOTOR_EXTRUDER_RETRY_PROCESS, desc=self.cmd_MOTOR_EXTRUDER_RETRY_PROCESS_help)
        finally:
            del method
        method = self.gcode.register_command
        try:
            method('MOTOR_VERIFY_SYS_PARAM', self.verify_sys_param)
        finally:
            del method
        method = self.printer.register_event_handler
        try:
            method('klippy:ready', self.find_objs)
        finally:
            del method
        self.button_stall_pin_init(config)
        self.cmd_transparent_send = None
        ppins = self.printer.lookup_object('pins')
        self.enable_pin_pa9 = ppins.setup_pin('digital_out', '!PA9')
        self.enable_pin_pa9.setup_max_duration(0.0)
        webhooks = self.printer.lookup_object('webhooks')
        method = webhooks.register_endpoint
        try:
            method('force_stop', self.web_force_stop)
        finally:
            del method
        self.position_min_cut_x = config.getsection('stepper_x').getfloat('position_min')
        self.position_min_cut_y = config.getsection('stepper_y').getfloat('position_min')
        method = self.printer.register_event_handler
        try:
            method('motor_control:extruder_motor_err_state', self.extruder_motor_err_state_query)
        finally:
            del method
        method = self.printer.register_event_handler
        try:
            method('motor_control:extruder_motor_err_clear', self.extruder_motor_err_clear)
        finally:
            del method

    def motor_extruder_flash_param(self, gcmd):
        if not gcmd.get("EXTRUDER", ""):
            return False
        num = gcmd.get_int("NUM", 0, minval=0, maxval=10)
        convert = self.bytes_to_int
        try:
            lookup = Motor_Addr.get
            try:
                key = str(num)
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                addr = convert(argument)
            finally:
                del argument
        finally:
            del convert
        convert = self.bytes_to_int
        try:
            lookup = Motor_cmd.get
            try:
                key = 'flash_param'
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                cmd = convert(argument)
            finally:
                del argument
        finally:
            del convert
        value = gcmd.get_int("DATA", None, minval=1, maxval=10)
        data_byte = self.parse.parse_num_to_byte(value)
        data_list = []
        data_list.append(value)
        if value == 3 or value == 4:
            data_byte += b"\x00"
            data_list.append(0)
        if value == 5 or value == 6:
            if not self.validate_id_params(gcmd):
                return
            if value == 5:
                if not self.validate_param(gcmd):
                    return
            id = gcmd.get_int("ID", None, minval=0, maxval=65536)
            params = gcmd.get("PARAMS", "")
            obj = params
            id_byte = self.parse.parse_num_to_byte(id)
            data_byte += id_byte
            data_list.append(id)
        if value == 5:
            if not self.validate_param_type_params(gcmd):
                return
            id_params_type = gcmd.get("PARAMS_TYPE", "float")
            if id_params_type == "float":
                pack = struct.pack
                try:
                    argument = float(params)
                    try:
                        float_bytes = pack("f", argument)
                    finally:
                        del argument
                finally:
                    del pack
                data_byte += float_bytes
                data_list.append(float(params))
                respond = self.gcode.respond_info
                try:
                    arguments = float_bytes
                    try:
                        message = "send float_bytes:%s" % arguments
                    finally:
                        del arguments
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond
            elif id_params_type == "uint16_t":
                pack = struct.pack
                try:
                    argument = int(params)
                    try:
                        uint16_t_bytes = pack("<H", argument)
                    finally:
                        del argument
                finally:
                    del pack
                data_byte += uint16_t_bytes
                data_list.append(int(params))
                respond = self.gcode.respond_info
                try:
                    arguments = uint16_t_bytes
                    try:
                        message = "send uint16_t:%s" % arguments
                    finally:
                        del arguments
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond
            elif id_params_type == "uint32_t":
                pack = struct.pack
                try:
                    argument = int(params)
                    try:
                        uint32_t_bytes = pack(">I", argument)
                    finally:
                        del argument
                finally:
                    del pack
                data_byte += uint32_t_bytes
                data_list.append(int(params))
                respond = self.gcode.respond_info
                try:
                    arguments = uint32_t_bytes
                    try:
                        message = "send uint32_t:%s" % arguments
                    finally:
                        del arguments
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond
            elif id_params_type == "int16_t":
                pack = struct.pack
                try:
                    argument = int(params)
                    try:
                        int16_t_bytes = pack("<h", argument)
                    finally:
                        del argument
                finally:
                    del pack
                data_byte += int16_t_bytes
                data_list.append(int(params))
                respond = self.gcode.respond_info
                try:
                    arguments = int16_t_bytes
                    try:
                        message = "send int16_t:%s" % arguments
                    finally:
                        del arguments
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond
            elif id_params_type == "int32_t":
                pack = struct.pack
                try:
                    argument = int(params)
                    try:
                        int32_t_bytes = pack(">I", argument)
                    finally:
                        del argument
                finally:
                    del pack
                data_byte += int32_t_bytes
                data_list.append(int(params))
                respond = self.gcode.respond_info
                try:
                    arguments = int32_t_bytes
                    try:
                        message = "send int32_t:%s" % arguments
                    finally:
                        del arguments
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond
        data = self.bytes_to_int(data_byte, is_data_obj=True)
        if value == 5:
            data_send = self.data_pack_extruder(addr, cmd, data_list, id_params_type=id_params_type)
        else:
            data_send = self.data_pack(addr, cmd, data_list)
        try:
            logging.info("data_send:%s", data_send)
            timeout = 8000
            send = self.cmd_transparent_send.send
            try:
                arguments = [self.oid, data_send, timeout]
                try:
                    params = send(arguments)
                finally:
                    del arguments
            finally:
                del send
            respond = self.gcode.respond_info
            try:
                arguments = (str(data_send), str(params))
                try:
                    message = "data_send:%s result:%s" % arguments
                finally:
                    del arguments
                try:
                    respond(message)
                finally:
                    del message
            finally:
                del respond
            if params.get("read"):
                result = params.get("read")[:-1]
                if value == 5:
                    respond = self.gcode.respond_info
                    try:
                        arguments = str(result)
                        try:
                            message = "result:%s" % arguments
                        finally:
                            del arguments
                        try:
                            respond(message)
                        finally:
                            del message
                    finally:
                        del respond
                    recv_value = self.result_to_value(result, id_params_type)
                    if recv_value is not None:
                        respond = self.gcode.respond_info
                        try:
                            arguments = (obj, recv_value)
                            try:
                                message = "set:%s recv:%s success" % arguments
                            finally:
                                del arguments
                            try:
                                respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                    else:
                        respond = self.gcode.respond_info
                        try:
                            arguments = (obj, recv_value)
                            try:
                                message = "set:%s recv:%s failed" % arguments
                            finally:
                                del arguments
                            try:
                                respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                motor_status = result[5:result[2] - 3 + 5]
                respond = self.gcode.respond_info
                try:
                    arguments = str(motor_status)
                    try:
                        message = "\u8bfb\u53d6\u5230\u7535\u673a\u8fd4\u56de\u503c:%s" % arguments
                    finally:
                        del arguments
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond
                if value == 6:
                    if motor_status and len(motor_status) == 1:
                        motor_status = [b"\x00" + motor_status]
                    if not self.validate_param_type_params(gcmd):
                        return
                    id_params_type = gcmd.get("PARAMS_TYPE", "float")
                    if motor_status and id_params_type == "float":
                        respond = self.gcode.respond_info
                        try:
                            arguments = (motor_status, struct.unpack("f", motor_status)[0])
                            try:
                                message = "flash\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s value:%s" % arguments
                            finally:
                                del arguments
                            try:
                                respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                    elif motor_status and id_params_type == "uint16_t":
                        respond = self.gcode.respond_info
                        try:
                            arguments = (motor_status, struct.unpack("<H", motor_status)[0])
                            try:
                                message = "flash\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s value:%s" % arguments
                            finally:
                                del arguments
                            try:
                                respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                    elif motor_status and id_params_type == "uint32_t":
                        respond = self.gcode.respond_info
                        try:
                            arguments = (motor_status, struct.unpack(">I", motor_status)[0])
                            try:
                                message = "flash\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s value:%s" % arguments
                            finally:
                                del arguments
                            try:
                                respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                    elif motor_status and id_params_type == "int16_t":
                        respond = self.gcode.respond_info
                        try:
                            arguments = (motor_status, struct.unpack("<h", motor_status)[0])
                            try:
                                message = "flash\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s value:%s" % arguments
                            finally:
                                del arguments
                            try:
                                respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                    elif motor_status and id_params_type == "int32_t":
                        respond = self.gcode.respond_info
                        try:
                            arguments = (motor_status, struct.unpack(">I", motor_status)[0])
                            try:
                                message = "flash\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s value:%s" % arguments
                            finally:
                                del arguments
                            try:
                                respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                    else:
                        respond = self.gcode.respond_info
                        try:
                            arguments = motor_status
                            try:
                                message = "flash\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s" % arguments
                            finally:
                                del arguments
                            try:
                                respond(message)
                            finally:
                                del message
                        finally:
                            del respond
        except Exception as err:
            logging.exception(err)
        return True

    def motor_extruder_sys_param(self, gcmd):
        if not gcmd.get("EXTRUDER", ""):
            return False
        num = gcmd.get_int("NUM", 0, minval=0, maxval=10)
        convert = self.bytes_to_int
        try:
            lookup = Motor_Addr.get
            try:
                key = str(num)
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                addr = convert(argument)
            finally:
                del argument
        finally:
            del convert
        convert = self.bytes_to_int
        try:
            lookup = Motor_cmd.get
            try:
                key = 'sys_param'
                try:
                    argument = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            try:
                cmd = convert(argument)
            finally:
                del argument
        finally:
            del convert
        value = gcmd.get_int("DATA", 1, minval=1, maxval=10)
        data_list = []
        data_byte = self.parse.parse_num_to_byte(value)
        data_list.append(value)
        if not self.validate_id_params(gcmd):
            return
        if value == 1:
            if not self.validate_param(gcmd):
                return
        id = gcmd.get_int("ID", -1, minval=-1, maxval=65536)
        params = gcmd.get("PARAMS", "")
        obj = params
        id_byte = self.parse.parse_num_to_byte(id)
        data_byte += id_byte
        data_list.append(id)
        if value == 1:
            if not self.validate_param_type_params(gcmd):
                return
            id_params_type = gcmd.get("PARAMS_TYPE", "float")
            if id_params_type == "float":
                pack = struct.pack
                try:
                    argument = float(params)
                    try:
                        float_bytes = pack("f", argument)
                    finally:
                        del argument
                finally:
                    del pack
                data_byte += float_bytes
                data_list.append(float(params))
                respond = self.gcode.respond_info
                try:
                    arguments = float_bytes
                    try:
                        message = "send float_bytes:%s" % arguments
                    finally:
                        del arguments
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond
            elif id_params_type == "uint16_t":
                pack = struct.pack
                try:
                    argument = int(params)
                    try:
                        uint16_t_bytes = pack("<H", argument)
                    finally:
                        del argument
                finally:
                    del pack
                data_byte += uint16_t_bytes
                data_list.append(int(params))
                respond = self.gcode.respond_info
                try:
                    arguments = uint16_t_bytes
                    try:
                        message = "send uint16_t:%s" % arguments
                    finally:
                        del arguments
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond
            elif id_params_type == "uint32_t":
                pack = struct.pack
                try:
                    argument = int(params)
                    try:
                        uint32_t_bytes = pack(">I", argument)
                    finally:
                        del argument
                finally:
                    del pack
                data_byte += uint32_t_bytes
                data_list.append(int(params))
                respond = self.gcode.respond_info
                try:
                    arguments = uint32_t_bytes
                    try:
                        message = "send uint32_t:%s" % arguments
                    finally:
                        del arguments
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond
            elif id_params_type == "int16_t":
                pack = struct.pack
                try:
                    argument = int(params)
                    try:
                        int16_t_bytes = pack("<h", argument)
                    finally:
                        del argument
                finally:
                    del pack
                data_byte += int16_t_bytes
                data_list.append(int(params))
                respond = self.gcode.respond_info
                try:
                    arguments = int16_t_bytes
                    try:
                        message = "send int16_t:%s" % arguments
                    finally:
                        del arguments
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond
            elif id_params_type == "int32_t":
                pack = struct.pack
                try:
                    argument = int(params)
                    try:
                        int32_t_bytes = pack(">I", argument)
                    finally:
                        del argument
                finally:
                    del pack
                data_byte += int32_t_bytes
                data_list.append(int(params))
                respond = self.gcode.respond_info
                try:
                    arguments = int32_t_bytes
                    try:
                        message = "send int32_t:%s" % arguments
                    finally:
                        del arguments
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond
        data = self.bytes_to_int(data_byte, is_data_obj=True)
        if value == 1:
            data_send = self.data_pack_extruder(addr, cmd, data_list, id_params_type=id_params_type)
        else:
            data_send = self.data_pack(addr, cmd, data_list)
        try:
            logging.info("data_send:%s", data_send)
            timeout = 8000
            send = self.cmd_transparent_send.send
            try:
                arguments = [self.oid, data_send, timeout]
                try:
                    params = send(arguments)
                finally:
                    del arguments
            finally:
                del send
            respond = self.gcode.respond_info
            try:
                arguments = (str(data_send), str(params))
                try:
                    message = "data_send:%s result:%s" % arguments
                finally:
                    del arguments
                try:
                    respond(message)
                finally:
                    del message
            finally:
                del respond
            if params.get("read"):
                result = params.get("read")[:-1]
                if value == 1:
                    respond = self.gcode.respond_info
                    try:
                        arguments = str(result)
                        try:
                            message = "result:%s" % arguments
                        finally:
                            del arguments
                        try:
                            respond(message)
                        finally:
                            del message
                    finally:
                        del respond
                    recv_value = self.result_to_value(result, id_params_type)
                    if recv_value is not None:
                        respond = self.gcode.respond_info
                        try:
                            arguments = (obj, recv_value)
                            try:
                                message = "set:%s recv:%s success" % arguments
                            finally:
                                del arguments
                            try:
                                respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                    else:
                        respond = self.gcode.respond_info
                        try:
                            arguments = (obj, recv_value)
                            try:
                                message = "set:%s recv:%s failed" % arguments
                            finally:
                                del arguments
                            try:
                                respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                motor_status = result[5:result[2] - 3 + 5]
                if value == 2:
                    if motor_status and len(motor_status) == 1:
                        motor_status = [b"\x00" + motor_status]
                    if not self.validate_param_type_params(gcmd):
                        return
                    id_params_type = gcmd.get("PARAMS_TYPE", "float")
                    if motor_status and id_params_type == "float":
                        respond = self.gcode.respond_info
                        try:
                            arguments = (motor_status, struct.unpack("f", motor_status)[0])
                            try:
                                message = "sys\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s value:%s" % arguments
                            finally:
                                del arguments
                            try:
                                respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                    elif motor_status and id_params_type == "uint16_t":
                        respond = self.gcode.respond_info
                        try:
                            arguments = (motor_status, struct.unpack("<H", motor_status)[0])
                            try:
                                message = "sys\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s value:%s" % arguments
                            finally:
                                del arguments
                            try:
                                respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                    elif motor_status and id_params_type == "uint32_t":
                        respond = self.gcode.respond_info
                        try:
                            arguments = (motor_status, struct.unpack(">I", motor_status)[0])
                            try:
                                message = "sys\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s value:%s" % arguments
                            finally:
                                del arguments
                            try:
                                respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                    elif motor_status and id_params_type == "int16_t":
                        respond = self.gcode.respond_info
                        try:
                            arguments = (motor_status, struct.unpack("<h", motor_status)[0])
                            try:
                                message = "sys\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s value:%s" % arguments
                            finally:
                                del arguments
                            try:
                                respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                    elif motor_status and id_params_type == "int32_t":
                        respond = self.gcode.respond_info
                        try:
                            arguments = (motor_status, struct.unpack(">I", motor_status)[0])
                            try:
                                message = "sys\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s value:%s" % arguments
                            finally:
                                del arguments
                            try:
                                respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                    else:
                        respond = self.gcode.respond_info
                        try:
                            arguments = motor_status
                            try:
                                message = "sys\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s" % arguments
                            finally:
                                del arguments
                            try:
                                respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                elif value == 3:
                    if motor_status[0] == 0:
                        respond = self.gcode.respond_info
                        try:
                            arguments = motor_status
                            try:
                                message = "sys\u53c2\u6570\u64cd\u4f5c-\u66f4\u65b0:%s \u5931\u8d25" % arguments
                            finally:
                                del arguments
                            try:
                                respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                    if motor_status[0] == 1:
                        respond = self.gcode.respond_info
                        try:
                            arguments = motor_status
                            try:
                                message = "sys\u53c2\u6570\u64cd\u4f5c-\u66f4\u65b0:%s \u6210\u529f" % arguments
                            finally:
                                del arguments
                            try:
                                respond(message)
                            finally:
                                del message
                        finally:
                            del respond
        except Exception as err:
            logging.exception(err)
        return True


    def _button_handler_x(self, eventtime, state):
        if state and self.is_ready and not self.is_homing:
            callback = [self.check_protection_code]
            try:
                motor_num = MOTOR_NUM[0]
            except BaseException:
                callback.clear()
                raise
            if type(callback[0]) is MethodType:
                function = callback[0].__func__
                receiver = [callback[0].__self__]
                callback.clear()
                try:
                    result = function(receiver.pop(), motor_num)
                finally:
                    del motor_num
                    del function
            else:
                function = callback.pop()
                try:
                    result = function(motor_num)
                finally:
                    del motor_num
                    del function
            del result

    def _button_handler_y(self, eventtime, state):
        if state and self.is_ready and not self.is_homing:
            callback = [self.check_protection_code]
            try:
                motor_num = MOTOR_NUM[1]
            except BaseException:
                callback.clear()
                raise
            if type(callback[0]) is MethodType:
                function = callback[0].__func__
                receiver = [callback[0].__self__]
                callback.clear()
                try:
                    result = function(receiver.pop(), motor_num)
                finally:
                    del motor_num
                    del function
            else:
                function = callback.pop()
                try:
                    result = function(motor_num)
                finally:
                    del motor_num
                    del function
            del result

    def _button_handler_z(self, eventtime, state):
        if state and self.is_ready:
            callback = [self.check_protection_code]
            try:
                motor_num = MOTOR_NUM[2]
            except BaseException:
                callback.clear()
                raise
            if type(callback[0]) is MethodType:
                function = callback[0].__func__
                receiver = [callback[0].__self__]
                callback.clear()
                try:
                    result = function(receiver.pop(), motor_num)
                finally:
                    del motor_num
                    del function
            else:
                function = callback.pop()
                try:
                    result = function(motor_num)
                finally:
                    del motor_num
                    del function
            del result

    def _button_handler_z1(self, eventtime, state):
        if state and self.is_ready:
            callback = [self.check_protection_code]
            try:
                motor_num = MOTOR_NUM[3]
            except BaseException:
                callback.clear()
                raise
            if type(callback[0]) is MethodType:
                function = callback[0].__func__
                receiver = [callback[0].__self__]
                callback.clear()
                try:
                    result = function(receiver.pop(), motor_num)
                finally:
                    del motor_num
                    del function
            else:
                function = callback.pop()
                try:
                    result = function(motor_num)
                finally:
                    del motor_num
                    del function
            del result

    def button_stall_pin_init(self, config):
        buttons = self.printer.load_object(config, "buttons")
        for motor in self.motor_closed_loop:
            if motor == "x":
                register = buttons.register_buttons
                result = register([self.motor_x_stall], self._button_handler_x)
                del register
                del result
            elif motor == "y":
                register = buttons.register_buttons
                result = register([self.motor_y_stall], self._button_handler_y)
                del register
                del result
            elif motor == "z":
                register = buttons.register_buttons
                result = register([self.motor_z_stall], self._button_handler_z)
                del register
                del result
            elif motor == "z1":
                register = buttons.register_buttons
                result = register([self.motor_z1_stall], self._button_handler_z1)
                del register
                del result
            elif motor == "e":
                register = buttons.register_buttons
                result = register([self.motor_e_stall], self._button_handler_e)
                del register
                del result

    def _motor_flash_param(self, num, data, id_byte, id_params_type, params=None):
        result = None
        if not num:
            return result
        call = Motor_Addr.get
        try:
            addr = call(MOTOR_NUM_TO_IO_ADDR[num])
        finally:
            del call
        call = Motor_cmd.get
        try:
            cmd = call("flash_param")
        finally:
            del call
        call = self.parse.parse_num_to_byte
        try:
            data_byte = call(data)
        finally:
            del call
        if data == 3 or data == 4:
            data_byte = data_byte + b'\x00'
        if data == 5 or data == 6:
            data_byte = data_byte + id_byte
            if data == 5:
                if id_params_type == "float":
                    call = struct.pack
                    try:
                        float_bytes = call("f", float(params))
                    finally:
                        del call
                    data_byte = data_byte + float_bytes
                    call = self.gcode.respond_info
                    try:
                        response = call("send float_bytes:%s" % float_bytes)
                    finally:
                        del call
                    del response
                elif id_params_type == "uint16_t":
                    call = struct.pack
                    try:
                        uint16_t_bytes = call("<H", int(params))
                    finally:
                        del call
                    data_byte = data_byte + uint16_t_bytes
                    call = self.gcode.respond_info
                    try:
                        response = call("send uint16_t:%s" % uint16_t_bytes)
                    finally:
                        del call
                    del response
                elif id_params_type == "uint32_t":
                    call = struct.pack
                    try:
                        uint32_t_bytes = call(">I", int(params))
                    finally:
                        del call
                    data_byte = data_byte + uint32_t_bytes
                    call = self.gcode.respond_info
                    try:
                        response = call("send uint32_t:%s" % uint32_t_bytes)
                    finally:
                        del call
                    del response
                elif id_params_type == "int16_t":
                    call = struct.pack
                    try:
                        int16_t_bytes = call("<h", int(params))
                    finally:
                        del call
                    data_byte = data_byte + int16_t_bytes
                    call = self.gcode.respond_info
                    try:
                        response = call("send int16_t:%s" % int16_t_bytes)
                    finally:
                        del call
                    del response
                elif id_params_type == "int32_t":
                    call = struct.pack
                    try:
                        int32_t_bytes = call(">I", int(params))
                    finally:
                        del call
                    data_byte = data_byte + int32_t_bytes
                    call = self.gcode.respond_info
                    try:
                        response = call("send int32_t:%s" % int32_t_bytes)
                    finally:
                        del call
                    del response
        call = self.box_action.motor_send_data
        try:
            result = call(addr, cmd, TIMEOUT_MAX, data=data_byte, retries_en=False)
        finally:
            del call
        if result:
            motor_status = result[5:result[2] - 3 + 5]
            call = self.gcode.respond_info
            try:
                response = call("\u8bfb\u53d6\u5230\u7535\u673a\u8fd4\u56de\u503c:%s" % str(motor_status))
            finally:
                del call
            del response
            if data == 5 and motor_status[0] == 0:
                call = self.gcode.respond_info
                try:
                    response = call("flash\u53c2\u6570\u64cd\u4f5c-\u5199\u5165:%s \u5931\u8d25" % motor_status)
                finally:
                    del call
                del response
            elif data == 5 and motor_status[0] == 1:
                call = self.gcode.respond_info
                try:
                    response = call("flash\u53c2\u6570\u64cd\u4f5c-\u5199\u5165:%s \u6210\u529f" % motor_status)
                finally:
                    del call
                del response
            elif data == 6:
                if motor_status and len(motor_status) == 1:
                    motor_status = [b'\x00' + motor_status]
                if motor_status and id_params_type == "float":
                    respond = self.gcode.respond_info
                    try:
                        unpack = struct.unpack
                        try:
                            values = (str(motor_status), unpack('f', motor_status)[0])
                        finally:
                            del unpack
                        try:
                            message = "flash\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s value:%s" % values
                        finally:
                            del values
                        try:
                            response = respond(message)
                        finally:
                            del message
                    finally:
                        del respond
                    del response
                elif motor_status and id_params_type == "uint16_t":
                    respond = self.gcode.respond_info
                    try:
                        unpack = struct.unpack
                        try:
                            values = (str(motor_status), unpack('<H', motor_status)[0])
                        finally:
                            del unpack
                        try:
                            message = "flash\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s value:%s" % values
                        finally:
                            del values
                        try:
                            response = respond(message)
                        finally:
                            del message
                    finally:
                        del respond
                    del response
                elif motor_status and id_params_type == "uint32_t":
                    respond = self.gcode.respond_info
                    try:
                        unpack = struct.unpack
                        try:
                            values = (str(motor_status), unpack('>I', motor_status)[0])
                        finally:
                            del unpack
                        try:
                            message = "flash\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s value:%s" % values
                        finally:
                            del values
                        try:
                            response = respond(message)
                        finally:
                            del message
                    finally:
                        del respond
                    del response
                elif motor_status and id_params_type == "int16_t":
                    respond = self.gcode.respond_info
                    try:
                        unpack = struct.unpack
                        try:
                            values = (str(motor_status), unpack('<h', motor_status)[0])
                        finally:
                            del unpack
                        try:
                            message = "flash\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s value:%s" % values
                        finally:
                            del values
                        try:
                            response = respond(message)
                        finally:
                            del message
                    finally:
                        del respond
                    del response
                elif motor_status and id_params_type == "int32_t":
                    respond = self.gcode.respond_info
                    try:
                        unpack = struct.unpack
                        try:
                            values = (str(motor_status), unpack('>I', motor_status)[0])
                        finally:
                            del unpack
                        try:
                            message = "flash\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s value:%s" % values
                        finally:
                            del values
                        try:
                            response = respond(message)
                        finally:
                            del message
                    finally:
                        del respond
                    del response
                else:
                    call = self.gcode.respond_info
                    try:
                        response = call("flash\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s" % motor_status)
                    finally:
                        del call
                    del response
        return result

    def _motor_sys_param(self, num, data, id_byte=None, id_params_type='float', params=None):
        try:
            result = None
            if not num:
                return result
            call = Motor_Addr.get
            try:
                addr = call(MOTOR_NUM_TO_IO_ADDR[num])
            finally:
                del call
            call = Motor_cmd.get
            try:
                cmd = call("sys_param")
            finally:
                del call
            call = self.parse.parse_num_to_byte
            try:
                data_byte = call(data)
            finally:
                del call
            if data == 1 or data == 2:
                data_byte = data_byte + id_byte
                if data == 1:
                    if id_params_type == "float":
                        call = struct.pack
                        try:
                            float_bytes = call("f", float(params))
                        finally:
                            del call
                        data_byte = data_byte + float_bytes
                        call = self.gcode.respond_info
                        try:
                            response = call("send float_bytes:%s" % float_bytes)
                        finally:
                            del call
                        del response
                    elif id_params_type == "uint16_t":
                        call = struct.pack
                        try:
                            uint16_t_bytes = call("<H", int(params))
                        finally:
                            del call
                        data_byte = data_byte + uint16_t_bytes
                        call = self.gcode.respond_info
                        try:
                            response = call("send uint16_t:%s" % uint16_t_bytes)
                        finally:
                            del call
                        del response
                    elif id_params_type == "uint32_t":
                        call = struct.pack
                        try:
                            uint32_t_bytes = call(">I", int(params))
                        finally:
                            del call
                        data_byte = data_byte + uint32_t_bytes
                        call = self.gcode.respond_info
                        try:
                            response = call("send uint32_t:%s" % uint32_t_bytes)
                        finally:
                            del call
                        del response
                    elif id_params_type == "int16_t":
                        call = struct.pack
                        try:
                            int16_t_bytes = call("<h", int(params))
                        finally:
                            del call
                        data_byte = data_byte + int16_t_bytes
                        call = self.gcode.respond_info
                        try:
                            response = call("send int16_t:%s" % int16_t_bytes)
                        finally:
                            del call
                        del response
                    elif id_params_type == "int32_t":
                        call = struct.pack
                        try:
                            int32_t_bytes = call(">I", int(params))
                        finally:
                            del call
                        data_byte = data_byte + int32_t_bytes
                        call = self.gcode.respond_info
                        try:
                            response = call("send int32_t:%s" % int32_t_bytes)
                        finally:
                            del call
                        del response
            call = self.box_action.motor_send_data
            try:
                result = call(addr, cmd, TIMEOUT_MAX, data=data_byte, retries_en=False)
            finally:
                del call
            if result:
                motor_status = result[5:result[2] - 3 + 5]
                if data == 1:
                    if not motor_status:
                        call = self.gcode.respond_info
                        try:
                            response = call(f'sys\u53c2\u6570\u64cd\u4f5c-\u5199\u5165:{motor_status} \u5931\u8d25')
                        finally:
                            del call
                        del response
                    if id_params_type == "float" and len(motor_status) == 4:
                        call = struct.unpack
                        try:
                            write_echo_data = call("f", motor_status)
                        finally:
                            del call
                        write_echo_data = write_echo_data[0]
                    elif id_params_type == "uint16_t" and len(motor_status) == 2:
                        call = struct.unpack
                        try:
                            write_echo_data = call("<H", motor_status)
                        finally:
                            del call
                        write_echo_data = write_echo_data[0]
                    elif id_params_type == "uint32_t" and len(motor_status) == 4:
                        call = struct.unpack
                        try:
                            write_echo_data = call(">I", motor_status)
                        finally:
                            del call
                        write_echo_data = write_echo_data[0]
                    elif id_params_type == "int16_t" and len(motor_status) == 2:
                        call = struct.unpack
                        try:
                            write_echo_data = call("<h", motor_status)
                        finally:
                            del call
                        write_echo_data = write_echo_data[0]
                    elif id_params_type == "int32_t" and len(motor_status) == 4:
                        call = struct.unpack
                        try:
                            write_echo_data = call(">I", motor_status)
                        finally:
                            del call
                        write_echo_data = write_echo_data[0]
                    else:
                        write_echo_data = None
                    if write_echo_data is not None:
                        call = self.gcode.respond_info
                        try:
                            response = call(f'sys\u53c2\u6570\u64cd\u4f5c-\u5199\u5165\u6210\u529f:{id_byte} value:{write_echo_data}')
                        finally:
                            del call
                        del response
                    else:
                        call = self.gcode.respond_info
                        try:
                            response = call(f'sys\u53c2\u6570\u64cd\u4f5c-\u5199\u5165\u5931\u8d25:{motor_status}')
                        finally:
                            del call
                        del response
                elif data == 2:
                    if motor_status and len(motor_status) == 1:
                        motor_status = [b'\x00' + motor_status]
                    if motor_status and id_params_type == "float":
                        respond = self.gcode.respond_info
                        try:
                            unpack = struct.unpack
                            try:
                                values = (str(motor_status), unpack('f', motor_status)[0])
                            finally:
                                del unpack
                            try:
                                message = "sys\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s value:%s" % values
                            finally:
                                del values
                            try:
                                response = respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                        del response
                    elif motor_status and id_params_type == "uint16_t":
                        respond = self.gcode.respond_info
                        try:
                            unpack = struct.unpack
                            try:
                                values = (str(motor_status), unpack('<H', motor_status)[0])
                            finally:
                                del unpack
                            try:
                                message = "sys\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s value:%s" % values
                            finally:
                                del values
                            try:
                                response = respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                        del response
                    elif motor_status and id_params_type == "uint32_t":
                        respond = self.gcode.respond_info
                        try:
                            unpack = struct.unpack
                            try:
                                values = (str(motor_status), unpack('>I', motor_status)[0])
                            finally:
                                del unpack
                            try:
                                message = "sys\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s value:%s" % values
                            finally:
                                del values
                            try:
                                response = respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                        del response
                    elif motor_status and id_params_type == "int16_t":
                        respond = self.gcode.respond_info
                        try:
                            unpack = struct.unpack
                            try:
                                values = (str(motor_status), unpack('<h', motor_status)[0])
                            finally:
                                del unpack
                            try:
                                message = "sys\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s value:%s" % values
                            finally:
                                del values
                            try:
                                response = respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                        del response
                    elif motor_status and id_params_type == "int32_t":
                        respond = self.gcode.respond_info
                        try:
                            unpack = struct.unpack
                            try:
                                values = (str(motor_status), unpack('>I', motor_status)[0])
                            finally:
                                del unpack
                            try:
                                message = "sys\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s value:%s" % values
                            finally:
                                del values
                            try:
                                response = respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                        del response
                    else:
                        call = self.gcode.respond_info
                        try:
                            response = call("sys\u53c2\u6570\u64cd\u4f5c-\u8bfb\u53d6:%s" % motor_status)
                        finally:
                            del call
                        del response
                elif data == 3:
                    if motor_status[0] == 0:
                        call = self.gcode.respond_info
                        try:
                            response = call("sys\u53c2\u6570\u64cd\u4f5c-\u66f4\u65b0:%s \u5931\u8d25" % motor_status)
                        finally:
                            del call
                        del response
                    elif motor_status[0] == 1:
                        call = self.gcode.respond_info
                        try:
                            response = call("sys\u53c2\u6570\u64cd\u4f5c-\u66f4\u65b0:%s \u6210\u529f" % motor_status)
                        finally:
                            del call
                        del response
            return result
        except Exception as e:
            call = logging.warning
            try:
                response = call(f'_motor_sys_param error:{e}')
            finally:
                del call
            del response

    def extruder_motor_err_clear(self):
        logging.info('motor_control extruder_motor_err_clear start')
        if not self.is_ready:
            return
        print_stats = self.printer.lookup_object('print_stats')
        info = logging.info
        try:
            result = info('motor_control print_stats.state:%s' % print_stats.state)
        finally:
            del info
        del result
        if print_stats.state == 'printing':
            if self.printer.lookup_object('k2_print').pause_start == False:
                logging.info('motor_control start PAUSE...')
                self.gcode.run_script_from_command('PAUSE')
                logging.info('motor_control PAUSE END')
                self.gcode.run_script_from_command('M400')
                logging.info('motor_control end PAUSE...')
        self.gcode.run_script_from_command('SET_STEPPER_ENABLE STEPPER=extruder ENABLE=0')
        self.gcode.run_script_from_command('MOTOR_CLEAR_ERR_WARN_CODE NUM=0 DATA=5')
        logging.info('motor_control extruder_motor_err_clear end')

    def extruder_motor_retry(self, eventtime):
        self.wait_auto_retry_end = True
        self.auto_retry = False
        box = self.printer.lookup_object('box', None)
        if box:
            logging.info('start box.flush_material')
            self.gcode.run_script('MOTOR_EXTRUDER_RETRY_PROCESS')

    def motor_flash_param(self, gcmd):
        call = self.validate_num_params
        try:
            valid = call(gcmd)
        finally:
            del call
        if not valid:
            return
        del valid
        call = self.validate_data_params
        try:
            valid = call(gcmd)
        finally:
            del call
        if not valid:
            return
        del valid
        call = gcmd.get
        try:
            extruder = call('EXTRUDER', '')
        finally:
            del call
        try:
            is_extruder = bool(extruder)
        finally:
            del extruder
        if is_extruder:
            if hasattr(self, 'motor_e_stall'):
                call = self.motor_extruder_flash_param
                try:
                    response = call(gcmd)
                finally:
                    del call
                del response
            return
        call = gcmd.get_int
        try:
            num = call('NUM', None, minval=1, maxval=10)
        finally:
            del call
        call = gcmd.get_int
        try:
            data = call('DATA', None, minval=1, maxval=10)
        finally:
            del call
        call = gcmd.get_int
        try:
            param_id = call('ID', None, minval=0, maxval=65536)
        finally:
            del call
        id_byte = b''
        if param_id is not None:
            call = self.parse.parse_num_to_byte
            try:
                id_byte = call(param_id)
            finally:
                del call
        call = gcmd.get
        try:
            params = call('PARAMS', '')
        finally:
            del call
        call = gcmd.get
        try:
            id_params_type = call('PARAMS_TYPE', 'float')
        finally:
            del call
        call = self._motor_flash_param
        try:
            response = call(num, data, id_byte, id_params_type, params=params)
        finally:
            del call
        del response

    def motor_sys_param(self, gcmd):
        call = self.validate_num_params
        try:
            valid = call(gcmd)
        finally:
            del call
        if not valid:
            return
        del valid
        call = self.validate_data_params
        try:
            valid = call(gcmd)
        finally:
            del call
        if not valid:
            return
        del valid
        try:
            call = gcmd.get
            try:
                extruder = call('EXTRUDER', '')
            finally:
                del call
            try:
                is_extruder = bool(extruder)
            finally:
                del extruder
            if is_extruder:
                if hasattr(self, 'motor_e_stall'):
                    call = self.motor_extruder_sys_param
                    try:
                        response = call(gcmd)
                    finally:
                        del call
                    del response
                return
            call = gcmd.get_int
            try:
                num = call('NUM', None, minval=1, maxval=10)
            finally:
                del call
            call = gcmd.get_int
            try:
                data = call('DATA', None, minval=1, maxval=10)
            finally:
                del call
            call = gcmd.get_int
            try:
                param_id = call('ID', None, minval=0, maxval=65536)
            finally:
                del call
            id_byte = b''
            if param_id is not None:
                call = self.parse.parse_num_to_byte
                try:
                    id_byte = call(param_id)
                finally:
                    del call
            call = gcmd.get
            try:
                params = call('PARAMS', '')
            finally:
                del call
            call = gcmd.get
            try:
                id_params_type = call('PARAMS_TYPE', 'float')
            finally:
                del call
            call = self._motor_sys_param
            try:
                response = call(num, data, id_byte, id_params_type, params=params)
            finally:
                del call
            del response
        except Exception as e:
            call = logging.warning
            try:
                response = call(e)
            finally:
                del call
            del response


    def motor_init(self, config):
        for obj in self.motor_closed_loop:
            init_method = getattr(self, f'motor_{obj}_init', None)
            if init_method:
                init_method(config)

        class MotorFlashParam:
            def __init__(self, value_id: Number, value: Number,
                         min_value: Number = None, max_value: Number = None,
                         value_type = 'float', tolerance: float = 1e-6,
                         configurable = False,
                         scope: Tuple[Number, Number] = (-1e999, 1e999),
                         reference_value: Number = None, override = False):
                tolerance = c_double(tolerance).value
                if value_type is not None and type(value_type) is not str:
                    raise TypeError("Argument 'value_type' has incorrect type (expected str, got %s)" % type(value_type).__name__)
                self.value_id = value_id
                self.value = value
                self.min_value = min_value
                self.max_value = max_value
                self.value_type = value_type
                self.tolerance = tolerance
                self.configurable = configurable
                self.scope = scope
                self.reference_value = reference_value
                self.override = override

        MotorFlashParam.__init__.__annotations__ = {
            'value_id': 'int', 'value': Number,
            'min_value': Number, 'max_value': Number,
            'value_type': 'str', 'tolerance': 'float',
            'configurable': bool, 'scope': Tuple[Number],
            'reference_value': Number, 'override': bool,
        }

        def init_system_params():
            previous_evaluation = {}
            for stepper, sys_params_table in self.motor_params_table.items():
                for key, value in sys_params_table.items():
                    get_value = config.get
                    try:
                        config_value = get_value(f'{stepper}_{key}', None)
                    finally:
                        del get_value
                    if config_value is None:
                        value_id = value.value_id
                        if value_id == 9 or value_id == 10 or value_id == 25:
                            value.override = True
                        else:
                            config_value = value.value
                    else:
                        value.override = True
                    expression = str(config_value)
                    evaluation_scope = {'config_value': config_value}
                    if 'config_value_eval' in previous_evaluation:
                        evaluation_scope['config_value_eval'] = config_value_eval
                    evaluation_scope['key'] = key
                    evaluation_scope['stepper'] = stepper
                    evaluation_scope['sys_params_table'] = sys_params_table
                    evaluation_scope['value'] = value
                    config_value_eval = eval(expression, globals(), evaluation_scope)
                    previous_evaluation['config_value_eval'] = True
                    value.value = config_value_eval

        motor_valid_addr_list = [stepper for stepper in MOTOR_NAME_TO_NUM
                                 if getattr(self, f'motor_{stepper}_stall', None)]
        for stepper in motor_valid_addr_list:
            self.motor_params_table[stepper] = {
                "param_flash_param_version": MotorFlashParam(value_id=0, value=518, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_boot_key": MotorFlashParam(value_id=1, value=17030, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_flash_key_write_retries_num": MotorFlashParam(value_id=2, value=5, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_system_startup_delay_ms": MotorFlashParam(value_id=3, value=100, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_pwm_period_s": MotorFlashParam(value_id=4, value=0.000035, min_value=0, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_task_period_s": MotorFlashParam(value_id=5, value=0.00014, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_use_default_param": MotorFlashParam(value_id=6, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_singleturn_control_mode": MotorFlashParam(value_id=7, value=1, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_elec_angle_calibration_standar_deg": MotorFlashParam(value_id=8, value=15.0, min_value=0, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_elec_offset": MotorFlashParam(value_id=9, value=282.4, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_phase_order_invert": MotorFlashParam(value_id=10, value=0, min_value=None, max_value=None, value_type="int16_t", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_emergency_pin_mode": MotorFlashParam(value_id=11, value=1, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_stall_mode": MotorFlashParam(value_id=12, value=2, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_stall_cur_A": MotorFlashParam(value_id=13, value=0.7, min_value=0, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_stall_pos_err_rad": MotorFlashParam(value_id=14, value=0.015, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_calibration_dir": MotorFlashParam(value_id=15, value=-1, min_value=None, max_value=None, value_type="int16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_encoder_cal_count_max": MotorFlashParam(value_id=16, value=200, min_value=None, max_value=None, value_type="int32_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_encoder_read_count": MotorFlashParam(value_id=17, value=10, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_delay_ms_per_step": MotorFlashParam(value_id=18, value=10, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_delay_ms_per_read": MotorFlashParam(value_id=19, value=1, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_ud_cal_set": MotorFlashParam(value_id=20, value=0.15, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_motion_dir": MotorFlashParam(value_id=21, value=1, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_encoder_calibtate_official_cal_count_max": MotorFlashParam(value_id=22, value=6400, min_value=None, max_value=None, value_type="uint32_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_encoder_calibtate_official_ud_cal_set": MotorFlashParam(value_id=23, value=0.15, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_encoder_calibration_round_count": MotorFlashParam(value_id=24, value=40, min_value=None, max_value=None, value_type="uint32_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_elec_offset_err_deg": MotorFlashParam(value_id=25, value=3.8, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "driver_board_param_power_supply_V": MotorFlashParam(value_id=26, value=24, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "driver_board_param_adc_ref_V": MotorFlashParam(value_id=27, value=3.3, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "driver_board_param_amp_gain": MotorFlashParam(value_id=28, value=20, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "driver_board_param_power_voltage_sample_gain": MotorFlashParam(value_id=29, value=11.234, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "driver_board_param_R_of_current_sample_ohm": MotorFlashParam(value_id=30, value=0.05, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "driver_board_param_adc_calibration_start_index": MotorFlashParam(value_id=31, value=100, min_value=None, max_value=None, value_type="int16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "driver_board_param_adc_calibration_buffer_size": MotorFlashParam(value_id=32, value=3000, min_value=None, max_value=None, value_type="int16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "driver_board_param_led_toggle_period_s": MotorFlashParam(value_id=33, value=0.5, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "driver_board_param_current_dir": MotorFlashParam(value_id=34, value=-1, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "driver_board_param_pwm_output_en": MotorFlashParam(value_id=35, value=0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "driver_board_param_mos_Ron": MotorFlashParam(value_id=36, value=0.2, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "driver_board_param_mos_deadtime": MotorFlashParam(value_id=37, value=2.5e-7, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "driver_board_param_mos_rise_fall_time": MotorFlashParam(value_id=38, value=1.5e-7, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "driver_board_param_mos_max_peak_current": MotorFlashParam(value_id=39, value=6.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "driver_board_param_mos_max_continuous_current": MotorFlashParam(value_id=40, value=4.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "motor_param_R": MotorFlashParam(value_id=41, value=0.6, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "motor_param_L": MotorFlashParam(value_id=42, value=0.001, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "motor_param_Ke": MotorFlashParam(value_id=43, value=0.101859, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "motor_param_Kt": MotorFlashParam(value_id=44, value=0.32, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "motor_param_mass": MotorFlashParam(value_id=45, value=0.06, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "motor_param_J": MotorFlashParam(value_id=46, value=0.01, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "motor_param_B": MotorFlashParam(value_id=47, value=0.01, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "motor_param_stepping_angle_degree": MotorFlashParam(value_id=48, value=1.8, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "motor_param_phase_nums": MotorFlashParam(value_id=49, value=2, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "motor_param_pole_pair": MotorFlashParam(value_id=50, value=50, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "motor_param_power_supply": MotorFlashParam(value_id=51, value=24, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "motor_param_rated_voltage": MotorFlashParam(value_id=52, value=2.5, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "motor_param_rated_current": MotorFlashParam(value_id=53, value=1.5, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "motor_param_maximum_holding_torque": MotorFlashParam(value_id=54, value=70, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "encoder_param_direction": MotorFlashParam(value_id=55, value=1, min_value=None, max_value=None, value_type="int32_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "encoder_param_encoder_bit": MotorFlashParam(value_id=56, value=15, min_value=None, max_value=None, value_type="uint32_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "encoder_param_encoder_max_pulse": MotorFlashParam(value_id=57, value=32768, min_value=None, max_value=None, value_type="int32_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "encoder_param_fake_encoder_en": MotorFlashParam(value_id=58, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "encoder_param_fake_encoder_step": MotorFlashParam(value_id=59, value=-1, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "mcu_param_sysclk_hz": MotorFlashParam(value_id=60, value=144000000, min_value=None, max_value=None, value_type="uint32_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "mcu_param_HSE_hz": MotorFlashParam(value_id=61, value=12000000, min_value=None, max_value=None, value_type="uint32_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "mcu_param_uart_baudrate": MotorFlashParam(value_id=62, value=230400, min_value=None, max_value=None, value_type="uint32_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "mcu_param_input_pulse_counter_trigger_mode": MotorFlashParam(value_id=63, value=1, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "mcu_param_input_pulse_counter_filter_level": MotorFlashParam(value_id=64, value=4, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_param_control_algorithm": MotorFlashParam(value_id=65, value=1, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_param_en_feedforward_idiq": MotorFlashParam(value_id=66, value=1, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_param_en_feedforward_uduq": MotorFlashParam(value_id=67, value=1, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_pos_loop_pid_param_en": MotorFlashParam(value_id=68, value=1, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_pos_loop_pid_param_bypass": MotorFlashParam(value_id=69, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_pos_loop_pid_param_kp": MotorFlashParam(value_id=70, value=300.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_pos_loop_pid_param_ki": MotorFlashParam(value_id=71, value=0.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_pos_loop_pid_param_kd": MotorFlashParam(value_id=72, value=0.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_pos_loop_pid_param_kc": MotorFlashParam(value_id=73, value=0.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_pos_loop_pid_ud_filter_param_en": MotorFlashParam(value_id=74, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_pos_loop_pid_ud_filter_param_fc": MotorFlashParam(value_id=75, value=1000, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_pos_loop_pid_param_limit_ui": MotorFlashParam(value_id=76, value=0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_pos_loop_pid_parma_limit_out": MotorFlashParam(value_id=77, value=314.1592653589, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_pos_loop_pid_fal_param_en": MotorFlashParam(value_id=78, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_pos_loop_pid_fal_param_a": MotorFlashParam(value_id=79, value=1.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_pos_loop_pid_fal_param_d": MotorFlashParam(value_id=80, value=1, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_pos_loop_pid_fal_param_zoom": MotorFlashParam(value_id=81, value=0.01, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_pos_loop_pid_fal_param_section_nums": MotorFlashParam(value_id=82, value=5, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_pos_loop_pid_fal_param_inteval_gain": MotorFlashParam(value_id=83, value=3, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_loop_pid_param_en": MotorFlashParam(value_id=84, value=1, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_loop_pid_param_bypass": MotorFlashParam(value_id=85, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_loop_pid_param_kp": MotorFlashParam(value_id=86, value=0.025, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_loop_pid_param_ki": MotorFlashParam(value_id=87, value=8, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_loop_pid_param_kd": MotorFlashParam(value_id=88, value=0.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_loop_pid_param_kc": MotorFlashParam(value_id=89, value=0.5, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_loop_pid_ud_filter_param_en": MotorFlashParam(value_id=90, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_loop_pid_ud_filter_param_fc": MotorFlashParam(value_id=91, value=1000, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_loop_pid_param_limit_ui": MotorFlashParam(value_id=92, value=5.5, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_loop_pid_parma_limit_out": MotorFlashParam(value_id=93, value=5.5, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_loop_pid_fal_param_en": MotorFlashParam(value_id=94, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_loop_pid_fal_param_a": MotorFlashParam(value_id=95, value=0.7, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_loop_pid_fal_param_d": MotorFlashParam(value_id=96, value=1, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_loop_pid_fal_param_zoom": MotorFlashParam(value_id=97, value=0.1, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_loop_pid_fal_param_section_nums": MotorFlashParam(value_id=98, value=6, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_loop_pid_fal_param_inteval_gain": MotorFlashParam(value_id=99, value=4, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_cur_loop_pid_param_en": MotorFlashParam(value_id=100, value=1, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_cur_loop_pid_param_bypass": MotorFlashParam(value_id=101, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_cur_loop_pid_param_kp": MotorFlashParam(value_id=102, value=2.5, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_cur_loop_pid_param_ki": MotorFlashParam(value_id=103, value=200.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_cur_loop_pid_param_kd": MotorFlashParam(value_id=104, value=0.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_cur_loop_pid_param_kc": MotorFlashParam(value_id=105, value=0.5, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_cur_loop_pid_ud_filter_param_en": MotorFlashParam(value_id=106, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_cur_loop_pid_ud_filter_param_fc": MotorFlashParam(value_id=107, value=1000, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_cur_loop_pid_param_limit_ui": MotorFlashParam(value_id=108, value=1.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_cur_loop_pid_parma_limit_out": MotorFlashParam(value_id=109, value=1.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_cur_loop_pid_fal_param_en": MotorFlashParam(value_id=110, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_cur_loop_pid_fal_param_a": MotorFlashParam(value_id=111, value=1.3, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_cur_loop_pid_fal_param_d": MotorFlashParam(value_id=112, value=1, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_cur_loop_pid_fal_param_zoom": MotorFlashParam(value_id=113, value=0.25, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_cur_loop_pid_fal_param_section_nums": MotorFlashParam(value_id=114, value=5, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_cur_loop_pid_fal_param_inteval_gain": MotorFlashParam(value_id=115, value=3, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_leso_param_b0k": MotorFlashParam(value_id=116, value=1, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_leso_param_b0": MotorFlashParam(value_id=117, value=6000, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_leso_param_z3k": MotorFlashParam(value_id=118, value=0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_leso_param_wp": MotorFlashParam(value_id=119, value=4000, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_leso_param_ws": MotorFlashParam(value_id=120, value=4000, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_leso_param_wd": MotorFlashParam(value_id=121, value=400, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_fwc_param_en": MotorFlashParam(value_id=122, value=1, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_fwc_param_I_max": MotorFlashParam(value_id=123, value=5.5, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_fwc_param_L": MotorFlashParam(value_id=124, value=0.001, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_fwc_param_phi_e": MotorFlashParam(value_id=125, value=0.00203718, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_fwc_param_pole_pair": MotorFlashParam(value_id=126, value=50, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_fwc_param_wm_base": MotorFlashParam(value_id=127, value=100, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_cur_filter_param_en": MotorFlashParam(value_id=128, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_cur_filter_param_Ts": MotorFlashParam(value_id=129, value=0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_cur_filter_param_fc": MotorFlashParam(value_id=130, value=6000, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_z3_filter_binary_second_order_param_en": MotorFlashParam(value_id=131, value=1, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_z3_filter_binary_second_order_param_fc": MotorFlashParam(value_id=132, value=2000, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_z3_filter_binary_second_order_param_depth": MotorFlashParam(value_id=133, value=0.5, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_z3_filter_binary_second_order_param_zeta_0N": MotorFlashParam(value_id=134, value=0.2, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_z3_filter_binary_second_order_param_zeta_0D": MotorFlashParam(value_id=135, value=0.5, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_out_filter_binary_second_order_param_en": MotorFlashParam(value_id=136, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_out_filter_binary_second_order_param_fc": MotorFlashParam(value_id=137, value=10000, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_out_filter_binary_second_order_param_depth": MotorFlashParam(value_id=138, value=0.3333333333333333, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_out_filter_binary_second_order_param_zeta_0N": MotorFlashParam(value_id=139, value=0.3, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_out_filter_binary_second_order_param_zeta_0D": MotorFlashParam(value_id=140, value=0.5, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_td_param_en": MotorFlashParam(value_id=141, value=1, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_td_param_h0_gain_h": MotorFlashParam(value_id=142, value=20.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_td_fhan_param_f0": MotorFlashParam(value_id=143, value=1000.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_td_param_d_gain": MotorFlashParam(value_id=144, value=1.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_td_param_dd_gain": MotorFlashParam(value_id=145, value=0.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_out_filter_notch_0_param_en": MotorFlashParam(value_id=146, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_out_filter_notch_0_param_f0": MotorFlashParam(value_id=147, value=10000, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_out_filter_notch_0_param_f_width": MotorFlashParam(value_id=148, value=30, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_out_filter_notch_0_param_depth": MotorFlashParam(value_id=149, value=0.5, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_out_filter_notch_1_param_en": MotorFlashParam(value_id=150, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_out_filter_notch_1_param_f0": MotorFlashParam(value_id=151, value=120, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_out_filter_notch_1_param_f_width": MotorFlashParam(value_id=152, value=20, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_out_filter_notch_1_param_depth": MotorFlashParam(value_id=153, value=0.5, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_out_filter_notch_2_param_en": MotorFlashParam(value_id=154, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_out_filter_notch_2_param_f0": MotorFlashParam(value_id=155, value=30, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_out_filter_notch_2_param_f_width": MotorFlashParam(value_id=156, value=10, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_spd_out_filter_notch_2_param_depth": MotorFlashParam(value_id=157, value=0.5, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_motor_ripple_compensator_en": MotorFlashParam(value_id=158, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_motor_ripple_compensator_sin_coeffs_0_term_A": MotorFlashParam(value_id=159, value=0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_motor_ripple_compensator_sin_coeffs_0_term_n": MotorFlashParam(value_id=160, value=0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_motor_ripple_compensator_sin_coeffs_0_term_phi": MotorFlashParam(value_id=161, value=0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_motor_ripple_compensator_sin_coeffs_1_term_A": MotorFlashParam(value_id=162, value=0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_motor_ripple_compensator_sin_coeffs_1_term_n": MotorFlashParam(value_id=163, value=0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_motor_ripple_compensator_sin_coeffs_1_term_phi": MotorFlashParam(value_id=164, value=0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_motor_ripple_compensator_sin_coeffs_2_term_A": MotorFlashParam(value_id=165, value=0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_motor_ripple_compensator_sin_coeffs_2_term_n": MotorFlashParam(value_id=166, value=0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "controller_motor_ripple_compensator_sin_coeffs_2_term_phi": MotorFlashParam(value_id=167, value=0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "flash_manager_param_checksum_restore_en": MotorFlashParam(value_id=168, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "logger_param_en": MotorFlashParam(value_id=169, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "logger_param_continuous_send": MotorFlashParam(value_id=170, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "logger_param_sample_interval_n": MotorFlashParam(value_id=171, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "logger_param_n_pack_to_send": MotorFlashParam(value_id=172, value=0, min_value=None, max_value=None, value_type="uint32_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "protection_param_protect_en": MotorFlashParam(value_id=173, value=1, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "protection_param_protect_report": MotorFlashParam(value_id=174, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "protection_param_err_code_mask": MotorFlashParam(value_id=175, value=4294952959, min_value=None, max_value=None, value_type="uint32_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "protection_param_warning_code_mask": MotorFlashParam(value_id=176, value=4294967295, min_value=None, max_value=None, value_type="uint32_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "protection_param_encoder_mutation_coefficient_upper": MotorFlashParam(value_id=177, value=0.5, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "protection_param_encoder_mutation_coefficient_lower": MotorFlashParam(value_id=178, value=0.1, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "protection_param_prt_peak_cur_A": MotorFlashParam(value_id=179, value=999.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "protection_param_prt_continuous_cur_A": MotorFlashParam(value_id=180, value=4.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "protection_param_prt_continuous_time_s": MotorFlashParam(value_id=181, value=1.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "protection_param_prt_over_speed_rad_s": MotorFlashParam(value_id=182, value=314.1592653589793, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "protection_param_prt_over_speed_time_s": MotorFlashParam(value_id=183, value=0.1, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "protection_param_pos_over_limit_max_rad": MotorFlashParam(value_id=184, value=999, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "protection_param_pos_over_limit_min_rad": MotorFlashParam(value_id=185, value=-999, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "protection_param_prt_cmd_mutation_turn": MotorFlashParam(value_id=186, value=0.1, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "protection_param_prt_track_max_err": MotorFlashParam(value_id=187, value=0.1, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "protection_param_prt_track_err_time": MotorFlashParam(value_id=188, value=0.1, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "protection_param_power_voltage_min": MotorFlashParam(value_id=189, value=20.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "protection_param_mcu_temp_max": MotorFlashParam(value_id=190, value=115.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "protection_err_code_backup": MotorFlashParam(value_id=191, value=0, min_value=None, max_value=None, value_type="uint32_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "protection_warning_code_backup": MotorFlashParam(value_id=192, value=0, min_value=None, max_value=None, value_type="uint32_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "chopper_param_chopper_mode": MotorFlashParam(value_id=193, value=1, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "chopper_param_svpwm_mode": MotorFlashParam(value_id=194, value=1, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "chopper_param_base_pwm": MotorFlashParam(value_id=195, value=0.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "chopper_param_offset_pwm_en": MotorFlashParam(value_id=196, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "chopper_param_offset_pwm": MotorFlashParam(value_id=197, value=0.02, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "chopper_param_hybrid_decay_ratio": MotorFlashParam(value_id=198, value=999999999, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "step_controller_param_pulse_count_max": MotorFlashParam(value_id=199, value=65000, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "step_controller_param_subdivision": MotorFlashParam(value_id=200, value=16, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "step_controller_param_pulse_trim_en": MotorFlashParam(value_id=201, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "step_controller_param_trim_action_period": MotorFlashParam(value_id=202, value=10, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "step_controller_param_extra_trim_pulse": MotorFlashParam(value_id=203, value=10, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "cmd_int_param_char_cmd_support": MotorFlashParam(value_id=204, value=1, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "system_id_RL_param_param_uin_startup": MotorFlashParam(value_id=205, value=0.06, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "system_id_RL_param_param_uin_calibrate": MotorFlashParam(value_id=206, value=0.15, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "system_id_RL_param_param_current_stable_err_condition": MotorFlashParam(value_id=207, value=0.005, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "system_id_RL_param_param_current_buff_size": MotorFlashParam(value_id=208, value=500, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "system_id_RL_param_param_current_err_size": MotorFlashParam(value_id=209, value=20, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "system_id_RL_param_param_prepare_time": MotorFlashParam(value_id=210, value=5000, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "system_id_RL_param_param_normal_R_range_min": MotorFlashParam(value_id=211, value=0.1, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "system_id_RL_param_param_normal_R_range_max": MotorFlashParam(value_id=212, value=100, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "system_id_RL_param_param_normal_L_range_min": MotorFlashParam(value_id=213, value=0.000001, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "system_id_RL_param_normal_L_range_max": MotorFlashParam(value_id=214, value=1.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "motor_phase_check_param_run_on_startup": MotorFlashParam(value_id=215, value=1, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "motor_phase_check_param_motor_resistance_tolerance_standar_percent": MotorFlashParam(value_id=216, value=30, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "zazen_param_zazen_en": MotorFlashParam(value_id=217, value=1, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "zazen_param_zazen_trigger_time_s": MotorFlashParam(value_id=218, value=1.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "zazen_param_zazen_gain_pos_kp": MotorFlashParam(value_id=219, value=1.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "zazen_param_zazen_gain_spd_kp": MotorFlashParam(value_id=220, value=0.5, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "zazen_param_zazen_gain_spd_ki": MotorFlashParam(value_id=221, value=0.5, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "zazen_param_zazen_gain_cur_kp": MotorFlashParam(value_id=222, value=1.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "zazen_param_zazen_gain_cur_ki": MotorFlashParam(value_id=223, value=1.0, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_flash_key": MotorFlashParam(value_id=224, value=23333, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "param_flash_checksum": MotorFlashParam(value_id=225, value=0, min_value=None, max_value=None, value_type="uint16_t", tolerance=0.000001, configurable=False, scope=(-1e999, 1e999), reference_value=None, override=False),
                "stall_pos_err_rad_for_slicer": MotorFlashParam(value_id=226, value=0.016375, min_value=None, max_value=None, value_type="float", tolerance=0.000001, configurable=True, scope=(-1e999, 1e999), reference_value=None, override=False),
            }
        init_system_params()

    def extruder_motor_err_state_query(self):
        if not self.is_ready:
            return
        is_extruder = True
        error_code_flag = False
        warning_code_flag = False
        motor_model = {'1': 'x', '2': 'y', '3': 'z1', '4': 'z2', '5': 'e'}
        num = 5
        data_byte = self.parse.parse_num_to_byte(11)
        convert = self.bytes_to_int
        try:
            addr = convert(Motor_Addr.get('1'))
        finally:
            del convert
        convert = self.bytes_to_int
        try:
            cmd = convert(Motor_cmd.get('protection'))
        finally:
            del convert
        data = self.bytes_to_int(data_byte, is_data_obj=is_extruder)
        data_send = self.data_pack(addr, cmd, data)
        logging.info('data_send:%s', data_send)
        send = self.cmd_transparent_send.send
        try:
            params = send([self.oid, data_send, TIMEOUT_SHORT_TIME_MS])
        finally:
            del send
        respond = self.gcode.respond_info
        try:
            format_args = (str(data_send), str(params))
            message = 'data_send:%s result:%s' % format_args
            del format_args
            try:
                response = respond(message)
            finally:
                del message
        finally:
            del respond
        del response
        result = params.get('read')
        if result is None:
            raise TypeError("'NoneType' object is unsliceable")
        result = result[:-1]
        if result:
            motor_status = result[5:result[2] - 3 + 5]
            binary_str = ''
            for byte_obj in motor_status:
                binary_str += bin(byte_obj)[2:].zfill(8)[::-1]
            error_code = binary_str[:32]
            warning_code = binary_str[32:]
            if '1' in error_code and error_code not in self.motor_error_code[str(num)]:
                for i in range(len(error_code)):
                    if int(error_code[i]):
                        if (self.auto_retry
                                and self.printer.lookup_object('print_stats').state == 'printing'
                                and self.printer.lookup_object('box', None)
                                and self.printer.lookup_object('box').box_action.Tn_action_flag is False
                                and self.printer.lookup_object('filament_rack', None)
                                and self.printer.lookup_object('filament_rack').remain_material.type):
                            pass
                        else:
                            respond = self.gcode._respond_error
                            try:
                                format_args = (motor_error_dict.get(str(i))[0],
                                               motor_error_dict.get(str(i))[2],
                                               motor_model.get(str(num)))
                                message = '{"code":"%s", "msg":"error_code %s", "values":["%s"]}' % format_args
                                del format_args
                                try:
                                    response = respond(message)
                                finally:
                                    del message
                            finally:
                                del respond
                            del response
                        self.printer.send_event('box:handle_motor_extruder_error')
                        logging.info('box:handle_motor_extruder_error start')
                        error_code_flag = True
                self.motor_error_code[str(num)].append(error_code) if error_code_flag else None
            if '1' in warning_code and warning_code not in self.motor_warning_code[str(num)]:
                for i in range(len(warning_code)):
                    if int(warning_code[i]):
                        respond = self.gcode._respond_error
                        try:
                            format_args = (motor_warning_dict.get(str(i))[0],
                                           motor_warning_dict.get(str(i))[2],
                                           motor_model.get(str(num)))
                            message = '{"code":"%s", "msg":"warning_code %s", "values":["%s"]}' % format_args
                            del format_args
                            try:
                                response = respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                        del response
                        warning_code_flag = True
                self.motor_warning_code[str(num)].append(warning_code) if warning_code_flag else None

    def _button_handler_e(self, eventtime, state):
        respond = self.gcode.respond_info
        try:
            message = 'extruder stall state:%s' % state
            try:
                response = respond(message)
            finally:
                del message
        finally:
            del respond
        del response
        if state and self.is_ready:
            self.gcode.respond_info('send_event motor_control:extruder_motor_err_state')
            self.printer.send_event('motor_control:extruder_motor_err_state')
        elif not state and self.is_ready:
            self.gcode.respond_info('send_event motor_control:extruder_motor_err_clear')
            if self.is_ready:
                if (self.auto_retry
                        and self.printer.lookup_object('print_stats').state == 'printing'
                        and self.printer.lookup_object('box', None)
                        and self.printer.lookup_object('box').box_action.Tn_action_flag is False
                        and self.printer.lookup_object('filament_rack', None)
                        and self.printer.lookup_object('filament_rack').remain_material.type):
                    self.gcode.respond_info('send_event motor_control:extruder_motor_retry')
                    register = self.reactor.register_callback
                    try:
                        callback = self.extruder_motor_retry
                        try:
                            result = register(callback)
                        finally:
                            del callback
                    finally:
                        del register
                    del result
                else:
                    while self.wait_auto_retry_end:
                        if self.printer.is_shutdown():
                            break
                        pause = self.reactor.pause
                        try:
                            result = pause(self.reactor.monotonic() + 0.2)
                        finally:
                            del pause
                        del result
                    self.printer.send_event('motor_control:extruder_motor_err_clear')


    def motor_read_pin_io(self, gcmd):
        pin = gcmd.get('PIN')
        toolhead = self.printer.lookup_object('toolhead')
        oid = toolhead.kin.rails[2].steppers[0]._oid
        query = self.mcu.lookup_query_command(
            'query_gpio_status oid=%c gpio_pin=%c',
            'gpio_status oid=%c status=%c', oid=oid)
        result = query.send([oid, pin])
        respond = self.gcode.respond_info
        try:
            response = respond(str(result))
        finally:
            del respond
        del response

    def motor_read_all_pin_io_status(self, gcmd):
        toolhead = self.printer.lookup_object('toolhead')
        oid = toolhead.kin.rails[2].steppers[0]._oid
        query = self.mcu.lookup_query_command(
            'query_gpio_status oid=%c gpio_pin=%c',
            'gpio_status oid=%c status=%c', oid=oid)
        pins = [self.motor_x_dir_info, self.motor_x_step_info,
                self.motor_y_dir_info, self.motor_y_step_info,
                self.motor_z_dir_info, self.motor_z_step_info,
                self.motor_z1_dir_info, self.motor_z1_step_info]
        index = 0
        for pin_info in pins:
            index += 1
            pin = pin_info[0]
            if pin_info[0] == '!PB7':
                pin = 'PB7'
            result = query.send([oid, pin])
            respond = self.gcode.respond_info
            try:
                values = (pin_info[0], str(result))
                text = 'get pin:%s status:%s' % values
                del values
                response = respond(text)
            finally:
                del respond
            del response

    def find_objs(self):
        self.toolhead = self.printer.lookup_object('toolhead')

    def _run_script_stats(self, name, key):
        @self.stats_query_string(name)
        def run(self):
            self.gcode.run_script(key)
        getter = run.__get__
        try:
            return getter(self, self.__class__)
        finally:
            del getter

    def stats(self, eventtime):
        if self.overcurrent_switch and self.motor_params_init:
            id_fdb = self._run_script_stats('motor_e_id_fdb', 'MOTOR_GET NUM=1 DATA=7 EXTRUDER=1')()
            iq_fdb = self._run_script_stats('motor_e_iq_fdb', 'MOTOR_GET NUM=1 DATA=8 EXTRUDER=1')()
            hypot = math.hypot
            try:
                overcurrent = hypot(float(id_fdb.split('=')[1]), float(iq_fdb.split('=')[1]))
            finally:
                del hypot
            return False, f'motor_control: {id_fdb} {iq_fdb} overcurrent={overcurrent}'
        return True, ''

    def motor_extruder_clear_err_warn_code(self):
        num = 1
        convert = self.bytes_to_int
        try:
            addr = convert(Motor_Addr.get(str(num)))
        finally:
            del convert
        convert = self.bytes_to_int
        try:
            cmd = convert(Motor_cmd.get('protection'))
        finally:
            del convert
        data = self.bytes_to_int(b'\x05', is_data_obj=True)
        data_send = self.data_pack(addr, cmd, data)
        try:
            logging.info('data_send:%s', data_send)
            send = self.cmd_transparent_send.send
            try:
                params = send([self.oid, data_send, TIMEOUT_SHORT_TIME_MS])
            finally:
                del send
            respond = self.gcode.respond_info
            try:
                values = (str(data_send), str(params))
                try:
                    respond('data_send:%s result:%s' % values)
                finally:
                    del values
            finally:
                del respond
            if params.get('read'):
                result = params.get('read')[:-1]
                motor_status = result[5:result[2] - 3 + 5]
                respond = self.gcode.respond_info
                try:
                    respond('get recv:%s' % motor_status)
                finally:
                    del respond
        except Exception as err:
            logging.exception(err)
        return True

    def motor_clear_err_warn_code(self, gcmd):
        if not self.validate_num_params(gcmd):
            return
        if not self.validate_data_params(gcmd):
            return
        num = gcmd.get_int('NUM', 0, minval=0, maxval=5)
        data = gcmd.get_int('DATA', None, minval=1, maxval=10)
        cmd = Motor_cmd.get('protection')
        data_byte = self.parse.parse_num_to_byte(data)
        if num == 0:
            objs = MOTOR_NUM
        else:
            objs = [num]
        num = '1'
        for obj in self.motor_closed_loop:
            if obj == 'x':
                num = '1'
            elif obj == 'y':
                num = '2'
            elif obj == 'z':
                num = '3'
            elif obj == 'z1':
                num = '4'
            elif obj == 'e':
                continue
            if int(num) not in objs:
                continue
            addr = Motor_Addr.get(str(num))
            self.box_action.motor_send_data(addr, cmd, 0.05,
                                            data=data_byte, retries_en=False)
            pause = self.reactor.pause
            try:
                pause(self.reactor.monotonic() + 0.05)
            finally:
                del pause
            self.motor_error_code[str(num)] = []
            self.motor_warning_code[str(num)] = []
        if gcmd.get_int('NUM', 0, minval=0, maxval=5) in (0, 5):
            try:
                self.motor_extruder_clear_err_warn_code()
                self.motor_error_code['5'] = []
                self.motor_warning_code['5'] = []
            except Exception as err:
                logging.error(err)

    def motor_sys_param_init(self, num, data, id, params, id_params_type):
        get_addr = Motor_Addr.get
        try:
            addr = get_addr(str(num))
        finally:
            del get_addr
        cmd = Motor_cmd.get('sys_param')
        data_byte = self.parse.parse_num_to_byte(data)
        if data == 3:
            result = self.box_action.motor_send_data(
                addr, cmd, 0.05, data=data_byte, retries_en=False)
            if result:
                motor_status = result[5:result[2] - 3 + 5]
                if motor_status[0] == 0:
                    respond = self.gcode.respond_info
                    try:
                        respond('motor_sys_param update:%s fail' % motor_status)
                    finally:
                        del respond
                if motor_status[0] == 1:
                    respond = self.gcode.respond_info
                    try:
                        respond('motor_sys_param update:%s success' % motor_status)
                    finally:
                        del respond
        else:
            id_byte = self.parse.parse_num_to_byte(id)
            data_byte += id_byte
            if id_params_type == 'float':
                pack = struct.pack
                try:
                    float_bytes = pack('f', float(params))
                finally:
                    del pack
                data_byte += float_bytes
            elif id_params_type == 'uint16_t':
                pack = struct.pack
                try:
                    uint16_t_bytes = pack('<H', int(params))
                finally:
                    del pack
                data_byte += uint16_t_bytes
            elif id_params_type == 'uint32_t':
                pack = struct.pack
                try:
                    uint32_t_bytes = pack('>I', int(params))
                finally:
                    del pack
                data_byte += uint32_t_bytes
            elif id_params_type == 'int16_t':
                pack = struct.pack
                try:
                    int16_t_bytes = pack('<h', int(params))
                finally:
                    del pack
                data_byte += int16_t_bytes
            elif id_params_type == 'int32_t':
                pack = struct.pack
                try:
                    int32_t_bytes = pack('>I', int(params))
                finally:
                    del pack
                data_byte += int32_t_bytes
            result = self.box_action.motor_send_data(
                addr, cmd, 2, data=data_byte, retries_en=False)
            value = self.result_to_value(result, id_params_type)
            return value

    def motor_extruder_sys_param_init(self, num, data, id, params, id_params_type):
        convert = self.bytes_to_int
        try:
            addr = convert(Motor_Addr.get(str(num)))
        finally:
            del convert
        convert = self.bytes_to_int
        try:
            cmd = convert(Motor_cmd.get('sys_param'))
        finally:
            del convert
        data_list = []
        data_list.append(data)
        if data == 3:
            result = None
            pack = self.data_pack
            try:
                data_send = pack(addr, cmd, self.bytes_to_int(b'\x03', is_data_obj=True))
            finally:
                del pack
            send = self.cmd_transparent_send.send
            try:
                recv = send([self.oid, data_send, 50])
            finally:
                del send
            if recv.get('read'):
                result = recv.get('read')[:-1]
            respond = self.gcode.respond_info
            try:
                values = (str(data_send), str(result))
                try:
                    respond('e data_send:%s recv_result:%s' % values)
                finally:
                    del values
            finally:
                del respond
            if result:
                motor_status = result[5:result[2] - 3 + 5]
                if motor_status[0] == 0:
                    respond = self.gcode.respond_info
                    try:
                        respond('motor_sys_param update:%s fail' % motor_status)
                    finally:
                        del respond
                if motor_status[0] == 1:
                    respond = self.gcode.respond_info
                    try:
                        respond('motor_sys_param update:%s success' % motor_status)
                    finally:
                        del respond
        else:
            if id > -1:
                data_list.append(id)
            if id_params_type == 'float':
                data_list.append(float(params))
            elif id_params_type == 'uint16_t':
                data_list.append(int(params))
            elif id_params_type == 'uint32_t':
                data_list.append(int(params))
            elif id_params_type == 'int16_t':
                data_list.append(int(params))
            elif id_params_type == 'int32_t':
                data_list.append(int(params))
            data_send = self.data_pack_extruder(addr, cmd, data_list,
                                               id_params_type=id_params_type)
            timeout = 2
            send = self.cmd_transparent_send.send
            try:
                recv = send([self.oid, data_send, timeout * 1000])
            finally:
                del send
            result = None
            if recv.get('read'):
                result = recv.get('read')[:-1]
            respond = self.gcode.respond_info
            try:
                values = (str(data_send), str(result))
                try:
                    respond('e data_send:%s recv_result:%s' % values)
                finally:
                    del values
            finally:
                del respond
            value = self.result_to_value(result, id_params_type)
            return value

    def check_and_retry(self, num, data, id, params, id_params_type, extruder=False):
        motor_names = {'1': 'x', '2': 'y', '3': 'z1', '4': 'z2', '5': 'e'}
        motor_name = motor_names.get(str(num))
        retry_count = 3
        while retry_count > 0:
            message = 'e motor ' if extruder == True else '%s motor ' % motor_name
            if extruder == True:
                result = self.motor_extruder_sys_param_init(num, data, id, params, id_params_type)
            else:
                result = self.motor_sys_param_init(num, data, id, params, id_params_type)
            if result is not None:
                if id_params_type == 'float' and round(params, 7) == round(result, 7):
                    return
                elif result == params:
                    return
                else:
                    values = (id, params, result)
                    message += 'ID:%s setting failed, target value:%s and read return result value:%s are not equal' % values
                    del values
                    self.gcode._respond_error(message)
            else:
                message += 'ID:%s setting failed, Read null value, resending parameters for re verification' % id
                self.gcode._respond_error(message)
            retry_count -= 1
        if extruder == True:
            motor_name = 'e'
        if retry_count == 0:
            respond = self.gcode._respond_error
            try:
                values = (motor_name, id, motor_name)
                try:
                    text = '{"code":"key803", "msg":"%s motor parameter ID:%s setting failed, exceeding the maximum retry count", "values":["%s"]}' % values
                finally:
                    del values
                try:
                    response = respond(text)
                finally:
                    del text
            finally:
                del respond
            del response
            pause = self.reactor.pause
            try:
                pause(self.reactor.monotonic() + 5)
            finally:
                del pause
            raise

    def set_motor_pin_normal(self, toolhead):
        for obj in self.motor_closed_loop:
            if obj == 'x':
                x_dir_value = 1 if self.motor_x_dir_info[0].startswith('!') and int(self.motor_x_dir_info[1]) == 0 else 0
                x_step_value = 1 if self.motor_x_step_info[0].startswith('!') and int(self.motor_x_step_info[1]) == 0 else 0
                register = toolhead.register_lookahead_callback
                try:
                    callback = lambda print_time: self.motor_x_dir.set_digital(print_time, x_dir_value)
                    try:
                        callback.__name__ = "lambda12"
                        callback.__qualname__ = "Motor_Control.set_motor_pin_normal.<locals>.<lambda>"
                        result = register(callback)
                    finally:
                        del callback
                finally:
                    del register
                del result
                register = toolhead.register_lookahead_callback
                try:
                    callback = lambda print_time: self.motor_x_step.set_digital(print_time, x_step_value)
                    try:
                        callback.__name__ = "lambda13"
                        callback.__qualname__ = "Motor_Control.set_motor_pin_normal.<locals>.<lambda>"
                        result = register(callback)
                    finally:
                        del callback
                finally:
                    del register
                del result
            elif obj == 'y':
                y_dir_value = 1 if self.motor_y_dir_info[0].startswith('!') and int(self.motor_y_dir_info[1]) == 0 else 0
                y_step_value = 1 if self.motor_y_step_info[0].startswith('!') and int(self.motor_y_step_info[1]) == 0 else 0
                register = toolhead.register_lookahead_callback
                try:
                    callback = lambda print_time: self.motor_y_dir.set_digital(print_time, y_dir_value)
                    try:
                        callback.__name__ = "lambda14"
                        callback.__qualname__ = "Motor_Control.set_motor_pin_normal.<locals>.<lambda>"
                        result = register(callback)
                    finally:
                        del callback
                finally:
                    del register
                del result
                register = toolhead.register_lookahead_callback
                try:
                    callback = lambda print_time: self.motor_y_step.set_digital(print_time, y_step_value)
                    try:
                        callback.__name__ = "lambda15"
                        callback.__qualname__ = "Motor_Control.set_motor_pin_normal.<locals>.<lambda>"
                        result = register(callback)
                    finally:
                        del callback
                finally:
                    del register
                del result
            elif obj == 'z':
                z_dir_value = 1 if self.motor_z_dir_info[0].startswith('!') and int(self.motor_z_dir_info[1]) == 0 else 0
                z_step_value = 1 if self.motor_z_step_info[0].startswith('!') and int(self.motor_z_step_info[1]) == 0 else 0
                register = toolhead.register_lookahead_callback
                try:
                    callback = lambda print_time: self.motor_z_dir.set_digital(print_time, z_dir_value)
                    try:
                        callback.__name__ = "lambda16"
                        callback.__qualname__ = "Motor_Control.set_motor_pin_normal.<locals>.<lambda>"
                        result = register(callback)
                    finally:
                        del callback
                finally:
                    del register
                del result
                register = toolhead.register_lookahead_callback
                try:
                    callback = lambda print_time: self.motor_z_step.set_digital(print_time, z_step_value)
                    try:
                        callback.__name__ = "lambda17"
                        callback.__qualname__ = "Motor_Control.set_motor_pin_normal.<locals>.<lambda>"
                        result = register(callback)
                    finally:
                        del callback
                finally:
                    del register
                del result
            elif obj == 'z1':
                z1_dir_value = 1 if self.motor_z1_dir_info[0].startswith('!') and int(self.motor_z1_dir_info[1]) == 0 else 0
                z1_step_value = 1 if self.motor_z1_step_info[0].startswith('!') and int(self.motor_z1_step_info[1]) == 0 else 0
                register = toolhead.register_lookahead_callback
                try:
                    callback = lambda print_time: self.motor_z1_dir.set_digital(print_time, z1_dir_value)
                    try:
                        callback.__name__ = "lambda18"
                        callback.__qualname__ = "Motor_Control.set_motor_pin_normal.<locals>.<lambda>"
                        result = register(callback)
                    finally:
                        del callback
                finally:
                    del register
                del result
                register = toolhead.register_lookahead_callback
                try:
                    callback = lambda print_time: self.motor_z1_step.set_digital(print_time, z1_step_value)
                    try:
                        callback.__name__ = "lambda19"
                        callback.__qualname__ = "Motor_Control.set_motor_pin_normal.<locals>.<lambda>"
                        result = register(callback)
                    finally:
                        del callback
                finally:
                    del register
                del result

    def set_motor_pin_dir(self, toolhead):
        for obj in self.motor_closed_loop:
            if obj == 'x':
                register = toolhead.register_lookahead_callback
                try:
                    def callback(print_time):
                        set_digital = self.motor_x_dir.set_digital
                        try:
                            return set_digital(print_time, int(self.motor_x_dir_info[1]))
                        finally:
                            del set_digital
                    try:
                        callback.__name__ = "lambda4"
                        callback.__qualname__ = "Motor_Control.set_motor_pin_dir.<locals>.<lambda>"
                        result = register(callback)
                    finally:
                        del callback
                finally:
                    del register
                del result
                register = toolhead.register_lookahead_callback
                try:
                    def callback(print_time):
                        set_digital = self.motor_x_step.set_digital
                        try:
                            return set_digital(print_time, int(self.motor_x_step_info[1]))
                        finally:
                            del set_digital
                    try:
                        callback.__name__ = "lambda5"
                        callback.__qualname__ = "Motor_Control.set_motor_pin_dir.<locals>.<lambda>"
                        result = register(callback)
                    finally:
                        del callback
                finally:
                    del register
                del result
            elif obj == 'y':
                register = toolhead.register_lookahead_callback
                try:
                    def callback(print_time):
                        set_digital = self.motor_y_dir.set_digital
                        try:
                            return set_digital(print_time, int(self.motor_y_dir_info[1]))
                        finally:
                            del set_digital
                    try:
                        callback.__name__ = "lambda6"
                        callback.__qualname__ = "Motor_Control.set_motor_pin_dir.<locals>.<lambda>"
                        result = register(callback)
                    finally:
                        del callback
                finally:
                    del register
                del result
                register = toolhead.register_lookahead_callback
                try:
                    def callback(print_time):
                        set_digital = self.motor_y_step.set_digital
                        try:
                            return set_digital(print_time, int(self.motor_y_step_info[1]))
                        finally:
                            del set_digital
                    try:
                        callback.__name__ = "lambda7"
                        callback.__qualname__ = "Motor_Control.set_motor_pin_dir.<locals>.<lambda>"
                        result = register(callback)
                    finally:
                        del callback
                finally:
                    del register
                del result
            elif obj == 'z':
                register = toolhead.register_lookahead_callback
                try:
                    def callback(print_time):
                        set_digital = self.motor_z_dir.set_digital
                        try:
                            return set_digital(print_time, int(self.motor_z_dir_info[1]))
                        finally:
                            del set_digital
                    try:
                        callback.__name__ = "lambda8"
                        callback.__qualname__ = "Motor_Control.set_motor_pin_dir.<locals>.<lambda>"
                        result = register(callback)
                    finally:
                        del callback
                finally:
                    del register
                del result
                register = toolhead.register_lookahead_callback
                try:
                    def callback(print_time):
                        set_digital = self.motor_z_step.set_digital
                        try:
                            return set_digital(print_time, int(self.motor_z_step_info[1]))
                        finally:
                            del set_digital
                    try:
                        callback.__name__ = "lambda9"
                        callback.__qualname__ = "Motor_Control.set_motor_pin_dir.<locals>.<lambda>"
                        result = register(callback)
                    finally:
                        del callback
                finally:
                    del register
                del result
            elif obj == 'z1':
                register = toolhead.register_lookahead_callback
                try:
                    def callback(print_time):
                        set_digital = self.motor_z1_dir.set_digital
                        try:
                            return set_digital(print_time, int(self.motor_z1_dir_info[1]))
                        finally:
                            del set_digital
                    try:
                        callback.__name__ = "lambda10"
                        callback.__qualname__ = "Motor_Control.set_motor_pin_dir.<locals>.<lambda>"
                        result = register(callback)
                    finally:
                        del callback
                finally:
                    del register
                del result
                register = toolhead.register_lookahead_callback
                try:
                    def callback(print_time):
                        set_digital = self.motor_z1_step.set_digital
                        try:
                            return set_digital(print_time, int(self.motor_z1_step_info[1]))
                        finally:
                            del set_digital
                    try:
                        callback.__name__ = "lambda11"
                        callback.__qualname__ = "Motor_Control.set_motor_pin_dir.<locals>.<lambda>"
                        result = register(callback)
                    finally:
                        del callback
                finally:
                    del register
                del result

    def motor_version(self, gcmd):
        if not self.validate_num_params(gcmd):
            return
        if not self.validate_data_params(gcmd):
            return
        if self.motor_extruder_version(gcmd):
            return
        num = gcmd.get_int('NUM', None, minval=1, maxval=10)
        data = gcmd.get_int('DATA', None, minval=0, maxval=10)
        if num:
            get_addr = Motor_Addr.get
            try:
                addr = get_addr(str(num))
            finally:
                del get_addr
            cmd = Motor_cmd.get('version')
            data_send = self.parse.parse_num_to_byte(0)
            result = self.box_action.motor_send_data(addr, cmd, TIMEOUT_MAX, data=data_send, retries_en=False)
            if result:
                versions = {'fireware_ver': '', 'cmd_system_ver': '', 'protection_ver': '', 'flash_param_ver': '', 'logger_ver': ''}
                read = result[5:result[2] - 3 + 5]
                values = (read[0], read[1], read[2], read[3])
                versions['fireware_ver'] = '%s.%s.%s.%s' % values
                del values
                values = (read[4], read[5])
                versions['cmd_system_ver'] = '%s.%s' % values
                del values
                values = (read[6], read[7])
                versions['protection_ver'] = '%s.%s' % values
                del values
                values = (read[8], read[9])
                versions['flash_param_ver'] = '%s.%s' % values
                del values
                values = (read[10], read[11])
                versions['logger_ver'] = '%s.%s' % values
                del values
                selected = {}
                if data == 0:
                    selected = versions
                elif data == 1:
                    selected['fireware_ver'] = versions['fireware_ver']
                elif data == 2:
                    selected['cmd_system_ver'] = versions['cmd_system_ver']
                elif data == 3:
                    selected['protection_ver'] = versions['protection_ver']
                elif data == 4:
                    selected['flash_param_ver'] = versions['flash_param_ver']
                elif data == 5:
                    selected['logger_ver'] = versions['logger_ver']
                respond = self.gcode.respond_info
                try:
                    values = (data_send, read, str(selected))
                    try:
                        text = '\u83b7\u53d6\u7248\u672c\u53f7\u6570\u636e%s: recv:%s result:%s' % values
                    finally:
                        del values
                    respond(text)
                finally:
                    del respond

    def verify_sys_param(self, _=None):
        def parsing_response(value_, result_value_bytes_):
            result_value_ = None
            if value_.value_type == 'float':
                if len(result_value_bytes_) == 4:
                    result_value_ = struct.unpack('f', result_value_bytes_)[0]
            elif value_.value_type == 'uint16_t':
                if len(result_value_bytes_) == 2:
                    result_value_ = struct.unpack('<H', result_value_bytes_)[0]
            elif value_.value_type == 'uint32_t':
                if len(result_value_bytes_) == 4:
                    result_value_ = struct.unpack('>I', result_value_bytes_)[0]
            elif value_.value_type == 'int16_t':
                if len(result_value_bytes_) == 2:
                    result_value_ = struct.unpack('<h', result_value_bytes_)[0]
            elif value_.value_type == 'int32_t':
                if len(result_value_bytes_) == 4:
                    result_value_ = struct.unpack('>I', result_value_bytes_)[0]
            return result_value_

        motor_nums = [MOTOR_NAME_TO_NUM[name] for name in MOTOR_NAME_TO_NUM
                      if getattr(self, f'motor_{name}_stall')]
        for num in motor_nums:
            log = logging.info
            try:
                log(f'update_flash_param stepper:{num}')
            finally:
                del log
            for key, value in self.motor_params_table[MOTOR_NUM_TO_NAME[num]].items():
                if not value.configurable:
                    log = logging.info
                    try:
                        log(f'update_flash_param key:{key} value:{value.value_id} is not configurable')
                    finally:
                        del log
                    continue
                if not value.override:
                    log = logging.info
                    try:
                        log(f'update_flash_param key:{key} value:{value.value_id} is not need override')
                    finally:
                        del log
                    continue
                result = None
                for retry in range(3):
                    send = self._motor_sys_param
                    try:
                        parse = self.parse.parse_num_to_byte
                        try:
                            id_byte = parse(value.value_id)
                        finally:
                            del parse
                        try:
                            result = send(num=num, data=2, id_byte=id_byte,
                                          id_params_type=value.value_type)
                        finally:
                            del id_byte
                    finally:
                        del send
                    if result:
                        break
                if not result:
                    log = logging.warning
                    try:
                        log(f'update_flash_param key:{key} value:{value.value_id} result is None')
                    finally:
                        del log
                    continue
                result_value_bytes = result[5:result[2] - 3 + 5]
                if not result_value_bytes:
                    log = logging.warning
                    try:
                        log(f'update_flash_param key:{key} value:{value.value_id} result_value_bytes is None')
                    finally:
                        del log
                    continue
                result_value = parsing_response(value, result_value_bytes)
                if result_value is None:
                    log = logging.warning
                    try:
                        log(f'update_flash_param key:{key} value:{value.value_id} result_value is None')
                    finally:
                        del log
                    continue
                identifier = value.value_id
                try:
                    if identifier == 9 or identifier == 10 or identifier == 25:
                        read_only = True
                    else:
                        read_only = False
                finally:
                    del identifier
                if read_only:
                    log = logging.info
                    try:
                        log(f'stepper:{num} key:{key} precision:{value.tolerance}  value:{value.value_id} result_value:{result_value} read success')
                    finally:
                        del log
                    continue
                isclose = math.isclose
                try:
                    matches = isclose(result_value, value.value, abs_tol=value.tolerance)
                finally:
                    del isclose
                if not matches:
                    log = logging.info
                    try:
                        log(f'stepper:{num} key:{key} precision:{value.tolerance}  value:{value.value_id} result_value:{result_value} is not close to configured value:{value.value}')
                    finally:
                        del log
                    result = None
                    for retry in range(3):
                        send = self._motor_sys_param
                        try:
                            parse = self.parse.parse_num_to_byte
                            try:
                                id_byte = parse(value.value_id)
                            finally:
                                del parse
                            try:
                                result = send(num=num, data=1, id_byte=id_byte,
                                              id_params_type=value.value_type,
                                              params=value.value)
                            finally:
                                del id_byte
                        finally:
                            del send
                        if result:
                            result_value = parsing_response(value, result[5:result[2] - 3 + 5])
                            log = logging.info
                            try:
                                log(f'update_flash_param key:{key} value:{value.value_id} result_value:{result_value} write success')
                            finally:
                                del log
                            break
                    if not result:
                        log = logging.warning
                        try:
                            log(f'update_flash_param key:{key} value:{value.value_id} result_value:{result_value} write failed')
                        finally:
                            del log
            logging.info('Updata param')
            self._motor_sys_param(num=num, data=3)

    def motor_elec_offset_calibrate(self, gcmd):
        if not self.validate_num_params(gcmd):
            return
        if self.motor_extruder_elec_offset_calibrate(gcmd):
            return
        num = gcmd.get_int('NUM', None, minval=1, maxval=10)
        if num:
            lookup = Motor_Addr.get
            try:
                key = str(num)
                try:
                    addr = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            cmd = Motor_cmd.get('elec_offset_calibrate')
            data = b'\x00'
            result = self.box_action.motor_send_data(addr, cmd, 60, data=data, retries_en=False)
            if result:
                state = ''
                status = result[5:result[2] - 3 + 5]
                first_byte = status[0]
                if first_byte == 0:
                    state = '\u6821\u51c6\u5931\u8d25'
                elif first_byte == 1:
                    state = '\u6821\u51c6\u6210\u529f'
                else:
                    state = '\u672a\u77e5\u7684\u72b6\u6001\u8fd4\u56de\u503c'
                bytes_2_5 = status[1:5]
                deg = struct.unpack('<f', bytes_2_5)[0]
                bytes_6_9 = status[5:9]
                pulse = struct.unpack('<f', bytes_6_9)[0]
                respond = self.gcode.respond_info
                try:
                    arguments = (state, deg, pulse)
                    try:
                        message = '\u7535\u96f6\u4f4d\u4e00\u952e\u6821\u51c6 status:%s, \u6821\u51c6\u7ed3\u679c\u7684\u6700\u5927\u504f\u5dee:%s\u00b0, \u6821\u51c6\u7ed3\u679c\u5747\u503c:%spulse' % arguments
                    finally:
                        del arguments
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond

    def motor_elec_offset_calibrate_all(self, gcmd):
        self.gcode.run_script_from_command('MOTOR_ELEC_OFFSET_CALIBRATE NUM=1')
        self.gcode.run_script_from_command('MOTOR_ELEC_OFFSET_CALIBRATE NUM=2')
        self.gcode.run_script_from_command('MOTOR_ELEC_OFFSET_CALIBRATE NUM=3')
        self.gcode.run_script_from_command('MOTOR_ELEC_OFFSET_CALIBRATE NUM=4')

    def motor_encoder_calibrate_official(self, gcmd):
        if not self.validate_num_params(gcmd):
            return
        if self.motor_extruder_encoder_calibrate_official(gcmd):
            return
        num = gcmd.get_int('NUM', None, minval=1, maxval=10)
        if num:
            lookup = Motor_Addr.get
            try:
                key = str(num)
                try:
                    addr = lookup(key)
                finally:
                    del key
            finally:
                del lookup
            cmd = Motor_cmd.get('encoder_calibrate_official')
            data = b'\x00'
            result = self.box_action.motor_send_data(addr, cmd, 60, data=data, retries_en=False)
            if result:
                state = ''
                status = result[5:result[2] - 3 + 5]
                first_byte = status[0]
                if first_byte == 12:
                    state = '\u6821\u51c6\u6210\u529f'
                elif first_byte == 0:
                    state = '\u6ca1\u6709\u8fd0\u884c\u6821\u51c6'
                elif first_byte == 4:
                    state = '\u6b63\u5728\u8fdb\u884c\u81ea\u6821\u51c6'
                elif first_byte == 8:
                    state = '\u8fd0\u884c\u6821\u51c6\u5931\u8d25'
                else:
                    state = '\u672a\u77e5\u7684\u72b6\u6001\u8fd4\u56de\u503c'
                respond = self.gcode.respond_info
                try:
                    message = '\u7f16\u7801\u5668\u78c1\u573a\u4e00\u952e\u6821\u51c6 status:%s' % state
                    try:
                        respond(message)
                    finally:
                        del message
                finally:
                    del respond

    def set_motor_pin(self):
        from extras.k2_transport import TransparentQuery
        self.cmd_transparent_send = TransparentQuery(self.nozzle_mcu,
            'transparent_send oid=%c write=%*s timeout_ms=%u',
            'transparent_response oid=%c read=%*s',
            oid=self.oid, cq=None)
        retry_count = 1
        toolhead = self.printer.lookup_object('toolhead')
        self.set_motor_pin_dir(toolhead)
        self.gcode.run_script_from_command('M400')
        self.check_motor_pin_io()
        self.gcode.respond_info('initial set_pin_io done')
        failed_motors = []
        num_to_name = {'1': 'x', '2': 'y', '3': 'z1', '4': 'z2', '5': 'e'}
        name_to_num = {'x': 1, 'y': 2, 'z': 3, 'z1': 4, 'e': 1}
        try:
            if self.switch == 0:
                for _ in range(3):
                    self.set_motor_pin_normal(toolhead)
                    pause = self.reactor.pause
                    try:
                        pause(self.reactor.monotonic() + 0.1)
                    finally:
                        del pause
                self.gcode.respond_info('switch=0 set_pin_io 0')
                self.gcode.run_script_from_command('M400')
                return None
            pause = self.reactor.pause
            try:
                pause(self.reactor.monotonic() + 0.2)
            finally:
                del pause
            while retry_count <= self.retries:
                failed_motors = []
                gcmd = self.gcode.create_gcode_command(
                    'MOTOR_SET_ADDR', 'MOTOR_SET_ADDR', {'NUM': 9})
                gcmd_e = self.gcode.create_gcode_command(
                    'MOTOR_SET_ADDR', 'MOTOR_SET_ADDR', {'NUM': 9, 'EXTRUDER': True})
                self.motor_set485_addr(gcmd)
                self.motor_set485_addr(gcmd_e)
                self.gcode.respond_info('broadcast retries:%s' % retry_count)
                pause = self.reactor.pause
                try:
                    pause(self.reactor.monotonic() + 0.1)
                finally:
                    del pause
                success_count = 0
                for obj in self.motor_closed_loop:
                    if obj != 'e':
                        num = name_to_num.get(obj, 0)
                        pause = self.reactor.pause
                        try:
                            pause(self.reactor.monotonic() + 0.05)
                        finally:
                            del pause
                        read_addr = self.motor_read485_addr
                        try:
                            result = read_addr(self.gcode.create_gcode_command(
                                'MOTOR_READ_ADDR', 'MOTOR_READ_ADDR', {'NUM': num}))
                        finally:
                            del read_addr
                        if result:
                            success_count += 1
                        elif num_to_name.get(str(num), None) not in failed_motors:
                            failed_motors.append(num_to_name.get(str(num), None))
                    else:
                        read_addr = self.motor_read485_addr
                        try:
                            result = read_addr(self.gcode.create_gcode_command(
                                'MOTOR_READ_ADDR', 'MOTOR_READ_ADDR', {'NUM': 1, 'EXTRUDER': True}))
                        finally:
                            del read_addr
                        if result:
                            success_count += 1
                        elif 'e' not in failed_motors:
                            failed_motors.append('e')
                if success_count == len(self.motor_closed_loop):
                    failed_motors = []
                    self.motor_params_init = True
                    self.gcode.respond_info('All motors are normal')
                    self.set_z_stall_mode()
                    if 'e' in self.motor_closed_loop:
                        self.set_e_stall_mode()
                    self.gcode.run_script_from_command('K_QUERY_MOTORS')
                    break
                pause = self.reactor.pause
                try:
                    pause(self.reactor.monotonic() + 1.0)
                finally:
                    del pause
                retry_count += 1
        except Exception as error:
            logging.error(error)
        for _ in range(3):
            self.set_motor_pin_normal(toolhead)
            pause = self.reactor.pause
            try:
                pause(self.reactor.monotonic() + 0.1)
            finally:
                del pause
        self.gcode.run_script_from_command('M400')
        self.check_motor_pin_io_restore()
        self.gcode.respond_info('restore set_pin_io done')
        pause = self.reactor.pause
        try:
            pause(self.reactor.monotonic() + 1.0)
        finally:
            del pause
        try:
            from extras.motor_params_config import objs
            for obj in self.motor_closed_loop:
                objs.get(obj, None)
        except Exception:
            pass
        # The native code evaluates this condition but does not branch on it.
        if retry_count != self.retries + 1:
            pass
        self.verify_sys_param()
        self.is_ready = True
        self.printer.send_event('auto_addr:start')
        if retry_count == self.retries + 1:
            self.is_ready = False
            self.gcode._respond_error(
                '{"code":"key798", "msg":"Motor connection failed, exceeding maximum retry count", "values":%s}'
                % failed_motors)
            pause = self.reactor.pause
            try:
                pause(self.reactor.monotonic() + 5.0)
            finally:
                del pause
            raise


    def motor_auto_check_protection(self, gcmd):
        if not self.validate_num_params(gcmd):
            return
        if not self.validate_data_params(gcmd):
            return
        num = gcmd.get_int('NUM', None, minval=1, maxval=10)
        motor_model = {'1': 'x', '2': 'y', '3': 'z1', '4': 'z2', '5': 'e'}
        data = gcmd.get_int('DATA', None, minval=1, maxval=12)
        addr = Motor_Addr.get(str(num))
        cmd = Motor_cmd.get('protection')
        tries = 3
        result = None
        error_code_flag = False
        warning_code_flag = False
        while tries > 0:
            tries -= 1
            data_byte = self.parse.parse_num_to_byte(data)
            if gcmd.get('EXTRUDER', ''):
                convert = self.bytes_to_int
                try:
                    addr = convert(Motor_Addr.get(str(num)))
                finally:
                    del convert
                convert = self.bytes_to_int
                try:
                    cmd = convert(Motor_cmd.get('protection'))
                finally:
                    del convert
                data_obj = self.bytes_to_int(data_byte, is_data_obj=True)
                data_send = self.data_pack(addr, cmd, data_obj)
                send = self.cmd_transparent_send.send
                try:
                    params = send([self.oid, data_send, TIMEOUT_SHORT_TIME_MS])
                finally:
                    del send
                respond = self.gcode.respond_info
                try:
                    format_args = (str(data_send), str(params))
                    message = 'data_send:%s result:%s' % format_args
                    del format_args
                    try:
                        response = respond(message)
                    finally:
                        del message
                finally:
                    del respond
                del response
                num = 5
                result = None
                if params.get('read'):
                    result = params.get('read')[:-1]
            else:
                result = self.box_action.motor_send_data(addr, cmd, TIMEOUT_MAX,
                                                        data=data_byte, retries_en=False)
            if result:
                break
        if result:
            motor_status = result[5:result[2] - 3 + 5]
            binary_str = ''
            for byte_obj in motor_status:
                binary_str += bin(byte_obj)[2:].zfill(8)[::-1]
            error_code = binary_str[:32]
            warning_code = binary_str[32:]
            if '1' in error_code and error_code not in self.motor_error_code[str(num)]:
                for i in range(len(error_code)):
                    if int(error_code[i]):
                        respond = self.gcode._respond_error
                        try:
                            format_args = (motor_error_dict.get(str(i))[0],
                                           motor_error_dict.get(str(i))[2],
                                           motor_model.get(str(num)))
                            message = '{"code":"%s", "msg":"error_code %s", "values":["%s"]}' % format_args
                            del format_args
                            try:
                                response = respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                        del response
                        error_code_flag = True
                self.motor_error_code[str(num)].append(error_code) if error_code_flag else None
            if '1' in warning_code and warning_code not in self.motor_warning_code[str(num)]:
                for i in range(len(warning_code)):
                    if int(warning_code[i]):
                        respond = self.gcode._respond_error
                        try:
                            format_args = (motor_warning_dict.get(str(i))[0],
                                           motor_warning_dict.get(str(i))[2],
                                           motor_model.get(str(num)))
                            message = '{"code":"%s", "msg":"warning_code %s", "values":["%s"]}' % format_args
                            del format_args
                            try:
                                response = respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                        del response
                        warning_code_flag = True
                self.motor_warning_code[str(num)].append(warning_code) if warning_code_flag else None

    def motor_check_protection_after_home(self, gcmd):
        if not self.validate_data_params(gcmd):
            return
        motor_model = {'1': 'x', '2': 'y', '3': 'z1', '4': 'z2', '5': 'e'}
        data = gcmd.get_int('DATA', None, minval=1, maxval=12)
        cmd = Motor_cmd.get('protection')
        data_byte = self.parse.parse_num_to_byte(data)
        num = '1'
        error_code_flag = False
        warning_code_flag = False
        for obj in self.motor_closed_loop:
            if obj == 'x':
                num = '1'
            elif obj == 'y':
                num = '2'
            elif obj == 'z':
                num = '3'
            elif obj == 'z1':
                num = '4'
            elif obj == 'e':
                num = '5'
            addr = Motor_Addr.get(num)
            if motor_model.get(num) == 'e':
                convert = self.bytes_to_int
                try:
                    addr = convert(Motor_Addr.get('1'))
                finally:
                    del convert
                convert = self.bytes_to_int
                try:
                    cmd = convert(Motor_cmd.get('protection'))
                finally:
                    del convert
                data_obj = self.bytes_to_int(data_byte, is_data_obj=True)
                data_send = self.data_pack(addr, cmd, data_obj)
                send = self.cmd_transparent_send.send
                try:
                    params = send([self.oid, data_send, TIMEOUT_SHORT_TIME_MS])
                finally:
                    del send
                respond = self.gcode.respond_info
                try:
                    format_args = (str(data_send), str(params))
                    message = 'data_send:%s result:%s' % format_args
                    del format_args
                    try:
                        response = respond(message)
                    finally:
                        del message
                finally:
                    del respond
                del response
                result = None
                if params.get('read'):
                    result = params.get('read')[:-1]
            else:
                result = self.box_action.motor_send_data(addr, cmd, 0.05,
                                                        data=data_byte, retries_en=False)
            if result:
                motor_status = result[5:result[2] - 3 + 5]
                binary_str = ''
                for byte_obj in motor_status:
                    binary_str += bin(byte_obj)[2:].zfill(8)[::-1]
                error_code = binary_str[:32]
                warning_code = binary_str[32:]
                if '1' in error_code and error_code not in self.motor_error_code[str(num)]:
                    for i in range(len(error_code)):
                        if int(error_code[i]):
                            respond = self.gcode._respond_error
                            try:
                                format_args = (motor_error_dict.get(str(i))[0],
                                               motor_error_dict.get(str(i))[2],
                                               motor_model.get(str(num)))
                                message = '{"code":"%s", "msg":"error_code %s", "values":["%s"]}' % format_args
                                del format_args
                                try:
                                    response = respond(message)
                                finally:
                                    del message
                            finally:
                                del respond
                            del response
                            error_code_flag = True
                    self.motor_error_code[str(num)].append(error_code) if error_code_flag else None
                if '1' in warning_code and warning_code not in self.motor_warning_code[str(num)]:
                    for i in range(len(warning_code)):
                        if int(warning_code[i]):
                            respond = self.gcode._respond_error
                            try:
                                format_args = (motor_warning_dict.get(str(i))[0],
                                               motor_warning_dict.get(str(i))[2],
                                               motor_model.get(str(num)))
                                message = '{"code":"%s", "msg":"warning_code %s", "values":["%s"]}' % format_args
                                del format_args
                                try:
                                    response = respond(message)
                                finally:
                                    del message
                            finally:
                                del respond
                            del response
                            warning_code_flag = True
                    self.motor_warning_code[str(num)].append(warning_code) if warning_code_flag else None
        if error_code_flag:
            self.gcode.run_script_from_command('M84')
            self.gcode.run_script_from_command('FORCE_MOVE STEPPER=stepper_y DISTANCE=0.01 VELOCITY=5')
            self.gcode.run_script_from_command('M84')
            self.gcode.respond_info('run_script_from_command M84')
            self.gcode.respond_info('motor_check_protection_after_home MOTOR_CLEAR_ERR_WARN_CODE NUM=0 DATA=5')
            self.gcode.run_script_from_command('MOTOR_CLEAR_ERR_WARN_CODE NUM=0 DATA=5')

    def check_protection_code(self, cur_obj, is_extruder=False):
        print_stats = self.printer.lookup_object('print_stats')
        motor_model = {'1': 'x', '2': 'y', '3': 'z1', '4': 'z2', '5': 'e'}
        if is_extruder:
            self.gcode.respond_info('motor_e 81 check_protection_code')
        else:
            respond = self.gcode.respond_info
            try:
                format_args = (motor_model.get(str(cur_obj)), cur_obj)
                message = 'motor_%s 8%s check_protection_code' % format_args
                del format_args
                try:
                    response = respond(message)
                finally:
                    del message
            finally:
                del respond
            del response
        if self.is_check_cut_pos_start:
            self.cut_state = True
            return
        data = 11
        cmd = Motor_cmd.get('protection')
        data_byte = self.parse.parse_num_to_byte(data)
        num = cur_obj
        addr = Motor_Addr.get(str(num))
        error_code_flag = False
        warning_code_flag = False

        def get_gcmd(num, data, is_extruder=False):
            create = self.gcode.create_gcode_command
            try:
                if is_extruder:
                    return create('MOTOR_CONTROL', 'MOTOR_CONTROL',
                                  {'NUM': num, 'DATA': data, 'EXTRUDER': True})
                return create('MOTOR_CONTROL', 'MOTOR_CONTROL', {'NUM': num, 'DATA': data})
            finally:
                del create

        if str(cur_obj) == '5':
            convert = self.bytes_to_int
            try:
                addr = convert(Motor_Addr.get('1'))
            finally:
                del convert
            convert = self.bytes_to_int
            try:
                cmd = convert(Motor_cmd.get('protection'))
            finally:
                del convert
            data_obj = self.bytes_to_int(data_byte, is_data_obj=True)
            data_send = self.data_pack(addr, cmd, data_obj)
            logging.info('data_send:%s', data_send)
            send = self.cmd_transparent_send.send
            try:
                params = send([self.oid, data_send, TIMEOUT_SHORT_TIME_MS])
            finally:
                del send
            respond = self.gcode.respond_info
            try:
                format_args = (str(data_send), str(params))
                message = 'data_send:%s result:%s' % format_args
                del format_args
                try:
                    response = respond(message)
                finally:
                    del message
            finally:
                del respond
            del response
            result = None
            if params.get('read'):
                result = params.get('read')[:-1]
        else:
            result = self.box_action.motor_send_data(addr, cmd, 0.05,
                                                    data=data_byte, retries_en=False)
        if result:
            motor_status = result[5:result[2] - 3 + 5]
            binary_str = ''
            for byte_obj in motor_status:
                binary_str += bin(byte_obj)[2:].zfill(8)[::-1]
            error_code = binary_str[:32]
            warning_code = binary_str[32:]
            if '1' in error_code and error_code not in self.motor_error_code[str(num)]:
                for i in range(len(error_code)):
                    if int(error_code[i]):
                        respond = self.gcode._respond_error
                        try:
                            format_args = (motor_error_dict.get(str(i))[0],
                                           motor_error_dict.get(str(i))[2],
                                           motor_model.get(str(num)))
                            message = '{"code":"%s", "msg":"error_code %s", "values":["%s"]}' % format_args
                            del format_args
                            try:
                                response = respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                        del response
                        if is_extruder:
                            self.printer.send_event('box:handle_motor_extruder_error')
                            logging.info('box:handle_motor_extruder_error start')
                        error_code_flag = True
                self.motor_error_code[str(num)].append(error_code) if error_code_flag else None
            if '1' in warning_code and warning_code not in self.motor_warning_code[str(num)]:
                for i in range(len(warning_code)):
                    if int(warning_code[i]):
                        respond = self.gcode._respond_error
                        try:
                            format_args = (motor_warning_dict.get(str(i))[0],
                                           motor_warning_dict.get(str(i))[2],
                                           motor_model.get(str(num)))
                            message = '{"code":"%s", "msg":"warning_code %s", "values":["%s"]}' % format_args
                            del format_args
                            try:
                                response = respond(message)
                            finally:
                                del message
                        finally:
                            del respond
                        del response
                        warning_code_flag = True
                self.motor_warning_code[str(num)].append(warning_code) if warning_code_flag else None
            if error_code_flag:
                if not is_extruder:
                    logging.info('motor_control disable start')
                    control = self.motor_control
                    try:
                        command = get_gcmd(1, 1)
                        try:
                            response = control(command)
                        finally:
                            del command
                    finally:
                        del control
                    del response
                    control = self.motor_control
                    try:
                        command = get_gcmd(2, 1)
                        try:
                            response = control(command)
                        finally:
                            del command
                    finally:
                        del control
                    del response
                    control = self.motor_control
                    try:
                        command = get_gcmd(3, 1)
                        try:
                            response = control(command)
                        finally:
                            del command
                    finally:
                        del control
                    del response
                    control = self.motor_control
                    try:
                        command = get_gcmd(4, 1)
                        try:
                            response = control(command)
                        finally:
                            del command
                    finally:
                        del control
                    del response
                    try:
                        control = self.motor_control
                        try:
                            command = get_gcmd(1, 1, True)
                            try:
                                response = control(command)
                            finally:
                                del command
                        finally:
                            del control
                        del response
                    except Exception as err:
                        logging.exception(err)
                    logging.info('motor_control disable end')
                    toolhead = self.printer.lookup_object('toolhead')
                    toolhead._handle_shutdown()
                    pause = self.reactor.pause
                    try:
                        response = pause(self.reactor.monotonic() + 0.2)
                    finally:
                        del pause
                    del response
                    toolhead.can_pause = True
                    self.force_stop()
                    if cur_obj in MOTOR_NUM[2:]:
                        self.gcode.respond_info('run ZDOWN_FORCE_STOP')
                        gcode = self.printer.lookup_object('gcode')
                        gcmd = gcode.create_gcode_command('ZDOWN_FORCE_STOP', 'ZDOWN_FORCE_STOP', {})
                        self.printer.lookup_object('z_align').cmd_ZDOWN_FORCE_STOP(gcmd)
                    try:
                        if print_stats.state == 'printing' and self.printer.lookup_object('k2_print').motor_cancel_print_start == False:
                            self.printer.lookup_object('k2_print').motor_cancel_print_start = True
                            self.gcode.respond_info('check_protection_code start CANCEL_PRINT')
                            self.gcode.run_script_from_command('MOTOR_CANCEL_PRINT')
                            self.gcode.respond_info('check_protection_code end CANCEL_PRINT')
                    except Exception as err:
                        logging.exception(err)
                    if self.printer.lookup_object('k2_print').motor_cancel_print_start == False:
                        self.gcode.respond_info('check_protection_code start M84')
                        self.gcode.run_script_from_command('M84')
                        self.gcode.run_script_from_command('FORCE_MOVE STEPPER=stepper_y DISTANCE=0.01 VELOCITY=5')
                        self.gcode.run_script_from_command('M84')
                        self.gcode.run_script_from_command('M400')
                        self.gcode.respond_info('check_protection_code end M84')
                    self.gcode.respond_info('run MOTOR_CLEAR_ERR_WARN_CODE NUM=0 DATA=5')
                    self.gcode.run_script_from_command('MOTOR_CLEAR_ERR_WARN_CODE NUM=0 DATA=5')
                elif is_extruder:
                    self.gcode.run_script_from_command('SET_STEPPER_ENABLE STEPPER=extruder ENABLE=0')
                    self.gcode.respond_info('run SET_STEPPER_ENABLE STEPPER=extruder ENABLE=0')
                    self.gcode.respond_info('run MOTOR_CLEAR_ERR_WARN_CODE NUM=0 DATA=5')
                    self.gcode.run_script_from_command('MOTOR_CLEAR_ERR_WARN_CODE NUM=0 DATA=5')
                    pause = self.reactor.pause
                    try:
                        response = pause(self.reactor.monotonic() + 0.2)
                    finally:
                        del pause
                    del response
                    log = logging.info
                    try:
                        message = 'motor_control print_stats.state:%s' % print_stats.state
                        try:
                            response = log(message)
                        finally:
                            del message
                    finally:
                        del log
                    del response
                    if print_stats.state == 'printing' and self.printer.lookup_object('k2_print').pause_start == False:
                        logging.info('motor_control start PAUSE...')
                        self.gcode.run_script_from_command('PAUSE')
                        logging.info('motor_control PAUSE END')
                        self.gcode.run_script_from_command('M400')
                        logging.info('motor_control end PAUSE...')

    def check_motor_pin_io(self):
        toolhead = self.printer.lookup_object('toolhead')
        oid = toolhead.kin.rails[2].steppers[0]._oid
        query = self.mcu.lookup_query_command(
            'query_gpio_status oid=%c gpio_pin=%c',
            'gpio_status oid=%c status=%c', oid=oid)

        def resend_set_io(obj_info, dir_step_obj, value):
            retries = 3
            pin_obj = obj_info[0]
            if pin_obj.startswith('!'):
                pin_obj = pin_obj[1:]
            while retries > 0:
                retries -= 1

                def set_digital(print_time):
                    setter = dir_step_obj.set_digital
                    try:
                        return setter(print_time, int(value))
                    finally:
                        del setter

                toolhead.register_lookahead_callback(set_digital)
                self.gcode.run_script_from_command('M400')
                pause = self.reactor.pause
                try:
                    pause(self.reactor.monotonic() + 0.2)
                finally:
                    del pause
                result = query.send([oid, pin_obj])
                if result.get('status', -1) != int(value):
                    if obj_info[0].startswith('!'):
                        log = logging.info
                        try:
                            values = (pin_obj, value, str(result))
                            log('pin:%s value:%s resend success result:%s' % values)
                        finally:
                            del log
                        return
                    log = logging.info
                    try:
                        values = (pin_obj, value, str(result))
                        log('pin:%s value:%s resend fail result:%s' % values)
                    finally:
                        del log
                else:
                    log = logging.info
                    try:
                        values = (pin_obj, value, str(result))
                        log('pin:%s value:%s resend success result:%s' % values)
                    finally:
                        del log
                    return
            respond = self.gcode._respond_error
            try:
                values = (pin_obj, value)
                respond('{"code":"key799", "msg":"Motor set pin init io status error", "values":["%s:%s"]}' % values)
            finally:
                del respond
            pause = self.reactor.pause
            try:
                pause(self.reactor.monotonic() + 5)
            finally:
                del pause
            raise

        infos = []
        objects = []
        for motor in self.motor_closed_loop:
            if motor == 'x':
                objects += [self.motor_x_dir, self.motor_x_step]
                infos += [self.motor_x_dir_info, self.motor_x_step_info]
            elif motor == 'y':
                objects += [self.motor_y_dir, self.motor_y_step]
                infos += [self.motor_y_dir_info, self.motor_y_step_info]
            elif motor == 'z':
                objects += [self.motor_z_dir, self.motor_z_step]
                infos += [self.motor_z_dir_info, self.motor_z_step_info]
            elif motor == 'z1':
                objects += [self.motor_z1_dir, self.motor_z1_step]
                infos += [self.motor_z1_dir_info, self.motor_z1_step_info]
        index = 0
        for obj_info in infos:
            pin_obj = obj_info[0]
            if pin_obj.startswith('!'):
                pin_obj = pin_obj[1:]
            
            result = query.send([oid, pin_obj])
            if result.get('status', -1) != int(obj_info[1]):
                if obj_info[0].startswith('!'):
                    log = logging.info
                    try:
                        values = (obj_info[0], obj_info[1], str(result))
                        log('pin:%s value:%s success result:%s' % values)
                    finally:
                        del log
                else:
                    log = logging.info
                    try:
                        values = (obj_info[0], obj_info[1], str(result))
                        log('pin:%s value:%s fail result:%s' % values)
                    finally:
                        del log
                    log = logging.info
                    try:
                        values = (obj_info[0], obj_info[1])
                        log('resend pin:%s value:%s' % values)
                    finally:
                        del log
                    resend_set_io(obj_info, objects[index], int(obj_info[1]))
            else:
                log = logging.info
                try:
                    values = (obj_info[0], obj_info[1], str(result))
                    log('pin:%s value:%s success result:%s' % values)
                finally:
                    del log
            index += 1

    def check_motor_pin_io_restore(self):
        toolhead = self.printer.lookup_object('toolhead')
        oid = toolhead.kin.rails[2].steppers[0]._oid
        query = self.mcu.lookup_query_command(
            'query_gpio_status oid=%c gpio_pin=%c',
            'gpio_status oid=%c status=%c', oid=oid)

        def resend_set_io(obj_info, dir_step_obj, value):
            retries = 3
            pin_obj = obj_info[0]
            if pin_obj.startswith('!'):
                pin_obj = pin_obj[1:]
            while retries > 0:
                retries -= 1

                def set_digital(print_time):
                    setter = dir_step_obj.set_digital
                    try:
                        return setter(print_time, int(value))
                    finally:
                        del setter

                toolhead.register_lookahead_callback(set_digital)
                self.gcode.run_script_from_command('M400')
                pause = self.reactor.pause
                try:
                    pause(self.reactor.monotonic() + 0.2)
                finally:
                    del pause
                result = query.send([oid, pin_obj])
                if result.get('status', -1) != int(value):
                    if obj_info[0].startswith('!'):
                        log = logging.info
                        try:
                            values = (pin_obj, value, str(result))
                            log('pin:%s value:%s resend success result:%s' % values)
                        finally:
                            del log
                        return
                    log = logging.info
                    try:
                        values = (pin_obj, value, str(result))
                        log('pin:%s value:%s resend fail result:%s' % values)
                    finally:
                        del log
                else:
                    log = logging.info
                    try:
                        values = (pin_obj, value, str(result))
                        log('pin:%s value:%s resend success result:%s' % values)
                    finally:
                        del log
                    return
            respond = self.gcode._respond_error
            try:
                values = (pin_obj, value)
                respond('{"code":"key800", "msg":"Motor set pin restore io status error", "values":["%s:%s"]}' % values)
            finally:
                del respond
            pause = self.reactor.pause
            try:
                pause(self.reactor.monotonic() + 5)
            finally:
                del pause
            raise

        infos = []
        objects = []
        for motor in self.motor_closed_loop:
            if motor == 'x':
                objects += [self.motor_x_dir, self.motor_x_step]
                infos += [self.motor_x_dir_info, self.motor_x_step_info]
            elif motor == 'y':
                objects += [self.motor_y_dir, self.motor_y_step]
                infos += [self.motor_y_dir_info, self.motor_y_step_info]
            elif motor == 'z':
                objects += [self.motor_z_dir, self.motor_z_step]
                infos += [self.motor_z_dir_info, self.motor_z_step_info]
            elif motor == 'z1':
                objects += [self.motor_z1_dir, self.motor_z1_step]
                infos += [self.motor_z1_dir_info, self.motor_z1_step_info]
        index = 0
        for obj_info in infos:
            pin_obj = obj_info[0]
            if pin_obj.startswith('!'):
                pin_obj = pin_obj[1:]
            value = int(obj_info[0].startswith('!'))
            result = query.send([oid, pin_obj])
            if result.get('status', -1) != 0:
                if obj_info[0].startswith('!'):
                    log = logging.info
                    try:
                        values = (obj_info[0], value, str(result))
                        log('pin:%s value:%s success result:%s' % values)
                    finally:
                        del log
                else:
                    log = logging.info
                    try:
                        values = (obj_info[0], value, str(result))
                        log('pin:%s value:%s fail result:%s' % values)
                    finally:
                        del log
                    log = logging.info
                    try:
                        values = (obj_info[0], value)
                        log('resend pin:%s value:%s' % values)
                    finally:
                        del log
                    resend_set_io(obj_info, objects[index], value)
            else:
                log = logging.info
                try:
                    values = (obj_info[0], value, str(result))
                    log('pin:%s value:%s success result:%s' % values)
                finally:
                    del log
            index += 1

    def set_z_stall_mode(self):
        cmd = Motor_cmd.get('stall_mode')
        for obj in self.motor_closed_loop:
            num = 0
            if obj == 'z':
                num = 3
            elif obj == 'z1':
                num = 4
            else:
                continue
            tries = 3
            while tries > 0:
                addr = Motor_Addr.get(str(num))
                send = self.box_action.motor_send_data
                try:
                    result = send(addr, cmd, 0.05,
                                  data=self.parse.parse_num_to_byte(2), retries_en=False)
                finally:
                    del send
                if result:
                    motor_status = result[5:result[2] - 3 + 5]
                    if motor_status == b'\x02':
                        log = logging.info
                        try:
                            values = (num, self.parse.parse_num_to_byte(2), motor_status)
                            message = 'stall mode change motor8%s: send:%s recv:%s' % values
                            del values
                            try:
                                response = log(message)
                            finally:
                                del message
                        finally:
                            del log
                        del response
                        break
                pause = self.reactor.pause
                try:
                    response = pause(self.reactor.monotonic() + 0.05)
                finally:
                    del pause
                del response
                tries -= 1
            if tries == 0:
                respond = self.gcode._respond_error
                try:
                    message = '{"code":"key801", "msg":"Motor %s set_z_stall_mode failed, exceeding maximum retry count", "values":["z"]}' % num
                    try:
                        response = respond(message)
                    finally:
                        del message
                finally:
                    del respond
                del response
                pause = self.reactor.pause
                try:
                    response = pause(self.reactor.monotonic() + 5)
                finally:
                    del pause
                del response
                raise

    def set_e_stall_mode(self):
        data_byte = self.parse.parse_num_to_byte(2)
        convert = self.bytes_to_int
        try:
            addr = convert(Motor_Addr.get('1'))
        finally:
            del convert
        convert = self.bytes_to_int
        try:
            cmd = convert(Motor_cmd.get('stall_mode'))
        finally:
            del convert
        data_obj = self.bytes_to_int(data_byte, is_data_obj=True)
        data_send = self.data_pack(addr, cmd, data_obj)
        tries = 3
        while tries > 0:
            send = self.cmd_transparent_send.send
            try:
                params = send([self.oid, data_send, TIMEOUT_SHORT_TIME_MS])
            finally:
                del send
            respond = self.gcode.respond_info
            try:
                values = (str(data_send), str(params))
                message = 'data_send:%s result:%s' % values
                del values
                try:
                    response = respond(message)
                finally:
                    del message
            finally:
                del respond
            del response
            result = None
            if params.get('read'):
                result = params.get('read')[:-1]
                motor_status = result[5:result[2] - 3 + 5]
                if motor_status == b'\x02':
                    log = logging.info
                    try:
                        values = (data_byte, motor_status)
                        message = 'stall mode change motor_e: send:%s recv:%s' % values
                        del values
                        try:
                            response = log(message)
                        finally:
                            del message
                    finally:
                        del log
                    del response
                    return
            tries -= 1
            if tries == 0:
                self.gcode._respond_error('{"code":"key802", "msg":"Motor e set_e_stall_mode failed, exceeding maximum retry count", "values":["e"]}')

    def motor_config_params_init(self):
        motor_num = {'x': 1, 'y': 2, 'z': 3, 'z1': 4, 'e': 1}
        try:
            from extras.motor_params_config import objs
        except:
            objs = {}
        for obj in self.motor_closed_loop:
            parameters = {}
            for name in objs.get(obj):
                item = objs.get(obj).get(name)
                if len(item) == 3:
                    parameters.update({int(item[0]): [item[1], float(item[2]) if item[1] == 'float' else int(item[2])]})
            for parameter_id in parameters:
                value = parameters.get(parameter_id, None)
                extruder = False if obj != 'e' else True
                self.check_and_retry(motor_num[obj], 1, parameter_id, value[1], value[0], extruder=extruder)
            pause = self.reactor.pause
            try:
                pause(self.reactor.monotonic() + 0.001)
            finally:
                del pause


    def cmd_MOTOR_EXTRUDER_RETRY_PROCESS(self, gcmd):
        logging.info('hys: MOTOR_EXTRUDER_RETRY_PROCESS start')
        self.gcode.run_script_from_command('SET_STEPPER_ENABLE STEPPER=extruder ENABLE=0')
        self.gcode.run_script_from_command('MOTOR_CLEAR_ERR_WARN_CODE NUM=0 DATA=5')
        self.gcode.run_script_from_command('M400')
        target_temp = self.printer.lookup_object('extruder').heater.target_temp
        position = self.toolhead.get_position()
        respond = self.gcode.respond_info
        try:
            values = (self.printer.lookup_object('gcode_move').speed * 60,
                      self.toolhead.get_max_velocity()[1],
                      self.printer.lookup_object('gcode_move').speed_factor)
            text = 'velocity: %s, accel: %s, speed: %s' % values
            del values
            response = respond(text)
            del text
        finally:
            del respond
        del response
        save_accel = self.printer.lookup_object('box').box_action.box_save.save_printer_accel
        try:
            response = save_accel(self.printer.lookup_object('gcode_move').speed * 60,
                                  self.toolhead.get_max_velocity()[1],
                                  self.printer.lookup_object('gcode_move').speed_factor)
        finally:
            del save_accel
        del response
        toolhead = self.printer.lookup_object('toolhead')
        max_z = toolhead.kin.limits[2][1]
        act_z = toolhead.get_position()[2]
        move_z = 0.0
        if max_z - act_z >= 2:
            move_z = 2.0
        elif max_z - act_z >= 1:
            move_z = 1.0
        log = logging.info
        try:
            values = (max_z, act_z, move_z)
            text = 'hys: max_z = %.2f, act_z = %.2f, move_z = %.2f' % values
            del values
            response = log(text)
            del text
        finally:
            del log
        del response
        if move_z != 0.0:
            self.gcode.run_script_from_command('G91')
            run = self.gcode.run_script_from_command
            try:
                response = run('G0 Z%.2f F300' % move_z)
            finally:
                del run
            del response
            self.gcode.run_script_from_command('G90')
        material = self.printer.lookup_object('filament_rack').remain_material
        get_temp = self.printer.lookup_object('box').box_action.get_material_target_max_temp
        try:
            material_temp = get_temp(type=material.type)
        finally:
            del get_temp
        get_speed = self.printer.lookup_object('box').box_action.get_material_max_extrusion_speed
        try:
            extrusion_speed = get_speed(type=material.type) * 60 * 0.8
        finally:
            del get_speed
        self.gcode.run_script_from_command('BOX_GO_TO_EXTRUDE_POS')
        run = self.gcode.run_script_from_command
        try:
            response = run('M104 S%s' % material_temp)
        finally:
            del run
        del response
        self.gcode.run_script_from_command('G91')
        self.gcode.run_script_from_command('G0 E0.01 F60')
        self.gcode.run_script_from_command('G90')
        self.gcode.run_script_from_command('G4 P30000')
        self.gcode.run_script_from_command('M400')
        self.gcode.run_script_from_command('G91')
        run = self.gcode.run_script_from_command
        try:
            response = run('G0 E-1 F%.2f' % extrusion_speed)
        finally:
            del run
        del response
        run = self.gcode.run_script_from_command
        try:
            response = run('G0 E70 F%.2f' % extrusion_speed)
        finally:
            del run
        del response
        self.gcode.run_script_from_command('G90')
        self.gcode.run_script_from_command('M400')
        self.gcode.run_script_from_command('BOX_NOZZLE_CLEAN')
        self.gcode.run_script_from_command('M400')
        run = self.gcode.run_script_from_command
        try:
            response = run('M104 S%s' % target_temp)
        finally:
            del run
        del response
        self.gcode.run_script_from_command('G91')
        self.gcode.run_script_from_command('G0 E0.01 F60')
        self.gcode.run_script_from_command('G90')
        run = self.gcode.run_script_from_command
        try:
            values = (position[0], position[1])
            text = 'G0 X%.2f Y%.2f F9000' % values
            del values
            response = run(text)
            del text
        finally:
            del run
        del response
        if move_z != 0.0:
            self.gcode.run_script_from_command('G91')
            run = self.gcode.run_script_from_command
            try:
                response = run('G0 Z-%s F300' % move_z)
            finally:
                del run
            del response
            self.gcode.run_script_from_command('G90')
        self.printer.lookup_object('box').box_action.box_save.restore_printer_accel()
        logging.info('hys: MOTOR_EXTRUDER_RETRY_PROCESS end')
        self.auto_retry = True
        self.wait_auto_retry_end = False

    def motor_check_cut_pos(self, gcmd):
        get_status = self.toolhead.get_status
        try:
            status = get_status(self.reactor.monotonic())
        finally:
            del get_status
        if 'xy' not in status['homed_axes']:
            self.gcode.run_script_from_command('G28 X Y')
            self.gcode.run_script_from_command('BED_MESH_CLEAR')
        del status
        pause = self.reactor.pause
        try:
            pause(self.reactor.monotonic() + 1)
        finally:
            del pause
        gcode_move = self.printer.lookup_object('gcode_move')
        self.is_check_cut_pos_start = True
        cmd = Motor_cmd.get('stall_mode')
        get_addr = Motor_Addr.get
        try:
            addr = get_addr(str(MOTOR_NUM[0]))
        finally:
            del get_addr
        data_byte = self.parse.parse_num_to_byte(4)
        result = self.box_action.motor_send_data(addr, cmd, 0.05, data=data_byte, retries_en=False)
        old_mode = result[5:result[2] - 3 + 4]
        respond = self.gcode.respond_info
        try:
            respond('get_stall_mode motor_9:%s  ' % old_mode)
        finally:
            del respond
        data_byte = self.parse.parse_num_to_byte(3)
        get_addr = Motor_Addr.get
        try:
            addr = get_addr(str(MOTOR_NUM[0]))
        finally:
            del get_addr
        result = self.box_action.motor_send_data(addr, cmd, 0.05, data=data_byte, retries_en=False)
        if result:
            motor_status = result[5:result[2] - 3 + 5]
            respond = self.gcode.respond_info
            try:
                values = (MOTOR_NUM[0], data_byte, motor_status)
                message = 'set_stall_mode motor_8:%s: send:%s recv:%s' % values
                del values
                respond(message)
                del message
            finally:
                del respond
        else:
            respond = self.gcode.respond_info
            try:
                values = (MOTOR_NUM[0], data_byte)
                message = 'set_stall_mode_faild motor_8:%s: send:%s recv:None' % values
                del values
                respond(message)
                del message
            finally:
                del respond
            self.is_check_cut_pos_start = False
            return
        if self.box_action.filament_switch_sensor.runout_helper.filament_present:
            self.gcode.respond_raw(key_cut_filament_err)
            get_addr = Motor_Addr.get
            try:
                addr = get_addr(str(MOTOR_NUM[0]))
            finally:
                del get_addr
            result = self.box_action.motor_send_data(addr, cmd, 0.05, data=old_mode, retries_en=False)
            if result:
                motor_status = result[5:result[2] - 3 + 5]
                respond = self.gcode.respond_info
                try:
                    values = (MOTOR_NUM[0], old_mode, motor_status)
                    message = 'set_stall_mode motor_10:%s: send:%s recv:%s' % values
                    del values
                    respond(message)
                    del message
                finally:
                    del respond
            self.is_check_cut_pos_start = False
            raise gcmd.error(key_cut_filament_err)
        if not self.box_action.cut.state():
            self.gcode.respond_raw(key_cutter_not_exist)
            get_addr = Motor_Addr.get
            try:
                addr = get_addr(str(MOTOR_NUM[0]))
            finally:
                del get_addr
            result = self.box_action.motor_send_data(addr, cmd, 0.05, data=old_mode, retries_en=False)
            if result:
                motor_status = result[5:result[2] - 3 + 5]
                respond = self.gcode.respond_info
                try:
                    values = (MOTOR_NUM[0], old_mode, motor_status)
                    message = 'set_stall_mode motor_10:%s: send:%s recv:%s' % values
                    del values
                    respond(message)
                    del message
                finally:
                    del respond
            self.is_check_cut_pos_start = False
            raise gcmd.error(key_cutter_not_exist)
        max_accel = self.toolhead.get_max_velocity()[1]
        saved_cruise_ratio = self.toolhead.get_status(self.reactor.monotonic())['minimum_cruise_ratio']
        self.gcode.run_script_from_command('SET_VELOCITY_LIMIT ACCEL=25000 MINIMUM_CRUISE_RATIO=0')
        pre_x = self.printer.lookup_object('box').box_action.boxcfg.pre_cut_cal_pos_x
        max_x = self.printer.lookup_object('box').box_action.boxcfg.check_cut_pos_x_max
        min_x = self.printer.lookup_object('box').box_action.boxcfg.check_cut_pos_x_min
        pre_y = self.printer.lookup_object('box').box_action.boxcfg.pre_cut_pos_y
        run = self.gcode.run_script_from_command
        try:
            run('G1 Y%.2f F8000' % pre_y)
        finally:
            del run
        run = self.gcode.run_script_from_command
        try:
            run('G1 X%.2f F5000' % pre_x)
        finally:
            del run
        self.gcode.run_script_from_command('M400')
        self.cut_state = False
        tries = 100
        while tries > 0:
            run = self.gcode.run_script_from_command
            try:
                run(f'G90\nG1 X{pre_x + 0.1 * (tries - 100)} F6000\nG90')
            finally:
                del run
            self.gcode.run_script_from_command('M400')
            if self.cut_state:
                x = gcode_move.last_position[0]
                self.gcode.run_script_from_command('G1 X200 F6000\nM400')
                cut_pos = x + self.cut_pos_offset
                respond = self.gcode.respond_info
                try:
                    respond('last cut_pos:%s' % self.box_action.boxcfg.cut_pos_x)
                finally:
                    del respond
                respond = self.gcode.respond_info
                try:
                    values = (str(gcode_move.last_position), x, self.cut_pos_offset, x, self.cut_pos_offset, cut_pos)
                    message = 'final pos:%s x:%s cut_pos_offset:%s cut_pos:%s+%s=%s' % values
                    del values
                    respond(message)
                    del message
                finally:
                    del respond
                if cut_pos > max_x or cut_pos < min_x:
                    respond = self.gcode.respond_raw
                    try:
                        values = (max_x, min_x, cut_pos)
                        message = key_cut_pos_err % values
                        del values
                        respond(message)
                        del message
                    finally:
                        del respond
                    warn = logging.warning
                    try:
                        values = (max_x, min_x, cut_pos)
                        message = key_cut_pos_err % values
                        del values
                        warn(message)
                        del message
                    finally:
                        del warn
                self.box_action.boxcfg.cut_pos_x = cut_pos
                configfile = self.printer.lookup_object('configfile')
                save = configfile.set
                try:
                    save('box', 'cut_pos_x', '%.2f' % self.box_action.boxcfg.cut_pos_x)
                finally:
                    del save
                self.gcode.run_script_from_command('CXSAVE_CONFIG')
                break
            self.gcode.run_script_from_command('G1 X50 F6000\nM400')
            tries -= 1
        data_byte = self.parse.parse_num_to_byte(2)
        get_addr = Motor_Addr.get
        try:
            addr = get_addr(str(MOTOR_NUM[0]))
        finally:
            del get_addr
        result = self.box_action.motor_send_data(addr, cmd, 0.05, data=data_byte, retries_en=False)
        if result:
            motor_status = result[5:result[2] - 3 + 5]
            respond = self.gcode.respond_info
            try:
                values = (MOTOR_NUM[0], data_byte, motor_status)
                message = 'set_stall_mode motor_8%s: send:%s recv:%s' % values
                del values
                respond(message)
                del message
            finally:
                del respond
        self.cut_state = False
        self.is_check_cut_pos_start = False
        run = self.gcode.run_script_from_command
        try:
            values = (max_accel, saved_cruise_ratio)
            message = 'SET_VELOCITY_LIMIT ACCEL=%.3f MINIMUM_CRUISE_RATIO=%.9f' % values
            del values
            run(message)
            del message
        finally:
            del run
        self.printer.send_event('v_sd:reset_cut_calibration_count')
