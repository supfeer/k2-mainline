"""Accept preparation cancellation before waiting for the G-code mutex."""
import logging
import math
import re

import greenlet


MESSAGES = {
    'ru': {'cancelled': 'Подготовка печати отменена.',
           'pending': 'Отмена подготовки ещё завершается. Дождитесь остановки.',
           'wait': 'Некорректные параметры ожидания.'},
    'en': {'cancelled': 'Print preparation cancelled.',
           'pending': 'Preparation cancellation is still finishing. Wait for it to stop.',
           'wait': 'Invalid wait parameters.'},
}
CANCEL = re.compile(r'^\s*(?:N\d+\s*)?(?:CANCEL_PRINT|MOTOR_CANCEL_PRINT)(?=\s|;|\*|$)', re.I)
START_COMMANDS = {'START_PRINT', 'SDCARD_PRINT_FILE', 'M23', 'M24', 'RESUME'}


class K2Cancel:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.reactor = self.printer.get_reactor()
        self.messages = config.getsection('k2_print').getchoice('language', MESSAGES, 'ru')
        self.owner = None
        self.requested = False
        self.printer.register_event_handler('klippy:ready', self.ready)

    def get_status(self, eventtime):
        return {'preparing': self.owner is not None, 'cancel_requested': self.requested}

    def check(self):
        if self.requested and greenlet.getcurrent() is self.owner and not self.cleaning():
            logging.info('K2 preparation reached cancellation checkpoint')
            raise self.gcode.error(self.messages['cancelled'])

    def cleaning(self):
        workflow = self.printer.lookup_object('k2_print', None)
        return workflow is not None and workflow.handling_error

    def request(self):
        if self.requested:
            return True
        if self.owner is None:
            return False
        self.requested = True
        self.sd.must_pause_work = True
        self.printer.lookup_object('heaters').turn_off_all_heaters()
        for obj_name, obj in self.printer.lookup_objects('serial_485'):
            cancel_fn = getattr(obj, 'cancel_pending', None)
            if callable(cancel_fn):
                cancel_fn()
        logging.info('K2 preparation cancellation requested before G-code mutex')
        self.reactor.register_callback(self.finish)
        return True

    def finish(self, eventtime):
        # The SD error handler also needs the mutex before it releases its worker.
        mutex = self.gcode.get_mutex()
        deadline = self.reactor.monotonic() + 15.
        while self.owner is not None or self.sd.is_active() or mutex.test():
            if self.printer.is_shutdown():
                self.requested = False
                return
            if self.reactor.monotonic() >= deadline:
                logging.error('K2 preparation cancellation timed out waiting for owner/SD/gcode mutex')
                self.requested = False
                return
            self.reactor.pause(self.reactor.monotonic() + .05)
        try:
            if not self.printer.is_shutdown():
                with mutex:
                    self.sd.do_cancel()
                    command = self.gcode.create_gcode_command('CANCEL_PRINT', 'CANCEL_PRINT', {})
                    self.original['CANCEL_PRINT'](command)
                logging.info('K2 preparation cancellation completed')
        finally:
            self.requested = False

    def guard(self, name, original):
        def execute(gcmd):
            if self.owner is None and not self.requested:
                return original(gcmd)
            if self.requested and name in START_COMMANDS:
                raise self.gcode.error(self.messages['pending'])
            if name in ('CANCEL_PRINT', 'MOTOR_CANCEL_PRINT') and self.request():
                self.check()
                return
            self.check()
            return original(gcmd)
        return execute

    def start(self, original):
        def execute(gcmd):
            if self.requested:
                raise self.gcode.error(self.messages['pending'])
            self.owner = greenlet.getcurrent()
            try:
                return original(gcmd)
            finally:
                self.owner = None
        return execute

    def endpoint(self, original, script=False):
        def handle(request):
            cancel = not script or any(CANCEL.match(line) for line in request.get_str('script').splitlines())
            if cancel and self.request():
                return
            return original(request)
        return handle

    def wait(self, gcmd, busy):
        if self.printer.get_start_args().get('debugoutput') is not None:
            return
        report_at = 0.
        while not self.printer.is_shutdown():
            self.check()
            now = self.reactor.monotonic()
            if not busy(now):
                return
            self.printer.lookup_object('toolhead').get_last_move_time()
            if now >= report_at:
                gcmd.respond_raw(self.printer.lookup_object('heaters')._get_temp(now))
                report_at = now + 1.
            self.reactor.pause(now + .2)

    def heat(self, gcmd, name):
        self.check()
        self.original['M104' if name == 'M109' else 'M140'](gcmd)
        if gcmd.get_float('S', 0.) <= 0:
            return
        if name == 'M190':
            heater = self.printer.lookup_object('heaters').lookup_heater('heater_bed')
        else:
            index = gcmd.get_int('T', None, minval=0)
            extruder = (self.printer.lookup_object('extruder' + (str(index) if index else ''))
                        if index is not None else self.printer.lookup_object('toolhead').get_extruder())
            heater = extruder.get_heater()
        self.wait(gcmd, heater.check_busy)

    def temperature_wait(self, gcmd):
        sensor_name = gcmd.get('SENSOR')
        minimum = gcmd.get_float('MINIMUM', float('-inf'))
        maximum = gcmd.get_float('MAXIMUM', float('inf'), above=minimum)
        if minimum == float('-inf') and maximum == float('inf'):
            return self.original['TEMPERATURE_WAIT'](gcmd)
        heaters = self.printer.lookup_object('heaters')
        sensor = heaters.heaters.get(sensor_name) or self.printer.lookup_object(sensor_name)
        self.wait(gcmd, lambda now: not minimum <= sensor.get_temp(now)[0] <= maximum)

    def dwell(self, gcmd):
        remaining = gcmd.get_float('P', 0., minval=0.) / 1000.
        if not math.isfinite(remaining):
            raise gcmd.error(self.messages['wait'])
        head = self.printer.lookup_object('toolhead')
        if remaining == 0:
            self.check()
            return head.dwell(0.)
        while remaining > 0:
            self.check()
            duration = min(.2, remaining)
            head.dwell(duration)
            remaining -= duration

    def blocking(self, name, original):
        def execute(gcmd):
            if greenlet.getcurrent() is not self.owner or self.cleaning():
                return original(gcmd)
            if name == 'G4':
                return self.dwell(gcmd)
            if name == 'TEMPERATURE_WAIT':
                return self.temperature_wait(self.gcode._get_extended_params(gcmd))
            return self.heat(gcmd, name)
        return execute

    def ready(self):
        self.sd = self.printer.lookup_object('virtual_sdcard')
        self.original = dict(self.gcode.ready_gcode_handlers)
        replacements = {'START_PRINT': self.start(self.original['START_PRINT'])}
        for name in ('G4', 'M109', 'M190', 'TEMPERATURE_WAIT'):
            replacements[name] = self.blocking(name, self.original[name])
        for name, handler in self.original.items():
            if name in self.gcode.base_gcode_handlers:
                continue
            callback = replacements.get(name, self.guard(name, handler))
            # Keep the already parsed native handlers and their help metadata.
            self.gcode.ready_gcode_handlers[name] = callback
        endpoints = self.printer.lookup_object('webhooks')._endpoints
        for name in ('pause_resume/cancel', 'gcode/script'):
            endpoints[name] = self.endpoint(endpoints[name], script=name == 'gcode/script')
        probe = self.printer.lookup_object('probe', None)
        if probe is not None:
            session = probe._probe_session
            run = session.run_probe

            def checked_probe(gcmd):
                self.check()
                result = run(gcmd)
                self.check()
                return result

            session.run_probe = checked_probe
        box = self.printer.lookup_object('box', None)
        if box is not None:
            box.box_action.reactor = CancellableReactor(box.box_action.reactor, self)


class CancellableReactor:
    def __init__(self, reactor, cancellation):
        self.reactor, self.cancellation = reactor, cancellation

    def __getattr__(self, name):
        return getattr(self.reactor, name)

    def pause(self, waketime):
        if greenlet.getcurrent() is not self.cancellation.owner or self.cancellation.cleaning():
            return self.reactor.pause(waketime)
        while True:
            self.cancellation.check()
            deadline = min(waketime, self.reactor.monotonic() + .2)
            now = self.reactor.pause(deadline)
            self.cancellation.check()
            if now < deadline or now >= waketime:
                return now


def load_config(config):
    return K2Cancel(config)
