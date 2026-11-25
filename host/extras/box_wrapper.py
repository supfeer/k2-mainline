from extras.auto_addr_wrapper import addr_manager_table_mb
from functools import partial
"""Reconstructed BoxAction methods; the full Box/CFS module is unfinished."""
import json
import logging
import os
import struct
import threading
from extras import k2_native as chelper

from extras.base_info import base_dir
def key_exists(value, key):
    if isinstance(value, dict):
        return key in value or any(key_exists(item, key) for item in value.values())
    if isinstance(value, list):
        return any(key_exists(item, key) for item in value)
    return False

_MATERIAL_DATABASE = {}


def _material_database(stream):
    status = os.fstat(stream.fileno())
    key = (stream.name, status.st_size, status.st_mtime_ns)
    if _MATERIAL_DATABASE.get('key') != key:
        _MATERIAL_DATABASE.clear()
        _MATERIAL_DATABASE.update(key=key, data=json.load(stream))
    return _MATERIAL_DATABASE['data']


def _relay_filament_insert(printer, sensor):
    # Stock filament_switch_sensor announced every insert edge to the CFS load
    # sequence; upstream only updates its runout state.
    helper = sensor.runout_helper
    if getattr(helper, 'k2_cfs_relay', False):
        return
    note = helper.note_filament_present

    def relay(eventtime, is_filament_present):
        # Service moves can keep upstream idle_timeout Printing during a pause.
        if not is_filament_present and printer.lookup_object('pause_resume').is_paused:
            helper.filament_present = is_filament_present
        else:
            note(eventtime, is_filament_present)
        if is_filament_present:
            printer.send_event('box:extrude_process_stage7')
    helper.note_filament_present = relay
    helper.k2_cfs_relay = True


def _query_measuring_wheel(reactor, box_action, addr):
    # Upstream statistics callbacks may not pause; the stock stats polled the
    # CFS synchronously, so the RS485 round trip runs in its own greenlet.
    if getattr(box_action, 'k2_wheel_query', False):
        return
    box_action.k2_wheel_query = True
    box_action.k2_wheel_query_started = None

    def query(eventtime):
        try:
            if not box_action.Tn_action_flag:
                box_action.k2_wheel_query_started = eventtime
                if box_action.communication_measuring_wheel(addr, 'GET') is True:
                    box_action.k2_wheel_sample = (addr, reactor.monotonic())
        finally:
            box_action.k2_wheel_query = False
    reactor.register_callback(query)

_LAST_SLOT_PATH = os.path.join(base_dir, 'creality/userdata/box/k2_last_slot.json')


def _load_last_slot(path=None):
    try:
        with open(path or _LAST_SLOT_PATH) as stream:
            slot = json.load(stream).get('last_cmd')
    except (OSError, ValueError, AttributeError):
        return None
    return slot if isinstance(slot, str) else None


def _save_last_slot(slot, path=None):
    path = path or _LAST_SLOT_PATH
    try:
        with open(path + '.tmp', 'w') as stream:
            json.dump({'last_cmd': slot}, stream)
        os.replace(path + '.tmp', path)
    except OSError as err:
        logging.warning('K2 cannot save the loaded CFS slot: %s', err)


class _LastSlot:
    # Stock unloaded the CFS at every print end, so last_cmd only lived in
    # memory. This port keeps filament loaded and must survive restarts; the
    # constructor's initial None is not saved.
    def __init__(self, name):
        self.key = '_k2_' + name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return instance.__dict__.get(self.key)

    def __set__(self, instance, slot):
        restored = self.key in instance.__dict__
        instance.__dict__[self.key] = slot
        if restored:
            _save_last_slot(slot)


def _restore_last_slot(action):
    # Trust the saved slot only while the toolhead holds filament and the CFS
    # still reports that slot in its connection; otherwise forget it.
    save = action.box_save
    slot = save.last_cmd or _load_last_slot()
    if slot is None:
        return
    content = action.box_state.Tnn_content.get(slot)
    valid = False
    if (content is not None and action.get_filament_sensor_detect() is True
            and action.check_connect(content['addr'])):
        try:
            action.communication_get_filament_sensor_state(content['addr'], 'CONNECTIONS')
        except action.gcode.error:
            pass
        else:
            mask = action.box_state.get_Tn_inner_data(
                content['addr'], 'filament_sensor', subpart='connections')
            valid = isinstance(mask, int) and bool(mask & (1 << 'ABCD'.index(content['num'])))
    if not valid:
        save.last_cmd_tmp = slot
    save.last_cmd = slot if valid else None
    logging.info('K2 CFS slot %s %s after restart', slot, 'restored' if valid else 'forgotten')


def _same_slot_print_mode(action, content):
    # The stock same-slot shortcut ran inside one print with the CFS already in
    # PRINT mode; after a restart the slot is idle and would not feed.
    return action.communication_set_box_mode(content['addr'], 'PRINT', content['num']) is not False

def _cfs_tail_slot(action):
    save = action.box_save
    slot = save.last_cmd or _load_last_slot() or save.last_cmd_tmp
    if (slot is None or not action.box_state.get_Tn_data(0, 0, uppart='enable')
            or action.get_filament_sensor_detect() is not True):
        return None
    content = action.box_state.get_Tnn_content(slot)
    addr, bit = content['addr'], 1 << 'ABCD'.index(content['num'])
    if not action.check_connect(addr):
        return None
    policy = action.printer.lookup_object('k2_print')
    for other in range(1, 5):
        if not action.check_connect(other):
            continue
        if action.communication_get_filament_sensor_state(other, 'CONNECTIONS') is not True:
            policy.fail('cfs_tail')
        mask = action.box_state.get_Tn_inner_data(other, 'filament_sensor', subpart='connections')
        if type(mask) is not int or not 0 <= mask <= 15:
            policy.fail('cfs_tail')
        if mask & ~(bit if other == addr else 0):
            return None
    connections = action.box_state.get_Tn_inner_data(addr, 'filament_sensor', subpart='connections')
    if connections == 0:
        return slot
    if action.communication_get_filament_sensor_state(addr, 'MATERIAL') is not True:
        policy.fail('cfs_tail')
    material = action.box_state.get_Tn_inner_data(addr, 'filament_sensor', subpart='material')
    if type(material) is not int or not 0 <= material <= 15:
        policy.fail('cfs_tail')
    return slot if material & bit == 0 else None


def _drain_cfs_tail(action, slot, **recode_err):
    policy = action.printer.lookup_object('k2_print')
    def require(result):
        if result is not True:
            policy.fail('cfs_tail')
    require(action.box_save.error in (None, 'filament_err'))
    addr = action.box_state.get_Tnn_content(slot)['addr']
    connections = action.box_state.get_Tn_inner_data(addr, 'filament_sensor', subpart='connections')
    previous = action.Tn_action_flag
    preload = action.box_save.preloading_en
    action.Tn_action_flag = True
    complete = False
    try:
        if preload:
            require(action.communication_set_pre_loading(addr, 0, force=True))
        require(action.communication_tighten_up_enable(addr, 'DISABLE'))
        # Empty channel sensors do not mean the tail has cleared the shared hub.
        num = action.box_state.get_Tnn_content(slot)['num']
        require(action.communication_set_box_mode(addr, 'PRINT', num))
        action.box_save.last_cmd = slot
        logging.info('K2 CFS drain tail slot=%s connections=%s', slot, connections)
        recode_err.setdefault('err', 'z_err_extrusion_all_materials_err')
        recode_err.setdefault('last_tnn', slot)
        recode_err.setdefault('vtnn', slot)
        require(action._flush_ending_material(**recode_err))
        require(action.get_filament_sensor_detect() is False)
        complete = True
        logging.info('K2 CFS drain tail slot=%s complete; head sensor clear', slot)
    finally:
        try:
            require(action.communication_set_box_mode(addr, 'IDLE'))
            if preload:
                require(action.communication_set_pre_loading(addr, 1, force=True))
            if complete:
                require(action.communication_tighten_up_enable(addr, 'ENABLE'))
        except Exception:
            if complete:
                raise
            logging.exception('K2 CFS tail cleanup failed for %s', slot)
        finally:
            action.Tn_action_flag = previous
            if not complete:
                try:
                    action.set_cool_temp()
                except Exception:
                    logging.exception('K2 CFS tail cooling failed for %s', slot)


def _manual_cfs_extrude(rack, gcmd, heat_only=False):
    box = rack.box
    action = box.box_action
    if not box.box_state.get_Tn_data(0, 0, uppart='enable'):
        return False
    remembered = action.box_save.last_cmd or _load_last_slot()
    if remembered is None:
        return False
    slot = remembered
    policy = box.printer.lookup_object('k2_print')

    def require(result, phase):
        if result is not True:
            logging.error('K2 CFS manual extrusion slot=%s failed at %s', slot, phase)
            policy.fail('cfs_manual')

    require(action.print_stats.state != 'printing' and not action.Tn_action_flag
            and action.box_save.error is None and not box.box_state.extrude_err,
            'ready')
    content = box.box_state.get_Tnn_content(slot)
    addr = content['addr']
    require(action.get_filament_sensor_detect() is True and action.check_connect(addr),
            'loaded filament')
    require(action.communication_get_filament_sensor_state(addr, 'CONNECTIONS'),
            'connection query')
    mask = box.box_state.get_Tn_inner_data(addr, 'filament_sensor', subpart='connections')
    require(type(mask) is int and mask in (0, 1 << 'ABCD'.index(content['num'])),
            'loaded slot')
    if _cfs_tail_slot(action) == slot:
        require(action.communication_get_box_state(addr), 'tail state query')
        require(box.box_state.get_Tn_data(addr, 'mode') in ('0', '2'), 'tail CFS mode')
        temperature = action.get_material_target_temp(type=action.remain_material.type)
        logging.info('K2 CFS manual tail slot=%s heat_only=%s temperature=%s',
                     slot, heat_only, temperature)
        if heat_only:
            action.quickly_wait_heating(temperature)
            return True
        _drain_cfs_tail(action, slot)
        try:
            vtnn = next((name for name, loaded in box.box_state.Tnn_map.items()
                         if loaded == slot), None)
            require(vtnn is not None, 'tail slot mapping')
            action.quickly_wait_heating(action.get_material_target_temp(tnn=slot))
            require(box.Tn_action(vtnn), 'load after tail')
        finally:
            try:
                require(action.communication_set_box_mode(addr, 'IDLE'), 'finish tail reload')
            finally:
                action.set_cool_temp()
        return True
    require(type(mask) is int and mask == 1 << 'ABCD'.index(content['num']),
            'loaded slot')
    if action.box_save.last_cmd is None:
        action.box_save.last_cmd = slot
    if heat_only:
        action.quickly_wait_heating(action.get_material_target_temp(tnn=slot))
        return True
    require(action.communication_get_box_state(addr), 'state query')
    mode = box.box_state.get_Tn_data(addr, 'mode')
    require(mode in ('0', '2'), 'idle or print mode')
    action.Tn_action_flag = True
    failed = True
    try:
        if mode == '0' and action.box_save.preloading_en:
            require(action.communication_set_pre_loading(addr, 0, force=True), 'close preload')
        require(action.communication_tighten_up_enable(addr, 'DISABLE'), 'disable tightening')
        require(action.communication_set_box_mode(addr, 'PRINT', content['num']), 'print mode')
        logging.info('K2 CFS manual extrusion slot=%s started from mode=%s', slot, mode)
        # This is a same-filament purge, not a material change with a longer flush.
        velocity, percent = action.get_flush_velocity(slot, slot)
        temperature = action.get_material_target_temp(tnn=slot)
        maximum = action.get_material_target_max_temp(tnn=slot)
        require(action.material_flush(rack.extrude, velocity, temperature, maximum, percent, slot),
                'flush')
        action.remain_material.save(
            color=box.box_state.get_Tn_data(addr, 'color_value', content['num']),
            type=box.box_state.get_Tn_data(addr, 'material_type', content['num']))
        failed = False
    except Exception:
        logging.exception('K2 CFS manual extrusion failed for %s', slot)
        raise
    finally:
        try:
            if mode == '0':
                require(action.communication_set_box_mode(addr, 'IDLE'), 'restore idle')
                if action.box_save.preloading_en:
                    require(action.communication_set_pre_loading(addr, 1, force=True), 'restore preload')
                require(action.communication_tighten_up_enable(addr, 'ENABLE'), 'restore tightening')
        except Exception:
            if not failed:
                raise
            logging.exception('K2 CFS manual extrusion cleanup failed for %s', slot)
        finally:
            action.Tn_action_flag = False
    logging.info('K2 CFS manual extrusion slot=%s complete, previous mode=%s', slot, mode)
    return True

import json
import math

# CFS 113 starts its PRINT-mode feed motor only on a buffer sensor edge and
# checks the odometer only while that motor runs. A missed edge leaves the
# motor stopped with no SPEED/ENWIND error; SET_BOX_MODE PRINT re-reads the
# buffer level and restarts it.
_FEED_CHECK_LENGTH = 35.   # normal bursts reach 25-30 mm; confirm with fresh sensors
_FEED_STALL_LENGTH = 60.   # preserve the larger allowance while the buffer is full
_FEED_WHEEL_STEP = 2.      # wheel travel in mm that counts as feeding
_FEED_JUMP = 20.           # extruder position jump in mm treated as a reset
_FEED_OBSERVATION_TIME = 10.  # stock CFS allows about 10 seconds to refill the buffer
_FEED_SETTLE_TIME = 5.


class _FeedWatch:
    def __init__(self):
        self.reference = None
        self.last = None
        self.busy = False
        self.limit = _FEED_CHECK_LENGTH
        self.started = None
        self.session = None
        self.detected = None
        self.detected_e = None
        self.buffer_time = None
        self.sd_pause_requested = False


def _feed_event(box, watch, phase, slot=None, addr=None):
    # Diagnostic reads are cached/local only. Never poll RS485 or flush moves.
    try:
        now = box.reactor.monotonic()
        toolhead = box.printer.lookup_object('toolhead')
        sd = box.printer.lookup_object('virtual_sdcard', None)
        gcode_move = box.printer.lookup_object('gcode_move', None)
        motor = box.printer.lookup_object('motor_control', None)
        estimated = box.printer.lookup_object('mcu').estimated_print_time(now)
        queued = getattr(toolhead, 'print_time', None)
        sample = getattr(box.box_action, 'k2_wheel_sample', None)
        reference = watch.reference
        position = _extruded(box, now)
        target = getattr(gcode_move, 'last_position', ())
        commanded = getattr(toolhead, 'commanded_pos', ())
        target_e = target[3] if len(target) > 3 else None
        queued_e = commanded[3] if len(commanded) > 3 else None
        in_command = False if sd is None else getattr(sd, 'cmd_from_sd', False)
        data = {
            'phase': phase, 'time': now, 'slot': slot, 'actual_e': position,
            'static_e': None if reference is None else position - reference[1],
            'static_seconds': None if watch.started is None else now - watch.started,
            'detection_seconds': None if watch.detected is None else now - watch.detected,
            'detection_e': watch.detected_e,
            'wheel': None if addr is None else _wheel(box, addr),
            'reference_wheel': None if reference is None else reference[2],
            'wheel_sample_age': (now - sample[1] if sample and sample[0] == addr else None),
            'wheel_query_pending': getattr(box.box_action, 'k2_wheel_query', False),
            'wheel_query_started': getattr(box.box_action, 'k2_wheel_query_started', None),
            'buffer': (None if addr is None else
                       box.box_state.get_Tn_inner_data(addr, None, uppart='buffer')),
            'buffer_sample_age': None if watch.buffer_time is None else now - watch.buffer_time,
            'state': box.box_action.print_stats.state,
            'cfs_error': box.box_action.box_save.error,
            'sd_active': None if sd is None else sd.is_active(),
            'sd_position': None if sd is None else sd.file_position,
            'sd_next_position': None if sd is None else getattr(sd, 'next_file_position', None),
            'sd_command_active': in_command,
            'sd_pause_requested': None if sd is None else sd.must_pause_work,
            'queued_horizon': None if queued is None else max(0., queued - estimated),
            # A meshed G1 may still add segments after the SD pause request.
            # These positions expose its unqueued E separately from the queue.
            'gcode_target_e': target_e, 'queued_e': queued_e,
            'inflight_e': (target_e - queued_e if in_command
                           and target_e is not None and queued_e is not None else None),
            'lookahead_moves': len(getattr(getattr(toolhead, 'lookahead', None), 'queue', ())),
            'motor_errors_cached': None if motor is None else getattr(motor, 'motor_error_code', None),
            'motor_ready_cached': None if motor is None else getattr(motor, 'is_ready', None),
        }
        logging.warning('K2 CFS feed diagnostic %s', json.dumps(data, sort_keys=True))
    except Exception:
        # A missing optional diagnostic must not delay the actual pause.
        logging.exception('K2 CFS feed diagnostic failed at %s', phase)


def _pause_feed_sd(box, watch, slot=None, addr=None):
    _feed_event(box, watch, 'sd_pause_request', slot, addr)
    # Like upstream filament_runout: stop new SD commands before waiting for
    # the G-code mutex or running cleanup. Already queued motion drains normally.
    box.printer.lookup_object('pause_resume').send_pause_command()
    watch.sd_pause_requested = True
    _feed_event(box, watch, 'sd_pause_sent', slot, addr)


def _wheel(box, addr):
    value = box.box_state.get_Tn_data(addr, 'measuring_wheel')
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _extruded(box, eventtime):
    print_time = box.printer.lookup_object('mcu').estimated_print_time(eventtime)
    position = box.printer.lookup_object('toolhead').get_extruder().find_past_position(print_time)
    if not math.isfinite(position):
        raise ValueError('Extruder position is not finite')
    return position


def _watch_feed(box, eventtime, slot, content):
    # Runs inside the statistics timer: an exception there stops Klipper.
    watch = box.__dict__.setdefault('k2_feed_watch', _FeedWatch())
    if watch.busy:
        return
    try:
        _check_feed(box, watch, eventtime, slot, content)
    except Exception:
        watch.busy = True
        logging.exception('K2 CFS feed watch failed; requesting pause')
        box.reactor.register_callback(lambda et: _feed_watch_failed(box, watch))


def _feed_watch_failed(box, watch):
    try:
        state = box.box_action.print_stats.state
        if state != 'printing' and not (state == 'paused' and watch.sd_pause_requested):
            return
        _pause_feed_sd(box, watch)
        with box.gcode.get_mutex():
            if box.box_action.print_stats.state in ('printing', 'paused'):
                box.gcode.run_script_from_command('PAUSE')
                box.gcode.respond_info(
                    box.printer.lookup_object('k2_print').messages['feed_watch'])
    except Exception:
        logging.exception('K2 CFS feed watch pause failed')
    finally:
        watch.reference = None
        watch.busy = False


def _check_feed(box, watch, eventtime, slot, content):
    action = box.box_action
    stats = action.print_stats
    # Runout may consume the remaining tail, but must retain feed protection.
    if (stats.state != 'printing' or action.Tn_action_flag
            or action.box_save.error not in (None, 'filament_err')):
        watch.reference = watch.last = None
        return
    wheel = _wheel(box, content['addr'])
    extruded = _extruded(box, eventtime)
    last, watch.last = watch.last, extruded
    reference = watch.reference
    session = (stats.print_start_time, stats.prev_pause_duration)
    if (last is None or abs(extruded - last) > _FEED_JUMP
            or reference is None or reference[0] != slot or watch.session != session
            or (wheel is not None and (reference[2] is None
                or abs(reference[2] - wheel) >= _FEED_WHEEL_STEP))):
        logging.info('K2 CFS feed reference time=%.6f slot=%s actual_e=%.6f '
                     'wheel=%s previous=%s since=%s session=%s',
                     eventtime, slot, extruded, wheel, reference, watch.started, session)
        watch.reference = (slot, extruded, wheel)
        watch.limit = _FEED_CHECK_LENGTH
        watch.started = eventtime
        watch.session = session
        watch.detected = None
        watch.detected_e = None
        watch.buffer_time = None
        watch.sd_pause_requested = False
        return
    if extruded - reference[1] < watch.limit:
        return
    watch.busy = True
    watch.detected = eventtime
    watch.detected_e = extruded
    box.reactor.register_callback(lambda et: _recover_feed(box, watch, slot, content, reference))


def _recover_feed(box, watch, slot, content, reference):
    action = box.box_action
    addr = content['addr']
    stats = action.print_stats
    deadline = watch.started + _FEED_OBSERVATION_TIME
    keep_reference = False

    def active():
        return (stats.state == 'printing' and not action.Tn_action_flag
                and (stats.print_start_time, stats.prev_pause_duration) == watch.session
                and action.box_save.last_cmd == slot
                and action.box_save.error in (None, 'filament_err'))

    def moved():
        if action.communication_measuring_wheel(addr, 'GET') is not True:
            raise ValueError('CFS measuring wheel query failed')
        action.k2_wheel_sample = (addr, box.reactor.monotonic())
        wheel = _wheel(box, addr)
        if wheel is None or reference[2] is None:
            raise ValueError('CFS measuring wheel is unavailable')
        return abs(reference[2] - wheel) >= _FEED_WHEEL_STEP
    def read_buffer():
        if action.communication_get_buffer_state(addr) is not True:
            raise ValueError('CFS buffer query failed')
        watch.buffer_time = box.reactor.monotonic()
        buffer = box.box_state.get_Tn_inner_data(addr, None, uppart='buffer')
        if buffer not in (0, 1, 2):
            raise ValueError('CFS buffer state is invalid')
        return buffer

    try:
        if not active():
            return
        _feed_event(box, watch, 'confirm_start', slot, addr)
        if moved():
            _feed_event(box, watch, 'fresh_wheel_moved', slot, addr)
            return
        if not active():
            return
        buffer = read_buffer()
        if not active():
            return
        if buffer != 2 and box.reactor.monotonic() < deadline:
            _feed_event(box, watch, 'observing_refill', slot, addr)
            box.reactor.pause(deadline)
            if not active():
                return
            buffer = read_buffer()
            if not active():
                return
            if moved():
                _feed_event(box, watch, 'feed_recovered_during_observation', slot, addr)
                return
        if not active():
            return
        if buffer == 1 and _extruded(box, box.reactor.monotonic()) - reference[1] < _FEED_STALL_LENGTH:
            watch.limit = _FEED_STALL_LENGTH
            keep_reference = True
            _feed_event(box, watch, 'full_buffer_allowance', slot, addr)
            return
        if buffer == 2 and action.box_save.error != 'filament_err':
            with box.gcode.get_mutex():
                if not active():
                    return
                if action.communication_set_box_mode(addr, 'PRINT', content['num']) is not True:
                    raise ValueError('CFS PRINT mode command failed')
            box.reactor.pause(max(deadline, box.reactor.monotonic() + _FEED_SETTLE_TIME))
            if not active():
                return
            if moved():
                logging.info('K2 CFS %s feed restarted by SET_BOX_MODE PRINT', slot)
                return
        if active():
            _feed_event(box, watch, 'stall_confirmed', slot, addr)
            logging.warning('K2 CFS %s feed stalled: wheel static for %.1f mm of extrusion,'
                            ' buffer state %s', slot, _extruded(box, box.reactor.monotonic())
                            - reference[1], buffer)
            # Preserve the original print context for the vendor PAUSE path,
            # even when do_pause() changes print_stats to paused while yielding.
            if action.box_save.error is None:
                action.box_save.recode_err('empty_print', tnn=slot)
            failure = action.box_save.error
            message = (key_filament_err % (addr, content['num'])
                       if failure == 'filament_err' else key_speed_err)
            _pause_feed_sd(box, watch, slot, addr)
            with box.gcode.get_mutex():
                if (action.print_stats.state not in ('printing', 'paused')
                        or action.box_save.last_cmd != slot
                        or action.box_save.error != failure):
                    return
                _feed_event(box, watch, 'error_start', slot, addr)
                logging.warning('K2 CFS %s feed did not resume; pausing the print', slot)
                error(message, box.gcode, box.reactor, 'printing',
                      action.box_save)
                _feed_event(box, watch, 'error_done', slot, addr)
    except Exception:
        logging.exception('K2 CFS feed watch failed')
        if active() or watch.sd_pause_requested:
            _feed_watch_failed(box, watch)
    finally:
        if not keep_reference:
            watch.reference = None
        watch.busy = False


BOX_NUM_POS = 1


DATA_POS = 5
LEN_POS = 2
CMD_POS = 4
ADDR_POS = 1
TIMEOUT_MAX = 3600
key_params_err = "!! {\"code\":\"key834\", \"msg\":\"params error, send data: %s\", \"values\": []}"
key_state_err = "!! {\"code\":\"key840\", \"msg\":\"box switch state error, check it's current action, addr: %d, cmd: 0x%x\", \"values\": []}"
key_extrude_err1 = "!! {\"code\":\"key835\", \"msg\":\"extrude error, maybe it's blocked at the connections\", \"values\": [%d, \"%s\"]}"
key_extrude_err4 = "!! {\"code\":\"key838\", \"msg\":\"extrude error, through the connections but not extrude\", \"values\": [%d, \"%s\"]}"
key_joint_err = "!! {\"code\":\"key844\", \"msg\":\"the pneumatic joint is abnormal and may collapse\", \"values\": []}"
key_retrude_err6 = "!! {\"code\":\"key865\", \"msg\":\"retrude error, failed to exit connections\", \"values\": [%d, \"%s\"]}"
key_retrude_err1 = "!! {\"code\":\"key849\", \"msg\":\"retrude error, failed to exit connections\", \"values\": [%d, \"%s\"]}"
key_retrude_err2 = "!! {\"code\":\"key850\", \"msg\":\"retrude error, unspecified retrude, multiple connections triggered, addr: %d\", \"values\": []}"
key_filament_err = "!! {\"code\":\"key839\", \"msg\":\"filament error, no filament detected at box extrude position\", \"values\": [%d, \"%s\"]}"
key_speed_err = "!! {\"code\":\"key846\", \"msg\":\"empty printing, box speed is smaller than extruder\", \"values\": []}"
key_enwind_err = "!! {\"code\":\"key847\", \"msg\":\"empty printing, material enwind\", \"values\": []}"
key_load_err = "!! {\"code\":\"key857\", \"msg\":\"motor load error\", \"values\": []}"
key_rfid_err = "!! {\"code\":\"key843\", \"msg\":\"rfid is error, get rfid: %s\", \"values\": [%d, \"%s\"]}"
key_dry_and_humidity_err = "!! {\"code\":\"key853\", \"msg\":\"humidity sensor error\", \"values\": [%d]}"
key_eeprom_err = "!! {\"code\":\"key858\", \"msg\":\"errprom error\", \"values\": [%d]}"
key_measuring_wheel_err = "!! {\"code\":\"key859\", \"msg\":\"measuring wheel error\", \"values\": [%d]}"
key_buffer_err = "!! {\"code\":\"key860\", \"msg\":\"buffer error\", \"values\": []}"
key_left_rfid_card_err = "!! {\"code\":\"key861\", \"msg\":\"left rfid card error\", \"values\": [%d]}"
key_right_rfid_card_err = "!! {\"code\":\"key862\", \"msg\":\"right rfid card error\", \"values\": [%d]}"


class ParseData:
    def __init__(self):
        pass

    def parse_num_string_to_byte(self, data):
        byte_string = []
        for c in data:
            byte_string.append(int(c))
        return bytes(byte_string)

    def parse_num_to_byte(self, data):
        return bytes([data])

    def get_key_from_value(self, dict, v):
        for key, value in dict.items():
            if value == v:
                return key

    def get_cmd_num(self, data_send, return_method):
        num_map_char = ["A", "B", "C", "D"]
        num_map_num = [1, 2, 3, 4]
        cmd_num = data_send[DATA_POS - 1]
        num = []
        for i in range(4):
            if cmd_num & (1 << i):
                if return_method == "CHAR":
                    num.append(num_map_char[i])
                elif return_method == "NUM":
                    num.append(num_map_num[i])
        return num

    def get_rfid(self, msg):
        num_map = ["A", "B", "C", "D"]
        length = msg[LEN_POS]
        data_pos = DATA_POS
        num = None
        ret = {}
        while data_pos < length + 2:
            rfid = []
            if chr(msg[data_pos]) == ":":
                num = chr(msg[data_pos - 1])
                head = data_pos + 1
                tail = None
                while chr(msg[data_pos]) != ";":
                    data_pos += 1
                    if data_pos > length + 2:
                        return None
                tail = data_pos
                rfid = "".join(chr(c) for c in msg[head:tail])
                if num not in num_map:
                    continue
                ret[num] = rfid
            data_pos += 1
        logging.info("ret: %s" % ret)
        return ret

    def get_remain_len(self, data_send, msg):
        num_char = self.get_cmd_num(data_send, "CHAR")
        num_num = self.get_cmd_num(data_send, "NUM")
        data_msg = msg[DATA_POS:DATA_POS + 4]
        remain_len_values = []
        for i in num_num:
            remain_len_values.append(str(data_msg[i - 1]))
        return dict(zip(num_char, remain_len_values))

    def get_measuring_wheel(self, data_send, msg):
        sub_cmd_msg = data_send[DATA_POS - 1]
        logging.debug("sub_cmd_msg: %d" % sub_cmd_msg)
        if sub_cmd_msg == 1:
            data_hex = ((msg[DATA_POS] << 24)
                        + (msg[DATA_POS + 1] << 16)
                        + (msg[DATA_POS + 2] << 8)
                        + msg[DATA_POS + 3])
            data = struct.unpack("<f", struct.pack("<I", data_hex))[0]
            values = (data_hex, data)
            logging.debug("[get_measuring_wheel] data_hex: 0x%x, data:%s" % values)
            return data
        return 0


class CutSensor:
    def __init__(self, config, switch_pin):
        self.printer = config.get_printer()
        buttons = self.printer.load_object(config, "buttons")
        self.gcode = config.get_printer().lookup_object("gcode")
        self.switch_pin = switch_pin
        self.cut_present = False
        self.cut_return_failed = 0
        self.cut_succeed_num = 0
        self.cut_release_failed = 0
        self.cut_release_succeed = 0
        if self.switch_pin:
            register_buttons = buttons.register_buttons
            register_buttons([self.switch_pin], self._button_handler)
        else:
            self.gcode.respond_info("[box] do not define 'switch_pin' for cutting")

    def _button_handler(self, eventtime, state):
        if state == self.cut_present:
            return
        self.cut_present = state
        if state:
            self.printer.send_event("v_sd:update_cut_used")

    def state(self):
        if self.cut_present:
            self.gcode.respond_info("[box] cut sensor detected")
            return True
        self.gcode.respond_info("[box] cut sensor not detected")
        return False


class BoxCfg:
    def __init__(self, config):
        self.bus = config.get("bus", None)
        if self.bus is None:
            raise config.error(
                "the bus of box is not configured, it is the name of serial_485, "
                "such as [serial_485 serial485] and bus: serial485")
        self.has_extrude_pos = config.getint("has_extrude_pos", 0, minval=0)
        self.filament_sensor = config.get("filament_sensor", None)
        if self.has_extrude_pos != 1:
            self.has_extrude_pos = 0
        self.version = config.getint("version", default=0, minval=0, maxval=1)
        min_x = config.getsection("stepper_x").getfloat("position_min")
        max_x = config.getsection("stepper_x").getfloat("position_max")
        min_y = config.getsection("stepper_y").getfloat("position_min")
        max_y = config.getsection("stepper_y").getfloat("position_max")
        velocity_max = config.getsection("printer").getfloat("max_velocity")
        self.box_extrude_retry_num = config.getint(
            "box_extrude_retry_num", default=0, minval=0, maxval=5)
        self.extrude_err_auto_retry_set = config.getint(
            "extrude_err_auto_retry_set", default=1, minval=0, maxval=1)
        self.box_first_clean_length = config.getint(
            "box_first_clean_length", default=90, minval=0, maxval=600)
        getint = config.getint
        self.box_need_clean_length = getint(
            "box_need_clean_length", default=80, minval=0,
            maxval=self.box_first_clean_length)
        self.box_need_clean_length_max = config.getint(
            "box_need_clean_length_max", default=100, minval=0, maxval=600)
        self.pre_cut_pos_x = config.getfloat(
            "pre_cut_pos_x", default=0, minval=min_x, maxval=max_x)
        self.pre_cut_pos_y = config.getfloat(
            "pre_cut_pos_y", default=0, minval=min_y, maxval=max_y)
        self.cut_pos_x = config.getfloat(
            "cut_pos_x", default=-6.5, minval=min_x, maxval=max_x)
        self.pre_cut_cal_pos_x = config.getfloat(
            "pre_cut_cal_pos_x", default=-5, minval=min_x, maxval=max_x)
        self.check_cut_pos_x_max = config.getfloat(
            "check_cut_pos_x_max", default=-5, minval=min_x, maxval=max_x)
        self.check_cut_pos_x_min = config.getfloat(
            "check_cut_pos_x_min", default=-10, minval=min_x, maxval=max_x)
        self.cut_pos_y = config.getfloat(
            "cut_pos_y", default=0, minval=min_y, maxval=max_y)
        self.cut_velocity = config.getfloat(
            "cut_velocity", default=6000, minval=1200, maxval=velocity_max * 60)
        self.cut_push_rod = config.getfloat("cut_push_rod", default=5.5)
        self.Tn_retrude = config.getfloat(
            "Tn_retrude", default=-60, minval=-120, maxval=-1)
        self.Tn_retrude_velocity = config.getintlist(
            "Tn_retrude_velocity", default=(360,))
        self.Tn_extrude_temp = config.getfloat(
            "Tn_extrude_temp", default=220, minval=180, maxval=300)
        self.nozzle_volume = config.getfloat(
            "nozzle_volume", default=183, minval=0, maxval=300)
        self.max_temp_flush_times = config.getint(
            "max_temp_flush_times", default=3, minval=0, maxval=10)
        self.diff_material_type_min_flush_len = config.getfloat(
            "diff_material_type_min_flush_len", default=370, minval=300, maxval=1000)
        self.flush_multiplier = config.getfloat(
            "flush_multiplier", default=1, minval=0, maxval=10)
        self.flush_velocity_factor = config.getfloat(
            "flush_velocity_factor", default=1.2, minval=1, maxval=5)
        self.Tn_extrude = config.getfloat(
            "Tn_extrude", default=140, minval=0, maxval=200)
        self.Tn_extrude_percent = config.getintlist(
            "Tn_extrude_percent", default=(1,))
        self.Tn_extrude_velocity = config.getintlist(
            "Tn_extrude_velocity", default=(360,))
        if len(self.Tn_extrude_percent) != len(self.Tn_extrude_velocity):
            error_factory = config.error
            lengths = (self.Tn_extrude_percent, self.Tn_extrude_velocity)
            raise error_factory(
                "Tn_extrude_percent[%s] and Tn_extrude_velocity[%s] mismatch." % lengths)
        self.buffer_empty_len = config.getfloat(
            "buffer_empty_len", default=30, minval=0, maxval=60)
        self.diff_length = config.getfloat(
            "diff_length", default=-10, minval=-30, maxval=30)
        self.extrude_material_len_for_box = config.getfloat(
            "extrude_material_len_for_box", default=3, minval=0, maxval=60)
        self.extrude_material_len_for_extruder = config.getfloat(
            "extrude_material_len_for_extruder", default=9, minval=0, maxval=60)
        self.extrude_material_velocity = config.getfloat(
            "extrude_material_velocity", default=180)
        self.extrude_material_times = config.getint("extrude_material_times", default=6)
        self.max_tube_length = config.getint("max_tube_length", default=3000)
        self.clean_left_pos_x = 0
        self.clean_left_pos_y = 0
        self.clean_right_pos_x = 0
        self.clean_right_pos_y = 0
        self.clean_velocity = 0
        self.extrude_pos_x = 0
        self.extrude_pos_y = 0
        self.clean_pos_min_x = 0
        self.clean_pos_min_y = 0
        self.clean_pos_max_x = 0
        self.clean_pos_max_y = 0
        if self.has_extrude_pos == 1:
            logging.info("machine has extrude pos")
            self.clean_left_pos_x = config.getfloat(
                "clean_left_pos_x", default=150, minval=min_x, maxval=max_x)
            self.clean_left_pos_y = config.getfloat(
                "clean_left_pos_y", default=368, minval=min_y, maxval=max_y)
            self.clean_right_pos_x = config.getfloat(
                "clean_right_pos_x", default=170, minval=min_x, maxval=max_x)
            self.clean_right_pos_y = config.getfloat(
                "clean_right_pos_y", default=368, minval=min_y, maxval=max_y)
            self.clean_velocity = config.getfloat(
                "clean_velocity", default=6000, minval=1200, maxval=velocity_max * 60)
            self.extrude_pos_x = config.getfloat(
                "extrude_pos_x", default=127, minval=min_x, maxval=max_x)
            self.extrude_pos_y = config.getfloat(
                "extrude_pos_y", default=368, minval=min_y, maxval=max_y)
            self.clean_pos_min_x = config.getfloat(
                "clean_pos_min_x", default=152, minval=min_x, maxval=max_x)
            self.clean_pos_min_y = config.getfloat(
                "clean_pos_min_y", default=362, minval=min_y, maxval=max_y)
            self.clean_pos_max_x = config.getfloat(
                "clean_pos_max_x", default=156, minval=min_x, maxval=max_x)
            self.clean_pos_max_y = config.getfloat(
                "clean_pos_max_y", default=368, minval=min_y, maxval=max_y)
            self.clean_pos_left = config.getfloat(
                "clean_pos_left_x", default=157, minval=min_x, maxval=max_x)
            self.clean_pos_right = config.getfloat(
                "clean_pos_right_x", default=172, minval=min_x, maxval=max_x)
            self.clean_pos_middle_y = config.getfloat(
                "clean_pos_middle_y", default=374, minval=min_y, maxval=max_y)
            self.safe_pos_y = config.getfloat(
                "safe_pos_y", default=350, minval=min_y, maxval=max_y)
            self.safe_pos_x = config.getfloat(
                "safe_pos_x", default=205, minval=min_x, maxval=max_x)
            logging.info("self.clean_left_pos_x: %s" % self.clean_left_pos_x)
            logging.info("self.clean_left_pos_y: %s" % self.clean_left_pos_y)
            logging.info("self.clean_right_pos_x: %s" % self.clean_right_pos_x)
            logging.info("self.clean_right_pos_y: %s" % self.clean_right_pos_y)
            logging.info("self.clean_velocity: %s" % self.clean_velocity)
            logging.info("self.extrude_pos_x: %s" % self.extrude_pos_x)
            logging.info("self.extrude_pos_y: %s" % self.extrude_pos_y)
            logging.info("self.clean_pos_min_x: %s" % self.clean_pos_min_x)
            logging.info("self.clean_pos_min_y: %s" % self.clean_pos_min_y)
            logging.info("self.clean_pos_max_x: %s" % self.clean_pos_max_x)
            logging.info("self.clean_pos_max_y: %s" % self.clean_pos_max_y)
            logging.info("self.cut_pos_x: %s" % self.cut_pos_x)
            logging.info("self.cut_pos_y: %s" % self.cut_pos_y)


class BoxState:
    def __init__(self):
        self.Tnn_content = {}
        self.Tn_map = {}
        self.Tnn_map = {}
        self.Tn_data = {}
        self.data_parts = [
            "state", "filament", "temperature", "dry_and_humidity",
            "filament_detected", "measuring_wheel", "version", "sn", "mode",
            "vender", "remain_len", "color_value", "material_type", "uuid",
            "change_color_num",
        ]
        self.data_upper_parts = [
            "filament", "state", "auto_refill", "enable", "filament_useup",
            "same_material",
        ]
        self.Tn_inner_data = {}
        self.inner_data_parts = ["filament_sensor"]
        self.inner_data_upper_parts = ["buffer"]
        self.tn_save_data_path = os.path.join(
            base_dir, "creality/userdata/box/tn_data.json")
        self.tn_save_data = None
        self.tn_save_data_parts = [
            "vender", "remain_len", "color_value", "material_type", "change_color_num",
        ]
        self.save_parts = ["base_data", "last_cmd", "enable", "tnn_map", "last_cmd"]
        self.is_continue_print = False
        self.material_database = os.path.join(
            base_dir, "creality/userdata/box/material_database.json")
        self.extrude_err = False
        self.resume_flag = False
        self.need_update_power_loss_info = True
        self.lock = threading.Lock()
        self.state_init()

    def generate_Tnn_content(self):
        for addr in range(1, 5):
            for num in ("A", "B", "C", "D"):
                tnn = "T" + str(addr) + num
                data = {"addr": addr, "num": num}
                self.Tnn_content[tnn] = data

    def generate_Tn_map(self):
        items = []
        for addr in ("1", "2", "3", "4"):
            for num in ("A", "B", "C", "D"):
                tnn = "T" + addr + num
                items.append(tnn)
        for i in range(16):
            tn = "T" + str(i)
            self.Tn_map[tn] = items[i]

    def generate_Tnn_map(self):
        items = []
        for addr in ("1", "2", "3", "4"):
            for num in ("A", "B", "C", "D"):
                tnn = "T" + addr + num
                items.append(tnn)
        for i in range(16):
            self.Tnn_map[items[i]] = items[i]

    def generate_Tn_data(self):
        self.Tn_data["filament"] = 1
        self.Tn_data["state"] = "None"
        self.Tn_data["auto_refill"] = 1
        self.Tn_data["enable"] = 1
        self.Tn_data["filament_useup"] = 1
        self.Tn_data["same_material"] = []
        self.Tn_data["map"] = self.Tnn_map
        for addr in range(1, 5):
            tn = "T" + str(addr)
            self.Tn_data[tn] = {}
            for i in range(len(self.data_parts)):
                if (self.data_parts[i] == "vender"
                        or self.data_parts[i] == "remain_len"
                        or self.data_parts[i] == "color_value"
                        or self.data_parts[i] == "material_type"
                        or self.data_parts[i] == "change_color_num"):
                    self.Tn_data[tn][self.data_parts[i]] = []
                    for j in range(4):
                        self.Tn_data[tn][self.data_parts[i]].append("-1")
                elif self.data_parts[i] == "state":
                    self.Tn_data[tn][self.data_parts[i]] = "None"
                elif self.data_parts[i] == "version":
                    self.Tn_data[tn][self.data_parts[i]] = "-1"
                elif self.data_parts[i] == "sn":
                    self.Tn_data[tn][self.data_parts[i]] = "-1"
                elif self.data_parts[i] == "mode":
                    self.Tn_data[tn][self.data_parts[i]] = "-1"
                else:
                    self.Tn_data[tn][self.data_parts[i]] = "None"

    def generate_Tn_inner_data(self):
        for addr in range(1, 5):
            tn = "T" + str(addr)
            self.Tn_inner_data["buffer"] = None
            self.Tn_inner_data[tn] = {}
            for i in range(len(self.inner_data_parts)):
                if self.inner_data_parts[i] == "filament_sensor":
                    self.Tn_inner_data[tn]["filament_sensor"] = {}
                    self.Tn_inner_data[tn]["filament_sensor"]["material"] = 0
                    self.Tn_inner_data[tn]["filament_sensor"]["connections"] = 0
                elif (self.inner_data_parts[i] == "vender"
                      or self.inner_data_parts[i] == "remain_len"):
                    self.Tn_inner_data[tn][self.inner_data_parts[i]] = []
                    for j in range(4):
                        self.Tn_inner_data[tn][self.inner_data_parts[i]].append("-1")

    def generate_tn_save_data(self):
        data = {}
        for addr in range(1, 5):
            tn = "T" + str(addr)
            data[tn] = {}
            for part in self.tn_save_data_parts:
                data[tn][part] = []
                for i in range(4):
                    data[tn][part].append("-1")
        return data

    def state_init(self):
        self.generate_Tnn_content()
        self.generate_Tn_map()
        self.generate_Tnn_map()
        self.generate_Tn_data()
        self.generate_Tn_inner_data()
        data = {}
        if os.path.exists(self.tn_save_data_path):
            try:
                with open(self.tn_save_data_path, "r") as stream:
                    data = json.load(stream)
            except Exception as error:
                logging.error(error)
        if data and "base_data" in data:
            if key_exists(data, "change_color_num"):
                self.tn_save_data = data.get("base_data", {})
                return
        self.tn_save_data = self.generate_tn_save_data()
        data["base_data"] = self.tn_save_data
        try:
            with open(self.tn_save_data_path, "w") as stream:
                json.dump(data, stream)
        except Exception as error:
            logging.error(error)

    def sync_tn_data(self, tn, material_status):
        for i in range(4):
            if material_status & (1 << i):
                if self.tn_save_data[tn]["vender"][i] == "-1":
                    self.Tn_data[tn]["vender"][i] = "unknown"
                    if self.tn_save_data[tn]["color_value"][i] != "-1":
                        self.Tn_data[tn]["color_value"][i] = self.tn_save_data[tn]["color_value"][i]
                        self.Tn_data[tn]["material_type"][i] = self.tn_save_data[tn]["material_type"][i]
                else:
                    self.Tn_data[tn]["vender"][i] = self.tn_save_data[tn]["vender"][i]
                    self.Tn_data[tn]["remain_len"][i] = self.tn_save_data[tn]["remain_len"][i]
                    self.Tn_data[tn]["color_value"][i] = self.tn_save_data[tn]["color_value"][i]
                    self.Tn_data[tn]["material_type"][i] = self.tn_save_data[tn]["material_type"][i]
                    self.Tn_data[tn]["change_color_num"][i] = self.tn_save_data[tn].setdefault(
                        "change_color_num", [-1, -1, -1, -1])[i]
            else:
                self.Tn_data[tn]["vender"][i] = "none"

    def modify_Tn_data(self, addr, data, part, num=None, uppart=None, cmd=False):
        num_map = {"A": 0, "B": 1, "C": 2, "D": 3}
        if uppart is not None and uppart in self.data_upper_parts:
            self.Tn_data[uppart] = data
            return
        if addr < 1 or addr > 4:
            warning = logging.warning
            warning("addr[%d] is not in range(0~3)" % addr)
            return
        tn = "T" + str(addr)
        if part not in self.data_parts:
            logging.warning("change Tn data error, has no part")
            return
        if (part == "vender" or part == "remain_len" or part == "color_value"
                or part == "material_type" or part == "change_color_num"):
            if num not in num_map:
                warning = logging.warning
                warning("change data[%s], but do not has num" % part)
                return
            self.Tn_data[tn][part][num_map[num]] = data
            if data != "none":
                self.modify_tn_save_data(addr, data, part, num)
                if cmd is True:
                    self.update_tn_save_data("base_data", save=True)
        else:
            self.Tn_data[tn][part] = data

    def modify_Tn_inner_data(self, addr, data, part, num=None, subpart=None, uppart=None):
        if uppart is not None and uppart in self.inner_data_upper_parts:
            self.Tn_inner_data[uppart] = data
            info = logging.info
            fields = (uppart, data)
            info("Tn_inner_data[%s] = %s" % fields)
            return
        if addr < 1 or addr > 4:
            warning = logging.warning
            warning("addr[%d] is not in range(0~3)" % addr)
            return
        tn = "T" + str(addr)
        if part not in self.inner_data_parts:
            warning = logging.warning
            warning("change Tn inner data error, has no part(%s)" % part)
            return
        if part == "filament_sensor":
            subparts = ["material", "connections"]
            if subpart not in subparts:
                warning = logging.warning
                fields = (subpart, subparts)
                warning("subpart error, %s is not in range: %s" % fields)
                return
            self.Tn_inner_data[tn][part][subpart] = data

    def get_tn_save_data(self, addr, part, num):
        num_map = {"A": 0, "B": 1, "C": 2, "D": 3}
        if addr < 1 or addr > 4:
            warning = logging.warning
            warning("addr[%d] is not in range(0~3)" % addr)
            return
        tn = "T" + str(addr)
        if part not in self.tn_save_data_parts:
            logging.warning("get Tn inner data error, has no part")
            return
        return self.tn_save_data[tn][part][num_map[num]]

    def modify_tn_save_data(self, addr, data, part, num):
        num_map = {"A": 0, "B": 1, "C": 2, "D": 3}
        if addr < 1 or addr > 4:
            warning = logging.warning
            warning("addr[%d] is not in range(0~3)" % addr)
            return
        tn = "T" + str(addr)
        if part not in self.tn_save_data_parts:
            warning = logging.warning
            warning("change tn save data error, has no part(%s)" % part)
            return
        self.tn_save_data[tn][part][num_map[num]] = data

    def update_tn_save_data(self, part, enable=None, tnn_map=None, last_cmd=None, save=False):
        if part not in self.save_parts:
            warning = logging.warning
            warning("part[%s] is invalid" % part)
            return
        data = {}
        try:
            with self.lock:
                with open(self.tn_save_data_path, "r") as stream:
                    data = json.load(stream)
        except Exception as error:
            logging.error(error)
        if part == "base_data":
            data["base_data"] = self.tn_save_data
        elif part == "last_cmd":
            data["last_cmd"] = last_cmd
        elif part == "enable":
            data["enable"] = enable
        elif part == "tnn_map":
            data["tnn_map"] = tnn_map
        info = logging.info
        fields = (part, enable, tnn_map, last_cmd, data)
        info("part: %s, enable: %s, tnn_map: %s, last_cmd: %s, data: %s" % fields)
        if save:
            info = logging.info
            info("save data: %s" % str(data))
            try:
                with self.lock:
                    with open(self.tn_save_data_path, "w") as stream:
                        json.dump(data, stream)
            except Exception as error:
                logging.error(error)

    def update_same_material_list(self):
        parts = {"material_type": 0, "color_value": 1, "tnn": 2, "material_name": 3}
        self.Tn_data["same_material"] = []
        same = self.Tn_data["same_material"]
        invalid = ["-1", "none", "unknown"]
        for tnn, content in self.Tnn_content.items():
            get_data = self.get_Tn_data
            material_type = get_data(content["addr"], "material_type", content["num"])
            get_data = self.get_Tn_data
            color_value = get_data(content["addr"], "color_value", content["num"])
            get_data = self.get_Tn_data
            vender = get_data(content["addr"], "vender", content["num"])
            if vender == "none" or vender == "-1":
                continue
            if material_type in invalid or color_value in invalid:
                continue
            for i in range(len(same)):
                if same[i][parts["material_type"]] == material_type:
                    if same[i][parts["color_value"]].lower() == color_value.lower():
                        if tnn not in same[i][parts["tnn"]]:
                            same[i][parts["tnn"]].append(tnn)
                            break
            else:
                material_name = ""
                material_id = material_type[1:6]
                info = logging.info
                info("material type id: %s" % material_id)
                try:
                    if os.path.exists(self.material_database):
                        with open(self.material_database, "r") as stream:
                            data = _material_database(stream)
                            materials = data.get("result", {}).get("list", [])
                            for i in range(len(materials)):
                                item_id = materials[i].get("base", {}).get("id", None)
                                item_name = materials[i].get("base", {}).get("meterialType", None)
                                if item_id is not None and item_name is not None:
                                    if item_id == material_id:
                                        material_name = item_name
                                        break
                except Exception as error:
                    log_error = logging.error
                    log_error("material database get material name: %s" % error)
                same.append([material_type, color_value, [tnn], material_name])

    def get_Tn_data(self, addr, part, num=None, uppart=None):
        num_map = {"A": 0, "B": 1, "C": 2, "D": 3}
        if uppart is not None and uppart in self.data_upper_parts:
            if uppart != "state":
                info = logging.info
                fields = (uppart, self.Tn_data[uppart])
                info("Tn_data[%s]: %s" % fields)
            return self.Tn_data[uppart]
        if part not in self.data_parts:
            logging.warning("get Tn data error, has no part")
            return
        if addr < 1 or addr > 4:
            warning = logging.warning
            warning("addr[%d] is not in range(0~3)" % addr)
            return
        tn = "T" + str(addr)
        if (part == "vender" or part == "remain_len" or part == "color_value"
                or part == "material_type" or part == "change_color_num"):
            if num not in num_map:
                warning = logging.warning
                warning("change data[%s], but do not has num" % part)
                return
            info = logging.info
            fields = (tn, part, num_map[num], self.Tn_data[tn][part][num_map[num]])
            info("Tn_data[%s][%s][%s]: %s" % fields)
            return self.Tn_data[tn][part][num_map[num]]
        if part != "state":
            emit = logging.debug if part == "measuring_wheel" else logging.info
            fields = (tn, part, self.Tn_data[tn][part])
            emit("Tn_data[%s][%s]: %s" % fields)
        return self.Tn_data[tn][part]

    def get_Tn_inner_data(self, addr, part, num=None, subpart=None, uppart=None):
        # The native interface accepts num but does not use it.
        if uppart is not None and uppart in self.inner_data_upper_parts:
            info = logging.info
            fields = (uppart, self.Tn_inner_data[uppart])
            info("Tn_inner_data[%s]: %s" % fields)
            return self.Tn_inner_data[uppart]
        if addr < 1 or addr > 4:
            warning = logging.warning
            warning("addr[%d] is not in range(0~3)" % addr)
            return
        tn = "T" + str(addr)
        if part not in self.inner_data_parts:
            logging.warning("get Tn inner data error, has no part")
            return
        if part == "filament_sensor":
            subparts = ["material", "connections"]
            if subpart not in subparts:
                warning = logging.warning
                fields = (subpart, subparts)
                warning("subpart error, %s is not in range: %s" % fields)
                return
            info = logging.info
            fields = (tn, part, subpart, self.Tn_inner_data[tn][part][subpart])
            info("Tn_inner_data[%s][%s][%s]: %s" % fields)
            return self.Tn_inner_data[tn][part][subpart]

    def get_box_mode(self, addr):
        return self.get_Tn_data(addr, "mode")

    def get_Tnn_map(self):
        return self.Tnn_map

    def get_Tnn_content(self, tnn):
        return self.Tnn_content[tnn]

    def e_err_set(self):
        self.extrude_err = True

    def clear_e_err(self):
        self.extrude_err = False

    def clear_color_count(self):
        for i in range(1, 5):
            if self.Tn_data["T" + str(i)].get("change_color_num"):
                update = self.Tn_data["T" + str(i)].update
                update({"change_color_num": [-1] * len(
                    self.Tn_data["T" + str(i)]["change_color_num"])})


class BoxSave:
    def __init__(self, config):
        self.last_cmd = None
        self.last_cmd_tmp = None
        self.config = config
        self.gcode = config.get_printer().lookup_object("gcode")
        self.fan0_last_value = None
        self.fan2_last_value = None
        self.output_pin_fan0 = None
        self.output_pin_fan2 = None
        self.move_z = 0.0
        self.max_velocity = None
        self.max_accel = None
        self.speed_factor = None
        self.error = None
        self.error_list = [
            "cut_err", "retrude_err", "box_extrude_err", "extruder_extrude_err",
            "state_err", "filament_err", "empty_print", "flush_err", "print_end_err",
            "e_err_cut_err", "z_err_extrusion_all_materials_err",
            "macro_extrusion_all_materials_err", "print_end_move_to_cut_err",
            "e_err_tn_extrude_err"]
        self.macro_error_list = [
            "macro_cut_err", "macro_retrude_err", "macro_box_extrude_err",
            "macro_extruder_extrude_err", "macro_flush_err"]
        self.cr_break_err_list = [
            "cr_break_ending_flush_err", "cr_break_cut_err", "cr_break_retrude_err",
            "cr_break_box_extrude_err", "cr_break_extruder_extrude_err",
            "cr_break_flush_err"]
        self.error_tnn = {"last_tnn": None, "tnn": None, "addr": None, "vtnn": None}
        self.error_next_macro = []
        self.last_err = None
        self.preloading_en = None
        self.preloading_poweron = None
        self.material_auto_refill_flag = False
        self.box_filament_err = False
        self.resume_tnn = None

    def save_fan(self):
        if self.output_pin_fan0 is not None:
            if self.fan0_last_value is None:
                self.fan0_last_value = self.output_pin_fan0.last_value * self.output_pin_fan0.scale
            self.gcode.run_script_from_command("SET_PIN PIN=fan0 VALUE=0.0")
        if self.output_pin_fan2 is not None:
            if self.output_pin_fan2 is not None and self.fan2_last_value is None:
                self.fan2_last_value = self.output_pin_fan2.last_value * self.output_pin_fan2.scale
            self.gcode.run_script_from_command("SET_PIN PIN=fan2 VALUE=0.0")

    def restore_fan(self):
        if self.output_pin_fan0 is not None:
            if self.fan0_last_value is not None:
                self.gcode.run_script_from_command("SET_PIN PIN=fan0 VALUE=%.2f" % self.fan0_last_value)
            self.fan0_last_value = None
        if self.output_pin_fan2 is not None:
            if self.fan2_last_value is not None:
                self.gcode.run_script_from_command("SET_PIN PIN=fan2 VALUE=%.2f" % self.fan2_last_value)
            self.fan2_last_value = None

    def save_printer_accel(self, velocity, accel, speed_factor):
        self.max_velocity = velocity
        self.max_accel = accel
        self.speed_factor = speed_factor * 6000
        self.gcode.run_script_from_command("M220 S100")

    def restore_printer_accel(self):
        if self.max_velocity and self.max_accel:
            _bind(self.gcode, 'run_script_from_command')("G0 F%.2f" % self.max_velocity)
            _bind(self.gcode, 'run_script_from_command')("M204 S%d" % self.max_accel)
            _bind(self.gcode, 'run_script_from_command')("M220 S%s" % self.speed_factor)
            self.max_velocity = None
            self.max_accel = None
            self.speed_factor = None

    def recode_err(self, err, last_tnn=None, tnn=None, addr=None, vtnn=None,
                   filament_sensor_enable=True):
        if (err not in self.error_list and err not in self.macro_error_list
                and err not in self.cr_break_err_list):
            warning = logging.warning
            values = (err, self.error_list)
            warning("err[%s] not in error_list[%s]" % values)
            return
        if self.error is None:
            self.error = err
            if last_tnn:
                self.error_tnn["last_tnn"] = last_tnn
            if tnn:
                self.error_tnn["tnn"] = tnn
            if addr:
                self.error_tnn["addr"] = addr
            if vtnn:
                self.error_tnn["vtnn"] = vtnn
            if err not in self.macro_error_list:
                if tnn:
                    self.save_resume_tnn(tnn)
            info = logging.info
            values = (err, last_tnn, tnn, addr)
            info("error: %s, last_tnn: %s, tnn: %s, addr: %s" % values)
            if err == "filament_err" and filament_sensor_enable:
                self.gcode.run_script_from_command(
                    "SET_FILAMENT_SENSOR SENSOR=filament_sensor ENABLE=1")
        else:
            warning = logging.warning
            values = (err, self.error)
            warning("generate new error[%s], last error[%s] do not clear" % values)

    def clear_err(self):
        self.error = None
        self.last_err = None
        self.error_next_macro = []
        self.save_err_tnn()

    def get_err(self):
        return self.error

    def save_err_tnn(self, last_tnn=None, tnn=None, addr=None, vtnn=None):
        self.error_tnn["last_tnn"] = last_tnn
        self.error_tnn["tnn"] = tnn
        self.error_tnn["addr"] = addr
        self.error_tnn["vtnn"] = vtnn
        info = logging.info
        values = (last_tnn, tnn, addr, vtnn)
        info("last_tnn: %s, tnn: %s, addr: %s, vtnn: %s" % values)

    def find_objs(self):
        for index in (0, 2):
            try:
                pin = self.config.get_printer().lookup_object("output_pin fan%d" % index)
            except Exception:
                pin = None
                logging.info("[box] do not define 'output_pin fan%d'", index)
            setattr(self, "output_pin_fan%d" % index, pin)

    def save_resume_tnn(self, tnn):
        self.resume_tnn = tnn

    def clear_resume_tnn(self):
        self.resume_tnn = None




Box_cmd = {'CREATE_CONNECT': b'\x01',
 'GET_RFID': b'\x02',
 'GET_REMAIN_LEN': b'\x03',
 'SET_BOX_MODE': b'\x04',
 'GET_BUFFER_STATE': b'\x05',
 'CTRL_MATERIAL_MOTOR_ACTION': b'\x06',
 'CTRL_CONNECTION_MOTOR_ACTION': b'\x07',
 'GET_FILAMENT_SENSOR_STATE': b'\x08',
 'SET_MOTOR_SPEED': b'\t',
 'GET_BOX_STATE': b'\n',
 'GET_BOX_MODE': b'\x0c',
 'SET_PRE_LOADING': b'\r',
 'MEASURING_WHEEL': b'\x0e',
 'TIGHTEN_UP_ENABLE': b'\x0f',
 'EXTRUDE_PROCESS': b'\x10',
 'RETRUDE_PROCESS': b'\x11',
 'EXTRUDE_PROCESS_MODEL2': b'\x13',
 'GET_VERSION_SN': b'\x14',
 'GET_HARDWARE_STATUS': b'\x15',
 'COMMUNICATION_TEST': b'U'}

cmd_timeout = {'CREATE_CONNECT': 2,
 'GET_RFID': 2,
 'GET_REMAIN_LEN': 2,
 'SET_BOX_MODE': 2,
 'GET_BUFFER_STATE': 2,
 'CTRL_MATERIAL_MOTOR_ACTION': 2,
 'CTRL_CONNECTION_MOTOR_ACTION': 2,
 'GET_FILAMENT_SENSOR_STATE': 2,
 'SET_MOTOR_SPEED': 2,
 'GET_BOX_STATE': 3600,
 'GET_BOX_MODE': 2,
 'SET_PRE_LOADING': 300,
 'MEASURING_WHEEL': 2,
 'EXTRUDE_PROCESS': 15,
 'RETRUDE_PROCESS': 150,
 'EXTRUDE_PROCESS_MODEL2': 150,
 'GET_VERSION_SN': 2,
 'GET_HARDWARE_STATUS': 2,
 'TIGHTEN_UP_ENABLE': 2}

key_timeout = '!! {"code":"key831", "msg":"serial_485 communication timeout", "values": [%d]}'


def _send_box_query(owner, addr, command_name):
    command = Box_cmd[command_name]
    send_data = owner.send_data
    try:
        parse_number = owner.parse.parse_num_to_byte
        try:
            address = parse_number(addr)
        finally:
            del parse_number
        try:
            return send_data(address, command, cmd_timeout[command_name])
        finally:
            del address
    finally:
        del send_data


state = {
    'OK': 0, 'PARAMS_ERR': 1, 'CRC_ERR': 2, 'STATE_ERR': 3, 'LENGTH_ERR': 4,
    'EXTRUDE_ERR1': 5, 'EXTRUDE_ERR4': 8, 'EXTRUDE_ERR5': 9,
    'EXTRUDE_ERR6': 10, 'EXTRUDE_ERR7': 11, 'EXTRUDE_ERR8': 12,
    'EXTRUDE_ERR9': 14, 'EXTRUDE_ERR10': 13, 'RETRUDE_ERR1': 19,
    'RETRUDE_ERR2': 20, 'RETRUDE_ERR3': 21, 'RETRUDE_ERR4': 22,
    'RETRUDE_ERR5': 23, 'RETRUDE_ERR6': 25, 'RETRUDE_ERR7': 26,
    'MOTOR_LOAD_ERR': 34, 'UPDATE_STATE': 48, 'FILAMENT_ERR': 80,
    'SPEED_ERR': 81, 'ENWIND_ERR': 82,
}
mode = {'IDLE': '0', 'PRELOADING': '1', 'PRINTING': '2', 'WRAPPERING': '3', 'ERR': '4', 'TEST': '5'}


def _motor_packet_length(owner, cmd, data, has_data):
    parse_number = owner.parse.parse_num_to_byte
    try:
        if has_data:
            return parse_number(len(cmd) + len(data) + 2)
        return parse_number(len(cmd) + 2)
    finally:
        del parse_number


def _bind(receiver, name):
    return getattr(receiver, name)


def _rich_ne(left, right):
    return left != right


TIMEOUT_SHORT_TIME = 2

def _send_box_command(owner, addr, command_name):
    command = Box_cmd[command_name]
    send = owner.send_data
    try:
        parse_number = _bind(owner.parse, 'parse_num_to_byte')
        try:
            address = parse_number(addr)
        finally:
            del parse_number
    except:
        del send
        raise
    try:
        timeout = cmd_timeout[command_name]
    except:
        del send, address
        raise
    try:
        return send(address, command, timeout)
    finally:
        del address, timeout, send



class BoxAction:

    def communication_create_connect(self, addr):
        return _send_box_command(self, addr, 'CREATE_CONNECT')


    def communication_get_version_sn(self, addr):
        return _send_box_command(self, addr, 'GET_VERSION_SN')


    def communication_get_box_mode(self, addr):
        return _send_box_command(self, addr, 'GET_BOX_MODE')


    def communication_get_rfid(self, addr, num=15):
        num &= 15
        data = _bind(self.parse, 'parse_num_to_byte')(num)
        try:
            cmd = Box_cmd['GET_RFID']
            send = self.send_data
            try:
                address = _bind(self.parse, 'parse_num_to_byte')(addr)
            except:
                del send
                raise
            try:
                arguments = (address, cmd, cmd_timeout['GET_RFID'])
            except:
                del address, send
                raise
            del address
            try:
                return send(*arguments, data=data)
            finally:
                del send, arguments
        finally:
            del data


    def communication_get_remain_len(self, addr, num=15):
        num &= 15
        data = _bind(self.parse, 'parse_num_to_byte')(num)
        try:
            cmd = Box_cmd['GET_REMAIN_LEN']
            send = self.send_data
            try:
                address = _bind(self.parse, 'parse_num_to_byte')(addr)
            except:
                del send
                raise
            try:
                arguments = (address, cmd, cmd_timeout['GET_REMAIN_LEN'])
            except:
                del address, send
                raise
            del address
            try:
                return send(*arguments, data=data)
            finally:
                del send, arguments
        finally:
            del data


    def communication_set_box_mode(self, addr, m, num='0'):
        num_map = {'A': b'\x01', 'B': b'\x02', 'C': b'\x04', 'D': b'\x08', '0': b'\x00'}
        mode_map = {'PRINT': b'\x00', 'IDLE': b'\x01'}
        cmd = Box_cmd['SET_BOX_MODE']
        send = self.send_data
        try:
            address = _bind(self.parse, 'parse_num_to_byte')(addr)
        except:
            del send
            raise
        try:
            arguments = (address, cmd, cmd_timeout['SET_BOX_MODE'])
        except:
            del address, send
            raise
        del address
        try:
            return send(*arguments, data=num_map[num] + mode_map[m])
        finally:
            del send, arguments


    def communication_tighten_up_enable(self, addr, enable):
        cmd = Box_cmd['TIGHTEN_UP_ENABLE']
        enable_map = {'ENABLE': 0, 'DISABLE': 1}
        data = _bind(self.parse, 'parse_num_to_byte')(enable_map[enable])
        try:
            send = self.send_data
            try:
                address = _bind(self.parse, 'parse_num_to_byte')(addr)
            except:
                del send
                raise
            try:
                arguments = (address, cmd, cmd_timeout['TIGHTEN_UP_ENABLE'])
            except:
                del send, address
                raise
            del address
            try:
                return send(*arguments, data=data)
            finally:
                del send, arguments
        finally:
            del data


    def communication_set_pre_loading(self, addr, action, num=15, force=False):
        if (self.print_stats.state == 'printing' or self.print_stats.state == 'paused') and force is False:
            return None
        num &= 15
        cmd = Box_cmd['SET_PRE_LOADING']
        num_data = _bind(self.parse, 'parse_num_to_byte')(num)
        action_data = None
        try:
            action_data = _bind(self.parse, 'parse_num_to_byte')(action)
            count = 0
            for index in range(4):
                if num & (1 << index):
                    count += 1
            if action == 0 or action == 1:
                send = self.send_data
                try:
                    address = _bind(self.parse, 'parse_num_to_byte')(addr)
                except:
                    del send
                    raise
                try:
                    arguments = (address, cmd, TIMEOUT_SHORT_TIME)
                except:
                    del send, address
                    raise
                del address
                try:
                    return send(*arguments, data=num_data + action_data)
                finally:
                    del send, arguments
            if _bind(self, 'get_filament_sensor_detect')():
                return None
            send = self.send_data
            try:
                address = _bind(self.parse, 'parse_num_to_byte')(addr)
            except:
                del send
                raise
            try:
                arguments = (address, cmd, count * cmd_timeout['SET_PRE_LOADING'])
            except:
                del send, address
                raise
            del address
            try:
                return send(*arguments, data=num_data + action_data)
            finally:
                del send, arguments
        finally:
            del num_data, action_data

    def _is_transport_uncertain(self):
        return bool(getattr(self, '_transport_uncertain_latched', False)) or getattr(self, '_last_send_status', None) in ('TIMEOUT', 'CRC_ERR', 'LENGTH_ERR', None)

    def send_data(self, addr, cmd, timeout, state=b'\xff', data=b'', report_err=True, _sensor_receipt=None, retries_en=None):
        num_map = {1: 'A', 2: 'B', 4: 'C', 8: 'D', 0: '0', 15: '0'}
        non_idempotent = cmd in (
            Box_cmd['EXTRUDE_PROCESS'],
            Box_cmd['RETRUDE_PROCESS'],
            Box_cmd['EXTRUDE_PROCESS_MODEL2'],
        )
        if retries_en is None:
            retries_en = not non_idempotent
        self._last_send_status = None
        if cmd == Box_cmd['EXTRUDE_PROCESS']:
            self.extrude_process_ret_state = None
        if len(data) == 0:
            data_send = addr + _bind(self.parse, 'parse_num_to_byte')(len(cmd) + 2) + state + cmd
        else:
            data_send = addr + _bind(self.parse, 'parse_num_to_byte')(len(cmd) + len(data) + 2) + state + cmd + data
        send_data_string = _bind(' ', 'join')((hex(value) for value in data_send))
        _bind(logging, 'debug')('data_send: %s' % send_data_string)
        times = (1 if timeout >= TIMEOUT_MAX else 5) if retries_en else 0
        while True:
            ret = _bind(self._serial, 'cmd_send_data_with_response')(data_send, timeout, retries_en=retries_en)
            if cmd == Box_cmd['COMMUNICATION_TEST']:
                self._last_send_status = 'OK' if ret else 'TIMEOUT'
                return ret[DATA_POS] if ret else None
            if ret is None:
                self._last_send_status = 'TIMEOUT'
                if non_idempotent:
                    self._transport_uncertain_latched = True
                if cmd == Box_cmd['EXTRUDE_PROCESS']:
                    self.extrude_process_ret_state = 'TIMEOUT'
                if cmd == Box_cmd['GET_BOX_STATE']:
                    return None
                _bind(self, 'timeout_process')(_bind(int, 'from_bytes')(addr, byteorder='big'))
                return False
            if _sensor_receipt is None:
                ret_state = _bind(self, 'ret_parse_process')(data_send, ret)
            else:
                ret_state = _bind(self, 'ret_parse_process')(
                    data_send, ret, _sensor_receipt=_sensor_receipt)
            self._last_send_status = ret_state
            if non_idempotent and ret_state in ('CRC_ERR', 'LENGTH_ERR'):
                self._transport_uncertain_latched = True
            if cmd == Box_cmd['EXTRUDE_PROCESS']:
                self.extrude_process_ret_state = ret_state
            if ret_state == 'OK':
                return True
            elif ret_state == 'PARAMS_ERR':
                error(key_params_err % send_data_string, self.gcode, self.reactor, self.print_stats.state, self.box_save)
                return False
            elif ret_state == 'CRC_ERR' or ret_state == 'LENGTH_ERR':
                if not retries_en or times <= 0:
                    return False
                times -= 1
            elif ret_state == 'STATE_ERR':
                if cmd == Box_cmd['EXTRUDE_PROCESS'] and report_err:
                    _bind(logging, 'warning')('[warning] cmd: %s, state: %s, report_err: %s' % (cmd, ret_state, report_err))
                    return True
                error(key_state_err % (_bind(int, 'from_bytes')(addr, byteorder='big'), _bind(int, 'from_bytes')(cmd, byteorder='big')), self.gcode, self.reactor, self.print_stats.state, self.box_save)
                _bind(self.box_save, 'recode_err')('state_err', addr=ret[ADDR_POS])
                self.box_save.error_tnn['addr'] = ret[ADDR_POS]
                return False
            elif ret_state == 'EXTRUDE_ERR1':
                if report_err and cmd != Box_cmd['GET_BOX_STATE']:
                    error(key_extrude_err1 % (_bind(int, 'from_bytes')(addr, byteorder='big'), num_map[data[0]]), self.gcode, self.reactor, self.print_stats.state, self.box_save)
                    self.box_save.error_tnn['addr'] = ret[ADDR_POS]
                return False
            elif ret_state == 'EXTRUDE_ERR5':
                if report_err and cmd != Box_cmd['GET_BOX_STATE']:
                    error(key_extrude_err4 % (_bind(int, 'from_bytes')(addr, byteorder='big'), num_map[data[0]]), self.gcode, self.reactor, self.print_stats.state, self.box_save)
                    self.box_save.error_tnn['addr'] = ret[ADDR_POS]
                return False
            elif ret_state == 'EXTRUDE_ERR6':
                return False
            elif ret_state == 'EXTRUDE_ERR7':
                if report_err and cmd != Box_cmd['GET_BOX_STATE']:
                    error(key_joint_err, self.gcode, self.reactor, self.print_stats.state, self.box_save)
                    self.box_save.error_tnn['addr'] = ret[ADDR_POS]
                return False
            elif ret_state == 'EXTRUDE_ERR8' or ret_state == 'EXTRUDE_ERR9' or ret_state == 'EXTRUDE_ERR10':
                return False
            elif ret_state == 'RETRUDE_ERR1':
                if report_err:
                    _bind(logging, 'warning')('failed to retreat to buffer empty limit')
                return False
            elif ret_state == 'RETRUDE_ERR2' or ret_state == 'RETRUDE_ERR6' or ret_state == 'RETRUDE_ERR7':
                if report_err and cmd != Box_cmd['GET_BOX_STATE']:
                    if _bind(self, 'get_filament_sensor_detect')():
                        error(key_retrude_err6 % (_bind(int, 'from_bytes')(addr, byteorder='big'), num_map[data[0]]), self.gcode, self.reactor, self.print_stats.state, self.box_save)
                    else:
                        error(key_retrude_err1 % (_bind(int, 'from_bytes')(addr, byteorder='big'), num_map[data[0] & 15]), self.gcode, self.reactor, self.print_stats.state, self.box_save)
                    self.box_save.error_tnn['addr'] = ret[ADDR_POS]
                return False
            elif ret_state == 'RETRUDE_ERR3':
                if report_err and cmd != Box_cmd['GET_BOX_STATE']:
                    error(key_retrude_err2 % _bind(int, 'from_bytes')(addr, byteorder='big'), self.gcode, self.reactor, self.print_stats.state, self.box_save)
                    self.box_save.error_tnn['addr'] = ret[ADDR_POS]
                return False
            elif ret_state == 'EXTRUDE_ERR4':
                if cmd != Box_cmd['GET_BOX_STATE']:
                    error(key_filament_err % (_bind(int, 'from_bytes')(addr, byteorder='big'), num_map[data[0]]), self.gcode, self.reactor, self.print_stats.state, self.box_save)
                    self.box_state.Tn_data['filament'] = 0
                    self.box_save.error_tnn['addr'] = ret[ADDR_POS]
                return False
            elif ret_state == 'RETRUDE_ERR4' or ret_state == 'RETRUDE_ERR5':
                if self.box_save.last_cmd is not None:
                    last_Tnn_content = _bind(self.box_state, 'get_Tnn_content')(self.box_save.last_cmd)
                    error(key_filament_err % (_bind(int, 'from_bytes')(addr, byteorder='big'), last_Tnn_content['num']), self.gcode, self.reactor, self.print_stats.state, self.box_save)
                    self.box_state.Tn_data['filament'] = 0
                    self.box_save.error_tnn['addr'] = ret[ADDR_POS]
                    return False
                _bind(logging, 'warning')('last_cmd is None')
                return True
            elif ret_state == 'FILAMENT_ERR':
                if self.Tn_action_flag:
                    _bind(logging, 'warning')('Tn_action_flag is %s' % self.Tn_action_flag)
                    return True
                if _bind(self.box_state, 'get_Tn_data')(0, 0, uppart='filament_useup') == 0:
                    _bind(self.box_state, 'modify_Tn_data')(0, 1, 0, uppart='filament_useup')
                if self.box_save.last_cmd is None:
                    _bind(logging, 'warning')('FILAMENT_ERR, last_cmd is None')
                    return True
                if self.box_save.box_filament_err is False:
                    self.box_save.box_filament_err = True
                    _bind(self.reactor, 'register_callback')(self.filament_err_tighten_up_event)
                if _bind(self.box_state, 'get_Tn_data')(0, 0, num=0, uppart='auto_refill') and (self.print_stats.state == 'printing' or self.print_stats.state == 'paused'):
                    if self.is_use_ending_material is False:
                        self.is_use_ending_material = True
                        _bind(self.box_save, 'recode_err')('filament_err', self.box_save.last_cmd, self.box_save.last_cmd, addr=ret[ADDR_POS])
                        _bind(logging, 'info')('filament err')
                    return True
                last_Tnn_content = _bind(self.box_state, 'get_Tnn_content')(self.box_save.last_cmd)
                error(key_filament_err % (_bind(int, 'from_bytes')(addr, byteorder='big'), last_Tnn_content['num']), self.gcode, self.reactor, self.print_stats.state, self.box_save)
                self.box_state.Tn_data['filament'] = 0
                _bind(self.box_save, 'recode_err')('filament_err', self.box_save.last_cmd, self.box_save.last_cmd, addr=ret[ADDR_POS])
                _bind(self.gcode, 'run_script')('BOX_EXTRUSION_ALL_MATERIALS')
                return False
            elif ret_state == 'UPDATE_STATE':
                msg_pos = {'HEAD': 0, 'ADDR': 1, 'LEN': 2, 'STATE': 3, 'CMD': 4, 'DATA': 5}
                msg_data = ret[msg_pos['DATA']:msg_pos['DATA'] + 4]
                msg_addr = ret[msg_pos['ADDR']]
                _bind(self, 'update_state_process')(msg_addr, msg_data)
                return True
            elif ret_state == 'SPEED_ERR':
                if self.box_save.error != 'filament_err' and self.box_save.last_cmd is not None:
                    error(key_speed_err, self.gcode, self.reactor, self.print_stats.state, self.box_save)
                    _bind(self.box_save, 'recode_err')('empty_print', tnn=self.box_save.last_cmd)
                return False
            elif ret_state == 'ENWIND_ERR':
                if self.box_save.error != 'filament_err' and self.box_save.last_cmd is not None:
                    if _bind(self.box_state, 'get_Tn_data')(0, 0, uppart='filament_useup') == 0:
                        _bind(self.box_state, 'modify_Tn_data')(0, 1, 0, uppart='filament_useup')
                    if self.box_state.resume_flag:
                        _bind(logging, 'warning')('wait resume end')
                        return False
                    _bind(self.box_save, 'recode_err')('empty_print', tnn=self.box_save.last_cmd)
                    error(key_enwind_err, self.gcode, self.reactor, self.print_stats.state, self.box_save)
                return False
            elif ret_state == 'MOTOR_LOAD_ERR':
                if report_err:
                    error(key_load_err, self.gcode, self.reactor, self.print_stats.state, self.box_save)
                return False
            else:
                return False

    def process_msg(self, data_send, msg, _sensor_receipt=None):
        lookup = self.parse.get_key_from_value
        try:
            commands = Box_cmd
            try:
                parse_number = self.parse.parse_num_to_byte
                try:
                    command_byte = parse_number(msg[CMD_POS])
                finally:
                    del parse_number
                try:
                    cmd = lookup(commands, command_byte)
                finally:
                    del command_byte
            finally:
                del commands
        finally:
            del lookup
        operation = logging.debug if cmd in ('GET_BOX_STATE', 'MEASURING_WHEEL') else logging.info
        try:
            operation_result = operation('cmd: %s' % cmd)
        finally:
            del operation
        del operation_result
        if cmd is None:
            operation = logging.warning
            try:
                operation_result = operation('ret cmd is none')
            finally:
                del operation
            del operation_result
            return
        if cmd == 'GET_RFID':
            operation = self.parse.get_rfid
            try:
                operation_result = operation(msg)
            finally:
                del operation
            rfid = operation_result
            del operation_result
            for key, value in rfid.items():
                operation = self.rfid_check
                try:
                    operation_result = operation(value)
                finally:
                    del operation
                ret = operation_result
                del operation_result
                if ret is False:
                    operation = error
                    try:
                        message = key_rfid_err % (value, msg[ADDR_POS], key)
                        gcode_arg = reactor_arg = stats_arg = state_arg = save_arg = None
                        call_succeeded = False
                        try:
                            gcode_arg = self.gcode
                            reactor_arg = self.reactor
                            stats_arg = self.print_stats
                            state_arg = stats_arg.state
                            stats_arg = None
                            save_arg = self.box_save
                            operation_result = operation(message, gcode_arg, reactor_arg, state_arg, save_arg)
                            call_succeeded = True
                        finally:
                            del message
                            gcode_arg = None
                            reactor_arg = None
                            stats_arg = None
                            state_arg = None
                            save_arg = None
                            del gcode_arg, reactor_arg, stats_arg, state_arg, save_arg, call_succeeded
                    finally:
                        del operation
                    del operation_result
                    operation = self.box_state.get_tn_save_data
                    try:
                        operation_result = operation(msg[ADDR_POS], 'vender', key)
                    finally:
                        del operation
                    vender = operation_result
                    del operation_result
                    if vender == 'unknown':
                        operation = self.box_state.modify_Tn_data
                        try:
                            operation_result = operation(msg[ADDR_POS], 'unknown', 'vender', key)
                        finally:
                            del operation
                        del operation_result
                        operation = self.box_state.get_tn_save_data
                        try:
                            operation_result = operation(msg[ADDR_POS], 'color_value', key)
                        finally:
                            del operation
                        color_value = operation_result
                        del operation_result
                        if color_value != 'unknown' and color_value != '-1':
                            operation = self.box_state.modify_Tn_data
                            try:
                                operation_result = operation(msg[ADDR_POS], color_value, 'color_value', key)
                            finally:
                                del operation
                            del operation_result
                            operation = self.box_state.get_tn_save_data
                            try:
                                operation_result = operation(msg[ADDR_POS], 'material_type', key)
                            finally:
                                del operation
                            material_type = operation_result
                            del operation_result
                            operation = self.box_state.modify_Tn_data
                            try:
                                operation_result = operation(msg[ADDR_POS], material_type, 'material_type', key)
                            finally:
                                del operation
                            del operation_result
                    else:
                        operation = self.box_state.modify_Tn_data
                        try:
                            operation_result = operation(msg[ADDR_POS], 'unknown', 'vender', key)
                        finally:
                            del operation
                        del operation_result
                        operation = self.box_state.modify_Tn_data
                        try:
                            operation_result = operation(msg[ADDR_POS], 'unknown', 'color_value', key)
                        finally:
                            del operation
                        del operation_result
                        operation = self.box_state.modify_Tn_data
                        try:
                            operation_result = operation(msg[ADDR_POS], 'unknown', 'material_type', key)
                        finally:
                            del operation
                        del operation_result
                else:
                    operation = self.box_state.modify_Tn_data
                    try:
                        operation_result = operation(msg[ADDR_POS], value, 'vender', key)
                    finally:
                        del operation
                    del operation_result
                    if value != 'none':
                        operation = self.box_state.modify_tn_save_data
                        try:
                            operation_result = operation(msg[ADDR_POS], value, 'vender', key)
                        finally:
                            del operation
                        del operation_result
                    if value != 'unknown' and value != 'none':
                        operation = self.box_state.modify_Tn_data
                        try:
                            operation_result = operation(msg[ADDR_POS], value[17:24], 'color_value', key)
                        finally:
                            del operation
                        del operation_result
                        operation = self.box_state.modify_tn_save_data
                        try:
                            operation_result = operation(msg[ADDR_POS], value[17:24], 'color_value', key)
                        finally:
                            del operation
                        del operation_result
                        operation = self.box_state.modify_Tn_data
                        try:
                            operation_result = operation(msg[ADDR_POS], value[11:17], 'material_type', key)
                        finally:
                            del operation
                        del operation_result
                        operation = self.box_state.modify_tn_save_data
                        try:
                            operation_result = operation(msg[ADDR_POS], value[11:17], 'material_type', key)
                        finally:
                            del operation
                        del operation_result
                    elif value == 'none':
                        operation = self.box_state.modify_Tn_data
                        try:
                            operation_result = operation(msg[ADDR_POS], 'none', 'color_value', key)
                        finally:
                            del operation
                        del operation_result
                        operation = self.box_state.modify_Tn_data
                        try:
                            operation_result = operation(msg[ADDR_POS], 'none', 'material_type', key)
                        finally:
                            del operation
                        del operation_result
                    elif value == 'unknown':
                        read_state = self.box_state.get_Tn_data
                        try:
                            current_color = read_state(msg[ADDR_POS], 'color_value', key)
                        finally:
                            del read_state
                        try:
                            restore_color = bool(current_color == '-1')
                        finally:
                            del current_color
                        if restore_color:
                            operation = self.box_state.get_tn_save_data
                            try:
                                operation_result = operation(msg[ADDR_POS], 'color_value', key)
                            finally:
                                del operation
                            color_value = operation_result
                            del operation_result
                            if color_value != 'unknown' and color_value != '-1':
                                operation = self.box_state.modify_Tn_data
                                try:
                                    operation_result = operation(msg[ADDR_POS], color_value, 'color_value', key)
                                finally:
                                    del operation
                                del operation_result
                                operation = self.box_state.get_tn_save_data
                                try:
                                    operation_result = operation(msg[ADDR_POS], 'material_type', key)
                                finally:
                                    del operation
                                material_type = operation_result
                                del operation_result
                                operation = self.box_state.modify_Tn_data
                                try:
                                    operation_result = operation(msg[ADDR_POS], material_type, 'material_type', key)
                                finally:
                                    del operation
                                del operation_result
                        del restore_color
            operation = self.reactor.register_callback
            try:
                operation_result = operation(self.update_tn_save_data)
            finally:
                del operation
            del operation_result
            operation = self.reactor.register_callback
            try:
                operation_result = operation(self.update_same_material_list)
            finally:
                del operation
            del operation_result
        elif cmd == 'GET_REMAIN_LEN':
            operation = self.parse.get_remain_len
            try:
                operation_result = operation(data_send, msg)
            finally:
                del operation
            remain_len = operation_result
            del operation_result
            for key, value in remain_len.items():
                operation = self.box_state.modify_Tn_data
                try:
                    operation_result = operation(msg[ADDR_POS], value, 'remain_len', key)
                finally:
                    del operation
                del operation_result
                operation = self.box_state.modify_tn_save_data
                try:
                    operation_result = operation(msg[ADDR_POS], value, 'remain_len', key)
                finally:
                    del operation
                del operation_result
            operation = self.reactor.register_callback
            try:
                operation_result = operation(self.update_tn_save_data)
            finally:
                del operation
            del operation_result
        elif cmd == 'MEASURING_WHEEL':
            operation = self.parse.get_measuring_wheel
            try:
                operation_result = operation(data_send, msg)
            finally:
                del operation
            measuring_wheel = operation_result
            del operation_result
            operation = logging.debug
            try:
                operation_result = operation('measuring_wheel = %d' % measuring_wheel)
            finally:
                del operation
            del operation_result
            operation = self.box_state.modify_Tn_data
            try:
                operation_result = operation(msg[ADDR_POS], measuring_wheel, 'measuring_wheel')
            finally:
                del operation
            del operation_result
        elif cmd == 'GET_FILAMENT_SENSOR_STATE':
            if _sensor_receipt is not None and len(msg) < DATA_POS + 2:
                request_text = ' '.join(hex(value) for value in data_send)
                raise self.gcode.error(key_params_err % request_text)
            sensor_state = msg[DATA_POS]
            sensor_map = ['material', 'connections']
            operation = self.box_state.modify_Tn_inner_data
            try:
                operation_result = operation(msg[ADDR_POS], sensor_state, 'filament_sensor', subpart=sensor_map[data_send[DATA_POS - 1]])
            finally:
                del operation
            del operation_result
            if _sensor_receipt is not None:
                _sensor_receipt['accepted'] = True
        elif cmd == 'GET_VERSION_SN':
            operation = '.'.join
            try:
                operation_result = operation((chr(c) for c in msg[5:8]))
            finally:
                del operation
            version = operation_result
            del operation_result
            operation = ''.join
            try:
                operation_result = operation((chr(c) for c in msg[8:-1]))
            finally:
                del operation
            sn = operation_result
            del operation_result
            operation = self.box_state.modify_Tn_data
            try:
                operation_result = operation(msg[ADDR_POS], version, 'version')
            finally:
                del operation
            del operation_result
            operation = self.box_state.modify_Tn_data
            try:
                operation_result = operation(msg[ADDR_POS], sn, 'sn')
            finally:
                del operation
            del operation_result
            message_format = 'version: %s, sn: %s'
            operation = logging.info
            try:
                operation_result = operation(message_format % (version, sn))
            finally:
                del operation
            del operation_result
        elif cmd == 'GET_BOX_STATE':
            temperature = msg[DATA_POS] if msg[DATA_POS] < 128 else msg[DATA_POS] - 256
            dry_and_humidity = msg[DATA_POS + 1] if msg[DATA_POS + 1] < 128 else msg[DATA_POS + 1] - 256
            if dry_and_humidity < 0 or dry_and_humidity > 100:
                operation = error
                try:
                    message = key_dry_and_humidity_err % dry_and_humidity
                    gcode_arg = reactor_arg = stats_arg = state_arg = save_arg = None
                    call_succeeded = False
                    try:
                        gcode_arg = self.gcode
                        reactor_arg = self.reactor
                        stats_arg = self.print_stats
                        state_arg = stats_arg.state
                        stats_arg = None
                        save_arg = self.box_save
                        operation_result = operation(message, gcode_arg, reactor_arg, state_arg, save_arg)
                        call_succeeded = True
                    finally:
                        del message
                        if call_succeeded:
                            gcode_arg = None
                            reactor_arg = None
                        else:
                            reactor_arg = None
                            gcode_arg = None
                        stats_arg = None
                        state_arg = None
                        save_arg = None
                        del gcode_arg, reactor_arg, stats_arg, state_arg, save_arg, call_succeeded
                finally:
                    del operation
                del operation_result
            mode = msg[DATA_POS + 3]
            operation = self.box_state.modify_Tn_data
            try:
                operation_result = operation(msg[ADDR_POS], str(temperature), 'temperature')
            finally:
                del operation
            del operation_result
            operation = self.box_state.modify_Tn_data
            try:
                operation_result = operation(msg[ADDR_POS], str(dry_and_humidity), 'dry_and_humidity')
            finally:
                del operation
            del operation_result
            operation = self.box_state.modify_Tn_data
            try:
                operation_result = operation(msg[ADDR_POS], str(mode), 'mode')
            finally:
                del operation
            del operation_result
        elif cmd == 'GET_BUFFER_STATE':
            buffer_state = msg[DATA_POS]
            operation = self.gcode.respond_info
            try:
                operation_result = operation('buffer_state: 0x%x' % buffer_state)
            finally:
                del operation
            del operation_result
            if buffer_state == 0:
                operation = self.gcode.respond_info
                try:
                    operation_result = operation('middle')
                finally:
                    del operation
                del operation_result
            elif buffer_state == 1:
                operation = self.gcode.respond_info
                try:
                    operation_result = operation('full')
                finally:
                    del operation
                del operation_result
            elif buffer_state == 2:
                operation = self.gcode.respond_info
                try:
                    operation_result = operation('empty')
                finally:
                    del operation
                del operation_result
            operation = self.box_state.modify_Tn_inner_data
            try:
                operation_result = operation(msg[ADDR_POS], buffer_state, None, uppart='buffer')
            finally:
                del operation
            del operation_result
        elif cmd == 'GET_BOX_MODE':
            mode = msg[DATA_POS]
            operation = self.box_state.modify_Tn_data
            try:
                operation_result = operation(msg[ADDR_POS], str(mode), 'mode')
            finally:
                del operation
            del operation_result
        elif cmd == 'GET_HARDWARE_STATUS':
            eeprom_status = msg[DATA_POS]
            dry_and_humidity_status = msg[DATA_POS + 1]
            measuring_wheel_status = msg[DATA_POS + 2]
            buffer_status = msg[DATA_POS + 3]
            left_rfid_card_status = msg[DATA_POS + 4]
            right_rfid_card_status = msg[DATA_POS + 5]
            if eeprom_status & 3 != 0:
                operation = error
                try:
                    message = key_eeprom_err % msg[ADDR_POS]
                    gcode_arg = reactor_arg = stats_arg = state_arg = save_arg = None
                    call_succeeded = False
                    try:
                        gcode_arg = self.gcode
                        reactor_arg = self.reactor
                        stats_arg = self.print_stats
                        state_arg = stats_arg.state
                        stats_arg = None
                        save_arg = self.box_save
                        operation_result = operation(message, gcode_arg, reactor_arg, state_arg, save_arg)
                        call_succeeded = True
                    finally:
                        del message
                        gcode_arg = None
                        reactor_arg = None
                        stats_arg = None
                        state_arg = None
                        save_arg = None
                        del gcode_arg, reactor_arg, stats_arg, state_arg, save_arg, call_succeeded
                finally:
                    del operation
                del operation_result
            if dry_and_humidity_status & 3 != 0:
                operation = error
                try:
                    message = key_dry_and_humidity_err % msg[ADDR_POS]
                    gcode_arg = reactor_arg = stats_arg = state_arg = save_arg = None
                    call_succeeded = False
                    try:
                        gcode_arg = self.gcode
                        reactor_arg = self.reactor
                        stats_arg = self.print_stats
                        state_arg = stats_arg.state
                        stats_arg = None
                        save_arg = self.box_save
                        operation_result = operation(message, gcode_arg, reactor_arg, state_arg, save_arg)
                        call_succeeded = True
                    finally:
                        del message
                        if call_succeeded:
                            gcode_arg = None
                            reactor_arg = None
                        else:
                            reactor_arg = None
                            gcode_arg = None
                        stats_arg = None
                        state_arg = None
                        save_arg = None
                        del gcode_arg, reactor_arg, stats_arg, state_arg, save_arg, call_succeeded
                finally:
                    del operation
                del operation_result
            if measuring_wheel_status & 3 != 0:
                operation = error
                try:
                    message = key_measuring_wheel_err % msg[ADDR_POS]
                    gcode_arg = reactor_arg = stats_arg = state_arg = save_arg = None
                    call_succeeded = False
                    try:
                        gcode_arg = self.gcode
                        reactor_arg = self.reactor
                        stats_arg = self.print_stats
                        state_arg = stats_arg.state
                        stats_arg = None
                        save_arg = self.box_save
                        operation_result = operation(message, gcode_arg, reactor_arg, state_arg, save_arg)
                        call_succeeded = True
                    finally:
                        del message
                        gcode_arg = None
                        reactor_arg = None
                        stats_arg = None
                        state_arg = None
                        save_arg = None
                        del gcode_arg, reactor_arg, stats_arg, state_arg, save_arg, call_succeeded
                finally:
                    del operation
                del operation_result
            if buffer_status & 3 != 0:
                operation = error
                try:
                    message = key_buffer_err
                    gcode_arg = reactor_arg = stats_arg = state_arg = save_arg = None
                    call_succeeded = False
                    try:
                        gcode_arg = self.gcode
                        reactor_arg = self.reactor
                        stats_arg = self.print_stats
                        state_arg = stats_arg.state
                        stats_arg = None
                        save_arg = self.box_save
                        operation_result = operation(message, gcode_arg, reactor_arg, state_arg, save_arg)
                        call_succeeded = True
                    finally:
                        del message
                        gcode_arg = None
                        reactor_arg = None
                        stats_arg = None
                        state_arg = None
                        save_arg = None
                        del gcode_arg, reactor_arg, stats_arg, state_arg, save_arg, call_succeeded
                finally:
                    del operation
                del operation_result
            if left_rfid_card_status & 3 != 0:
                operation = error
                try:
                    message = key_left_rfid_card_err % msg[ADDR_POS]
                    gcode_arg = reactor_arg = stats_arg = state_arg = save_arg = None
                    call_succeeded = False
                    try:
                        gcode_arg = self.gcode
                        reactor_arg = self.reactor
                        stats_arg = self.print_stats
                        state_arg = stats_arg.state
                        stats_arg = None
                        save_arg = self.box_save
                        operation_result = operation(message, gcode_arg, reactor_arg, state_arg, save_arg)
                        call_succeeded = True
                    finally:
                        del message
                        gcode_arg = None
                        reactor_arg = None
                        stats_arg = None
                        state_arg = None
                        save_arg = None
                        del gcode_arg, reactor_arg, stats_arg, state_arg, save_arg, call_succeeded
                finally:
                    del operation
                del operation_result
            if right_rfid_card_status & 3 != 0:
                operation = error
                try:
                    message = key_right_rfid_card_err % msg[ADDR_POS]
                    gcode_arg = reactor_arg = stats_arg = state_arg = save_arg = None
                    call_succeeded = False
                    try:
                        gcode_arg = self.gcode
                        reactor_arg = self.reactor
                        stats_arg = self.print_stats
                        state_arg = stats_arg.state
                        stats_arg = None
                        save_arg = self.box_save
                        operation_result = operation(message, gcode_arg, reactor_arg, state_arg, save_arg)
                        call_succeeded = True
                    finally:
                        del message
                        gcode_arg = None
                        reactor_arg = None
                        stats_arg = None
                        state_arg = None
                        save_arg = None
                        del gcode_arg, reactor_arg, stats_arg, state_arg, save_arg, call_succeeded
                finally:
                    del operation
                del operation_result

    def ret_parse_process(self, data_send, msg, _sensor_receipt=None):
        if _sensor_receipt is not None:
            valid_request = (len(data_send) == 5
                             and data_send[CMD_POS - 1:CMD_POS] == Box_cmd['GET_FILAMENT_SENSOR_STATE']
                             and data_send[DATA_POS - 1] in (0, 1))
            valid_response = (len(msg) >= 6 and valid_request
                              and msg[ADDR_POS] == data_send[0]
                              and msg[CMD_POS] == data_send[CMD_POS - 1]
                              and len(msg) == msg[LEN_POS] + 3)
            if not valid_response:
                request_text = ' '.join(hex(value) for value in data_send)
                raise self.gcode.error(key_params_err % request_text)
        positions = {'HEAD': 0, 'ADDR': 1, 'LEN': 2, 'STATE': 3, 'CMD': 4, 'DATA': 5}
        formatted = ' '.join(hex(value) for value in msg)
        info = logging.debug
        try:
            ignored = info('msg: %s' % formatted)
        finally:
            del info
        del ignored
        code = msg[positions['STATE']]
        get_key = self.parse.get_key_from_value
        try:
            name = get_key(state, code)
        finally:
            del get_key
        info = logging.debug if code == state['OK'] else logging.info
        try:
            message_format = 'state: %s[0x%x]'
            ignored = info(message_format % (name, code))
        finally:
            del info
        del ignored
        if code == state['OK']:
            process = self.process_msg
            try:
                if _sensor_receipt is None:
                    ignored = process(data_send, msg)
                else:
                    ignored = process(data_send, msg, _sensor_receipt=_sensor_receipt)
            finally:
                del process
            del ignored
        return name

    def motor_send_data(self, addr, cmd, timeout, state=b'\x00', data=b'', retries_en=True):
        if len(data) == 0:
            data_send = addr + _motor_packet_length(self, cmd, data, False) + state + cmd
        else:
            data_send = addr + _motor_packet_length(self, cmd, data, True) + state + cmd + data
        info = logging.info
        try:
            ignored = info('data_send: %s' % data_send)
        finally:
            del info
        del ignored
        gcode = self.printer.lookup_object('gcode')
        params = self._serial.cmd_send_data_with_response(data_send, timeout, retries_en=retries_en)
        respond_info = gcode.respond_info
        try:
            message_format = 'data_send:%s recv_result:%s'
            ignored = respond_info(message_format % (str(data_send), str(params)))
        finally:
            del respond_info
        del ignored
        if not params:
            params = b''
        return params

    def enable_filament_sensor(self):
        self.filament_sensor_enable = True

    def disable_filament_sensor(self):
        self.filament_sensor_enable = False

    def enable_heart_process(self, tnn):
        index = int(tnn[1]) - 1
        self.heart_process_enable[index] = True

    def disable_heart_process(self, tnn):
        index = int(tnn[1]) - 1
        self.heart_process_enable[index] = False

    def update_same_material_list(self, eventtime):
        update = self.box_state.update_same_material_list
        try:
            ignored = update()
        finally:
            del update
        del ignored

    def update_tn_save_data(self, eventtime):
        update = self.box_state.update_tn_save_data
        try:
            ignored = update("base_data", save=True)
        finally:
            del update
        del ignored

    def check_same_box(self, tnn):
        if self.box_save.last_cmd is None:
            return True
        if tnn[BOX_NUM_POS] == self.box_save.last_cmd[BOX_NUM_POS]:
            return True
        return False

    def check_connect(self, addr):
        get_data = self.box_state.get_Tn_data
        try:
            state = get_data(addr, "state")
        finally:
            del get_data
        if state == "connect":
            return True
        return False

    def get_filament_sensor_detect(self):
        if self.filament_switch_sensor is None:
            return True
        if self.filament_switch_sensor.runout_helper.filament_present == 1:
            logging.info("filament_sensor true")
            return True
        logging.info("filament_sensor false")
        return False

    def get_five_way_sensor_detect(self):
        for addr in range(1, 5):
            if self.check_connect(addr):
                self.communication_get_filament_sensor_state(addr, "CONNECTIONS")
                get_data = self.box_state.get_Tn_inner_data
                try:
                    next_value = get_data(addr, "filament_sensor", subpart="connections")
                finally:
                    del get_data
                value = next_value
                del next_value
                if value != 0:
                    return True
        return False

    def update_filament_pos(self, tnn, data):
        addr = int(tnn[1])
        num = tnn[2]
        position = num if data else "None"
        modify = self.box_state.modify_Tn_data
        try:
            ignored = modify(addr, position, "filament", num)
        finally:
            del modify
        del ignored

    def __init__(self, config, box_state):
        self.config = config
        self.printer = self.config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = config.get_printer().lookup_object("gcode")
        self.box_save = BoxSave(config)
        self.parse = ParseData()
        self.boxcfg = BoxCfg(config)
        switch_pin = config.get("switch_pin", None)
        self.cut = CutSensor(config, switch_pin)
        self.box_state = box_state
        self.toolhead = None
        self.gcode_move = None
        self._serial = None
        self.filament_switch_sensor = None
        self.print_stats = None
        self.is_shutdown = True
        self.is_use_ending_material = False
        register = self.printer.register_event_handler
        try:
            register("klippy:shutdown", self._handle_shutdown)
        finally:
            del register
        register = self.printer.register_event_handler
        try:
            register("klippy:ready", self._handle_ready)
        finally:
            del register
        self.flushing_sign = os.path.join(
            base_dir, "creality/userdata/config/flushing_sign")
        self.material_database = os.path.join(
            base_dir, "creality/userdata/box/material_database.json")
        self.ffi_main, self.ffi_lib = chelper.get_ffi()
        self.scv = self.ffi_main.new("rgb_t *")
        self.tcv = self.ffi_main.new("rgb_t *")
        self.extrude_process_ret_state = None
        self._last_send_status = None
        self._transport_uncertain_latched = False
        self.filament_sensor_enable = False
        self.heart_process_enable = [True, True, True, True]
        self.extrude_tnn = None
        self.Tn_action_flag = False
        self.extrude_process_stage7_flag = False
        self.extrude_process_stage7_ret = {
            "ret": None, "extrude_process_ret_state": None}
        register = self.printer.register_event_handler
        try:
            register("box:extrude_process_stage7", self.extrude_process_stage7)
        finally:
            del register
        self.remain_material = None
        self.test = None
        for addr in range(1, 5):
            for num in range(1, 16):
                name = "auto_get_rfid_addr" + str(addr) + "_num" + str(num)
                generate = self.generate_auto_get_rfid_func
                try:
                    callback = generate(addr, num)
                finally:
                    del generate
                setattr(self, name, callback)
                del name, callback

    def _handle_ready(self):
        self.is_shutdown = False

    def find_objs(self):
        self.toolhead = self.printer.lookup_object("toolhead")
        self.gcode_move = self.printer.lookup_object("gcode_move", None)
        self._serial = self.printer.lookup_object("serial_485 serial485")
        self.print_stats = self.printer.lookup_object("print_stats")
        if self.boxcfg.filament_sensor is not None:
            lookup = self.printer.lookup_object
            try:
                sensor = lookup("filament_switch_sensor " + self.boxcfg.filament_sensor)
            finally:
                del lookup
            self.filament_switch_sensor = sensor
            _relay_filament_insert(self.printer, sensor)
            del sensor
        self.remain_material = self.printer.lookup_object("filament_rack").remain_material

    def generate_auto_get_rfid_func(self, addr, num):
        def function(eventtime):
            self.communication_get_rfid(addr, num)
            self.communication_get_remain_len(addr, num)
        return function

    def extrude_process_stage7(self):
        if self.filament_sensor_enable is False:
            return None
        get_content = self.box_state.get_Tnn_content
        try:
            Tnn_content = get_content(self.extrude_tnn)
        finally:
            del get_content
        self.gcode.run_script_from_command("G91")
        self.gcode.run_script_from_command("G0 E5 F60")
        self.gcode.run_script_from_command("G90")
        communicate = self.communication_extrude_process
        try:
            ret = communicate(Tnn_content["addr"], Tnn_content["num"], stage=6)
        finally:
            del communicate
        self.extrude_process_stage7_ret = {
            "ret": ret, "extrude_process_ret_state": self.extrude_process_ret_state}
        self.extrude_process_stage7_flag = True
        info = logging.info
        try:
            info("extrude_process_stage7 tnn: %s" % self.extrude_tnn)
        finally:
            del info

    def _handle_shutdown(self):
        self.is_shutdown = True

    def has_flushing_sign(self):
        """Consume the recovery marker; this is not a pure predicate."""
        exists = os.path.exists
        try:
            found = exists(self.flushing_sign)
        finally:
            del exists
        if found:
            del found
            remove = os.remove
            try:
                remove(self.flushing_sign)
            finally:
                del remove
            return True
        return False

    def set_flushing_sign(self):
        self.gcode.run_script_from_command("M400")
        self.gcode.run_script_from_command("SET_LIMITS")
        self.gcode.run_script_from_command("M400")

    def reset_flushing_sign(self):
        self.gcode.run_script_from_command("M400")
        self.gcode.run_script_from_command("RESTORE_LIMITS")
        self.gcode.run_script_from_command("M400")

    def blow(self):
        fan0_value = (self.box_save.output_pin_fan0.last_value
                      * self.box_save.output_pin_fan0.scale)
        self.gcode.run_script_from_command("SET_PIN PIN=fan0 VALUE=255")
        self.gcode.run_script_from_command("G4 P5000")
        self.gcode.run_script_from_command("M400")
        command = self.gcode.run_script_from_command
        try:
            command("SET_PIN PIN=fan0 VALUE=%.2f" % fan0_value)
        finally:
            del command

    def check_and_extrude(self, extrude, velocity, wait=True):
        if self.box_state.extrude_err:
            return False
        self.gcode.run_script_from_command("G91")
        command = self.gcode.run_script_from_command
        try:
            values = (extrude, velocity)
            command("G0 E%.2f F%.2f" % values)
        finally:
            del command
        del values
        self.gcode.run_script_from_command("G90")
        if wait:
            self.gcode.run_script_from_command("M400")
        if self.box_state.extrude_err:
            return False
        return True

    def check_speed_and_extrude(self, extrude, velocity, wait=True):
        if self.box_state.extrude_err:
            return False
        info = logging.info
        try:
            values = (extrude, velocity)
            info("check_and_extrude extrude: %s, velocity: %s" % values)
        finally:
            del info
        del values
        command = self.gcode.run_script_from_command
        try:
            command(f"G91\n G0 F{velocity} \n M400\n G0 E{extrude:.2f}\n G90")
        finally:
            del command
        if wait:
            self.gcode.run_script_from_command("M400")
        if self.box_state.extrude_err:
            return False
        return True

    def quickly_wait_heating(self, set_temp):
        command = self.gcode.run_script_from_command
        try:
            command("M104 S%.2f" % set_temp)
        finally:
            del command
        while True:
            if self.is_shutdown:
                return None
            cur_temp = self.toolhead.get_extruder().get_heater().smoothed_temp
            lower = set_temp - 10 <= cur_temp
            # Preserve the native false-result recheck without losing bool errors.
            if lower:
                within = cur_temp <= set_temp + 10
            else:
                within = lower
            if within and self.toolhead.get_extruder().get_heater().can_extrude:
                break
            pause = self.reactor.pause
            try:
                pause(self.reactor.monotonic() + 0.5)
            finally:
                del pause
        self.gcode.run_script_from_command("G91")
        self.gcode.run_script_from_command("G0 E0.01 F60")
        self.gcode.run_script_from_command("G90")

    def check_flush_temp_and_extrude(self, length, index, temp, max_temp,
                                    velocity, max_temp_flush=False):
        velocity_factor = 1
        if index == 0:
            if max_temp_flush:
                wait_heating = self.quickly_wait_heating
                try:
                    wait_heating(max_temp - 5)
                finally:
                    del wait_heating
                command = self.gcode.run_script_from_command
                try:
                    command("M104 S%.2f" % max_temp)
                finally:
                    del command
                logging.info("set target max temp")
        if max_temp_flush:
            if index < self.boxcfg.max_temp_flush_times:
                velocity_factor = self.boxcfg.flush_velocity_factor
            elif index == self.boxcfg.max_temp_flush_times:
                command = self.gcode.run_script_from_command
                try:
                    command("M104 S%.2f" % temp)
                finally:
                    del command
                logging.info("restore target temp")
            else:
                velocity_factor = 1
            info = logging.info
            try:
                info(f"restore velocity_factor={velocity_factor}")
            finally:
                del info
            for portion, factor in ((0.2, 1), (0.8, velocity_factor)):
                info = logging.info
                try:
                    info(f"restore velocity={velocity[0] * factor}")
                finally:
                    del info
                extrude = self.check_speed_and_extrude
                try:
                    ret = extrude(length * portion, velocity[0] * factor)
                finally:
                    del extrude
                if ret is False:
                    return False
        else:
            extrude = self.check_and_extrude
            try:
                ret = extrude(length, velocity[0])
            finally:
                del extrude
            if ret is False:
                return False

    def nozzle_clean(self, circle=True):
        clean_offset_pos = self.reactor.monotonic() % 3
        respond = self.gcode.respond_info
        try:
            respond("temp_pos:%s" % str(clean_offset_pos))
        finally:
            del respond
        if circle:
            self.set_flushing_sign()
        self.gcode.run_script_from_command("G90")
        max_accel = self.toolhead.get_max_velocity()[1]
        saved_cruise_ratio = self.toolhead.get_status(self.reactor.monotonic())['minimum_cruise_ratio']
        self.gcode.run_script_from_command("SET_VELOCITY_LIMIT ACCEL=5000 MINIMUM_CRUISE_RATIO=0")
        y_pos = self.toolhead.get_position()[1]
        if y_pos < self.boxcfg.safe_pos_y:
            command = self.gcode.run_script_from_command
            try:
                values = (self.boxcfg.clean_right_pos_x, self.boxcfg.clean_velocity)
                command("G0 X%.2f F%.2f" % values)
            finally:
                del command
            del values
            command = self.gcode.run_script_from_command
            try:
                values = (self.boxcfg.safe_pos_y, self.boxcfg.clean_velocity)
                command("G0 Y%.2f F%.2f" % values)
            finally:
                del command
            del values
        command = self.gcode.run_script_from_command
        try:
            values = (self.boxcfg.clean_pos_right, self.boxcfg.clean_velocity)
            command("G0 X%.2f F%.2f" % values)
        finally:
            del command
        del values
        command = self.gcode.run_script_from_command
        try:
            values = (self.boxcfg.clean_left_pos_x, self.boxcfg.clean_velocity)
            command("G0 X%.2f F%.2f" % values)
        finally:
            del command
        del values
        if circle:
            command = self.gcode.run_script_from_command
            try:
                values = (self.boxcfg.clean_pos_left + clean_offset_pos,
                          self.boxcfg.clean_pos_middle_y)
                command("G0 X%.2f Y%.2f F12000" % values)
            finally:
                del command
            del values
            self.gcode.run_script_from_command("G2 I4 J0 P1 F10000")
            command = self.gcode.run_script_from_command
            try:
                values = (self.boxcfg.clean_pos_right - clean_offset_pos,
                          self.boxcfg.clean_pos_middle_y)
                command("G0 X%.2f Y%.2f F10000" % values)
            finally:
                del command
            del values
            self.gcode.run_script_from_command("G3 I-4 J0 P1 F10000")
        for i in range(3):
            command = self.gcode.run_script_from_command
            try:
                values = (self.boxcfg.clean_right_pos_x, self.boxcfg.clean_right_pos_y,
                          self.boxcfg.clean_velocity)
                command("G0 X%.2f Y%.2f F%.2f" % values)
            finally:
                del command
            del values
            command = self.gcode.run_script_from_command
            try:
                values = (self.boxcfg.extrude_pos_x, self.boxcfg.clean_left_pos_y,
                          self.boxcfg.clean_velocity)
                command("G0 X%.2f Y%.2f F%.2f" % values)
            finally:
                del command
            del values
        if circle:
            command = self.gcode.run_script_from_command
            try:
                values = (self.boxcfg.clean_right_pos_x, self.boxcfg.clean_velocity)
                command("G0 X%.2f F%.2f" % values)
            finally:
                del command
            del values
            command = self.gcode.run_script_from_command
            try:
                values = (self.boxcfg.safe_pos_y, self.boxcfg.clean_velocity)
                command("G0 Y%.2f F%.2f" % values)
            finally:
                del command
            del values
        self.gcode.run_script_from_command("M400")
        command = self.gcode.run_script_from_command
        try:
            values = (max_accel, saved_cruise_ratio)
            command("SET_VELOCITY_LIMIT ACCEL=%.3f MINIMUM_CRUISE_RATIO=%.9f" % values)
        finally:
            del command
        del values
        if circle:
            self.reset_flushing_sign()

    def convert_scv(self, cv):
        try:
            self.scv.r = cv[0]
            self.scv.g = cv[1]
            self.scv.b = cv[2]
        except Exception as err:
            warning = logging.warning
            try:
                warning("convert_scv err, cv: %s" % cv)
            finally:
                del warning
            return False
        return True

    def convert_tcv(self, cv):
        try:
            self.tcv.r = cv[0]
            self.tcv.g = cv[1]
            self.tcv.b = cv[2]
        except Exception as err:
            warning = logging.warning
            try:
                warning("convert_tcv err, cv: %s" % cv)
            finally:
                del warning
            return False
        return True

    def material_volume_to_length(self, volume):
        return volume / 2.4

    def get_flush_len(self, scv, tcv):
        if self.convert_scv(scv) is False:
            return None
        if self.convert_tcv(tcv) is False:
            return None
        get_volume = self.ffi_lib.get_flushing_volume
        try:
            flush_volume = get_volume(self.scv[0], self.tcv[0])
        finally:
            del get_volume
        respond = self.gcode.respond_info
        try:
            values = (flush_volume, self.boxcfg.nozzle_volume)
            respond("flush_volume: %d, nozzle_volume: %d" % values)
        finally:
            del respond
        del values
        convert_volume = self.material_volume_to_length
        try:
            flush_len = convert_volume(flush_volume + self.boxcfg.nozzle_volume)
        finally:
            del convert_volume
        flush_len = flush_len * self.boxcfg.flush_multiplier
        respond = self.gcode.respond_info
        try:
            respond("flush_len: %f" % flush_len)
        finally:
            del respond
        return flush_len

    def cal_flush_list(self, src_length, cal_length):
        remain_length = src_length
        if src_length <= self.boxcfg.box_need_clean_length_max:
            if src_length >= self.boxcfg.box_first_clean_length:
                cal_length.append(src_length)
            else:
                portion = self.boxcfg.box_first_clean_length
                cal_length.append(portion)
                del portion
        else:
            portion = self.boxcfg.box_first_clean_length
            cal_length.append(portion)
            del portion
            remain_length -= self.boxcfg.box_first_clean_length
            count = 1
            while remain_length >= self.boxcfg.box_need_clean_length:
                portion = self.boxcfg.box_need_clean_length
                cal_length.append(portion)
                del portion
                remain_length -= self.boxcfg.box_need_clean_length
                count += 1
            if count == 1 and remain_length > 0:
                portion = self.boxcfg.box_need_clean_length
                cal_length.append(portion)
                del portion
            elif count >= 2 and remain_length > 0:
                remain_average = (
                    remain_length - (self.boxcfg.box_need_clean_length_max
                                     - self.boxcfg.box_first_clean_length)
                ) / (count - 1)
                if remain_length <= (self.boxcfg.box_need_clean_length_max
                                     - self.boxcfg.box_need_clean_length):
                    cal_length[count - 1] += remain_length
                elif remain_average > (self.boxcfg.box_need_clean_length_max
                                       - self.boxcfg.box_need_clean_length):
                    portion = self.boxcfg.box_need_clean_length
                    cal_length.append(portion)
                    del portion
                else:
                    cal_length[0] = self.boxcfg.box_need_clean_length_max
                    for i in range(count - 1):
                        cal_length[i + 1] += remain_average


    def communication_get_buffer_state(self, addr):
        return _send_box_query(self, addr, "GET_BUFFER_STATE")

    def communication_get_box_state(self, addr):
        return _send_box_query(self, addr, "GET_BOX_STATE")

    def communication_get_hardware_status(self, addr):
        return _send_box_query(self, addr, "GET_HARDWARE_STATUS")

    def communication_get_filament_sensor_state(self, addr, pos):
        positions = {'MATERIAL': b'\x00', 'CONNECTIONS': b'\x01'}
        command = Box_cmd['GET_FILAMENT_SENSOR_STATE']
        receipt = {'accepted': False}
        address = _bind(self.parse, 'parse_num_to_byte')(addr)
        _bind(self, 'send_data')(
            address, command, cmd_timeout['GET_FILAMENT_SENSOR_STATE'],
            data=positions[pos], _sensor_receipt=receipt)
        if not receipt['accepted']:
            command_number = int.from_bytes(command, byteorder='big')
            raise self.gcode.error(key_state_err % (addr, command_number))
        return True

    def timeout_process(self, addr):
        get_data = self.box_state.get_Tn_data
        try:
            state = get_data(addr, "state")
        finally:
            del get_data
        if state == "connect":
            if self.box_state.Tn_data["enable"] == 1:
                handler = error
                try:
                    handler(key_timeout % addr, self.gcode, self.reactor,
                            self.print_stats.state, self.box_save)
                finally:
                    del handler
            modify_data = self.box_state.modify_Tn_data
            try:
                modify_data(addr, data="disconnect", part="state")
            finally:
                del modify_data


    def check_rfid_valid(self, rfid):
        material_type = rfid[12:17]
        try:
            if _bind(os.path, 'exists')(self.material_database):
                with open(self.material_database, 'r') as file:
                    data = _material_database(file)
                    material_list = _bind(_bind(data, 'get')('result', {}), 'get')('list', [])
                    for i in range(len(material_list)):
                        material_id = _bind(_bind(material_list[i], 'get')('base', {}), 'get')('id', None)
                        if material_id is not None and material_type == material_id:
                            _bind(self.gcode, 'respond_info')('material_type: %s' % material_type)
                            return True
        except Exception as err:
            _bind(logging, 'error')('material database get extrusion speed: %s' % err)
        return False


    def rfid_check(self, rfid):
        if rfid == 'unknown' or rfid == 'none':
            return True
        if len(rfid) != 40:
            return False
        return _bind(self, 'check_rfid_valid')(rfid)


    def update_state_process(self, addr, data):
        data_map = {'unchanged': 0, 'insert': 1, 'extract': 2, 'completed': 3}
        num_map = {0: 'A', 1: 'B', 2: 'C', 3: 'D'}
        data_string = ' '.join(hex(value) for value in data)
        _bind(logging, 'info')('update_state_process, data: %s' % data_string)
        for num in range(4):
            if self.box_save.preloading_en:
                if data[num] == data_map['insert']:
                    if self.print_stats.state == 'printing' or self.print_stats.state == 'paused':
                        vender = _bind(self.box_state, 'get_tn_save_data')(addr, 'vender', num_map[num])
                        if vender == '-1':
                            _bind(self.box_state, 'modify_Tn_data')(addr, 'unknown', 'vender', num_map[num])
                            _bind(self.box_state, 'modify_Tn_data')(addr, 'unknown', 'color_value', num_map[num])
                            _bind(self.box_state, 'modify_Tn_data')(addr, 'unknown', 'material_type', num_map[num])
                        elif vender == 'unknown':
                            _bind(self.box_state, 'modify_Tn_data')(addr, vender, 'vender', num_map[num])
                            color_value = _bind(self.box_state, 'get_tn_save_data')(addr, 'color_value', num_map[num])
                            if color_value != 'unknown' and color_value != '-1':
                                _bind(self.box_state, 'modify_Tn_data')(addr, color_value, 'color_value', num_map[num])
                                material_type = _bind(self.box_state, 'get_tn_save_data')(addr, 'material_type', num_map[num])
                                _bind(self.box_state, 'modify_Tn_data')(addr, material_type, 'material_type', num_map[num])
                        else:
                            if _bind(self, 'check_printing_used_material')(addr, num_map[num]):
                                _bind(self.box_state, 'modify_Tn_data')(addr, vender, 'vender', num_map[num])
                                remain_len = _bind(self.box_state, 'get_tn_save_data')(addr, 'remain_len', num_map[num])
                                _bind(self.box_state, 'modify_Tn_data')(addr, remain_len, 'remain_len', num_map[num])
                                color_value = _bind(self.box_state, 'get_tn_save_data')(addr, 'color_value', num_map[num])
                                _bind(self.box_state, 'modify_Tn_data')(addr, color_value, 'color_value', num_map[num])
                                material_type = _bind(self.box_state, 'get_tn_save_data')(addr, 'material_type', num_map[num])
                                _bind(self.box_state, 'modify_Tn_data')(addr, material_type, 'material_type', num_map[num])
                            else:
                                _bind(self.box_state, 'modify_Tn_data')(addr, 'unknown', 'vender', num_map[num])
                                _bind(self.box_state, 'modify_Tn_data')(addr, 'unknown', 'color_value', num_map[num])
                                _bind(self.box_state, 'modify_Tn_data')(addr, 'unknown', 'material_type', num_map[num])
                    else:
                        _bind(self.box_state, 'modify_Tn_data')(addr, 'unknown', 'vender', num_map[num])
                elif data[num] == data_map['extract']:
                    _bind(self.box_state, 'modify_Tn_data')(addr, 'none', 'vender', num_map[num])
                elif data[num] == data_map['completed']:
                    if self.print_stats.state == 'printing' or self.print_stats.state == 'paused':
                        vender = _bind(self.box_state, 'get_tn_save_data')(addr, 'vender', num_map[num])
                        if vender == '-1':
                            _bind(self.box_state, 'modify_Tn_data')(addr, 'unknown', 'vender', num_map[num])
                            _bind(self.box_state, 'modify_Tn_data')(addr, 'unknown', 'color_value', num_map[num])
                            _bind(self.box_state, 'modify_Tn_data')(addr, 'unknown', 'material_type', num_map[num])
                        elif vender == 'unknown':
                            _bind(self.box_state, 'modify_Tn_data')(addr, vender, 'vender', num_map[num])
                            color_value = _bind(self.box_state, 'get_tn_save_data')(addr, 'color_value', num_map[num])
                            if color_value != 'unknown' and color_value != '-1':
                                _bind(self.box_state, 'modify_Tn_data')(addr, color_value, 'color_value', num_map[num])
                                material_type = _bind(self.box_state, 'get_tn_save_data')(addr, 'material_type', num_map[num])
                                _bind(self.box_state, 'modify_Tn_data')(addr, material_type, 'material_type', num_map[num])
                        else:
                            _bind(self.box_state, 'modify_Tn_data')(addr, vender, 'vender', num_map[num])
                            remain_len = _bind(self.box_state, 'get_tn_save_data')(addr, 'remain_len', num_map[num])
                            _bind(self.box_state, 'modify_Tn_data')(addr, remain_len, 'remain_len', num_map[num])
                            color_value = _bind(self.box_state, 'get_tn_save_data')(addr, 'color_value', num_map[num])
                            _bind(self.box_state, 'modify_Tn_data')(addr, color_value, 'color_value', num_map[num])
                            material_type = _bind(self.box_state, 'get_tn_save_data')(addr, 'material_type', num_map[num])
                            _bind(self.box_state, 'modify_Tn_data')(addr, material_type, 'material_type', num_map[num])
                    else:
                        func_name = 'auto_get_rfid_addr' + str(addr) + '_num' + str(1 << num)
                        try:
                            func = getattr(self, func_name)
                            _bind(self.reactor, 'register_callback')(func)
                        except Exception as err:
                            _bind(logging, 'warning')(err)
            else:
                if data[num] == data_map['insert'] or data[num] == data_map['completed']:
                    if self.print_stats.state == 'printing' or self.print_stats.state == 'paused':
                        vender = _bind(self.box_state, 'get_tn_save_data')(addr, 'vender', num_map[num])
                        if vender == '-1':
                            _bind(self.box_state, 'modify_Tn_data')(addr, 'unknown', 'vender', num_map[num])
                            _bind(self.box_state, 'modify_Tn_data')(addr, 'unknown', 'color_value', num_map[num])
                            _bind(self.box_state, 'modify_Tn_data')(addr, 'unknown', 'material_type', num_map[num])
                        elif vender == 'unknown':
                            _bind(self.box_state, 'modify_Tn_data')(addr, vender, 'vender', num_map[num])
                            color_value = _bind(self.box_state, 'get_tn_save_data')(addr, 'color_value', num_map[num])
                            if color_value != 'unknown' and color_value != '-1':
                                _bind(self.box_state, 'modify_Tn_data')(addr, color_value, 'color_value', num_map[num])
                                material_type = _bind(self.box_state, 'get_tn_save_data')(addr, 'material_type', num_map[num])
                                _bind(self.box_state, 'modify_Tn_data')(addr, material_type, 'material_type', num_map[num])
                        else:
                            if _bind(self, 'check_printing_used_material')(addr, num_map[num]):
                                _bind(self.box_state, 'modify_Tn_data')(addr, vender, 'vender', num_map[num])
                                remain_len = _bind(self.box_state, 'get_tn_save_data')(addr, 'remain_len', num_map[num])
                                _bind(self.box_state, 'modify_Tn_data')(addr, remain_len, 'remain_len', num_map[num])
                                color_value = _bind(self.box_state, 'get_tn_save_data')(addr, 'color_value', num_map[num])
                                _bind(self.box_state, 'modify_Tn_data')(addr, color_value, 'color_value', num_map[num])
                                material_type = _bind(self.box_state, 'get_tn_save_data')(addr, 'material_type', num_map[num])
                                _bind(self.box_state, 'modify_Tn_data')(addr, material_type, 'material_type', num_map[num])
                            else:
                                _bind(self.box_state, 'modify_Tn_data')(addr, 'unknown', 'vender', num_map[num])
                                _bind(self.box_state, 'modify_Tn_data')(addr, 'unknown', 'color_value', num_map[num])
                                _bind(self.box_state, 'modify_Tn_data')(addr, 'unknown', 'material_type', num_map[num])
                    else:
                        _bind(self.box_state, 'modify_Tn_data')(addr, 'unknown', 'vender', num_map[num])
                        vender = _bind(self.box_state, 'get_tn_save_data')(addr, 'vender', num_map[num])
                        if vender == 'unknown':
                            color_value = _bind(self.box_state, 'get_tn_save_data')(addr, 'color_value', num_map[num])
                            if color_value != 'unknown' and color_value != '-1':
                                _bind(self.box_state, 'modify_Tn_data')(addr, color_value, 'color_value', num_map[num])
                                material_type = _bind(self.box_state, 'get_tn_save_data')(addr, 'material_type', num_map[num])
                                _bind(self.box_state, 'modify_Tn_data')(addr, material_type, 'material_type', num_map[num])
                            else:
                                _bind(self.box_state, 'modify_Tn_data')(addr, 'unknown', 'color_value', num_map[num])
                                _bind(self.box_state, 'modify_Tn_data')(addr, 'unknown', 'material_type', num_map[num])
                        else:
                            _bind(self.box_state, 'modify_Tn_data')(addr, 'unknown', 'color_value', num_map[num])
                            _bind(self.box_state, 'modify_Tn_data')(addr, 'unknown', 'material_type', num_map[num])
                elif data[num] == data_map['extract']:
                    _bind(self.box_state, 'modify_Tn_data')(addr, 'none', 'vender', num_map[num])
        _bind(self.reactor, 'register_callback')(self.update_tn_save_data)
        _bind(self.reactor, 'register_callback')(self.update_same_material_list)


    def communication_extrude_process(self, addr, num, trigger='', stage=2, report_err=True, extrude=None):
        cmd = Box_cmd['EXTRUDE_PROCESS']
        num_map = {'A': b'\x01', 'B': b'\x02', 'C': b'\x04', 'D': b'\x08'}
        data_ready = False
        if self.boxcfg.version == 0:
            trigger_pos = {'CONNECTION': b'\x00', 'BUFFER': b'\x01'}
            data = num_map[num] + trigger_pos[trigger]
            data_ready = True
        elif self.boxcfg.version == 1:
            if extrude:
                data = num_map[num] + _bind(self.parse, 'parse_num_to_byte')(stage) + _bind(self.parse, 'parse_num_to_byte')(extrude)
            else:
                data = num_map[num] + _bind(self.parse, 'parse_num_to_byte')(stage) + b'\x00'
            data_ready = True
        try:
            send = self.send_data
            try:
                address = _bind(self.parse, 'parse_num_to_byte')(addr)
            except:
                del send
                raise
            try:
                arguments = (address, cmd, cmd_timeout['EXTRUDE_PROCESS'])
            except:
                del send, address
                raise
            del address
            try:
                return send(*arguments, data=data, report_err=report_err, retries_en=False)
            finally:
                del send, arguments
        finally:
            if data_ready:
                del data


    def communication_retrude_process(self, addr, num, trigger, report_err=True):
        trigger_pos = {'BUFFER': b'\x00', 'MATERIAL': b'\x01'}
        num_map = {'A': b'\x01', 'B': b'\x02', 'C': b'\x04', 'D': b'\x08', '0': b'\x00'}
        cmd = Box_cmd['RETRUDE_PROCESS']
        data = num_map[num] + trigger_pos[trigger]
        try:
            send = self.send_data
            try:
                address = _bind(self.parse, 'parse_num_to_byte')(addr)
            except:
                del send
                raise
            try:
                arguments = (address, cmd, cmd_timeout['RETRUDE_PROCESS'])
            except:
                del address, send
                raise
            del address
            try:
                return send(*arguments, data=data, report_err=report_err, retries_en=False)
            finally:
                del send, arguments
        finally:
            del data


    def communication_ctrl_connection_motor_action(self, addr, act):
        action = {'STOP': b'\x00', 'EXTRUDE': b'\x01', 'RETRUDE': b'\x02'}
        cmd = Box_cmd['CTRL_CONNECTION_MOTOR_ACTION']
        send = self.send_data
        try:
            address = _bind(self.parse, 'parse_num_to_byte')(addr)
        except:
            del send
            raise
        try:
            arguments = (address, cmd, cmd_timeout['CTRL_CONNECTION_MOTOR_ACTION'])
        except:
            del address, send
            raise
        del address
        try:
            return send(*arguments, data=action[act])
        finally:
            del send, arguments


    def communication_measuring_wheel(self, addr, action):
        cmd = Box_cmd['MEASURING_WHEEL']
        act_map = {'CLEAN': 0, 'GET': 1}
        action_encode = _bind(self.parse, 'parse_num_to_byte')(act_map[action])
        try:
            send = self.send_data
            try:
                address = _bind(self.parse, 'parse_num_to_byte')(addr)
            except:
                del send
                raise
            try:
                arguments = (address, cmd, cmd_timeout['MEASURING_WHEEL'])
            except:
                del send, address
                raise
            del address
            try:
                return send(*arguments, data=action_encode)
            finally:
                del send, arguments
        finally:
            del action_encode


    def communication_extrude2_process(self, addr, num, trigger):
        trigger_pos = {'CONNECTION': b'\x00', 'BUFFER': b'\x01'}
        num_map = {'A': b'\x01', 'B': b'\x02', 'C': b'\x04', 'D': b'\x08'}
        cmd = Box_cmd['EXTRUDE_PROCESS_MODEL2']
        data = num_map[num] + trigger_pos[trigger]
        try:
            send = self.send_data
            try:
                address = _bind(self.parse, 'parse_num_to_byte')(addr)
            except:
                del send
                raise
            try:
                arguments = (address, cmd, cmd_timeout['EXTRUDE_PROCESS_MODEL2'])
            except:
                del address, send
                raise
            del address
            try:
                return send(*arguments, data=data)
            finally:
                del send, arguments
        finally:
            del data


    def communication_test(self, count, timeout, interval, addr, num):
        error_times = 0
        timeout_times = 0
        start_format = '%d. addr[%d] test start'
        timeout_format = '%d.timeout, TIMEOUT=%.2f, send data: 0x%x'
        error_format = '%d.get return data(0x%x) error, send data: 0x%x'
        finish_format = '%d. addr[%d] test finish'
        for i in range(count):
            cmd = Box_cmd['COMMUNICATION_TEST']
            respond = _bind(self.gcode, 'respond_info')
            try:
                ignored = respond(start_format % (i, addr))
            finally:
                del respond
            del ignored
            send = self.send_data
            try:
                address = _bind(self.parse, 'parse_num_to_byte')(addr)
            except:
                del send
                raise
            arguments = (address, cmd, timeout)
            del address
            try:
                keywords = {'data': _bind(self.parse, 'parse_num_to_byte')(num)}
            except:
                del arguments, send
                raise
            try:
                response = send(*arguments, **keywords)
            finally:
                del send, arguments, keywords
            ret = response
            del response
            if ret is None:
                timeout_times += 1
                respond = _bind(self.gcode, 'respond_info')
                try:
                    ignored = respond(timeout_format % (timeout_times, timeout, num))
                finally:
                    del respond
                del ignored
            elif ret != ((num + 1) & 255):
                error_times += 1
                respond = _bind(self.gcode, 'respond_info')
                try:
                    ignored = respond(error_format % (error_times, ret, num))
                finally:
                    del respond
                del ignored
            pause = _bind(self.reactor, 'pause')
            try:
                ignored = pause(_bind(self.reactor, 'monotonic')() + interval)
            finally:
                del pause
            del ignored
            respond = _bind(self.gcode, 'respond_info')
            try:
                ignored = respond(finish_format % (i, addr))
            finally:
                del respond
            del ignored


    def get_gcode_used_tnn(self):
        sdcard = _bind(self.printer, 'lookup_object')('virtual_sdcard')
        default_filament_colour = _bind(
            _bind(self.printer.lookup_object('k2_print').gcode_metadata, 'get')('metadata'), 'get')(
                'default_filament_colour', None)
        message_format = 'default_filament_colour: %s'
        emit = _bind(logging, 'info')
        try:
            ignored = emit(message_format % default_filament_colour)
        finally:
            del emit
        del ignored
        ret = {}
        if default_filament_colour is None:
            emit = _bind(logging, 'warning')
            try:
                ignored = emit('do not clear the content')
            finally:
                del emit
            del ignored
        elif default_filament_colour:
            for n in range(len(default_filament_colour)):
                if default_filament_colour[n]:
                    tn_format = 'T%d'
                    tn = tn_format % n
                    vtnn = self.box_state.Tn_map[tn]
                    tnn = self.box_state.Tnn_map[vtnn]
                    ret[vtnn] = tnn
        message_format = 'ret: %s'
        emit = _bind(logging, 'info')
        try:
            ignored = emit(message_format % ret)
        finally:
            del emit
        del ignored
        return ret


    def check_printing_used_material(self, addr, num):
        gcode_used_tnn = self.get_gcode_used_tnn()
        for vtnn, tnn in gcode_used_tnn.items():
            tnn_content = _bind(self.box_state, 'get_Tnn_content')(tnn)
            if tnn_content['addr'] == addr and tnn_content['num'] == num:
                message_format = 'vtnn = %s, tnn = %s, addr = %s, num = %s'
                emit = _bind(logging, 'info')
                try:
                    ignored = emit(message_format % (vtnn, tnn, addr, num))
                finally:
                    del emit
                del ignored
                return True
        return False


    def is_material_available(self, tnn):
        num_map = {'A': 0, 'B': 1, 'C': 2, 'D': 3}
        Tnn_content = _bind(self.box_state, 'get_Tnn_content')(tnn)
        query = self.communication_get_filament_sensor_state
        try:
            ignored = query(Tnn_content['addr'], 'MATERIAL')
        finally:
            del query
        del ignored
        material_status = _bind(self.box_state, 'get_Tn_inner_data')(
            Tnn_content['addr'], 'filament_sensor', subpart='material')
        if material_status & (1 << num_map[Tnn_content['num']]):
            return True
        return False


    def use_ending_material_flag_clear(self):
        if self.is_use_ending_material:
            self.is_use_ending_material = False
            emit = _bind(logging, 'info')
            try:
                ignored = emit('use_ending_material_flag_clear')
            finally:
                del emit
            del ignored
            if self.box_save.error == 'filament_err':
                clear = _bind(self.box_save, 'clear_err')
                try:
                    ignored = clear()
                finally:
                    del clear
                del ignored


    def retrude_process_clear_flag(self):
        clear = self.use_ending_material_flag_clear
        try:
            ignored = clear()
        finally:
            del clear
        del ignored
        if self.box_save.last_cmd:
            self.box_save.last_cmd_tmp = self.box_save.last_cmd
            update = self.update_filament_pos
            try:
                ignored = update(self.box_save.last_cmd, None)
            finally:
                del update
            del ignored
            self.box_save.last_cmd = None


    def check_material_refill(self):
        if self.box_save.error == 'filament_err':
            addr = self.box_save.error_tnn['addr']
            if addr is None:
                message_format = '[error] addr: %s'
                emit = _bind(logging, 'warning')
                try:
                    ignored = emit(message_format % addr)
                finally:
                    del emit
                del ignored
                return None
            query = self.communication_set_box_mode
            try:
                ignored = query(addr, 'IDLE')
            finally:
                del query
            del ignored
        if self.box_save.box_filament_err:
            if self.is_use_ending_material:
                self.is_use_ending_material = False
                run_script = _bind(self.gcode, 'run_script_from_command')
                try:
                    ignored = run_script('SET_FILAMENT_SENSOR SENSOR=filament_sensor ENABLE=0')
                finally:
                    del run_script
                del ignored
                register = _bind(self.reactor, 'register_callback')
                try:
                    ignored = register(self.material_auto_refill)
                finally:
                    del register
                del ignored
            else:
                run_script = _bind(self.gcode, 'run_script_from_command')
                try:
                    ignored = run_script('SET_FILAMENT_SENSOR SENSOR=filament_sensor ENABLE=0')
                finally:
                    del run_script
                del ignored
                emit = _bind(logging, 'warning')
                try:
                    ignored = emit('no auto refill')
                finally:
                    del emit
                del ignored
            self.box_save.box_filament_err = False


    def update_Tnn_map(self):
        emit = _bind(logging, 'info')
        try:
            ignored = emit('update_Tnn_map')
        finally:
            del emit
        del ignored
        index_map = {'material_type': 0, 'color_value': 1, 'tnn': 2, 'material_name': 3}
        same_tnn_list = []
        last_cmd = self.box_save.last_cmd
        Tnn_content = _bind(self.box_state, 'get_Tnn_content')(last_cmd)
        type = _bind(self.box_state, 'get_Tn_data')(
            Tnn_content['addr'], 'material_type', Tnn_content['num'])
        color = _bind(self.box_state, 'get_Tn_data')(
            Tnn_content['addr'], 'color_value', Tnn_content['num'])
        same_material_list = _bind(self.box_state, 'get_Tn_data')(0, 0, num=0, uppart='same_material')
        for i in range(len(same_material_list)):
            if same_material_list[i][index_map['material_type']] == type and _bind(
                    same_material_list[i][index_map['color_value']], 'lower')() == _bind(color, 'lower')():
                same_tnn_list = same_material_list[i][index_map['tnn']]
        message_format = 'same_material_list: %s, same_tnn_list: %s'
        emit = _bind(logging, 'info')
        try:
            ignored = emit(message_format % (same_material_list, same_tnn_list))
        finally:
            del emit
        del ignored
        if len(same_tnn_list) < 1:
            emit = _bind(logging, 'warning')
            try:
                ignored = emit('same_tnn_list len smaller than 2')
            finally:
                del emit
            del ignored
            return None
        next_tnn = None
        for i in range(len(same_tnn_list)):
            tnn_tmp = same_tnn_list[0]
            if self.is_material_available(tnn_tmp):
                next_tnn = tnn_tmp
                break
        if next_tnn is None:
            emit = _bind(logging, 'warning')
            try:
                ignored = emit('next_tnn is None')
            finally:
                del emit
            del ignored
            return None
        for vtnn, tnn in _bind(_bind(self.box_state, 'get_Tnn_map')(), 'items')():
            if tnn == last_cmd:
                message_format = 'BOX_MODIFY_TN %s=%s'
                run_script = _bind(self.gcode, 'run_script_from_command')
                try:
                    ignored = run_script(message_format % (vtnn, next_tnn))
                finally:
                    del run_script
                del ignored


    def material_auto_refill(self, eventtime):
        emit = _bind(logging, 'info')
        try:
            ignored = emit('material_auto_refill')
        finally:
            del emit
        del ignored
        index_map = {'material_type': 0, 'color_value': 1, 'tnn': 2, 'material_name': 3}
        last_cmd = self.box_save.last_cmd
        read = _bind(self.box_state, 'get_Tn_data')
        try:
            enabled = read(0, 0, num=0, uppart='auto_refill')
        finally:
            del read
        try:
            active = bool(enabled)
        finally:
            del enabled
        if not active or last_cmd is None:
            emit = _bind(logging, 'warning')
            try:
                ignored = emit('disable material automatic refill')
            finally:
                del emit
            del ignored
            emit = _bind(self.gcode, 'respond_info')
            try:
                ignored = emit('disable material automatic refill')
            finally:
                del emit
            del ignored
            self.box_state.Tn_data['filament'] = 0
            return
        read = _bind(self.box_state, 'get_Tnn_content')
        try:
            Tnn_content = read(last_cmd)
        finally:
            del read
        read = _bind(self.box_state, 'get_Tn_data')
        try:
            type = read(Tnn_content['addr'], 'material_type', Tnn_content['num'])
        finally:
            del read
        read = _bind(self.box_state, 'get_Tn_data')
        try:
            color = read(Tnn_content['addr'], 'color_value', Tnn_content['num'])
        finally:
            del read
        emit = _bind(logging, 'info')
        message_format = 'last_cmd: %s, Tnn_content: %s, type: %s, color: %s'
        try:
            ignored = emit(message_format % (last_cmd, Tnn_content, type, color))
        finally:
            del emit
        del ignored
        same_tnn_list = []
        read = _bind(self.box_state, 'get_Tn_data')
        try:
            same_material_list = read(0, 0, num=0, uppart='same_material')
        finally:
            del read
        for i in range(len(same_material_list)):
            if same_material_list[i][index_map['material_type']] == type and same_material_list[i][index_map['color_value']].lower() == color.lower():
                same_tnn_list = same_material_list[i][index_map['tnn']]
        emit = _bind(logging, 'info')
        message_format = 'same_material_list: %s, same_tnn_list: %s'
        try:
            ignored = emit(message_format % (same_material_list, same_tnn_list))
        finally:
            del emit
        del ignored
        if len(same_tnn_list) < 1:
            emit = _bind(logging, 'warning')
            try:
                ignored = emit('no identical supplies')
            finally:
                del emit
            del ignored
            emit = _bind(self.gcode, 'respond_info')
            try:
                ignored = emit('no identical supplies')
            finally:
                del emit
            del ignored
            self.box_state.Tn_data['filament'] = 0
            update = _bind(self, 'update_filament_pos')
            try:
                ignored = update(self.box_save.last_cmd, None)
            finally:
                del update
            del ignored
            self.box_save.last_cmd_tmp = self.box_save.last_cmd
            self.box_save.last_cmd = None
            emit = error
            message = gcode_arg = reactor_arg = stats_arg = state_arg = save_arg = None
            try:
                try:
                    message = key_filament_err % (Tnn_content['addr'], Tnn_content['num'])
                    gcode_arg = self.gcode
                    reactor_arg = self.reactor
                    stats_arg = self.print_stats
                    state_arg = stats_arg.state
                    stats_arg = None
                    save_arg = self.box_save
                    arguments = (message, gcode_arg, reactor_arg, state_arg, save_arg)
                    message = gcode_arg = reactor_arg = state_arg = save_arg = None
                    try:
                        ignored = emit(*arguments)
                    finally:
                        del arguments
                finally:
                    del message
                    gcode_arg = None
                    reactor_arg = None
                    stats_arg = None
                    state_arg = None
                    save_arg = None
                    del gcode_arg, reactor_arg, stats_arg, state_arg, save_arg
            finally:
                del emit
            del ignored
            return
        tnn_tmp = last_cmd
        next_tnn = None
        for i in range(len(same_tnn_list)):
            tnn_tmp = same_tnn_list[i]
            if tnn_tmp[1] < last_cmd[1]:
                continue
            elif tnn_tmp[1] == last_cmd[1] and tnn_tmp[2] < last_cmd[2]:
                continue
            read = _bind(self, 'is_material_available')
            try:
                available = read(tnn_tmp)
            finally:
                del read
            try:
                active = bool(available)
            finally:
                del available
            if active:
                next_tnn = tnn_tmp
                break
        if next_tnn is None:
            for i in range(len(same_tnn_list)):
                tnn_tmp = same_tnn_list[i]
                read = _bind(self, 'is_material_available')
                try:
                    available = read(tnn_tmp)
                finally:
                    del read
                try:
                    active = bool(available)
                finally:
                    del available
                if active:
                    next_tnn = tnn_tmp
                    break
        if next_tnn is None:
            emit = _bind(logging, 'warning')
            try:
                ignored = emit('no tray with ingredients found')
            finally:
                del emit
            del ignored
            emit = _bind(self.gcode, 'respond_info')
            try:
                ignored = emit('no tray with ingredients found')
            finally:
                del emit
            del ignored
            self.box_state.Tn_data['filament'] = 0
            self.box_save.last_cmd_tmp = self.box_save.last_cmd
            self.box_save.last_cmd = None
            emit = error
            message = gcode_arg = reactor_arg = stats_arg = state_arg = save_arg = None
            try:
                try:
                    message = key_filament_err % (Tnn_content['addr'], Tnn_content['num'])
                    gcode_arg = self.gcode
                    reactor_arg = self.reactor
                    stats_arg = self.print_stats
                    state_arg = stats_arg.state
                    stats_arg = None
                    save_arg = self.box_save
                    arguments = (message, gcode_arg, reactor_arg, state_arg, save_arg)
                    message = gcode_arg = reactor_arg = state_arg = save_arg = None
                    try:
                        ignored = emit(*arguments)
                    finally:
                        del arguments
                finally:
                    del message
                    gcode_arg = None
                    reactor_arg = None
                    stats_arg = None
                    state_arg = None
                    save_arg = None
                    del gcode_arg, reactor_arg, stats_arg, state_arg, save_arg
            finally:
                del emit
            del ignored
            return
        emit = _bind(logging, 'info')
        message_format = 'next_tnn = %s'
        try:
            ignored = emit(message_format % next_tnn)
        finally:
            del emit
        del ignored
        emit = _bind(self.gcode, 'respond_info')
        message_format = 'next_tnn = %s'
        try:
            ignored = emit(message_format % next_tnn)
        finally:
            del emit
        del ignored
        self.box_save.last_cmd_tmp = self.box_save.last_cmd
        self.box_save.last_cmd = None
        if next_tnn != self.box_save.last_cmd_tmp:
            read = _bind(self.box_state, 'get_Tnn_content')
            try:
                tnn_content = read(self.box_save.last_cmd_tmp)
            finally:
                del read
            update = _bind(self.box_state, 'modify_Tn_data')
            try:
                ignored = update(tnn_content['addr'], 'none', 'vender', tnn_content['num'])
            finally:
                del update
            del ignored
            update = _bind(self.box_state, 'update_same_material_list')
            try:
                ignored = update()
            finally:
                del update
            del ignored
            read = _bind(self.box_state, 'get_Tnn_map')
            try:
                mapping = read()
            finally:
                del read
            try:
                read = _bind(mapping, 'items')
            finally:
                del mapping
            try:
                entries = read()
            finally:
                del read
            try:
                iterator = iter(entries)
            finally:
                del entries
            try:
                for vtnn, tnn in iterator:
                    if tnn == last_cmd:
                        emit = _bind(self.gcode, 'run_script_from_command')
                        message_format = 'BOX_MODIFY_TN %s=%s'
                        try:
                            ignored = emit(message_format % (vtnn, next_tnn))
                        finally:
                            del emit
                        del ignored
            finally:
                del iterator
        read = _bind(self.box_state, 'get_Tnn_map')
        try:
            mapping = read()
        finally:
            del read
        try:
            read = _bind(mapping, 'items')
        finally:
            del mapping
        try:
            entries = read()
        finally:
            del read
        try:
            iterator = iter(entries)
        finally:
            del entries
        try:
            for vtnn, tnn in iterator:
                if tnn == next_tnn:
                    clear = _bind(self.box_save, 'clear_err')
                    try:
                        ignored = clear()
                    finally:
                        del clear
                    del ignored
                    self.box_save.material_auto_refill_flag = True
                    emit = _bind(self.gcode, 'run_script_from_command')
                    try:
                        ignored = emit(vtnn)
                    finally:
                        del emit
                    del ignored
                    return

        finally:
            del iterator


    def go_to_extrude_pos(self):
        if 'x' not in _bind(self.toolhead, 'get_status')(_bind(self.reactor, 'monotonic')())['homed_axes']:
            _bind(self.gcode, 'run_script_from_command')('G28 X Y')
            _bind(self.gcode, 'run_script_from_command')('BED_MESH_CLEAR')
        _bind(self, 'set_flushing_sign')()
        position = self.toolhead.get_position()
        y = position[1]
        _bind(self.gcode, 'run_script_from_command')('G90')
        # Repeated purge requests must keep the flushing state without another brush detour.
        if (abs(position[0] - self.boxcfg.extrude_pos_x) < .005
                and abs(y - self.boxcfg.extrude_pos_y) < .005):
            return
        x_format = 'G0 X%.2f F%.2f'
        y_format = 'G0 Y%.2f F%.2f'
        xy_format = 'G0 X%.2f Y%.2f F%.2f'
        if y <= self.boxcfg.safe_pos_y:
            _bind(self.gcode, 'run_script_from_command')(y_format % (self.boxcfg.safe_pos_y, 24000))
            _bind(self.gcode, 'run_script_from_command')(x_format % (self.boxcfg.clean_left_pos_x + 4, 24000))
        else:
            _bind(self.gcode, 'run_script_from_command')(x_format % (self.boxcfg.clean_left_pos_x, self.boxcfg.clean_velocity))
        _bind(self.gcode, 'run_script_from_command')(y_format % (self.boxcfg.clean_left_pos_y, self.boxcfg.clean_velocity))
        _bind(self.gcode, 'run_script_from_command')(xy_format % (
            self.boxcfg.clean_left_pos_x, self.boxcfg.extrude_pos_y, self.boxcfg.clean_velocity))
        _bind(self.gcode, 'run_script_from_command')(xy_format % (
            self.boxcfg.extrude_pos_x, self.boxcfg.extrude_pos_y, self.boxcfg.clean_velocity))


    def set_cool_temp(self):
        if self.print_stats.state == 'paused':
            if (_bind(self.toolhead.get_extruder(), 'get_heater')().target_temp
                    >= _bind(self.toolhead.get_extruder(), 'get_heater')().min_extrude_temp):
                temperature_format = 'SET_GCODE_VARIABLE MACRO=PRINTER_PARAM VARIABLE=hotend_temp VALUE=%s'
                _bind(self.gcode, 'run_script_from_command')(temperature_format %
                    _bind(self.toolhead.get_extruder(), 'get_heater')().target_temp)
            _bind(self.gcode, 'run_script_from_command')('M104 S140')
        elif self.print_stats.state == 'printing':
            _bind(logging, 'warning')('unknown action')
        else:
            _bind(self.gcode, 'run_script_from_command')('M104 S0')


    def get_material_target_temp(self, tnn=None, type=None):
        if tnn is None and type is None:
            logging.warning('last_tnn is none')
            return self.boxcfg.Tn_extrude_temp
        _type = type
        if type is None:
            Tnn_content = self.box_state.get_Tnn_content(tnn)
            _type = self.box_state.get_Tn_data(Tnn_content['addr'], 'material_type', Tnn_content['num'])
            if _type is None or _type == 'None':
                logging.warning('failed to obtain consumable type')
                return self.boxcfg.Tn_extrude_temp
        _type = _type[1:6]
        next_temp = None
        try:
            if os.path.exists(self.material_database):
                with open(self.material_database, 'r') as file:
                    data = _material_database(file)
                    material_list = data.get('result', {}).get('list', [])
                    for i in range(len(material_list)):
                        material_id = material_list[i].get('base', {}).get('id', None)
                        nozzle_temp = material_list[i].get('kvParam', {}).get('nozzle_temperature', None)
                        if material_id is not None and nozzle_temp is not None and material_id == _type:
                            next_temp = int(nozzle_temp)
                            break
        except Exception as err:
            logging.error('material database get nozzle temp: %s' % err)
        if next_temp is None:
            logging.warning('get material target temp fail')
            return self.boxcfg.Tn_extrude_temp
        logging.info('get next material temp: %d' % next_temp)
        return next_temp


    def get_material_target_max_temp(self, tnn=None, type=None):
        if tnn is None and type is None:
            logging.warning('last_tnn is none')
            return self.boxcfg.Tn_extrude_temp
        _type = type
        if type is None:
            Tnn_content = self.box_state.get_Tnn_content(tnn)
            _type = self.box_state.get_Tn_data(Tnn_content['addr'], 'material_type', Tnn_content['num'])
            if _type is None or _type == 'None':
                logging.warning('failed to obtain consumable type')
                return self.boxcfg.Tn_extrude_temp
        _type = _type[1:6]
        next_temp = None
        try:
            if os.path.exists(self.material_database):
                with open(self.material_database, 'r') as file:
                    data = _material_database(file)
                    material_list = data.get('result', {}).get('list', [])
                    for i in range(len(material_list)):
                        material_id = material_list[i].get('base', {}).get('id', None)
                        nozzle_max_temp = material_list[i].get('base', {}).get('maxTemp', None)
                        if material_id is not None and nozzle_max_temp is not None and material_id == _type:
                            next_temp = int(nozzle_max_temp)
                            break
        except Exception as err:
            logging.error('material database get nozzle max temp: %s' % err)
        if next_temp is None:
            logging.warning('get material target max temp fail')
            return self.boxcfg.Tn_extrude_temp
        logging.info('get next material max temp: %d' % next_temp)
        return next_temp


    def get_material_max_extrusion_speed(self, tnn=None, type=None):
        if tnn is None and type is None:
            logging.warning('last_tnn is none')
            return None
        _type = type
        if type is None:
            Tnn_content = self.box_state.get_Tnn_content(tnn)
            _type = self.box_state.get_Tn_data(Tnn_content['addr'], 'material_type', Tnn_content['num'])
            if _type is None or _type == 'None':
                logging.warning('failed to obtain consumable type')
                return None
        _type = _type[1:6]
        max_extrusion_speed = None
        try:
            if os.path.exists(self.material_database):
                with open(self.material_database, 'r') as file:
                    data = _material_database(file)
                    material_list = data.get('result', {}).get('list', [])
                    for i in range(len(material_list)):
                        material_id = material_list[i].get('base', {}).get('id', None)
                        max_volumetric_speed = material_list[i].get('kvParam', {}).get('filament_max_volumetric_speed', None)
                        if material_id is not None and max_volumetric_speed is not None and material_id == _type:
                            self.gcode.respond_info('max_volumetric_speed: %s' % max_volumetric_speed)
                            max_extrusion_speed = float(max_volumetric_speed) / 2.4
                            break
        except Exception as err:
            logging.error('material database get extrusion speed: %s' % err)
        if max_extrusion_speed is None:
            logging.warning('get material extrusion speed fail')
            return None
        logging.info('get material extrusion speed: %d' % max_extrusion_speed)
        return max_extrusion_speed


    def get_flush_temp(self, last_tnn, current_tnn):
        last_temp = self.get_material_target_temp(type=self.remain_material.type)
        next_temp = self.get_material_target_temp(current_tnn)
        flush_temp = next_temp if next_temp > last_temp else last_temp
        self.gcode.respond_info('flush_temp: %d' % flush_temp)
        return last_temp, next_temp, flush_temp


    def get_flush_max_temp(self, last_tnn, current_tnn):
        last_max_temp = self.get_material_target_max_temp(type=self.remain_material.type)
        next_max_temp = self.get_material_target_max_temp(current_tnn)
        flush_max_temp = next_max_temp if next_max_temp > last_max_temp else last_max_temp
        self.gcode.respond_info('flush_max_temp: %d' % flush_max_temp)
        return last_max_temp, next_max_temp, flush_max_temp


    def get_flush_velocity(self, last_tnn, current_tnn):
        last_speed = self.get_material_max_extrusion_speed(type=self.remain_material.type)
        nest_speed = self.get_material_max_extrusion_speed(current_tnn)
        if last_speed is None or nest_speed is None:
            logging.warning('failed to get flush speed from file')
            return self.boxcfg.Tn_extrude_velocity, self.boxcfg.Tn_extrude_percent
        velocity = []
        percent = []
        velocity.append(nest_speed * 60)
        percent.append(1)
        for i in range(len(velocity)):
            message_format = 'num: %d, velocity: %f, percent %f'
            self.gcode.respond_info(message_format % (i, velocity[i], percent[i]))
        return velocity, percent


    def move_to_safe_pos(self):
        if 'xy' not in _bind(self.toolhead, 'get_status')(_bind(self.reactor, 'monotonic')())['homed_axes']:
            return
        _bind(self.gcode, 'run_script_from_command')('G90')
        y_format = 'G0 Y%.2f F%.2f'
        _bind(self.gcode, 'run_script_from_command')(y_format % (self.boxcfg.safe_pos_y, self.boxcfg.clean_velocity))
        x_format = 'G0 X%.2f F%.2f'
        _bind(self.gcode, 'run_script_from_command')(x_format % (self.boxcfg.safe_pos_x, self.boxcfg.clean_velocity))
        _bind(self, 'reset_flushing_sign')()


    def Tn_Extrude(self, extrude, velocity, temp, max_temp=None, percent=None, tnn=None):
        _bind(self, 'quickly_wait_heating')(temp)
        _bind(self.gcode, 'run_script_from_command')('G91')
        if tnn != None:
            Tnn_content = _bind(self.box_state, 'get_Tnn_content')(tnn)
        else:
            Tnn_content = None
        message_format = 'extrude = %s, velocity: %s, temp: %s, percent: %s, tnn: %s'
        _bind(logging, 'info')(message_format % (extrude, velocity, temp, percent, tnn))
        if percent is None or len(percent) == 1:
            if percent is None:
                coefficient = 1
            else:
                coefficient = percent[0]
            if tnn is None or extrude < 0:
                ret = _bind(self, 'check_and_extrude')(extrude * coefficient, velocity[0])
                if ret is False:
                    _bind(logging, 'warning')('extrude err')
                    return False
            else:
                diff = 0
                ret = _bind(self, 'communication_measuring_wheel')(Tnn_content['addr'], 'GET')
                if ret is False:
                    return False
                last_measuring_wheel = _bind(self.box_state, 'get_Tn_data')(Tnn_content['addr'], 'measuring_wheel')
                remain_length = extrude * coefficient
                length = []
                _bind(self, 'cal_flush_list')(extrude * coefficient, length)
                _bind(logging, 'info')('length: %s' % length)
                for i in range(len(length)):
                    if i > 0 and i == len(length) - 1:
                        ret = _bind(self, 'check_flush_temp_and_extrude')(length[i], self.boxcfg.max_temp_flush_times, temp, max_temp, velocity, max_temp_flush=True)
                    else:
                        ret = _bind(self, 'check_flush_temp_and_extrude')(length[i], i, temp, max_temp, velocity, max_temp_flush=True)
                    if ret is False:
                        return False
                    ret = _bind(self, 'communication_measuring_wheel')(Tnn_content['addr'], 'GET')
                    if ret is False:
                        return False
                    current_measuring_wheel = _bind(self.box_state, 'get_Tn_data')(Tnn_content['addr'], 'measuring_wheel')
                    diff += current_measuring_wheel - last_measuring_wheel
                    message_format = 'last_measuring_wheel = %s, current_measuring_wheel = %s, diff = %s'
                    _bind(logging, 'info')(message_format % (last_measuring_wheel, current_measuring_wheel, diff))
                    last_measuring_wheel = current_measuring_wheel
                    _bind(self, 'blow')()
                    if self.box_save.error is not None:
                        _bind(logging, 'warning')('error: %s' % self.box_save.error)
                        return False
                    if i == len(length) - 1:
                        _bind(self, 'nozzle_clean')()
                    else:
                        _bind(self, 'nozzle_clean')(circle=False)
                if diff > self.boxcfg.diff_length:
                    error(key_nozzle_blocked_err, self.gcode, self.reactor, self.print_stats.state, self.box_save)
                    return False
        elif Tnn_content != None:
            ret = _bind(self, 'communication_measuring_wheel')(Tnn_content['addr'], 'GET')
            if ret is False:
                return False
            last_measuring_wheel = _bind(self.box_state, 'get_Tn_data')(Tnn_content['addr'], 'measuring_wheel')
            length = 0
            for i in range(len(percent)):
                ret = _bind(self, 'check_and_extrude')(extrude * percent[i], velocity[i])
                if ret is False:
                    _bind(logging, 'warning')('extrude err')
                    return False
                length += extrude * percent[i]
                if length >= 2 * self.boxcfg.buffer_empty_len:
                    ret = _bind(self, 'communication_measuring_wheel')(Tnn_content['addr'], 'GET')
                    if ret is False:
                        return False
                    current_measuring_wheel = _bind(self.box_state, 'get_Tn_data')(Tnn_content['addr'], 'measuring_wheel')
                    diff = current_measuring_wheel - last_measuring_wheel
                    message_format = 'last_measuring_wheel = %s, current_measuring_wheel = %s, diff = %s'
                    _bind(logging, 'info')(message_format % (last_measuring_wheel, current_measuring_wheel, diff))
                    last_measuring_wheel = current_measuring_wheel
                    length = 0
                    if diff > self.boxcfg.diff_length:
                        error(key_nozzle_blocked_err, self.gcode, self.reactor, self.print_stats.state, self.box_save)
                        return False
                _bind(self, 'blow')()
                _bind(self, 'nozzle_clean')()
                _bind(self, 'go_to_extrude_pos')()
        else:
            for i in range(len(percent)):
                ret = _bind(self, 'check_and_extrude')(extrude * percent[i], velocity[i])
                if ret is False:
                    _bind(logging, 'warning')('extrude err')
                    return False
                _bind(self, 'blow')()
                _bind(self, 'nozzle_clean')()
                _bind(self, 'go_to_extrude_pos')()
        return True


    def make_material_loose(self):
        _bind(logging, 'info')('move to retry')
        x = _bind(_bind(self.config, 'getsection')('stepper_x'), 'getfloat')('position_max') - 10
        y = self.boxcfg.safe_pos_y - 10
        _bind(self.gcode, 'run_script_from_command')('BOX_NOZZLE_CLEAN')
        command_format = 'G0 X%.2f Y%.2f F%.2f'
        _bind(self.gcode, 'run_script_from_command')(command_format % (x, y, self.boxcfg.clean_velocity))
        _bind(self.gcode, 'run_script_from_command')(command_format % (x, y / 2, self.boxcfg.clean_velocity))
        _bind(self.gcode, 'run_script_from_command')(command_format % (x, y, self.boxcfg.clean_velocity))
        _bind(self.gcode, 'run_script_from_command')('BOX_GO_TO_EXTRUDE_POS')
        _bind(self.gcode, 'run_script_from_command')('M400')


    def box_retrude_material_filament_err_part(self):
        num_map = {'A': 0, 'B': 1, 'C': 2, 'D': 3}
        last_Tnn_content = _bind(self.box_state, 'get_Tnn_content')(self.box_save.last_cmd)
        _bind(self, 'communication_get_filament_sensor_state')(last_Tnn_content['addr'], 'MATERIAL')
        material_status = _bind(self.box_state, 'get_Tn_inner_data')(last_Tnn_content['addr'], 'filament_sensor', subpart='material')
        if material_status & (1 << num_map[last_Tnn_content['num']]) == 0:
            _bind(logging, 'info')('is filament error')
            ret = _bind(self, 'communication_set_box_mode')(last_Tnn_content['addr'], 'PRINT', last_Tnn_content['num'])
            if ret is False:
                return False
            _bind(self.box_state, 'modify_Tn_data')(0, 1, 0, uppart='filament_useup')
            _bind(self, 'extrusion_all_materials')()
            _bind(self.box_state, 'modify_Tn_data')(0, 0, 0, uppart='filament_useup')
            _bind(self, 'communication_get_filament_sensor_state')(last_Tnn_content['addr'], 'MATERIAL')
            material_status = _bind(self.box_state, 'get_Tn_inner_data')(last_Tnn_content['addr'], 'filament_sensor', subpart='material')
            if material_status & (1 << num_map[last_Tnn_content['num']]) == 0:
                _bind(self.box_state, 'modify_Tn_data')(last_Tnn_content['addr'], 'none', 'vender', last_Tnn_content['num'])
                _bind(self.box_state, 'update_same_material_list')()
                _bind(self, 'update_Tnn_map')()
            _bind(self, 'retrude_process_clear_flag')()
            return False
        return True


    def extruder_extrude(self, tnn):
        if _bind(self.box_state, 'get_Tn_data')(0, 0, uppart='filament_useup'):
            _bind(self.box_state, 'modify_Tn_data')(0, 0, 0, uppart='filament_useup')
        return True


    def extrusion_all_materials(self):
        temp = _bind(self, 'get_material_target_temp')(type=self.remain_material.type)
        if self.print_stats.state == 'paused' and _bind(self.config, 'has_section')('gcode_macro PRINTER_PARAM'):
            if _bind(self.printer, 'lookup_object')('gcode_macro PRINTER_PARAM').variables['hotend_temp']:
                temp = _bind(self.printer, 'lookup_object')('gcode_macro PRINTER_PARAM').variables['hotend_temp']
                _bind(logging, 'info')('hotend_temp: %s' % temp)
        velocity = _bind(self, 'get_material_max_extrusion_speed')(type=self.remain_material.type)
        message_format = 'extrude all material, last_cmd: %s, temp: %s'
        _bind(logging, 'info')(message_format % (self.box_save.last_cmd, temp))
        if velocity is None:
            velocity = self.boxcfg.Tn_extrude_velocity[0]
        else:
            velocity = velocity * 60
        _bind(self, 'quickly_wait_heating')(temp)
        length = 0
        break_flag = False
        while True:
            _bind(self, 'go_to_extrude_pos')()
            ret = _bind(self, 'check_and_extrude')(80, velocity, wait=False)
            wait = 80 / velocity * 60
            if wait > 0.5:
                _bind(self.reactor, 'pause')(wait - 0.5)
                _bind(self.gcode, 'run_script_from_command')('M400')
            else:
                _bind(self.gcode, 'run_script_from_command')('M400')
            if ret is False:
                _bind(logging, 'warning')('extrude err')
                return False
            _bind(self, 'blow')()
            _bind(self, 'nozzle_clean')(circle=False)
            length += 80
            if break_flag:
                break
            if _bind(self, 'get_filament_sensor_detect')() is False:
                break_flag = True
            elif length >= self.boxcfg.max_tube_length:
                logging.error('K2 CFS tail sensor still occupied after %s mm', length)
                return False
        _bind(self, 'set_cool_temp')()
        if self.box_save.last_cmd:
            Tnn_content = _bind(self.box_state, 'get_Tnn_content')(self.box_save.last_cmd)
            _bind(self, 'communication_set_box_mode')(Tnn_content['addr'], 'IDLE')
        return True


    def box_extrude_material(self, tnn):
        self._transport_uncertain_latched = False
        if self.print_stats.state == 'printing' or self.print_stats.state == 'paused':
            _bind(self.box_state, 'update_tn_save_data')('last_cmd', last_cmd=tnn, save=True)
        tnn_content = self.box_state.get_Tnn_content(tnn)
        _bind(self, 'communication_set_box_mode')(tnn_content['addr'], 'IDLE')
        if self.print_stats.state != 'printing' and self.print_stats.state != 'paused':
            for addr in range(1, 5):
                if self.check_connect(addr):
                    self.communication_tighten_up_enable(addr, 'DISABLE')
        y_pos = self.toolhead.get_position()[1]
        if y_pos < self.boxcfg.safe_pos_y:
            self.go_to_extrude_pos()
        if self.box_save.last_cmd is None:
            last_cmd = self.box_save.last_cmd_tmp
        else:
            last_cmd = self.box_save.last_cmd
        current_cmd = tnn
        ret = self.is_material_available(tnn)
        if ret is False:
            error(key_filament_err % (tnn_content['addr'], tnn_content['num']),
                  self.gcode, self.reactor, self.print_stats.state, self.box_save)
            _bind(self.box_save, 'recode_err')(
                'filament_err', last_cmd, current_cmd,
                addr=tnn_content['addr'], filament_sensor_enable=False)
            return ret
        velocity, _ = self.get_flush_velocity(last_cmd, current_cmd)
        _, _, flush_temp = self.get_flush_temp(last_cmd, current_cmd)
        if last_cmd is None and self.remain_material.type is None:
            self.quickly_wait_heating(240)
        elif (self.printer.lookup_object('k2_print').gcode_metadata
              and self.printer.lookup_object('k2_print').gcode_metadata['metadata']['model_info']['multicolor_method'] == 0):
            self.quickly_wait_heating(flush_temp)
        elif not self.toolhead.get_extruder().get_heater().can_extrude:
            self.quickly_wait_heating(flush_temp)
        ret = _bind(self, 'communication_extrude_process')(
            tnn_content['addr'], tnn_content['num'], stage=0, report_err=False)
        if ret is False:
            if self._is_transport_uncertain():
                _bind(logging, 'warning')(
                    'K2 CFS blocking connection retry after uncertain EXTRUDE_PROCESS stage=0 transport')
                return False
            ret = _bind(self, 'extrude_process_auto_retry_process')(
                tnn, 'auto_retry_connection', [0, 0, 0, 0])
            if ret is False:
                return ret
        _bind(self.box_state, 'modify_Tn_data')(
            tnn_content['addr'],
            str(int(_bind(self.box_state, 'get_Tn_data')(
                tnn_content['addr'], 'change_color_num', tnn_content['num'])) + 1),
            'change_color_num', tnn_content['num'])
        self.enable_filament_sensor()
        self.disable_heart_process(tnn)
        ret = _bind(self, 'box_extrude_material_part')(tnn, [0, 0, 0, 0])
        self.enable_heart_process(tnn)
        self.disable_filament_sensor()
        if ret is False:
            return False
        ret = _bind(self, 'communication_set_box_mode')(
            tnn_content['addr'], 'PRINT', tnn_content['num'])
        if ret is False:
            return ret
        if (self.printer.lookup_object('k2_print').gcode_metadata
                and self.printer.lookup_object('k2_print').gcode_metadata['metadata']['model_info']['multicolor_method'] == 1
                and last_cmd is None and self.remain_material.type is None):
            self.gcode.run_script_from_command('M106 P0 S50')
            self.gcode.run_script_from_command('M400')
            _bind(self, 'check_and_extrude')(90, velocity[0] * 0.5)
            _bind(self, 'nozzle_clean')(circle=False)
            self.go_to_extrude_pos()
            self.quickly_wait_heating(flush_temp)
        if self.print_stats.state != 'printing' and self.print_stats.state != 'paused':
            for addr in range(1, 5):
                if self.check_connect(addr):
                    self.communication_tighten_up_enable(addr, 'ENABLE')
        self.box_save.last_cmd = tnn
        self.update_filament_pos(tnn, current_cmd)
        self.box_state.Tn_data['filament'] = 1
        return True


    def box_extrude_material_part(self, tnn, retry):
        self.extrude_tnn = tnn
        tnn_content = self.box_state.get_Tnn_content(tnn)
        retry_index = {
            'auto_retry_connection': 0,
            'auto_retry_filament_sensor': 1,
            'auto_retry_filament_sensor_second': 2,
            'auto_retry_extruder_gear': 3,
        }
        self.extrude_process_stage7_ret = {'ret': None, 'extrude_process_ret_state': None}
        ret = _bind(self, 'communication_extrude_process')(
            tnn_content['addr'], tnn_content['num'], stage=4)
        if ret is False:
            return ret
        timeout = 90
        while timeout > 0:
            _bind(logging, 'info')('timeout = %s' % timeout)
            if not self.extrude_process_stage7_flag and (timeout & 1) == 0:
                ret = _bind(self, 'communication_extrude_process')(
                    tnn_content['addr'], tnn_content['num'], stage=5, report_err=False)
                if ret is False:
                    if self._is_transport_uncertain():
                        _bind(logging, 'warning')(
                            'K2 CFS blocking retry after uncertain EXTRUDE_PROCESS stage=5 transport')
                        self.disable_filament_sensor()
                        self.extrude_process_stage7_flag = False
                        return False
                    if _bind(self.box_state, 'get_Tn_data')(tnn_content['addr'], 'state') == 'disconnect':
                        self.disable_filament_sensor()
                        self.extrude_process_stage7_flag = False
                        return ret
                    if self.extrude_process_ret_state == 'EXTRUDE_ERR7':
                        error(key_joint_err, self.gcode, self.reactor, self.print_stats.state, self.box_save)
                        self.box_save.error_tnn['addr'] = tnn_content['addr']
                        return ret
                    if (self.extrude_process_ret_state == 'EXTRUDE_ERR6'
                            or (self.extrude_process_ret_state == 'EXTRUDE_ERR8'
                                and self.get_filament_sensor_detect() is False)):
                        if retry[retry_index['auto_retry_filament_sensor']] == 0:
                            ret = self.extrude_process_auto_retry_process(tnn, 'auto_retry_filament_sensor', retry)
                            return ret
                        elif retry[retry_index['auto_retry_filament_sensor']] == 1:
                            if retry[retry_index['auto_retry_filament_sensor_second']] == 0:
                                ret = self.extrude_process_auto_retry_process(tnn, 'auto_retry_filament_sensor_second', retry)
                                return ret
                        if retry[retry_index['auto_retry_filament_sensor_second']] == 1:
                            error(key_extrude_err2 % (tnn_content['addr'], tnn_content['num']),
                                  self.gcode, self.reactor, self.print_stats.state, self.box_save)
                            self.box_save.error_tnn['addr'] = tnn_content['addr']
                            return ret
            if self.extrude_process_stage7_flag:
                _bind(logging, 'info')('extrude_process_stage7_ret: %s' % self.extrude_process_stage7_ret)
                self.extrude_process_stage7_flag = False
                ret = self.extrude_process_stage7_ret['ret']
                if ret is False:
                    break
                ret = self.box_extrude_material_stage8(tnn)
                if ret:
                    return True
                if self._is_transport_uncertain():
                    _bind(logging, 'warning')(
                        'K2 CFS blocking gear retry after uncertain stage8 transport')
                    return False
                if retry[retry_index['auto_retry_extruder_gear']] == 0:
                    ret = self.extrude_process_auto_retry_process(tnn, 'auto_retry_extruder_gear', retry)
                    return ret
                error(key_extrude_err3 % (tnn_content['addr'], tnn_content['num']),
                      self.gcode, self.reactor, self.print_stats.state, self.box_save)
                return False
            _bind(self.reactor, 'pause')(self.reactor.monotonic() + 1)
            timeout -= 1
        if timeout <= 0:
            logging.warning('extrude process timeout')
            error(key_extrude_err2 % (tnn_content['addr'], tnn_content['num']),
                  self.gcode, self.reactor, self.print_stats.state, self.box_save)
            self.box_save.error_tnn['addr'] = tnn_content['addr']
            return False
        elif ret is False and self.extrude_process_ret_state == 'EXTRUDE_ERR9':
            error(key_joint_err, self.gcode, self.reactor, self.print_stats.state, self.box_save)
            self.box_save.error_tnn['addr'] = tnn_content['addr']
            return ret
        elif ret is False and self.extrude_process_stage7_ret['extrude_process_ret_state'] == 'EXTRUDE_ERR10':
            error(key_extrude_err5, self.gcode, self.reactor, self.print_stats.state, self.box_save)
            self.box_save.error_tnn['addr'] = tnn_content['addr']
        else:
            message_format = '[unknown scene] ret: %s, ret_state: %s'
            _bind(logging, 'warning')(message_format % (ret, self.extrude_process_ret_state))
        return False


    def box_extrude_material_stage8(self, tnn):
        tnn_content = self.box_state.get_Tnn_content(tnn)
        for _ in range(self.boxcfg.extrude_material_times):
            self.gcode.run_script_from_command('G91')
            movement_format = 'G0 E%.2f F%.2f'
            _bind(self.gcode, 'run_script_from_command')(
                movement_format % (self.boxcfg.extrude_material_len_for_extruder,
                                   self.boxcfg.extrude_material_velocity))
            self.gcode.run_script_from_command('G90')
            _bind(self.reactor, 'pause')(self.reactor.monotonic() + 1)
            ret = _bind(self, 'communication_extrude_process')(
                tnn_content['addr'], tnn_content['num'], stage=7,
                extrude=self.boxcfg.extrude_material_len_for_box, report_err=False)
            if ret is False:
                if self.extrude_process_ret_state == 'EXTRUDE_ERR9':
                    for _ in range(5):
                        self.gcode.run_script_from_command('G91')
                        self.gcode.run_script_from_command('G0 E6 F180')
                        self.gcode.run_script_from_command('G90')
                        self.gcode.run_script_from_command('M400')
                        _bind(self.reactor, 'pause')(self.reactor.monotonic() + 0.5)
                        _bind(self, 'communication_get_buffer_state')(tnn_content['addr'])
                        buffer_state = _bind(self.box_state, 'get_Tn_inner_data')(
                            tnn_content['addr'], None, uppart='buffer')
                        if buffer_state != 1:
                            return True
                else:
                    message_format = '[unknown scene] ret: %s, ret_state: %s'
                    _bind(logging, 'warning')(message_format % (ret, self.extrude_process_ret_state))
                logging.warning('stage8 return error')
                return False
            self.gcode.run_script_from_command('M400')
            _bind(self.reactor, 'pause')(self.reactor.monotonic() + 0.5)
            _bind(self, 'communication_get_buffer_state')(tnn_content['addr'])
            buffer_state = _bind(self.box_state, 'get_Tn_inner_data')(
                tnn_content['addr'], None, uppart='buffer')
            if buffer_state != 1:
                return True
        logging.warning('buffer is always full')
        return False


    def box_retrude_material(self):
        self._transport_uncertain_latched = False
        tail = _cfs_tail_slot(self)
        if tail is not None:
            _drain_cfs_tail(self, tail)
            return True
        num_map = {'A': 0, 'B': 1, 'C': 2, 'D': 3}
        if self.box_save.last_cmd is None:
            sensor_status = self.get_filament_sensor_detect()
            if not sensor_status:
                for addr in range(1, 5):
                    if not self.check_connect(addr):
                        continue
                    self.communication_get_filament_sensor_state(addr, 'CONNECTIONS')
                    self.communication_get_filament_sensor_state(addr, 'MATERIAL')
                    connections_status = _bind(self.box_state, 'get_Tn_inner_data')(
                        addr, 'filament_sensor', subpart='connections')
                    material_status = _bind(self.box_state, 'get_Tn_inner_data')(
                        addr, 'filament_sensor', subpart='material')
                    for num in num_map:
                        if (connections_status & material_status & (1 << num_map[num])) != 0:
                            ret = self.communication_set_box_mode(addr, 'IDLE')
                            if ret is False:
                                return False
                            ret = self.communication_retrude_process(addr, num, 'MATERIAL')
                            if ret is False:
                                self.box_save.error_tnn['addr'] = addr
                                return False
                self.retrude_process_clear_flag()
                return True

            times = [[], []]
            _bind(self, 'quickly_wait_heating')(self.boxcfg.Tn_extrude_temp)
            for addr in range(1, 5):
                if not self.check_connect(addr):
                    continue
                self.communication_get_filament_sensor_state(addr, 'CONNECTIONS')
                self.communication_get_filament_sensor_state(addr, 'MATERIAL')
                connections_status = _bind(self.box_state, 'get_Tn_inner_data')(
                    addr, 'filament_sensor', subpart='connections')
                material_status = _bind(self.box_state, 'get_Tn_inner_data')(
                    addr, 'filament_sensor', subpart='material')
                if connections_status == 0:
                    continue
                ret = self.communication_set_box_mode(addr, 'IDLE')
                if ret is False:
                    return False
                ret = self.communication_retrude_process(addr, '0', 'BUFFER')
                if ret is False:
                    if self._is_transport_uncertain():
                        _bind(logging, 'warning')(
                            'K2 CFS blocking retrude fallback after uncertain BUFFER transport')
                        return False
                    if self.box_save.error == 'state_err':
                        logging.warning('state_err')
                        return False
                    if self.get_filament_sensor_detect():
                        error(key_retrude_err3, self.gcode, self.reactor,
                              self.print_stats.state, self.box_save)
                        return False
                    self.communication_set_box_mode(addr, 'IDLE')
                    for num in num_map:
                        if (material_status & (1 << num_map[num])) != 0:
                            if (connections_status & (1 << num_map[num])) != 0:
                                ret = self.communication_retrude_process(addr, num, 'MATERIAL')
                                if ret is False:
                                    logging.warning('[retrude error] buffer empty error and failed to retrude to material sensor')
                                    self.box_save.error_tnn['addr'] = addr
                                    return False
                        if (material_status & (1 << num_map[num])) == 0:
                            if (connections_status & (1 << num_map[num])) != 0:
                                error(key_material_err % (addr, num), self.gcode, self.reactor,
                                      self.print_stats.state, self.box_save)
                                return ret
                    continue

                if self.toolhead.get_extruder().get_heater().can_extrude is not True:
                    _bind(self, 'quickly_wait_heating')(self.boxcfg.Tn_extrude_temp)
                times[0] = []
                times[1] = []
                for num in num_map:
                    if connections_status & (1 << num_map[num]):
                        if material_status & (1 << num_map[num]):
                            times[0].append(num)
                        else:
                            times[1].append(num)
                for num in times[0]:
                    for attempt in range(3):
                        ret = _bind(self, 'Tn_Extrude')(self.boxcfg.Tn_retrude,
                                              self.boxcfg.Tn_retrude_velocity,
                                              self.boxcfg.Tn_extrude_temp)
                        if ret is False:
                            self.box_state.clear_e_err()
                            return False
                        ret = self.communication_retrude_process(addr, '0', 'BUFFER')
                        if ret is False:
                            if self._is_transport_uncertain():
                                _bind(logging, 'warning')(
                                    'K2 CFS blocking retrude retry after uncertain BUFFER transport')
                                return False
                            if self.box_save.error == 'state_err':
                                logging.warning('state_err')
                                return False
                            if self.get_filament_sensor_detect():
                                error(key_retrude_err3, self.gcode, self.reactor,
                                      self.print_stats.state, self.box_save)
                                return False
                            ret = self.communication_set_box_mode(addr, 'IDLE')
                            if ret is False:
                                return False
                            break
                    self.gcode.run_script_from_command('G91')
                    self.gcode.run_script_from_command('G0 E-10 F60')
                    self.gcode.run_script_from_command('G90')
                    ret = self.communication_retrude_process(addr, num, 'MATERIAL')
                    if ret is False:
                        return False
                    self.retrude_process_clear_flag()
                    if not self.get_filament_sensor_detect():
                        return True
                    error(key_retrude_err4, self.gcode, self.reactor,
                          self.print_stats.state, self.box_save)
                    return False
            self.retrude_process_clear_flag()
            if self.get_filament_sensor_detect():
                error(key_sensor_err, self.gcode, self.reactor,
                      self.print_stats.state, self.box_save)
                return False
        else:
            last_Tnn_content = _bind(self.box_state, 'get_Tnn_content')(self.box_save.last_cmd)
            ret = _bind(self, 'communication_set_box_mode')(last_Tnn_content['addr'], 'IDLE')
            if ret is False:
                return False
            ret = self.box_retrude_material_filament_err_part()
            if ret == False:
                return True
            velocity, _ = _bind(self, 'get_flush_velocity')(self.box_save.last_cmd, self.box_save.last_cmd)
            _, _, flush_temp = _bind(self, 'get_flush_temp')(self.box_save.last_cmd, self.box_save.last_cmd)
            sensor_status = self.get_filament_sensor_detect()
            if sensor_status:
                for attempt in range(3):
                    ret = _bind(self, 'communication_retrude_process')(
                        last_Tnn_content['addr'], last_Tnn_content['num'], 'BUFFER')
                    if ret is False:
                        if self._is_transport_uncertain():
                            _bind(logging, 'warning')(
                                'K2 CFS blocking retrude fallback after uncertain BUFFER transport')
                            return False
                        if self.box_save.error == 'state_err':
                            logging.warning('state_err')
                            return False
                        if self.get_filament_sensor_detect():
                            error(key_retrude_err3, self.gcode, self.reactor,
                                  self.print_stats.state, self.box_save)
                            return False
                        ret = _bind(self, 'communication_set_box_mode')(last_Tnn_content['addr'], 'IDLE')
                        if ret is False:
                            return False
                        break
                    ret = _bind(self, 'Tn_Extrude')(self.boxcfg.Tn_retrude, velocity, flush_temp)
                    if ret is False:
                        self.box_state.clear_e_err()
                        return False
                else:
                    ret = _bind(self, 'communication_retrude_process')(
                        last_Tnn_content['addr'], last_Tnn_content['num'], 'BUFFER')
                    if ret is False:
                        if self._is_transport_uncertain():
                            _bind(logging, 'warning')(
                                'K2 CFS blocking retrude fallback after uncertain BUFFER transport')
                            return False
                        if self.box_save.error == 'state_err':
                            logging.warning('state_err')
                            return False
                        if self.get_filament_sensor_detect():
                            error(key_retrude_err3, self.gcode, self.reactor,
                                  self.print_stats.state, self.box_save)
                            return False
                        ret = _bind(self, 'communication_set_box_mode')(last_Tnn_content['addr'], 'IDLE')
                        if ret is False:
                            return False
                self.gcode.run_script_from_command('G91')
                self.gcode.run_script_from_command('G0 E-10 F60')
                self.gcode.run_script_from_command('G90')
            ret = _bind(self, 'communication_retrude_process')(
                last_Tnn_content['addr'], last_Tnn_content['num'], 'MATERIAL', report_err=False)
            if ret is False:
                if self._is_transport_uncertain():
                    _bind(logging, 'warning')(
                        'K2 CFS blocking make_material_loose retry after uncertain MATERIAL transport')
                    return False
                self.make_material_loose()
                ret = _bind(self, 'communication_set_box_mode')(last_Tnn_content['addr'], 'IDLE')
                if ret is False:
                    return False
                ret = _bind(self, 'communication_retrude_process')(
                    last_Tnn_content['addr'], last_Tnn_content['num'], 'MATERIAL')
                if ret is False:
                    return False
            self.retrude_process_clear_flag()
        if not self.get_filament_sensor_detect():
            return True
        error(key_retrude_err4, self.gcode, self.reactor,
              self.print_stats.state, self.box_save)
        return False


    def break_move_to_cut(self):
        tail = _cfs_tail_slot(self)
        if tail is not None:
            _drain_cfs_tail(self, tail)
            return True
        self.gcode.run_script_from_command('M400')
        if 'x' not in _bind(self.toolhead, 'get_status')(self.reactor.monotonic())['homed_axes']:
            self.gcode.run_script_from_command('G28 X Y')
            self.gcode.run_script_from_command('BED_MESH_CLEAR')
        if self.get_filament_sensor_detect() is False:
            return True
        position_x = self.toolhead.get_position()[0]
        message_format = 'src_pos_x = %.2f'
        self.gcode.respond_info(message_format % position_x)
        if self.cut.state() is False:
            error(key_cutter_err, self.gcode, self.reactor, self.print_stats.state, self.box_save)
            return False
        index = 0
        for index in range(3):
            command_format = 'G0 X%.2f'
            _bind(self.gcode, 'run_script_from_command')(command_format % self.boxcfg.cut_pos_x)
            _bind(self.gcode, 'run_script_from_command')(command_format % (self.boxcfg.cut_pos_x + self.boxcfg.cut_push_rod))
            self.gcode.run_script_from_command('M400')
            if self.toolhead.get_extruder().get_heater().can_extrude:
                if index == 0:
                    ret = self.check_and_extrude(-2, 360)
                    if ret is False:
                        logging.warning('extrude err')
                        return ret
            if self.cut.cut_present:
                self.gcode.respond_info('[box] cut to return OK')
                break
            count = 0
            for _ in range(3):
                _bind(self.reactor, 'pause')(self.reactor.monotonic() + 1)
                if self.cut.cut_present:
                    break
                count += 1
            if count < 3:
                self.gcode.respond_info('[box] cut to return_3 OK')
                break
            if self.cut.cut_present:
                break
            cut = self.cut
            try:
                cut.cut_return_failed += 1
            finally:
                del cut
            message_format = 'cut to return failed index: %d'
            _bind(self.gcode, 'respond_info')(message_format % self.cut.cut_return_failed)
            index += 1
        if index >= 3:
            self.gcode.respond_info('[box] cut to return failed')
            index = 0
        if self.cut.state() is False:
            error(key_cut_err, self.gcode, self.reactor, self.print_stats.state, self.box_save)
            return False
        if self.toolhead.get_extruder().get_heater().can_extrude:
            ret = self.check_and_extrude(3, 300, wait=False)
            if ret is False:
                logging.warning('extrude err')
                return ret
            ret = self.check_and_extrude(-3, 300, wait=False)
            if ret is False:
                logging.warning('extrude err')
                return False
        return True


    def cut_hall_find_test(self):
        accel = self.toolhead.get_max_velocity()[1]
        saved_cruise_ratio = self.toolhead.get_status(self.reactor.monotonic())['minimum_cruise_ratio']
        self.gcode.run_script_from_command('SET_VELOCITY_LIMIT ACCEL=500 MINIMUM_CRUISE_RATIO=0.4')
        if self.cut.switch_pin:
            command_format = 'G0 X%.2f Y%.2f F%.2f'
            _bind(self.gcode, 'run_script_from_command')(command_format % (self.boxcfg.cut_pos_x, self.boxcfg.cut_pos_y, self.boxcfg.cut_velocity))
            self.gcode.run_script_from_command('M400')
            if self.cut.cut_present:
                cut = self.cut
                try:
                    cut.cut_return_failed += 1
                finally:
                    del cut
                message_format = 'cut to return failed index: %d'
                _bind(self.gcode, 'respond_info')(message_format % self.cut.cut_return_failed)
            else:
                cut = self.cut
                try:
                    cut.cut_succeed_num += 1
                finally:
                    del cut
                message_format = 'self.cut_succeed_num: %d'
                _bind(self.gcode, 'respond_info')(message_format % self.cut.cut_succeed_num)
            command_format = 'G0 X%.2f Y%.2f F%.2f'
            _bind(self.gcode, 'run_script_from_command')(command_format % (self.boxcfg.pre_cut_pos_x, self.boxcfg.pre_cut_pos_y, self.boxcfg.cut_velocity))
            self.gcode.run_script_from_command('M400')
            if self.cut.cut_present:
                cut = self.cut
                try:
                    cut.cut_succeed_num += 1
                finally:
                    del cut
                message_format = 'self.release_succeed_num: %d'
                _bind(self.gcode, 'respond_info')(message_format % self.cut.cut_release_succeed)
            else:
                cut = self.cut
                try:
                    cut.cut_return_failed += 1
                finally:
                    del cut
                message_format = 'cself.release_failed_num: %d'
                _bind(self.gcode, 'respond_info')(message_format % self.cut.cut_release_failed)
            command_format = 'G0 X100 Y%.2f F%.2f'
            _bind(self.gcode, 'run_script_from_command')(command_format % (self.boxcfg.pre_cut_pos_y, self.boxcfg.cut_velocity))
            self.gcode.run_script_from_command('M400')
        command_format = 'SET_VELOCITY_LIMIT ACCEL=%.3f MINIMUM_CRUISE_RATIO=%.9f'
        self.gcode.run_script_from_command(command_format % (accel, saved_cruise_ratio))


    def cut_hall_find_zero(self):
        if 'x' not in _bind(self.toolhead, 'get_status')(self.reactor.monotonic())['homed_axes']:
            self.gcode.run_script_from_command('G28 X Y')
        self.gcode.run_script_from_command('G1 Y20 F6000')
        self.gcode.run_script_from_command('G1 X-3 F6000')
        self.cut.cut_hall_move = 0
        for _ in range(50):
            if self.cut.state() is False:
                cut = self.cut
                try:
                    cut.cut_hall_move += -4
                finally:
                    del cut
                message_format = 'self.cut.cut_hall_move111: %f'
                _bind(self.gcode, 'respond_info')(message_format % self.cut.cut_hall_move)
                break
            self.gcode.run_script_from_command('G91')
            self.gcode.run_script_from_command('G1 x-0.1 F600')
            self.gcode.run_script_from_command('G90')
            self.gcode.run_script_from_command('M400')
            cut = self.cut
            try:
                cut.cut_hall_move += -0.1
            finally:
                del cut
        message_format = 'self.boxcfg.cut_pos_x: %f'
        _bind(logging, 'info')(message_format % self.boxcfg.cut_pos_x)


    def cut_hall_test(self):
        self.cut_hall_find_test()


    def cut_hall_zero(self):
        self.cut_hall_find_zero()


    def cut_material(self):
        self.box_save.save_fan()
        temperature = _bind(self, 'get_material_target_temp')(tnn=self.box_save.last_cmd, type=self.remain_material.type)
        command_format = 'M104 S%.2f'
        self.gcode.run_script_from_command(command_format % temperature)
        if self.toolhead.get_extruder().get_heater().can_extrude is False:
            self.go_to_extrude_pos()
        self.quickly_wait_heating(temperature)
        self.gcode.run_script_from_command('M400')
        return self.move_to_cut()


    def move_to_cut(self):
        tail = _cfs_tail_slot(self)
        if tail is not None:
            _drain_cfs_tail(self, tail)
            return True
        velocity = _bind(self, 'get_material_max_extrusion_speed')(type=self.remain_material.type)
        if velocity is None:
            velocity = self.boxcfg.Tn_extrude_velocity[0]
        else:
            velocity = velocity * 60
        if 'x' not in _bind(self.toolhead, 'get_status')(self.reactor.monotonic())['homed_axes']:
            self.gcode.run_script_from_command('G28 X Y')
            self.gcode.run_script_from_command('BED_MESH_CLEAR')
        if self.get_filament_sensor_detect() is False:
            return True
        if self.boxcfg.has_extrude_pos != 1:
            self.z_move()
            self.gcode.run_script_from_command('G90')
            command_format = 'G0 X%.2f F%.2f'
            _bind(self.gcode, 'run_script_from_command')(command_format % (self.boxcfg.pre_cut_pos_x, self.boxcfg.cut_velocity))
            command_format = 'G0 X%.2f Y%.2f F%.2f'
            _bind(self.gcode, 'run_script_from_command')(command_format % (self.boxcfg.pre_cut_pos_x, self.boxcfg.pre_cut_pos_y, self.boxcfg.cut_velocity))
        if self.toolhead.get_extruder().get_heater().can_extrude:
            ret = self.check_and_extrude(-2, 360, wait=False)
            if ret is False:
                logging.warning('extrude err')
                self.box_state.clear_e_err()
        self.gcode.run_script_from_command('G90')
        command_format = 'G0 Y%.2f F%.2f'
        _bind(self.gcode, 'run_script_from_command')(command_format % (self.boxcfg.pre_cut_pos_y, self.boxcfg.cut_velocity))
        command_format = 'G0 X%.2f F%.2f'
        _bind(self.gcode, 'run_script_from_command')(command_format % (self.boxcfg.pre_cut_pos_x, self.boxcfg.cut_velocity))
        if self.cut.switch_pin:
            if self.cut.state() is False:
                error(key_cutter_err, self.gcode, self.reactor, self.print_stats.state, self.box_save)
                return False
        self.gcode.run_script_from_command('M400')
        accel = self.toolhead.get_max_velocity()[1]
        saved_cruise_ratio = self.toolhead.get_status(self.reactor.monotonic())['minimum_cruise_ratio']
        self.gcode.run_script_from_command('SET_VELOCITY_LIMIT ACCEL=5000 MINIMUM_CRUISE_RATIO=0')
        self.set_flushing_sign()
        if self.cut.switch_pin:
            index = 0
            for index in range(3):
                command_format = 'G0 X%.2f Y%.2f F%.2f'
                _bind(self.gcode, 'run_script_from_command')(command_format % (self.boxcfg.pre_cut_pos_x, self.boxcfg.pre_cut_pos_y, self.boxcfg.cut_velocity * 0.3))
                self.gcode.run_script_from_command('SET_VELOCITY_LIMIT ACCEL=3000 MINIMUM_CRUISE_RATIO=0')
                _bind(self.gcode, 'run_script_from_command')(command_format % (self.boxcfg.cut_pos_x, self.boxcfg.cut_pos_y, self.boxcfg.cut_velocity))
                _bind(self.gcode, 'run_script_from_command')(command_format % (self.boxcfg.cut_pos_x + self.boxcfg.cut_push_rod, self.boxcfg.pre_cut_pos_y, self.boxcfg.cut_velocity * 0.3))
                self.gcode.run_script_from_command('SET_VELOCITY_LIMIT ACCEL=5000 MINIMUM_CRUISE_RATIO=0')
                self.gcode.run_script_from_command('M400')
                if index == 0:
                    if self.toolhead.get_extruder().get_heater().can_extrude:
                        ret = self.check_and_extrude(-2, 360)
                        if ret is False:
                            logging.warning('extrude err')
                            return ret
                if self.cut.cut_present:
                    self.gcode.respond_info('[box] cut to return OK')
                    break
                count = 0
                for _ in range(3):
                    _bind(self.reactor, 'pause')(self.reactor.monotonic() + 1)
                    if self.cut.cut_present:
                        break
                    count += 1
                if count < 3:
                    self.gcode.respond_info('[box] cut to return_3 OK')
                    break
                if self.cut.cut_present:
                    break
                cut = self.cut
                try:
                    cut.cut_return_failed += 1
                finally:
                    del cut
                message_format = 'cut to return failed index: %d'
                _bind(self.gcode, 'respond_info')(message_format % self.cut.cut_return_failed)
                index += 1
            self.reset_flushing_sign()
            if index >= 3:
                self.gcode.respond_info('[box] cut to return failed')
                index = 0
            if self.cut.state() is False:
                error(key_cut_err, self.gcode, self.reactor, self.print_stats.state, self.box_save)
                return False
            if self.toolhead.get_extruder().get_heater().can_extrude:
                ret = self.check_and_extrude(7, velocity, wait=False)
                if ret is False:
                    logging.warning('extrude err')
                    return ret
                ret = self.check_and_extrude(-7, 300, wait=False)
                if ret is False:
                    logging.warning('extrude err')
                    return False
        else:
            for _ in range(3):
                command_format = 'G0 X%.2f Y%.2f F%.2f'
                _bind(self.gcode, 'run_script_from_command')(command_format % (self.boxcfg.cut_pos_x, self.boxcfg.cut_pos_y, self.boxcfg.cut_velocity))
                _bind(self.gcode, 'run_script_from_command')(command_format % (self.boxcfg.pre_cut_pos_x, self.boxcfg.pre_cut_pos_y, self.boxcfg.cut_velocity))
            self.gcode.run_script_from_command('M400')
            if self.toolhead.get_extruder().get_heater().can_extrude:
                self.gcode.run_script_from_command('G91')
                self.gcode.run_script_from_command('G0 E-2 F600')
                self.gcode.run_script_from_command('G90')
            self.gcode.run_script_from_command('M400')
        command_format = 'SET_VELOCITY_LIMIT ACCEL=%.3f MINIMUM_CRUISE_RATIO=%.9f'
        self.gcode.run_script_from_command(command_format % (accel, saved_cruise_ratio))
        return True


    def z_down(self):
        accel = self.toolhead.get_max_velocity()[1]
        self.gcode.run_script_from_command('M204 S1000')
        position_z = self.toolhead.get_position()[2]
        maximum_z = self.config.getsection('stepper_z').getfloat('position_max')
        if 'z' not in _bind(self.toolhead, 'get_status')(self.reactor.monotonic())['homed_axes']:
            return
        if self.toolhead.get_extruder().get_heater().can_extrude:
            ret = self.check_and_extrude(-1, 180, wait=False)
            if ret is False:
                logging.warning('extrude err')
                self.box_state.clear_e_err()
            ret = self.check_and_extrude(-2, 360, wait=False)
            if ret is False:
                logging.warning('extrude err')
                self.box_state.clear_e_err()
        if maximum_z - position_z > 0.5:
            self.gcode.run_script_from_command('G91')
            self.gcode.run_script_from_command('SET_GCODE_VARIABLE MACRO=PRINTER_PARAM VARIABLE=z_safe_pause VALUE=0.5')
            self.gcode.run_script_from_command('G0 Z0.5 F600')
            self.gcode.run_script_from_command('G90')
            self.box_save.move_z = 0.5
        command_format = 'M204 S%s'
        self.gcode.run_script_from_command(command_format % accel)
        self.gcode.run_script_from_command('M400')


    def z_move(self):
        position_z = self.toolhead.get_position()[2]
        if position_z is not None and position_z < 50.0:
            self.box_save.move_z = 50.0 - position_z
            self.gcode.run_script_from_command('G91')
            command_format = 'G0 Z%.2f F600'
            _bind(self.gcode, 'run_script_from_command')(command_format % self.box_save.move_z)
            self.gcode.run_script_from_command('G90')


    def z_restore(self):
        accel = self.toolhead.get_max_velocity()[1]
        self.gcode.run_script_from_command('M204 S1000')
        if self.box_save.move_z:
            self.gcode.run_script_from_command('G91')
            self.gcode.run_script_from_command('SET_GCODE_VARIABLE MACRO=PRINTER_PARAM VARIABLE=z_safe_pause VALUE=0')
            command_format = 'G0 Z-%.2f F600'
            _bind(self.gcode, 'run_script_from_command')(command_format % self.box_save.move_z)
            self.gcode.run_script_from_command('G90')
            self.box_save.move_z = 0.0
        command_format = 'M204 S%s'
        self.gcode.run_script_from_command(command_format % accel)


    def material_flush(self, length, velocity, temp, max_temp=None, percent=None, tnn=None):
        result = _bind(self, 'Tn_Extrude')(length, velocity, temp, max_temp, percent, tnn)
        if result is False:
            _bind(self.box_state, 'clear_e_err')()
            return False
        _bind(self, 'move_to_safe_pos')()
        _bind(self.box_save, 'restore_fan')()
        return True


    def is_use_ending_material_flush(self, **recode_err):
        tail = _cfs_tail_slot(self)
        if tail is None:
            return self._flush_ending_material(**recode_err)
        _drain_cfs_tail(self, tail, **recode_err)
        return True

    def _flush_ending_material(self, **recode_err):
        _bind(logging, 'info')('is use ending material')
        _bind(self.gcode, 'run_script_from_command')('SET_FILAMENT_SENSOR SENSOR=filament_sensor ENABLE=0')
        _bind(self.gcode, 'run_script_from_command')('M400')
        result = _bind(self, 'extrusion_all_materials')()
        if result is False:
            _bind(self.box_save, 'clear_err')()
            _bind(self.box_state, 'clear_e_err')()
            _bind(self.box_save, 'recode_err')(**recode_err)
            return False
        content = _bind(self.box_state, 'get_Tnn_content')(self.box_save.last_cmd)
        _bind(self, 'communication_get_filament_sensor_state')(content['addr'], 'MATERIAL')
        sensor = _bind(self.box_state, 'get_Tn_inner_data')(content['addr'], 'filament_sensor', subpart='material')
        channels = {'A': 0, 'B': 1, 'C': 2, 'D': 3}
        if sensor & (1 << channels[content['num']]) == 0:
            _bind(self.box_state, 'modify_Tn_data')(content['addr'], 'none', 'vender', content['num'])
            _bind(self.box_state, 'update_same_material_list')()
            _bind(self, 'update_Tnn_map')()
            _bind(self, 'update_filament_pos')(self.box_save.last_cmd, None)
        self.is_use_ending_material = False
        self.box_save.box_filament_err = False
        _bind(self.box_save, 'clear_err')()
        self.box_save.last_cmd_tmp = self.box_save.last_cmd
        self.box_save.last_cmd = None
        return True


    def set_temp(self):
            temp = _bind(self, 'get_material_target_temp')(
                tnn=self.box_save.last_cmd,
                type=self.remain_material.type)
            _bind(self, 'quickly_wait_heating')(temp)
            _bind(self.gcode, 'run_script_from_command')('M400')


    def extrude_process_auto_retry_process(self, tnn, type, retry):
            tnn_content = _bind(self.box_state, 'get_Tnn_content')(tnn)
            retry_index = {
                'auto_retry_connection': 0,
                'auto_retry_filament_sensor': 1,
                'auto_retry_filament_sensor_second': 2,
                'auto_retry_extruder_gear': 3,
            }
            if self._is_transport_uncertain():
                _bind(logging, 'warning')(
                    'K2 CFS refusing %s after uncertain transport', type)
                return False
            if type == 'auto_retry_connection':
                ret = _bind(self, 'communication_set_box_mode')(tnn_content['addr'], 'IDLE')
                if ret is False:
                    return False
                ret = _bind(self, 'communication_retrude_process')(
                    tnn_content['addr'], tnn_content['num'], 'MATERIAL')
                if ret is False:
                    _bind(logging, 'warning')('auto_retry_connection retrude_process failed')
                    return False
                ret = _bind(self, 'communication_extrude_process')(
                    tnn_content['addr'], tnn_content['num'], stage=0)
                if ret is False:
                    _bind(logging, 'warning')('auto_retry_connection extrude_process stage=0 failed')
                    return False
                return ret
            if type == 'auto_retry_filament_sensor':
                _bind(self, 'make_material_loose')()
                retry[retry_index['auto_retry_filament_sensor']] += 1
                return _bind(self, 'box_extrude_material_part')(tnn, retry)
            if type == 'auto_retry_filament_sensor_second':
                retry[retry_index['auto_retry_filament_sensor_second']] += 1
                retry[retry_index['auto_retry_filament_sensor']] = 0
                ret = _bind(self, 'communication_set_box_mode')(tnn_content['addr'], 'IDLE')
                if ret is False:
                    return False
                ret = _bind(self, 'communication_retrude_process')(
                    tnn_content['addr'], tnn_content['num'], 'MATERIAL')
                if ret is False:
                    _bind(logging, 'warning')('auto_retry_filament_sensor_second retrude_process failed')
                    return False
                ret = _bind(self, 'communication_extrude_process')(
                    tnn_content['addr'], tnn_content['num'], stage=0)
                if ret is False:
                    _bind(logging, 'warning')('auto_retry_connection extrude_process stage=0 failed')
                    return False
                return _bind(self, 'box_extrude_material_part')(tnn, retry)
            if type == 'auto_retry_extruder_gear':
                retry[retry_index['auto_retry_extruder_gear']] += 1
                retry[retry_index['auto_retry_filament_sensor']] = 0
                retry[retry_index['auto_retry_filament_sensor_second']] = 0
                ret = _bind(self, 'move_to_cut')()
                if ret is False:
                    _bind(logging, 'warning')('auto_retry_extruder_gear move_to_cut failed')
                    return False
                _bind(self, 'go_to_extrude_pos')()
                ret = _bind(self, 'box_retrude_material')()
                if ret is False or self._is_transport_uncertain():
                    _bind(logging, 'warning')('auto_retry_extruder_gear retrude_material failed')
                    return False
                ret = _bind(self, 'communication_extrude_process')(
                    tnn_content['addr'], tnn_content['num'], stage=0)
                if ret is False:
                    _bind(logging, 'warning')('auto_retry_connection extrude_process stage=0 failed')
                    return False
                return _bind(self, 'box_extrude_material_part')(tnn, retry)
            return None


    def get_box_mode_and_wait(self):
            for num in range(4):
                addr = num + 1
                if _bind(self, 'check_connect')(addr):
                    _bind(self, 'communication_get_box_state')(addr)
                    wait_times = 0
                    while True:
                        wait_times += 1
                        box_mode = _bind(self.box_state, 'get_Tn_data')(addr, 'mode')
                        if box_mode != mode['PRELOADING']:
                            break
                        if wait_times > 60:
                            _bind(logging, 'info')('wait_times > 60')
                            break
                        if self.is_shutdown:
                            break
                        _bind(logging, 'info')('box mode = %s' % box_mode)
                        _bind(self.reactor, 'pause')(_bind(self.reactor, 'monotonic')() + 1)


    def power_loss_restore(self):
            enable = None
            data = {}
            if not os.path.exists(self.box_state.tn_save_data_path):
                _bind(logging, 'warning')('file not exist')
                return
            try:
                with open(self.box_state.tn_save_data_path, 'r') as file:
                    data = json.load(file)
            except Exception as err:
                _bind(logging, 'error')(err)
            if data is None:
                _bind(logging, 'warning')('data is None')
                return
            enable = data.get('enable', None)
            tnn_map = data.get('tnn_map', {})
            last_cmd = data.get('last_cmd', None)
            _bind(logging, 'info')('enable = %s, map = %s, last_cmd = %s' % (enable, tnn_map, last_cmd))
            if enable is not None and (enable == 1 or enable == 0):
                _bind(self.gcode, 'run_script_from_command')('BOX_ENABLE_CFS_PRINT ENABLE=%s' % enable)
            if len(tnn_map) > 0:
                for tnn in self.box_state.Tnn_map:
                    self.box_state.Tnn_map[tnn] = tnn_map[tnn]
                _bind(logging, 'info')('Tnn_map: %s' % self.box_state.Tnn_map)
            if last_cmd is not None:
                self.box_save.last_cmd = last_cmd


    def power_loss_clean(self):
            if not os.path.exists(self.box_state.tn_save_data_path):
                _bind(logging, 'warning')('file not exist')
                return
            try:
                with open(self.box_state.tn_save_data_path, 'r') as file:
                    data = json.load(file)
            except Exception as err:
                _bind(logging, 'error')(err)
            if data is None:
                _bind(logging, 'warning')('data is None')
                return
            enable = data.get('enable', None)
            tnn_map = data.get('tnn_map', {})
            last_cmd = data.get('last_cmd', None)
            if enable is not None:
                del data['enable']
            if len(tnn_map) > 0:
                del data['tnn_map']
            if last_cmd is not None:
                del data['last_cmd']
            try:
                with open(self.box_state.tn_save_data_path, 'w') as file:
                    json.dump(data, file)
            except Exception as err:
                _bind(logging, 'error')(err)
            _bind(logging, 'info')('clean the data of power loss')


    def filament_conflict_check(self, print_type):
            extruder_filament = _bind(self, 'get_filament_sensor_detect')()
            five_way_filament = _bind(self, 'get_five_way_sensor_detect')()
            is_filament_conflict = False
            if print_type == 'cfs' and five_way_filament is False:
                if extruder_filament:
                    is_filament_conflict = True
            if print_type == 'rack' and five_way_filament is True:
                is_filament_conflict = True
            return (is_filament_conflict, extruder_filament, five_way_filament)


    def filament_err_tighten_up_event(self, eventtime):
            _bind(logging, 'info')('filament_err_tighten_up_event')
            if self.box_save.last_cmd is None:
                _bind(logging, 'warning')('last_cmd is None')
                return
            tnn_content = _bind(self.box_state, 'get_Tnn_content')(self.box_save.last_cmd)
            num_map = {'A': 1, 'B': 2, 'C': 4, 'D': 8}
            slot = self.box_save.last_cmd
            def active():
                return (not self.printer.is_shutdown() and self.box_save.box_filament_err
                        and self.box_save.last_cmd == slot)
            while active():
                _bind(self, 'communication_get_filament_sensor_state')(
                    tnn_content['addr'], 'CONNECTIONS')
                connections_status = _bind(self.box_state, 'get_Tn_inner_data')(
                    tnn_content['addr'], 'filament_sensor', subpart='connections')
                if connections_status & num_map[tnn_content['num']] == 0:
                    extrude = _bind(self.toolhead, 'get_position')()[3]
                    _bind(logging, 'info')('extrude = %s' % extrude)
                    while _bind(self.toolhead, 'get_position')()[3] - extrude < 120:
                        _bind(logging, 'info')('E pos: %s' % _bind(self.toolhead, 'get_position')()[3])
                        if not active():
                            return
                        _bind(self.reactor, 'pause')(_bind(self.reactor, 'monotonic')() + 3.0)
                    if not active():
                        return
                    _bind(self, 'communication_set_box_mode')(tnn_content['addr'], 'IDLE')
                    if not active():
                        return
                    _bind(self, 'communication_tighten_up_enable')(tnn_content['addr'], 'ENABLE')
                    while active():
                        if self.print_stats.state != 'printing' and self.print_stats.state != 'paused':
                            _bind(logging, 'info')('state: %s' % self.print_stats.state)
                            return
                        if self.heart_process_enable[tnn_content['addr'] - 1]:
                            material_status = _bind(self, 'communication_get_filament_sensor_state')(
                                tnn_content['addr'], 'MATERIAL')
                        material_status = _bind(self.box_state, 'get_Tn_inner_data')(
                            tnn_content['addr'], 'filament_sensor', subpart='material')
                        if not active():
                            return
                        if material_status & num_map[tnn_content['num']]:
                            if _bind(self.box_state, 'get_Tn_data')(
                                    tnn_content['addr'], 'vender', num=tnn_content['num']) != 'none':
                                _bind(self, 'communication_tighten_up_enable')(tnn_content['addr'], 'DISABLE')
                                self.box_state.Tn_data['filament'] = 1
                                return
                        _bind(self.reactor, 'pause')(_bind(self.reactor, 'monotonic')() + 5)
                _bind(self.reactor, 'pause')(_bind(self.reactor, 'monotonic')() + 5)


    def break_box_extruder_retrude(self, length):
            last_Tnn_content = _bind(self.box_state, 'get_Tnn_content')(self.box_save.last_cmd)
            _bind(self, 'communication_set_box_mode')(last_Tnn_content['addr'], 'IDLE')
            ret = _bind(self, 'communication_retrude_process')(
                last_Tnn_content['addr'], last_Tnn_content['num'], 'BUFFER')
            if ret is False:
                _bind(self, 'communication_set_box_mode')(last_Tnn_content['addr'], 'IDLE')
                return
            ret = _bind(self, 'check_and_extrude')(length, 300, wait=False)
            if ret is False:
                _bind(logging, 'warning')('extrude err')
                return False
            return None









from gcode import CommandError


class BoxError(CommandError):
    def __init__(self, message, command=None, **kwargs):
        self.message = message
        super().__init__(message)



class error(Exception):
    def __init__(self, msg, gcode, reactor, stats, box_save):
        if box_save.last_err == msg:
            warning = logging.warning
            try:
                warning("the same err, last_err: %s", box_save.last_err)
            finally:
                del warning
            return
        logging.info(msg)
        self.gcode = gcode
        self.reactor = reactor
        self.gcode.respond_raw(msg)
        box_save.last_err = msg
        if ("key843" in msg or "key853" in msg or "key858" in msg
                or "key859" in msg or "key860" in msg or "key861" in msg
                or "key862" in msg):
            return
        if box_save.move_z:
            self.gcode.run_script_from_command("G91")
            run_script = self.gcode.run_script_from_command
            try:
                run_script("G0 Z-%.2f F600" % box_save.move_z)
            finally:
                del run_script
            self.gcode.run_script_from_command("G90")
            self.gcode.run_script_from_command("BOX_MOVE_TO_SAFE_POS")
            self.gcode.run_script_from_command("M400")
            box_save.move_z = 0.0
        if box_save.max_velocity:
            respond_info = self.gcode.respond_info
            try:
                respond_info("max_velocity = %s" % box_save.max_velocity)
            finally:
                del respond_info
            run_script = self.gcode.run_script_from_command
            try:
                run_script("G0 F%.2f" % box_save.max_velocity)
            finally:
                del run_script
            box_save.max_velocity = None
        if box_save.max_accel:
            respond_info = self.gcode.respond_info
            try:
                respond_info("max_accel = %s" % box_save.max_accel)
            finally:
                del respond_info
            run_script = self.gcode.run_script_from_command
            try:
                run_script("M204 S%d" % box_save.max_accel)
            finally:
                del run_script
            box_save.max_accel = None
        if box_save.speed_factor:
            respond_info = self.gcode.respond_info
            try:
                respond_info("speed_factor = %s" % box_save.speed_factor)
            finally:
                del respond_info
            run_script = self.gcode.run_script_from_command
            try:
                run_script("M220 S%s" % box_save.speed_factor)
            finally:
                del run_script
            box_save.speed_factor = None
        if stats == "paused":
            self.gcode.run_script_from_command("M104 S140")
            self.gcode.run_script_from_command("BOX_MOVE_TO_SAFE_POS")
        if stats == "printing":
            if self.gcode.printer.lookup_object("k2_print").pause_start == False:
                self.gcode.run_script_from_command("PAUSE")
            self.gcode.run_script_from_command("M400")


import copy


class MultiColorMeterialBoxWrapper:
    error = BoxError

    def get_status(self, eventime):
        res = copy.deepcopy(self.box_state.Tn_data)
        return res


    def cmd_create_connect(self, gcmd):
        if self.box_action.print_stats.state == 'printing':
            return
        addr = _bind(gcmd, 'get_int')('ADDR', minval=1, maxval=4)
        _bind(self.box_action, 'communication_create_connect')(addr)


    def cmd_get_rfid(self, gcmd):
        addr = _bind(gcmd, 'get_int')('ADDR', minval=1, maxval=4)
        num = _bind(gcmd, 'get_int')('NUM', minval=0, maxval=15)
        _bind(self.box_action, 'communication_get_rfid')(addr, num)


    def cmd_get_remain_len(self, gcmd):
        addr = _bind(gcmd, 'get_int')('ADDR', minval=1, maxval=4)
        num = _bind(gcmd, 'get_int')('NUM', minval=0, maxval=15)
        _bind(self.box_action, 'communication_get_remain_len')(addr, num)


    def cmd_set_box_mode(self, gcmd):
        addr = _bind(gcmd, 'get_int')('ADDR', minval=1, maxval=4)
        mode = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('MODE')
        num = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('NUM')
        if num is None:
            num = '0'
        _bind(self.box_action, 'communication_set_box_mode')(addr, mode, num)


    def cmd_set_pre_loading(self, gcmd):
        addr = _bind(gcmd, 'get_int')('ADDR', default=0, minval=1, maxval=4)
        action = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('ACTION')
        num = _bind(gcmd, 'get_int')('NUM', default=0, minval=0, maxval=15)
        power_on = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('POWER_ON')
        if power_on == 'ENABLE':
            self.box_action.box_save.preloading_poweron = True
        elif power_on == 'DISABLE':
            self.box_action.box_save.preloading_poweron = False
        else:
            _bind(logging, 'warning')("[warning] the param of 'POWER_ON' is error, only 'ENABLE' and 'DISABLE'")
        if action == 'CLOSE':
            self.box_action.box_save.preloading_en = False
            action_num = 0
        elif action == 'OPEN':
            self.box_action.box_save.preloading_en = True
            action_num = 1
        elif action == 'RUN':
            action_num = 2
        elif action == 'TIGHT':
            action_num = 3
        else:
            _bind(logging, 'warning')("[warning] param error, ACTION only with 'OPEN' and 'CLOSE', 'RUN', 'TIGHT'")
            return
        if action == 'CLOSE' or action == 'OPEN':
            for box_addr in range(1, 5):
                if _bind(self.box_action, 'check_connect')(box_addr):
                    _bind(self.box_action, 'communication_set_pre_loading')(box_addr, action_num, num)
        else:
            _bind(self.box_action, 'communication_set_pre_loading')(addr, action_num, num)


    def cmd_tighten_up_enable(self, gcmd):
        addr = _bind(gcmd, 'get_int')('ADDR', minval=1, maxval=4)
        enable = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('ENABLE')
        if enable == 'DISABLE' or enable == 'ENABLE':
            _bind(self.box_action, 'communication_tighten_up_enable')(addr, enable)
        else:
            _bind(logging, 'warning')("param is error, ENABLE only with 'DISABLE' and 'ENABLE'")
            return


    def cmd_get_version_sn(self, gcmd):
        addr = _bind(gcmd, 'get_int')('ADDR', minval=1, maxval=4)
        _bind(self.box_action, 'communication_get_version_sn')(addr)


    def cmd_get_box_mode(self, gcmd):
        addr = _bind(gcmd, 'get_int')('ADDR', minval=1, maxval=4)
        _bind(self.box_action, 'communication_get_box_mode')(addr)


    def __init__(self, config):
        self.config = config
        self.printer = _bind(config, 'get_printer')()
        self.reactor = _bind(self.printer, 'get_reactor')()
        self.gcode = _bind(self.printer, 'lookup_object')('gcode')
        self.webhooks = _bind(self.printer, 'lookup_object')('webhooks')
        self.load_ai = None
        self.parse = ParseData()
        self.box_state = BoxState()
        self.box_action = BoxAction(config, self.box_state)
        _bind(self.webhooks, 'register_endpoint')('filament_conflict_check', self.filament_conflict_check)
        _bind(self.webhooks, 'register_endpoint')('filament_state_get', self.box_filament_state_get)
        _bind(self.gcode, 'register_command')('BOX_CUT_STATE', self.cmd_cut_state)
        _bind(self.gcode, 'register_command')('BOX_SEND_DATA', self.cmd_send_data)
        _bind(self.gcode, 'register_command')('BOX_COMMUNICATION_TEST', self.cmd_communication_test)
        _bind(self.gcode, 'register_command')('BOX_MODIFY_TN', self.cmd_modify_Tnn_map)
        _bind(self.gcode, 'register_command')('BOX_MODIFY_TN_DATA', self.cmd_modify_Tn_data)
        _bind(self.gcode, 'register_command')('BOX_MODIFY_TN_INNER_DATA', self.cmd_modify_Tn_inner_data)
        _bind(self.printer, 'register_event_handler')('klippy:ready', self.find_objs)
        for Tnn in self.box_state.Tnn_map:
            setattr(self, 'cmd_' + Tnn, _bind(self, 'generate_Tnn_func')(Tnn))
        for Tn in self.box_state.Tn_map:
            setattr(self, 'cmd_' + Tn, _bind(self, 'generate_Tn_func')(Tn))
            if Tn not in ('T1', 'T2', 'T3', 'T4'):
                self.gcode.register_command(Tn, getattr(self, 'cmd_' + Tn))
        self.gcode.register_command('BOX_SELECT', self.cmd_BOX_SELECT)
        _bind(self.gcode, 'register_command')('BOX_CREATE_CONNECT', self.cmd_create_connect)
        _bind(self.gcode, 'register_command')('BOX_EXTRUDE_PROCESS', self.cmd_extrude_process)
        _bind(self.gcode, 'register_command')('BOX_RETRUDE_PROCESS', self.cmd_retrude_process)
        _bind(self.gcode, 'register_command')('BOX_CTRL_CONNECTION_MOTOR_ACTION', self.cmd_ctrl_connection_motor_action)
        _bind(self.gcode, 'register_command')('BOX_GET_FILAMENT_SENSOR_STATE', self.cmd_get_filament_sensor_state)
        _bind(self.gcode, 'register_command')('BOX_SET_BOX_MODE', self.cmd_set_box_mode)
        _bind(self.gcode, 'register_command')('BOX_GET_BUFFER_STATE', self.cmd_get_buffer_state)
        _bind(self.gcode, 'register_command')('BOX_GET_RFID', self.cmd_get_rfid)
        _bind(self.gcode, 'register_command')('BOX_GET_REMAIN_LEN', self.cmd_get_remain_len)
        _bind(self.gcode, 'register_command')('BOX_GET_BOX_STATE', self.cmd_get_box_state)
        _bind(self.gcode, 'register_command')('BOX_GET_BOX_MODE', self.cmd_get_box_mode)
        _bind(self.gcode, 'register_command')('BOX_SET_PRE_LOADING', self.cmd_set_pre_loading)
        _bind(self.gcode, 'register_command')('BOX_MEASURING_WHEEL', self.cmd_measuring_wheel)
        _bind(self.gcode, 'register_command')('BOX_TIGHTEN_UP_ENABLE', self.cmd_tighten_up_enable)
        _bind(self.gcode, 'register_command')('BOX_NOZZLE_CLEAN', self.cmd_nozzle_clean)
        _bind(self.gcode, 'register_command')('BOX_MOVE_TO_SAFE_POS', self.cmd_move_to_safe_pos)
        _bind(self.gcode, 'register_command')('BOX_TN_EXTRUDE', self.cmd_Tn_Extrude)
        _bind(self.gcode, 'register_command')('BOX_BLOW', self.cmd_blow)
        _bind(self.gcode, 'register_command')('BOX_GET_FLUSH_LEN', self.cmd_get_flush_len)
        _bind(self.gcode, 'register_command')('BOX_GO_TO_EXTRUDE_POS', self.cmd_go_to_extrude_pos)
        _bind(self.gcode, 'register_command')('BOX_MOVE_TO_CUT', self.cmd_move_to_cut)
        _bind(self.gcode, 'register_command')('BOX_SAVE_FAN', self.cmd_save_fan)
        _bind(self.gcode, 'register_command')('BOX_RESTORE_FAN', self.cmd_restore_fan)
        _bind(self.gcode, 'register_command')('BOX_END', self.cmd_box_end)
        _bind(self.gcode, 'register_command')('BOX_ERROR_RESUME_PROCESS', self.cmd_error_resume_process)
        _bind(self.gcode, 'register_command')('BOX_CUT_MATERIAL', self.cmd_cut_material)
        _bind(self.gcode, 'register_command')('BOX_SET_TEMP', self.cmd_set_temp)
        _bind(self.gcode, 'register_command')('BOX_MODE_WAIT', self.cmd_box_mode_wait)
        _bind(self.gcode, 'register_command')('BOX_RETRUDE_MATERIAL', self.cmd_box_retrude_material)
        _bind(self.gcode, 'register_command')('BOX_EXTRUDE_MATERIAL', self.cmd_box_extrude_material)
        _bind(self.gcode, 'register_command')('BOX_EXTRUDER_EXTRUDE', self.cmd_extruder_extrude)
        _bind(self.gcode, 'register_command')('BOX_MATERIAL_FLUSH', self.cmd_material_flush)
        _bind(self.gcode, 'register_command')('BOX_MATERIAL_CHANGE_FLUSH', self.cmd_material_change_flush)
        _bind(self.gcode, 'register_command')('BOX_RETRUDE_MATERIAL_WITH_TNN', self.cmd_retrude_material_with_tnn)
        _bind(self.gcode, 'register_command')('BOX_EXTRUDE_2_PROCESS', self.cmd_extrude2_process)
        _bind(self.gcode, 'register_command')('BOX_GET_VERSION_SN', self.cmd_get_version_sn)
        _bind(self.gcode, 'register_command')('BOX_GET_HARDWARE_STATUS', self.cmd_get_hardware_status)
        _bind(self.gcode, 'register_command')('BOX_GENERATE_FLUSH_ARRAY', self.cmd_generate_flush_array)
        _bind(self.gcode, 'register_command')('BOX_SHOW_TNN_INNER_DATA', self.cmd_show_Tnn_data)
        _bind(self.gcode, 'register_command')('BOX_ENABLE_AUTO_REFILL', self.cmd_set_enable_auto_refill)
        _bind(self.gcode, 'register_command')('BOX_CHECK_MATERIAL_REFILL', self.cmd_check_material_refill)
        _bind(self.gcode, 'register_command')('BOX_TNN_RETRY_PROCESS', self.cmd_Tnn_retry_process)
        _bind(self.gcode, 'register_command')('BOX_ERROR_CLEAR', self.cmd_error_clear)
        _bind(self.gcode, 'register_command')('BOX_START_PRINT', self.cmd_box_start_print)
        _bind(self.gcode, 'register_command')('BOX_END_PRINT', self.cmd_box_end_print)
        _bind(self.gcode, 'register_command')('BOX_ENABLE_CFS_PRINT', self.cmd_box_enable_CFS_print)
        _bind(self.gcode, 'register_command')('BOX_POWER_LOSS_RESTORE', self.cmd_power_loss_restore)
        _bind(self.gcode, 'register_command')('BOX_UPDATE_SAME_MATERIAL_LIST', self.cmd_update_same_material_list)
        _bind(self.gcode, 'register_command')('BOX_SET_CURRENT_BOX_IDLE_MODE', self.cmd_BOX_SET_CURRENT_BOX_IDLE_MODE)
        _bind(self.gcode, 'register_command')('BOX_EXTRUSION_ALL_MATERIALS', self.cmd_extrusion_all_materials)
        _bind(self.gcode, 'register_command')('BOX_DISABLE_HEART_PROCESS', self.cmd_disable_heart_process)
        _bind(self.gcode, 'register_command')('BOX_ENABLE_HEART_PROCESS', self.cmd_enable_heart_process)
        _bind(self.gcode, 'register_command')('BOX_GET_GCODE_USED_TNN', self.cmd_get_gcode_used_tnn)
        _bind(self.gcode, 'register_command')('BOX_FIRST_POWER_ON_PRELOAD', self.cmd_first_power_on_preload)
        self.test = None
        _bind(self.gcode, 'register_command')('BOX_TEST_MAKE_ERROR', self.cmd_make_error)
        _bind(self.gcode, 'register_command')('BOX_SHOW_ERROR', self.cmd_show_error)
        _bind(self.gcode, 'register_command')('BOX_SHOW_FLUSH_LIST', self.cmd_show_flush_list)
        if self.box_action.cut.switch_pin:
            _bind(self.gcode, 'register_command')('BOX_CUT_HALL_ZERO', self.cmd_hall_zero)
            _bind(self.gcode, 'register_command')('BOX_CUT_HALL_TEST', self.cmd_hall_test)
        _bind(self.gcode, 'register_command')('BOX_GET_FLUSH_VELOCITY_TEST', self.cmd_get_flush_velocity_test)
        _bind(self.printer, 'register_event_handler')('box:handle_motor_extruder_error', self.box_state.e_err_set)
        _bind(self.printer, 'register_event_handler')('box:clear_box_color_count', self.box_state.clear_color_count)
        _bind(self.gcode, 'register_command')('CR_BOX_PRE_OPT', self.cmd_CR_BOX_PRE_OPT)
        _bind(self.gcode, 'register_command')('CR_BOX_CUT', self.cmd_CR_BOX_CUT)
        _bind(self.gcode, 'register_command')('CR_BOX_RETRUDE', self.cmd_CR_BOX_RETRUDE)
        _bind(self.gcode, 'register_command')('CR_BOX_EXTRUDE', self.cmd_CR_BOX_EXTRUDE)
        _bind(self.gcode, 'register_command')('CR_BOX_WASTE', self.cmd_CR_BOX_WASTE)
        _bind(self.gcode, 'register_command')('CR_BOX_FLUSH', self.cmd_CR_BOX_FLUSH)
        _bind(self.gcode, 'register_command')('CR_BOX_END_OPT', self.cmd_CR_BOX_END_OPT)
        _bind(self.gcode, 'register_command')('CR_BOX_PRE_OPT_EXTRA', self.cmd_CR_BOX_PRE_OPT_EXTRA)
        _bind(self.gcode, 'register_command')('CR_BOX_END_OPT_EXTRA', self.cmd_CR_BOX_END_OPT_EXTRA)


    def cmd_BOX_SELECT(self, gcmd):
        params = gcmd.get_command_parameters()
        if set(params) == {'SLOT'} and params['SLOT'] in self.box_state.Tnn_map:
            return self.generate_Tnn_func(params['SLOT'])(gcmd)
        if set(params) == {'TOOL'} and params['TOOL'] in {str(n) for n in range(16)}:
            return self.generate_Tn_func('T' + params['TOOL'])(gcmd)
        raise gcmd.error(self.printer.lookup_object('k2').messages['cfs_slot'])

    def generate_Tnn_func(self, tnn):
    
        def function(gcmd):
            if self.box_action.box_state.Tn_data['enable'] == 0:
                return
            if self.box_state.Tn_data['state'] != 'connect':
                return
            if self.box_state.Tn_data[self.box_state.Tnn_map[tnn][:2]]['state'] != 'connect':
                _bind(logging, 'warning')('addr[%s] is not connected' % self.box_state.Tnn_map[tnn][:2])
                return
            _bind(self, 'Tn_action')(tnn)
        return function


    def generate_Tn_func(self, tn):
    
        def function(gcmd):
            if self.box_action.box_state.Tn_data['enable'] == 0:
                return
            if self.box_state.Tn_data['state'] != 'connect':
                return
            vtnn = self.box_state.Tn_map[tn]
            tnn = self.box_state.Tnn_map[vtnn]
            if self.box_state.Tn_data[tnn[:2]]['state'] != 'connect':
                _bind(logging, 'warning')('addr[%s] is not connected' % tnn[:2])
                return
            _bind(self, 'Tn_action')(vtnn)
        return function


    def cmd_Tnn_retry_process(self, gcmd):
        _bind(self, 'Tnn_retry_process')()


    def heart_process(self, eventtime):
        addr = 0
        timeout_times = [5, 5, 5, 5]
        loading_format = 'get preloading_poweron, %s'
        timeout_format = 'communication_get_box_state return false, timeout_times: %d'
        for _ in range(10):
            if self.box_action.box_save.preloading_poweron is not None:
                _bind(logging, 'info')(loading_format % self.box_action.box_save.preloading_poweron)
                break
            _bind(self.reactor, 'pause')(_bind(self.reactor, 'monotonic')() + 0.5)
        while True:
            if _bind(self.printer, 'is_shutdown')():
                return
            _bind(self, 'get_connect_state')()
            addr = (addr & 3) + 1
            if _bind(self.box_action, 'check_connect')(addr) and self.box_action.heart_process_enable[addr - 1]:
                response = _bind(self.box_action, 'communication_get_box_state')(addr)
                if response is None:
                    timeout_times[addr - 1] -= 1
                    _bind(logging, 'info')(timeout_format % timeout_times[addr - 1])
                else:
                    timeout_times[addr - 1] = 5
                if timeout_times[addr - 1] <= 0:
                    _bind(self.box_action, 'timeout_process')(addr)
            _bind(self, 'box_connect_state_check')()
            _bind(self, 'test_error')()
            _bind(self.reactor, 'pause')(_bind(self.reactor, 'monotonic')() + 1.0)


    def Tn_action(self, vtnn):
        previous = self.box_action.Tn_action_flag
        self.box_action.Tn_action_flag = True
        try:
            result = self._Tn_action(vtnn)
        finally:
            self.box_action.Tn_action_flag = previous
        if result is True and not previous and self.box_action.box_save.material_auto_refill_flag:
            self.box_action.box_save.material_auto_refill_flag = False
            self.gcode.run_script_from_command('RESUME')
        return result

    def _Tn_action(self, vtnn):
        _restore_last_slot(self.box_action)
        last_tnn = self.box_action.box_save.last_cmd
        tnn = self.box_state.Tnn_map[vtnn]
        _bind(self.box_action, 'get_box_mode_and_wait')()
        if last_tnn:
            last_content = _bind(self.box_state, 'get_Tnn_content')(last_tnn)
            content = _bind(self.box_state, 'get_Tnn_content')(tnn)
            if last_content == content:
                if self.box_action.box_save.error is None or self.box_action.box_save.error == 'filament_err':
                    if self.box_state.is_continue_print:
                        message_format = 'is_continue_print, last_tnn[%s] is the same with tnn[%s]'
                        _bind(logging, 'info')(message_format % (last_tnn, tnn))
                        if _bind(self.box_action, 'get_filament_sensor_detect')():
                            return _bind(self, 'power_loss_same_material_process')(last_tnn, tnn, vtnn)
                    elif (_bind(self.box_action, 'get_filament_sensor_detect')() is True
                          and _cfs_tail_slot(self.box_action) is None):
                        target_temp = _bind(self.box_action, 'get_material_target_temp')(tnn)
                        _bind(self.box_action, 'quickly_wait_heating')(target_temp)
                        message_format = 'last_tnn[%s] is the same with tnn[%s]'
                        _bind(logging, 'info')(message_format % (last_tnn, tnn))
                        return _same_slot_print_mode(self.box_action, content)
        if self.box_state.is_continue_print:
            self.box_state.is_continue_print = False
        _bind(self.box_action.box_save, 'save_printer_accel')(
            self.box_action.gcode_move.speed * 60,
            _bind(self.box_action.toolhead, 'get_max_velocity')()[1],
            self.box_action.gcode_move.speed_factor)
        _bind(self.box_action, 'z_down')()
        _bind(self.box_action.box_save, 'save_fan')()
        tail = _cfs_tail_slot(self.box_action)
        if tail is not None:
            last_tnn = tail
            _drain_cfs_tail(self.box_action, tail, vtnn=vtnn)
        elif self.box_action.is_use_ending_material:
            action_result = _bind(self.box_action, 'is_use_ending_material_flush')(
                err='z_err_extrusion_all_materials_err', vtnn=vtnn)
            if action_result is False:
                return action_result
        if self.box_action.box_save.last_cmd is not None or _bind(self.box_action, 'get_filament_sensor_detect')():
            action_result = _bind(self.box_action, 'cut_material')()
            if action_result is False:
                if self.box_state.extrude_err:
                    _bind(self.box_state, 'clear_e_err')()
                    _bind(self.box_action.box_save, 'recode_err')('e_err_cut_err', last_tnn, tnn)
                else:
                    _bind(self.box_action.box_save, 'recode_err')('cut_err', tnn=tnn, vtnn=vtnn)
                return action_result
            _bind(self.box_action, 'go_to_extrude_pos')()
            if self.box_action.get_filament_sensor_detect():
                _bind(self.box_action, 'check_and_extrude')(8, 300)
        else:
            _bind(self.box_action, 'go_to_extrude_pos')()
        action_result = _bind(self.box_action, 'box_retrude_material')()
        if action_result is False:
            _bind(self.box_action.box_save, 'recode_err')('retrude_err', last_tnn, tnn)
            return action_result
        action_result = _bind(self.box_action, 'box_extrude_material')(tnn)
        if action_result is False:
            _bind(self.box_action.box_save, 'recode_err')('box_extrude_err', last_tnn, tnn)
            return action_result
        action_result = _bind(self.box_action, 'extruder_extrude')(tnn)
        if action_result is False:
            _bind(self.box_action.box_save, 'recode_err')('extruder_extrude_err', last_tnn, tnn)
            return action_result
        if self.load_ai:
            _bind(self.load_ai, 'execute_toolhead_ai_waste_management')()
        action_result = _bind(self, 'material_change_flush')(last_tnn, tnn)
        if action_result is False:
            _bind(self.box_action.box_save, 'recode_err')('flush_err', last_tnn, tnn)
            return action_result
        _bind(self.box_action, 'z_restore')()
        _bind(self.gcode, 'run_script_from_command')('G92 E0')
        _bind(self.gcode, 'run_script_from_command')('G90')
        _bind(self.box_action.box_save, 'restore_printer_accel')()
        return True


    def Tnn_retry_process(self):
        message_format = 'error: %s, tnn: %s, last_tnn: %s, addr: %s'
        _bind(logging, 'info')(message_format % (
            self.box_action.box_save.error,
            self.box_action.box_save.error_tnn['tnn'],
            self.box_action.box_save.error_tnn['last_tnn'],
            self.box_action.box_save.error_tnn['addr']))
        self.box_action.box_save.last_err = None
        _bind(self.box_action, 'go_to_extrude_pos')()
        if self.box_action.box_save.material_auto_refill_flag:
            self.box_action.box_save.material_auto_refill_flag = False
        if (self.box_action.box_save.error == 'cut_err'
                or self.box_action.box_save.error == 'z_err_extrusion_all_materials_err'
                or self.box_action.box_save.error == 'e_err_tn_extrude_err'):
            vtnn = self.box_action.box_save.error_tnn['vtnn']
            _bind(self.box_action.box_save, 'clear_err')()
            _bind(self, 'Tn_action')(vtnn)
        elif (self.box_action.box_save.error == 'retrude_err'
                or self.box_action.box_save.error == 'e_err_cut_err'):
            _bind(self, 'retrude_err_retry_process')()
        elif self.box_action.box_save.error == 'box_extrude_err':
            _bind(self, 'box_extrude_err_retry_process')()
        elif self.box_action.box_save.error == 'extruder_extrude_err':
            _bind(self, 'extruder_extrude_err_retry_process')()
        elif self.box_action.box_save.error == 'flush_err':
            _bind(self, 'flush_err_retry_process')()
        elif self.box_action.box_save.error == 'filament_err':
            _bind(self, 'filament_err_retry_process')()
        elif self.box_action.box_save.error == 'empty_print':
            self.box_state.resume_flag = True
            _bind(logging, 'info')('set resume_flag')
            _bind(self, 'empty_print_retry_process')()
        elif self.box_action.box_save.error == 'macro_extrusion_all_materials_err':
            _bind(self, 'macro_extrusion_all_materials_err_retry_process')()
        elif self.box_action.box_save.error == 'print_end_move_to_cut_err':
            _bind(self, 'print_end_move_to_cut_err_retry_process')()
        elif self.box_action.box_save.error in self.box_action.box_save.macro_error_list:
            _bind(self, 'macro_err_retry_process')()
        elif self.box_action.box_save.error in self.box_action.box_save.cr_break_err_list:
            _bind(self, 'cr_break_err_retry_process')()
        elif self.box_action.box_save.error == 'print_end_err':
            _bind(self, 'print_end_err_retry_process')()
        if self.box_action.box_save.error is not None:
            _bind(self.box_action, 'set_cool_temp')()
        elif self.box_action.print_stats.state == 'paused':
            _bind(self.gcode, 'run_script_from_command')('RESUME')
            if self.box_state.resume_flag:
                _bind(self.gcode, 'run_script_from_command')('M400')
                self.box_state.resume_flag = False
                _bind(logging, 'info')('clear resume_flag')


    def get_connect_state(self):
        preloading_modes = {'CLOSE': 0, 'OPEN': 1, 'RUN': 2, 'TIGHT': 3}
        preload_addresses = 0
        for i in range(4):
            previous_state = _bind(self.box_state, 'get_Tn_data')(i + 1, 'state')
            if (addr_manager_table_mb[i].online == 1
                    and addr_manager_table_mb[i].mode == 0
                    and (previous_state == 'disconnect' or previous_state == 'None')):
                _bind(self.box_state, 'modify_Tn_data')(i + 1, 'connect', 'state')
                _bind(self.box_action, 'communication_set_box_mode')(i + 1, 'IDLE')
                _bind(self.box_action, 'communication_get_version_sn')(i + 1)
                if previous_state == 'None':
                    preload_addresses |= 1 << i
                    _bind(self.box_action, 'communication_get_hardware_status')(i + 1)
                    _bind(self.box_action, 'communication_get_filament_sensor_state')(i + 1, 'MATERIAL')
                    material_status = _bind(self.box_state, 'get_Tn_inner_data')(
                        i + 1, 'filament_sensor', subpart='material')
                    tn = 'T' + str(i + 1)
                    _bind(self.box_state, 'sync_tn_data')(tn, material_status)
                    _bind(self.reactor, 'register_callback')(self.box_action.update_same_material_list)
                    _bind(self.box_state, 'modify_Tn_data')(
                        i + 1, addr_manager_table_mb[i].uniid, 'uuid')
                if self.box_action.box_save.preloading_en:
                    _bind(self.box_action, 'communication_set_pre_loading')(i + 1, preloading_modes['OPEN'])
                else:
                    _bind(self.box_action, 'communication_set_pre_loading')(i + 1, preloading_modes['CLOSE'])
        if (preload_addresses != 0 and self.box_action.box_save.preloading_poweron
                and _bind(self.box_action, 'get_filament_sensor_detect')() is False):
            _bind(self.gcode, 'run_script')('BOX_FIRST_POWER_ON_PRELOAD ADDRS=%d' % preload_addresses)


    def box_connect_state_check(self):
        connected_count = 0
        for addr in range(1, 5):
            box_state = _bind(self.box_action.box_state, 'get_Tn_data')(addr, 'state')
            if box_state == 'connect':
                connected_count += 1
        if connected_count == 0:
            _bind(self.box_action.box_state, 'modify_Tn_data')(0, 'disconnect', None, uppart='state')
            if self.box_action.box_state.Tn_data['enable'] == 1:
                _bind(self.gcode, 'run_script_from_command')(
                    'SET_FILAMENT_SENSOR SENSOR=filament_sensor ENABLE=1')
        elif _bind(self.box_action.box_state, 'get_Tn_data')(None, None, uppart='state') == 'disconnect':
            _bind(self.box_action.box_state, 'modify_Tn_data')(0, 'connect', None, uppart='state')
            if self.box_action.box_state.Tn_data['enable'] == 1:
                _bind(self.gcode, 'run_script_from_command')(
                    'SET_FILAMENT_SENSOR SENSOR=filament_sensor ENABLE=0')
        elif _bind(self.box_action.box_state, 'get_Tn_data')(None, None, uppart='state') == 'None':
            _bind(self.box_action.box_state, 'modify_Tn_data')(0, 'connect', None, uppart='state')
            if self.box_action.box_state.Tn_data['enable'] == 1:
                _bind(self.gcode, 'run_script_from_command')(
                    'SET_FILAMENT_SENSOR SENSOR=filament_sensor ENABLE=0')


    def test_error(self):
        if self.box_action.box_save.error:
            return
        if self.test == 'empty_print':
            if _bind(self.box_state, 'get_Tn_data')(0, 0, uppart='filament_useup') == 0:
                _bind(self.box_state, 'modify_Tn_data')(0, 1, 0, uppart='filament_useup')
            if self.box_state.resume_flag:
                _bind(logging, 'warning')('wait resume end')
                return False
            _bind(self.box_action.box_save, 'recode_err')(
                'empty_print', tnn=self.box_action.box_save.last_cmd)
            error(key_enwind_err, self.gcode, self.reactor,
                  self.box_action.print_stats.state, self.box_action.box_save)


    def print_end_move_to_cut_err_retry_process(self):
        _bind(self.box_action.box_save, 'clear_err')()
        _bind(self.box_action, 'go_to_extrude_pos')()
        ret = _bind(self.box_action, 'box_retrude_material')()
        if ret is False:
            _bind(self.box_action.box_save, 'recode_err')('print_end_err', self.box_action.box_save.last_cmd)
            return False
        _bind(self.box_action, 'move_to_safe_pos')()
        self.box_action.box_save.last_cmd_tmp = self.box_action.box_save.last_cmd
        self.box_action.box_save.last_cmd = None


    def macro_extrusion_all_materials_err_retry_process(self):
        tnn = self.box_action.box_save.error_tnn['tnn']
        last_tnn = self.box_action.box_save.error_tnn['last_tnn']
        _bind(self.box_action.box_save, 'clear_err')()
        _bind(self, 'cmd_extrusion_all_materials')(None)
        _bind(self, 'filament_err_retry_process')(tnn=tnn, last_tnn=last_tnn)


    def print_end_err_retry_process(self):
        last_tnn = self.box_action.box_save.error_tnn['last_tnn']
        if last_tnn:
            last_tnn_content = _bind(self.box_state, 'get_Tnn_content')(last_tnn)
            ret = _bind(self.box_action, 'communication_set_box_mode')(last_tnn_content['addr'], 'IDLE')
            if ret is False:
                return False
        _bind(self.box_action.box_save, 'clear_err')()
        _bind(self.gcode, 'run_script_from_command')('END_PRINT')


    def macro_err_retry_process(self):
        gcmd = list(self.box_action.box_save.error_next_macro)
        last_tnn = self.box_action.box_save.error_tnn['last_tnn']
        tnn = self.box_action.box_save.error_tnn['tnn']
        addr = self.box_action.box_save.error_tnn['addr']
        if tnn:
            tnn_content = _bind(self.box_state, 'get_Tnn_content')(tnn)
            ret = _bind(self.box_action, 'communication_set_box_mode')(tnn_content['addr'], 'IDLE')
            if ret is False:
                return False
        elif last_tnn:
            last_tnn_content = _bind(self.box_state, 'get_Tnn_content')(last_tnn)
            ret = _bind(self.box_action, 'communication_set_box_mode')(last_tnn_content['addr'], 'IDLE')
            if ret is False:
                return False
        elif addr:
            ret = _bind(self.box_action, 'communication_set_box_mode')(addr, 'IDLE')
            if ret is False:
                return False
        _bind(self.box_action.box_save, 'clear_err')()
        command_format = 'gcmd: %s'
        _bind(logging, 'info')(command_format % gcmd)
        for i in range(len(gcmd)):
            _bind(self.gcode, 'run_script_from_command')(gcmd[i])
        _bind(self.gcode, 'run_script_from_command')('M400')


    def cmd_extrusion_all_materials(self, gcmd):
        ret = _bind(self.box_action, 'extrusion_all_materials')()
        if ret is False:
            _bind(self.box_action.box_save, 'clear_err')()
            _bind(self.box_state, 'clear_e_err')()
            _bind(self.box_action.box_save, 'recode_err')('macro_extrusion_all_materials_err')
            return
        self.box_action.box_save.last_cmd_tmp = self.box_action.box_save.last_cmd
        self.box_action.box_save.last_cmd = None
        _bind(self.gcode, 'run_script_from_command')('SET_FILAMENT_SENSOR SENSOR=filament_sensor ENABLE=0')


    def filament_err_retry_process(self, last_tnn=None, tnn=None):
        _last_tnn = self.box_action.box_save.error_tnn['last_tnn'] if last_tnn is None else last_tnn
        _tnn = self.box_action.box_save.error_tnn['tnn'] if tnn is None else tnn
        if _tnn is None:
            message_format = '[error] tnn is None, last_tnn: %s, tnn: %s'
            _bind(logging, 'warning')(message_format % (_last_tnn, _tnn))
            return
        tnn_content = _bind(self.box_state, 'get_Tnn_content')(_tnn)
        ret = _bind(self.box_action, 'communication_set_box_mode')(tnn_content['addr'], 'IDLE')
        if ret is False:
            return False
        _bind(self.box_action.box_save, 'clear_err')()
        ret = _bind(self.box_action, 'box_extrude_material')(_tnn)
        if ret is False:
            if not self.box_action._is_transport_uncertain():
                _bind(self.box_action, 'box_retrude_material')()
            _bind(self.box_action.box_save, 'recode_err')('box_extrude_err', _last_tnn, _tnn)
            return False
        ret = _bind(self.box_action, 'extruder_extrude')(_tnn)
        if ret is False:
            _bind(self.box_action.box_save, 'recode_err')('extruder_extrude_err', _last_tnn, _tnn)
            return False
        ret = _bind(self, 'material_change_flush')(_last_tnn, _tnn)
        if ret is False:
            _bind(self.box_action.box_save, 'recode_err')('flush_err', _last_tnn, _tnn)
            return False
        _bind(self.gcode, 'run_script_from_command')('G92 E0')
        _bind(self.gcode, 'run_script_from_command')('G90')
        _bind(self.gcode, 'run_script_from_command')('M400')
        return True


    def retrude_err_retry_process(self):
        last_tnn = self.box_action.box_save.error_tnn['last_tnn']
        tnn = self.box_action.box_save.error_tnn['tnn']
        if tnn is None:
            message_format = '[error] last_tnn: %s, tnn: %s'
            _bind(logging, 'warning')(message_format % (last_tnn, tnn))
            return
        if last_tnn:
            last_tnn_content = _bind(self.box_state, 'get_Tnn_content')(last_tnn)
            ret = _bind(self.box_action, 'communication_set_box_mode')(last_tnn_content['addr'], 'IDLE')
            if ret is False:
                return False
        elif self.box_action.box_save.error_tnn['addr']:
            ret = _bind(self.box_action, 'communication_set_box_mode')(self.box_action.box_save.error_tnn['addr'], 'IDLE')
            if ret is False:
                return False
        _bind(self.box_action.box_save, 'clear_err')()
        _bind(self.box_action, 'go_to_extrude_pos')()
        ret = _bind(self.box_action, 'box_retrude_material')()
        if ret is False:
            _bind(self.box_action.box_save, 'recode_err')('retrude_err', last_tnn, tnn)
            return False
        ret = _bind(self.box_action, 'box_extrude_material')(tnn)
        if ret is False:
            _bind(self.box_action.box_save, 'recode_err')('box_extrude_err', last_tnn, tnn)
            return False
        ret = _bind(self.box_action, 'extruder_extrude')(tnn)
        if ret is False:
            _bind(self.box_action.box_save, 'recode_err')('extruder_extrude_err', last_tnn, tnn)
            return False
        ret = _bind(self, 'material_change_flush')(last_tnn, tnn)
        if ret is False:
            _bind(self.box_action.box_save, 'recode_err')('flush_err', last_tnn, tnn)
            return False
        _bind(self.gcode, 'run_script_from_command')('G92 E0')
        _bind(self.gcode, 'run_script_from_command')('G90')
        _bind(self.gcode, 'run_script_from_command')('M400')
        return True


    def box_extrude_err_retry_process(self):
        last_tnn = self.box_action.box_save.error_tnn['last_tnn']
        tnn = self.box_action.box_save.error_tnn['tnn']
        if tnn is None:
            message_format = '[error] tnn is None, last_tnn: %s, tnn: %s'
            _bind(logging, 'warning')(message_format % (last_tnn, tnn))
            return
        tnn_content = _bind(self.box_state, 'get_Tnn_content')(tnn)
        ret = _bind(self.box_action, 'communication_set_box_mode')(tnn_content['addr'], 'IDLE')
        if ret is False:
            return False
        _bind(self.box_action.box_save, 'clear_err')()
        ret = _bind(self.box_action, 'box_extrude_material')(tnn)
        if ret is False:
            _bind(self.box_action.box_save, 'recode_err')('box_extrude_err', last_tnn, tnn)
            return False
        ret = _bind(self.box_action, 'extruder_extrude')(tnn)
        if ret is False:
            _bind(self.box_action.box_save, 'recode_err')('extruder_extrude_err', last_tnn, tnn)
            return False
        ret = _bind(self, 'material_change_flush')(last_tnn, tnn)
        if ret is False:
            _bind(self.box_action.box_save, 'recode_err')('flush_err', last_tnn, tnn)
            return False
        _bind(self.gcode, 'run_script_from_command')('G92 E0')
        _bind(self.gcode, 'run_script_from_command')('G90')
        _bind(self.gcode, 'run_script_from_command')('M400')
        return True


    def extruder_extrude_err_retry_process(self):
        last_tnn = self.box_action.box_save.error_tnn['last_tnn']
        tnn = self.box_action.box_save.error_tnn['tnn']
        if last_tnn is None and tnn is None:
            message_format = '[error] last_tnn: %s, tnn: %s'
            _bind(logging, 'warning')(message_format % (last_tnn, tnn))
            return
        tnn_content = _bind(self.box_state, 'get_Tnn_content')(tnn)
        ret = _bind(self.box_action, 'communication_set_box_mode')(tnn_content['addr'], 'IDLE')
        if ret is False:
            return False
        _bind(self.box_action.box_save, 'clear_err')()
        ret = _bind(self.box_action, 'extruder_extrude')(tnn)
        if ret is False:
            _bind(self.box_action.box_save, 'recode_err')('extruder_extrude_err', last_tnn, tnn)
            return False
        ret = _bind(self, 'material_change_flush')(last_tnn, tnn)
        if ret is False:
            _bind(self.box_action.box_save, 'recode_err')('flush_err', last_tnn, tnn)
            return False
        _bind(self.gcode, 'run_script_from_command')('G92 E0')
        _bind(self.gcode, 'run_script_from_command')('G90')
        _bind(self.gcode, 'run_script_from_command')('M400')
        return True


    def flush_err_retry_process(self):
        _bind(self.box_action, 'go_to_extrude_pos')()
        last_tnn = self.box_action.box_save.error_tnn['last_tnn']
        tnn = self.box_action.box_save.error_tnn['tnn']
        tnn_content = _bind(self.box_state, 'get_Tnn_content')(tnn)
        ret = _bind(self.box_action, 'communication_set_box_mode')(tnn_content['addr'], 'IDLE')
        if ret is False:
            return False
        ret = _bind(self.box_action, 'communication_set_box_mode')(tnn_content['addr'], 'PRINT', num=tnn_content['num'])
        if ret is False:
            return False
        _bind(self.box_action.box_save, 'clear_err')()
        ret = _bind(self, 'material_change_flush')(last_tnn, tnn)
        if ret is False:
            _bind(self.box_action.box_save, 'recode_err')('flush_err', last_tnn, tnn)
            return False
        _bind(self.box_action, 'z_restore')()
        _bind(self.gcode, 'run_script_from_command')('G92 E0')
        _bind(self.gcode, 'run_script_from_command')('G90')
        _bind(self.box_action.box_save, 'restore_printer_accel')()
        return True


    def cmd_get_box_state(self, gcmd):
        addr = _bind(gcmd, 'get_int')('ADDR', minval=1, maxval=4)
        _bind(self.box_action, 'communication_get_box_state')(addr)


    def cmd_get_buffer_state(self, gcmd):
        addr = _bind(gcmd, 'get_int')('ADDR', minval=1, maxval=4)
        _bind(self.box_action, 'communication_get_buffer_state')(addr)


    def cmd_get_hardware_status(self, gcmd):
        addr = _bind(gcmd, 'get_int')('ADDR', minval=1, maxval=4)
        _bind(self.box_action, 'communication_get_hardware_status')(addr)


    def cmd_cut_state(self, gcmd):
        _bind(self.box_action.cut, 'state')()


    def cmd_error_clear(self, gcmd):
        _bind(self, 'error_clear')()


    def cmd_error_resume_process(self, gcmd):
        _bind(self, 'error_resume_process')()
        action = self.box_action
        policy = self.printer.lookup_object('k2_print')
        if (action.print_stats.state != 'paused' or policy.resume_err
                or not self.box_state.get_Tn_data(0, 0, uppart='enable')):
            return
        slot = action.box_save.last_cmd
        if slot is None:
            return

        def require(result, phase):
            if result is not True:
                logging.error('K2 CFS resume slot=%s failed at %s', slot, phase)
                policy.fail('resume')

        require(action.box_save.error is None and not action.Tn_action_flag
                and not self.box_state.extrude_err, 'ready')
        content = self.box_state.get_Tnn_content(slot)
        addr = content['addr']
        require(action.get_filament_sensor_detect() is True and action.check_connect(addr),
                'loaded filament')
        require(action.communication_get_filament_sensor_state(addr, 'CONNECTIONS'),
                'connection query')
        mask = self.box_state.get_Tn_inner_data(addr, 'filament_sensor', subpart='connections')
        require(type(mask) is int and mask == 1 << 'ABCD'.index(content['num']), 'loaded slot')
        # Error clearing and a manual purge can leave the loaded slot in IDLE.
        if action.box_save.preloading_en:
            require(action.communication_set_pre_loading(addr, 0, force=True), 'close preload')
        require(action.communication_tighten_up_enable(addr, 'DISABLE'), 'disable tightening')
        require(action.communication_set_box_mode(addr, 'PRINT', content['num']), 'print mode')
        require(action.communication_get_box_state(addr), 'state query')
        require(self.box_state.get_Tn_data(addr, 'mode') == '2', 'confirmed print mode')
        logging.info('K2 CFS resume slot=%s PRINT confirmed', slot)


    def cmd_get_gcode_used_tnn(self, gcmd):
        _bind(self.box_action, 'get_gcode_used_tnn')()


    def cmd_update_same_material_list(self, gcmd):
        _bind(self.box_state, 'update_same_material_list')()


    def cmd_set_enable_auto_refill(self, gcmd):
        self.box_state.Tn_data['auto_refill'] = _bind(gcmd, 'get_int')('ENABLE', 1)


    def cmd_disable_heart_process(self, gcmd):
        tnn = _bind(gcmd, 'get')('TNN')
        _bind(self.box_action, 'disable_heart_process')(tnn)


    def cmd_enable_heart_process(self, gcmd):
        tnn = _bind(gcmd, 'get')('TNN')
        _bind(self.box_action, 'enable_heart_process')(tnn)


    def cmd_show_error(self, gcmd):
        message_format = 'error: %s, error_tnn: %s'
        _bind(self.gcode, 'respond_info')(message_format % (
            self.box_action.box_save.error, self.box_action.box_save.error_tnn))


    def cmd_modify_Tn_data(self, gcmd):
        addr = _bind(gcmd, 'get_int')('ADDR', minval=1, maxval=4)
        num = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('NUM')
        part = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('PART')
        data = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('DATA')
        _bind(self.box_state, 'modify_Tn_data')(addr, data, part, num, cmd=True)


    def cmd_modify_Tn_inner_data(self, gcmd):
        addr = _bind(gcmd, 'get_int')('ADDR', minval=1, maxval=4)
        num = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('NUM')
        part = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('PART')
        data = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('DATA')
        _bind(self.box_state, 'modify_Tn_inner_data')(addr, data, part, num)


    def cmd_modify_Tnn_map(self, gcmd):
        for tnn in self.box_state.Tnn_map:
            mapped = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')(tnn)
            if mapped is not None and mapped in self.box_state.Tnn_map:
                self.box_state.Tnn_map[tnn] = mapped
        _bind(self.box_state, 'update_tn_save_data')(
            'tnn_map', tnn_map=self.box_state.Tnn_map, save=True)
        _bind(logging, 'info')('Tnn_map: %s' % self.box_state.Tnn_map)


    def cmd_communication_test(self, gcmd):
        count = _bind(gcmd, 'get_int')('COUNT', default=10, minval=0, maxval=1000000)
        timeout = _bind(gcmd, 'get_float')('TIMEOUT', default=0.1, minval=0.0, maxval=1.0)
        interval = _bind(gcmd, 'get_float')('INTERVAL', default=0.1, minval=0.01, maxval=1.0)
        addr = _bind(gcmd, 'get_int')('ADDR', minval=1, maxval=4)
        num = _bind(gcmd, 'get_int')('NUM', minval=0, maxval=255)
        _bind(self.box_action, 'communication_test')(count, timeout, interval, addr, num)


    def cmd_get_filament_sensor_state(self, gcmd):
        addr = _bind(gcmd, 'get_int')('ADDR', minval=1, maxval=4)
        pos = gcmd.get('POSITION')
        if pos not in ('CONNECTIONS', 'MATERIAL'):
            self.printer.lookup_object('k2_print').fail('cfs_sensor_position')
        _bind(self.box_action, 'communication_get_filament_sensor_state')(addr, pos)
        _bind(self.gcode, 'respond_info')('[box] filament sensor state: %x' %
            _bind(self.box_state, 'get_Tn_inner_data')(
                addr, 'filament_sensor', subpart=_bind(pos, 'lower')()))


    def cmd_measuring_wheel(self, gcmd):
        addr = _bind(gcmd, 'get_int')('ADDR', minval=1, maxval=4)
        action = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('ACTION')
        if action == 'CLEAN' or action == 'GET':
            _bind(self.box_action, 'communication_measuring_wheel')(addr, action)
        else:
            _bind(logging, 'warning')("param is error, ACTION only with 'GET' and 'CLEAN'")


    def cmd_show_flush_list(self, gcmd):
        flush_length = _bind(gcmd, 'get_float')('FLUSH_LENGTH', default=0)
        flush_length_list = []
        _bind(self.box_action, 'cal_flush_list')(flush_length, flush_length_list)
        _bind(self.gcode, 'respond_info')('flush_length: %s' % flush_length_list)


    def cmd_show_Tnn_data(self, gcmd):
        color_map = {
            'F4E076': '\u6d45\u9ec4', 'FF614B': '\u6a59\u7ea2',
            '6C84FF': '\u94b4\u84dd', '9CFF4F': '\u8349\u7eff',
            'BA552A': '\u8910\u8272', 'FF1E1E': '\u5927\u7ea2',
            'FFFFFF': '\u767d\u8272', '9EA7AE': '\u7070\u8272',
            'CE58F8': '\u7d2b\u8272', '26EEEE': '\u5929\u84dd',
            '66DBA2': '\u6d45\u7eff', 'FF97E1': '\u5ae9\u7c89\u7ea2',
            'FF8B1F': '\u6854\u9ec4', '1B04AE': '\u6df1\u84dd',
            '000000': '\u9ed1\u8272', '7A92AC': '\u6df1\u7070',
            '00A3FF': '\u84dd\u8272', '72A530': '\u6a44\u6984\u7eff',
            '3DDF57': '\u4e2d\u7eff', 'FF37AF': '\u7c89\u8272',
            'FFF014': '\u67e0\u6aac\u9ec4', 'B2A1E1': '\u6de1\u7d2b\u8272',
            '8A43FF': '\u84dd\u7d2b', 'FFA800': '\u4e2d\u9ec4',
        }
        data = _bind(copy, 'deepcopy')(self.box_state.Tn_data)
        num_map = {'A': 0, 'B': 1, 'C': 2, 'D': 3}
        for addr in range(1, 5):
            for num in num_map:
                tn = 'T' + str(addr)
                cv = data[tn]['color_value'][num_map[num]]
                if cv != 'None' and cv[1:7] in color_map:
                    data[tn]['color_value'][num_map[num]] = color_map[cv[1:7]]
        _bind(self.gcode, 'respond_info')('Tn_data: %s' % data)


    def cmd_get_flush_velocity_test(self, gcmd):
        last_tnn = _bind(gcmd, 'get')('LAST')
        next_tnn = _bind(gcmd, 'get')('NEXT')
        _bind(self.box_action, 'get_flush_velocity')(last_tnn, next_tnn)


    def cmd_first_power_on_preload(self, gcmd):
        action_map = {'CLOSE': 0, 'OPEN': 1, 'RUN': 2, 'TIGHT': 3}
        addrs = _bind(gcmd, 'get_int')('ADDRS', 0)
        for i in range(4):
            addr = i + 1
            if addrs & (1 << i):
                _bind(self.box_action, 'communication_get_filament_sensor_state')(
                    addr, 'MATERIAL')
                material_status = _bind(self.box_state, 'get_Tn_inner_data')(
                    addr, 'filament_sensor', subpart='material')
                if material_status == 0:
                    continue
                message_format = 'get_connect_state: get_filament_sensor_state material_status:%s'
                _bind(logging, 'info')(message_format % material_status)
                _bind(self.box_action, 'communication_set_pre_loading')(
                    addr, action_map['RUN'], num=material_status)
                _bind(self.box_action, 'communication_get_rfid')(addr, num=material_status)
                _bind(self.box_action, 'communication_get_remain_len')(
                    addr, num=material_status)


    def cmd_send_data(self, gcmd):
        message_format = 'gcmd = %s'
        _bind(logging, 'info')(message_format % gcmd)
        cmd = _bind(gcmd, 'get_int')('CMD', 10, minval=0, maxval=32)
        data = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('DATA')
        addr = _bind(gcmd, 'get_int')('ADDR', None, minval=1, maxval=4)
        state = _bind(gcmd, 'get_int')('STATE', None, minval=0, maxval=255)
        timeout = _bind(gcmd, 'get_int')('TIMEOUT', 5, minval=1, maxval=120)
        if data is not None:
            data_encode = _bind(self.parse, 'parse_num_string_to_byte')(data)
        else:
            data_encode = ''
        if addr is None:
            _bind(logging, 'info')('addr is None')
            addr = 0
        addr_encode = _bind(self.parse, 'parse_num_to_byte')(addr)
        if state is None:
            _bind(self.gcode, 'respond_info')('state is None')
            state = 0
        state_encode = _bind(self.parse, 'parse_num_to_byte')(state)
        cmd_encode = _bind(self.parse, 'parse_num_to_byte')(cmd)
        _bind(self.box_action, 'send_data')(
            addr_encode, cmd_encode, timeout, state_encode, data_encode)


    def cmd_extrude_process(self, gcmd):
        addr = _bind(gcmd, 'get_int')('ADDR', minval=1, maxval=4)
        num = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('NUM')
        if self.box_action.boxcfg.version == 0:
            trigger = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('TRIGGER')
            _bind(self.box_action, 'communication_extrude_process')(addr, num, trigger)
        elif self.box_action.boxcfg.version == 1:
            stage = _bind(gcmd, 'get_int')('STAGE', default=2, minval=1, maxval=4)
            _bind(self.box_action, 'communication_extrude_process')(addr, num, stage=stage)


    def cmd_extrude2_process(self, gcmd):
        addr = _bind(gcmd, 'get_int')('ADDR', minval=1, maxval=4)
        num = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('NUM')
        trigger = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('TRIGGER')
        _bind(self.box_action, 'communication_extrude2_process')(addr, num, trigger)


    def cmd_retrude_process(self, gcmd):
        addr = _bind(gcmd, 'get_int')('ADDR', minval=1, maxval=4)
        num = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('NUM')
        trigger = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('TRIGGER')
        _bind(self.box_action, 'communication_retrude_process')(addr, num, trigger)


    def cmd_ctrl_connection_motor_action(self, gcmd):
        addr = _bind(gcmd, 'get_int')('ADDR', minval=1, maxval=4)
        action = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('ACTION')
        _bind(self.box_action, 'communication_ctrl_connection_motor_action')(addr, action)


    def cmd_blow(self, gcmd):
        _bind(self.box_action, 'blow')()


    def cmd_get_flush_len(self, gcmd):
        tmp = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('SCV')
        if len(tmp) != 6:
            _bind(self.gcode, 'respond_info')('warning, the length of "SCV" is not 6')
            return
        hex_pairs = [tmp[j:j + 2] for j in range(0, len(tmp), 2)]
        scv = [int(pair, 16) for pair in hex_pairs]
        message_format = 'scv: %s'
        _bind(self.gcode, 'respond_info')(message_format % scv)
        tmp = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('TCV')
        if len(tmp) != 6:
            _bind(self.gcode, 'respond_info')('warning, the length of "TCV" is not 6')
            return
        hex_pairs = [tmp[j:j + 2] for j in range(0, len(tmp), 2)]
        tcv = [int(pair, 16) for pair in hex_pairs]
        message_format = 'tcv: %s'
        _bind(self.gcode, 'respond_info')(message_format % tcv)
        _bind(self.box_action, 'get_flush_len')(scv, tcv)


    def cmd_box_end(self, gcmd):
        _bind(self, 'box_end')()


    def cmd_retrude_material_with_tnn(self, gcmd):
        _bind(self.box_action, 'go_to_extrude_pos')()
        if _bind(self.box_action, 'get_filament_sensor_detect')():
            _bind(self.box_action, 'Tn_Extrude')(
                self.box_action.boxcfg.Tn_retrude,
                self.box_action.boxcfg.Tn_retrude_velocity,
                self.box_action.boxcfg.Tn_extrude_temp)
            _bind(self.gcode, 'run_script_from_command')('G91')
            _bind(self.gcode, 'run_script_from_command')('G0 E-15 F120')
            _bind(self.gcode, 'run_script_from_command')('G90')
        tnn = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('TNN')
        if tnn is not None:
            if tnn not in self.box_action.box_state.Tnn_map:
                message_format = 'TNN[%s] not in Tnn gcode'
                _bind(logging, 'warning')(message_format % tnn)
                return
        else:
            _bind(self.gcode, 'respond_info')('[box] retrude, no tnn')
            for i in range(1, 5):
                if _bind(self.box_action, 'check_connect')(i):
                    _bind(self.box_action, 'communication_set_box_mode')(i, 'IDLE')
                    _bind(self.box_action, 'communication_retrude_process')(
                        i, '0', 'MATERIAL')
            self.box_action.box_save.last_cmd_tmp = self.box_action.box_save.last_cmd
            self.box_action.box_save.last_cmd = None
            return
        Tnn_content = _bind(self.box_state, 'get_Tnn_content')(tnn)
        _bind(self.box_action, 'communication_set_box_mode')(Tnn_content['addr'], 'IDLE')
        _bind(self.box_action, 'communication_ctrl_connection_motor_action')(
            Tnn_content['addr'], 'STOP')
        _bind(self.box_action, 'communication_retrude_process')(
            Tnn_content['addr'], Tnn_content['num'], 'MATERIAL')
        self.box_action.box_save.last_cmd_tmp = self.box_action.box_save.last_cmd
        self.box_action.box_save.last_cmd = None


    def cmd_generate_flush_array(self, gcmd):
        str = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('FLUSH_ARRAY')
        message_format = 'str: %s'
        _bind(self.gcode, 'respond_info')(message_format % str)
        rows = _bind(_bind(str, 'strip')(), 'split')('_')
        flush_array = []
        for row in rows:
            elements = _bind(row, 'split')(',')
            row_array = []
            for element in elements:
                if element:
                    row_array.append(int(element))
            if row_array:
                flush_array.append(row_array)
        message_format = 'flush_array: %s'
        _bind(self.gcode, 'respond_info')(message_format % flush_array)
        for i in range(4):
            for j in range(4):
                message_format = 'flush_array[%d][%d] = %d'
                _bind(logging, 'info')(message_format % (i, j, flush_array[i][j]))


    def cmd_check_material_refill(self, gcmd):
        _bind(self.box_action, 'check_material_refill')()


    def cmd_box_start_print(self, gcmd):
        action_map = {'CLOSE': 0, 'OPEN': 1, 'RUN': 2, 'TIGHT': 3}
        for addr in range(1, 5):
            if _bind(self.box_action, 'check_connect')(addr):
                if self.box_action.box_save.preloading_en:
                    if _bind(self.box_action, 'communication_set_pre_loading')(
                            addr, action_map['CLOSE'], force=True) is not True:
                        self.printer.lookup_object('k2_print').fail('cfs_start')
                if _bind(self.box_action, 'communication_tighten_up_enable')(
                        addr, 'DISABLE') is not True:
                    self.printer.lookup_object('k2_print').fail('cfs_start')
        if self.box_state.need_update_power_loss_info == False:
            self.box_state.need_update_power_loss_info = True


    def cmd_box_end_print(self, gcmd):
        action = self.box_action
        failure = None

        def attempt(callback, *args, require_ack=False, **kwargs):
            nonlocal failure
            try:
                result = callback(*args, **kwargs)
                if require_ack and result is not True:
                    self.printer.lookup_object('k2_print').fail('cfs_end')
                return result
            except Exception as error:
                logging.exception('K2 CFS end cleanup failed: %s args=%s', callback.__name__, args)
                if failure is None:
                    failure = error

        for addr in range(1, 5):
            if attempt(action.check_connect, addr):
                if action.box_save.preloading_en:
                    attempt(action.communication_set_pre_loading, addr, 1,
                            force=True, require_ack=True)
                attempt(action.communication_tighten_up_enable, addr, 'ENABLE', require_ack=True)
        for slot in self.box_state.Tnn_map:
            self.box_state.Tnn_map[slot] = slot
        attempt(action.box_save.clear_resume_tnn)
        self.box_state.need_update_power_loss_info = True
        attempt(action.power_loss_clean)
        attempt(self.box_state.modify_Tn_data, 0, 1, 0, uppart='enable')
        attempt(self.gcode.run_script_from_command,
                'SET_FILAMENT_SENSOR SENSOR=filament_sensor ENABLE=0')
        logging.info('Tnn_map: %s', self.box_state.Tnn_map)
        if failure is not None:
            raise failure


    def cmd_box_enable_CFS_print(self, gcmd):
        enable = _bind(gcmd, 'get_int')('ENABLE', minval=0, maxval=1)
        if enable == 0:
            _bind(self.box_state, 'modify_Tn_data')(0, 0, 0, uppart='enable')
            _bind(self.gcode, 'run_script_from_command')(
                'SET_FILAMENT_SENSOR SENSOR=filament_sensor ENABLE=1')
            _bind(logging, 'info')('disable box print')
            _bind(self.box_state, 'update_tn_save_data')('enable', enable=0, save=True)
        else:
            _bind(self.box_state, 'modify_Tn_data')(0, 1, 0, uppart='enable')
            _bind(self.gcode, 'run_script_from_command')(
                'SET_FILAMENT_SENSOR SENSOR=filament_sensor ENABLE=0')
            _bind(logging, 'info')('enable box print')
            _bind(self.box_state, 'update_tn_save_data')('enable', enable=1, save=True)


    def cmd_BOX_SET_CURRENT_BOX_IDLE_MODE(self, gcmd):
        if self.box_action.box_save.last_cmd is not None:
            content = self.box_state.get_Tnn_content(self.box_action.box_save.last_cmd)
            if self.box_action.communication_set_box_mode(content['addr'], 'IDLE') is not True:
                self.printer.lookup_object('k2_print').fail('cfs_end')


    def cmd_make_error(self, gcmd):
        error_list = ['empty_print', 'flush_err', 'extrude_timeout']
        error_index = _bind(gcmd, 'get_int')('ERROR_INDEX', default=255)
        if error_index == 255 or error_index >= len(error_list):
            message_format = 'param of "ERROR_INDEX" is error\n\t error_list: %s'
            _bind(self.gcode, 'respond_info')(message_format % error_list)
            self.test = None
            return
        tnn = None
        for index, value in enumerate(error_list):
            if error_index == index:
                message_format = 'make %s error'
                _bind(self.gcode, 'respond_info')(message_format % value)
                if value == 'empty_print':
                    if _bind(self.box_state, 'get_Tn_data')(0, 0, uppart='filament_useup') == 0:
                        _bind(self.box_state, 'modify_Tn_data')(0, 1, 0, uppart='filament_useup')
                    _bind(self.box_action.box_save, 'recode_err')(
                        'empty_print', tnn=self.box_action.box_save.last_cmd)
                    error(key_enwind_err, self.gcode, self.reactor,
                          self.box_action.print_stats.state, self.box_action.box_save)
                    self.test = value
                    return
                elif value == 'flush_err':
                    if self.box_action.box_save.last_cmd is None:
                        _bind(self.gcode, 'respond_info')('[error] need to extrude before this')
                    _bind(self.box_action.box_save, 'recode_err')(
                        'flush_err', tnn, self.box_action.box_save.last_cmd)
                    return
                elif value == 'extrude_timeout':
                    if self.box_action.test:
                        self.box_action.test = False
                    else:
                        self.box_action.test = True
                    message_format = 'extrude_timeout, test: %s'
                    _bind(self.gcode, 'respond_info')(message_format % self.box_action.test)


    def cmd_CR_BOX_PRE_OPT_EXTRA(self, gcmd):
        _bind(self.box_action, 'z_down')()
        _bind(self.gcode, 'run_script_from_command')('G90')
        command_format = 'G0 Y%.2f'
        _bind(self.gcode, 'run_script_from_command')(
            command_format % self.box_action.boxcfg.pre_cut_pos_y)
        _bind(self.gcode, 'run_script_from_command')('M400')


    def cmd_CR_BOX_PRE_OPT(self, gcmd):
        if self.box_action.box_state.Tn_data['enable'] == 0:
            return
        if self.box_state.Tn_data['state'] != 'connect':
            return
        self.box_action.Tn_action_flag = True
        if self.box_action.print_stats.state == 'printing':
            self.box_state.need_update_power_loss_info = False
        _bind(self.box_action, 'set_flushing_sign')()
        if self.box_action.is_use_ending_material:
            ret = _bind(self.box_action, 'is_use_ending_material_flush')(
                err='cr_break_ending_flush_err')
            if ret is False:
                return False
        _bind(self.gcode, 'run_script_from_command')('M400')


    def cmd_CR_BOX_CUT(self, gcmd):
        if (self.box_action.box_save.last_cmd is not None
                or _bind(self.box_action, 'get_filament_sensor_detect')()):
            ret = _bind(self.box_action, 'break_move_to_cut')()
            if ret is False:
                if self.box_state.extrude_err:
                    _bind(self.box_state, 'clear_e_err')()
                _bind(self.box_action.box_save, 'recode_err')('cr_break_cut_err')
                return False
        _bind(self.gcode, 'run_script_from_command')('M400')


    def cmd_CR_BOX_RETRUDE(self, gcmd):
        length = _bind(gcmd, 'get_float')('LENGTH', default=0)
        if length:
            if self.box_action.box_save.last_cmd:
                _bind(self.box_action, 'break_box_extruder_retrude')(length)
            else:
                message_format = 'hys: length = %s, last_cmd: %s'
                _bind(logging, 'info')(
                    message_format % (length, self.box_action.box_save.last_cmd))
            return
        ret = _bind(self.box_action, 'box_retrude_material')()
        if ret is False:
            _bind(self.box_action.box_save, 'recode_err')(
                'cr_break_retrude_err', self.box_action.box_save.last_cmd)
            return False
        _bind(self.gcode, 'run_script_from_command')('M400')


    def cmd_CR_BOX_EXTRUDE(self, gcmd):
        vtnn = _bind(gcmd, 'get')('TNN')
        tnn = self.box_state.Tnn_map[vtnn]
        if _bind(self.box_action, 'get_filament_sensor_detect')():
            _bind(logging, 'info')('hys: filament_sensor detected, return')
            return
        ret = _bind(self.box_action, 'box_extrude_material')(tnn)
        if ret is False:
            _bind(self.box_action.box_save, 'recode_err')(
                'cr_break_box_extrude_err', tnn=tnn, vtnn=vtnn)
            return False
        ret = _bind(self.box_action, 'extruder_extrude')(tnn)
        if ret is False:
            _bind(self.box_action.box_save, 'recode_err')(
                'cr_break_extruder_extrude_err', tnn=tnn, vtnn=vtnn)
            return False
        _bind(self.gcode, 'run_script_from_command')('M400')


    def cmd_CR_BOX_WASTE(self, gcmd):
        if self.load_ai is None:
            _bind(logging, 'warning')('load_ai module not found, skip waste detection')
            return
        message_format = ('execute_toolhead_ai_waste_management start: ai_switch=%d, '
                          'ai_waste_switch=%d, t_command_count=%d')
        _bind(logging, 'info')(message_format % (
            self.load_ai.ai_switch, self.load_ai.ai_waste_switch,
            self.load_ai.t_command_count))
        message_format = 'ai_switch = %d, ai_waste_switch = %d \n'
        _bind(self.gcode, 'respond_info')(message_format % (
            self.load_ai.ai_switch, self.load_ai.ai_waste_switch))
        if self.load_ai.t_command_count < 2:
            self.load_ai.t_command_count += 1
            return
        self.load_ai.t_command_count = 0
        if int(self.load_ai.ai_waste_switch) != 1:
            _bind(logging, 'info')('ai_waste_switch is disabled, skip waste detection')
            return
        _bind(self.gcode, 'run_script_from_command')('LOAD_AI_NOZZLE_CAM_POWER_ON')
        _bind(self.gcode, 'run_script_from_command')('BOX_GO_TO_EXTRUDE_POS')
        _bind(self.gcode, 'run_script_from_command')('M400')
        _bind(self.gcode, 'run_script_from_command')('G91')
        _bind(self.gcode, 'run_script_from_command')('G1 X-2 F12000')
        _bind(self.gcode, 'run_script_from_command')('M400')
        _bind(self.gcode, 'run_script_from_command')('G1 X9 F12000')
        _bind(self.gcode, 'run_script_from_command')('M400')
        _bind(self.gcode, 'run_script_from_command')('G4 P2000')
        _bind(self.gcode, 'run_script_from_command')('M400')
        _bind(self.gcode, 'respond_info')('WILL LOAD_AI_DEAL')
        _bind(self.gcode, 'run_script_from_command')('LOAD_AI_DETECT_WASTE')
        _bind(self.gcode, 'run_script_from_command')('LOAD_AI_NOZZLE_CAM_POWER_OFF')
        _bind(self.gcode, 'run_script_from_command')('G1 X-7')
        _bind(self.gcode, 'run_script_from_command')('M400')
        _bind(self.gcode, 'run_script_from_command')('G90')
        _bind(logging, 'info')('execute_toolhead_ai_waste_management end!!!')


    def cmd_CR_BOX_FLUSH(self, gcmd):
        vtnn = _bind(gcmd, 'get')('TNN')
        tnn = self.box_state.Tnn_map[vtnn]
        last_tnn = self.box_action.box_save.last_cmd_tmp
        ret = _bind(self, 'material_change_flush')(last_tnn, tnn)
        if ret is False:
            _bind(self.box_action.box_save, 'recode_err')(
                'cr_break_flush_err', tnn=tnn, vtnn=vtnn)
            return False
        _bind(self.gcode, 'run_script_from_command')('M400')


    def cmd_CR_BOX_END_OPT(self, gcmd):
        self.box_action.Tn_action_flag = False
        if self.box_action.print_stats.state == 'printing':
            self.box_state.need_update_power_loss_info = True
        _bind(self.box_action, 'reset_flushing_sign')()


    def cmd_CR_BOX_END_OPT_EXTRA(self, gcmd):
        _bind(self.box_action, 'z_restore')()
        _bind(self.box_action, 'move_to_safe_pos')()
        _bind(self.gcode, 'run_script_from_command')('M400')


    def has_flushing_sign(self):
        return _bind(self.box_action, 'has_flushing_sign')()


    def find_objs(self):
        self.load_ai = _bind(self.printer, 'lookup_object')('load_ai', None)
        _bind(self.box_action, 'find_objs')()
        _bind(self.box_action.box_save, 'find_objs')()
        _bind(self.printer, 'send_event')('box:ready')
        _bind(self.reactor, 'register_callback')(self.heart_process)
        if (self.box_action.boxcfg.cut_pos_x > self.box_action.boxcfg.pre_cut_cal_pos_x
                or self.box_action.boxcfg.cut_pos_x < _bind(_bind(self.config, 'getsection')('stepper_x'), 'getfloat')('position_min')):
            _bind(self.gcode, 'respond_raw')(key_cut_pos_err % self.box_action.boxcfg.cut_pos_x)
            _bind(logging, 'warning')(key_cut_pos_err % self.box_action.boxcfg.cut_pos_x)


    def get_flush_length_from_gcode(self, last_tnn, current_tnn):
        message_format = 'flush; last_tnn: %s, current_tnn: %s'
        _bind(logging, 'info')(message_format % (last_tnn, current_tnn))
        virtual_sdcard = _bind(self.printer, 'lookup_object')('virtual_sdcard')
        flush_para = _bind(self.printer.lookup_object('k2_print'), 'get_gcode_flush_para')()
        if flush_para is None:
            _bind(logging, 'warning')('Error in getting flushing parameters')
            return None
        _bind(logging, 'info')('flush_para: %s' % flush_para)
        multiplier = flush_para['flush_multiplier']
        if multiplier is None:
            multiplier = 1
        volumes = flush_para['flush_volumes_matrix']
        if volumes is None or len(volumes) < 2:
            _bind(logging, 'warning')('gcode has no flush parameters')
            return None
        material_quantity = _bind(math, 'sqrt')(len(volumes))
        if _bind(material_quantity, 'is_integer')() is False:
            _bind(logging, 'warning')('flushing parameter format is abnormal')
            return None
        _bind(logging, 'info')('material_quantity: %d' % material_quantity)
        array = _bind(np, 'array')(volumes)
        matrix = _bind(array, 'reshape')(int(material_quantity), int(material_quantity))
        for name, tnn in _bind(self.box_state.Tn_map, 'items')():
            if last_tnn == self.box_state.Tnn_map[tnn]:
                last_tnn = int(name[1:])
                break
        if not isinstance(last_tnn, int) or last_tnn >= material_quantity:
            _bind(logging, 'warning')('The box address does not comply with the rules, last_tnn: %s' % last_tnn)
            return None
        for name, tnn in _bind(self.box_state.Tn_map, 'items')():
            if current_tnn == self.box_state.Tnn_map[tnn]:
                current_tnn = int(name[1:])
                break
        if not isinstance(current_tnn, int) or current_tnn >= material_quantity:
            _bind(logging, 'warning')('The box address does not comply with the rules, current_tnn: %s' % current_tnn)
            return None
        volume = matrix[last_tnn][current_tnn]
        length = _bind(self.box_action, 'material_volume_to_length')(volume) * multiplier
        message_format = 'flush_volume: %f, flush_len: %f'
        _bind(self.gcode, 'respond_info')(message_format % (volume, length))
        return length


    def get_flush_len(self, last_tnn, current_tnn):
        ignored = ['-1', 'none', 'unknown']
        multiplier = 1
        if last_tnn is None and self.box_action.remain_material.color is None:
            _bind(logging, 'info')('last_tnn is none and save color is none')
            return self.box_action.boxcfg.Tn_extrude
        if self.box_action.print_stats.state == 'printing' and last_tnn is not None:
            _bind(logging, 'info')('Try to get the resize length from the file')
            length = _bind(self, 'get_flush_length_from_gcode')(last_tnn, current_tnn)
            current = _bind(self.box_state, 'get_Tnn_content')(current_tnn)
            previous = _bind(self.box_state, 'get_Tnn_content')(last_tnn)
            source_type = _bind(self.box_state, 'get_Tn_data')(previous['addr'], 'material_type', previous['num'])
            target_type = _bind(self.box_state, 'get_Tn_data')(current['addr'], 'material_type', current['num'])
            if length is not None:
                if source_type != target_type:
                    multiplier = 2
                    _bind(logging, 'info')('different material type')
                    if length * multiplier > self.box_action.boxcfg.diff_material_type_min_flush_len:
                        return length * multiplier
                    _bind(logging, 'info')('different material type min flush len')
                    return self.box_action.boxcfg.diff_material_type_min_flush_len
                if length * multiplier < self.box_action.boxcfg.box_first_clean_length + self.box_action.boxcfg.box_need_clean_length:
                    return self.box_action.boxcfg.box_first_clean_length + self.box_action.boxcfg.box_need_clean_length
                return length * multiplier
        current = _bind(self.box_state, 'get_Tnn_content')(current_tnn)
        color = self.box_action.remain_material.color
        if color is None or color in ignored:
            _bind(logging, 'info')('scv(last_tnn: %s) is ignore' % last_tnn)
            return self.box_action.boxcfg.Tn_extrude
        components = [color[index:index + 2] for index in range(1, len(color), 2)]
        source_rgb = [int(component, 16) for component in components]
        color = _bind(self.box_state, 'get_Tn_data')(current['addr'], 'color_value', current['num'])
        if color is None or color in ignored:
            _bind(logging, 'info')('tcv(last_tnn: %s) is ignore' % current_tnn)
            return self.box_action.boxcfg.Tn_extrude
        components = [color[index:index + 2] for index in range(1, len(color), 2)]
        target_rgb = [int(component, 16) for component in components]
        length = _bind(self.box_action, 'get_flush_len')(source_rgb, target_rgb)
        if length is None:
            message_format = '[warning] do not get flush_len check the color_value of last_tnn(%s) and current(%s)'
            _bind(logging, 'warning')(message_format % (last_tnn, current_tnn))
            return self.box_action.boxcfg.Tn_extrude
        target_type = _bind(self.box_state, 'get_Tn_data')(current['addr'], 'material_type', current['num'])
        source_type = self.box_action.remain_material.type
        if source_type != target_type:
            multiplier = 2
            _bind(self.gcode, 'respond_info')('different material type')
            if length * multiplier > self.box_action.boxcfg.diff_material_type_min_flush_len:
                _bind(self.gcode, 'respond_info')(('flush_len: %f' % length) * multiplier)
                return length * multiplier
            _bind(logging, 'info')('different material type min flush len')
            return self.box_action.boxcfg.diff_material_type_min_flush_len
        _bind(self.gcode, 'respond_info')(('flush_len: %f' % length) * multiplier)
        if length * multiplier < self.box_action.boxcfg.box_first_clean_length + self.box_action.boxcfg.box_need_clean_length:
            return self.box_action.boxcfg.box_first_clean_length + self.box_action.boxcfg.box_need_clean_length
        return length * multiplier


    def material_change_flush(self, last_tnn, tnn):
        _bind(self.gcode, 'run_script_from_command')('M106 P0 S50')
        _bind(self.gcode, 'run_script_from_command')('M400')
        length = _bind(self, 'get_flush_len')(last_tnn, tnn)
        velocity, percent = _bind(self.box_action, 'get_flush_velocity')(last_tnn, tnn)
        last_temp, current_temp, flush_temp = _bind(self.box_action, 'get_flush_temp')(last_tnn, tnn)
        last_max_temp, current_max_temp, flush_max_temp = _bind(self.box_action, 'get_flush_max_temp')(last_tnn, tnn)
        _bind(self.box_action, 'quickly_wait_heating')(flush_temp)
        if last_temp >= current_temp:
            result = _bind(self.box_action, 'material_flush')(length, velocity, flush_temp, flush_max_temp, percent, tnn)
        else:
            result = _bind(self.box_action, 'material_flush')(length, velocity, flush_temp, flush_temp, percent, tnn)
        if result is False:
            return False
        content = _bind(self.box_state, 'get_Tnn_content')(tnn)
        _bind(self.box_action.remain_material, 'save')(
            color=_bind(self.box_state, 'get_Tn_data')(content['addr'], 'color_value', content['num']),
            type=_bind(self.box_state, 'get_Tn_data')(content['addr'], 'material_type', content['num']))
        _bind(self.gcode, 'run_script_from_command')('M104 S%d' % current_temp)
        return True


    def flush_material(self, velocity=300):
        _bind(self.box_action, 'go_to_extrude_pos')()
        if self.box_action.remain_material:
            speed = _bind(self.box_action, 'get_material_max_extrusion_speed')(type=self.box_action.remain_material.type)
            velocity = speed * 60 * 0.8 if speed else velocity
        length = self.box_action.boxcfg.Tn_extrude
        flush_list = []
        _bind(self.box_action, 'cal_flush_list')(length, flush_list)
        for index in range(len(flush_list)):
            _bind(self.box_action, 'check_and_extrude')(flush_list[index], velocity, wait=True)
            _bind(self.box_action, 'blow')()
            if self.box_action.box_save.error is not None:
                _bind(logging, 'warning')('error: %s' % self.box_action.box_save.error)
                return False
            if index != len(flush_list) - 1:
                _bind(self.box_action, 'nozzle_clean')(circle=False)
            else:
                _bind(self.box_action, 'nozzle_clean')()
        _bind(self.box_action, 'move_to_safe_pos')()


    def filament_conflict_check(self, web_request):
            print_type = _bind(web_request, 'get')('printing_method', 'cfs')
            conflict, extruder, five_way = _bind(self.box_action, 'filament_conflict_check')(print_type)
            check_result = {'conflict': conflict, 'extruder': extruder, 'five_way': five_way}
            _bind(web_request, 'send')(check_result)


    def stats(self, eventtime):
            last_cmd = self.box_action.box_save.last_cmd
            if last_cmd:
                last_Tnn_content = _bind(self.box_state, 'get_Tnn_content')(last_cmd)
                if (_bind(self.box_state, 'get_Tn_data')(last_Tnn_content['addr'], 'state') == 'connect'
                        and not self.box_action.Tn_action_flag):
                    if self.box_action.print_stats.state == 'printing':
                        _query_measuring_wheel(self.reactor, self.box_action, last_Tnn_content['addr'])
                    _watch_feed(self, eventtime, last_cmd, last_Tnn_content)
                    measuring_wheel = _bind(self.box_state, 'get_Tn_data')(last_Tnn_content['addr'], 'measuring_wheel')
                    return (False, f"box: addr={last_Tnn_content['addr']} measuring_wheel={measuring_wheel}")
            return (True, '')


    def box_filament_state_get(self, web_request):
            state = {}
            for addr in range(1, 5):
                if _bind(self.box_action, 'check_connect')(addr):
                    _bind(self.box_action, 'communication_get_filament_sensor_state')(addr, 'CONNECTIONS')
                    _bind(self.box_action, 'communication_get_filament_sensor_state')(addr, 'MATERIAL')
                    connections_status = _bind(self.box_state, 'get_Tn_inner_data')(
                        addr, 'filament_sensor', subpart='connections')
                    material_status = _bind(self.box_state, 'get_Tn_inner_data')(
                        addr, 'filament_sensor', subpart='material')
                    TN = 'T%d' % addr
                    state[TN] = {'material': material_status, 'connections': connections_status}
            _bind(web_request, 'send')(state)


    def power_loss_same_material_process(self, last_tnn, tnn, vtnn):
            Tnn_content = _bind(self.box_state, 'get_Tnn_content')(tnn)
            num_map = {'A': 0, 'B': 1, 'C': 2, 'D': 3}
            material_status = _bind(self.box_action, 'communication_get_filament_sensor_state')(
                Tnn_content['addr'], 'MATERIAL')
            material_status = _bind(self.box_state, 'get_Tn_inner_data')(
                Tnn_content['addr'], 'filament_sensor', subpart='material')
            connections_status = _bind(self.box_action, 'communication_get_filament_sensor_state')(
                Tnn_content['addr'], 'CONNECTIONS')
            connections_status = _bind(self.box_state, 'get_Tn_inner_data')(
                Tnn_content['addr'], 'filament_sensor', subpart='connections')
            mask = 1 << num_map[Tnn_content['num']]
            if material_status & connections_status & mask:
                _bind(self.box_state, 'get_Tn_data')(0, 0, uppart='filament_useup')
                _bind(self.box_action, 'communication_set_box_mode')(
                    Tnn_content['addr'], 'PRINT', num=Tnn_content['num'])
                ret = _bind(self, 'flush_material')()
                if ret is False:
                    _bind(self.box_state, 'clear_e_err')()
                    _bind(self.box_action.box_save, 'recode_err')('e_err_tn_extrude_err', tnn=tnn, vtnn=vtnn)
                    return False
                self.box_state.is_continue_print = False
                return True
            _bind(self.box_action, 'communication_set_box_mode')(
                Tnn_content['addr'], 'PRINT', num=Tnn_content['num'])
            material_type = _bind(self.box_state, 'get_tn_save_data')(
                Tnn_content['addr'], 'material_type', Tnn_content['num'])
            _bind(self.box_state, 'modify_Tn_data')(
                Tnn_content['addr'], material_type, 'material_type', num=Tnn_content['num'])
            color_value = _bind(self.box_state, 'get_tn_save_data')(
                Tnn_content['addr'], 'color_value', Tnn_content['num'])
            _bind(self.box_state, 'modify_Tn_data')(
                Tnn_content['addr'], color_value, 'color_value', num=Tnn_content['num'])
            _bind(self.box_action, 'extrusion_all_materials')()
            _bind(self.reactor, 'register_callback')(self.box_action.material_auto_refill).wait()
            if _bind(self.box_action, 'get_filament_sensor_detect')() is False:
                _bind(self.box_action.box_save, 'recode_err')(
                    'filament_err', last_tnn, tnn,
                    addr=Tnn_content['addr'], filament_sensor_enable=False)
            return True


    def empty_print_retry_process(self):
            tnn = self.box_action.box_save.error_tnn['tnn']
            if tnn is None:
                _bind(logging, 'warning')('[warning] tnn is None, error_tnn: %s' % tnn)
                _bind(self.box_action.box_save, 'clear_err')()
                return
            tnn_content = _bind(self.box_state, 'get_Tnn_content')(tnn)
            _bind(self.box_action, 'communication_set_box_mode')(tnn_content['addr'], 'IDLE')
            _bind(self.box_action, 'communication_set_box_mode')(
                tnn_content['addr'], 'PRINT', num=tnn_content['num'])
            _bind(self.box_action.box_save, 'clear_err')()
            temp = _bind(self.box_action, 'get_material_target_temp')(tnn)
            _bind(self.box_action, 'quickly_wait_heating')(temp)


    def error_clear(self):
            error_tnn = self.box_action.box_save.error_tnn
            error = self.box_action.box_save.error
            _bind(logging, 'info')('error: %s, error_tnn: %s' % (error, error_tnn))
            if error in self.box_action.box_save.cr_break_err_list:
                _bind(self, 'cr_break_err_clear')()
                return
            if self.box_action.box_save.material_auto_refill_flag:
                self.box_action.box_save.material_auto_refill_flag = False
            if error in [
                'cut_err',
                'macro_cut_err',
                'flush_err',
                'macro_flush_err',
                'print_end_move_to_cut_err',
                'z_err_extrusion_all_materials_err',
                'e_err_tn_extrude_err',
            ]:
                _bind(self.box_action.box_save, 'clear_err')()
                return
            if error in ['retrude_err', 'macro_retrude_err', 'e_err_cut_err']:
                last_tnn = error_tnn.get('last_tnn')
                if last_tnn is None:
                    addr = error_tnn.get('addr')
                    if addr:
                        ret = _bind(self.box_action, 'communication_set_box_mode')(addr, 'IDLE')
                        if ret is False:
                            return False
                else:
                    Tnn_content = _bind(self.box_state, 'get_Tnn_content')(last_tnn)
                    ret = _bind(self.box_action, 'communication_set_box_mode')(Tnn_content['addr'], 'IDLE')
                    if ret is False:
                        return False
                _bind(self.box_action.box_save, 'clear_err')()
                return
            if error in [
                'box_extrude_err',
                'extruder_extrude_err',
                'macro_box_extrude_err',
                'macro_extruder_extrude_err',
            ]:
                tnn = error_tnn.get('tnn')
                if tnn is None:
                    _bind(logging, 'warning')('[warning] tnn is None, error_tnn: %s' % error_tnn)
                    _bind(self.box_action.box_save, 'clear_err')()
                    return
                Tnn_content = _bind(self.box_state, 'get_Tnn_content')(tnn)
                _bind(self.box_action, 'communication_set_box_mode')(Tnn_content['addr'], 'IDLE')
                _bind(self.box_action.box_save, 'clear_err')()
                return
            if error == 'state_err':
                addr = error_tnn.get('addr')
                if addr is None:
                    _bind(logging, 'warning')('[error] addr: %s' % addr)
                else:
                    _bind(self.box_action, 'communication_set_box_mode')(addr, 'IDLE')
                _bind(self.box_action.box_save, 'clear_err')()
                return
            if error == 'filament_err':
                self.box_action.box_save.box_filament_err = False
                tnn = error_tnn.get('tnn')
                if tnn is None:
                    _bind(logging, 'warning')('[warning] tnn is None, error_tnn: %s' % error_tnn)
                    _bind(self.box_action.box_save, 'clear_err')()
                    return
                Tnn_content = _bind(self.box_state, 'get_Tnn_content')(tnn)
                ret = _bind(self.box_action, 'communication_set_box_mode')(Tnn_content['addr'], 'IDLE')
                if ret is False:
                    return False
                _bind(self.box_action.box_save, 'clear_err')()
                return
            if error == 'empty_print':
                tnn = error_tnn.get('tnn')
                if tnn is None:
                    _bind(logging, 'warning')('[warning] tnn is None, error_tnn: %s' % error_tnn)
                    _bind(self.box_action.box_save, 'clear_err')()
                    return
                Tnn_content = _bind(self.box_state, 'get_Tnn_content')(tnn)
                _bind(self.box_action, 'communication_set_box_mode')(Tnn_content['addr'], 'IDLE')
                _bind(self.box_action, 'communication_set_box_mode')(Tnn_content['addr'], 'PRINT', num=Tnn_content['num'])
                _bind(self.box_action.box_save, 'clear_err')()
                return
            if error == 'print_end_err':
                last_tnn = error_tnn.get('last_tnn')
                if last_tnn is None:
                    addr = error_tnn.get('addr')
                    if addr:
                        _bind(self.box_action, 'communication_set_box_mode')(addr, 'IDLE')
                    else:
                        last_cmd = self.box_action.box_save.last_cmd
                        if last_cmd:
                            Tnn_content = _bind(self.box_state, 'get_Tnn_content')(last_cmd)
                            _bind(self.box_action, 'communication_set_box_mode')(Tnn_content['addr'], 'IDLE')
                else:
                    Tnn_content = _bind(self.box_state, 'get_Tnn_content')(last_tnn)
                    _bind(self.box_action, 'communication_set_box_mode')(Tnn_content['addr'], 'IDLE')
                _bind(self.box_action.box_save, 'clear_err')()
                return
            if error == 'macro_extrusion_all_materials_err':
                last_cmd = self.box_action.box_save.last_cmd
                Tnn_content = _bind(self.box_state, 'get_Tnn_content')(last_cmd)
                _bind(self.box_action, 'communication_set_box_mode')(Tnn_content['addr'], 'IDLE')
                self.box_action.box_save.last_cmd_tmp = last_cmd
                self.box_action.box_save.last_cmd = None
                _bind(self.box_action.box_save, 'clear_err')()
                return
            if error in self.box_action.box_save.error_list or error in self.box_action.box_save.macro_error_list:
                _bind(self.box_action.box_save, 'clear_err')()
                return
            _bind(logging, 'warning')('[warning] error(%s) not in error list' % error)


    def box_start_get_connect(self, eventtime):
            num = 0
            for i in range(8):
                addr = (i & 3) + 1
                ret = _bind(self.box_action, 'communication_get_box_state')(addr)
                if ret is True:
                    if _bind(self.box_action, 'check_connect')(addr) is False:
                        _bind(self.box_state, 'modify_Tn_data')(addr, 'connect', 'state')
                        _bind(self.box_action, 'communication_set_box_mode')(addr, 'IDLE')
                if _bind(self.box_state, 'get_Tn_data')(addr, 'state') == 'connect':
                    num += 1
                if num > 0:
                    self.box_state.Tn_data['state'] = 'connect'
                    _bind(self.gcode, 'run_script_from_command')(
                        'SET_FILAMENT_SENSOR SENSOR=filament_sensor ENABLE=0')
                else:
                    self.box_state.Tn_data['state'] = 'disconnect'


    def box_start_get_rfid_and_remain_len(self, eventtime):
            for i in range(1, 5):
                if _bind(self.box_state, 'get_Tn_data')(i, 'state') == 'connect':
                    _bind(self.box_action, 'communication_get_rfid')(i, 15)
                    _bind(self.box_action, 'communication_get_remain_len')(i, 15)


    def box_end(self):
            enable = self.box_action.box_state.Tn_data['enable']
            if enable == 0:
                _bind(self.box_action, 'move_to_safe_pos')()
                return
            last_cmd = self.box_action.box_save.last_cmd
            if last_cmd is None:
                _bind(self.box_action, 'move_to_safe_pos')()
                return
            if self.box_action.is_use_ending_material:
                ret = _bind(self.box_action, 'is_use_ending_material_flush')(
                    err='print_end_err',
                    last_tnn=last_cmd)
                if ret is False:
                    return False
            elif _bind(self.box_action, 'get_filament_sensor_detect')():
                _bind(self.box_action, 'go_to_extrude_pos')()
                temp = _bind(self.box_action, 'get_material_target_temp')(
                    tnn=last_cmd,
                    type=self.box_action.remain_material.type)
                _bind(self.box_action, 'quickly_wait_heating')(temp)
                ret = _bind(self.box_action, 'move_to_cut')()
                if ret is False:
                    _bind(self.box_state, 'clear_e_err')()
                    _bind(self.box_action.box_save, 'recode_err')(
                        'print_end_move_to_cut_err')
                    return False
                _bind(self.box_action, 'go_to_extrude_pos')()
                ret = _bind(self.box_action, 'box_retrude_material')()
                if ret is False:
                    _bind(self.box_action.box_save, 'recode_err')('print_end_err', last_cmd)
                    return False
            _bind(self.box_action, 'move_to_safe_pos')()
            self.box_action.box_save.last_cmd_tmp = last_cmd
            self.box_action.box_save.last_cmd = None


    def error_resume_process(self):
            enable = self.box_action.box_state.Tn_data['enable']
            if enable == 0:
                _bind(logging, 'info')('[error_resume_process] enable = 0')
                _bind(self.box_action.box_save, 'clear_resume_tnn')()
                return
            if self.box_action.print_stats.state != 'paused':
                _bind(logging, 'info')('[error_resume_process] state is not paused')
                _bind(self.box_action.box_save, 'clear_resume_tnn')()
                return
            tnn = self.box_action.box_save.resume_tnn
            if tnn is None:
                _bind(logging, 'info')('[error_resume_process] resume_tnn is None')
                return
            sensor_detected = _bind(self.box_action, 'get_filament_sensor_detect')()
            if sensor_detected is True and self.box_action.box_save.error == 'filament_err':
                _bind(logging, 'info')('[error_resume_process] filament_err and filament_sensor detect')
                return
            _bind(self.box_action.box_save, 'clear_resume_tnn')()
            _bind(self.box_action.box_save, 'clear_err')()
            if _bind(self.box_action, 'get_filament_sensor_detect')() is True:
                _bind(logging, 'info')('[error_resume_process] filament sensor is detected')
                return
            _bind(self.box_action, 'go_to_extrude_pos')()
            ret = _bind(self.box_action, 'box_retrude_material')()
            if ret is False:
                _bind(self.box_action.box_save, 'recode_err')(
                    'retrude_err', self.box_action.box_save.last_cmd, tnn)
                _bind(self.printer, 'lookup_object')('k2_print').resume_err = True
                _bind(self.box_action, 'move_to_safe_pos')()
                _bind(self.gcode, 'run_script_from_command')('M400')
                return
            ret = _bind(self.box_action, 'box_extrude_material')(tnn)
            if ret is False:
                _bind(self.box_action.box_save, 'recode_err')('box_extrude_err', None, tnn)
                _bind(self.printer, 'lookup_object')('k2_print').resume_err = True
                _bind(self.box_action, 'move_to_safe_pos')()
                _bind(self.gcode, 'run_script_from_command')('M400')
                return
            ret = _bind(self.box_action, 'extruder_extrude')(tnn)
            if ret is False:
                _bind(self.box_action.box_save, 'recode_err')('extruder_extrude_err', None, tnn)
                _bind(self.printer, 'lookup_object')('k2_print').resume_err = True
                _bind(self.box_action, 'move_to_safe_pos')()
                _bind(self.gcode, 'run_script_from_command')('M400')
                return
            ret = _bind(self, 'material_change_flush')(None, tnn)
            if ret is False:
                _bind(self.box_action.box_save, 'recode_err')('flush_err', None, tnn)
                _bind(self.printer, 'lookup_object')('k2_print').resume_err = True
                _bind(self.box_action, 'move_to_safe_pos')()
                _bind(self.gcode, 'run_script_from_command')('M400')
                return
            return True


    def cr_break_err_clear(self):
            vtnn = self.box_action.box_save.error_tnn['vtnn']
            last_tnn = self.box_action.box_save.error_tnn['last_tnn']
            _bind(self.box_action.box_save, 'clear_err')()
            content = None
            if vtnn:
                tnn = self.box_state.Tnn_map[vtnn]
                content = _bind(self.box_state, 'get_Tnn_content')(tnn)
            elif last_tnn:
                content = _bind(self.box_state, 'get_Tnn_content')(last_tnn)
            else:
                _bind(logging, 'info')('unmarked')
                return
            _bind(self.box_action, 'communication_set_box_mode')(content['addr'], 'IDLE')


    def cr_break_err_retry_process(self):
            if self.box_action.box_save.error == 'cr_break_ending_flush_err':
                _bind(self.box_action.box_save, 'clear_err')()
                _bind(self.gcode, 'run_script_from_command')('CR_BOX_PRE_OPT')
            elif self.box_action.box_save.error == 'cr_break_cut_err':
                _bind(self.box_action.box_save, 'clear_err')()
                _bind(self.gcode, 'run_script_from_command')('CR_BOX_CUT')
            elif self.box_action.box_save.error == 'cr_break_retrude_err':
                _bind(self.box_action.box_save, 'clear_err')()
                _bind(self.gcode, 'run_script_from_command')('CR_BOX_RETRUDE')
            elif self.box_action.box_save.error == 'cr_break_box_extrude_err':
                vtnn = self.box_action.box_save.error_tnn['vtnn']
                _bind(self.box_action.box_save, 'clear_err')()
                _bind(self.gcode, 'run_script_from_command')('CR_BOX_EXTRUDE TNN=%s' % vtnn)
            elif self.box_action.box_save.error == 'cr_break_extruder_extrude_err':
                vtnn = self.box_action.box_save.error_tnn['vtnn']
                _bind(self.box_action.box_save, 'clear_err')()
                tnn = self.box_state.Tnn_map[vtnn]
                ret = _bind(self.box_action, 'extruder_extrude')(tnn)
                if ret is False:
                    _bind(self.box_action.box_save, 'recode_err')('cr_break_extruder_extrude_err', vtnn=vtnn)
                    return False
            elif self.box_action.box_save.error == 'cr_break_flush_err':
                vtnn = self.box_action.box_save.error_tnn['vtnn']
                _bind(self.box_action.box_save, 'clear_err')()
                _bind(self.gcode, 'run_script_from_command')('CR_BOX_FLUSH TNN=%s' % vtnn)
            else:
                return

    def cmd_Tn_Extrude(self, gcmd):
        _bind(self.box_action, 'Tn_Extrude')(
            self.box_action.boxcfg.Tn_extrude,
            self.box_action.boxcfg.Tn_extrude_velocity,
            self.box_action.boxcfg.Tn_extrude_temp,
            self.box_action.boxcfg.Tn_extrude_percent)

    def cmd_box_extrude_material(self, gcmd):
        if self.box_action.box_save.error is None:
            _bind(logging, 'info')('gcmd: %s' % _bind(gcmd, 'get_command_parameters')())
            tnn = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('TNN')
            _bind(logging, 'info')('tnn: %s' % tnn)
            if tnn is not None and tnn not in self.box_action.box_state.Tnn_map:
                _bind(logging, 'warning')('TNN[%s] not in Tnn gcode' % tnn)
                return
            ret = _bind(self.box_action, 'box_extrude_material')(tnn)
            if ret:
                return
            _bind(self.box_action, 'set_cool_temp')()
            _bind(self.box_action.box_save, 'recode_err')('macro_box_extrude_err', tnn=tnn)
        command = _bind(gcmd, 'get_commandline')()
        _bind(self.box_action.box_save.error_next_macro, 'append')(command)

    def cmd_box_mode_wait(self, gcmd):
        _bind(self.box_action, 'get_box_mode_and_wait')()

    def cmd_box_retrude_material(self, gcmd):
        if self.box_action.box_save.error is None:
            ret = _bind(self.box_action, 'box_retrude_material')()
            if ret:
                _bind(self.box_action, 'set_cool_temp')()
                return
            _bind(self.box_action, 'set_cool_temp')()
            _bind(self.box_action.box_save, 'recode_err')('macro_retrude_err')
        command = _bind(gcmd, 'get_commandline')()
        _bind(self.box_action.box_save.error_next_macro, 'append')(command)

    def cmd_cut_material(self, gcmd):
        ret = _bind(self.box_action, 'cut_material')()
        if not ret:
            _bind(self.box_action.box_save, 'recode_err')('macro_cut_err')
            if self.box_state.extrude_err:
                _bind(self.box_state, 'clear_e_err')()
            else:
                command = _bind(gcmd, 'get_commandline')()
                _bind(self.box_action.box_save.error_next_macro, 'append')(command)

    def cmd_extruder_extrude(self, gcmd):
        if self.box_action.box_save.error is None:
            tnn = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('TNN')
            _bind(logging, 'info')('tnn: %s' % tnn)
            if tnn is not None and tnn not in self.box_action.box_state.Tnn_map:
                _bind(logging, 'warning')('TNN[%s] not in Tnn gcode' % tnn)
                return
            ret = _bind(self.box_action, 'extruder_extrude')(tnn)
            if ret:
                return
            _bind(self.box_action, 'set_cool_temp')()
            _bind(self.box_action.box_save, 'recode_err')('macro_extruder_extrude_err', tnn=tnn)
        command = _bind(gcmd, 'get_commandline')()
        _bind(self.box_action.box_save.error_next_macro, 'append')(command)

    def cmd_go_to_extrude_pos(self, gcmd):
        _bind(self.box_action, 'go_to_extrude_pos')()

    def cmd_hall_test(self, gcmd):
        _bind(self.box_action, 'cut_hall_test')()

    def cmd_hall_zero(self, gcmd):
        _bind(self.box_action, 'cut_hall_zero')()

    def cmd_material_change_flush(self, gcmd):
        if self.box_action.box_save.error is None:
            last_tnn = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('LAST_TNN')
            tnn = _bind(_bind(gcmd, 'get_command_parameters')(), 'get')('TNN')
            if last_tnn is None:
                last_tnn = self.box_action.box_save.last_cmd_tmp
            if tnn is None:
                if self.box_action.box_save.last_cmd is None:
                    _bind(logging, 'warning')('tnn and last_cmd are None')
                tnn = self.box_action.box_save.last_cmd
            if ((tnn is not None and tnn not in self.box_action.box_state.Tnn_map)
                    or (last_tnn is not None and last_tnn not in self.box_action.box_state.Tnn_map)):
                warning = _bind(logging, 'warning')
                warning('TNN[%s] or LAST_TNN[%s] not in Tnn gcode' % (tnn, last_tnn))
                return
            ret = _bind(self, 'material_change_flush')(last_tnn, tnn)
            if ret:
                _bind(self.box_action, 'set_cool_temp')()
                return
            _bind(self.box_action, 'set_cool_temp')()
            _bind(self.box_action.box_save, 'recode_err')('macro_flush_err')
        command = _bind(gcmd, 'get_commandline')()
        _bind(self.box_action.box_save.error_next_macro, 'append')(command)

    def cmd_material_flush(self, gcmd):
        velocity = None
        percent = None
        extrude = _bind(gcmd, 'get_int')(
            'LEN', self.box_action.boxcfg.Tn_extrude, minval=0, maxval=200)
        try:
            velocity = [int(value) for value in _bind(
                _bind(_bind(_bind(gcmd, 'get_command_parameters')(), 'get')('VELOCITY'), 'strip')(),
                'split')(',')]
        except Exception as caught:
            parse_error = caught
            _bind(self.gcode, 'respond_info')('warning: get velocity failed, velocity: %s' % velocity)
            velocity = self.box_action.boxcfg.Tn_extrude_velocity
        temp = _bind(gcmd, 'get_int')(
            'TEMP', self.box_action.boxcfg.Tn_extrude_temp, minval=180, maxval=300)
        try:
            percent = [int(value) for value in _bind(
                _bind(_bind(_bind(gcmd, 'get_command_parameters')(), 'get')('PERCENT'), 'strip')(),
                'split')(',')]
        except Exception as caught:
            parse_error = caught
            _bind(self.gcode, 'respond_info')('warning: get percent failed, percent: %s' % percent)
            percent = self.box_action.boxcfg.Tn_extrude_percent
        _bind(self.box_action, 'material_flush')(extrude, velocity, temp, percent)
        ret = _bind(self.box_action, 'check_and_extrude')(-5, 360)
        if ret is False:
            _bind(self.box_state, 'clear_e_err')()

    def cmd_move_to_cut(self, gcmd):
        ret = _bind(self.box_action, 'move_to_cut')()
        if ret is False:
            _bind(self.box_state, 'clear_e_err')()

    def cmd_move_to_safe_pos(self, gcmd):
        _bind(self.box_action, 'move_to_safe_pos')()

    def cmd_nozzle_clean(self, gcmd):
        circle = _bind(gcmd, 'get_int')('CIRCLE', 1, minval=0, maxval=1)
        nozzle_clean = _bind(self.box_action, 'nozzle_clean')
        nozzle_clean(circle=_rich_ne(circle, 0))

    def cmd_power_loss_restore(self, gcmd):
        _bind(self.box_action, 'power_loss_restore')()

    def cmd_set_temp(self, gcmd):
        _bind(self.box_action, 'set_temp')()

    def cmd_save_fan(self, gcmd):
        _bind(self.box_action.box_save, 'save_fan')()

    def cmd_restore_fan(self, gcmd):
        _bind(self.box_action.box_save, 'restore_fan')()














key_nozzle_blocked_err = '!! {"code":"key845", "msg":"the nozzle is blocked", "values": []}'

key_material_err = '!! {"code":"key848", "msg":"material err, may break at connections", "values": [%d, "%s"]}'

key_retrude_err3 = '!! {"code":"key851", "msg":"retrude error, retrude but not trigger buffer empty limit", "values": []}'

key_sensor_err = '!! {"code":"key852", "msg":"check extruder filament sensor and box sensor state", "values": []}'

key_retrude_err4 = '!! {"code":"key863", "msg":"retrude error, retrude success but filament sensor is detected", "values": []}'

key_extrude_err2 = '!! {"code":"key836", "msg":"extrude error, maybe there\'s a blockage between the connections and the filament sensor", "values": [%d, "%s"]}'

key_extrude_err3 = '!! {"code":"key837", "msg":"extrude error, maybe there is a blockage between the filament sensor and the extrusion gear", "values": [%d, "%s"]}'

key_extrude_err5 = '!! {"code":"key864", "msg":"extrude error, extrude but not trigger buffer full limit", "values": []}'

key_cut_err = '!! {"code":"key841", "msg":"cut error, cut sensor not detected, cutting not rebound", "values": []}'

key_cutter_err = '!! {"code":"key856", "msg":"no cutter", "values": []}'

import math
import numpy as np
key_cut_pos_err = '!! {"code":"key855", "msg":"cut position error", "values": [%.2f]}'


BoxSave.last_cmd = _LastSlot('last_cmd')
