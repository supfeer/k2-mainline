"""K2 platform identity and service limits, independent of the probe driver."""
from collections import deque
import logging
import math
import os
import time

# Pre-generated SciPy 1.17.1 butter(2, .1, 'highpass', fs=1280, output='sos').
# The embedded host does not need SciPy to use its native drift filter.

# Keep the faster contact filter available without a runtime SciPy dependency.

MESSAGES = {
    'en': {
        'xy': 'Invalid K2 XY limits. Check the stepper_x and stepper_y ranges.',
        'z': 'Invalid K2 Z limits. Check the stepper_z range and alignment offset.',
        'offset': 'Invalid mesh or Z safety offset. Calibrate before moving Z.',
        'probe_conflict': 'PRTouch and load_cell_probe cannot share the nozzle sensor. Enable only one driver.',
        'reference_state': 'Cannot refresh the unloaded load-cell reference: calibration, idle motors and homed XYZ with at least 1.5 mm clearance are required.',
        'reference_range': 'Unloaded load-cell reference is invalid or exceeds the ADC safety range.',
        'reference_failed': 'Load-cell reference update failed; restart the firmware before probing.',
        'reference_set': 'Unloaded ADC reference: %i counts; force scale unchanged.',
        'reference_wait': 'Waiting for a stable unloaded ADC reference before probing.',
        'reference_unstable': 'The unloaded ADC reference did not stabilize; probing was not started. Inspect the sensor after the printer cools before retrying.',
        'reference_window': 'Invalid load-cell stability parameters. Check STABLE_TIME, MAX_DRIFT and TIMEOUT.',
        'motor_fault': 'Motor %s protection fault: %s. Motion stopped; inspect the driver before restarting.',
        'motor_fault_query': 'Motor %s protection signal could not be verified: %s. Motion stopped.',
        'bridge_fault': 'No reliable nozzle bridge response (%s). Motion stopped; FIRMWARE_RESTART is required.',
        'cfs_slot': 'Invalid CFS selection. Specify one slot T1A-T4D or one tool 0-15.',
        'probe_timing': 'K2 probe timing is not ready or the probe speed is invalid.',
        'z_bottom_config': 'Invalid bottom-endstop configuration. Check the two Z motors, pins, speeds and travel limits.',
        'z_bottom_state': 'Bottom homing is blocked: motors are not ready, a protection fault is active, or homing was interrupted.',
        'z_bottom_limit': 'Bottom endstops did not trigger together within the permitted travel. Motion stopped.',
        'z_bottom_stop': 'Bottom-endstop homing was stopped.',
        'motor_gpio_handoff': 'Motor GPIO handoff to the native MCU steppers failed. Motion is blocked.',
        'load_cell_start': 'CS1237 initialization failed: %s. Probing is blocked.',
        'host_scheduler': 'Cannot release the stock CPU1 restriction for UART workers: %s. Startup is blocked.',
        'reactor_cpu': 'Cannot move the Klipper reactor thread to CPU1 at nice -10: %s. Startup is blocked.',
        'uart_realtime': 'Cannot give MCU UART workers real-time priority: %s. Startup is blocked.',
        'motion_buffer': 'buffer_time must exceed step_gen_time by at least 0.5 s',
        'tap_retry': 'The contact fit failed; repeating this tap once.',
        'tap_weak': 'The probe signal has no reliable contact segment.',
        'tap_clean_retry': 'The contact fit is unreliable; cleaning the nozzle before one retry.',
        'tap_background': 'No reliable fresh unloaded probe baseline; probing was not started.',
        'tap_background_wait': 'Waiting for probe vibrations to settle before contact.',
        'init_state': 'Initial calibration requires ready idle motors and an active load_cell_probe; if Z is homed, at least 1.5 mm clearance is required.',
        'init_set': 'Initial unloaded load-cell reference saved in memory: %i counts. Run SAVE_CONFIG to persist before homing Z.',
    },
    'ru': {
        'xy': 'Некорректные пределы XY. Проверьте диапазоны stepper_x и stepper_y.',
        'z': 'Некорректные пределы Z. Проверьте диапазон stepper_z и поправку выравнивания.',
        'offset': 'Некорректная поправка сетки или безопасности Z. Выполните калибровку перед перемещением Z.',
        'probe_conflict': 'PRTouch и load_cell_probe не могут совместно использовать датчик сопла. Оставьте только один драйвер.',
        'reference_state': 'Нельзя обновить ненагруженную опору датчика: нужны калибровка, готовые неподвижные моторы, хоуминг XYZ и зазор не менее 1,5 мм.',
        'reference_range': 'Ненагруженная опора датчика некорректна или выходит за защитный диапазон ADC.',
        'reference_failed': 'Обновление опоры датчика не завершено; перезапустите прошивку перед измерениями.',
        'reference_set': 'Ненагруженная опора ADC: %i отсчётов; масштаб силы не изменён.',
        'reference_wait': 'Ожидание стабильного ненагруженного нуля ADC перед касанием.',
        'reference_unstable': 'Ненагруженный ноль ADC не стабилизировался; касание не начато. Перед повтором проверьте датчик на остывшем принтере.',
        'reference_window': 'Некорректные параметры стабильности датчика. Проверьте STABLE_TIME, MAX_DRIFT и TIMEOUT.',
        'motor_fault': 'Защита мотора %s: %s. Движение остановлено; проверьте привод перед перезапуском.',
        'motor_fault_query': 'Не удалось проверить сигнал защиты мотора %s: %s. Движение остановлено.',
        'bridge_fault': 'Нет достоверного ответа моста сопла (%s). Движение остановлено; требуется FIRMWARE_RESTART.',
        'cfs_slot': 'Неверный выбор CFS. Укажите один слот T1A-T4D или один инструмент 0-15.',
        'probe_timing': 'Тайминги датчика K2 не готовы либо задана недопустимая скорость касания.',
        'z_bottom_config': 'Некорректная настройка нижних концевиков. Проверьте два привода Z, пины, скорости и пределы хода.',
        'z_bottom_state': 'Хоуминг нижних концевиков запрещён: моторы не готовы, сработала защита либо операция прервана.',
        'z_bottom_limit': 'Нижние концевики не сработали вместе в пределах допустимого хода. Движение остановлено.',
        'z_bottom_stop': 'Хоуминг нижних концевиков остановлен.',
        'motor_gpio_handoff': 'Не удалось передать GPIO моторов штатным MCU-stepper. Движение запрещено.',
        'load_cell_start': 'Не удалось инициализировать CS1237: %s. Пробинг запрещён.',
        'host_scheduler': 'Не удалось снять штатное ограничение CPU1 для потоков UART: %s. Запуск запрещён.',
        'reactor_cpu': 'Не удалось перенести поток реактора Klipper на CPU1 с приоритетом nice -10: %s. Запуск запрещён.',
        'uart_realtime': 'Не удалось дать потокам UART MCU приоритет реального времени: %s. Запуск запрещён.',
        'motion_buffer': 'buffer_time должен превышать step_gen_time минимум на 0,5 с',
        'tap_retry': 'Расчёт касания не удался; касание в этой точке повторяется один раз.',
        'tap_weak': 'Сигнал касания не позволяет достоверно определить контакт.',
        'tap_clean_retry': 'Расчёт касания недостоверен; сопло очищается перед одним повтором.',
        'tap_background': 'Не удалось получить достоверный свежий фон датчика; касание не начато.',
        'tap_background_wait': 'Ожидание затухания колебаний датчика перед касанием.',
        'init_state': 'Для первичной калибровки нужны готовые неподвижные моторы и load_cell_probe; если Z обнулён, зазор должен быть не менее 1,5 мм.',
        'init_set': 'Первичная опора датчика сохранена в памяти: %i отсчётов. Выполните SAVE_CONFIG перед хоумингом Z.',
    },
}


class K2:
    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.tap_history = deque(maxlen=6)
        self.messages = config.getchoice('language', MESSAGES, 'ru')
        self.printer.register_event_handler('klippy:connect', self.prepare_uart_affinity)
        self.printer.register_event_handler('klippy:connect', self.prepare_uart_realtime)
        self.printer.register_event_handler('klippy:connect', self.prepare_uart_irqs)
        self.printer.register_event_handler('klippy:connect', self.prepare_reactor_cpu)
        self.drip_time = config.getfloat('drip_time', DRIP_TIME, minval=0.1, maxval=0.5)
        self.trsync_timeout = config.getfloat('trsync_timeout', TRSYNC_TIMEOUT,
                                              minval=0.025, maxval=0.1)
        self.step_gen_time = config.getfloat('step_gen_time', STEP_GEN_TIME,
                                             minval=0.45, maxval=1.5)
        self.buffer_time = config.getfloat('buffer_time', BUFFER_TIME,
                                           minval=1.0, maxval=3.0)
        if self.buffer_time < self.step_gen_time + 2. * STEP_GEN_BATCH_TIME:
            raise config.error(self.messages['motion_buffer'])
        self.printer.register_event_handler('klippy:connect', self.prepare_motion_timing)
        self.printer.register_event_handler('klippy:connect', self.prepare_sd_slices)
        if config.has_section('prtouch_v3') and config.has_section('load_cell_probe'):
            raise config.error(self.messages['probe_conflict'])
        if config.has_section('load_cell_probe'):
            from . import trigger_analog
            # Upstream supports pre-generated data for hosts without SciPy.
            trigger_analog.GeneratedSOS.setdefault(('highpass', 12800., 2), DRIFT_FILTER_SOS)
            trigger_analog.GeneratedSOS.setdefault(('lowpass', 1280. / 150., 2), BUZZ_FILTER_SOS)
            trigger_analog.GeneratedSOS.setdefault(('lowpass', 1280. / 150., 1), BUZZ_FILTER_SOS_ORDER1)
            self.printer.register_event_handler('klippy:connect', self.prepare_load_cell)
            self.printer.register_event_handler('klippy:connect', self.prepare_load_cell_idle_sleep)
            self.printer.register_event_handler('klippy:connect', self.prepare_tap_retry)
            self.printer.register_event_handler('klippy:connect', self.prepare_mesh_finalize)
            for event in ('gcode:command_error', 'klippy:shutdown', 'klippy:disconnect'):
                self.printer.register_event_handler(event, self.handle_probe_error)
        if config.getboolean('load_cell_auto_reference', False):
            if (not config.has_section('load_cell_probe')
                    or config.getsection('load_cell_probe').getfloat('sample_retract_dist', 2.)
                    < UNLOADED_MIN_Z):
                raise config.error(self.messages['reference_state'])
            self.printer.register_event_handler('homing:homing_move_begin', self.handle_probe_begin)
            self.printer.register_event_handler('klippy:connect', self.prepare_probe_settle)
        gcode = self.printer.lookup_object('gcode')
        for name, handler in (
                ('RESET_HOME_AXES_XY', self.cmd_reset_home_axes_xy),
                ('SET_LIMITS', self.cmd_set_limits),
                ('RESTORE_LIMITS', self.cmd_restore_limits),
                ('K_LOAD_CELL_INIT', self.cmd_load_cell_init),
                ('K_LOAD_CELL_REFERENCE', self.cmd_load_cell_reference),
                ('SET_Z_LIMIT', self.cmd_set_z_limit),
                ('RESTORE_Z_LIMIT', self.cmd_restore_zlimit),
                ('K_PROFILE_REACTOR', self.cmd_profile_reactor),
                ('CXSAVE_CONFIG', self.cmd_save_config_without_restart)):
            gcode.register_command(name, handler)

    def kinematics(self):
        return self.printer.lookup_object('toolhead').get_kinematics()

    def prepare_uart_affinity(self):
        if (not hasattr(os, 'sched_getaffinity') or not hasattr(os, 'sched_setaffinity')
                or os.cpu_count() != 2 or os.sched_getaffinity(0) != {1}):
            return
        try:
            # Release the inherited stock pin before any new UART worker starts.
            os.sched_setaffinity(0, {0, 1})
            for task in os.listdir('/proc/self/task'):
                try:
                    os.sched_setaffinity(int(task), {0, 1})
                    if os.sched_getaffinity(int(task)) != {0, 1}:
                        raise OSError('CPU affinity remains restricted')
                except ProcessLookupError:
                    continue
        except OSError as error:
            raise self.printer.config_error(self.messages['host_scheduler'] % error) from error
        logging.info('K2 released stock CPU1 affinity; UART workers may run on both CPUs')

    def prepare_uart_realtime(self):
        if (not hasattr(os, 'sched_setscheduler') or not hasattr(os, 'sched_setaffinity')
                or os.cpu_count() != 2):
            return
        workers = []
        try:
            # Multi-MCU trsync allows 25 ms; keep the native UART workers off the
            # CPU0 USB/UART interrupt load and ahead of the Python reactor.
            for task in os.listdir('/proc/self/task'):
                try:
                    with open('/proc/self/task/%s/comm' % task) as stream:
                        name = stream.read().strip()
                    if not name.startswith(UART_WORKER_PREFIX):
                        continue
                    tid = int(task)
                    os.sched_setaffinity(tid, {UART_WORKER_CPU})
                    os.sched_setscheduler(tid, os.SCHED_FIFO,
                                          os.sched_param(UART_WORKER_PRIORITY))
                    if (os.sched_getscheduler(tid) != os.SCHED_FIFO
                            or os.sched_getaffinity(tid) != {UART_WORKER_CPU}):
                        raise OSError('UART worker scheduling was not applied')
                    workers.append(name)
                except (ProcessLookupError, FileNotFoundError):
                    continue
            if not workers:
                raise OSError('No native UART workers found')
        except OSError as error:
            raise self.printer.config_error(self.messages['uart_realtime'] % error) from error
        logging.info('K2 UART workers run SCHED_FIFO %d on CPU%d: %s',
                     UART_WORKER_PRIORITY, UART_WORKER_CPU, ', '.join(sorted(workers)))

    def prepare_uart_irqs(self):
        ports = []
        for name, mcu in self.printer.lookup_objects('mcu'):
            helper = getattr(mcu, '_conn_helper', None)
            port = helper.get_serialport()[0] if helper is not None else ''
            if port.startswith(UART_DEVICE_PREFIX) and port[len(UART_DEVICE_PREFIX):].isdigit():
                ports.append('uart' + port[len(UART_DEVICE_PREFIX):])
        if not ports or os.cpu_count() != 2:
            return
        moved = []
        try:
            # UART receive interrupts default to CPU0 with the USB camera load.
            with open('/proc/interrupts') as stream:
                lines = stream.read().splitlines()
            for line in lines:
                fields = line.split()
                if not fields or fields[-1] not in ports or not fields[0].rstrip(':').isdigit():
                    continue
                path = '/proc/irq/%s/smp_affinity_list' % fields[0].rstrip(':')
                with open(path, 'w') as stream:
                    stream.write(str(UART_WORKER_CPU))
                with open(path) as stream:
                    if stream.read().strip() != str(UART_WORKER_CPU):
                        raise OSError('UART interrupt affinity was not applied')
                moved.append(fields[-1])
            if sorted(moved) != sorted(ports):
                raise OSError('UART interrupts not found: %s' % ', '.join(
                    sorted(set(ports) - set(moved))))
        except OSError as error:
            raise self.printer.config_error(self.messages['uart_realtime'] % error) from error
        logging.info('K2 UART interrupts on CPU%d: %s', UART_WORKER_CPU, ', '.join(sorted(moved)))

    def prepare_reactor_cpu(self):
        if not hasattr(os, 'sched_setaffinity') or os.cpu_count() != 2:
            return
        try:
            # The USB camera tasklets take about half of CPU0 and starved the
            # reactor for up to 1.5 s; stock Klipper also ran on CPU1.
            os.sched_setaffinity(0, {REACTOR_CPU})
            if os.sched_getaffinity(0) != {REACTOR_CPU}:
                raise OSError('CPU affinity was not applied')
            # Creality daemons and camera AI run at nice 0 and filled both CPUs
            # during the first layer; stock Klipper ran its reactor at nice -10.
            # The Python serial readers need the GIL to deliver MCU responses.
            tids = [os.getpid()]
            for task in os.listdir('/proc/self/task'):
                try:
                    with open('/proc/self/task/%s/comm' % task) as stream:
                        if stream.read().startswith(SERIAL_READER_PREFIX):
                            tids.append(int(task))
                except FileNotFoundError:
                    continue
            for tid in tids:
                os.setpriority(os.PRIO_PROCESS, tid, REACTOR_NICE)
                if os.getpriority(os.PRIO_PROCESS, tid) != REACTOR_NICE:
                    raise OSError('Thread priority was not applied')
        except OSError as error:
            raise self.printer.config_error(self.messages['reactor_cpu'] % error) from error
        logging.info('K2 reactor thread runs on CPU%d; %d threads at nice %d',
                     REACTOR_CPU, len(tids), REACTOR_NICE)

    def prepare_motion_timing(self):
        import mcu
        import toolhead
        from . import motion_queuing
        # Homing and probing queue steps only DRIP_TIME ahead and two MCUs must
        # confirm each other within TRSYNC_TIMEOUT. Python and UART delivery stalls
        # on this host exceed upstream's 0.1 s and 0.025 s.
        motion_queuing.DRIP_TIME = self.drip_time
        mcu.TRSYNC_TIMEOUT = self.trsync_timeout
        # Printing moves get steps only 0.45-0.7 s ahead upstream; reactor stalls
        # of 0.6 s on this host then end in "Stepper too far in past". Stock
        # Klipper queued 1-2 s; the lookahead stays well ahead of step generation.
        motion_queuing.BGFLUSH_SG_LOW_TIME = self.step_gen_time
        motion_queuing.BGFLUSH_SG_HIGH_TIME = self.step_gen_time + STEP_GEN_BATCH_TIME
        toolhead.BUFFER_TIME_HIGH = self.buffer_time
        logging.info('K2 homing drip time %.3f s, multi-MCU trsync timeout %.3f s',
                     motion_queuing.DRIP_TIME, mcu.TRSYNC_TIMEOUT)
        logging.info('K2 print step generation %.3f-%.3f s ahead, move buffer %.3f s',
                     motion_queuing.BGFLUSH_SG_LOW_TIME,
                     motion_queuing.BGFLUSH_SG_HIGH_TIME, toolhead.BUFFER_TIME_HIGH)

    def prepare_sd_slices(self):
        sdcard = self.printer.lookup_object('virtual_sdcard', None)
        if sdcard is None or isinstance(sdcard.gcode, SlicedGCode):
            return
        sdcard.gcode = SlicedGCode(sdcard.gcode, self.printer.get_reactor())
        logging.info('K2 SD print yields to the reactor every %.3f s', SD_SLICE_TIME)

    def prepare_load_cell(self):
        from . import cs1237
        sensor = self.printer.lookup_object('load_cell_probe')._load_cell.sensor
        if not isinstance(sensor, cs1237.CS1237):
            return
        if sensor.batch_bulk.is_started:
            raise self.printer.config_error(self.messages['load_cell_start'] % 'sensor already active')
        reactor = self.printer.get_reactor()
        deadline = reactor.monotonic() + 2.
        failure = None
        samples = []
        try:
            sensor._start_measurements()
            while reactor.monotonic() < deadline:
                sensor.ffreader._update_clock()
                # Cold conversion can arrive after a zero-count status response.
                if sensor.ffreader.clock_sync.chip_clock_covariance > 0.:
                    samples = sensor.ffreader.pull_samples()
                    sensor._convert_samples(samples)
                    if samples:
                        break
                reactor.pause(min(deadline, reactor.monotonic() + .02))
            else:
                raise TimeoutError('No valid clocked CS1237 samples within 2 seconds')
        except BaseException as error:
            failure = error
        finally:
            try:
                sensor._finish_measurements()
            except Exception as error:
                self.printer.invoke_shutdown(self.messages['load_cell_start'] % error)
                if failure is None:
                    failure = error
                else:
                    logging.exception('K2 CS1237 stop failed after initialization failure')
        if failure is not None:
            if not isinstance(failure, Exception):
                raise failure
            raise self.printer.config_error(self.messages['load_cell_start'] % failure) from failure
        logging.info('K2 CS1237 initialized with %d valid samples; native reader will restart', len(samples))

    def prepare_load_cell_idle_sleep(self):
        probe = self.printer.lookup_object('load_cell_probe', None)
        if probe is None:
            return
        cell = getattr(probe, '_load_cell', None)
        sensor = getattr(cell, 'sensor', None)
        if cell is None or sensor is None or not hasattr(sensor, 'batch_bulk'):
            return
        reactor = self.printer.get_reactor()
        mcu = sensor.get_mcu()
        orig_event = cell._sensor_data_event
        orig_add_client = cell.clients.add_client
        state = {'active_until': reactor.monotonic() + LOAD_CELL_IDLE_TIMEOUT}

        def has_external_clients():
            return any(cb != cell._track_force for cb in cell.clients.client_cbs)

        def gated_sensor_event(msg):
            result = orig_event(msg)
            if not result or mcu.is_fileoutput():
                return result
            now = reactor.monotonic()
            if has_external_clients():
                state['active_until'] = now + LOAD_CELL_IDLE_TIMEOUT
                return True
            if now >= state['active_until']:
                logging.debug('K2 load cell entering idle sleep')
                return False
            return True

        def waking_add_client(callback):
            if callback != cell._track_force:
                state['active_until'] = reactor.monotonic() + LOAD_CELL_IDLE_TIMEOUT
                if gated_sensor_event not in sensor.batch_bulk.client_cbs:
                    sensor.add_client(gated_sensor_event)
                elif not sensor.batch_bulk.is_started:
                    sensor.batch_bulk._start()
            return orig_add_client(callback)

        cell._sensor_data_event = gated_sensor_event
        cell.clients.add_client = waking_add_client
        self._load_cell_idle_state = state
        logging.info('K2 load cell idle sleep armed (%.1f s timeout)', LOAD_CELL_IDLE_TIMEOUT)

    def prepare_tap_retry(self):
        # Contact relaxes before retraction; fit the captured descent instead.
        tapping = self.printer.lookup_object('load_cell_probe')._tapping_move
        analyze, run_tap = tapping._analyze_ascent, tapping.run_tap
        error = self.printer.command_error
        find_best_fit = tapping._best_fit.find_best_fit

        def checked_fit(data):
            result = find_best_fit(data)
            if self.tap_history:
                self.tap_history[-1].update(fit=result, fit_samples=data)
            if tapping._load_cell_probing_move._mcu.is_fileoutput():
                return result
            contact, below, above, slope = result
            if below < 3 or above < 3:
                return result
            signal, noise = tap_fit_quality(data, contact, slope)
            logging.info('K2 tap fit: contact=%.6f slope=%.3f signal=%.3f noise=%.3f',
                         contact, slope, signal, noise)
            # A fit to unloaded noise can have enough points on both sides.
            if (not all(math.isfinite(v) for v in (contact, slope, signal, noise))
                    or slope >= 0. or signal <= 0. or signal < 6. * noise):
                raise error(self.messages['tap_weak'])
            return result

        def analyze_ascent(gcmd, samples, ascent_start_time, toolhead, raw_z):
            if tapping._load_cell_probing_move._mcu.is_fileoutput():
                return analyze(gcmd, samples, ascent_start_time, toolhead, raw_z)
            from .load_cell_probe import _lookup_z_pos
            trigger_time = tapping._load_cell_probing_move._mcu_trigger_analog.get_last_trigger_time()
            logging.info('K2 tap timing: trigger=%.6f ascent=%.6f hold=%.6f raw_z=%.6f',
                         trigger_time, ascent_start_time, ascent_start_time - trigger_time, raw_z)
            self.tap_history.append({
                'start': ascent_start_time, 'trigger': trigger_time, 'raw_z': raw_z,
                'phase': 'descent',
                'samples': [sample for sample in samples
                            if trigger_time - .2 <= sample[0] <= ascent_start_time + .4]})
            try:
                try:
                    if trigger_time - DESCENT_WINDOW <= toolhead.motion_queuing.clear_history_time:
                        raise ValueError('Probe step history expired')
                    data = descent_samples(samples, trigger_time,
                                           lambda when: _lookup_z_pos(toolhead, when))
                except ValueError as failure:
                    logging.warning('K2 descent data rejected: %s', failure)
                    raise error(self.messages['tap_weak']) from failure
                contact, below, above, slope = checked_fit(data)
                if below < 3 or above < 3:
                    raise error(self.messages['tap_weak'])
                logging.info('K2 descent contact: z=%.6f trigger_z=%.6f samples=%d',
                             contact, _lookup_z_pos(toolhead, trigger_time), len(data))
                return contact
            except error:
                self.log_failed_ascent(samples, ascent_start_time, toolhead, raw_z)
                raise

        def retrying_run_tap(gcmd):
            tapping._is_last_result_valid = False
            try:
                return run_tap(gcmd)
            except error as failure:
                if ((str(failure) == self.messages['tap_weak']
                     or str(failure).startswith('Insufficient ascent samples'))
                        and self.printer.lookup_object('gcode_macro _K_PROBE_CLEAN_RETRY', None) is not None):
                    logging.warning('K2 tap fit failed (%s); cleaning before one retry', failure)
                    gcmd.respond_info(self.messages['tap_clean_retry'])
                    self.printer.lookup_object('gcode').run_script_from_command(
                        '_K_PROBE_CLEAN_RETRY')
                elif str(failure).startswith('Insufficient ascent samples'):
                    logging.warning('K2 tap fit failed (%s); repeating the tap once', failure)
                    gcmd.respond_info(self.messages['tap_retry'])
                else:
                    raise
            return run_tap(gcmd)
        tapping._analyze_ascent = analyze_ascent
        tapping._best_fit.find_best_fit = checked_fit
        tapping.run_tap = retrying_run_tap

    def prepare_probe_settle(self):
        move = self.printer.lookup_object('load_cell_probe')._tapping_move._load_cell_probing_move
        probing_move = move.probing_move
        start_collector = move._start_collector
        trigger = move._mcu_trigger_analog
        home_start = trigger.home_start

        def settled_probing_move(gcmd):
            if move._mcu.is_fileoutput():
                return probing_move(gcmd)
            accepted = []

            def tare_collector():
                # Only the native tare collector is checked; contact collection is unchanged.
                move._start_collector = start_collector
                collector = start_collector()
                collect_min = collector.collect_min
                collector.collect_min = lambda count: self.collect_probe_tare(
                    gcmd, move, collector, collect_min, count, accepted)
                return collector

            def checked_home_start(print_time, *args, **kwargs):
                cancellation = self.printer.lookup_object('k2_cancel', None)
                if cancellation is not None:
                    cancellation.check()
                if not accepted:
                    raise gcmd.error(self.messages['tap_background'])
                window = accepted[0]
                horizon = print_time - window['mean_time'] + self.probe_approach_time(move, gcmd)
                budget = window['noise'] + abs(window['slope']) * horizon
                limit = .75 * move._config_helper.get_trigger_force_grams(gcmd) * move._load_cell.get_counts_per_gram()
                logging.info('K2 probe tare arm: time=%.6f age=%.6f horizon=%.6f budget=%.1f limit=%.1f',
                             print_time, print_time - window['last_time'], horizon, budget, limit)
                if (not math.isfinite(print_time) or not math.isfinite(budget)
                        or not window['last_time'] <= print_time <= window['arm_deadline']
                        or horizon < 0. or budget > limit):
                    raise gcmd.error(self.messages['tap_background'])
                return home_start(print_time, *args, **kwargs)

            move._start_collector = tare_collector
            trigger.home_start = checked_home_start
            try:
                return probing_move(gcmd)
            finally:
                move._start_collector = start_collector
                trigger.home_start = home_start
        move.probing_move = settled_probing_move

    def probe_approach_time(self, move, gcmd):
        head = self.printer.lookup_object('toolhead')
        kin = head.get_kinematics()
        speeds = (move._param_helper.get_probe_params(gcmd)['probe_speed'],
                  head.max_velocity, kin.max_z_velocity)
        accelerations = (head.max_accel, kin.max_z_accel)
        distance = head.get_position()[2] - move._z_min_position
        if not all(math.isfinite(v) and v > 0. for v in speeds + accelerations + (distance,)):
            raise gcmd.error(self.messages['probe_timing'])
        speed, accel = min(speeds), min(accelerations)
        return distance / speed + speed / accel + .001

    def collect_probe_tare(self, gcmd, move, collector, collect_min, count, accepted):
        from .load_cell_probe import check_sensor_errors
        from toolhead import BUFFER_TIME_START
        reactor = self.printer.get_reactor()
        head = self.printer.lookup_object('toolhead')
        cell = move._load_cell
        rate = cell.get_sensor().get_samples_per_second()
        count = max(count, math.ceil(.3 * rate))
        started = reactor.monotonic()
        attempts = 0
        announced = False
        try:
            while reactor.monotonic() - started < 90.:
                cancellation = self.printer.lookup_object('k2_cancel', None)
                if cancellation is not None:
                    cancellation.check()
                results = collect_min(count)
                samples = check_sensor_errors(results, self.printer)
                if len(samples) < count:
                    raise gcmd.error(self.messages['tap_background'])
                self.tap_history.append({'phase': 'tare', 'samples': samples[-512:]})
                if cancellation is not None:
                    cancellation.check()
                self.unloaded_probe(gcmd.error, native_tare=True)
                try:
                    window = probe_tare_window(samples, rate, cell.saturation_range())
                except (TypeError, ValueError, OverflowError) as failure:
                    logging.warning('K2 invalid probe tare: %s', failure)
                    raise gcmd.error(self.messages['tap_background']) from failure
                now = reactor.monotonic()
                print_now = move._mcu.estimated_print_time(now)
                arm_time = max(head.print_time, print_now + BUFFER_TIME_START,
                               head.motion_queuing.calc_step_gen_restart(print_now))
                # Reserve another queue priming interval for the native arm callbacks.
                horizon = arm_time + BUFFER_TIME_START - window['mean_time'] + self.probe_approach_time(move, gcmd)
                budget = window['noise'] + abs(window['slope']) * horizon
                limit = .75 * move._config_helper.get_trigger_force_grams(gcmd) * cell.get_counts_per_gram()
                attempts += 1
                age = print_now - window['last_time']
                logging.info('K2 probe tare window: time=%.6f elapsed=%.3f attempt=%d samples=%d mean_time=%.6f '
                             'age=%.6f mean=%.1f slope=%.1f noise=%.1f horizon=%.6f budget=%.1f limit=%.1f',
                             now, now - started, attempts, len(samples), window['mean_time'], age,
                             window['mean_raw'], window['slope'], window['noise'], horizon, budget, limit)
                if -.01 <= age <= .25 and horizon > 0. and budget <= limit:
                    window['arm_deadline'] = arm_time + BUFFER_TIME_START
                    accepted.append(window)
                    return results
                if not announced and now - started >= 1.:
                    gcmd.respond_info(self.messages['tap_background_wait'])
                    announced = True
                # collect_min resets its cursor; a retry must discard older ADC batches.
                collector.start_collecting(min_time=max(print_now, window['last_time'] + 1.e-9))
            raise gcmd.error(self.messages['tap_background'])
        finally:
            collector.stop_collecting()
            callbacks = cell.clients.client_cbs
            if collector._on_samples in callbacks:
                callbacks.remove(collector._on_samples)

    def prepare_mesh_finalize(self):
        mesh = self.printer.lookup_object('bed_mesh', None)
        if mesh is None:
            return
        helper = mesh.bmc.probe_mgr.probe_helper
        finalize = helper.finalize_callback

        def finalize_after_moves(positions):
            # Mesh calculation can outlast the final probe lift's step deadline.
            self.printer.lookup_object('toolhead').wait_moves()
            return finalize(positions)
        helper.finalize_callback = finalize_after_moves

    def log_failed_ascent(self, samples, ascent_start_time, toolhead, raw_z):
        try:
            from .load_cell_probe import _lookup_z_pos
            window = [(round(sample[0] - ascent_start_time, 5), round(sample[1], 1),
                       round(_lookup_z_pos(toolhead, sample[0]), 4))
                      for sample in samples
                      if ascent_start_time - .5 <= sample[0] <= ascent_start_time + .4]
            logging.info('K2 failed tap: raw_z=%.4f ascent_start=%.4f samples=%d first=%.4f'
                         ' last=%.4f window(t,force,z)=%s', raw_z, ascent_start_time,
                         len(samples), samples[0][0] - ascent_start_time if samples else 0.,
                         samples[-1][0] - ascent_start_time if samples else 0., window)
        except Exception:
            logging.exception('K2 failed tap data unavailable')

    def handle_probe_error(self, *args):
        if self.tap_history:
            logging.info('K2 recent taps at failure: %s', list(self.tap_history))
            self.tap_history.clear()
        probe = self.printer.lookup_object('load_cell_probe', None)
        if probe is None:
            return
        tapping = getattr(probe, '_tapping_move', None)
        if tapping is not None:
            tapping._is_last_result_valid = False
        from . import load_cell
        # Failed upstream taps can abandon an unbounded sample collector.
        active = False
        for callback in list(probe._load_cell.clients.client_cbs):
            collector = getattr(callback, '__self__', None)
            if isinstance(collector, load_cell.LoadCellSampleCollector):
                active |= collector.is_started
                samples, errors = collector.stop_collecting()
                if samples:
                    logging.info('K2 failed probe raw: count=%d errors=%s tail=%s',
                                 len(samples), errors, samples[-1280:])
        if active:
            try:
                for name, mcu in self.printer.lookup_objects('mcu'):
                    logging.info('K2 failed probe MCU trace %s:\n%s', name,
                                 mcu._serial.dump_debug())
            except Exception:
                logging.exception('K2 failed probe MCU trace unavailable')

    def _check_init_state(self, error_factory):
        probe = self.printer.lookup_object('load_cell_probe', None)
        motor = self.printer.lookup_object('motor_control', None)
        if (probe is None or self.printer.is_shutdown()
                or motor is None or not motor.is_ready or motor.is_homing
                or any(motor.motor_error_code.values())):
            raise error_factory(self.messages['init_state'])
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.wait_moves()
        status = toolhead.get_status(self.printer.get_reactor().monotonic())
        position = toolhead.get_position()[2]
        if 'z' in set(status['homed_axes']):
            max_z = status['axis_maximum'][2] + BOTTOM_REF_MARGIN
            if not math.isfinite(position) or not UNLOADED_MIN_Z <= position <= max_z:
                raise error_factory(self.messages['init_state'])
        return probe

    def _wait_adc_baseline(self, gcmd, check_fn, stable_time, max_drift, timeout):
        reactor = self.printer.get_reactor()
        started = reactor.monotonic()
        deadline = started + timeout
        history = deque()
        first = None
        gcmd.respond_info(self.messages['reference_wait'])
        while reactor.monotonic() < deadline:
            cancellation = self.printer.lookup_object('k2_cancel', None)
            if cancellation is not None:
                cancellation.check()
            cell = check_fn(gcmd.error)._load_cell
            baseline = cell.avg_counts()
            now = reactor.monotonic()
            if isinstance(baseline, bool) or not math.isfinite(baseline):
                raise gcmd.error(self.messages['reference_range'])
            if first is None:
                first = baseline
            history.append((now, baseline))
            while len(history) > 2 and history[1][0] <= now - stable_time:
                history.popleft()
            if now > deadline:
                break
            spread = max(point[1] for point in history) - min(point[1] for point in history)
            if now - history[0][0] >= stable_time and spread <= max_drift * stable_time:
                logging.info('K2 probe settled: time=%.6f elapsed=%.3f first=%.1f last=%.1f spread=%.1f',
                             now, now - started, first, baseline, spread)
                return baseline
        logging.warning('K2 probe settle timeout: elapsed=%.3f first=%s recent=%s',
                        reactor.monotonic() - started, first, list(history))
        raise gcmd.error(self.messages['reference_unstable'])

    def cmd_load_cell_init(self, gcmd):
        stable_time = gcmd.get_float('STABLE_TIME', 5., minval=0., maxval=120.)
        max_drift = gcmd.get_float('MAX_DRIFT', 100., above=0.)
        min_timeout = max(stable_time + 2., 5.)
        timeout = gcmd.get_float('TIMEOUT', max(60., min_timeout), minval=min_timeout, maxval=600.)
        if not all(math.isfinite(value) for value in (stable_time, max_drift, timeout)):
            raise gcmd.error(self.messages['reference_window'])
        probe = self._check_init_state(gcmd.error)
        if stable_time > 0.:
            baseline = self._wait_adc_baseline(
                gcmd, self._check_init_state, stable_time, max_drift, timeout)
        else:
            baseline = probe._load_cell.avg_counts()
        cell = probe._load_cell
        default_cpg = cell.counts_per_gram if cell.counts_per_gram is not None else 100.
        counts_per_gram = gcmd.get_float('COUNTS_PER_GRAM', default_cpg, minval=1.)
        safety_limit = probe._tapping_move._config_helper.get_safety_limit_grams()
        band = counts_per_gram * safety_limit
        adc_min, adc_max = cell.saturation_range()
        if (isinstance(baseline, bool) or not math.isfinite(baseline)
                or not math.isfinite(counts_per_gram)
                or not math.isfinite(band) or band <= 0
                or not adc_min + band < baseline < adc_max - band):
            raise gcmd.error(self.messages['reference_range'])
        ref = int(round(baseline))
        cell.set_calibration(counts_per_gram, ref)
        cell.tare(ref)
        gcmd.respond_info(self.messages['init_set'] % ref)

    def cmd_load_cell_reference(self, gcmd):
        stable_time = gcmd.get_float('STABLE_TIME', 0., minval=0., maxval=120.)
        if not math.isfinite(stable_time) or stable_time < 0:
            raise gcmd.error(self.messages['reference_window'])
        baseline = None
        if stable_time:
            max_drift = gcmd.get_float('MAX_DRIFT', 100., above=0.)
            timeout = gcmd.get_float('TIMEOUT', 240., above=stable_time, maxval=600.)
            if (not all(math.isfinite(value) for value in (max_drift, timeout))
                    or max_drift <= 0 or timeout < stable_time + 2.):
                raise gcmd.error(self.messages['reference_window'])
            baseline = self.wait_load_cell_reference(gcmd, stable_time, max_drift, timeout)
        reference = self.update_load_cell_reference(gcmd.error, baseline)
        gcmd.respond_info(self.messages['reference_set'] % reference)

    def wait_load_cell_reference(self, gcmd, stable_time, max_drift, timeout):
        self.unloaded_probe(gcmd.error)
        head = self.printer.lookup_object('toolhead')
        logging.info('K2 probe settle begin: time=%.6f pos=%s',
                     self.printer.get_reactor().monotonic(), head.get_position()[:3])
        return self._wait_adc_baseline(
            gcmd, self.unloaded_probe, stable_time, max_drift, timeout)

    def handle_probe_begin(self, move):
        probe = self.printer.lookup_object('load_cell_probe')
        trigger = probe._mcu_trigger_analog
        if trigger not in move.get_mcu_endstops():
            return
        cell = probe._load_cell
        previous = cell.reference_tare_counts
        low, high = trigger._raw_min, trigger._raw_max
        if (cell.tare_counts is None or type(previous) not in (int, float)
                or not all(math.isfinite(value) for value in (previous, low, high)) or low >= high):
            raise self.printer.command_error(self.messages['reference_state'])
        # Native probing has already tared and selected any command-specific force limit.
        reference = self.update_load_cell_reference(
            self.printer.command_error, cell.tare_counts, native_tare=True)
        logging.info('K2 probe armed tare: time=%.6f pos=%s tare=%d previous=%d',
                     self.printer.get_reactor().monotonic(),
                     self.printer.lookup_object('toolhead').get_position()[:3], reference, previous)
        delta = reference - previous
        try:
            adc_min, adc_max = cell.saturation_range()
            if not adc_min <= low + delta < high + delta <= adc_max:
                raise ValueError('Shifted raw range exceeds the ADC rails')
            trigger.set_raw_range(low + delta, high + delta)
        except Exception as error:
            self.printer.invoke_shutdown(self.messages['reference_failed'])
            raise self.printer.command_error(self.messages['reference_failed']) from error

    def unloaded_probe(self, error_factory, native_tare=False):
        probe = self.printer.lookup_object('load_cell_probe', None)
        motor = self.printer.lookup_object('motor_control', None)
        if (probe is None or not probe._load_cell.is_calibrated() or self.printer.is_shutdown()
                or motor is None or not motor.is_ready or motor.is_homing
                or any(motor.motor_error_code.values())):
            raise error_factory(self.messages['reference_state'])
        toolhead = self.printer.lookup_object('toolhead')
        if native_tare:
            # Native tare already waited for motion; a second drain ages its zero.
            print_time, estimated, empty = toolhead.check_busy(self.printer.get_reactor().monotonic())
            if not empty or (print_time > estimated and not toolhead.special_queuing_state):
                raise error_factory(self.messages['reference_state'])
        else:
            toolhead.wait_moves()
        status = toolhead.get_status(self.printer.get_reactor().monotonic())
        position = toolhead.get_position()[2]
        if (not set('xyz') <= set(status['homed_axes']) or not math.isfinite(position)
                or not UNLOADED_MIN_Z <= position <= status['axis_maximum'][2]):
            raise error_factory(self.messages['reference_state'])
        return probe

    def update_load_cell_reference(self, error_factory, baseline=None, native_tare=False):
        probe = self.unloaded_probe(error_factory, native_tare=native_tare)
        cell = probe._load_cell
        low, high = probe._tapping_move._config_helper.get_safety_range()
        band = (high - low) / 2.
        baseline = cell.avg_counts() if baseline is None else baseline
        adc_min, adc_max = cell.saturation_range()
        if (isinstance(baseline, bool) or not math.isfinite(baseline)
                or not math.isfinite(band) or band <= 0
                or not adc_min + band < baseline < adc_max - band):
            raise error_factory(self.messages['reference_range'])
        previous = (cell.reference_tare_counts, cell.tare_counts)
        try:
            cell.reference_tare_counts = int(baseline)
            cell.tare(baseline)
            self.printer.send_event('load_cell:calibrate', cell)
        except Exception as error:
            cell.reference_tare_counts, cell.tare_counts = previous
            self.printer.invoke_shutdown(self.messages['reference_failed'])
            raise error_factory(self.messages['reference_failed']) from error
        return cell.reference_tare_counts

    def cmd_profile_reactor(self, gcmd):
        duration = gcmd.get_float('DURATION', 20., minval=1., maxval=300.)
        count = gcmd.get_int('TOP', 15, minval=1, maxval=50)
        reactor = self.printer.get_reactor()
        stats = {}

        def wrap(name, func):
            def wrapper(*args, **kwargs):
                start = time.perf_counter()
                try:
                    return func(*args, **kwargs)
                finally:
                    elapsed = time.perf_counter() - start
                    entry = stats.setdefault(name, [0, 0., 0.])
                    entry[0] += 1
                    entry[1] += elapsed
                    entry[2] = max(entry[2], elapsed)
            return wrapper
        # Timers that pause the reactor also count their paused time.
        restore = []
        for timer in list(reactor._timers):
            callback = timer.callback
            timer.callback = wrap('timer ' + getattr(callback, '__qualname__', repr(callback)),
                                  callback)
            restore.append((timer, 'callback', callback, True))
        for name, obj in self.printer.lookup_objects():
            method = getattr(obj, 'get_status', None)
            if not callable(method):
                continue
            own = 'get_status' in getattr(obj, '__dict__', {})
            try:
                obj.get_status = wrap('status ' + name, method)
            except AttributeError:
                continue
            restore.append((obj, 'get_status', method, own))
        try:
            reactor.pause(reactor.monotonic() + duration)
        finally:
            for obj, attr, original, own in reversed(restore):
                if own:
                    setattr(obj, attr, original)
                else:
                    delattr(obj, attr)
        rows = sorted(stats.items(), key=lambda item: -item[1][1])[:count]
        lines = ['%-58s n=%5d total=%7.1f ms max=%6.1f ms' % (
            name[:58], calls, total * 1000., peak * 1000.)
            for name, (calls, total, peak) in rows]
        logging.info('K2 reactor profile over %.0f s:\n%s', duration, '\n'.join(lines))
        gcmd.respond_info('\n'.join(lines))

    def cmd_save_config_without_restart(self, gcmd):
        # Stock callers (cutter calibration, auto_addr) apply the value live
        # and keep running after the save; an upstream restart here would kill
        # the calling command mid-sequence.
        autosave = self.printer.lookup_object('configfile').autosave
        gcode = self.printer.lookup_object('gcode')
        had_own = 'request_restart' in gcode.__dict__
        previous = gcode.__dict__.get('request_restart')
        gcode.request_restart = lambda result: None
        try:
            autosave.cmd_SAVE_CONFIG(gcmd)
        finally:
            if had_own:
                gcode.request_restart = previous
            else:
                del gcode.request_restart
        autosave.status_save_pending = {}
        autosave.save_config_pending = False

    def cmd_reset_home_axes_xy(self, gcmd):
        self.kinematics().note_xy_not_homed()

    def cmd_set_limits(self, gcmd):
        bounds = []
        for axis, low, high in (('x', -12., 356.), ('y', -10., 400.)):
            section = self.config.getsection('stepper_' + axis)
            bounds.extend((section.getfloat('position_min', low),
                           section.getfloat('position_max', high)))
        if (not all(math.isfinite(value) for value in bounds)
                or bounds[0] >= bounds[1] or bounds[2] >= bounds[3]):
            raise gcmd.error(self.messages['xy'])
        self.kinematics().set_limits(*bounds)

    def cmd_restore_limits(self, gcmd):
        self.kinematics().restore_limits()

    def cmd_set_z_limit(self, gcmd):
        align = self.printer.lookup_object('z_align', None)
        gap = align.zmax_safe_pox_diff if align is not None else 0.
        mesh = self.printer.lookup_object('bed_mesh', None)
        fade = mesh.fade_target if mesh is not None else 0.
        fade = abs(fade) if isinstance(fade, (int, float)) else 0.
        if not math.isfinite(fade) or not math.isfinite(gap):
            raise gcmd.error(self.messages['offset'])
        if fade > 5:
            fade = 0.
        self.set_z_bounds(gcmd, -gap + fade + .2)

    def cmd_restore_zlimit(self, gcmd):
        self.set_z_bounds(gcmd, 0.)

    def set_z_bounds(self, gcmd, adjustment):
        section = self.config.getsection('stepper_z')
        minimum = section.getfloat('position_min', -10.)
        maximum = section.getfloat('position_max', 360.) + adjustment
        if (not math.isfinite(minimum) or not math.isfinite(maximum)
                or minimum >= maximum):
            raise gcmd.error(self.messages['z'])
        self.kinematics().set_z_limit(minimum, maximum)


class SlicedGCode:
    # virtual_sdcard yields only after each 8 KB block. First-layer arcs took
    # 6.5 ms per G1 here, so one block held the reactor for over a second and
    # step generation fell behind ("Stepper too far in past").
    def __init__(self, gcode, reactor):
        self._gcode = gcode
        self._reactor = reactor
        self._slice_start = reactor.monotonic()

    def __getattr__(self, name):
        return getattr(self._gcode, name)

    def run_script(self, script):
        reactor = self._reactor
        if reactor.monotonic() - self._slice_start >= SD_SLICE_TIME:
            reactor.pause(reactor.NOW)
            self._slice_start = reactor.monotonic()
        return self._gcode.run_script(script)


DESCENT_WINDOW = .15
DESCENT_SAMPLE_GAP = 4. / 1280.


def probe_tare_window(samples, sample_rate, raw_range):
    if len(samples) < 3 or not math.isfinite(sample_rate) or sample_rate <= 0.:
        raise ValueError('Incomplete probe tare window')
    times = [row[0] for row in samples]
    raw = [row[2] for row in samples]
    if (any(isinstance(v, bool) or not math.isfinite(v) for v in times + raw)
            or any(not raw_range[0] < value < raw_range[1] for value in raw)
            or any(not 0. < right - left <= 4. / sample_rate + 1.e-9
                   for left, right in zip(times, times[1:]))):
        raise ValueError('Invalid probe tare samples')
    span = times[-1] - times[0]
    expected_span = (len(times) - 1) / sample_rate
    if not .9 * expected_span <= span <= 1.2 * expected_span or max(raw) == min(raw):
        raise ValueError('Stalled ADC values or invalid probe sample clock')
    relative = [value - times[0] for value in times]
    mean_time = sum(relative) / len(relative)
    mean_raw = sum(raw) / len(raw)
    offsets = [value - mean_time for value in relative]
    slope = sum(t * (value - mean_raw) for t, value in zip(offsets, raw)) / sum(t * t for t in offsets)
    b0, b1, _, _, a1, _ = BUZZ_FILTER_SOS_ORDER1[0]
    previous = filtered = energy = peak = 0.
    for offset, value in zip(offsets, raw):
        residual = value - mean_raw - slope * offset
        filtered = b0 * residual + b1 * previous - a1 * filtered
        previous = residual
        energy += filtered * filtered
        peak = max(peak, abs(filtered))
    noise = max(peak, 4. * math.sqrt(energy / len(raw)))
    if not all(math.isfinite(value) for value in (slope, noise, mean_raw)):
        raise ValueError('Nonfinite probe tare fit')
    return {'mean_time': times[0] + mean_time, 'last_time': times[-1],
            'mean_raw': mean_raw, 'slope': slope, 'noise': noise}


def descent_samples(samples, trigger_time, position_at):
    if not math.isfinite(trigger_time) or any(not math.isfinite(s[0]) for s in samples):
        raise ValueError('Invalid probe timestamps')
    start = trigger_time - DESCENT_WINDOW
    window = [sample for sample in samples if start <= sample[0] <= trigger_time]
    if (len(window) < 6 or window[0][0] - start > DESCENT_SAMPLE_GAP + 1.e-9
            or trigger_time - window[-1][0] > DESCENT_SAMPLE_GAP + 1.e-9):
        raise ValueError('Incomplete probe descent window')
    data = []
    for index, sample in enumerate(window):
        if index and not 0 < sample[0] - window[index - 1][0] <= DESCENT_SAMPLE_GAP + 1.e-9:
            raise ValueError('Discontinuous probe descent samples')
        force, z = sample[1], position_at(sample[0])
        if not math.isfinite(force) or not math.isfinite(z):
            raise ValueError('Invalid probe descent values')
        if data and z > data[-1][1] + 1.e-9:
            raise ValueError('Probe step history reverses before the trigger')
        data.append((force, z))
    if data[0][1] <= data[-1][1]:
        raise ValueError('Probe step history has no descent')
    # LCBestFit searches increasing Z; sorting would conceal a corrupt trajectory.
    return data[::-1]


def tap_fit_quality(data, contact, slope):
    residuals = [force - slope * min(z - contact, 0.) for force, z in data]
    baseline = sum(residuals) / len(residuals)
    noise = math.sqrt(sum((value - baseline) ** 2 for value in residuals) / len(residuals))
    return -slope * (contact - data[0][1]), noise


def load_config(config):
    return K2(config)

# Lowest Z where a reference refresh may start: homed contacts sit at about 0 and
# hot optical-frame contacts measured 0.1-0.65 mm.
UNLOADED_MIN_Z = 1.5
BOTTOM_REF_MARGIN = 2.0
DRIP_TIME = 0.35
TRSYNC_TIMEOUT = 0.05
STEP_GEN_TIME = 1.0
STEP_GEN_BATCH_TIME = 0.25
BUFFER_TIME = 2.0
SD_SLICE_TIME = 0.1
LOAD_CELL_IDLE_TIMEOUT = 10.0

# Below the Linux MCU (SCHED_FIFO 49), above Wi-Fi driver threads (1).
UART_WORKER_PREFIX = 'serialq '
UART_WORKER_CPU = 1
REACTOR_CPU = 1
REACTOR_NICE = -10
SERIAL_READER_PREFIX = 'serialhdl'
UART_DEVICE_PREFIX = '/dev/ttyS'
UART_WORKER_PRIORITY = 10

# Data generated for the standard DigitalFilter lookup, not a replacement implementation.
DRIFT_FILTER_SOS = [[0.99965296000277, -1.99930592000554, 0.99965296000277, 1.0, -1.9993057995687766, 0.9993060404423032]]
BUZZ_FILTER_SOS = [[0.08782128175913714, 0.1756425635182743, 0.08782128175913714, 1., -1.004772209703067, 0.35605733673961537]]
BUZZ_FILTER_SOS_ORDER1 = [[0.27836524305456783, 0.27836524305456783, 0., 1., -0.44326951389086444, 0.]]
