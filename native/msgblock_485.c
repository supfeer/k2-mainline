/* Reconstructed from the pinned Creality ARM object; protocol checkpoint. */
#include <string.h>
#include "msgblock_485.h"
#include "pyhelper.h"

uint8_t
msgblock_485_crc8(const uint8_t *data, uint32_t len)
{
    uint8_t crc = 0;
    for (uint32_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; bit++)
            crc = (uint8_t)((crc << 1) ^ ((crc & 0x80) ? 0x07 : 0));
    }
    return crc;
}

static void
dump_bytes(const uint8_t *buf, int len)
{
    for (int i = 0; i < len; i++)
        errorf("buf[%d] = 0x%x", i, buf[i]);
}

int
msgblock_485_check(uint8_t *need_sync, uint8_t *buf, int buf_len)
{
    if (buf_len < MESSAGE_BUF_MIN) {
        errorf("buf_len = 0x%x", (unsigned)buf_len);
        dump_bytes(buf, buf_len);
        return 0;
    }

    int search_offset = 0;
    if (!*need_sync) {
        int msglen = buf[MESSAGE_485_POS_LEN];
        if (buf[0] != MESSAGE_485_HEAD) {
            errorf("msghead = 0x%x, buf_len = 0x%x", buf[0],
                   (unsigned)buf_len);
            dump_bytes(buf, buf_len);
        } else if (msglen < MESSAGE_485_MIN) {
            errorf("msglen = 0x%x, buf_len = 0x%x", (unsigned)msglen,
                   (unsigned)buf_len);
            dump_bytes(buf, buf_len);
            search_offset = 1;
        } else if (msglen + MESSAGE_485_SIZE_OUTSIDE_DATA > buf_len) {
            errorf("buf_len = 0x%x, msglen = 0x%x", (unsigned)buf_len,
                   (unsigned)msglen);
            dump_bytes(buf, buf_len);
            return 0;
        } else {
            uint8_t crc = msgblock_485_crc8(buf + MESSAGE_485_POS_LEN,
                                          (uint32_t)msglen);
            uint8_t expected = buf[msglen + MESSAGE_485_POS_LEN];
            if (crc == expected)
                return msglen + MESSAGE_485_SIZE_OUTSIDE_DATA;
            errorf("crc = 0x%x, msgcrc8 = 0x%x, buf_len = 0x%x",
                   crc, expected, (unsigned)buf_len);
            search_offset = 1;
        }
    }

    uint8_t *next = memchr(buf + search_offset, MESSAGE_485_HEAD,
                           (size_t)(buf_len - search_offset));
    errorf("Discard bytes until next HEAD found");
    dump_bytes(buf, buf_len);
    if (next) {
        int discard = (int)(buf - next);
        *need_sync = 0;
        errorf("-(next_head - buf) = %d", discard);
        return discard;
    }
    errorf("-buf_len = %d", -buf_len);
    *need_sync = 1;
    return -buf_len;
}
