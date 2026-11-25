// Hardware SPI HAL stubs (K2 Plus peripherals use spi_software on GD32)

#include "command.h" // shutdown
#include "gpio.h" // spi_setup
#include "sched.h" // shutdown

struct spi_config
spi_setup(uint32_t bus, uint8_t mode, uint32_t rate)
{
    (void)bus;
    (void)mode;
    (void)rate;
    shutdown("Hardware SPI bus not configured on GD32");
}

void
spi_prepare(struct spi_config config)
{
    (void)config;
}

void
spi_transfer(struct spi_config config, uint8_t receive_data,
             uint8_t len, uint8_t *data)
{
    (void)config;
    (void)receive_data;
    (void)len;
    (void)data;
}
