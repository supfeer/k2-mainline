"""Recovered filament rack behavior; hardware qualification is separate."""
import copy
import json
import logging
import os
import subprocess
import threading
import time

from extras.base_info import base_dir
from extras.box_wrapper import _manual_cfs_extrude


class RemainMaterial:
    def __init__(self, config):
        self.path = os.path.join(
            base_dir, "creality/userdata/box/remain_material_data.json")
        self.config = config
        self.color = None
        self.type = None
        self.velocity = None
        self.init()

    def init(self):
        data = {}
        error = None
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as stream:
                    data = json.load(stream)
            except Exception as caught:
                error = caught
                logging.error(error)
        if data and "remain_material" in data:
            self.color = data.get("remain_material", {})["color"]
            self.type = data.get("remain_material", {})["type"]
        else:
            data["remain_material"] = {"color": None, "type": None}
            try:
                with open(self.path, "w") as stream:
                    json.dump(data, stream)
            except Exception as caught:
                error = caught
                logging.error(error)

    def save(self, color=None, type=None):
        self.color = color
        self.type = type
        data = {}
        error = None
        try:
            with open(self.path, "r") as stream:
                data = json.load(stream)
        except Exception as caught:
            error = caught
            logging.error(error)
        data["remain_material"] = {"color": self.color, "type": self.type}
        log_info = logging.info
        try:
            material = (self.color, self.type)
            message = "save: color: %s, type: %s" % material
            del material
            result = log_info(message)
        finally:
            del log_info
        del message, result
        try:
            with open(self.path, "w") as stream:
                json.dump(data, stream)
        except Exception as caught:
            error = caught
            logging.error(error)
        self.get_velocity()

    def get_velocity(self):
        get_material_target_speed = self.config.get_printer().lookup_object(
            "filament_rack").get_material_target_speed
        try:
            velocity = get_material_target_speed(type=self.type)
        finally:
            del get_material_target_speed
        self.velocity = velocity

    def get_status(self):
        return {
            "remain_material_color": self.color,
            "remain_material_type": self.type,
            "remain_material_velocity": self.velocity,
        }


class FilamentRackWrapper:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        not_pin = config.get("not_pin", None)
        self.addr = config.getint("addr", default=17)
        self.extrude = config.getint("extrude", default=140, minval=0, maxval=300)
        self.temp = config.getint("temp", default=220, minval=140, maxval=350)
        self.velocity = config.getint("velocity", default=360, minval=0, maxval=900)
        self.diff_material_type_min_flush_len = config.getfloat(
            "diff_material_type_min_flush_len", default=370, minval=300, maxval=1000)
        self.max_temp_flush_times = config.getint(
            "max_temp_flush_times", default=3, minval=0, maxval=10)
        self.beep_didi_param = config.getfloatlist("didi", default=())
        self.need_clean_length = config.getint(
            "need_clean_length", default=70, minval=0, maxval=200)
        buttons = self.printer.load_object(config, "buttons")
        self.material_database = os.path.join(
            base_dir, "creality/userdata/box/material_database.json")
        self.system_config = os.path.join(
            base_dir, "creality/userdata/config/system_config.json")
        self._serial = None
        self.toolhead = None
        self.print_stats = None
        self.output_pin_fan0 = None
        self.fan0_last_value = None
        self.can_get_filament_rack = False
        self.can_get_filament_rack_tmp = False
        self.box = None
        if (config.has_section("motor_control")
                and config.getsection("motor_control").getint("switch") == 1):
            ready_event = "auto_addr:start"
        else:
            ready_event = "klippy:ready"
        for event_name, handler_name in (
                (ready_event, "enable_get_filament"), ("klippy:ready", "find_objs")):
            register_event_handler = self.printer.register_event_handler
            try:
                result = register_event_handler(event_name, getattr(self, handler_name))
            finally:
                del register_event_handler
            del result
        for command, handler_name in (
                ("FILAMENT_RACK", "cmd_filament_rack"),
                ("FILAMENT_RACK_FLUSH", "cmd_filament_rack_flush"),
                ("FILAMENT_RACK_SAVE_FAN", "cmd_save_fan"),
                ("FILAMENT_RACK_RESTORE_FAN", "cmd_restore_fan"),
                ("FILAMENT_RACK_SET_TEMP", "cmd_filament_rack_set_temp"),
                ("FILAMENT_RACK_PRE_FLUSH", "cmd_filament_rack_pre_flush"),
                ("FILAMENT_RACK_MODIFY", "cmd_filament_rack_modify"),
                ("SET_COOL_TEMP", "cmd_set_cool_temp")):
            register_command = self.gcode.register_command
            try:
                result = register_command(command, getattr(self, handler_name))
            finally:
                del register_command
            del result
        self.filament_rack = None
        if not_pin:
            log_info = logging.info
            try:
                result = log_info("[filament rack] not_pin: %s" % not_pin)
            finally:
                del log_info
            del result
            register_buttons = buttons.register_buttons
            try:
                result = register_buttons([not_pin], self._button_handler)
            finally:
                del register_buttons
            del result
        else:
            logging.warning("[filament rack] do not define not_pin")
        self.filament_rack_data = {
            "vender": "-1", "color_value": "-1", "material_type": "-1",
        }
        self.remain_material = RemainMaterial(config)

    def cmd_filament_rack_flush(self, gcmd):
        if _manual_cfs_extrude(self, gcmd, heat_only=False):
            return
        self.gcode.run_script_from_command("M106 P0 S50")
        # Resolve each callable before its arguments, including in Cython builds.
        get_material_target_temp = self.get_material_target_temp
        try:
            remaining_max_temp = get_material_target_temp(
                type=self.remain_material.type, max=True)
        finally:
            del get_material_target_temp
        get_material_target_temp = self.get_material_target_temp
        try:
            remaining_temp = get_material_target_temp(type=self.remain_material.type)
        finally:
            del get_material_target_temp
        get_material_target_temp = self.get_material_target_temp
        try:
            current_max_temp = get_material_target_temp(
                type=self.filament_rack_data["material_type"], max=True)
        finally:
            del get_material_target_temp
        get_material_target_temp = self.get_material_target_temp
        try:
            current_temp = get_material_target_temp(
                type=self.filament_rack_data["material_type"])
        finally:
            del get_material_target_temp
        max_temp = (current_max_temp if current_max_temp > remaining_max_temp
                    else remaining_max_temp)
        temp = current_temp if current_temp > remaining_temp else remaining_temp
        get_material_target_speed = self.get_material_target_speed
        try:
            velocity = get_material_target_speed(
                type=self.filament_rack_data["material_type"])
        finally:
            del get_material_target_speed
        flush_length = (self.extrude
                        if self.remain_material.type == self.filament_rack_data["material_type"]
                        else self.diff_material_type_min_flush_len)
        unknown_colors = ["-1", "none", "unknown"]
        remaining_color = self.remain_material.color
        current_color = self.filament_rack_data["color_value"]
        if (remaining_color is not None and remaining_color not in unknown_colors
                and current_color is not None and current_color not in unknown_colors):
            remaining_parts = [remaining_color[index:index + 2]
                               for index in range(1, len(remaining_color), 2)]
            remaining_rgb = [int(part, 16) for part in remaining_parts]
            current_parts = [current_color[index:index + 2]
                             for index in range(1, len(current_color), 2)]
            current_rgb = [int(part, 16) for part in current_parts]
            color_flush_length = self.box.box_action.get_flush_len(remaining_rgb, current_rgb)
            if color_flush_length:
                flush_length = (flush_length if flush_length > color_flush_length
                                else color_flush_length)
        log_info = logging.info
        try:
            material = (self.remain_material.color, self.remain_material.type,
                        self.filament_rack_data, remaining_max_temp, remaining_temp,
                        current_max_temp, current_temp)
            message = ("hys: remain_material.color: %s, remain_material.type: %s, "
                       "filament_rack_data: %s, remain_max_temp = %s, remain_temp = %s, "
                       "current_max_temp = %s, current_temp = %s, ") % material
            del material
            result = log_info(message)
        finally:
            del log_info
        del message, result
        length_list = []
        self.box.box_action.cal_flush_list(flush_length, length_list)
        respond_info = self.gcode.respond_info
        try:
            parameters = (flush_length, length_list, temp, max_temp)
            message = "hys: flush_length: %s, length_list: %s, temp: %s, max_temp: %s" % parameters
            del parameters
            result = respond_info(message)
        finally:
            del respond_info
        del message, result
        velocity_list = []
        velocity_list.append(velocity)
        for index in range(len(length_list)):
            if index > 0 and index == len(length_list) - 1:
                if remaining_temp > current_temp:
                    check_flush_temp_and_extrude = self.box.box_action.check_flush_temp_and_extrude
                    try:
                        result = check_flush_temp_and_extrude(
                            length_list[index], self.max_temp_flush_times, temp, max_temp,
                            velocity_list, max_temp_flush=True)
                    finally:
                        del check_flush_temp_and_extrude
                else:
                    check_flush_temp_and_extrude = self.box.box_action.check_flush_temp_and_extrude
                    try:
                        result = check_flush_temp_and_extrude(
                            length_list[index], self.max_temp_flush_times, temp, temp,
                            velocity_list, max_temp_flush=True)
                    finally:
                        del check_flush_temp_and_extrude
            else:
                if remaining_temp > current_temp:
                    check_flush_temp_and_extrude = self.box.box_action.check_flush_temp_and_extrude
                    try:
                        result = check_flush_temp_and_extrude(
                            length_list[index], index, temp, max_temp,
                            velocity_list, max_temp_flush=True)
                    finally:
                        del check_flush_temp_and_extrude
                else:
                    check_flush_temp_and_extrude = self.box.box_action.check_flush_temp_and_extrude
                    try:
                        result = check_flush_temp_and_extrude(
                            length_list[index], index, temp, temp,
                            velocity_list, max_temp_flush=True)
                    finally:
                        del check_flush_temp_and_extrude
            if result is False:
                self.box.box_state.clear_e_err()
                return False
            self.blow()
            if index == len(length_list) - 1:
                self.box.box_action.nozzle_clean()
            else:
                nozzle_clean = self.box.box_action.nozzle_clean
                try:
                    clean_result = nozzle_clean(circle=False)
                finally:
                    del nozzle_clean
                del clean_result
        save_material = self.remain_material.save
        try:
            save_result = save_material(color=self.filament_rack_data["color_value"],
                                        type=self.filament_rack_data["material_type"])
        finally:
            del save_material
        del save_result

    def cmd_filament_rack_pre_flush(self, gcmd):
        return None

    def cmd_filament_rack_modify(self, gcmd):
        color_value = material_type = vender = None
        error = None
        try:
            color_value = gcmd.get_command_parameters().get("COLOR_VALUE")
            material_type = gcmd.get_command_parameters().get("MATERIAL_TYPE")
            vender = gcmd.get_command_parameters().get("VENDER")
        except Exception as caught:
            error = caught
            logging.warning(error)
            return
        if color_value is None:
            logging.warning("color_value get failed")
            color_value = "-1"
        if material_type is None:
            logging.warning("material_type get failed")
            material_type = "-1"
        if vender is None:
            logging.warning("vender get failed")
            vender = "-1"
        log_info = logging.info
        try:
            material = (vender, color_value, material_type)
            message = "vender: %s, color_value: %s, material_type: %s" % material
            del material
            result = log_info(message)
        finally:
            del log_info
        del message, result
        self.filament_rack_data["material_type"] = material_type
        self.filament_rack_data["color_value"] = color_value
        self.filament_rack_data["vender"] = vender

    def cmd_filament_rack_set_temp(self, gcmd):
        if _manual_cfs_extrude(self, gcmd, heat_only=True):
            return
        rack_temperature = self.get_material_target_temp()
        get_material_target_temp = self.get_material_target_temp
        try:
            remaining_temperature = get_material_target_temp(type=self.remain_material.type)
        finally:
            del get_material_target_temp
        temperature = (rack_temperature if rack_temperature > remaining_temperature
                       else remaining_temperature)
        del rack_temperature, remaining_temperature
        log_info = logging.info
        try:
            result = log_info("temp = %s" % temperature)
        finally:
            del log_info
        del result
        self.quickly_wait_heating(temperature)
        self.gcode.run_script_from_command("M400")

    def quickly_wait_heating(self, temp):
        run_script_from_command = self.gcode.run_script_from_command
        try:
            result = run_script_from_command("M104 S%d" % temp)
        finally:
            del run_script_from_command
        del result
        while not self.printer.is_shutdown():
            current_temperature = self.toolhead.get_extruder().get_heater().smoothed_temp
            # Separate comparisons so Cython propagates truth-conversion errors.
            lower_result = temp - 10 <= current_temperature
            lower_true = bool(lower_result)
            if lower_true:
                del lower_result
                within_range = current_temperature <= temp + 10
            else:
                within_range = lower_result
                del lower_result
            in_range = bool(within_range)
            del within_range
            if in_range and self.toolhead.get_extruder().get_heater().can_extrude:
                break
            pause = self.reactor.pause
            try:
                result = pause(self.reactor.monotonic() + 0.5)
            finally:
                del pause
            del result
        else:
            return
        self.gcode.run_script_from_command("G91")
        self.gcode.run_script_from_command("G0 E0.01 F60")
        self.gcode.run_script_from_command("G90")

    def beep(self):
        sound_enabled = True
        error = None
        try:
            path_exists = os.path.exists
            try:
                exists = path_exists(self.system_config)
            finally:
                del path_exists
            if exists:
                with open(self.system_config, "r") as stream:
                    loads = json.loads
                    try:
                        data = loads(stream.read())
                    finally:
                        del loads
                    if data.get("user_info", {}).get("sound_sw", 1) == 0:
                        sound_enabled = False
        except Exception as caught:
            error = caught
            logging.error("system config get sound switch: %s" % error)
        if not sound_enabled:
            return
        if len(self.beep_didi_param):
            for index in range(len(self.beep_didi_param)):
                if index % 2 == 0:
                    try:
                        logging.info("beep")
                        check_output = subprocess.check_output
                        try:
                            output = check_output(
                                "/usr/bin/beep.sh %s" % str(self.beep_didi_param[index]),
                                shell=True)
                        finally:
                            del check_output
                        output = output.decode("utf-8")
                        logging.info(output)
                    except Exception as caught:
                        error = caught
                        logging.error(error)
                sleep = time.sleep
                try:
                    result = sleep(self.beep_didi_param[index])
                finally:
                    del sleep
                del result
        else:
            try:
                logging.info("beep")
                output = subprocess.check_output("/usr/bin/beep.sh 0.2", shell=True).decode("utf-8")
                logging.info(output)
            except Exception as caught:
                error = caught
                logging.error(error)

    def cmd_set_cool_temp(self, gcmd):
        if self.print_stats.state == "paused":
            if (self.toolhead.get_extruder().get_heater().target_temp
                    >= self.toolhead.get_extruder().get_heater().min_extrude_temp):
                run_script_from_command = self.gcode.run_script_from_command
                try:
                    result = run_script_from_command(
                        "SET_GCODE_VARIABLE MACRO=PRINTER_PARAM VARIABLE=hotend_temp VALUE=%s"
                        % self.toolhead.get_extruder().get_heater().target_temp)
                finally:
                    del run_script_from_command
                del result
            self.gcode.run_script_from_command("M104 S140")
        elif self.print_stats.state == "printing":
            logging.warning("unknown action")
        else:
            self.gcode.run_script_from_command("M104 S0")

    def check_rfid_valid(self, rfid):
        material_type = rfid[12:17]
        error = None
        try:
            path_exists = os.path.exists
            try:
                exists = path_exists(self.material_database)
            finally:
                del path_exists
            if exists:
                with open(self.material_database, "r") as stream:
                    data = json.load(stream)
                    materials = data.get("result", {}).get("list", [])
                    for index in range(len(materials)):
                        material_id = materials[index].get("base", {}).get("id", None)
                        if material_id is not None and material_type == material_id:
                            respond_info = self.gcode.respond_info
                            try:
                                result = respond_info("material_type: %s" % material_type)
                            finally:
                                del respond_info
                            del result
                            return True
        except Exception as caught:
            error = caught
            logging.error("material database get extrusion speed: %s" % error)
        return False

    def send_data(self, eventtime=None):
        if self.can_get_filament_rack is False:
            if self.can_get_filament_rack_tmp is True:
                return
            self.can_get_filament_rack_tmp = True
            while self.can_get_filament_rack is False:
                pause = self.reactor.pause
                try:
                    result = pause(self.reactor.monotonic() + 1)
                finally:
                    del pause
                del result
        beep_thread = threading.Thread(target=self.beep)
        beep_thread.start()
        data_send = b"\x11\x03\xff\x02"
        hex_string = " ".join(hex(value) for value in data_send)
        logging.info("[filament rack] data_send: %s" % hex_string)
        response = self._serial.cmd_send_data_with_response(data_send, 0.5)
        if response is None:
            logging.warning("[filament rack] do not get respond")
            return
        # Preserve Python's index protocol; Cython's direct chr() accepts floats.
        character = chr
        rfid = "".join(character(value) for value in response[5:-1])
        if len(rfid) == 40 and self.check_rfid_valid(rfid):
            logging.info("[filament rack] rfid: %s" % rfid)
            self.filament_rack_data["vender"] = rfid
            self.filament_rack_data["material_type"] = rfid[11:17]
            self.filament_rack_data["color_value"] = rfid[17:24]
        else:
            logging.warning("[filament rack] rfid[%s] is error" % rfid)

    def cmd_filament_rack(self, gcmd):
        self.send_data()

    def find_objs(self):
        self._serial = self.printer.lookup_object("serial_485 serial485")
        self.toolhead = self.printer.lookup_object("toolhead")
        self.print_stats = self.printer.lookup_object("print_stats")
        self.output_pin_fan0 = self.printer.lookup_object("output_pin fan0")
        self.box = self.printer.lookup_object("box")
        self.remain_material.get_velocity()

    def _button_handler(self, eventtime, state):
        logging.info("[filament rack] not_pin trigger, state: %s" % state)
        if state:
            register_callback = self.reactor.register_callback
            try:
                result = register_callback(self.send_data)
            finally:
                del register_callback
            del result

    def get_material_target_temp(self, type=None, max=False):
        if type is None:
            type = self.filament_rack_data["material_type"]
        if type is None or type == "None" or type == "-1":
            logging.warning("failed to obtain consumable type")
            return self.temp
        type = type[1:6]
        temperature = None
        error = None
        try:
            path_exists = os.path.exists
            try:
                exists = path_exists(self.material_database)
            finally:
                del path_exists
            if exists:
                with open(self.material_database, "r") as stream:
                    data = json.load(stream)
                    materials = data.get("result", {}).get("list", [])
                    for index in range(len(materials)):
                        material_id = materials[index].get("base", {}).get("id", None)
                        nozzle_temperature = materials[index].get("kvParam", {}).get(
                            "nozzle_temperature", None)
                        maximum_temperature = materials[index].get("base", {}).get("maxTemp", None)
                        if material_id is not None and nozzle_temperature is not None and material_id == type:
                            temperature = int(nozzle_temperature) if max is False else int(maximum_temperature)
                            break
        except Exception as caught:
            error = caught
            logging.error("material database get nozzle temp: %s" % error)
        if temperature is None:
            logging.warning("get material target temp fail")
            return self.temp
        logging.info("get material temp: %d" % temperature)
        return temperature

    def get_material_target_speed(self, type=None):
        if type is None:
            type = self.filament_rack_data["material_type"]
        if type is None or type == "None" or type == "-1":
            logging.warning("failed to obtain consumable type")
            return self.velocity
        type = type[1:6]
        extrusion_speed = None
        error = None
        try:
            path_exists = os.path.exists
            try:
                exists = path_exists(self.material_database)
            finally:
                del path_exists
            if exists:
                with open(self.material_database, "r") as stream:
                    data = json.load(stream)
                    materials = data.get("result", {}).get("list", [])
                    for index in range(len(materials)):
                        material_id = materials[index].get("base", {}).get("id", None)
                        volumetric_speed = materials[index].get("kvParam", {}).get(
                            "filament_max_volumetric_speed", None)
                        if material_id is not None and volumetric_speed is not None and material_id == type:
                            respond_info = self.gcode.respond_info
                            try:
                                result = respond_info("max_volumetric_speed: %s" % volumetric_speed)
                            finally:
                                del respond_info
                            del result
                            extrusion_speed = float(volumetric_speed) / 2.4
                            break
        except Exception as caught:
            error = caught
            logging.error("material database get extrusion speed: %s" % error)
        if extrusion_speed is None:
            logging.warning("get material extrusion speed fail")
            return self.velocity
        logging.info("get material extrusion speed: %d" % extrusion_speed)
        return extrusion_speed * 60

    def enable_get_filament(self):
        self.can_get_filament_rack = True

    def get_status(self, eventtime):
        status = copy.deepcopy(self.filament_rack_data)
        update_status = status.update
        try:
            result = update_status(self.remain_material.get_status())
        finally:
            del update_status
        del result
        return status

    def blow(self):
        fan0_value = self.output_pin_fan0.last_value * self.output_pin_fan0.scale
        self.gcode.run_script_from_command("SET_PIN PIN=fan0 VALUE=255")
        self.gcode.run_script_from_command("G4 P3000")
        self.gcode.run_script_from_command("M400")
        run_script_from_command = self.gcode.run_script_from_command
        try:
            result = run_script_from_command("SET_PIN PIN=fan0 VALUE=%.2f" % fan0_value)
        finally:
            del run_script_from_command
        del result

    def cmd_save_fan(self, gcmd):
        if self.output_pin_fan0 is not None:
            if self.fan0_last_value is None:
                self.fan0_last_value = (
                    self.output_pin_fan0.last_value * self.output_pin_fan0.scale)
            self.gcode.run_script_from_command("SET_PIN PIN=fan0 VALUE=0.0")

    def cmd_restore_fan(self, gcmd):
        if self.output_pin_fan0 is not None:
            if self.fan0_last_value is not None:
                run_script_from_command = self.gcode.run_script_from_command
                try:
                    result = run_script_from_command(
                        "SET_PIN PIN=fan0 VALUE=%.2f" % self.fan0_last_value)
                finally:
                    del run_script_from_command
                del result
            self.fan0_last_value = None
