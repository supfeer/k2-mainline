# Архитектура K2 Mainline (Klipper v0.14.0 на Creality K2 Plus)

Документ описывает реальную архитектуру `k2_mainline/`, распределение модулей между апстримом Klipper `v0.14.0`, нативными библиотеками, адаптерами хоста, MCU-прошивками и безопасным процессом OTA-обновления.

---

## 1. Общие принципы дизайна

1. **Чистый хост апстрима**: архив `klipper-v0.14.0.tar.gz` (`klipper-c9559fa04ec6b11fb1a9a1610903546319fcf310`) проверяется по SHA-256 и при распаковке фиксирует контрольный штамп деревьев `klippy/` и `src/` в `work/klipper-v0.14.0.verified.json`. Сборка хоста (`assemble_host.py`) перепроверяет этот штамп и копирует модули K2 из [host/](file:///Users/supfeer/IdeaProjects/creality_helper/k2_mainline/host) с 0 изменениями и 0 перекрытиями апстримных файлов.
2. **Честная изоляция стадии MCU**: `build_mcu.py` не трогает кеш-дерево `work/klipper-v0.14.0`, а собирает прошивки в изолированном временном каталоге `work/mcu-stage`, добавляя `src/gd32`, `lib/gd32f30x` и две вставки подключения `MACH_GD32` в копию `src/Kconfig` внутри staging-дерева.
3. **Отказ от закрытых `.o` и строкового патчинга хоста**: удалены `prtouch_v3.o`, `z_align.o`, `k2_lis2dw.py`, `k2_cfs_diagnostic.py`, `k2_feed_diagnostics.py`.
4. **Безопасность OTA и CFS `1.1.3`**: обновляются только разделы `kernel` (`bootA/B`) и `rootfs` (`rootfsA/B`); исключены загрузчики (`uboot`, `awuboot`, `boot0`, `recovery`), а при загрузке ОС исключён общий обновлятор `mcu_util_485`.

---

## 2. Уровень MCU (GD32) и Native (`out/mcu/`, `out/native/`)

### Прошивки MCU (`scripts/build_mcu.py`, хостовый `arm-none-eabi-gcc`)
- **Основная плата (`main`)**: **`GD32F303RET6`** (`gd32f303xe`, `mcu/config.main`) -> `mcu0_140_G32-mcu0_022_950.bin`.
- **Плата сопла (`nozzle`)**: **`GD32F303CBT6`** (`gd32f303xb`, `mcu/config.nozzle`) -> `noz0_130_G30-noz0_021_950.bin`.
- **Матрица реализованных возможностей HAL `mcu/src/gd32/`**:
  - реализованы: системная инициализация 120 МГц, таймеры/IRQ (`armcm_timer`), UART (`USART0` PA9/PA10, `USART1` PA2/PA3 на 230400 бод), прозрачный мост `USART2` (`PB10/PB11`), `HAVE_GPIO`, `ADC`, тахометр вентиляторов (`config_fancheck`), программный SPI для `[lis2dw]` и `cs1237` / `trigger_analog`;
  - не используются/заглушки: аппаратный `spi_setup()` и `i2c` (в этих целях `[lis2dw]` работает через `spi_software_*` апстрима, EEPROM `bl24c16f` — через I²C Linux MCU), CAN не включён;
  - linker script `gd32_link.lds.S` вычитает смещение загрузчика (`CONFIG_FLASH_APPLICATION_ADDRESS - CONFIG_FLASH_BOOT_ADDRESS`, 12 КиБ) из `CONFIG_FLASH_SIZE`.
- **Упаковка `pack_mcu_bin.py`**: добавляет заголовок загрузчика `fw0` с CRC16 и суффиксом `_950`.
- **CFS `1.1.3`**: файлы `cfs0_050_G30-cfs0_000_113.bin` и `cfs0_050_G32-cfs0_000_113.bin` сохраняются из стока побайтово.

### Нативные ARMv7-артефакты (`scripts/build_native.py`, Docker `linux/arm/v7` Buster)
- `c_helper.so` — собран из чистого апстрима `klippy/chelper`.
- `k2_hardware.so` — отдельная нативная библиотека для K2/CFS (`msgblock_485`, `serial_485_queue`, `filament_change`, `get_flushing_volume`).
- `klipper_mcu` — динамический ARM EABI5 ELF (под целевой `glibc 2.29` / rootfs K2) с заглушками Linux MCU.

---

## 3. Хост-слой и совместимость (`host/`, `scripts/assemble_host.py`)

27 модулей `k2_mainline/host/` собираются поверх 214 апстримных файлов и `.so` в единое дерево `out/host/klippy/`:

1. **Кинематика и 10 адаптеров K2**: `k2_corexy.py`, `k2.py`, `k2_native.py`, `k2_transport.py`, `k2_display.py`, `k2_print.py`, `k2_mesh.py`, `k2_cancel.py`, `auto_addr.py`, `motor_control.py`, `z_align.py`.
   - `z_align.py` (`NativeZAlign`): сегменты ограничены `min(safe_dist, zmax_safe_pox_diff)`, при срабатывании одного концевика разрешён лишь один ограниченный доворот в пределах `zmax_safe_pox_diff`, проверяется разброс шагов `retry_tolerance` с ретраями `retries`, а `ZDOWN_FORCE_STOP` в простое не блокирует последующий хоуминг.
   - `k2.py` + `[load_cell_probe]`: содержит `K_LOAD_CELL_INIT` (первичная калибровка разгруженного датчика без `reference_tare_counts` и без требования уже установленного Z; сохраняемые поля `counts_per_gram` и `z_offset` вынесены в `printer.cfg` для совместимости с `SAVE_CONFIG`) и `K_LOAD_CELL_REFERENCE`; `START_PRINT` проверяет `is_calibrated` до начала движений и нагрева.
   - `k2_cancel.py`: при отмене подготовки дедлайн 15 с распространяется также на освобождение G-code mutex.
2. **5 автономных портированных драйверов**: `box_wrapper.py`, `filament_rack_wrapper.py`, `motor_control_wrapper.py`, `serial_485_wrapper.py`, `fan_feedback.py`.
   - `fan_feedback.py`: `_get_cx_fan_status(self, web_request)` корректно отвечает через `web_request.send(...)`.
   - `serial_485_wrapper.py` и `box_wrapper.py`: `EXTRUDE_PROCESS`, `RETRUDE_PROCESS` и `EXTRUDE_PROCESS_MODEL2` вызываются с `retries_en=False`; верхнеуровневые сценарии подачи/отвода, `extrude_process_auto_retry_process()`, ветвь `auto_retry_extruder_gear` (проверка `move_to_cut()` и `box_retrude_material()` до `stage=0`) и аварийный отвод не запускают автоповторы/подачу при отказе или неопределённом исходе транспорта (`TIMEOUT` / `CRC_ERR` / `LENGTH_ERR`); ожидание RS-485 ограничено таймаутом и немедленно прерывается через `cancel_pending()` при отмене в `k2_cancel.py`.
3. **11 сохранённых модулей совместимости**: `auto_addr_wrapper`, `base_info`, `belt_mdl`, `bl24c16f`, `box`, `custom_macro`, `filament_rack`, `load_ai`, `metadata`, `photograph`, `serial_485`.
4. **Совместимость со штатным экраном и общим PWM**:
   - `k2_display.py` регистрирует `ChamberFanBridge` как виртуальный `output_pin fan1` и обработчик `SET_PIN PIN=fan1` поверх **единственного** аппаратного владельца `PA0` (`[temperature_fan chamber_fan]`), исключив конфликт с `[duplicate_pin_override]` на `PA0`; макросы `Qmode`, `Qmode_exit`, `M106`, `M107` и `M141` согласованно отслеживают `fan1_was_throttled`, `fan1_value` и `chamber_fan_target` как при входе в тихий режим, так и при изменении скоростей/целей внутри `Qmode`, а `custom_macro.cmd_SET_QMODE_FLAG` проверяет наличие `virtual_sdcard.speed_mode_path` через `getattr` без ошибок в лог.
   - `custom_macro.py` очищен от обращений к удалённому `prtouch`/`CHECK_BED_MESH`/`can_break_flag`/`ACCEL_TO_DECEL` и делегирует экранную калибровку/прайм-линию в `G29`/`NOZZLE_CLEAR`/`K_PRIME_LINE`; `M141` допускает пустой вызов со штатного `master-server`; `[idle_timeout]` на паузе отключает только экструдер (`M104 S0`), сохраняя стол и Z; `START_PRINT`/`K_PRINT_PARAMETERS` поддерживают `BED_TEMP=0` (холодная пластина); `K_Z_CLEARANCE` безопасно обрабатывает положение на нижнем оптическом упоре `bottom_reference` без ошибки диапазона; `G29` передаёт `SOAK=0` по умолчанию и сохраняет профиль `default`; `INPUTSHAPER` сохраняет результат через `CXSAVE_CONFIG`.
   - `k2_print.py` обеспечивает `pause_resume/set_print_first_layer` (`{"state": "success"}`), `virtual_sdcard.first_layer_stop: False` и `getBootLoaderVersion` с чтением `/tmp/.mcu_version` и fallback.
5. **Конфигурация и миграция (`config/`, `k2_sync_config.py`)**:
   - [k2_sync_config.py](file:///Users/supfeer/IdeaProjects/creality_helper/k2_mainline/config/k2_sync_config.py) сохраняет пользовательские `box.cfg` и `k2_variables.cfg` во всех ветках (включая `fresh`), парсит канонический блок `#*# SAVE_CONFIG` с многострочными полями (`[bed_mesh default]`), комментирует перекрытые сохранёнными значениями ключи (PID сопла и стола) в теле `printer.cfg`, валидирует синтаксис в памяти, обновляет управляемые `*.cfg` и только последней атомарной записью коммитит `printer.cfg` с `# Version: v9.5.0`.

---

## 4. Безопасный OTA-пакет и init.d (`ota/`, `scripts/_ota_container_pack.py`)

1. **Исключение `mcu_util_485` при запуске системы**:
   - `init_patches.patch_mcu_update_init()` заменяет штатный `/etc/init.d/mcu_update` на изолированный двухфазный скрипт только для `F008` (`mcu0_140_G32` и `noz0_130_G30`), который сначала выполняет handshake и проверку аппаратных префиксов на обоих загрузчиках, затем прошивает/запускает оба контроллера без повторного handshake после `mcu_util -u` (учитывая автозапуск приложения) и ни при каких ошибках не вызывает `mcu_util_485` или `extruder_update`.
   - Неизменённый эталон стокового скрипта сохраняется в `/usr/share/k2-ota/mcu_update.stock` (`SHA-256: fe2b542886c22d345e5f83dfcb9248749415647b4dfdf2fa57deae0fc9a2501e`).
2. **Упаковка строго по инвентарю**: `_ota_container_pack.py` переносит в `/usr/share/klipper/klippy/` только файлы из `inventory.json` с побайтовой проверкой SHA-256 (0 лишних `.pyc`/`__pycache__`), отслеживает xattrs и hardlinks в `tree_state()`.
3. **Двухфазная установка SWUpdate (6 записей CPIO: `sw-description`, `preinstall.sh`, `postinstall.sh`, `kernel`, `rootfs`, `cpio_item_md5`)**:
   - **`preinstall.sh`** (только чтение и бэкап до записи в разделы): сверяет переданный SWUpdate аргумент слота (`now_A_next_B` / `now_B_next_A`) с `boot_partition`, `root_partition` и реальным `root=` из `/proc/cmdline`, строго валидирует `major:minor` block-device ID без суффиксов и попарные различия между `bootA/B` и `rootfsA/B` (включая перекрёстные алиасы, активный корень, `UDISK`/`rootfs_data`), проверяет минимальный размер в секторах под упакованные `kernel` и `rootfs` (`@KERNEL_MIN_SECTORS@`, `@ROOTFS_MIN_SECTORS@`, `rootfsA/B=614400`), свободное место на `/mnt/UDISK`, отсутствие активной печати через `/tmp/klippy_uds`, чистоту `/overlay/upper` и сохраняет бэкапы в `/mnt/UDISK/k2-mainline-backup.*`.
   - **`postinstall.sh`** (4 фазы обратимой фиксации после записи `kernel` и `rootfs`): аудит A -> обратимое перемещение (`mv`) разрешённого списка теней/whiteout `/overlay/upper` во временную область `/overlay/.k2-ota-stage.*` в фазе B **до** записи U-Boot -> атомарная запись и readback-проверка U-Boot через `fw_setenv -s` в фазе C (с явным откатом без подавления ошибок, проверкой readback отката, возвратом теней overlay при успехе отката или `CRITICAL:` в `stderr` при двойном отказе) -> очистка staging в фазе D, не затрагивая `/overlay/upper/etc/dropbear/authorized_keys`.
   - **`rollback_postinstall.sh`** (опция `--with-rollback`): в фазе A проверяет полноту бэкапа `1.1.4.11` (6 обязательных файлов + рекурсивный обход всех `[include ...]` и синтаксиса секций через Python), в фазе B обратимо переносит/подготавливает замену конфигов и теней overlay до U-Boot, в фазе C фиксирует `version=1.1.4.11` через `fw_setenv -s` с тем же обратимым откатом, и только в фазе D удаляет staging.
   - **`init_patches.py` (`/etc/init.d/mcu_update`)**: `compare_version()` завершается с ошибкой при отсутствии каталога или числе `.bin != 1`, а `probe_single_mcu()` распространяет отказ в фазу 1 (`auto_update` останавливается до прошивки MAIN или NOZZLE).
4. **Атомарная публикация (`build_ota.py`)**: сборка `.img` ведётся во временном `work/ota-stage`, сверяет хэши входов с `verify_host_arm_report.json` и переносит `.img` и отчёты в `out/ota/` только после прохождения всех ARMv7-проверок `swupdate`.
