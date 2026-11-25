"""Editable serial485 transport with cancellation-safe session ownership."""
import logging
import threading

import greenlet
from extras import k2_native as chelper
import msgproto
import serial

HEAD_POS = 0
ADDR_POS = 1
LEN_POS = 2
STATE_POS = 3
CMD_POS = 4
DATA_POS = 5
TIMEOUT_MAX = 3600
DefaultMessages_485 = {'#addr': 0}


def _notification_keys(data):
    addr, cmd = data[ADDR_POS - 1], data[CMD_POS - 1]
    return ('' if 252 <= addr <= 254 and cmd == 160 else '#addr_' + str(addr),
            '#cmd_' + str(cmd))


class error(Exception):
    pass


class Serialhdl_485:
    def __init__(self, reactor, warn_prefix=''):
        self.reactor = reactor
        self.warn_prefix = warn_prefix
        self.serial_dev = None
        self.msgparser = msgproto.MessageParser(warn_prefix=warn_prefix)
        self.ffi_main, self.ffi_lib = chelper.get_ffi()
        self.serialqueue = None
        self._closing = True
        self.stats_buf = self.ffi_main.new('char[4096]')
        self.lock = threading.Lock()
        self.background_thread = None
        self.handlers = {}
        self.pending_notifications = {}
        self.send_notifications = {}
        self.msgparser._init_messages(DefaultMessages_485)
        self.mutex = reactor.mutex()

    def _bg_thread(self):
        try:
            response = self.ffi_main.new('struct pull_message *')
            while not self._closing:
                self.ffi_lib.serial_485_queue_pull(self.serialqueue, response)
                count = response.len
                if count < 0 or self._closing:
                    break
                if not count:
                    continue
                try:
                    notifications_addr = ''
                    if response.msg[CMD_POS] != 160:
                        notifications_addr = '#addr_' + str(response.msg[ADDR_POS])
                    notifications_cmd = '#cmd_' + str(response.msg[CMD_POS])
                    notifications = notifications_addr + notifications_cmd
                except Exception as err:
                    logging.warning(err)
                    continue
                with self.lock:
                    completion = self.send_notifications.get(notifications)
                    hdl = self.handlers.get((notifications_addr, notifications_cmd),
                                            self.handle_default)
                if completion is None:
                    continue
                params = self.msgparser.parse(response.msg[0:count])
                try:
                    hdl(params)
                except:
                    logging.exception('%sException in serial callback', self.warn_prefix)
                if self._closing:
                    break
                with self.lock:
                    if self.send_notifications.get(notifications) is not completion:
                        continue
                    del self.send_notifications[notifications]
                self.reactor.async_complete(completion, params)
        finally:
            self._request_disconnect()

    def _request_disconnect(self):
        if not self._closing:
            queue = self.serialqueue
            self._closing = True
            # Object identity also protects against native pointer reuse.
            self.reactor.register_async_callback(
                lambda eventtime: self.disconnect() if self.serialqueue is queue else None)

    def is_connected(self):
        return self.serialqueue is not None and not self._closing

    def _start_session(self, serial_dev, serial_fd_type=b'u'):
        if self.serialqueue is not None:
            raise error('Serial485 session is already active')
        self.serial_dev = serial_dev
        queue = self.ffi_main.NULL
        try:
            queue = self.ffi_lib.serial_485_queue_alloc(serial_dev.fileno(), serial_fd_type)
            if queue == self.ffi_main.NULL:
                raise error('Unable to initialize serial485 queue')
            self.serialqueue = self.ffi_main.gc(queue, self.ffi_lib.serial_485_queue_free)
            queue = self.ffi_main.NULL
            self._closing = False
            self.background_thread = threading.Thread(target=self._bg_thread)
            self.background_thread.start()
        except Exception:
            try:
                if queue != self.ffi_main.NULL:
                    self.ffi_lib.serial_485_queue_free(queue)
                self.disconnect()
            except Exception:
                logging.exception('%sSerial485 session cleanup failed', self.warn_prefix)
            raise
        return True

    def connect_uart(self, serialport, baud, rts=True):
        if self.serialqueue is not None:
            raise error('Serial485 session is already active')
        logging.info('%s Starting serial connect' % self.warn_prefix)
        start_time = self.reactor.monotonic()
        while True:
            if self.reactor.monotonic() > start_time + 50.:
                raise error('Unable to connect serial_485')
            try:
                serial_dev = serial.Serial(baudrate=baud, timeout=0, exclusive=True)
                serial_dev.port = serialport
                serial_dev.rts = rts
                serial_dev.open()
            except (OSError, IOError, serial.SerialException) as e:
                logging.warning('%sUnable to open serial port: %s', self.warn_prefix, e)
                self.reactor.pause(self.reactor.monotonic() + 5.)
                continue
            ret = self._start_session(serial_dev, b'4')
            if ret:
                break

    def disconnect(self):
        if self.background_thread is threading.current_thread():
            self._request_disconnect()
            return
        self._closing = True
        try:
            if self.serialqueue is not None:
                self.ffi_lib.serial_485_queue_exit(self.serialqueue)
                if self.background_thread is not None and self.background_thread.ident is not None:
                    self.background_thread.join()
                self.background_thread = None
                queue = self.serialqueue
                self.serialqueue = None
                self.ffi_main.gc(queue, None)
                self.ffi_lib.serial_485_queue_free(queue)
            if self.serial_dev is not None:
                self.serial_dev.close()
                self.serial_dev = None
        finally:
            with self.lock:
                completions = list(self.send_notifications.values())
                self.send_notifications.clear()
            with self.mutex:
                completions.extend(pn for pending in self.pending_notifications.values()
                                   for pn in pending)
                self.pending_notifications.clear()
            seen = set()
            for completion in completions:
                if id(completion) not in seen and not completion.test():
                    completion.complete(None)
                seen.add(id(completion))

    def stats(self, eventtime):
        if self.serialqueue is None:
            return ''
        self.ffi_lib.serial_485_queue_get_stats(self.serialqueue, self.stats_buf, len(self.stats_buf))
        return self.ffi_main.string(self.stats_buf).decode()

    def raw_send(self, cmd):
        if not self.is_connected():
            raise error('Serial485 session is closed')
        self.ffi_lib.serial_485_queue_send(self.serialqueue, cmd, len(cmd))

    def raw_send_wait_ack(self, cmd, notifications, timeout):
        completion = self.reactor.completion()
        with self.lock:
            if not self.is_connected():
                return None
            if notifications in self.send_notifications:
                raise error('Serial485 request is already awaiting a response')
            self.send_notifications[notifications] = completion
        try:
            self.ffi_lib.serial_485_queue_send(self.serialqueue, cmd, len(cmd))
            return completion.wait(self.reactor.monotonic() + timeout)
        finally:
            with self.lock:
                if self.send_notifications.get(notifications) is completion:
                    del self.send_notifications[notifications]

    def get_response(self, cmd, timeout, notifications_addr, notifications_cmd, retries_en=True):
        cmd_str = ' '.join(hex(val) for val in cmd)
        notifications = notifications_addr + notifications_cmd
        with self.mutex:
            pending = self.pending_notifications.get(notifications)
            if not pending:
                return None
            completion = pending[0]
        retries = 4
        if not retries_en or timeout >= TIMEOUT_MAX:
            retries = 0
        params = None
        try:
            while self.is_connected():
                with self.mutex:
                    pending = self.pending_notifications.get(notifications)
                    if not pending or pending[0] is not completion:
                        return None
                logging.debug('retries = %d, cmd = %s' % (retries, cmd_str))
                params = self.raw_send_wait_ack(cmd, notifications,
                                                timeout if timeout < TIMEOUT_MAX else 1)
                with self.mutex:
                    pending = self.pending_notifications.get(notifications)
                    if not pending or pending[0] is not completion:
                        return None
                logging.debug('cmd[%s] get response or timeout' % cmd_str)
                if params is not None or retries == 0:
                    if params is None and not retries_en:
                        if cmd and cmd[0] == 0xfe:
                            logging.debug('broadcast RS-485 poll timed out: %s', cmd_str)
                        else:
                            logging.warning('non-idempotent RS-485 command timed out without retry: %s', cmd_str)
                    return params
                logging.warning('do not get response, retries = %d, cmd = %s' % (retries, cmd))
                retries -= 1
            return None
        finally:
            with self.mutex:
                pending = self.pending_notifications.get(notifications)
                owned = bool(pending) and pending[0] is completion
                if owned:
                    pending.pop(0)
                    if not pending:
                        del self.pending_notifications[notifications]
            if owned and not completion.test():
                completion.complete(params)

    def register_response(self, callback, notifications_addr, notifications_cmd):
        with self.lock:
            if callback is None:
                del self.handlers[(notifications_addr, notifications_cmd)]
            else:
                self.handlers[(notifications_addr, notifications_cmd)] = callback

    def handle_default(self, params):
        logging.debug('%sgot %s', self.warn_prefix, params)


class Serial_485_Wrapper:
    def __init__(self, config):
        self.printer = printer = config.get_printer()
        self.name = config.get_name().split()[1]
        logging.info('serial_485: self.name: %s' % self.name)
        printer.add_object('serial_485 ' + self.name, self)
        self.reactor = printer.get_reactor()
        self.baud = config.getint('baud', 460800, minval=2400)
        self.serial_port = config.get('serial')
        self.warn_prefix = 'Serial_485: '
        self._serial = Serialhdl_485(self.reactor, self.warn_prefix)
        printer.register_event_handler('klippy:connect', self._connect)
        printer.register_event_handler('klippy:disconnect', self._disconnect)
        printer.register_event_handler('klippy:shutdown', self._shutdown)
        printer.register_event_handler('klippy:firmware_restart', self._firmware_restart)
        self.gcode = printer.lookup_object('gcode')
        self.gcode.register_command('SERIAL_SEND_STR', self.cmd_485_send_data)
        self.send_list = []
        self.last_params = None
        self._cancelled = False
        self.send_timer = self.reactor.register_timer(self.send_queue_process, self.reactor.NEVER)

    def handle_callback(self, params):
        self.last_params = params

    def _connect(self):
        self._cancelled = False
        self._serial.connect_uart(self.serial_port, self.baud)
        if self.send_list:
            self.reactor.update_timer(self.send_timer, self.reactor.NOW)

    def _disconnect(self):
        self._shutdown()

    def _shutdown(self):
        self.reactor.update_timer(self.send_timer, self.reactor.NEVER)
        self.cancel_pending()
        self._serial.disconnect()

    def cancel_pending(self):
        self._cancelled = True
        self.send_list.clear()
        with self._serial.mutex:
            completions = [
                comp for pending in self._serial.pending_notifications.values() for comp in pending
            ]
            self._serial.pending_notifications.clear()
        with self._serial.lock:
            completions.extend(self._serial.send_notifications.values())
            self._serial.send_notifications.clear()
        seen = set()
        for completion in completions:
            if id(completion) not in seen and not completion.test():
                completion.complete(None)
            seen.add(id(completion))

    def _firmware_restart(self):
        self._shutdown()

    def register_response(self, cb, notifications_addr, notifications_cmd):
        self._serial.register_response(cb, notifications_addr, notifications_cmd)

    def cmd_485_send_data(self, gcmd):
        logging.info('gcmd = %s' % gcmd)
        data = gcmd.get_command_parameters().get('STR').encode()
        self._serial.raw_send(data)

    def cmd_send_data_with_response(self, data, timeout, retries_en=True):
        if not self._serial.is_connected():
            return None
        cancellation = self.printer.lookup_object('k2_cancel', None)
        self._cancelled = False
        notifications_addr, notifications_cmd = _notification_keys(data)
        notifications = notifications_addr + notifications_cmd
        completion = self.reactor.completion()
        with self._serial.mutex:
            if notifications not in self._serial.pending_notifications:
                self._serial.pending_notifications[notifications] = [completion]
            else:
                self._serial.pending_notifications[notifications].append(completion)
        entry = (data, timeout, notifications_addr, notifications_cmd, retries_en)
        self.add_send_data(*entry)
        eff_timeout = timeout if timeout < TIMEOUT_MAX else 1.
        attempts = 5 if (retries_en and timeout < TIMEOUT_MAX) else 1
        deadline = self.reactor.monotonic() + attempts * eff_timeout + 2.0
        params = None
        try:
            while True:
                if (cancellation is not None
                        and greenlet.getcurrent() is cancellation.owner
                        and not cancellation.cleaning()):
                    cancellation.check()
                now = self.reactor.monotonic()
                if now >= deadline or self._cancelled:
                    break
                params = completion.wait(min(deadline, now + .2))
                if completion.test():
                    break
        finally:
            self.remove_send_data(entry)
            with self._serial.mutex:
                pending = self._serial.pending_notifications.get(notifications)
                if pending and completion in pending:
                    pending.remove(completion)
                    if not pending:
                        del self._serial.pending_notifications[notifications]
            if not completion.test():
                completion.complete(None)
        if params is None:
            logging.debug('cmd_485_send_data_with_response timeout')
            return None
        params_data = ' '.join(hex(val) for val in params['#msg'])
        logging.debug('cmd_485_send_data_with_response params: %s', params_data)
        return params['#msg']

    def add_send_data(self, *args):
        self.send_list.append(args)
        if self._serial.is_connected():
            self.reactor.update_timer(self.send_timer, self.reactor.NOW)

    def send_queue_process(self, eventtime):
        send_list_copy = self.send_list[:]
        for args in send_list_copy:
            if not self._serial.is_connected():
                self.send_list.clear()
                return self.reactor.NEVER
            if not any(item is args for item in self.send_list):
                continue
            self._serial.get_response(*args)
            if any(item is args for item in self.send_list):
                self.remove_send_data(args)
        if not self._serial.is_connected() or not self.send_list:
            return self.reactor.NEVER
        return self.reactor.monotonic() + .01

    def remove_send_data(self, args):
        if args in self.send_list:
            self.send_list.remove(args)

    def is_exist_in_send_list(self, data, timeout):
        notifications_addr, notifications_cmd = _notification_keys(data)
        args = (data, timeout, notifications_addr, notifications_cmd)
        return any(item[:4] == args for item in self.send_list)
