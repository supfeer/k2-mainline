// Placeholder I2C HAL stubs matching Klipper v0.14.0 i2ccmds interface

#include "gpio.h" // i2c_setup, i2c_write, i2c_read
#include "i2ccmds.h" // I2C_BUS_TIMEOUT

struct i2c_config
i2c_setup(uint32_t bus, uint32_t rate, uint8_t addr)
{
    (void)bus;
    (void)rate;
    return (struct i2c_config){.i2c = 0, .addr = addr};
}

int
i2c_write(struct i2c_config config, uint8_t write_len, uint8_t *write)
{
    (void)config;
    (void)write_len;
    (void)write;
    return I2C_BUS_TIMEOUT;
}

int
i2c_read(struct i2c_config config, uint8_t reg_len, uint8_t *reg,
         uint8_t read_len, uint8_t *read)
{
    (void)config;
    (void)reg_len;
    (void)reg;
    (void)read_len;
    (void)read;
    return I2C_BUS_TIMEOUT;
}
