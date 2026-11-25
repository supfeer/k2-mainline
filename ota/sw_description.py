"""SWUpdate libconfig sw-description generator for Creality K2 Plus A/B images."""

from __future__ import annotations

import re

FORBIDDEN_TOKENS = ("uboot", "awuboot", "boot0", "upgrade_recovery", "recovery")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")


def render_sw_description(
    package_version: str,
    target_version: str,
    *,
    description: str = "K2 Plus Mainline Upstream Klipper v0.14.0",
    postinstall_script: str = "postinstall.sh",
) -> bytes:
    """Build SWUpdate 2019.11 sw-description with A/B boot/rootfs slots and pre/postinstall hooks."""
    for label, value in (("package_version", package_version), ("target_version", target_version)):
        if not VERSION_RE.match(value):
            raise ValueError(f"Invalid {label}: {value!r}")
    if any(ch in description for ch in ('"', "\\", "\n", "\r")):
        raise ValueError("Invalid characters in sw-description description string")
    if not re.match(r"^[A-Za-z0-9._-]+$", postinstall_script):
        raise ValueError(f"Invalid postinstall_script name: {postinstall_script!r}")

    slot_blocks: list[str] = []
    for current, target in (("A", "B"), ("B", "A")):
        mode_name = f"now_{current}_next_{target}"
        # Do NOT emit sw-description bootenv here: postinstall commits and verifies
        # U-Boot slot env atomically via fw_setenv -s BEFORE touching /overlay/upper or configs.
        slot_blocks.append(
            f"""        {mode_name} = {{
            images: (
                {{
                    filename = "kernel";
                    device = "/dev/by-name/boot{target}";
                    installed-directly = true;
                }},
                {{
                    filename = "rootfs";
                    device = "/dev/by-name/rootfs{target}";
                    installed-directly = true;
                }}
            );
            scripts: (
                {{
                    filename = "preinstall.sh";
                    type = "preinstall";
                    data = "{mode_name}";
                }},
                {{
                    filename = "{postinstall_script}";
                    type = "postinstall";
                    data = "{mode_name}";
                }}
            );
        }};"""
        )

    text = (
        f'software = {{\n'
        f'    version = "{package_version}";\n'
        f'    description = "{description}";\n'
        f'    stable = {{\n'
        + "\n".join(slot_blocks)
        + "\n    };\n};\n"
    )
    lowered = text.lower()
    if "sha256" in lowered:
        raise AssertionError("Vendor SWUpdate 2019.11 rejects sha256 properties")
    for token in FORBIDDEN_TOKENS:
        if token in lowered:
            raise AssertionError(f"Forbidden token in sw-description: {token}")
    return text.encode("ascii")
