"""Own nozzle transactions through the standard MCU command/subscription APIs."""
import threading
from types import SimpleNamespace
from weakref import WeakKeyDictionary

_bridges = WeakKeyDictionary()


class TransparentQuery:
    def __init__(self, mcu, msgformat, respformat, oid=None, cq=None):
        self.mcu, self.response, self.oid = mcu, respformat, oid
        self.printer = mcu.get_printer()
        self.reactor = self.printer.get_reactor()
        self.command = mcu.lookup_command(msgformat, cq=cq)
        self.file_query = (mcu.lookup_query_command(msgformat, respformat, oid=oid, cq=cq)
                           if mcu.is_fileoutput() else None)
        if mcu not in _bridges:
            _bridges[mcu] = SimpleNamespace(mutex=self.reactor.mutex(), tainted=False)
        self.bridge = _bridges[mcu]

    def _fail(self, reason):
        self.bridge.tainted = True
        message = self.printer.lookup_object('k2').messages['bridge_fault'] % reason
        if not self.printer.is_shutdown():
            self.printer.invoke_shutdown(message)
        raise self.printer.command_error(message)

    def send(self, data=(), minclock=0, reqclock=0, retry=True):
        if self.file_query is not None:
            return self.file_query.send(data, minclock, reqclock, retry=False)
        with self.bridge.mutex:
            if self.bridge.tainted or self.printer.is_shutdown():
                self._fail('bridge unavailable')
            completion = self.reactor.completion()
            lock = threading.Lock()
            active = True

            def respond(params):
                nonlocal active
                with lock:
                    if not active:
                        return
                    active = False
                self.reactor.async_complete(completion, dict(params))

            subscription = self.mcu.register_serial_response(respond, self.response, self.oid)
            try:
                # The MCU emits one terminal reply; replay could repeat a motor write.
                self.command.send_wait_ack(data, minclock, max(minclock, reqclock))
                deadline = self.reactor.monotonic() + max(5., data[-1] / 1000. + 1.)
                params = completion.wait(deadline)
                if params is None:
                    self._fail('timeout')
                if not isinstance(params.get('read'), (bytes, bytearray)):
                    self._fail('invalid response')
                return params
            except self.printer.command_error:
                self.bridge.tainted = True
                if not self.printer.is_shutdown():
                    self._fail('transport error')
                raise
            except Exception as error:
                self._fail(str(error))
            finally:
                with lock:
                    active = False
                subscription.unregister()
