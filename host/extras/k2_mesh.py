"""Validate K2 mesh contacts and identify reusable mesh requests."""
import json
import logging
import math
from statistics import median


MESSAGES = {
    'ru': {
        'recheck': 'Перепроверка точки сетки X%.2f Y%.2f после отхода на %.1f мм.',
        'unstable': 'Повторные касания точки сетки не совпали. Очистите сопло и поверхность перед повторной калибровкой.',
        'geometry': 'Нет безопасного пути для перепроверки точки сетки. Проверьте границы перемещений.',
        'state': 'Не удалось подтвердить контекст сетки. Запустите подготовку печати заново.',
    },
    'en': {
        'recheck': 'Rechecking mesh point X%.2f Y%.2f after a %.1f mm side move.',
        'unstable': 'Repeated mesh contacts disagree. Clean the nozzle and surface before recalibrating.',
        'geometry': 'No safe path for the mesh recheck. Check the travel limits.',
        'state': 'The mesh context could not be verified. Start print preparation again.',
    },
}


def outliers(positions, tolerance):
    if len(positions) < 9:
        return []
    values = [(p.bed_x, p.bed_y, p.bed_z) for p in positions]
    if not all(math.isfinite(v) for point in values for v in point):
        raise ValueError('Non-finite mesh position')
    cx, cy, cz = [sum(p[a] for p in values) / len(values) for a in range(3)]
    centered = [(x-cx, y-cy, z-cz) for x, y, z in values]
    xx = sum(x*x for x, y, z in centered)
    yy = sum(y*y for x, y, z in centered)
    xy = sum(x*y for x, y, z in centered)
    xz = sum(x*z for x, y, z in centered)
    yz = sum(y*z for x, y, z in centered)
    determinant = xx*yy - xy*xy
    if determinant <= 1.e-12 * xx * yy:
        raise ValueError('Degenerate mesh geometry')
    sx, sy = (xz*yy-yz*xy)/determinant, (yz*xx-xz*xy)/determinant
    residuals = [z-sx*x-sy*y for x, y, z in centered]
    suspects = []
    for index, (x, y, z) in enumerate(values):
        neighbors = sorted((j for j in range(len(values)) if j != index),
            key=lambda j: (values[j][0]-x)**2 + (values[j][1]-y)**2)[:8]
        deviation = abs(residuals[index] - median(residuals[j] for j in neighbors))
        if deviation > tolerance:
            suspects.append((deviation, index))
    return sorted(suspects, reverse=True)


class K2Mesh:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.messages = MESSAGES[config.getsection('k2_print').get('language', 'ru')]
        self.tolerance = config.getfloat('outlier_tolerance', .3, above=0.)
        self.offset = config.getfloat('recheck_offset', 5., above=0., maxval=10.)
        if not all(math.isfinite(v) for v in (self.tolerance, self.offset)):
            raise config.error(self.messages['geometry'])
        self.request = self.cached_request = self.cached_profile = None
        self.profile_name = 'k2_load_cell'
        self.matches = False
        self.gcmd = None
        self.manual = False
        self.gcode.register_command('K_MESH_CHECK', self.cmd_check)
        self.gcode.register_command('K_MESH_COMMIT', self.cmd_commit)
        self.gcode.register_command('K_MESH_DISCARD_PENDING', self.cmd_discard_pending)
        self.printer.register_event_handler('klippy:connect', self.connect)

    def fail(self, key):
        raise self.gcode.error(self.messages[key])

    def profile(self, name=None):
        mesh = self.printer.lookup_object('bed_mesh')
        profiles = mesh.get_status(self.printer.get_reactor().monotonic())['profiles']
        target = self.profile_name if name is None else name
        return json.dumps(profiles.get(target), sort_keys=True, allow_nan=False)

    def cmd_check(self, gcmd):
        self.matches = False
        params = dict(gcmd.get_command_parameters())
        profile_name = params.pop('PROFILE', '').strip()
        if not profile_name:
            profile_name = 'k2_adaptive' if params.get('ADAPTIVE') == '1' else 'k2_load_cell'
        self.profile_name = profile_name
        for key in ('CALIBRATE', 'PREPARE_ONLY', 'BED_TEMP', 'EXTRUDER_TEMP',
                    'SOAK', 'CHAMBER_TEMP', 'Z_TILT'):
            params.pop(key, None)
        objects = []
        if params.get('ADAPTIVE') == '1':
            exclude = self.printer.lookup_object('exclude_object', None)
            if exclude is not None:
                objects = exclude.get_status(self.printer.get_reactor().monotonic())['objects']
        try:
            self.request = json.dumps([profile_name, params, objects], sort_keys=True, allow_nan=False)
            self.matches = (self.request == self.cached_request
                            and self.profile(profile_name) == self.cached_profile)
        except (TypeError, ValueError):
            self.fail('state')

    def cmd_commit(self, gcmd):
        try:
            profile = self.profile(self.profile_name)
        except (TypeError, ValueError):
            self.fail('state')
        if self.request is None or profile == 'null':
            self.fail('state')
        self.cached_request, self.cached_profile = self.request, profile
        self.matches = True

    def cmd_discard_pending(self, gcmd):
        prof_name = gcmd.get('PROFILE', 'k2_adaptive').strip()
        if not prof_name:
            return
        section = 'bed_mesh ' + prof_name
        autosave = self.printer.lookup_object('configfile').autosave
        if autosave.fileconfig.has_section(section):
            autosave.fileconfig.remove_section(section)
        pending = dict(autosave.status_save_pending)
        if section in pending:
            del pending[section]
            autosave.status_save_pending = pending
        autosave.save_config_pending = bool(autosave.status_save_pending)

    def get_status(self, eventtime):
        return {'matches': self.matches}

    def connect(self):
        from . import probe
        self.average = probe.calc_probe_z_average
        self.probe = self.printer.lookup_object('probe')
        helper = self.printer.lookup_object('bed_mesh').bmc.probe_mgr.probe_helper
        self.helper = helper
        start, finalize = helper.start_probe, helper.finalize_callback

        def start_checked(gcmd):
            self.gcmd = gcmd
            self.manual = gcmd.get('METHOD', 'automatic').lower() == 'manual'
            session = self.probe._probe_session
            owned = session.hw_probe_session is None
            try:
                return start(gcmd)
            except BaseException:
                if owned and session.hw_probe_session is not None:
                    try:
                        session.end_probe_session()
                    except Exception:
                        logging.exception('K2 mesh probe-session cleanup failed')
                raise
            finally:
                self.gcmd = None

        def finalize_checked(positions):
            head = self.printer.lookup_object('toolhead')
            head.wait_moves()
            try:
                checked = positions if self.manual else self.recheck(positions)
            except ValueError:
                self.fail('geometry')
            head.wait_moves()
            return finalize(checked)

        helper.start_probe, helper.finalize_callback = start_checked, finalize_checked

    def nudge(self, index, point, params):
        head = self.printer.lookup_object('toolhead')
        status = head.get_status(self.printer.get_reactor().monotonic())
        current = head.get_position()
        safe_z = max(current[2], self.helper.horizontal_move_z,
                     point.test_z + params['load_cell_retract_dist'])
        if not set('xyz') <= set(status['homed_axes']) or safe_z > status['axis_maximum'][2]:
            self.fail('geometry')
        self.helper._move([None, None, safe_z], params['lift_speed'])
        self.helper._move_next(index)
        target = head.get_position()[:3]
        side = list(target)
        for axis in (0, 1):
            for direction in (1, -1):
                value = target[axis] + direction*self.offset
                if status['axis_minimum'][axis] <= value <= status['axis_maximum'][axis]:
                    side[axis] = value
                    self.helper._move(side, self.helper.speed)
                    self.helper._move(target, self.helper.speed)
                    head.wait_moves()
                    return
        self.fail('geometry')

    def recheck(self, positions):
        checked = positions
        confirmed = set()
        while True:
            suspects = [(d, i) for d, i in outliers(checked, self.tolerance) if i not in confirmed]
            if not suspects:
                return checked
            if self.gcmd is None:
                self.fail('state')
            _, index = suspects[0]
            point = checked[index]
            self.gcmd.respond_info(self.messages['recheck'] % (point.bed_x, point.bed_y, self.offset))
            params = self.probe.get_probe_params(self.gcmd)
            repeats = []
            for unused in range(2):
                self.nudge(index, point, params)
                session = self.probe._probe_session
                session.run_probe(self.gcmd)
                repeats.extend(session.pull_probed_results())
            if (len(repeats) != 2 or not all(math.isfinite(p.bed_z) for p in repeats)
                    or abs(repeats[0].bed_z-repeats[1].bed_z) > params['samples_tolerance']):
                self.fail('unstable')
            replacement = self.average(repeats, 'median')
            logging.info('K2 mesh recheck X%.3f Y%.3f original=%.6f repeats=%s accepted=%.6f',
                         point.bed_x, point.bed_y, point.bed_z,
                         [p.bed_z for p in repeats], replacement.bed_z)
            checked = list(checked)
            checked[index] = replacement
            confirmed.add(index)
            self.helper._raise_tool()
            self.printer.lookup_object('toolhead').wait_moves()


def load_config(config):
    return K2Mesh(config)
