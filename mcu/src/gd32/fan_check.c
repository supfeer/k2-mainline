// Creality tachometer fan speed sampling handler (config_fancheck / query_fancheck)

#include "autoconf.h" // CONFIG_CLOCK_FREQ
#include "basecmd.h" // oid_alloc, oid_lookup, foreach_oid
#include "board/gpio.h" // gpio_in_setup, gpio_in_read
#include "board/misc.h" // timer_read_time, timer_from_us
#include "command.h" // DECL_COMMAND, sendf, shutdown
#include "sched.h" // DECL_TASK, DECL_SHUTDOWN, sched_add_timer

#define USR_EXTRAS_FAN_MAX_SPEED_RPM      12000
#define USR_EXTRAS_FAN_MIN_PULSE_WIDTH    (CONFIG_CLOCK_FREQ / (USR_EXTRAS_FAN_MAX_SPEED_RPM / 60 * 4))
#define USR_EXTRAS_FAN_CHECK_PERIOD_PREDIV 2
#define USR_EXTRAS_FAN_CHECK_PERIOD       (USR_EXTRAS_FAN_MIN_PULSE_WIDTH >> USR_EXTRAS_FAN_CHECK_PERIOD_PREDIV)
#define USR_EXTRAS_FAN_CHECK_CNT_LIMIT    ((1 << USR_EXTRAS_FAN_CHECK_PERIOD_PREDIV) * 30)

struct fancheck_status {
    uint8_t check_sample[USR_EXTRAS_FAN_CHECK_CNT_LIMIT];
    uint32_t speed;
};

enum {
    USR_EXTRAS_FAN_START = 1 << 0,
    USR_EXTRAS_FAN_MAX = 5,
};

static struct task_wake fancheck_wake;
static struct fancheck_status fc_status[USR_EXTRAS_FAN_MAX];

struct fancheck {
    struct timer time;
    struct gpio_in fan[USR_EXTRAS_FAN_MAX];
    struct fancheck_status *status;
    uint32_t period_ticks;
    uint8_t check_indx;
    uint8_t flags;
};

static uint_fast8_t
fancheck_period_event(struct timer *t)
{
    struct fancheck *fc = container_of(t, struct fancheck, time);
    fc->time.waketime += fc->period_ticks;
    sched_wake_task(&fancheck_wake);
    if (fc->flags & USR_EXTRAS_FAN_START)
        return SF_RESCHEDULE;
    return SF_DONE;
}

static uint8_t
fancheck_cal_pulse(uint8_t *pbuff, uint8_t len)
{
    uint8_t lowindex = len - 1;
    uint8_t highindex = 0;

    if (len < 3)
        return 0;

    for (uint8_t i = 1; i < len; i++) {
        if (pbuff[i] != pbuff[0]) {
            lowindex = i;
            break;
        }
    }
    if (lowindex >= len - 1)
        return 0;

    for (int8_t i = len - 2; i > 0; i--) {
        if (pbuff[i] != pbuff[len - 1]) {
            highindex = (uint8_t)i;
            break;
        }
    }
    if (highindex <= 1)
        return 0;

    if (pbuff[lowindex] == pbuff[highindex]) {
        for (int8_t i = (int8_t)highindex - 1; i > 0; i--) {
            if (pbuff[highindex] != pbuff[i]) {
                highindex = (uint8_t)i;
                break;
            }
        }
    }
    if (highindex < lowindex + 4)
        return 0;

    uint8_t span = highindex - lowindex + 1;
    uint8_t filter_max = (span * 3) >> 2;
    uint8_t filter_min = span >> 2;
    uint8_t oldval = pbuff[lowindex];
    uint8_t sum = 0;
    uint8_t pulse_cnt = 0;

    for (uint8_t i = lowindex; i <= highindex; i++) {
        sum += pbuff[i];
        if (pbuff[i] != oldval) {
            pulse_cnt++;
            oldval = pbuff[i];
        }
    }

    if (sum < filter_min || sum > filter_max)
        return 0;

    pulse_cnt = (pulse_cnt + 1) >> 1;
    if (!pulse_cnt)
        return 0;
    return sum / pulse_cnt;
}

static void
fancheck_cal_speed(struct fancheck_status *fs)
{
    uint8_t pulse_samples = fancheck_cal_pulse(
        fs->check_sample, USR_EXTRAS_FAN_CHECK_CNT_LIMIT);
    uint32_t speed = 0;
    if (pulse_samples) {
        // Compute RPM in integer arithmetic: 60 / 4 == 15 ticks-per-half-cycle factor.
        speed = (uint32_t)(((uint64_t)CONFIG_CLOCK_FREQ * 15ULL)
                           / ((uint64_t)pulse_samples * USR_EXTRAS_FAN_CHECK_PERIOD));
    }
    fs->speed = speed;
}

void
command_config_fancheck(uint32_t *args)
{
    struct fancheck *fc = oid_alloc(
        args[0], command_config_fancheck, sizeof(*fc));
    fc->time.func = fancheck_period_event;

    if (args[1] > USR_EXTRAS_FAN_MAX)
        shutdown("The fan number is more than USR_EXTRAS_FAN_MAX");

    for (uint8_t i = 1; i <= args[1]; i++)
        fc->fan[i - 1] = gpio_in_setup(args[2 * i], !!args[2 * i + 1]);

    fc->period_ticks = USR_EXTRAS_FAN_CHECK_PERIOD;
    fc->check_indx = 0;
    fc->flags = 0;
    fc->status = fc_status;
}
DECL_COMMAND(
    command_config_fancheck,
    "config_fancheck oid=%c fan_num=%c"
    " fan0_pin=%c pull_up0=%c fan1_pin=%c pull_up1=%c"
    " fan2_pin=%c pull_up2=%c fan3_pin=%c pull_up3=%c"
    " fan4_pin=%c pull_up4=%c");

void
command_query_fancheck(uint32_t *args)
{
    struct fancheck *fc = oid_lookup(args[0], command_config_fancheck);
    sched_del_timer(&fc->time);

    fc->flags = args[1] << 1;
    if (!fc->flags || (fc->flags & 0xC1))
        shutdown("args[1] is invalid");

    fc->flags |= USR_EXTRAS_FAN_START;
    fc->time.waketime = timer_read_time() + timer_from_us(500);
    sched_add_timer(&fc->time);
}
DECL_COMMAND(command_query_fancheck, "query_fancheck oid=%c which_fan=%c");

void
fancheck_task(void)
{
    uint8_t oid;
    struct fancheck *fc;
    if (!sched_check_wake(&fancheck_wake))
        return;

    foreach_oid(oid, fc, command_config_fancheck) {
        if (fc->check_indx < USR_EXTRAS_FAN_CHECK_CNT_LIMIT) {
            for (uint8_t i = 0; i < USR_EXTRAS_FAN_MAX; i++) {
                uint8_t temp = 0;
                if ((fc->flags >> (i + 1)) & 0x01)
                    temp = gpio_in_read(fc->fan[i]);
                fc->status[i].check_sample[fc->check_indx] = !!temp;
            }
            fc->check_indx++;
        } else {
            fc->flags &= ~USR_EXTRAS_FAN_START;
            for (uint8_t i = 0; i < USR_EXTRAS_FAN_MAX; i++) {
                if ((fc->flags >> (i + 1)) & 0x01)
                    fancheck_cal_speed(&fc->status[i]);
                else
                    fc->status[i].speed = 0;
            }

            sendf("fan_status oid=%c fan0_speed=%u fan1_speed=%u fan2_speed=%u"
                  " fan3_speed=%u fan4_speed=%u",
                  oid,
                  fc->status[0].speed,
                  fc->status[1].speed,
                  fc->status[2].speed,
                  fc->status[3].speed,
                  fc->status[4].speed);

            for (uint8_t j = 0; j < USR_EXTRAS_FAN_MAX; j++) {
                for (uint8_t i = 0; i < USR_EXTRAS_FAN_CHECK_CNT_LIMIT; i++)
                    fc->status[j].check_sample[i] = 0;
                fc->status[j].speed = 0;
                fc->flags &= ~(1 << (j + 1));
            }
            fc->check_indx = 0;
        }
    }
}
DECL_TASK(fancheck_task);

void
fancheck_shutdown(void)
{
    uint8_t oid;
    struct fancheck *fc;
    foreach_oid(oid, fc, command_config_fancheck) {
        fc->check_indx = 0;
        fc->period_ticks = 0;
        fc->flags = 0;
        for (uint8_t j = 0; j < USR_EXTRAS_FAN_MAX; j++) {
            for (uint8_t i = 0; i < USR_EXTRAS_FAN_CHECK_CNT_LIMIT; i++)
                fc->status[j].check_sample[i] = 0;
            fc->status[j].speed = 0;
        }
    }
}
DECL_SHUTDOWN(fancheck_shutdown);
