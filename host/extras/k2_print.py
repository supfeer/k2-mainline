"""Keep K2 driver metadata outside the upstream virtual-SD worker."""
import json
import logging
import math
import os
import sys

MESSAGES = {
    'en': {
        'recovery': 'Creality power-loss recovery is unavailable with upstream SD. Start a new job or use the previous firmware.',
        'first_layer': 'Creality first-layer mode is unavailable. Use an explicit PAUSE in the slicer.',
        'motor': 'Motor initialization is incomplete. Wait for motor_ready before starting the job.',
        'file': 'Cannot select the G-code file. Select an existing file from the configured directory.',
        'metadata': 'Cannot read valid CFS metadata. Check the file before starting the job.',
        'filter': 'This file requires Creality command filtering. Use the previous firmware until this mode is ported.',
        'changed': 'The G-code file changed after selection. Select it again before resuming.',
        'temperature': 'Invalid print temperature. Pass finite positive BED_TEMP and EXTRUDER_TEMP values.',
        'manual_material': 'Cannot change filament until printing is paused. Pause the job before trying again.',
        'resume': 'CFS recovery failed. Printing remains paused; resolve the feed error before resuming.',
        'feed_watch': 'CFS feed monitoring failed. Printing has been paused; check klippy.log before resuming.',
        'cfs_start': 'CFS did not acknowledge print preparation. Preparation has been stopped.',
        'cfs_end': 'CFS did not acknowledge print completion. Check the feed state and error log.',
        'cfs_manual': 'CFS manual extrusion failed. Check the loaded slot and the error log.',
        'cfs_sensor_position': 'Use POSITION=CONNECTIONS or POSITION=MATERIAL for the CFS sensor query.',
        'cfs_pending': 'CFS still has a feed error. Resolve it before loading or unloading filament.',
        'cfs_tail': 'Could not clear the remaining filament. Check the path and toolhead sensor before loading the next spool.',
    },
    'ru': {
        'recovery': 'Восстановление Creality после отключения питания недоступно с upstream SD. Начните новое задание или используйте прежнюю прошивку.',
        'first_layer': 'Режим первого слоя Creality недоступен. Добавьте явную PAUSE в слайсере.',
        'motor': 'Моторы ещё инициализируются. Дождитесь motor_ready перед запуском задания.',
        'file': 'Не удалось выбрать G-code. Выберите существующий файл из настроенного каталога.',
        'metadata': 'Не удалось прочитать корректные данные CFS. Проверьте файл перед запуском задания.',
        'filter': 'Файл требует фильтрации команд Creality. Используйте прежнюю прошивку до переноса этого режима.',
        'changed': 'G-code изменился после выбора. Выберите файл заново перед продолжением.',
        'temperature': 'Некорректная температура печати. Передайте конечные положительные BED_TEMP и EXTRUDER_TEMP.',
        'manual_material': 'Не удалось приостановить печать для смены материала. Поставьте задание на паузу и повторите команду.',
        'resume': 'Восстановление подачи CFS не удалось. Печать остаётся на паузе; устраните ошибку подачи перед продолжением.',
        'feed_watch': 'Контроль подачи CFS завершился ошибкой. Печать приостановлена; проверьте klippy.log перед продолжением.',
        'cfs_start': 'CFS не подтвердила подготовку к печати. Подготовка остановлена.',
        'cfs_end': 'CFS не подтвердила завершение печати. Проверьте состояние подачи и журнал ошибки.',
        'cfs_manual': 'Ручная подача CFS не выполнена. Проверьте загруженный слот и журнал ошибки.',
        'cfs_sensor_position': 'Для опроса датчика CFS укажите POSITION=CONNECTIONS или POSITION=MATERIAL.',
        'cfs_pending': 'В CFS сохранена ошибка подачи. Устраните её перед загрузкой или выгрузкой филамента.',
        'cfs_tail': 'Не удалось полностью выдавить остаток прутка. Проверьте тракт и датчик головы перед загрузкой следующей катушки.',
    },
}


class K2Print:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.messages = config.getchoice('language', MESSAGES, 'ru')
        self.sd = self.printer.load_object(config, 'virtual_sdcard')
        self._wrap_sd_status()
        self.loading = False
        self.selected = None
        self.gcode_metadata = None
        self.pause = self.printer.load_object(config, 'pause_resume')
        self.pause_start = False
        self.motor_cancel_print_start = False
        self.resume_err = False
        macros = self.printer.load_object(config, 'gcode_macro')
        self.error_gcode = macros.load_template(config, 'on_error_gcode', '')
        self.handling_error = False
        self.printer.register_event_handler('gcode:command_error', self.handle_command_error)
        self.resume = self.gcode.register_command('RESUME', None)
        self.gcode.register_command('RESUME', self.cmd_resume)
        self.printer.register_event_handler('virtual_sdcard:reset_file', self.reset)
        self.printer.register_event_handler('klippy:ready', self.handle_ready)
        for name in ('SDCARD_PRINT_FILE', 'M23', 'M24'):
            original = self.gcode.register_command(name, None)
            self.gcode.register_command(name, self.wrap_start(name, original))
        self.gcode.register_command('K_PRINT_PARAMETERS', self.cmd_parameters)
        self.gcode.register_command('K_PRINT_FINISH', self.cmd_finish)
        self.gcode.register_command('CLEAR_EEPROM_INFO', self.require_legacy_recovery)
        webhooks = self.printer.lookup_object('webhooks')
        for name, handler in (
                ('pause_resume/check_continue_print_state', self.recovery_status),
                ('pause_resume/cancel_continue_print', self.reject_recovery),
                ('pause_resume/set_print_first_layer', self.reject_first_layer),
                ('getBootLoaderVersion', self.application_version)):
            webhooks.register_endpoint(name, handler)

    def fail(self, key):
        raise self.gcode.error(self.messages[key])

    def handle_command_error(self):
        if self.handling_error or self.printer.is_shutdown():
            return
        self.handling_error = True
        try:
            self.error_gcode.run_gcode_from_command()
        except Exception:
            # Cleanup must never replace the original command failure.
            logging.exception('Configured K2 command-error cleanup failed')
        finally:
            self.handling_error = False

    def require_legacy_recovery(self, gcmd=None):
        self.fail('recovery')

    def reject_recovery(self, request):
        raise request.error(self.messages['recovery'])

    def _wrap_sd_status(self):
        orig_get_status = self.sd.get_status

        def wrapped(eventtime):
            status = dict(orig_get_status(eventtime))
            status.setdefault('first_layer_stop', False)
            return status

        self.sd.get_status = wrapped

    def reject_first_layer(self, request):
        response = {'state': 'success'}
        request.send(response)
        return response

    def recovery_status(self, request):
        response = {'file_state': False, 'eeprom_state': False, 'supported': False}
        request.send(response)
        return response

    def _read_mcu_version_file(self):
        try:
            with open('/tmp/.mcu_version', 'r') as stream:
                payload = json.load(stream)
            version = payload.get('mcu0', {}).get('version', '')
            return version if isinstance(version, str) else ''
        except (OSError, ValueError, AttributeError):
            return ''

    def application_version(self, request):
        # Retain the vendor endpoint name; this is not a bootloader readback.
        mcu = self.printer.lookup_object('mcu')
        now = self.printer.get_reactor().monotonic()
        version = (
            mcu.get_constants().get('software_version', '')
            or self._read_mcu_version_file()
            or mcu.get_status(now).get('mcu_version', '')
            or self.printer.get_start_args().get('software_version', '')
        )
        response = {'software_version': version}
        request.send(response)
        return response

    def clear_color_count(self):
        self.printer.send_event('box:clear_box_color_count')

    def handle_ready(self):
        # Macros have completed rename_existing by the ready event.
        for name, wrapper in (('START_PRINT', self.wrap_preparation),
                              ('END_PRINT', self.wrap_finish),
                              ('_K_ABORT_PREPARATION', self.wrap_finish)):
            original = self.gcode.register_command(name, None)
            if original is not None:
                self.gcode.register_command(name, wrapper(original))
        for name, flag in (('PAUSE', 'pause_start'),
                           ('MOTOR_CANCEL_PRINT', 'motor_cancel_print_start')):
            original = self.gcode.register_command(name, None)
            if original is not None:
                self.gcode.register_command(name, self.wrap_macro(original, flag))
        for name in self.gcode.get_command_help():
            if name.startswith(('BOX_LOAD_MATERIAL', 'BOX_QUIT_MATERIAL')):
                original = self.gcode.register_command(name, None)
                if original is not None:
                    self.gcode.register_command(name, self.wrap_macro(original, 'manual_material', name))

    def wrap_preparation(self, original):
        def execute(gcmd):
            try:
                return original(gcmd)
            except Exception:
                previous, self.handling_error = self.handling_error, True
                try:
                    self.printer.lookup_object('heaters').turn_off_all_heaters()
                    if not self.printer.is_shutdown():
                        self.gcode.run_script_from_command('_K_ABORT_PREPARATION')
                    else:
                        self.cmd_finish(gcmd)
                except Exception:
                    logging.exception('K2 preparation cleanup failed')
                finally:
                    self.handling_error = previous
                raise
        return execute

    def wrap_finish(self, original):
        def execute(gcmd):
            previous, self.handling_error = self.handling_error, True
            try:
                self.printer.lookup_object('heaters').turn_off_all_heaters()
                return original(gcmd)
            finally:
                failed = sys.exc_info()[0] is not None
                try:
                    self.cmd_finish(gcmd)
                except Exception:
                    if not failed:
                        raise
                    logging.exception('K2 finalization failed after the original command error')
                finally:
                    self.handling_error = previous
        return execute

    def wrap_macro(self, original, flag, name=None):
        def execute(gcmd):
            if flag == 'manual_material':
                if self.sd.is_active():
                    self.gcode.run_script_from_command('PAUSE')
                    if not self.pause.is_paused or self.sd.is_active():
                        self.fail('manual_material')
                box = self.printer.lookup_object('box', None)
                pending = None if box is None else box.box_action.box_save.error
                logging.info('K2 CFS manual command=%s pending_error=%s', name, pending)
                if pending is not None and name not in ('BOX_LOAD_MATERIAL_END', 'BOX_QUIT_MATERIAL_END'):
                    self.fail('cfs_pending')
                return original(gcmd)
            previous = getattr(self, flag)
            if flag == 'pause_start':
                self.pause_start = True
            try:
                return original(gcmd)
            finally:
                setattr(self, flag, previous if flag == 'pause_start' else False)
        return execute

    def cmd_resume(self, gcmd):
        if not self.pause.is_paused:
            return self.resume(gcmd)
        if self.resume_err:
            self.resume_err = False
            gcmd.respond_info(self.messages['resume'])
            return
        self.check_motors()
        if self.pause.sd_paused:
            self.clear_color_count()
        return self.resume(gcmd)

    def reset(self):
        if not self.loading:
            self.selected = None
            self.gcode_metadata = None

    def check_motors(self):
        motor = self.printer.lookup_object('motor_control', None)
        if motor is not None and not motor.is_ready:
            self.fail('motor')

    def fingerprint(self, path):
        info = os.stat(path)
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

    def select_path(self, filename, subdirs):
        filename = filename.lstrip('/')
        files = dict(self.sd.get_file_list(check_subdirs=subdirs))
        if filename not in files:
            filename = {name.lower(): name for name in files}.get(filename.lower())
        if filename is None:
            self.fail('file')
        root = os.path.realpath(self.sd.sdcard_dirname)
        path = os.path.realpath(os.path.join(root, filename))
        if os.path.commonpath((root, path)) != root or not os.path.isfile(path):
            self.fail('file')
        return path

    def read_metadata(self, path):
        from . import metadata
        slicer, unused_identity = metadata.get_slicer(path)
        result = {}
        # Reuse existing parsers without thumbnail writes or G-code preprocessing.
        for name in ('model_info', 'flush_para', 'default_filament_colour'):
            parser = getattr(slicer, 'parse_' + name, None)
            if parser is not None:
                result[name] = parser()
        info = result.setdefault('model_info', {})
        if not isinstance(info, dict):
            raise ValueError('Invalid model_info')
        info.setdefault('multicolor_method', 0)
        return {'file': os.path.basename(path), 'metadata': result}

    def prepare_metadata(self, path):
        try:
            before = self.fingerprint(path)
            data = self.sd.executor.submit(self.read_metadata, path)
            after = self.fingerprint(path)
            method = data['metadata']['model_info']['multicolor_method']
            if type(method) is not int or method not in (0, 1):
                raise ValueError('Invalid multicolor_method')
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise self.gcode.error(self.messages['metadata']) from error
        if before != after:
            self.fail('changed')
        # No implicit per-line filtering is allowed in the upstream worker.
        if method == 1:
            self.fail('filter')
        return data, (path, after)

    def wrap_start(self, name, original):
        def start(gcmd):
            if self.sd.is_active():
                raise gcmd.error('SD busy')
            if gcmd.get_int('ISCONTINUEPRINT', 0):
                self.require_legacy_recovery()
            if gcmd.get_int('FIRST_FLOOR_PRINT', 0):
                self.fail('first_layer')
            self.check_motors()
            if name == 'M24':
                if self.sd.current_file is None:
                    self.fail('file')
                path = os.path.realpath(self.sd.current_file.name)
                if self.selected != (path, self.fingerprint(path)):
                    self.fail('changed')
                self.clear_color_count()
                return original(gcmd)
            filename = (gcmd.get('FILENAME') if name == 'SDCARD_PRINT_FILE'
                        else gcmd.get_raw_command_parameters().strip())
            path = self.select_path(filename, name == 'SDCARD_PRINT_FILE')
            data, selected = self.prepare_metadata(path)
            self.loading = True
            self.gcode_metadata, self.selected = data, selected
            try:
                if name == 'SDCARD_PRINT_FILE':
                    self.clear_color_count()
                result = original(gcmd)
            except Exception:
                self.gcode_metadata, self.selected = None, None
                raise
            finally:
                self.loading = False
            box = self.printer.lookup_object('box', None)
            if box is not None:
                box.box_state.is_continue_print = False
            return result
        return start

    def get_gcode_flush_para(self):
        return (self.gcode_metadata or {}).get('metadata', {}).get('flush_para')

    def cmd_parameters(self, gcmd):
        self.check_motors()
        bed = gcmd.get_float('BED_TEMP')
        nozzle = gcmd.get_float('EXTRUDER_TEMP')
        if not (math.isfinite(bed) and bed >= 0 and math.isfinite(nozzle) and nozzle > 0):
            self.fail('temperature')
        custom = self.printer.lookup_object('custom_macro')
        custom.default_bed_temp, custom.default_extruder_temp = bed, nozzle
        box = self.printer.lookup_object('box', None)
        if box is not None:
            box.box_action.boxcfg.Tn_extrude_temp = nozzle

    def cmd_finish(self, gcmd):
        previous, self.handling_error = self.handling_error, True
        failure = None
        try:
            for command in ('BOX_SET_CURRENT_BOX_IDLE_MODE', 'BOX_END_PRINT',
                            'BOX_ENABLE_HEART_PROCESS TNN=T1A',
                            'BOX_ENABLE_HEART_PROCESS TNN=T2A',
                            'BOX_ENABLE_HEART_PROCESS TNN=T3A',
                            'BOX_ENABLE_HEART_PROCESS TNN=T4A',
                            'BED_MESH_CLEAR', 'SET_GCODE_OFFSET Z=0 MOVE=0',
                            'PRINT_PREPARE_CLEAR'):
                if self.printer.is_shutdown():
                    break
                try:
                    self.gcode.run_script_from_command(command)
                except Exception as error:
                    logging.exception('K2 finalization command failed: %s', command)
                    if failure is None:
                        failure = error
            try:
                self.reset_print_state()
            except Exception as error:
                logging.exception('K2 print state reset failed')
                if failure is None:
                    failure = error
        finally:
            self.handling_error = previous
        if failure is not None:
            raise failure

    def reset_print_state(self):
        self.reset()
        box = self.printer.lookup_object('box', None)
        if box is not None:
            action = box.box_action
            saved = action.box_save
            # End/cancel discards runout recovery without moving or unloading filament.
            if saved.error == 'filament_err':
                saved.clear_err()
            action.is_use_ending_material = False
            saved.box_filament_err = saved.material_auto_refill_flag = False
            saved.fan0_last_value = saved.fan2_last_value = None

    def get_status(self, eventtime):
        return {'power_loss_recovery': False, 'creality_command_filtering': False,
                'resume_err': self.resume_err,
                'metadata_loaded': self.gcode_metadata is not None}


def load_config(config):
    return K2Print(config)
