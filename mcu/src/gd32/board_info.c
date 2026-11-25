// Creality bootloader version block placed at offset 0x200 via SV_SECTION.

#include "autoconf.h"

#if CONFIG_BOARD_INFO_CONFIGURE
#ifndef BOARD_FW_VERSION
#error "BOARD_FW_VERSION must be defined when CONFIG_BOARD_INFO_CONFIGURE is enabled"
#endif

// Bytes [0x200..0x20b] store the 12-byte ASCII tag; bytes [0x20c..0x211]
// remain zero-initialized until pack_mcu_bin.py writes CRC16 and file length.
static const char software_version[32]
    __attribute__((section("SV_SECTION"), used)) = BOARD_FW_VERSION;
#endif
