# GD32F30x build rules for upstream Klipper v0.14.0

CROSS_PREFIX=arm-none-eabi-
CPP=$(CROSS_PREFIX)cpp

dirs-y += src/gd32 src/generic lib/gd32f30x

MCU := $(shell echo $(CONFIG_MCU))
MCU_UPPER := $(shell echo $(CONFIG_MCU) | tr a-z A-Z | tr X x)

CFLAGS-$(CONFIG_HAVE_LIMITED_CODE_SIZE) += -Os
CFLAGS-$(CONFIG_MACH_GD32F303XB) += -DGD32F30X_HD
CFLAGS-$(CONFIG_MACH_GD32F303XC) += -DGD32F30X_HD
CFLAGS-$(CONFIG_MACH_GD32F303XE) += -DGD32F30X_HD
CFLAGS-$(CONFIG_MACH_GD32F303XG) += -DGD32F30X_XD
CFLAGS-$(CONFIG_MACH_GD32F303XI) += -DGD32F30X_XD
CFLAGS-$(CONFIG_MACH_GD32F303XK) += -DGD32F30X_XD
CFLAGS-$(CONFIG_MACH_GD32F305XX) += -DGD32F30X_CL
CFLAGS-$(CONFIG_MACH_GD32F307XX) += -DGD32F30X_CL
CFLAGS-$(CONFIG_MACH_GD32F30X) += -DUSE_STDPERIPH_DRIVER -mcpu=cortex-m4 -Ilib/gd32f30x
CFLAGS += $(CFLAGS-y) -D$(MCU_UPPER) -mthumb -Ilib/cmsis-core

CFLAGS_klipper.elf += -nostdlib -lgcc -lc_nano
CFLAGS_klipper.elf += -T $(OUT)src/gd32/gd32_link.ld
$(OUT)klipper.elf: $(OUT)src/gd32/gd32_link.ld

ifeq ($(CONFIG_BOARD_INFO_CONFIGURE),y)
ifeq ($(CONFIG_MAIN_MCU_BOARD),y)
board_type := mcu
else ifeq ($(CONFIG_NOZZLE_MCU_BOARD),y)
board_type := noz
else ifeq ($(CONFIG_BED_MCU_BOARD),y)
board_type := bed
endif

board_id := $(patsubst "%",%,$(CONFIG_MCU_BOARD_ID))
board_hw_ver := $(patsubst "%",%,$(CONFIG_MCU_BOARD_HW_VER))
board_fw_ver := $(patsubst "%",%,$(CONFIG_MCU_BOARD_FW_VER))
board_fw_res := $(patsubst "%",%,$(CONFIG_MCU_BOARD_FW_RESERVED))
board_fw_version := $(board_type)$(board_id)_$(board_fw_ver)_$(board_fw_res)

CFLAGS += -DBOARD_FW_VERSION=\"$(board_fw_version)\"
endif

# Add source files
src-y += generic/armcm_boot.c generic/armcm_irq.c
src-y += generic/armcm_reset.c generic/crc16_ccitt.c
src-$(CONFIG_MACH_GD32F30X) += gd32/gd32f30x.c gd32/gd32f30x_gpio.c
src-$(CONFIG_MACH_GD32F30X) += generic/armcm_timer.c
src-$(CONFIG_BOARD_INFO_CONFIGURE) += gd32/board_info.c
src-$(CONFIG_WANT_ADC) += gd32/adc.c
src-$(CONFIG_WANT_I2C) += gd32/i2c.c
src-$(CONFIG_WANT_SPI) += gd32/spi.c
src-$(CONFIG_SERIAL) += gd32/serial.c gd32/serial_irq.c
src-$(CONFIG_TRANSPARENT_SERIAL) += gd32/usart_transparent.c
src-$(CONFIG_WANT_FANCHECK) += gd32/fan_check.c

# Binary output rule
target-y += $(OUT)klipper.bin

$(OUT)klipper.bin: $(OUT)klipper.elf
	@echo "  Creating bin file $@"
	$(Q)$(OBJCOPY) -O binary $< $@
