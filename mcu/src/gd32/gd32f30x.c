// Core system clock, watchdog, and startup code for GD32F30x boards
//
// Based on GigaDevice GD32F30x firmware support and Klipper ARM Cortex-M boot.

#include "autoconf.h" // CONFIG_CLOCK_FREQ
#include "board/armcm_boot.h" // armcm_main
#include "board/armcm_reset.h" // try_request_canboot
#include "board/irq.h" // irq_disable
#include "board/misc.h" // bootloader_request
#include "command.h" // shutdown
#include "internal.h" // enable_pclock
#include "sched.h" // sched_main
#include "gd32f30x_gpio.h"

#define SYSTEM_CLOCK_120M_PLL_HXTAL ((uint32_t)120000000U)

#define RCU_MODIFY(__delay) do {                                        \
        volatile uint32_t i;                                            \
        if (0 != (__delay)) {                                           \
            RCU_CFG0 |= RCU_AHB_CKSYS_DIV2;                             \
            for (i = 0; i < (__delay); i++) {                           \
            }                                                           \
            RCU_CFG0 |= RCU_AHB_CKSYS_DIV4;                             \
            for (i = 0; i < (__delay); i++) {                           \
            }                                                           \
        }                                                               \
    } while (0)

void
watchdog_init(void)
{
    uint8_t prescaler_div = FWDGT_PSC_DIV8; // IRC40K / 8 = 5 kHz
    uint16_t reload_value = 2500;           // 500 ms window
    uint32_t timeout = FWDGT_PSC_TIMEOUT;
    uint32_t flag_status = RESET;

    FWDGT_CTL = FWDGT_WRITEACCESS_ENABLE;

    do {
        flag_status = FWDGT_STAT & FWDGT_STAT_PUD;
    } while ((--timeout > 0U) && ((uint32_t)RESET != flag_status));

    if ((uint32_t)RESET != flag_status)
        shutdown("watchdog initialize fail");

    FWDGT_PSC = (uint32_t)prescaler_div;

    timeout = FWDGT_RLD_TIMEOUT;
    do {
        flag_status = FWDGT_STAT & FWDGT_STAT_RUD;
    } while ((--timeout > 0U) && ((uint32_t)RESET != flag_status));

    if ((uint32_t)RESET != flag_status)
        shutdown("watchdog initialize fail");

    FWDGT_RLD = RLD_RLD(reload_value);
    FWDGT_CTL = FWDGT_KEY_RELOAD;
    FWDGT_CTL = FWDGT_KEY_ENABLE;
}
DECL_INIT(watchdog_init);

void
watchdog_task(void)
{
    FWDGT_CTL = FWDGT_KEY_RELOAD;
}
DECL_TASK(watchdog_task);

uint32_t SystemCoreClock = SYSTEM_CLOCK_120M_PLL_HXTAL;

static void
system_clock_120m_hxtal(void)
{
    uint32_t timeout = 0U;
    uint32_t stab_flag = 0U;

    RCU_CTL |= RCU_CTL_HXTALEN;

    do {
        timeout++;
        stab_flag = (RCU_CTL & RCU_CTL_HXTALSTB);
    } while ((0U == stab_flag) && (HXTAL_STARTUP_TIMEOUT != timeout));

    if (0U == (RCU_CTL & RCU_CTL_HXTALSTB)) {
        for (;;)
            ;
    }

    RCU_APB1EN |= RCU_APB1EN_PMUEN;
    PMU_CTL |= PMU_CTL_LDOVS;

    // AHB = SYSCLK (120 MHz), APB1 = AHB/2 (60 MHz), APB2 = AHB/2 (60 MHz)
    RCU_CFG0 |= RCU_AHB_CKSYS_DIV1;
    RCU_CFG0 |= RCU_APB2_CKAHB_DIV2;
    RCU_CFG0 |= RCU_APB1_CKAHB_DIV2;

#if (defined(GD32F30X_HD) || defined(GD32F30X_XD))
    // Select HXTAL/2 as PLL input: (8 MHz / 2) * 30 = 120 MHz
    RCU_CFG0 &= ~(RCU_CFG0_PLLSEL | RCU_CFG0_PREDV0);
    RCU_CFG0 |= (RCU_PLLSRC_HXTAL_IRC48M | RCU_CFG0_PREDV0);

    RCU_CFG0 &= ~(RCU_CFG0_PLLMF | RCU_CFG0_PLLMF_4 | RCU_CFG0_PLLMF_5);
    RCU_CFG0 |= RCU_PLL_MUL30;
#elif defined(GD32F30X_CL)
    RCU_CFG0 &= ~(RCU_CFG0_PLLMF | RCU_CFG0_PLLMF_4 | RCU_CFG0_PLLMF_5);
    RCU_CFG0 |= (RCU_PLLSRC_HXTAL_IRC48M | RCU_PLL_MUL30);

    RCU_CFG1 &= ~(RCU_CFG1_PLLPRESEL | RCU_CFG1_PREDV0SEL | RCU_CFG1_PLL1MF
                  | RCU_CFG1_PREDV1 | RCU_CFG1_PREDV0);
    RCU_CFG1 |= (RCU_PLLPRESRC_HXTAL | RCU_PREDV0SRC_CKPLL1 | RCU_PLL1_MUL8
                 | RCU_PREDV1_DIV5 | RCU_PREDV0_DIV10);

    RCU_CTL |= RCU_CTL_PLL1EN;
    while ((RCU_CTL & RCU_CTL_PLL1STB) == 0U) {
    }
#endif

    RCU_CTL |= RCU_CTL_PLLEN;
    while (0U == (RCU_CTL & RCU_CTL_PLLSTB)) {
    }

    // Enable high-drive mode for 120 MHz core frequency
    PMU_CTL |= PMU_CTL_HDEN;
    while (0U == (PMU_CS & PMU_CS_HDRF)) {
    }

    PMU_CTL |= PMU_CTL_HDS;
    while (0U == (PMU_CS & PMU_CS_HDSRF)) {
    }

    RCU_CFG0 &= ~RCU_CFG0_SCS;
    RCU_CFG0 |= RCU_CKSYSSRC_PLL;
    while (0U == (RCU_CFG0 & RCU_SCSS_PLL)) {
    }
}

static void
system_init(void)
{
#if (__FPU_PRESENT == 1) && (__FPU_USED == 1)
    SCB->CPACR |= ((3UL << 10 * 2) | (3UL << 11 * 2));
#endif
    RCU_CTL |= RCU_CTL_IRC8MEN;
    while (0U == (RCU_CTL & RCU_CTL_IRC8MSTB)) {
    }
    RCU_MODIFY(0x50);

    RCU_CFG0 &= ~RCU_CFG0_SCS;

#if (defined(GD32F30X_HD) || defined(GD32F30X_XD))
    RCU_CTL &= ~(RCU_CTL_PLLEN | RCU_CTL_CKMEN | RCU_CTL_HXTALEN);
    RCU_INT = 0x009f0000U;
#elif defined(GD32F30X_CL)
    RCU_CTL &= ~(RCU_CTL_PLLEN | RCU_CTL_PLL1EN | RCU_CTL_PLL2EN
                 | RCU_CTL_CKMEN | RCU_CTL_HXTALEN);
    RCU_INT = 0x00ff0000U;
#endif

    RCU_CTL &= ~(RCU_CTL_HXTALBPS);
    RCU_CFG0 = 0x00000000U;
    RCU_CFG1 = 0x00000000U;

    system_clock_120m_hxtal();
}

void
bootloader_request(void)
{
    try_request_canboot();
}

void
armcm_main(void)
{
    system_init();
    SCB->VTOR = (uint32_t)VectorTable;

    // Disable JTAG while preserving SWD to free PB3, PB4, PA15 pins
    rcu_periph_clock_enable(RCU_AF);
    gpio_pin_remap_config(GPIO_SWJ_SWDPENABLE_REMAP, ENABLE);

    sched_main();
}
