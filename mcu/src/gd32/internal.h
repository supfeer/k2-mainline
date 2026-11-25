#ifndef __GD32_INTERNAL_H
#define __GD32_INTERNAL_H
// Local register, clock, and GPIO helpers for GD32F30x

#include "autoconf.h" // CONFIG_MACH_GD32F30X
#include <stdint.h> // uint32_t, int32_t

#include "gd32f30x.h"

#define AHB_FREQ                (CONFIG_CLOCK_FREQ)
#define APB1_FREQ               (CONFIG_CLOCK_FREQ >> 1)
#define APB2_FREQ               (CONFIG_CLOCK_FREQ >> 1)

#if CONFIG_MACH_GD32F303XX
#define APB2_ADC_FREQ           (APB2_FREQ >> 1)
#else
#error "Unsupported GD32 MCU family; check src/gd32/Kconfig"
#endif

#define GPIO(PORT, NUM)         (((PORT) - 'A') * 16 + (NUM))
#define GPIO2PORT(PIN)          ((PIN) / 16)
#define GPIO2BIT(PIN)           (1U << ((PIN) % 16))

uint32_t get_pclock_frequency(uint32_t periph_base);
uint32_t is_enable_pclock(uint32_t pclk);
void enable_pclock(uint32_t pclk);
void gpio_peripheral(uint32_t gpio, uint32_t otype, int32_t pull_up);
uint_fast8_t gd32_serial_tx_idle(void);

#endif // __GD32_INTERNAL_H
