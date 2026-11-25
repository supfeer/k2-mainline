# K2 Plus hardware interfaces on the upstream CoreXY kinematics.
# This file may be distributed under the terms of the GNU GPLv3 license.
import math
from . import corexy


class K2CoreXYKinematics(corexy.CoreXYKinematics):
    def home_z_with_sensorless(self, homing_state, top):
        rail = self.rails[2]
        maximum = rail.get_range()[1]
        if not math.isfinite(top) or not 0 <= top <= maximum:
            raise homing_state.printer.command_error("Invalid K2 sensorless travel")
        if homing_state.stepper_z_sensorless_flag:
            raise homing_state.printer.command_error("K2 sensorless homing already active")
        info = rail.get_homing_info()
        homepos = [None, None, info.position_endstop + top, None]
        forcepos = list(homepos)
        forcepos[2] -= 1.5 * (info.position_endstop - top)
        homing_state.stepper_z_sensorless_flag = True
        try:
            homing_state.home_rails([rail], forcepos, homepos)
        finally:
            homing_state.stepper_z_sensorless_flag = False

    def get_max_z_velocity(self):
        return self.max_z_velocity

    def set_max_z_velocity(self, value):
        self.max_z_velocity = value

    def get_max_z_accel(self):
        return self.max_z_accel

    def set_max_z_accel(self, value):
        self.max_z_accel = value

    def set_z_limit(self, minimum, maximum):
        self.limits[2] = (minimum, maximum)

    def set_limits(self, min_x, max_x, min_y, max_y):
        self.limits[0] = (min_x, max_x)
        self.limits[1] = (min_y, max_y)

    def restore_limits(self):
        for index in (0, 1):
            self.limits[index] = self.rails[index].get_range()

    def note_z_not_homed(self):
        self.clear_homing_state('z')

    def note_xy_not_homed(self):
        self.clear_homing_state('xy')

    def get_status_for_record_z_pos(self):
        return self.limits[2][0] <= self.limits[2][1]


def load_kinematics(toolhead, config):
    return K2CoreXYKinematics(toolhead, config)
