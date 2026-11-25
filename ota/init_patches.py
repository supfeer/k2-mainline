"""Minimal, fail-closed init.d patchers for K2 Plus upstream Klipper rootfs."""

from __future__ import annotations

import re

_SYNC_BLOCK_PATTERN = re.compile(
    r"    if \[ -s \$PRINTER_CONFIG_DIR/printer\.cfg \]; then\n"
    r".*?"
    r"    else\n"
    r"        cp -af \"\$\{CONFIG_PATH\}\"/\*\.cfg \$PRINTER_CONFIG_DIR/\n"
    r"        sync\n"
    r"    fi\n",
    re.DOTALL,
)

_SYNC_REPLACEMENT = (
    '    $PROG -B "$ROM_CONFIG_DIR/k2_sync_config.py" "$CONFIG_PATH" "$PRINTER_CONFIG_DIR" || {\n'
    '        echo "k2_sync_config failed!"\n'
    '        exit 1\n'
    '    }\n'
    '    sync\n'
)

_SAFE_MCU_UPDATE_SCRIPT = r"""#!/bin/sh /etc/rc.common

START=13
STOP=90
USE_PROCD=1
DEPEND=boot

PROG=/usr/bin/mcu_util
RESET_BIN=/usr/bin/mcu_reset.sh
GPIO_PWR_VAL=/sys/class/gpio/gpio140/value
VERSION_FILE=/tmp/.mcu_version
LOG_FILE=/tmp/mcu_update.log
KLIPPER_DIR=/usr/share/klipper
FW_ROOT_DIR=$KLIPPER_DIR/fw

RET_SUCCESS=0
RET_TIMEOUT=1
RET_ERROR=2

write_log()
{
    [ -e "$LOG_FILE" ] || touch "$LOG_FILE"
    echo "[$(date "+%Y-%m-%d %H:%M:%S")] $1" >> "$LOG_FILE"
}

mcu_handshake()
{
    local ret
    local output
    local tty_path=$1
    output=$("$PROG" -i "$tty_path" -c)
    ret=$?
    if [ "$ret" -ne "$RET_SUCCESS" ]; then
        write_log "$output"
        write_log "handshake $tty_path fail, ret=$ret"
    else
        write_log "handshake $tty_path success"
    fi
    echo "$ret"
}

get_version()
{
    local version
    local ret
    local tty_path=$1
    version=$("$PROG" -i "$tty_path" -g)
    ret=$?
    if [ "$ret" -ne "$RET_SUCCESS" ]; then
        write_log "$version"
        write_log "get_version $tty_path fail, ret=$ret"
        echo "unknown"
    else
        write_log "get_version $version"
        echo "$version"
    fi
}

is_invalid_fw_version()
{
    local version=$1
    local fw_version=
    fw_version=$(echo "$version" | awk 'BEGIN{FS="-"} {print $2} END{}')
    if [ "${fw_version:4:1}" != "_" ] || [ "${fw_version:8:1}" != "_" ]; then
        echo "true"
    else
        echo "false"
    fi
}

compare_version()
{
    local version=$1
    local fw_dir=$2
    local force_update=$3
    local ret_fw_bin=
    local fw_bin=
    local hw_version=
    local orig_fw_version=
    local target_fw_version=
    local invalid_fw_version=false
    local tmp=
    local count=0

    hw_version=$(echo "$version" | awk 'BEGIN{FS="-"} {print $1} END{}')
    invalid_fw_version=$(is_invalid_fw_version "$version")
    if [ "$invalid_fw_version" = "true" ]; then
        write_log "get invalid fw version"
    fi

    cd "$fw_dir" || { write_log "failed to enter fw directory: $fw_dir"; return 1; }
    for f in "${hw_version}"*.bin; do
        [ -e "$f" ] || break
        count=$((count + 1))
        fw_bin="$f"
    done
    if [ "$count" -ne 1 ]; then
        write_log "expected single firmware file for $hw_version, found $count"
        return 1
    fi
    tmp=${fw_bin%.*}
    orig_fw_version=${version:18:7}
    target_fw_version=${tmp:18:7}
    if [ "$invalid_fw_version" = "true" ] || [ "$force_update" = "true" ] || [ "$target_fw_version" != "$orig_fw_version" ]; then
        ret_fw_bin="$fw_dir/$fw_bin"
        write_log "old version: $version, will update: $ret_fw_bin"
    fi
    echo "$ret_fw_bin"
    return 0
}

startup_app()
{
    local ret
    local output
    local tty_path=$1
    output=$("$PROG" -i "$tty_path" -s)
    ret=$?
    if [ "$ret" -ne "$RET_SUCCESS" ]; then
        write_log "$output"
        write_log "startup $tty_path fail, ret=$ret"
    else
        write_log "startup app success"
    fi
    echo "$ret"
}

reset_mcus_to_bootloader()
{
    [ -x "$RESET_BIN" ] || {
        write_log "missing mcu_reset helper: $RESET_BIN"
        return 1
    }
    "$RESET_BIN" enable || {
        write_log "mcu_reset enable failed"
        return 1
    }
    [ -r "$GPIO_PWR_VAL" ] || {
        write_log "mcu power gpio missing after enable: $GPIO_PWR_VAL"
        return 1
    }
    "$RESET_BIN" || {
        write_log "mcu_reset power cycle failed"
        return 1
    }
    if [ "$(cat "$GPIO_PWR_VAL" 2>/dev/null)" != "0" ]; then
        write_log "mcu power gpio did not return low after reset"
        return 1
    fi
    sleep 1
    write_log "mcu power-reset complete"
    return 0
}

fw_update()
{
    local ret
    local output
    local tty_path=$1
    local fw_path=$2
    output=$("$PROG" -i "$tty_path" -u -f "$fw_path")
    ret=$?
    if [ "$ret" -ne "$RET_SUCCESS" ]; then
        write_log "$output"
        write_log "fw_update $tty_path fail, $fw_path, ret=$ret"
    else
        write_log "fw_update success"
    fi
    echo "$ret"
}

probe_single_mcu()
{
    local tty_path=$1
    local expected_hw=$2
    local fw_dir=$3
    local force_update=$4
    local label=$5
    local ret
    local version
    local hw_actual
    local fw_bin

    [ -e "$tty_path" ] || {
        write_log "$label serial device missing: $tty_path"
        return 1
    }

    ret=$(mcu_handshake "$tty_path")
    [ "$ret" -eq "$RET_SUCCESS" ] || return 2

    version=$(get_version "$tty_path")
    [ "$version" != "unknown" ] || return 2

    hw_actual=$(echo "$version" | awk 'BEGIN{FS="-"} {print $1} END{}')
    if [ "$hw_actual" != "$expected_hw" ]; then
        write_log "$label hardware mismatch: got '$hw_actual', expected '$expected_hw'"
        return 1
    fi

    fw_bin=$(compare_version "$version" "$fw_dir" "$force_update") || return 1
    printf '%s|%s\n' "$version" "$fw_bin"
    return 0
}

apply_single_mcu()
{
    local tty_path=$1
    local cur_version=$2
    local fw_bin=$3
    local label=$4
    local ret
    local target_ver

    if [ "x$fw_bin" != "x" ]; then
        # Stock mcu_util -u verifies flash via bootloader protocol and auto-starts the app;
        # querying bootloader handshake immediately after -u fails because app is already running.
        ret=$(fw_update "$tty_path" "$fw_bin")
        [ "$ret" -eq "$RET_SUCCESS" ] || return 1
        target_ver=$(basename "$fw_bin" .bin)
        echo "$target_ver"
    else
        ret=$(startup_app "$tty_path")
        [ "$ret" -eq "$RET_SUCCESS" ] || return 1
        echo "$cur_version"
    fi
    return 0
}

auto_update()
{
    # Isolated two-phase transition for F008 MAIN and NOZZLE only;
    # RS-485 bus updater is intentionally omitted to protect CFS 1.1.3.
    . /usr/share/libubox/jshn.sh

    local model=$(get_sn_mac.sh model)
    local board=$(get_sn_mac.sh board)
    local structure_version=$(get_sn_mac.sh structure_version)
    local fw_dir="$FW_ROOT_DIR/F008"
    local mcu0_serial=/dev/ttyS2
    local noz0_serial=/dev/ttyS3
    local mcu0_force=false
    local noz0_force=false
    local mcu0_probe=""
    local noz0_probe=""
    local mcu0_cur_ver=""
    local mcu0_fw_bin=""
    local noz0_cur_ver=""
    local noz0_fw_bin=""
    local mcu0_version=""
    local noz0_version=""
    local attempt
    local probe_rc
    local apply_rc=0

    if [ "$model" != "F008" ] || [ "x$structure_version" != "x0" ]; then
        write_log "unsupported printer identity model=$model struct=$structure_version"
        exit 1
    fi

    case "$board" in
        CR0CN240110C10|CR0CN240319C13|CR0CN240319C14) ;;
        *)
            write_log "unsupported board: $board"
            exit 1
            ;;
    esac

    [ -d "$fw_dir" ] || {
        write_log "missing fw directory: $fw_dir"
        exit 1
    }

    case "${FORCE:-}" in
        mcu0) mcu0_force=true ;;
        noz0) noz0_force=true ;;
        all)
            mcu0_force=true
            noz0_force=true
            ;;
    esac

    # A bootloader transport error is retryable while neither MCU has been written.
    for attempt in 1 2; do
        reset_mcus_to_bootloader || exit 1
        probe_rc=0
        mcu0_probe=$(probe_single_mcu "$mcu0_serial" "mcu0_140_G32" "$fw_dir" "$mcu0_force" "mcu0") || probe_rc=$?
        if [ "$probe_rc" -eq 0 ]; then
            noz0_probe=$(probe_single_mcu "$noz0_serial" "noz0_130_G30" "$fw_dir" "$noz0_force" "noz0") || probe_rc=$?
        fi
        [ "$probe_rc" -eq 0 ] && break
        [ "$probe_rc" -eq 2 ] && [ "$attempt" -eq 1 ] || exit 1
        write_log "bootloader probe failed; retrying both probes after one power cycle"
    done

    mcu0_cur_ver=${mcu0_probe%%|*}
    mcu0_fw_bin=${mcu0_probe#*|}
    noz0_cur_ver=${noz0_probe%%|*}
    noz0_fw_bin=${noz0_probe#*|}

    # Phase 2: Flash or start app on both MCUs; fail service startup if either fails.
    mcu0_version=$(apply_single_mcu "$mcu0_serial" "$mcu0_cur_ver" "$mcu0_fw_bin" "mcu0") || apply_rc=1
    noz0_version=$(apply_single_mcu "$noz0_serial" "$noz0_cur_ver" "$noz0_fw_bin" "noz0") || apply_rc=1
    [ "$apply_rc" -eq 0 ] || exit 1

    json_init
    json_add_object "mcu0"
    json_add_string "serial" "$mcu0_serial"
    json_add_string "version" "$mcu0_version"
    json_close_object
    json_add_object "noz0"
    json_add_string "serial" "$noz0_serial"
    json_add_string "version" "$noz0_version"
    json_close_object
    json_dump > "$VERSION_FILE"
    json_cleanup
}

start_service()
{
    [ -x "$PROG" ] || {
        echo "not found $PROG!"
        exit 1
    }
    auto_update
}
"""


def patch_klipper_mcu_init(original: str) -> str:
    """Use absolute /usr/bin/klipper_mcu path so upstream execv(argv[0]) works."""
    relative = "procd_set_param command klipper_mcu -r"
    absolute = "procd_set_param command /usr/bin/klipper_mcu -r"
    lines = original.splitlines(keepends=True)
    cmd_lines = [
        idx for idx, line in enumerate(lines)
        if line.lstrip().startswith("procd_set_param command ")
    ]
    if len(cmd_lines) != 1:
        raise ValueError(f"Expected 1 procd command line in klipper_mcu init, got {len(cmd_lines)}")
    idx = cmd_lines[0]
    stripped = lines[idx].strip()
    if stripped == absolute:
        return original
    if stripped != relative:
        raise ValueError(f"Unexpected klipper_mcu command line: {stripped!r}")
    lines[idx] = lines[idx].replace(relative, absolute, 1)
    return "".join(lines)


def patch_klipper_init(original: str) -> str:
    """Remove async mcu_reset race and route copy_config() through k2_sync_config.py."""
    if original.count("\n    mcu_reset\n") != 1:
        raise ValueError("Expected single mcu_reset invocation in etc/init.d/klipper")
    updated = original.replace(
        "\n    mcu_reset\n",
        "\n    # S54 already manages /usr/bin/klipper_mcu; skip async mcu_reset so its PTY stays intact.\n",
        1,
    )
    matches = _SYNC_BLOCK_PATTERN.findall(updated)
    if len(matches) != 1:
        raise ValueError(f"Expected 1 copy_config sync block in etc/init.d/klipper, found {len(matches)}")
    updated = _SYNC_BLOCK_PATTERN.sub(lambda _m: _SYNC_REPLACEMENT, updated, count=1)
    return updated


def patch_mcu_update_init(original: str) -> str:
    """Replace stock F008 mcu_update with isolated MAIN+NOZZLE updater (excluding mcu_util_485)."""
    required_anchors = (
        "PROG=/usr/bin/mcu_util",
        "PROG_485=/usr/bin/mcu_util_485",
        "compare_version()",
        "auto_update()",
    )
    for anchor in required_anchors:
        if anchor not in original:
            raise ValueError(f"Expected stock mcu_update anchor missing: {anchor!r}")
    return _SAFE_MCU_UPDATE_SCRIPT


_UPSTREAM_MOONRAKER_INIT_SCRIPT = """#!/bin/sh /etc/rc.common

START=56
USE_PROCD=1
DEPEND=fstab,mcu_update

PROG=/usr/share/moonraker-env/bin/python
MOONRAKER_DIR=/usr/share/moonraker
USER_DATA=/mnt/UDISK/printer_data
CONF=${USER_DATA}/config/moonraker.conf
DEFAULT_CONF=${MOONRAKER_DIR}/moonraker.conf
ASVC=${USER_DATA}/moonraker.asvc
DEFAULT_ASVC=${MOONRAKER_DIR}/moonraker.asvc
LOG=${USER_DATA}/logs/moonraker.log

seed_if_missing()
{
    local src="$1"
    local dst="$2"
    if [ ! -s "$dst" ] && [ -f "$src" ]; then
        cp -f "$src" "${dst}.tmp" && sync && mv "${dst}.tmp" "$dst" && sync
    fi
}

start_service() {

    [ "x$(get_sn_mac.sh pcba_test)" = "x1" ] && exit 0

    mkdir -p "${USER_DATA}/config" "${USER_DATA}/logs" "${USER_DATA}/gcodes" "${USER_DATA}/database" "${USER_DATA}/comms"
    seed_if_missing "$DEFAULT_CONF" "$CONF"
    seed_if_missing "$DEFAULT_ASVC" "$ASVC"

    procd_open_instance
    procd_set_param env HOME=/mnt/UDISK
    procd_set_param command ${PROG} -B ${MOONRAKER_DIR}/moonraker/moonraker.py -d ${USER_DATA} -c ${CONF} -l ${LOG}
    procd_close_instance
}
"""


def patch_moonraker_init(original: str) -> str:
    """Point OpenWrt procd moonraker service at upstream Moonraker v0.10.0 and writable UDISK config."""
    required_anchors = (
        "START=56",
        "PROG=/usr/share/moonraker-env/bin/python",
        "CONF=/usr/share/moonraker/moonraker.conf",
    )
    for anchor in required_anchors:
        if anchor not in original:
            raise ValueError(f"Expected stock moonraker init anchor missing: {anchor!r}")
    return _UPSTREAM_MOONRAKER_INIT_SCRIPT
