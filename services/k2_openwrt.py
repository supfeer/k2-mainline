"""OpenWrt procd machine provider extension for upstream Moonraker."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .machine import BaseProvider

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from .klippy_connection import KlippyConnection
    from .machine import Machine

_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_DEFAULT_BASE_SERVICES = ("klipper", "klipper_mcu", "moonraker", "nginx", "go2rtc")
_ACTION_TIMEOUT_S = 45.0
_UBUS_TIMEOUT_S = 5.0


class OpenWrtProcdProvider(BaseProvider):
    """Manage OpenWrt procd services and power actions without editing upstream Moonraker."""

    def __init__(self, config: ConfigHelper, machine: Machine) -> None:
        super().__init__(config)
        self.machine = machine
        self.base_services = _DEFAULT_BASE_SERVICES
        self._init_dir = Path("/etc/init.d")
        self._ubus_bin = "/bin/ubus"
        self._reboot_bin = "/sbin/reboot"
        self._poweroff_bin = "/sbin/poweroff"

    def _service_script(self, service_name: str) -> Optional[Path]:
        if not _SERVICE_NAME_RE.fullmatch(service_name):
            return None
        init_root = self._init_dir.resolve()
        cand = self._init_dir / service_name
        if not cand.exists() or cand.is_symlink() or not cand.is_file():
            return None
        try:
            if cand.resolve().parent != init_root:
                return None
        except OSError:
            return None
        if not os.access(cand, os.X_OK):
            return None
        return cand

    def _managed_services(self) -> List[str]:
        candidates: List[str] = []
        for svc in tuple(self.base_services) + tuple(self.machine._allowed_services):
            if svc not in candidates and self._service_script(svc) is not None:
                candidates.append(svc)
        return candidates

    async def _query_ubus_services(self) -> Dict[str, Any]:
        if not (os.path.isfile(self._ubus_bin) and os.access(self._ubus_bin, os.X_OK)):
            return {}
        try:
            proc = await asyncio.create_subprocess_exec(
                self._ubus_bin,
                "call",
                "service",
                "list",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_UBUS_TIMEOUT_S
            )
        except Exception as err:
            logging.debug("ubus service query failed: %s", err)
            return {}
        if proc.returncode != 0 or not stdout:
            return {}
        try:
            data = json.loads(stdout.decode("utf-8", errors="replace"))
        except ValueError as err:
            logging.debug("Invalid ubus service list JSON: %s", err)
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _service_state_from_ubus(
        procd_data: Dict[str, Any], service_name: str
    ) -> Dict[str, str]:
        svc_entry = procd_data.get(service_name)
        if isinstance(svc_entry, dict):
            instances = svc_entry.get("instances")
            if isinstance(instances, dict):
                for inst in instances.values():
                    if isinstance(inst, dict) and inst.get("running") is True:
                        return {
                            "active_state": "active",
                            "sub_state": "running",
                        }
        return {
            "active_state": "inactive",
            "sub_state": "dead",
        }

    async def _refresh_services(self) -> None:
        procd_data = await self._query_ubus_services()
        updated: Dict[str, Dict[str, str]] = {}
        for svc in self._managed_services():
            updated[svc] = self._service_state_from_ubus(procd_data, svc)
        self.available_services = updated
        if "service_state" in self.machine.system_info:
            self.machine.system_info["available_services"] = list(updated.keys())
            self.machine.system_info["service_state"] = dict(updated)

    async def initialize(self) -> None:
        await self._refresh_services()

    def _spawn_detached(self, cmd: List[str]) -> None:
        subprocess.Popen(
            cmd,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )

    async def shutdown(self) -> None:
        script = f"sleep 0.5; sync; {self._poweroff_bin}"
        self._spawn_detached(["/bin/sh", "-c", script])

    async def reboot(self) -> None:
        script = f"sleep 0.5; sync; {self._reboot_bin}"
        self._spawn_detached(["/bin/sh", "-c", script])

    async def do_service_action(
        self, action: str, service_name: str
    ) -> None:
        if action not in ("start", "stop", "restart"):
            raise self.server.error(f"Unsupported service action: {action}", 400)
        if not _SERVICE_NAME_RE.fullmatch(service_name):
            raise self.server.error(f"Invalid service name: {service_name}", 400)
        if service_name != "moonraker" and not self.is_service_available(service_name):
            raise self.server.error(f"Service '{service_name}' not available", 400)
        script_path = self._service_script(service_name)
        if script_path is None:
            raise self.server.error(f"Service script missing: {service_name}", 404)

        if service_name == "moonraker":
            if action != "restart":
                raise self.server.error(
                    f"Service action '{action}' not available for moonraker", 400
                )
            self._spawn_detached(["/bin/sh", "-c", f"sleep 0.5; {script_path} restart"])
            return

        proc = await asyncio.create_subprocess_exec(
            str(script_path),
            action,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_ACTION_TIMEOUT_S
            )
        except asyncio.TimeoutError as err:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise self.server.error(
                f"Service action '{action}' timed out for {service_name}", 500
            ) from err
        if proc.returncode != 0:
            err_text = (stderr or stdout or b"").decode("utf-8", errors="replace").strip()
            raise self.server.error(
                f"Service '{service_name}' {action} failed (rc={proc.returncode}): {err_text}",
                500,
            )

        await self._refresh_services()
        if service_name in self.available_services:
            self.server.send_event(
                "machine:service_state_changed",
                {service_name: self.available_services[service_name]},
            )

    async def check_virt_status(self) -> Dict[str, Any]:
        return {
            "virt_type": "none",
            "virt_identifier": "none",
        }

    async def extract_service_info(
        self,
        service_name: str,
        pid: int,
        properties: Optional[List[str]] = None,
        raw: bool = False,
    ) -> Dict[str, Any]:
        procd_data = await self._query_ubus_services()
        state = self._service_state_from_ubus(procd_data, service_name)
        if state["active_state"] != "active" and service_name == "moonraker":
            state = {"active_state": "active", "sub_state": "running"}
        return {
            "unit_name": service_name,
            "active_state": state["active_state"],
            "sub_state": state["sub_state"],
            "manager": "openwrt_procd",
            "pid": pid,
        }


def load_component(config: ConfigHelper) -> OpenWrtProcdProvider:
    server = config.get_server()
    machine: Machine = server.lookup_component("machine")
    provider = OpenWrtProcdProvider(config, machine)
    machine.sys_provider = provider
    machine.provider_type = "openwrt_procd"
    machine.system_info["provider"] = "openwrt_procd"
    klippy_conn: KlippyConnection = server.lookup_component("klippy_connection")
    klippy_conn.remote_methods["shutdown_machine"] = provider.shutdown
    klippy_conn.remote_methods["reboot_machine"] = provider.reboot
    logging.info("Registered OpenWrt procd machine provider")
    return provider
