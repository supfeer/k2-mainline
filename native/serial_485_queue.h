#ifndef SERIAL_485_QUEUE_H
#define SERIAL_485_QUEUE_H
#include <stdint.h>
#include "list.h"
#include "msgblock_485.h"
#define BUFFER_MAX 512
struct serial_485_queue;
struct pull_message {
    int len;
    uint8_t msg[BUFFER_MAX];
};
struct serial_485_queue *serial_485_queue_alloc(int serial_fd, char serial_fd_type);
void serial_485_queue_exit(struct serial_485_queue *sq);
void serial_485_queue_free(struct serial_485_queue *sq);
void serial_485_queue_send(struct serial_485_queue *sq, uint8_t *msg, int len);
void serial_485_queue_pull(struct serial_485_queue *sq, struct pull_message *pqm);
void serial_485_queue_get_stats(struct serial_485_queue *sq, char *buf, int len);
int serial_485_queue_extract_old(struct serial_485_queue *sq, int sentq,
                               struct pull_message *p, int max);
#endif
