"""Isolated K2 hardware bindings; never replace the upstream C helper."""
from pathlib import Path

import cffi

HARDWARE_DEFS = r'''
    #define BUFFER_MAX 512
    struct pull_message {
        int len;
        uint8_t msg[BUFFER_MAX];
    };
    struct serial_485_queue;
    void serial_485_queue_send(struct serial_485_queue *sq, uint8_t *msg, int len);
    void serial_485_queue_pull(struct serial_485_queue *sq, struct pull_message *pqm);
    void serial_485_queue_get_stats(struct serial_485_queue *sq, char *buf, int len);
    struct serial_485_queue *serial_485_queue_alloc(int serial_fd, char serial_fd_type);
    void serial_485_queue_free(struct serial_485_queue *sq);
    void serial_485_queue_exit(struct serial_485_queue *sq);
    uint8_t msgblock_485_crc8(const uint8_t *data, uint32_t len);
    typedef struct { uint8_t r, g, b; } rgb_t;
    int get_flushing_volume(const rgb_t source, const rgb_t target);
'''

_binding = None


def get_ffi():
    global _binding
    if _binding is None:
        ffi = cffi.FFI()
        ffi.cdef(HARDWARE_DEFS)
        library = ffi.dlopen(str(Path(__file__).with_name('k2_hardware.so')))
        _binding = ffi, library
    return _binding
