"""Home the K2 bottom switches through native stepper/endstop ownership."""
import logging
import math

import mcu
from . import homing, probe
from .k2 import MESSAGES


class BottomEndstop(mcu.MCU_endstop):
    def __init__(self, chip, pin, count):
        self.filter_count = count
        super().__init__(chip, pin)

    def home_start(self, print_time, sample_time, sample_count, rest_time,
                   triggered=True):
        # Preserve the configured debounce at the former half-step sampling rate.
        return super().home_start(print_time, max(sample_time, rest_time / 2.),
                                  max(sample_count, self.filter_count), rest_time,
                                  triggered=triggered)


class NativeZAlign:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.messages = config.getsection('k2').getchoice('language', MESSAGES, 'ru')
        self.maximum = config.getsection('stepper_z').getfloat('position_max')
        self.quick_speed = config.getfloat('quick_speed', above=0.)
        self.slow_speed = config.getfloat('slow_speed', above=0.)
        self.safe_distance = config.getfloat('safe_dist', above=0.)
        self.timeout = config.getfloat('timeout', above=0.)
        self.zmax_safe_pox_diff = config.getfloat('zmax_safe_pox_diff', minval=0.)
        count = config.getint('filter_cnt', minval=1)
        untriggered = config.getint('zes_untrig', minval=0, maxval=1)
        pins = config.getlist('endstop_pin_z')
        self.retract = config.getfloat('rising_dist', above=0.)
        config.getfloat('distance_ratio', minval=0., maxval=1.)
        self.retries = config.getint('retries', minval=1)
        self.retry_tolerance = config.getint('retry_tolerance', minval=0)
        config.getint('zd_up', minval=0, maxval=1)
        if (len(pins) != 2 or len(set(pins)) != 2
                or self.slow_speed > self.quick_speed
                or not all(math.isfinite(v) and v > 0 for v in
                           (self.maximum, self.quick_speed, self.slow_speed,
                            self.safe_distance, self.timeout, self.retract))
                or not math.isfinite(self.zmax_safe_pox_diff)
                or self.retract >= self.maximum
                or self.retract * 2 > self.maximum + self.safe_distance):
            raise config.error(self.messages['z_bottom_config'])
        self.endstops = []
        self.steppers = {}
        ppins = self.printer.lookup_object('pins')
        query = self.printer.load_object(config, 'query_endstops')
        for index, pin in enumerate(pins):
            descriptor = ('^!' if untriggered else '') + pin
            params = ppins.lookup_pin(descriptor, can_invert=True, can_pullup=True)
            endstop = BottomEndstop(params['chip'], params, count)
            name = 'z_bottom' if index == 0 else 'z1_bottom'
            self.endstops.append((endstop, name))
            query.register_endstop(endstop, name)
        self.bound = set()
        self.states = [-1, -1]
        self.active = self.force_stop_flag = self.is_already_zodwn = False
        self.cur_retries = self.zdown_switch_enable = 0
        probe.LookupZSteppers(config, self.add_stepper)
        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('Z_ALIGN_ENDSTOPS', self.run)
        gcode.register_command('ZDOWN', self.run)
        gcode.register_command('ZDOWN_FORCE_STOP', self.stop)
        gcode.register_command('ZDOWN_SWITCH', self.switch)
        self.printer.lookup_object('webhooks').register_endpoint(
            'zdown_force_stop', self.web_stop)

    def _handle_ready(self):
        self.force_stop_flag = False

    def add_stepper(self, stepper):
        name = stepper.get_name()
        if name not in ('stepper_z', 'stepper_z1') or name in self.bound:
            raise self.printer.config_error(self.messages['z_bottom_config'])
        endstop = self.endstops[name == 'stepper_z1'][0]
        if endstop.get_mcu() is not stepper.get_mcu():
            raise self.printer.config_error(self.messages['z_bottom_config'])
        endstop.add_stepper(stepper)
        self.steppers[name] = stepper
        self.bound.add(name)

    def check(self):
        cancellation = self.printer.lookup_object('k2_cancel', None)
        if cancellation is not None:
            cancellation.check()
        motor = self.printer.lookup_object('motor_control')
        if (self.printer.is_shutdown() or self.force_stop_flag
                or not motor.is_ready or motor.is_homing
                or any(motor.motor_error_code.values())
                or len(self.bound) != 2):
            raise self.printer.command_error(self.messages['z_bottom_state'])

    def read_states(self, toolhead):
        print_time = toolhead.get_last_move_time()
        self.states = [int(endstop.query_endstop(print_time))
                       for endstop, name in self.endstops]
        return self.states

    def _probe_segment(self, toolhead, speed, distance, deadline):
        self.check()
        if self.printer.get_reactor().monotonic() > deadline:
            raise self.printer.command_error(self.messages['z_bottom_limit'])
        position = toolhead.get_position()
        position[2] = self.maximum - distance
        toolhead.set_position(position, homing_axes='z')
        target = list(position)
        target[2] = self.maximum
        move = homing.HomingMove(self.printer, self.endstops)
        move.homing_move(target, speed, check_triggered=False)
        self.check()
        if self.printer.get_reactor().monotonic() > deadline:
            raise self.printer.command_error(self.messages['z_bottom_limit'])
        steps = {}
        for sp in move.stepper_positions:
            steps[sp.stepper_name] = abs(sp.trig_pos - sp.start_pos)
        return target, self.read_states(toolhead), steps

    def run(self, gcmd):
        if self.active:
            raise gcmd.error(self.messages['z_bottom_state'])
        speed = gcmd.get_float('SPEED', self.quick_speed, above=0.,
                               maxval=self.quick_speed)
        remaining = gcmd.get_float('DISTANCE', self.maximum + self.safe_distance,
                                   above=0., maxval=self.maximum + self.safe_distance)
        released = gcmd.get_int('REQUIRE_RELEASE', 0, minval=0, maxval=1)
        if (not all(math.isfinite(v) and v > 0 for v in (speed, remaining))
                or speed > self.quick_speed
                or remaining > self.maximum + self.safe_distance):
            raise gcmd.error(self.messages['z_bottom_config'])
        self.check()
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.wait_moves()
        if released and any(self.read_states(toolhead)):
            raise gcmd.error(self.messages['z_bottom_limit'])
        self.active = True
        self.force_stop_flag = False
        self.is_already_zodwn = False
        self.cur_retries = 0
        skew_limit = self.zmax_safe_pox_diff if self.zmax_safe_pox_diff > 0 else self.safe_distance
        segment_cap = min(self.safe_distance, skew_limit)
        deadline = self.printer.get_reactor().monotonic() + self.timeout
        try:
            finish_used = False
            while remaining > 0:
                distance = min(remaining, segment_cap)
                target, states, steps = self._probe_segment(toolhead, speed, distance, deadline)
                remaining -= distance
                if any(states) and not all(states):
                    if finish_used:
                        raise gcmd.error(self.messages['z_bottom_limit'])
                    finish_used = True
                    sz = steps.get('stepper_z', 0) * self.steppers['stepper_z'].get_step_dist()
                    sz1 = steps.get('stepper_z1', 0) * self.steppers['stepper_z1'].get_step_dist()
                    skew_mm = abs(sz - sz1)
                    margin = min(remaining, max(0., skew_limit - skew_mm))
                    if margin <= 1.e-6:
                        raise gcmd.error(self.messages['z_bottom_limit'])
                    target, states, steps2 = self._probe_segment(toolhead, speed, margin, deadline)
                    remaining -= margin
                    if not all(states):
                        raise gcmd.error(self.messages['z_bottom_limit'])
                    for k, v in steps2.items():
                        steps[k] = steps.get(k, 0) + v
                if all(states):
                    step_diff = abs(steps.get('stepper_z', 0) - steps.get('stepper_z1', 0))
                    if self.retry_tolerance > 0 and step_diff > self.retry_tolerance:
                        self.cur_retries += 1
                        if self.cur_retries > self.retries:
                            raise gcmd.error(self.messages['z_bottom_limit'])
                        pos = toolhead.get_position()
                        pos[2] = self.maximum
                        toolhead.set_position(pos, homing_axes='z')
                        retract_target = list(pos)
                        retract_target[2] = max(0., self.maximum - self.retract)
                        toolhead.manual_move(retract_target, self.slow_speed)
                        toolhead.wait_moves()
                        remaining = min(self.maximum + self.safe_distance, self.retract * 2.)
                        finish_used = False
                        continue
                    toolhead.set_position(target, homing_axes='z')
                    self.is_already_zodwn = True
                    return
            raise gcmd.error(self.messages['z_bottom_limit'])
        except BaseException:
            try:
                self.printer.lookup_object('stepper_enable').motor_off()
            except Exception:
                logging.exception('Native bottom homing cleanup failed')
            raise
        finally:
            # Bottom switches establish travel, not the nozzle's Z origin.
            toolhead.get_kinematics().clear_homing_state('z')
            self.active = False

    def stop(self, gcmd=None):
        if not self.active:
            logging.info('Z_ALIGN force stop ignored while idle')
            return
        self.force_stop_flag = True
        self.printer.invoke_shutdown(self.messages['z_bottom_stop'])

    def web_stop(self, request):
        self.stop()
        request.send({'result': 'success'})

    cmd_ZDOWN_FORCE_STOP = stop

    def switch(self, gcmd):
        self.zdown_switch_enable = gcmd.get_int('ENABLE', 1, minval=0, maxval=1)
        if self.zdown_switch_enable:
            self.force_stop_flag = False

    def get_status(self, eventtime):
        return {'endstop_pin_status': list(self.states), 'native_endstops': True,
                'is_already_zodwn': self.is_already_zodwn}


def load_config(config):
    return NativeZAlign(config)
