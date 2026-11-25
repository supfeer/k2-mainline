# K2 Mainline: прошивка на чистом апстриме Klipper v0.14.0 для Creality K2 Plus

`k2_mainline/` — автономный сборочный стек и пакет модулей для сборки цельного SWUpdate OTA-образа `.img` для **Creality K2 Plus (`F008`, плата `CR0CN240319C13`)** на базе немодифицированного апстрима **Klipper `v0.14.0`** (`c9559fa04ec6b11fb1a9a1610903546319fcf310`).

> [!IMPORTANT]
> Сборочная проверка запускает целевой rootfs K2 Plus на ARMv7, использует штатный `/sbin/swupdate` и тестирует сценарии `mcu_update` и `k2_sync_config`. Аппаратную приёмку фиксируют отдельно для конкретного образа и принтера. Перед обновлением сохраните доступ по SSH и штатный образ `CR0CN240110C10_ota_img_V1.1.4.11.img`.

---

## Ключевые особенности

- **Чистый апстрим Klipper `v0.14.0`**: 0 изменений в ядре хоста `klippy/` и базовой кинематике, 0 теневых перекрытий апстримных модулей (`modified_upstream_files: 0`, `upstream_shadow_collisions: 0`).
- **Удалены закрытые `.o` и устаревшие форки**:
  - без `prtouch_v3.o`, `z_align.o`, `k2_lis2dw.py`, `k2_cfs_diagnostic.py`, `k2_feed_diagnostics.py`;
  - без инъекции кода в хост заменой строк при сборке;
  - используется апстримный `[load_cell_probe]` (с командой первичной калибровки `K_LOAD_CELL_INIT` и ранней блокировкой `START_PRINT`), чистый Python-драйвер `NativeZAlign` (`host/extras/z_align.py`) с ограничением перекоса по `zmax_safe_pox_diff` и стандартный апстримный `[lis2dw]` (программный SPI).
- **Сохранение штатного экрана**: реализованы и проверены 14 RPC-эндпоинтов (`shakehands`, `get_mesh`, `getBootLoaderVersion`, `get_cx_fan_status`, `pause_resume/check_continue_print_state`, `pause_resume/cancel_continue_print`, `pause_resume/set_print_first_layer`, `zdown_force_stop`, `info`, `objects/*`, `gcode/*`).
- **Веб-сервисы из коробки**. Moonraker `v0.10.0` на порту `7125`, Fluidd `v1.38.1` на `4408` и Mainsail `v2.19.0` на `4409`. Moonraker читает настройки из `/mnt/UDISK/printer_data/config/moonraker.conf` и управляет службами через OpenWrt procd.
- **Миграция данных Moonraker**. При первом запуске новый Moonraker переносит данные из `database/data.mdb` в `database/moonraker-sql.db` в каталоге `/mnt/UDISK/printer_data`. Исходная LMDB сохраняется для штатного Moonraker при откате. Записи, добавленные после миграции, остаются в SQLite.
- **Безопасность CFS `1.1.3` и RS-485**:
  - при старте системы исключён общий обновлятор `mcu_util_485`: двухфазный `/etc/init.d/mcu_update` проверяет аппаратные префиксы и загрузочные версии обеих плат до записи и прошивает только `MAIN` и `NOZZLE` (учитывая автозапуск приложения командой `mcu_util -u`), а эталонный стоковый скрипт сохранён без изменений в `/usr/share/k2-ota/mcu_update.stock` вместе со стоковыми прошивками CFS `1.1.3`;
  - неидемпотентные команды подачи/отвода CFS (`EXTRUDE_PROCESS` / `RETRUDE_PROCESS` / `EXTRUDE_PROCESS_MODEL2`) выполняются с `retries_en=False`, а верхний уровень блокирует повторную подачу/отвод при неопределённом исходе транспорта (`TIMEOUT` / `CRC_ERR` / `LENGTH_ERR`).
- **Один OTA-файл `.img`**: двустадийный `sw-description` (`preinstall.sh` + `postinstall.sh`) с жёсткой проверкой активного A/B-слота, перекрёстных блочных алиасов, отсутствия активной печати и атомарной фиксацией переменных U-Boot до очистки общих теней overlay.
- **Чистый репозиторий**: все исходники, кеши и артефакты сборки хранятся в `$K2_BUILD_ROOT` (`/Volumes/Media4tb/k2-mainline-builds`).

---

## Требования

- **Python 3.12+** и кросс-компилятор **`arm-none-eabi-gcc`** на хосте (macOS/Linux) для сборки прошивок MCU.
- **Docker Desktop** с платформами `linux/arm/v7` (`k2-mainline-arm-build:buster` для нативных `.so`, `klipper_mcu` и target chroot проверок) и `linux/arm64` (`k2-mainline-ota-tools:alpine` для SquashFS).
- Диск с достаточным свободным местом в `$K2_BUILD_ROOT` и локальная копия стокового `.img` с точным SHA-256 из [upstream.json](file:///Users/supfeer/IdeaProjects/creality_helper/k2_mainline/upstream.json).

---

## Быстрая сборка и проверка

```sh
# Полная параллельная или последовательная сборка с проверкой зависимостей стадий и тестами:
make -C k2_mainline -j4 all
```

Цели [Makefile](file:///Users/supfeer/IdeaProjects/creality_helper/k2_mainline/Makefile):

| Цель | Назначение |
| :--- | :--- |
| `fetch` | Проверка архива `klipper-v0.14.0.tar.gz`, стокового `.img`, безопасная распаковка и запись штампа целостности деревьев `klippy/` и `src/` |
| `mcu` | Сборка прошивок `MAIN` (`GD32F303RET6` / `gd32f303xe`) и `NOZZLE` (`GD32F303CBT6` / `gd32f303xb`) через хостовый `arm-none-eabi-gcc` |
| `native` | Сборка апстримной `c_helper.so`, вынесенной `k2_hardware.so` и динамического ARM EABI5 бинарника `klipper_mcu` в Docker ARMv7 |
| `services` | Сборка Moonraker и зависимостей для Python 3.9 ARMv7, проверка ELF, подготовка Fluidd, Mainsail и curl |
| `host` | Сборка чистого дерева `klippy/` с записью хэшей входов и ARMv7-проверкой на реальном rootfs K2 |
| `ota` | Проверка соответствия хэшей входов, перепаковка SquashFS, проверка `swupdate` / `preinstall` / `postinstall` / `mcu_update` в `work/ota-stage` и публикация в `out/ota/` |
| `test` | Полная сборка зависимостей + запуск `unittest` (`test_milestone1.py`, `test_host_and_ota.py`) |
| `test-only` | Запуск автотестов без пересборки |
| `clean-work` | Очистка staging-каталогов в `$K2_BUILD_ROOT/work/*-stage` |

Для генерации дополнительного отладочного образа отката на сток `1.1.4.11` (`V1.1.4.951` с `rollback_postinstall.sh`):

```sh
python3 -B k2_mainline/scripts/build_ota.py --with-rollback
```

---

## Выходные артефакты (`K2_BUILD_ROOT/out/`)

- `out/ota/CR0CN240110C10_ota_img_V1.1.4.950.img` — итоговый SWUpdate OTA-образ (публикуется только после прохождения всех проверок Stage 2).
- `out/ota/ota_verification_report.json` — полный отчёт сборки, сверки хэшей входов и офлайн-проверок SWUpdate, `preinstall`, `postinstall`, `mcu_update` и сценария отката.
- `out/host/klippy/`, `out/host/inventory.json`, `out/host/verify_host_arm_report.json` — хост-дерево и отчёт ARMv7-проверки.
- `out/mcu/` — `mcu0_140_G32-mcu0_022_950.bin`, `noz0_130_G30-noz0_021_950.bin`, `.elf`, `.dict`.
- `out/native/` — `c_helper.so`, `k2_hardware.so`, `klipper_mcu`, `build_native_report.json`.
- `out/services/build_services_report.json` — версии, состав и контрольные суммы пакетов веб-сервисов.

---

## Первая настройка после прошивки

При обновлении Klipper переносит пригодную калибровку тензодатчика из прежнего конфига. Если калибровки нет, первый хоуминг Z или `START_PRINT` автоматически калибрует датчик после отвода стола и сохраняет результат в `printer.cfg`. Полная сетка, построенная отдельной командой `G29`, сохраняется в профиле `k2_load_cell`.

Подробнее об архитектуре — в [ARCHITECTURE.md](file:///Users/supfeer/IdeaProjects/creality_helper/k2_mainline/ARCHITECTURE.md).
