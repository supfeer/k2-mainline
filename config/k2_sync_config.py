"""Synchronize K2 Plus Klipper configs on boot while preserving user SAVE_CONFIG and customizations."""

from __future__ import annotations

import configparser
import math
import os
import re
import shutil
import sys
import time
from pathlib import Path

SAVE_CONFIG_MARKER = "#*# <---------------------- SAVE_CONFIG ---------------------->"
AUTOSAVE_CANONICAL_PREAMBLE = (
    "#*# <---------------------- SAVE_CONFIG ---------------------->\n"
    "#*# DO NOT EDIT THIS BLOCK OR BELOW. The contents are auto-generated.\n"
    "#*#\n"
)

VERSION_RE = re.compile(r"^\s*#\s*Version\s*:\s*(\S+)", re.IGNORECASE)
SAVE_SECTION_RE = re.compile(r"^#\*#[ \t]*\[([^\]]+)\][ \t]*(?:[#;].*)?$")
SAVE_OPTION_RE = re.compile(r"^#\*#[ \t]*([A-Za-z0-9_]+)[ \t]*([=:])")
SAVE_CONT_RE = re.compile(r"^#\*#[ \t]+[ \t\S]+")

REGULAR_SECTION_RE = re.compile(r"^[ \t]*\[([^\]]+)\]")
REGULAR_OPTION_RE = re.compile(r"^[ \t]*([A-Za-z0-9_]+)[ \t]*([=:])")

MANAGED_CONFIG_FILES = (
    "gcode_macro.cfg",
    "k2_print.cfg",
    "k2_cfs_tools.cfg",
    "k2_homing.cfg",
    "k2_load_cell.cfg",
    "k2_load_cell_homing.cfg",
    "k2_mcu_temperature.cfg",
)

USER_PRESERVED_FILES = frozenset({
    "box.cfg",
    "k2_variables.cfg",
})

OBSOLETE_SAVE_SECTIONS = frozenset({
    "prtouch",
    "prtouch_v2",
    "prtouch_v3",
    "temperature_sensor mcu_temp",
    "k2_lis2dw",
    "k2_feed_diagnostics",
    "k2_cfs_diagnostic",
})

ALLOWED_SAVE_SECTION_EXACT = frozenset({
    "extruder",
    "heater_bed",
    "input_shaper",
    "box",
    "auto_addr",
    "load_cell_probe",
    "z_align",
    "stepper_z",
    "stepper_z1",
    "bltouch",
    "probe",
    "motor_control",
})

ALLOWED_SAVE_SECTION_PREFIXES = (
    "bed_mesh ",
    "skew_correction ",
    "heater_generic ",
    "belt_mdl ",
)

OBSOLETE_STEPPER_Z_OPTIONS = frozenset({
    "position_endstop",
    "position_min",
    "position_max",
    "homing_speed",
    "second_homing_speed",
    "homing_retract_dist",
})

LEGACY_LOAD_CELL_MIN_RAW = -8388608
LEGACY_LOAD_CELL_MAX_RAW = 8388607
_INT_TOKEN_RE = re.compile(r"^[+-]?\d+$")


def parse_cfg_version(cfg_path: Path) -> str:
    """Return normalized config version token from the top header (e.g. '1.1.4' or '9.5.0')."""
    if not cfg_path.is_file():
        return "0.0.0"
    with cfg_path.open("r", encoding="utf-8", errors="replace") as fh:
        for _ in range(25):
            line = fh.readline()
            if not line:
                break
            match = VERSION_RE.match(line)
            if match:
                return match.group(1).lstrip("vV").strip()
    return "0.0.0"


def _is_allowed_save_section(section_name: str) -> bool:
    if section_name in OBSOLETE_SAVE_SECTIONS:
        return False
    if section_name in ALLOWED_SAVE_SECTION_EXACT:
        return True
    return any(section_name.startswith(prefix) for prefix in ALLOWED_SAVE_SECTION_PREFIXES)


def extract_sanitized_save_config(old_printer_cfg: str) -> tuple[str, dict[str, set[str]]]:
    """Extract sanitized SAVE_CONFIG block and map of saved section -> lowercase option names."""
    idx = old_printer_cfg.find(SAVE_CONFIG_MARKER)
    if idx < 0:
        return "", {}
    raw_tail = old_printer_cfg[idx:]
    kept_blocks: list[str] = []
    kept_options: dict[str, set[str]] = {}

    current_name: str | None = None
    header_line = ""
    # Each entry in current_opts: (opt_lower_or_none, raw_lines)
    current_opts: list[tuple[str | None, list[str]]] = []

    def flush_section() -> None:
        if current_name is None or not _is_allowed_save_section(current_name):
            return
        kept_entries: list[tuple[str, list[str]]] = []
        for opt_name, raw_lines in current_opts:
            if opt_name is None:
                continue
            if current_name == "stepper_z" and opt_name in OBSOLETE_STEPPER_Z_OPTIONS:
                continue
            kept_entries.append((opt_name, raw_lines))
        if not kept_entries:
            return
        if current_name.startswith("bed_mesh ") and not any(name == "version" for name, _ in kept_entries):
            return
        block_lines = [header_line]
        opt_set = kept_options.setdefault(current_name.lower(), set())
        for opt_name, raw_lines in kept_entries:
            opt_set.add(opt_name)
            block_lines.extend(raw_lines)
        block_lines.append("#*#\n")
        kept_blocks.append("".join(block_lines))

    lines = raw_tail.splitlines(keepends=True)
    # Skip preamble lines before the first section header; emit canonical header below.
    seen_section = False
    for line in lines:
        if not line.startswith("#*#"):
            # Stop or ignore non-#*# corruption inside SAVE_CONFIG tail.
            continue
        sec_match = SAVE_SECTION_RE.match(line)
        if sec_match:
            flush_section()
            current_name = sec_match.group(1).strip()
            header_line = f"#*# [{current_name}]\n"
            current_opts = []
            seen_section = True
            continue
        if not seen_section or current_name is None:
            continue
        opt_match = SAVE_OPTION_RE.match(line)
        if opt_match:
            opt_name = opt_match.group(1).strip().lower()
            current_opts.append((opt_name, [line if line.endswith("\n") else line + "\n"]))
            continue
        # Preserve continuation lines only when attached to an active option.
        stripped_body = line[3:]
        if (
            current_opts
            and current_opts[-1][0] is not None
            and stripped_body.startswith((" ", "\t"))
            and stripped_body.strip()
            and not stripped_body.lstrip().startswith(("#", ";"))
        ):
            current_opts[-1][1].append(line if line.endswith("\n") else line + "\n")

    flush_section()
    if not kept_blocks:
        return "", {}
    text = AUTOSAVE_CANONICAL_PREAMBLE + "".join(kept_blocks).rstrip("\n") + "\n"
    return text, kept_options


def sanitize_save_config(old_printer_cfg: str) -> str:
    """Return canonical sanitized SAVE_CONFIG tail text (empty string if nothing remains)."""
    text, _ = extract_sanitized_save_config(old_printer_cfg)
    return text


def extract_legacy_load_cell_calibration(old_lc_path: Path) -> list[tuple[str, str]]:
    """Extract validated legacy [load_cell_probe] calibration from old k2_load_cell.cfg."""
    if not old_lc_path.is_file():
        return []
    parser = configparser.RawConfigParser(strict=False, inline_comment_prefixes=(";", "#"))
    try:
        parser.read_string(old_lc_path.read_text(encoding="utf-8", errors="replace"), source=str(old_lc_path))
    except (OSError, configparser.Error):
        return []
    if not parser.has_section("load_cell_probe") or not parser.has_option("load_cell_probe", "reference_tare_counts"):
        return []
    raw_ref = parser.get("load_cell_probe", "reference_tare_counts").strip()
    if not _INT_TOKEN_RE.match(raw_ref):
        return []
    ref = int(raw_ref, 10)
    if not (LEGACY_LOAD_CELL_MIN_RAW < ref < LEGACY_LOAD_CELL_MAX_RAW):
        return []

    entries: list[tuple[str, str]] = []
    if parser.has_option("load_cell_probe", "counts_per_gram"):
        try:
            cpg = float(parser.get("load_cell_probe", "counts_per_gram").strip())
        except ValueError:
            cpg = float("nan")
        if math.isfinite(cpg) and cpg >= 1.0:
            entries.append(("counts_per_gram", f"{cpg:.5f}"))
    entries.append(("reference_tare_counts", str(ref)))
    if parser.has_option("load_cell_probe", "z_offset"):
        try:
            z_off = float(parser.get("load_cell_probe", "z_offset").strip())
        except ValueError:
            z_off = float("nan")
        if math.isfinite(z_off) and abs(z_off) <= 5.0:
            entries.append(("z_offset", f"{z_off:.3f}"))
    return entries


def _append_save_section(tail_text: str, section: str, entries: list[tuple[str, str]]) -> str:
    """Append one canonical SAVE_CONFIG section block to an extracted SAVE_CONFIG tail."""
    if not entries:
        return tail_text
    block = [f"#*# [{section}]\n"]
    for opt, val in entries:
        block.append(f"#*# {opt} = {val}\n")
    if not tail_text:
        return AUTOSAVE_CANONICAL_PREAMBLE + "".join(block)
    base = tail_text if tail_text.endswith("\n") else tail_text + "\n"
    if not base.endswith("#*#\n"):
        base += "#*#\n"
    return base + "".join(block)


def comment_saved_overrides(rom_text: str, saved_options: dict[str, set[str]]) -> str:
    """Comment out ROM defaults in printer.cfg that have surviving overrides in SAVE_CONFIG."""
    if not saved_options:
        return rom_text
    out_lines: list[str] = []
    cur_section: str | None = None
    commenting_continuation = False

    for line in rom_text.splitlines(keepends=True):
        stripped_ws = line.lstrip()
        if not stripped_ws.strip() or stripped_ws.startswith(("#", ";")):
            if not stripped_ws.strip():
                commenting_continuation = False
            out_lines.append(line)
            continue
        if line[0] in (" ", "\t"):
            if commenting_continuation:
                out_lines.append("# " + line)
            else:
                out_lines.append(line)
            continue
        commenting_continuation = False
        sec_match = REGULAR_SECTION_RE.match(line)
        if sec_match:
            cur_section = sec_match.group(1).strip().lower()
            out_lines.append(line)
            continue
        opt_match = REGULAR_OPTION_RE.match(line)
        if opt_match and cur_section and opt_match.group(1).strip().lower() in saved_options.get(cur_section, ()):
            commenting_continuation = True
            out_lines.append("# " + line)
            continue
        out_lines.append(line)
    return "".join(out_lines)


def _validate_merged_printer_cfg(merged_text: str) -> None:
    """Validate merged printer.cfg syntax using Klipper's regular + autosave section rules."""
    idx = merged_text.find(AUTOSAVE_CANONICAL_PREAMBLE)
    if idx >= 0:
        regular_data = merged_text[:idx]
        tail_raw = merged_text[idx + len(AUTOSAVE_CANONICAL_PREAMBLE) :]
        autosave_lines: list[str] = []
        for line in tail_raw.splitlines():
            if not line:
                continue
            if not line.startswith("#*#") or (len(line) >= 4 and not line.startswith("#*# ")):
                raise ValueError(f"Malformed SAVE_CONFIG line after migration: {line!r}")
            autosave_lines.append(line[4:])
        autosave_data = "\n".join(autosave_lines)
    else:
        regular_data = merged_text
        autosave_data = ""

    parser = configparser.RawConfigParser(strict=False, inline_comment_prefixes=(";", "#"))
    parser.read_string(regular_data, source="printer.cfg")
    if autosave_data.strip():
        auto_parser = configparser.RawConfigParser(strict=False, inline_comment_prefixes=(";", "#"))
        auto_parser.read_string(autosave_data, source="SAVE_CONFIG")


def _fsync_dir(dir_path: Path) -> None:
    try:
        fd = os.open(dir_path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_atomic(dest: Path, data: bytes, mode: int = 0o644) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_name(f".{dest.name}.tmp")
    try:
        with tmp_path.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, dest)
    except BaseException:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def _backup_user_configs(user_dir: Path, old_version: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    safe_ver = re.sub(r"[^A-Za-z0-9._-]", "_", old_version)
    backups_root = user_dir / "backups"
    backups_root.mkdir(parents=True, exist_ok=True)
    candidate = backups_root / f"backup-{safe_ver}-{stamp}"
    suffix = 1
    while candidate.exists():
        candidate = backups_root / f"backup-{safe_ver}-{stamp}-{suffix}"
        suffix += 1
    candidate.mkdir(parents=True, exist_ok=False)
    for cfg in sorted(user_dir.glob("*.cfg")):
        if cfg.is_file():
            shutil.copy2(cfg, candidate / cfg.name)
    _fsync_dir(candidate)
    return candidate


def _install_non_printer_configs(rom_dir: Path, user_dir: Path, *, overwrite_managed: bool) -> None:
    for name in MANAGED_CONFIG_FILES:
        src = rom_dir / name
        dst = user_dir / name
        if src.is_file() and (overwrite_managed or not dst.is_file() or dst.stat().st_size == 0):
            _write_atomic(dst, src.read_bytes())
    for cfg in sorted(rom_dir.glob("*.cfg")):
        if cfg.name == "printer.cfg" or cfg.name in MANAGED_CONFIG_FILES:
            continue
        dest = user_dir / cfg.name
        if not cfg.is_file():
            continue
        if cfg.name in USER_PRESERVED_FILES and dest.is_file() and dest.stat().st_size > 0:
            continue
        if not dest.exists() or dest.stat().st_size == 0:
            _write_atomic(dest, cfg.read_bytes())


def sync_configs(rom_dir: Path, user_dir: Path) -> str:
    """Sync ROM configs to user_dir cleanly, preserving user calibration, variables, and order."""
    if not rom_dir.is_dir():
        raise FileNotFoundError(f"Missing ROM config directory: {rom_dir}")
    user_dir.mkdir(parents=True, exist_ok=True)
    user_printer = user_dir / "printer.cfg"
    rom_printer = rom_dir / "printer.cfg"
    if not rom_printer.is_file():
        raise FileNotFoundError(f"Missing ROM printer.cfg: {rom_printer}")

    if not user_printer.is_file() or user_printer.stat().st_size == 0:
        rom_bytes = rom_printer.read_bytes()
        _validate_merged_printer_cfg(rom_bytes.decode("utf-8"))
        # Preserve existing non-empty user box.cfg / k2_variables.cfg even on fresh printer.cfg.
        _install_non_printer_configs(rom_dir, user_dir, overwrite_managed=True)
        # Commit versioned printer.cfg last.
        _write_atomic(user_printer, rom_bytes)
        _fsync_dir(user_dir)
        return "fresh"

    old_ver = parse_cfg_version(user_printer)
    new_ver = parse_cfg_version(rom_printer)
    if old_ver == new_ver:
        _install_non_printer_configs(rom_dir, user_dir, overwrite_managed=False)
        _fsync_dir(user_dir)
        return "unchanged"

    _backup_user_configs(user_dir, old_ver)
    old_text = user_printer.read_text(encoding="utf-8", errors="replace")
    rom_text = rom_printer.read_text(encoding="utf-8")
    preserved_tail, saved_opts = extract_sanitized_save_config(old_text)
    if "load_cell_probe" not in saved_opts:
        legacy_lc = extract_legacy_load_cell_calibration(user_dir / "k2_load_cell.cfg")
        if legacy_lc:
            preserved_tail = _append_save_section(preserved_tail, "load_cell_probe", legacy_lc)
            saved_opts["load_cell_probe"] = {opt for opt, _ in legacy_lc}
    adjusted_rom = comment_saved_overrides(rom_text, saved_opts)
    merged = adjusted_rom.rstrip() + "\n"
    if preserved_tail:
        merged += "\n" + preserved_tail.lstrip("\n")
    if not merged.endswith("\n"):
        merged += "\n"
    _validate_merged_printer_cfg(merged)

    # Write managed includes and missing side files first; write version-bumped printer.cfg last.
    _install_non_printer_configs(rom_dir, user_dir, overwrite_managed=True)
    _write_atomic(user_printer, merged.encode("utf-8"))
    _fsync_dir(user_dir)
    return f"upgraded:{old_ver}->{new_ver}"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        sys.stderr.write("Usage: k2_sync_config.py <rom_config_dir> <user_config_dir>\n")
        return 2
    action = sync_configs(Path(args[0]), Path(args[1]))
    print(f"k2_sync_config: {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
