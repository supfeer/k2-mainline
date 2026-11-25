import json
import logging
from mcu import MCU_trsync, TriggerDispatch

from . import motor_control_wrapper


def decode_protection_response(response, address):
    if (not isinstance(response, (bytes, bytearray)) or len(response) != 14
            or response[0] != 0xf7 or response[1] != address
            or response[2] != 11 or response[3] > 3 or response[4] != 0x0c):
        raise ValueError('Invalid motor protection response')
    crc = 0
    for byte in response[2:-1]:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ (0x07 if crc & 0x80 else 0)) & 0xff
    if crc != response[-1]:
        raise ValueError('Motor protection response CRC mismatch')
    return ''.join(format(byte, '08b')[::-1] for byte in response[5:9])


def load_config(config):
    motor = motor_control_wrapper.Motor_Control(config)
    if motor.switch != 1:
        return motor
    original_status = motor.get_status
    protection = {'verified': False, 'error': None}

    def get_status(eventtime):
        status = original_status(eventtime)
        status['errors'] = {name: list(codes)
                            for name, codes in motor.motor_error_code.items()}
        status['motor_ready'] = bool(status['motor_ready'] and not any(status['errors'].values()))
        status['protection_verified'] = protection['verified']
        status['protection_error'] = protection['error']
        return status

    motor.get_status = get_status
    printer = config.get_printer()
    reactor = printer.get_reactor()
    main_mcu = printer.lookup_object('mcu')
    handoff = TriggerDispatch(main_mcu)

    def bind_handoff_steppers():
        names = set()
        for stepper in printer.lookup_object('toolhead').get_kinematics().get_steppers():
            if stepper.get_mcu() is not main_mcu:
                raise printer.config_error(printer.lookup_object('k2').messages['motor_gpio_handoff'])
            names.add(stepper.get_name())
            handoff.add_stepper(stepper)
        if names != {'stepper_x', 'stepper_y', 'stepper_z', 'stepper_z1'}:
            raise printer.config_error(printer.lookup_object('k2').messages['motor_gpio_handoff'])

    printer.register_event_handler('klippy:mcu_identify', bind_handoff_steppers)
    original_protection = motor.check_protection_code

    def check_protection_code(number, is_extruder=False):
        if printer.is_shutdown():
            return
        if motor.is_check_cut_pos_start:
            motor.cut_state = True
            return
        if is_extruder:
            return original_protection(number, is_extruder=True)
        messages = printer.lookup_object('k2').messages
        try:
            if number not in (1, 2, 3, 4):
                raise ValueError('Invalid axis motor number')
            address = 0x80 + number
            response = motor.box_action.motor_send_data(
                bytes([address]), b'\x0c', .25, data=b'\x0b', retries_en=False)
            if printer.is_shutdown():
                return
            bits = decode_protection_response(response, address)
        except Exception as error:
            if printer.is_shutdown():
                return
            protection.update(verified=False, error=str(error))
            motor.is_ready = False
            printer.invoke_shutdown(messages['motor_fault_query'] % (number, error))
            return
        warnings = ''.join(format(byte, '08b')[::-1] for byte in response[9:13])
        if '1' in warnings and motor.motor_warning_code[str(number)] != [warnings]:
            logging.warning('K2 motor %s warning: %s', number, warnings)
        motor.motor_warning_code[str(number)] = [warnings] if '1' in warnings else []
        if '1' not in bits:
            return
        motor.motor_error_code[str(number)] = [bits]
        protection.update(verified=False, error='Motor %s: %s' % (number, bits))
        motor.is_ready = False
        # A reactor callback must not run the vendor stop/clear/FORCE_MOVE scripts.
        printer.invoke_shutdown(messages['motor_fault'] % (number, bits))

    motor.check_protection_code = check_protection_code

    def query_motors(gcmd):
        protection.update(verified=False, error=None)
        pending = {}
        try:
            for number in (1, 2, 3, 4):
                address = 0x80 + number
                response = motor.box_action.motor_send_data(
                    bytes([address]), b'\x0c', .25, data=b'\x0b', retries_en=False)
                bits = decode_protection_response(response, address)
                pending[str(number)] = [bits] if '1' in bits else []
        except Exception as error:
            protection['error'] = str(error)
            logging.warning('K2 motor protection query failed: %s', error)
            return
        motor.motor_error_code.update(pending)
        protection['verified'] = True

    printer.lookup_object('gcode').register_command('K_QUERY_MOTORS', query_motors)
    mutex = printer.lookup_object('gcode').get_mutex()
    scheduled = False

    def clear_ready():
        motor.is_ready = False

    def initialize(eventtime):
        try:
            if printer.is_shutdown():
                return
            motor.set_motor_pin()
            if printer.is_shutdown():
                return
            toolhead = printer.lookup_object('toolhead')
            toolhead.wait_moves()
            oid = toolhead.get_kinematics().get_steppers()[0].get_oid()
            gpio = main_mcu.lookup_query_command('query_gpio_status oid=%c gpio_pin=%c',
                                                'gpio_status oid=%c status=%c', oid=oid)
            if gpio.send([oid, 'PA9'])['status'] != 1:
                raise printer.command_error(printer.lookup_object('k2').messages['motor_gpio_handoff'])
            # GPIO addressing bypasses the MCU stepper's cached direction bits.
            handoff.start(toolhead.get_last_move_time())
            if handoff.stop() != MCU_trsync.REASON_HOST_REQUEST:
                raise printer.command_error(printer.lookup_object('k2').messages['motor_gpio_handoff'])
            logging.info('K2 motor GPIO handed back to native steppers without movement')
            if not motor.is_ready or not motor.motor_params_init:
                raise printer.command_error('motor_ready=false')
        except Exception as error:
            clear_ready()
            logging.exception('K2 motor initialization failed')
            # A host restart keeps the nozzle board and its extruder driver
            # running; reset the MCUs once, like upstream's automated restart.
            # Latched driver faults are never retried.
            if (printer.get_start_args().get('start_reason') != 'firmware_restart'
                    and not any(motor.motor_error_code.values())):
                logging.info('K2 motor initialization failed without an MCU reset;'
                             ' attempting automated firmware restart')
                printer.request_exit('firmware_restart')
                return
            printer.invoke_shutdown(json.dumps({
                'code': 'key798',
                'msg': 'Motor initialization failed: %s',
                'values': [str(error)],
            }))
        finally:
            if printer.is_shutdown():
                clear_ready()
            mutex.__exit__(None, None, None)

    def handle_ready():
        nonlocal scheduled
        if scheduled or printer.is_shutdown():
            return
        scheduled = True
        # Claim the command mutex before yielding to external G-code input.
        # Upstream ReactorMutex has no greenlet owner; release in initialize.
        if mutex.test():
            raise printer.command_error('G-code mutex busy during motor startup')
        mutex.__enter__()
        try:
            reactor.register_callback(initialize)
        except BaseException:
            mutex.__exit__(None, None, None)
            raise

    printer.register_event_handler('klippy:ready', handle_ready)
    printer.register_event_handler('klippy:shutdown', clear_ready)
    return motor
