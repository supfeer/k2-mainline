// GD32 ARM Cortex-M linker script with Creality SV_SECTION metadata placement
//
// Copyright (C) 2019  Kevin O'Connor <kevin@koconnor.net>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "autoconf.h" // CONFIG_FLASH_APPLICATION_ADDRESS

OUTPUT_FORMAT("elf32-littlearm", "elf32-littlearm", "elf32-littlearm")
OUTPUT_ARCH(arm)

MEMORY
{
  rom (rx) : ORIGIN = CONFIG_FLASH_APPLICATION_ADDRESS , LENGTH = CONFIG_FLASH_SIZE - (CONFIG_FLASH_APPLICATION_ADDRESS - CONFIG_FLASH_BOOT_ADDRESS)
  ram (rwx) : ORIGIN = CONFIG_RAM_START , LENGTH = CONFIG_RAM_SIZE
}

SECTIONS
{
    .text : {
        . = ALIGN(4);
        _text_vectortable_start = .;
        KEEP(*(.vector_table))
        _text_vectortable_end = .;
#if CONFIG_BOARD_INFO_CONFIGURE
        ASSERT((_text_vectortable_end - _text_vectortable_start) <= 0x200,
               "Vector table exceeds Creality 0x200 SV_SECTION offset");
        . = _text_vectortable_start + 0x200;
        KEEP(*(SV_SECTION))
#endif
        *(.text .text.*)
        *(.rodata .rodata*)
    } > rom

    . = ALIGN(4);
    _data_flash = .;

#if CONFIG_ARMCM_RAM_VECTORTABLE
    .ram_vectortable (NOLOAD) : {
        _ram_vectortable_start = .;
        . = . + ( _text_vectortable_end - _text_vectortable_start ) ;
        _ram_vectortable_end = .;
    } > ram
#endif

    .data : AT (_data_flash)
    {
        . = ALIGN(4);
        _data_start = .;
        *(.ramfunc .ramfunc.*);
        *(.data .data.*);
        . = ALIGN(4);
        _data_end = .;
    } > ram

    .bss (NOLOAD) :
    {
        . = ALIGN(4);
        _bss_start = .;
        *(.bss .bss.*)
        *(COMMON)
        . = ALIGN(4);
        _bss_end = .;
    } > ram

    _stack_start = CONFIG_RAM_START + CONFIG_RAM_SIZE - CONFIG_STACK_SIZE ;
    .stack _stack_start (NOLOAD) :
    {
        . = . + CONFIG_STACK_SIZE;
        _stack_end = .;
    } > ram

    /DISCARD/ : {
        // Unused C++ / libc init-fini and exception unwind tables
        *(.init)
        *(.fini)
        *(.ARM.extab)
        *(.ARM.exidx)
    }
}
