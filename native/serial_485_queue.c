/* Editable RS-485 transport. The CFFI queue handle is opaque. */
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "serial_485_queue.h"
#include "pyhelper.h"

/* ponytail: cap each backlog at 1024 frames; add caller backpressure if
 * workloads exceed this. The void send ABI can only report failure by exit.
 */
#define QUEUE_LIMIT 1024
#define HISTORY_LIMIT 100

struct queued_frame {
    struct list_node node;
    int len, pos;
    uint8_t msg[BUFFER_MAX];
};

struct serial_485_queue {
    int serial_fd, serial_flags, write_only, pipe_fds[2];
    pthread_t tid;
    pthread_mutex_t lock;
    pthread_cond_t cond;
    int stopped, joining, joined;
    struct list_head pending, received, old_sent, old_received;
    unsigned pending_count, received_count, sent_history, received_history;
    uint8_t input[4096], need_sync;
    int input_pos;
    uint32_t bytes_write, bytes_read, bytes_invalid;
};

static void
stop_locked(struct serial_485_queue *sq)
{
    sq->stopped = 1;
    pthread_cond_broadcast(&sq->cond);
}

static void
kick_background(struct serial_485_queue *sq)
{
    ssize_t ret;
    do {
        ret = write(sq->pipe_fds[1], ".", 1);
    } while (ret < 0 && errno == EINTR);
    /* A full nonblocking pipe already contains a wakeup. */
    if (ret < 0 && errno != EAGAIN && errno != EWOULDBLOCK)
        report_errno("RS485 wakeup", ret);
}

static void
free_frames(struct list_head *head)
{
    while (!list_empty(head)) {
        struct queued_frame *frame = list_first_entry(head, struct queued_frame, node);
        list_del(&frame->node);
        free(frame);
    }
}

static void
remember_frame(struct list_head *head, unsigned *count, struct queued_frame *frame)
{
    list_add_tail(&frame->node, head);
    if (++*count > HISTORY_LIMIT) {
        frame = list_first_entry(head, struct queued_frame, node);
        list_del(&frame->node);
        free(frame);
        --*count;
    }
}

static void
write_frames(struct serial_485_queue *sq)
{
    /* Limit work per poll iteration so receive and exit cannot starve. */
    for (int budget = 32; budget && !list_empty(&sq->pending); budget--) {
        struct queued_frame *frame = list_first_entry(&sq->pending, struct queued_frame, node);
        ssize_t ret = write(sq->serial_fd, frame->msg + frame->pos, frame->len - frame->pos);
        if (ret < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
            return;
        if (ret <= 0) {
            errorf("RS485 write failed: %s", ret < 0 ? strerror(errno) : "zero write");
            stop_locked(sq);
            return;
        }
        frame->pos += ret;
        sq->bytes_write += ret;
        if (frame->pos == frame->len) {
            list_del(&frame->node);
            --sq->pending_count;
            remember_frame(&sq->old_sent, &sq->sent_history, frame);
        }
    }
}

static void
read_frames(struct serial_485_queue *sq)
{
    ssize_t ret = read(sq->serial_fd, sq->input + sq->input_pos,
                       sizeof(sq->input) - sq->input_pos);
    if (ret < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
        return;
    if (ret <= 0) {
        errorf("RS485 read failed: %s", ret < 0 ? strerror(errno) : "EOF");
        stop_locked(sq);
        return;
    }
    sq->input_pos += ret;
    while (sq->input_pos >= MESSAGE_BUF_MIN) {
        uint8_t old_sync = sq->need_sync;
        int len = msgblock_485_check(&sq->need_sync, sq->input, sq->input_pos);
        if (!len) {
            /* The pinned decoder can clear sync without consuming a byte. */
            if (old_sync && !sq->need_sync)
                continue;
            if (sq->input_pos == sizeof(sq->input)) {
                errorf("RS485 input buffer has no parse progress");
                stop_locked(sq);
            }
            return;
        }
        int consumed = len < 0 ? -len : len;
        if (consumed > sq->input_pos || (len > BUFFER_MAX)) {
            errorf("RS485 decoder returned an invalid length");
            stop_locked(sq);
            return;
        }
        if (len < 0) {
            sq->bytes_invalid += consumed;
        } else {
            if (sq->received_count == QUEUE_LIMIT) {
                errorf("RS485 receive backlog exhausted");
                stop_locked(sq);
                return;
            }
            struct queued_frame *frame = calloc(1, sizeof(*frame));
            if (!frame) {
                errorf("RS485 receive allocation failed");
                stop_locked(sq);
                return;
            }
            frame->len = len;
            memcpy(frame->msg, sq->input, len);
            list_add_tail(&frame->node, &sq->received);
            ++sq->received_count;
            sq->bytes_read += len;
            pthread_cond_broadcast(&sq->cond);
        }
        sq->input_pos -= consumed;
        memmove(sq->input, sq->input + consumed, sq->input_pos);
    }
}

static void *
background_thread(void *data)
{
    struct serial_485_queue *sq = data;
    /* Only this worker writes the device. EPIPE must not kill the host. */
    sigset_t signals;
    sigemptyset(&signals);
    sigaddset(&signals, SIGPIPE);
    int ret = pthread_sigmask(SIG_BLOCK, &signals, NULL);
    pthread_mutex_lock(&sq->lock);
    if (ret) {
        errorf("RS485 SIGPIPE mask failed: %s", strerror(ret));
        stop_locked(sq);
    }
    while (!sq->stopped) {
        struct pollfd fds[2] = {
            {sq->serial_fd, (sq->write_only ? 0 : POLLIN)
                           | (list_empty(&sq->pending) ? 0 : POLLOUT), 0},
            {sq->pipe_fds[0], POLLIN, 0}
        };
        pthread_mutex_unlock(&sq->lock);
        ret = poll(fds, 2, 1000);
        pthread_mutex_lock(&sq->lock);
        if (sq->stopped)
            break;
        if (ret < 0) {
            if (errno == EINTR)
                continue;
            errorf("RS485 poll failed: %s", strerror(errno));
            stop_locked(sq);
            break;
        }
        if (fds[1].revents & POLLIN) {
            char buf[256];
            /* One bounded drain is enough; more bytes cause another poll. */
            ssize_t count = read(sq->pipe_fds[0], buf, sizeof(buf));
            if (count <= 0 && !(count < 0 && (errno == EAGAIN || errno == EINTR)))
                stop_locked(sq);
        }
        if (fds[1].revents & (POLLERR | POLLHUP | POLLNVAL))
            stop_locked(sq);
        if (!sq->stopped && (fds[0].revents & POLLIN))
            read_frames(sq);
        if (fds[0].revents & (POLLERR | POLLHUP | POLLNVAL)) {
            errorf("RS485 device disconnected");
            stop_locked(sq);
        }
        if (!sq->stopped && (fds[0].revents & POLLOUT))
            write_frames(sq);
    }
    stop_locked(sq);
    pthread_mutex_unlock(&sq->lock);
    return NULL;
}

struct serial_485_queue *
serial_485_queue_alloc(int serial_fd, char serial_fd_type)
{
    struct serial_485_queue *sq = calloc(1, sizeof(*sq));
    if (!sq)
        return NULL;
    sq->serial_fd = serial_fd;
    sq->serial_flags = fcntl(serial_fd, F_GETFL);
    sq->pipe_fds[0] = sq->pipe_fds[1] = -1;
    sq->write_only = serial_fd_type == 'f';
    list_init(&sq->pending);
    list_init(&sq->received);
    list_init(&sq->old_sent);
    list_init(&sq->old_received);
    int mutex_ready = 0, cond_ready = 0;
    if (sq->serial_flags < 0 || pipe(sq->pipe_fds))
        goto fail;
    if (fcntl(serial_fd, F_SETFL, sq->serial_flags | O_NONBLOCK) < 0)
        goto fail;
    for (int i = 0; i < 2; i++) {
        int flags = fcntl(sq->pipe_fds[i], F_GETFL);
        if (flags < 0 || fcntl(sq->pipe_fds[i], F_SETFL, flags | O_NONBLOCK) < 0
            || fcntl(sq->pipe_fds[i], F_SETFD, FD_CLOEXEC) < 0)
            goto fail;
    }
    if (pthread_mutex_init(&sq->lock, NULL))
        goto fail;
    mutex_ready = 1;
    if (pthread_cond_init(&sq->cond, NULL))
        goto fail;
    cond_ready = 1;
    if (pthread_create(&sq->tid, NULL, background_thread, sq))
        goto fail;
    return sq;
fail:
    errorf("RS485 queue initialization failed");
    if (cond_ready)
        pthread_cond_destroy(&sq->cond);
    if (mutex_ready)
        pthread_mutex_destroy(&sq->lock);
    if (sq->pipe_fds[0] >= 0)
        close(sq->pipe_fds[0]);
    if (sq->pipe_fds[1] >= 0)
        close(sq->pipe_fds[1]);
    if (sq->serial_flags >= 0)
        fcntl(serial_fd, F_SETFL, sq->serial_flags);
    free(sq);
    return NULL;
}

void
serial_485_queue_exit(struct serial_485_queue *sq)
{
    if (!sq)
        return;
    pthread_mutex_lock(&sq->lock);
    stop_locked(sq);
    while (sq->joining)
        pthread_cond_wait(&sq->cond, &sq->lock);
    if (sq->joined) {
        pthread_mutex_unlock(&sq->lock);
        return;
    }
    sq->joining = 1;
    pthread_mutex_unlock(&sq->lock);
    kick_background(sq);
    int ret = pthread_join(sq->tid, NULL);
    pthread_mutex_lock(&sq->lock);
    sq->joining = 0;
    sq->joined = !ret;
    pthread_cond_broadcast(&sq->cond);
    pthread_mutex_unlock(&sq->lock);
    if (ret)
        errorf("RS485 pthread_join failed: %s", strerror(ret));
}

void
serial_485_queue_free(struct serial_485_queue *sq)
{
    if (!sq)
        return;
    /* The caller joins its pull thread before releasing the opaque handle. */
    serial_485_queue_exit(sq);
    if (!sq->joined)
        return; /* Do not free memory still reachable by a live worker. */
    free_frames(&sq->pending);
    free_frames(&sq->received);
    free_frames(&sq->old_sent);
    free_frames(&sq->old_received);
    close(sq->pipe_fds[0]);
    close(sq->pipe_fds[1]);
    if (fcntl(sq->serial_fd, F_SETFL, sq->serial_flags) < 0)
        report_errno("RS485 restore fd flags", -1);
    pthread_cond_destroy(&sq->cond);
    pthread_mutex_destroy(&sq->lock);
    free(sq);
}

void
serial_485_queue_send(struct serial_485_queue *sq, uint8_t *msg, int len)
{
    if (!sq)
        return;
    pthread_mutex_lock(&sq->lock);
    if (!sq->stopped) {
        /* Payload is addr,len,state,command,data; encoded frame adds HEAD/CRC. */
        if (!msg || len < 4 || len > MESSAGE_485_MAX + 1 || msg[1] != len - 1
            || sq->pending_count == QUEUE_LIMIT) {
            errorf("RS485 invalid outgoing frame or exhausted backlog");
            stop_locked(sq);
        } else {
            struct queued_frame *frame = calloc(1, sizeof(*frame));
            if (!frame) {
                errorf("RS485 transmit allocation failed");
                stop_locked(sq);
            } else {
                frame->msg[0] = MESSAGE_485_HEAD;
                memcpy(frame->msg + 1, msg, len);
                frame->msg[len + 1] = msgblock_485_crc8(frame->msg + 2, msg[1]);
                frame->len = len + 2;
                list_add_tail(&frame->node, &sq->pending);
                ++sq->pending_count;
            }
        }
    }
    pthread_mutex_unlock(&sq->lock);
    kick_background(sq);
}

void
serial_485_queue_pull(struct serial_485_queue *sq, struct pull_message *pqm)
{
    pthread_mutex_lock(&sq->lock);
    while (list_empty(&sq->received) && !sq->stopped)
        pthread_cond_wait(&sq->cond, &sq->lock);
    pqm->len = -1;
    if (!list_empty(&sq->received)) {
        struct queued_frame *frame = list_first_entry(&sq->received, struct queued_frame, node);
        list_del(&frame->node);
        --sq->received_count;
        pqm->len = frame->len;
        memcpy(pqm->msg, frame->msg, frame->len);
        remember_frame(&sq->old_received, &sq->received_history, frame);
    }
    pthread_mutex_unlock(&sq->lock);
}

void
serial_485_queue_get_stats(struct serial_485_queue *sq, char *buf, int len)
{
    if (len <= 0 || !buf)
        return;
    pthread_mutex_lock(&sq->lock);
    snprintf(buf, len, "bytes_write=%u bytes_read=%u bytes_invalid=%u",
             sq->bytes_write, sq->bytes_read, sq->bytes_invalid);
    pthread_mutex_unlock(&sq->lock);
}

int
serial_485_queue_extract_old(struct serial_485_queue *sq, int sentq,
                             struct pull_message *p, int max)
{
    if (max < 0 || (!p && max))
        return 0;
    pthread_mutex_lock(&sq->lock);
    struct list_head *old = sentq ? &sq->old_sent : &sq->old_received;
    int count = 0;
    while (!list_empty(old)) {
        struct queued_frame *frame = list_first_entry(old, struct queued_frame, node);
        list_del(&frame->node);
        if (count < max) {
            p[count].len = frame->len;
            memcpy(p[count].msg, frame->msg, frame->len);
            ++count;
        }
        free(frame);
    }
    *(sentq ? &sq->sent_history : &sq->received_history) = 0;
    pthread_mutex_unlock(&sq->lock);
    return count;
}
