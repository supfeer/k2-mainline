// GPIO functions for GD32F30x boards
//
// Preserves K2 Plus board pull-up/pull-down semantics where pull_up <= 0 selects
// internal pull-down (IPD), pull_up == 1 selects pull-up (IPU), and any other
// positive value (e.g. 3 in serial.c) selects floating input.

#include "basecmd.h" // oid_alloc
#include "board/irq.h" // irq_save
#include "command.h" // DECL_COMMAND
#include "gpio.h" // gpio_out_setup
#include "internal.h" // GPIO
#include "sched.h" // shutdown
#include "gd32f30x_rcu.h"

#if CONFIG_MACH_GD32F303XB
DECL_ENUMERATION_RANGE("pin", "PA0", GPIO('A', 0), 16);
DECL_ENUMERATION_RANGE("pin", "PB0", GPIO('B', 0), 16);
DECL_ENUMERATION_RANGE("pin", "PC0", GPIO('C', 0), 16);
#elif CONFIG_MACH_GD32F303XE
DECL_ENUMERATION_RANGE("pin", "PA0", GPIO('A', 0), 16);
DECL_ENUMERATION_RANGE("pin", "PB0", GPIO('B', 0), 16);
DECL_ENUMERATION_RANGE("pin", "PC0", GPIO('C', 0), 16);
DECL_ENUMERATION_RANGE("pin", "PD0", GPIO('D', 0), 16);
#else
DECL_ENUMERATION_RANGE("pin", "PA0", GPIO('A', 0), 16);
DECL_ENUMERATION_RANGE("pin", "PB0", GPIO('B', 0), 16);
DECL_ENUMERATION_RANGE("pin", "PC0", GPIO('C', 0), 16);
DECL_ENUMERATION_RANGE("pin", "PD0", GPIO('D', 0), 16);
DECL_ENUMERATION_RANGE("pin", "PE0", GPIO('E', 0), 16);
#endif

static const uint32_t gpio_port_base[] = {
    GPIOA, GPIOB, GPIOC, GPIOD, GPIOE, GPIOF, GPIOG,
};

static const uint32_t gpio_pclk[] = {
    RCU_GPIOA, RCU_GPIOB, RCU_GPIOC, RCU_GPIOD,
    RCU_GPIOE, RCU_GPIOF, RCU_GPIOG,
};

uint32_t
get_pclock_frequency(uint32_t periph_base)
{
    if (periph_base == 0)
        return AHB_FREQ >> 1;
    return APB2_ADC_FREQ;
}

uint32_t
is_enable_pclock(uint32_t pclk)
{
    return RCU_REG_VAL(pclk) & BIT(RCU_BIT_POS(pclk));
}

void
enable_pclock(uint32_t pclk)
{
    RCU_REG_VAL(pclk) |= BIT(RCU_BIT_POS(pclk));
}

// Configure pin mode: otype 0=output, 1=input, 2=analog, 3=AFIO
void
gpio_peripheral(uint32_t gpio, uint32_t otype, int32_t pull_up)
{
    uint32_t port = GPIO2PORT(gpio);
    if (port >= sizeof(gpio_port_base) / sizeof(gpio_port_base[0]))
        shutdown("Invalid GPIO port");

    uint16_t pin = (uint16_t)GPIO2BIT(gpio);
    uint32_t gpio_periph = gpio_port_base[port];
    uint32_t speed = GPIO_OSPEED_50MHZ;
    uint32_t pinmode = 0;

    enable_pclock(gpio_pclk[port]);

    if (otype == 0) {
        pinmode = (pull_up == 1) ? GPIO_MODE_OUT_PP : GPIO_MODE_OUT_OD;
    } else if (otype == 1) {
        if (pull_up <= 0)
            pinmode = GPIO_MODE_IPD;
        else if (pull_up == 1)
            pinmode = GPIO_MODE_IPU;
        else
            pinmode = GPIO_MODE_IN_FLOATING;
    } else if (otype == 2) {
        pinmode = GPIO_MODE_AIN;
    } else if (otype == 3) {
        pinmode = (pull_up == 0) ? GPIO_MODE_AF_OD : GPIO_MODE_AF_PP;
    } else {
        shutdown("gpio initialize fail");
    }

    uint32_t temp_mode = (uint32_t)(pinmode & 0x0FU);
    if ((pinmode & 0x10U) != 0U) {
        if (speed == GPIO_OSPEED_MAX) {
            temp_mode |= 0x03U;
            GPIOx_SPD(gpio_periph) |= (uint32_t)pin;
        } else {
            temp_mode |= speed;
        }
    }

    for (uint16_t i = 0U; i < 8U; i++) {
        if ((1U << i) & pin) {
            uint32_t reg = GPIO_CTL0(gpio_periph);
            reg &= ~GPIO_MODE_MASK(i);
            reg |= GPIO_MODE_SET(i, temp_mode);
            if (pinmode == GPIO_MODE_IPD)
                GPIO_BC(gpio_periph) = (uint32_t)((1U << i) & pin);
            else if (pinmode == GPIO_MODE_IPU)
                GPIO_BOP(gpio_periph) = (uint32_t)((1U << i) & pin);
            GPIO_CTL0(gpio_periph) = reg;
        }
    }

    for (uint16_t i = 8U; i < 16U; i++) {
        if ((1U << i) & pin) {
            uint32_t reg = GPIO_CTL1(gpio_periph);
            reg &= ~GPIO_MODE_MASK(i - 8U);
            reg |= GPIO_MODE_SET(i - 8U, temp_mode);
            if (pinmode == GPIO_MODE_IPD)
                GPIO_BC(gpio_periph) = (uint32_t)((1U << i) & pin);
            else if (pinmode == GPIO_MODE_IPU)
                GPIO_BOP(gpio_periph) = (uint32_t)((1U << i) & pin);
            GPIO_CTL1(gpio_periph) = reg;
        }
    }
}

void
gpio_out_reset(struct gpio_out g, uint8_t val)
{
    uint32_t port = GPIO2PORT(g.pin);
    uint32_t pin = GPIO2BIT(g.pin);
    irqstatus_t flag = irq_save();

    gpio_peripheral(g.pin, 0, 1);
    if (!val)
        GPIO_BC(gpio_port_base[port]) = pin;
    else
        GPIO_BOP(gpio_port_base[port]) = pin;

    irq_restore(flag);
}

struct gpio_out
gpio_out_setup(uint8_t pin, uint8_t val)
{
    if (GPIO2PORT(pin) >= sizeof(gpio_port_base) / sizeof(gpio_port_base[0]))
        shutdown("not an output pin");
    struct gpio_out g = {.pin = pin};
    gpio_out_reset(g, val);
    return g;
}

void
gpio_out_toggle_noirq(struct gpio_out g)
{
    uint32_t port = GPIO2PORT(g.pin);
    uint32_t pin = GPIO2BIT(g.pin);
    if (GPIO_OCTL(gpio_port_base[port]) & pin)
        GPIO_BC(gpio_port_base[port]) = pin;
    else
        GPIO_BOP(gpio_port_base[port]) = pin;
}

void
gpio_out_toggle(struct gpio_out g)
{
    irqstatus_t flag = irq_save();
    gpio_out_toggle_noirq(g);
    irq_restore(flag);
}

uint8_t
gpio_out_read(uint8_t gpio_indx)
{
    if (GPIO2PORT(gpio_indx) >= sizeof(gpio_port_base) / sizeof(gpio_port_base[0]))
        shutdown("not an output pin");
    uint32_t port = GPIO2PORT(gpio_indx);
    uint32_t pin = GPIO2BIT(gpio_indx);
    return !!(GPIO_OCTL(gpio_port_base[port]) & pin);
}

void
command_query_gpio_status(uint32_t *args)
{
    sendf("gpio_status oid=%c status=%c", args[0], gpio_out_read(args[1]));
}
DECL_COMMAND(command_query_gpio_status, "query_gpio_status oid=%c gpio_pin=%c");

void
gpio_out_write(struct gpio_out g, uint8_t val)
{
    uint32_t port = GPIO2PORT(g.pin);
    uint32_t pin = GPIO2BIT(g.pin);
    if (!val)
        GPIO_BC(gpio_port_base[port]) = pin;
    else
        GPIO_BOP(gpio_port_base[port]) = pin;
}

struct gpio_in
gpio_in_setup(uint8_t pin, int8_t pull_up)
{
    if (GPIO2PORT(pin) >= sizeof(gpio_port_base) / sizeof(gpio_port_base[0]))
        shutdown("Not an input pin");
    struct gpio_in g = {.pin = pin};
    gpio_in_reset(g, pull_up);
    return g;
}

void
gpio_in_reset(struct gpio_in g, int8_t pull_up)
{
    irqstatus_t flag = irq_save();
    gpio_peripheral(g.pin, 1, pull_up);
    irq_restore(flag);
}

uint8_t
gpio_in_read(struct gpio_in g)
{
    uint32_t port = GPIO2PORT(g.pin);
    uint32_t pin = GPIO2BIT(g.pin);
    return !!(GPIO_ISTAT(gpio_port_base[port]) & pin);
}

#define PCF_POSITION_MASK  ((uint32_t)0x000F0000U)
#define PCF_SWJCFG_MASK    ((uint32_t)0xF0FFFFFFU)
#define PCF_LOCATION1_MASK ((uint32_t)0x00200000U)
#define PCF_LOCATION2_MASK ((uint32_t)0x00100000U)
#define AFIO_PCF1_FIELDS   ((uint32_t)0x80000000U)
#define LSB_16BIT_MASK     ((uint16_t)0xFFFFU)

void
gpio_pin_remap_config(uint32_t gpio_remap, ControlStatus newvalue)
{
    uint32_t temp_reg = (gpio_remap & AFIO_PCF1_FIELDS) ? AFIO_PCF1 : AFIO_PCF0;
    uint32_t temp_mask = (gpio_remap & PCF_POSITION_MASK) >> 0x10U;
    uint32_t remap1 = gpio_remap & LSB_16BIT_MASK;

    if ((PCF_LOCATION1_MASK | PCF_LOCATION2_MASK)
        == (gpio_remap & (PCF_LOCATION1_MASK | PCF_LOCATION2_MASK))) {
        temp_reg &= PCF_SWJCFG_MASK;
        AFIO_PCF0 &= PCF_SWJCFG_MASK;
    } else if (PCF_LOCATION2_MASK == (gpio_remap & PCF_LOCATION2_MASK)) {
        uint32_t remap2 = ((uint32_t)0x03U) << temp_mask;
        temp_reg &= ~remap2;
        temp_reg |= ~PCF_SWJCFG_MASK;
    } else {
        temp_reg &= ~(remap1 << ((gpio_remap >> 0x15U) * 0x10U));
        temp_reg |= ~PCF_SWJCFG_MASK;
    }

    if (newvalue != DISABLE)
        temp_reg |= (remap1 << ((gpio_remap >> 0x15U) * 0x10U));

    if (gpio_remap & AFIO_PCF1_FIELDS)
        AFIO_PCF1 = temp_reg;
    else
        AFIO_PCF0 = temp_reg;
}

void
rcu_periph_clock_enable(rcu_periph_enum periph)
{
    RCU_REG_VAL(periph) |= BIT(RCU_BIT_POS(periph));
}
