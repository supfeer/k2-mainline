"""Expose the stock K2 display protocol without changing upstream webhooks."""
import math


class ChamberFanBridge:
    """Expose output_pin fan1 status and SET_PIN PIN=fan1 via single chamber_fan owner."""

    def __init__(self, printer, chamber_fan):
        self.printer = printer
        self.chamber_fan = chamber_fan
        self.manual_override = False
        self.manual_value = 0.
        self._orig_set_tf_speed = chamber_fan.set_tf_speed
        self._orig_set_temp = chamber_fan.set_temp
        chamber_fan.set_tf_speed = self._wrapped_set_tf_speed
        chamber_fan.set_temp = self._wrapped_set_temp
        gcode = printer.lookup_object('gcode')
        gcode.register_mux_command('SET_PIN', 'PIN', 'fan1', self.cmd_SET_PIN,
                                   desc='Set chamber exhaust fan manual speed (0..255)')

    def _wrapped_set_tf_speed(self, read_time, value):
        if self.manual_override:
            return
        self._orig_set_tf_speed(read_time, value)

    def _apply_fan_speed(self, value):
        gcrq = getattr(self.chamber_fan.fan, 'gcrq', None)
        if gcrq is not None and gcrq.toolhead is None:
            self.chamber_fan.fan.last_req_value = value
            return
        self.chamber_fan.fan.set_speed_from_command(value)

    def _wrapped_set_temp(self, degrees):
        self.manual_override = False
        self.manual_value = 0.
        self._orig_set_temp(degrees)
        if degrees <= 0.:
            self._apply_fan_speed(0.)

    def cmd_SET_PIN(self, gcmd):
        raw = gcmd.get_float('VALUE', minval=0., maxval=255.)
        if not math.isfinite(raw):
            raise gcmd.error('Invalid fan1 VALUE')
        normalized = raw / 255.
        self._orig_set_temp(0.)
        self.manual_override = normalized > 0.
        self.manual_value = normalized
        self._apply_fan_speed(normalized)

    def get_status(self, eventtime):
        if self.manual_override:
            return {'value': round(self.manual_value, 4)}
        return {'value': round(self.chamber_fan.fan.last_req_value, 4)}


class K2Display:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.handshakes = 0
        self.last_handshake = None
        legacy = self.printer.lookup_object('gcode_io', None)
        if legacy is not None and not legacy.bytes_read:
            legacy.pipe_is_active = False
        if (config.has_section('temperature_fan chamber_fan')
                and not config.has_section('output_pin fan1')):
            if config.has_section('duplicate_pin_override'):
                self.printer.load_object(config, 'duplicate_pin_override')
            chamber_fan = self.printer.load_object(config, 'temperature_fan chamber_fan')
            self.printer.add_object('output_pin fan1', ChamberFanBridge(self.printer, chamber_fan))
        webhooks = self.printer.lookup_object('webhooks')
        webhooks.register_endpoint('shakehands', self.handle_handshake)
        webhooks.register_endpoint('get_mesh', self.handle_mesh)

    def handle_handshake(self, request):
        message, state = self.printer.get_state_message()
        now = self.printer.get_reactor().monotonic()
        request.send({'state': state, 'state_message': message,
                      'shakehands_id': request.id, 'curtime': now})
        self.handshakes += 1
        self.last_handshake = now

    def handle_mesh(self, request):
        bed_mesh = self.printer.lookup_object('bed_mesh', None)
        now = self.printer.get_reactor().monotonic()
        matrix = ([[]] if bed_mesh is None else
                  bed_mesh.get_status(now)['probed_matrix'])
        request.send({'probed_matrix': matrix})

    def get_status(self, eventtime):
        return {'handshakes': self.handshakes,
                'last_handshake': self.last_handshake}


def load_config(config):
    return K2Display(config)
