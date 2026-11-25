# Support for 1-wire based temperature sensors
#
# Copyright (C) 2020 Alan Lord <alanslists@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

class CUSTOM_MACRO:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.pheaters = None
        self.heater_hot = None
        self.extruder_temp=None
        self.bed_temp=None
        self.prtouch = None
        self.gcode.register_command("CX_PRINT_LEVELING_CALIBRATION", self.cmd_CX_PRINT_LEVELING_CALIBRATION, desc=self.cmd_CX_PRINT_LEVELING_CALIBRATION_help)
        self.gcode.register_command("CX_CLEAN_CALIBRATION_FLAGS", self.cmd_CX_CLEAN_CALIBRATION_FLAGS, desc=self.cmd_CX_CLEAN_CALIBRATION_FLAGS_help)
        self.gcode.register_command("CX_PRINT_DRAW_ONE_LINE", self.cmd_CX_PRINT_DRAW_ONE_LINE, desc=self.cmd_CX_PRINT_DRAW_ONE_LINE_help)
        self.gcode.register_command("CX_ROUGH_G28", self.cmd_CX_ROUGH_G28, desc=self.cmd_CX_ROUGH_G28_help)
        self.gcode.register_command("CX_NOZZLE_CLEAR", self.cmd_CX_NOZZLE_CLEAR, desc=self.cmd_CX_NOZZLE_CLEAR_help)
        self.default_extruder_temp = config.getfloat("default_extruder_temp", default=240.0)
        self.default_bed_temp = config.getfloat("default_bed_temp", default=50.0)
        self.g28_ext_temp = config.getfloat("g28_ext_temp", default=140.0)
        self.nozzle_clear = config.getboolean('nozzle_clear', True)
        self.calibration = config.getint('calibration', default=0)
        self.leveling_calibration = 0
        self.qmode_flag = 0
        self.gcode.register_command("SET_QMODE_FLAG", self.cmd_SET_QMODE_FLAG, desc=self.cmd_SET_QMODE_FLAG_help)
        pass


    def get_status(self, eventtime):
        return {
            'leveling_calibration': self.leveling_calibration,
            'default_extruder_temp': self.default_extruder_temp,
            'default_bed_temp': self.default_bed_temp,
            'g28_ext_temp': self.g28_ext_temp,
            'qmode_flag': self.qmode_flag
        }

    cmd_CX_PRINT_LEVELING_CALIBRATION_help = "Start Print function,three parameter:EXTRUDER_TEMP(180-300),BED_TEMP(0-100),CALIBRATION(0 or 1)"
    def cmd_CX_PRINT_LEVELING_CALIBRATION(self, gcmd):
        bed = self.bed_temp if self.bed_temp is not None else self.default_bed_temp
        ext = self.extruder_temp if self.extruder_temp is not None else self.default_extruder_temp
        self.gcode.run_script_from_command('G29 BED_TEMP=%d EXTRUDER_TEMP=%d' % (int(bed), int(ext)))

    cmd_CX_CLEAN_CALIBRATION_FLAGS_help = "Clean calibration flags"
    def cmd_CX_CLEAN_CALIBRATION_FLAGS(self, gcmd):
        self.leveling_calibration = 0

    cmd_CX_PRINT_DRAW_ONE_LINE_help = "Draw one line before printing"
    def cmd_CX_PRINT_DRAW_ONE_LINE(self, gcmd):
        start_macro = self.printer.lookup_object('gcode_macro START_PRINT', None)
        variables = getattr(start_macro, 'variables', None) or {}
        if int(variables.get('prepare', 0)):
            self.gcode.run_script_from_command('K_PRIME_LINE')
            return
        ext = self.extruder_temp if self.extruder_temp is not None else self.default_extruder_temp
        self.gcode.run_script_from_command('IF_NEED_HOME')
        self.gcode.run_script_from_command('BOX_GO_TO_EXTRUDE_POS')
        self.gcode.run_script_from_command('M109 S%d' % int(ext))
        self.gcode.run_script_from_command('G91')
        self.gcode.run_script_from_command('G1 E20 F300')
        self.gcode.run_script_from_command('G90')
        self.gcode.run_script_from_command('M400')
        self.gcode.run_script_from_command('BOX_NOZZLE_CLEAN')
        self.gcode.run_script_from_command('G92 E0')

    cmd_CX_ROUGH_G28_help = "rough G28"
    def cmd_CX_ROUGH_G28(self, gcmd):
        self.extruder_temp = gcmd.get_float('EXTRUDER_TEMP', default=self.default_extruder_temp, minval=140.0, maxval=350.0)
        self.g28_ext_temp = min(max(self.extruder_temp - 70.0, 140.0), 180.0)
        self.bed_temp = gcmd.get_float('BED_TEMP', default=self.default_bed_temp, minval=0.0, maxval=130.0)
        self.leveling_calibration = gcmd.get_int('LEVELING_CALIBRATION', default=1, minval=0, maxval=1)
        self.gcode.run_script_from_command('M104 S%d' % int(self.g28_ext_temp))
        self.gcode.run_script_from_command('M140 S%d' % int(self.bed_temp))
        self.gcode.run_script_from_command('G28')

    cmd_CX_NOZZLE_CLEAR_help = "nozzle clear with temperature"
    def cmd_CX_NOZZLE_CLEAR(self, gcmd):
        self.gcode.run_script_from_command('NOZZLE_CLEAR')
    cmd_SET_QMODE_FLAG_help = "set qmode flag"
    def cmd_SET_QMODE_FLAG(self, gcmd):
        self.qmode_flag =  gcmd.get_int('FLAG', default=1, minval=0, maxval=1)
        gcmd.respond_info("[cmd_SET_QMODE_FLAG] self.qmode_flag={}".format(self.qmode_flag))
        import json, logging
        try:
            print_stats = self.printer.lookup_object('print_stats')
            v_sd = self.printer.lookup_object('virtual_sdcard', None)
            speed_mode_path = getattr(v_sd, 'speed_mode_path', None)
            if speed_mode_path and print_stats.state == "printing" and self.qmode_flag == 1:
                result = {}
                result["speed_mode"] = 2
                with open(speed_mode_path, "w") as f:
                    f.write(json.dumps(result))
                    f.flush()
        except Exception as err:
            err_msg = "cmd_SET_QMODE_FLAG err %s" % str(err)
            logging.error(err_msg)
        pass

def load_config(config):
    return CUSTOM_MACRO(config)
