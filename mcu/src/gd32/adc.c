// ADC driver for GigaDevice GD32F30x boards

#include "board/irq.h" // irq_save
#include "board/misc.h" // timer_from_us
#include "command.h" // shutdown
#include "generic/armcm_timer.h" // udelay
#include "gpio.h" // gpio_adc_setup
#include "internal.h" // GPIO
#include "sched.h" // sched_shutdown

DECL_CONSTANT("ADC_MAX", 4095);
#define ADC_TEMPERATURE_PIN 0xfe
DECL_ENUMERATION("pin", "ADC_TEMPERATURE", ADC_TEMPERATURE_PIN);

static const uint8_t adc_pins[] = {
    GPIO('A', 0), GPIO('A', 1), GPIO('A', 2), GPIO('A', 3),
    GPIO('A', 4), GPIO('A', 5), GPIO('A', 6), GPIO('A', 7),
    GPIO('B', 0), GPIO('B', 1), GPIO('C', 0), GPIO('C', 1),
    GPIO('C', 2), GPIO('C', 3), GPIO('C', 4), GPIO('C', 5),
    ADC_TEMPERATURE_PIN,
};

#define ADCx                        ADC0
#define _ADC_STAT                   ADC_STAT(ADCx)
#define _ADC_CTL0                   ADC_CTL0(ADCx)
#define _ADC_CTL1                   ADC_CTL1(ADCx)
#define _ADC_SAMPT0                 ADC_SAMPT0(ADCx)
#define _ADC_SAMPT1                 ADC_SAMPT1(ADCx)
#define _ADC_RSQ0                   ADC_RSQ0(ADCx)
#define _ADC_RSQ1                   ADC_RSQ1(ADCx)
#define _ADC_RSQ2                   ADC_RSQ2(ADCx)
#define _ADC_RDATA                  ADC_RDATA(ADCx)
#define ADC_EXTTRIG_REGULAR_NONE    ADC0_1_2_EXTTRIG_REGULAR_NONE
#define RCU_ADC                     RCU_ADC0
#define RCU_ADC_PSC_OFFSET          ((uint32_t)14U)

static void
gd32_adc_clock_config(void)
{
    uint32_t reg0 = RCU_CFG0;
    uint32_t reg1 = RCU_CFG1;

    reg0 &= ~(RCU_CFG0_ADCPSC_2 | RCU_CFG0_ADCPSC);
    reg0 |= (RCU_CKADC_CKAPB2_DIV6 << RCU_ADC_PSC_OFFSET);
    reg1 &= ~RCU_CFG1_ADCPSC_3;

    RCU_CFG0 = reg0;
    RCU_CFG1 = reg1;
}

static void
gd32_adc_regular_channel_config(uint8_t rank, uint8_t channel, uint32_t sample_time)
{
    uint32_t rsq, sampt;

    if (rank < 6U) {
        rsq = _ADC_RSQ2;
        rsq &= ~((uint32_t)(ADC_RSQX_RSQN << (5U * rank)));
        rsq |= ((uint32_t)channel << (5U * rank));
        _ADC_RSQ2 = rsq;
    } else if (rank < 12U) {
        rsq = _ADC_RSQ1;
        rsq &= ~((uint32_t)(ADC_RSQX_RSQN << (5U * (rank - 6U))));
        rsq |= ((uint32_t)channel << (5U * (rank - 6U)));
        _ADC_RSQ1 = rsq;
    } else if (rank < 16U) {
        rsq = _ADC_RSQ0;
        rsq &= ~((uint32_t)(ADC_RSQX_RSQN << (5U * (rank - 12U))));
        rsq |= ((uint32_t)channel << (5U * (rank - 12U)));
        _ADC_RSQ0 = rsq;
    }

    if (channel < 10U) {
        sampt = _ADC_SAMPT1;
        sampt &= ~((uint32_t)(ADC_SAMPTX_SPTN << (3U * channel)));
        sampt |= (uint32_t)(sample_time << (3U * channel));
        _ADC_SAMPT1 = sampt;
    } else if (channel < 19U) {
        sampt = _ADC_SAMPT0;
        sampt &= ~((uint32_t)(ADC_SAMPTX_SPTN << (3U * (channel - 10U))));
        sampt |= (uint32_t)(sample_time << (3U * (channel - 10U)));
        _ADC_SAMPT0 = sampt;
    }
}

struct gpio_adc
gpio_adc_setup(uint8_t pin)
{
    uint32_t chan;
    for (chan = 0; ; chan++) {
        if (chan >= sizeof(adc_pins) / sizeof(adc_pins[0]))
            shutdown("Not a valid ADC pin");
        if (adc_pins[chan] == pin)
            break;
    }

    struct gpio_adc res = {.chan = chan};
    if (!is_enable_pclock(RCU_ADC)) {
        enable_pclock(RCU_ADC);

        _ADC_CTL0 |= ADC_MODE_FREE;
        gd32_adc_clock_config();

        _ADC_CTL1 &= ~((uint32_t)ADC_CTL1_DAL);
        _ADC_RSQ0 &= ~((uint32_t)ADC_RSQ0_RL);
        _ADC_RSQ0 |= RSQ0_RL((uint32_t)0U);
        _ADC_CTL1 &= ~((uint32_t)ADC_CTL1_ETSRC);
        _ADC_CTL1 |= (uint32_t)ADC_EXTTRIG_REGULAR_NONE;
        _ADC_CTL1 |= ADC_CTL1_ETERC;
        _ADC_CTL1 |= (uint32_t)ADC_CTL1_ADCON;

        // Brief stabilization delay once before calibration (~20 us).
        udelay(20);

        _ADC_CTL1 |= (uint32_t)ADC_CTL1_RSTCLB;
        while (_ADC_CTL1 & ADC_CTL1_RSTCLB)
            ;

        _ADC_CTL1 |= ADC_CTL1_CLB;
        while (_ADC_CTL1 & ADC_CTL1_CLB)
            ;
    }

    if (pin == ADC_TEMPERATURE_PIN) {
        if (!(_ADC_CTL1 & ADC_CTL1_TSVREN)) {
            _ADC_CTL1 |= (uint32_t)ADC_CTL1_TSVREN;
            udelay(10);
        }
    } else {
        gpio_peripheral(pin, 2, 0);
    }

    return res;
}

uint32_t
gpio_adc_sample(struct gpio_adc g)
{
    uint32_t stat = _ADC_STAT;
    if (stat & ADC_FLAG_STRC) {
        if (!(stat & ADC_FLAG_EOC) || (_ADC_RSQ2 & ADC_RSQX_RSQN) != g.chan)
            return timer_from_us(20);
        return 0;
    }

    uint32_t sample_time = (g.chan == 16U)
        ? ADC_SAMPLETIME_239POINT5
        : ADC_SAMPLETIME_55POINT5;
    gd32_adc_regular_channel_config(0, (uint8_t)g.chan, sample_time);
    _ADC_CTL1 |= ADC_CTL1_SWRCST;
    return timer_from_us(g.chan == 16U ? 30 : 20);
}

uint16_t
gpio_adc_read(struct gpio_adc g)
{
    (void)g;
    _ADC_STAT = ~((uint32_t)(ADC_FLAG_EOC | ADC_FLAG_STRC));
    return (uint16_t)_ADC_RDATA;
}

void
gpio_adc_cancel_sample(struct gpio_adc g)
{
    irqstatus_t flag = irq_save();
    if ((_ADC_STAT & ADC_FLAG_STRC) && (_ADC_RSQ2 & ADC_RSQX_RSQN) == g.chan)
        gpio_adc_read(g);
    irq_restore(flag);
}
