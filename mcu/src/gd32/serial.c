// Interrupt-driven USART serial port driver for GD32F30x

#include "autoconf.h" // CONFIG_SERIAL_BAUD
#include "board/armcm_boot.h" // armcm_enable_irq
#include "board/serial_irq.h" // serial_rx_byte
#include "command.h" // DECL_CONSTANT_STR
#include "internal.h" // gpio_peripheral
#include "sched.h" // DECL_INIT

#if CONFIG_GD32_SERIAL_USART1_PA2_PA3
    #define GPIO_Rx           GPIO('A', 3)
    #define GPIO_Tx           GPIO('A', 2)
    #define USARTx            USART1
    #define USARTx_IRQn       USART1_IRQn
    #define USARTx_PCLK       RCU_USART1
    #define USARTx_IRQHandler USART1_IRQHandler
    DECL_CONSTANT_STR("RESERVE_PINS_serial", "PA2,PA3");
#elif CONFIG_GD32_SERIAL_USART0_PA9_PA10
    #define GPIO_Rx           GPIO('A', 10)
    #define GPIO_Tx           GPIO('A', 9)
    #define USARTx            USART0
    #define USARTx_IRQn       USART0_IRQn
    #define USARTx_PCLK       RCU_USART0
    #define USARTx_IRQHandler USART0_IRQHandler
    DECL_CONSTANT_STR("RESERVE_PINS_serial", "PA9,PA10");
#else
    #error "Unsupported GD32 serial configuration"
#endif

#define CTL0_FLAGS (USART_WL_8BIT | USART_PM_NONE | USART_RECEIVE_ENABLE \
                    | USART_TRANSMIT_ENABLE | USART_CTL0_UEN \
                    | USART_CTL0_RBNEIE)

void
USARTx_IRQHandler(void)
{
    uint32_t stat = USART_STAT0(USARTx);

    if (stat & (USART_STAT0_RBNE | USART_STAT0_ORERR)) {
        // Reading STAT0 then DATA clears any hardware overrun error (ORERR).
        serial_rx_byte((uint_fast8_t)(USART_DATA(USARTx) & 0xffU));
    }

    if ((stat & USART_STAT0_TBE) && (USART_CTL0(USARTx) & USART_CTL0_TBEIE)) {
        uint8_t data = 0;
        int ret = serial_get_tx_byte(&data);
        if (ret)
            USART_CTL0(USARTx) = CTL0_FLAGS;
        else
            USART_DATA(USARTx) = (USART_DATA_DATA & data);
    }
}

void
serial_enable_tx_irq(void)
{
    USART_CTL0(USARTx) = CTL0_FLAGS | USART_CTL0_TBEIE;
}

// Report whether the shared console TX queue has completely drained.
uint_fast8_t
gd32_serial_tx_idle(void)
{
    return !(USART_CTL0(USARTx) & USART_CTL0_TBEIE);
}

void
serial_init(void)
{
    uint32_t uclk = get_pclock_frequency(0);
    uint32_t baudval = CONFIG_SERIAL_BAUD;

    // Configure RX as floating input (pull_up=3) and TX as alternate-function push-pull.
    gpio_peripheral(GPIO_Rx, 1, 3);
    gpio_peripheral(GPIO_Tx, 3, 1);

    enable_pclock(USARTx_PCLK);

    USART_CTL0(USARTx) = 0;
    USART_CTL1(USARTx) &= ~USART_CTL1_STB;
    USART_CTL1(USARTx) |= USART_STB_1BIT;

    uint32_t udiv = (uclk + baudval / 2U) / baudval;
    uint32_t intdiv = udiv & 0x0000fff0U;
    uint32_t fradiv = udiv & 0x0000000fU;
    USART_BAUD(USARTx) = (USART_BAUD_FRADIV | USART_BAUD_INTDIV) & (intdiv | fradiv);

    USART_CTL0(USARTx) = CTL0_FLAGS;
    armcm_enable_irq(USARTx_IRQHandler, USARTx_IRQn, 0);
}
DECL_INIT(serial_init);
