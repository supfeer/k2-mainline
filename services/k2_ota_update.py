#!/usr/share/klippy-env/bin/python
"""K2 Plus mainline GitHub Releases OTA updater CLI and local screen proxy."""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_REPO = "supfeer/k2-mainline"
DEFAULT_BOARD = "CR0CN240110C10"
DEFAULT_MODEL = "F008"
DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 7129
DEFAULT_CACHE_TTL_SEC = 300
DEFAULT_CONFIG_PATH = Path("/mnt/UDISK/printer_data/config/k2_ota.conf")
DEFAULT_UPGRADE_DIR = Path("/mnt/UDISK/creality/upgrade")
DEFAULT_LOCAL_OTA_BIN = Path("/etc/ota_bin/local_ota_update.sh")
DEFAULT_KLIPPY_UDS = Path("/tmp/klippy_uds")

# Tail bytes held back during streaming until SHA-256 and CRC64-ECMA match manifest.
VERIFY_TAIL_BYTES = 64
STREAM_CHUNK_BYTES = 64 * 1024
MIN_EXTRA_FREE_BYTES = 512 * 1024 * 1024

_FILENAME_RE = re.compile(r"^CR0CN240110C10_ota_img_V(\d+\.\d+\.\d+\.\d+)\.img$")
_VERSION_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MD5_RE = re.compile(r"^[0-9a-f]{32}$")
_CRC64_POLY_REV = 0xC96C5795D7870F42


def _build_crc64_table() -> tuple[int, ...]:
    table: list[int] = []
    for i in range(256):
        crc = i
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ _CRC64_POLY_REV
            else:
                crc >>= 1
        table.append(crc)
    return tuple(table)


_CRC64_TABLE = _build_crc64_table()


def crc64_ecma_update(crc: int, data: bytes) -> int:
    """Update AlibabaCloud::OSS::CRC64 (ECMA-182 reflected) with a chunk."""
    c = (crc ^ 0xFFFFFFFFFFFFFFFF) & 0xFFFFFFFFFFFFFFFF
    tbl = _CRC64_TABLE
    for b in data:
        c = tbl[(c ^ b) & 0xFF] ^ (c >> 8)
    return (c ^ 0xFFFFFFFFFFFFFFFF) & 0xFFFFFFFFFFFFFFFF


def crc64_ecma_bytes(data: bytes) -> int:
    """Compute AlibabaCloud::OSS::CRC64 for an in-memory buffer."""
    return crc64_ecma_update(0, data)


def crc64_ecma_file(path: Path) -> int:
    """Compute AlibabaCloud::OSS::CRC64 for a file on disk."""
    crc = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(256 * 1024)
            if not chunk:
                break
            crc = crc64_ecma_update(crc, chunk)
    return crc


def version_to_int64(ver: str) -> int:
    """Match libcrealitycloud.so util::VersionStringToInt64(char*)."""
    matches = re.findall(r"[0-9]{1,3}", ver or "")[:8]
    total = 0
    for idx, tok in enumerate(matches):
        shift = 56 - 8 * idx
        total = (total + (int(tok) << shift)) & 0xFFFFFFFFFFFFFFFF
    return total


def version_tuple(ver: str) -> tuple[int, ...]:
    """Parse dotted numeric version into integer tuple for strict comparison."""
    parts = re.findall(r"[0-9]+", ver or "")
    return tuple(int(p) for p in parts)


def is_newer_version(candidate: str, current: str) -> bool:
    """Return True only when candidate is strictly newer in both tuple and screen int64 order."""
    if not candidate or not _VERSION_RE.match(candidate):
        return False
    if not current:
        return True
    return version_tuple(candidate) > version_tuple(current) and version_to_int64(candidate) > version_to_int64(current)


def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Load OTA settings from optional key=value config file and environment overrides."""
    cfg: dict[str, Any] = {
        "enabled": True,
        "repo": DEFAULT_REPO,
        "manifest_url": "",
        "listen_host": DEFAULT_LISTEN_HOST,
        "listen_port": DEFAULT_LISTEN_PORT,
        "cache_ttl": DEFAULT_CACHE_TTL_SEC,
        "allow_http": False,
    }
    if config_path.is_file():
        try:
            for raw in config_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith(("#", ";", "[")):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                key = k.strip().lower()
                val = v.strip().strip('"').strip("'")
                if key == "enabled":
                    cfg["enabled"] = val.lower() not in ("0", "false", "no", "off")
                elif key == "repo" and val:
                    cfg["repo"] = val
                elif key == "manifest_url":
                    cfg["manifest_url"] = val
                elif key == "listen_port" and val.isdigit():
                    cfg["listen_port"] = int(val)
                elif key == "cache_ttl" and val.isdigit():
                    cfg["cache_ttl"] = max(5, int(val))
        except OSError:
            pass

    if os.environ.get("K2_OTA_ENABLED"):
        cfg["enabled"] = os.environ["K2_OTA_ENABLED"].strip().lower() not in ("0", "false", "no", "off")
    if os.environ.get("K2_OTA_REPO"):
        cfg["repo"] = os.environ["K2_OTA_REPO"].strip()
    if os.environ.get("K2_OTA_MANIFEST_URL"):
        cfg["manifest_url"] = os.environ["K2_OTA_MANIFEST_URL"].strip()
    if os.environ.get("K2_OTA_PORT", "").isdigit():
        cfg["listen_port"] = int(os.environ["K2_OTA_PORT"])
    if os.environ.get("K2_OTA_CACHE_TTL", "").isdigit():
        cfg["cache_ttl"] = max(1, int(os.environ["K2_OTA_CACHE_TTL"]))
    if os.environ.get("K2_OTA_ALLOW_HTTP", "").strip() == "1":
        cfg["allow_http"] = True

    if not cfg["manifest_url"]:
        cfg["manifest_url"] = (
            f"https://github.com/{cfg['repo']}/releases/latest/download/manifest.json"
        )
    return cfg


def _validate_url(url: str, *, allow_http: bool) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https":
        if not parsed.netloc:
            raise ValueError(f"Invalid URL host: {url!r}")
        return url
    if allow_http and parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost"):
        return url
    raise ValueError(f"Refusing insecure or unsupported URL: {url!r}")


def validate_manifest(raw: Any, *, allow_http: bool = False) -> dict[str, Any]:
    """Validate GitHub Releases manifest.json schema and security invariants."""
    if not isinstance(raw, dict):
        raise ValueError("Manifest root must be a JSON object")
    if raw.get("schema_version") != 1:
        raise ValueError(f"Unsupported manifest schema_version: {raw.get('schema_version')!r}")

    version = str(raw.get("version", "")).strip()
    if not _VERSION_RE.match(version):
        raise ValueError(f"Invalid manifest version: {version!r}")

    board = str(raw.get("board", "")).strip()
    model = str(raw.get("model", "")).strip()
    if board != DEFAULT_BOARD or model != DEFAULT_MODEL:
        raise ValueError(f"Unsupported manifest target board={board!r} model={model!r}")

    filename = str(raw.get("filename", "")).strip()
    m = _FILENAME_RE.match(filename)
    if not m or m.group(1) != version:
        raise ValueError(f"Manifest filename {filename!r} does not match version {version!r}")

    size = raw.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= VERIFY_TAIL_BYTES:
        raise ValueError(f"Invalid manifest size: {size!r}")

    sha256_hex = str(raw.get("sha256", "")).strip().lower()
    if not _SHA256_RE.match(sha256_hex):
        raise ValueError(f"Invalid manifest sha256: {sha256_hex!r}")

    md5_hex = str(raw.get("md5", "")).strip().lower()
    if md5_hex and not _MD5_RE.match(md5_hex):
        raise ValueError(f"Invalid manifest md5: {md5_hex!r}")

    crc64_raw = str(raw.get("crc64ecma", "")).strip()
    if not crc64_raw.isdigit() or int(crc64_raw) > 0xFFFFFFFFFFFFFFFF:
        raise ValueError(f"Invalid manifest crc64ecma: {crc64_raw!r}")

    download_url = _validate_url(str(raw.get("download_url", "")).strip(), allow_http=allow_http)

    notes_raw = raw.get("release_notes", [])
    notes: list[str] = []
    if isinstance(notes_raw, list):
        notes = [str(item) for item in notes_raw if str(item)]
    elif isinstance(notes_raw, str) and notes_raw.strip():
        notes = [notes_raw.strip()]
    if not notes:
        notes = [f"K2 Mainline v{version}"]

    return {
        "schema_version": 1,
        "version": version,
        "tag": str(raw.get("tag", f"v{version}")).strip(),
        "repo": str(raw.get("repo", DEFAULT_REPO)).strip(),
        "board": board,
        "model": model,
        "filename": filename,
        "size": size,
        "sha256": sha256_hex,
        "md5": md5_hex,
        "crc64ecma": crc64_raw,
        "download_url": download_url,
        "release_notes": notes,
        "published_at": str(raw.get("published_at", "")).strip(),
    }


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    for ca_bundle in (
        os.environ.get("SSL_CERT_FILE", ""),
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/ssl/cert.pem",
    ):
        if ca_bundle and Path(ca_bundle).is_file():
            ctx.load_verify_locations(cafile=ca_bundle)
            break
    return ctx


def fetch_manifest(manifest_url: str, *, timeout: float = 15.0, allow_http: bool = False) -> dict[str, Any]:
    """Download and validate manifest.json from GitHub Releases."""
    url = _validate_url(manifest_url, allow_http=allow_http)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "k2-mainline-ota/1.0",
            "Accept": "application/json",
        },
    )
    ctx = _ssl_context() if url.startswith("https://") else None
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        data = resp.read(256 * 1024)
    return validate_manifest(json.loads(data.decode("utf-8")), allow_http=allow_http)


def read_local_version() -> str:
    """Read currently installed firmware version from U-Boot env."""
    override = os.environ.get("K2_OTA_CURRENT_VERSION", "").strip()
    if override:
        return override
    try:
        out = subprocess.check_output(
            ["fw_printenv", "-n", "version"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
        ).strip()
        return out
    except (OSError, subprocess.SubprocessError):
        return ""


def read_local_board() -> str:
    override = os.environ.get("K2_OTA_BOARD", "").strip()
    if override:
        return override
    try:
        return subprocess.check_output(
            ["fw_printenv", "-n", "board"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def check_klipper_idle(uds_path: Path = DEFAULT_KLIPPY_UDS) -> None:
    """Raise RuntimeError if Klipper is actively printing or moving."""
    if not uds_path.is_socket():
        return
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        sock.connect(str(uds_path))
        req = (
            json.dumps(
                {
                    "id": 9102,
                    "method": "objects/query",
                    "params": {"objects": {"print_stats": None, "idle_timeout": None}},
                }
            ).encode("utf-8")
            + b"\x03"
        )
        sock.sendall(req)
        buf = bytearray()
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
            if 3 in chunk:
                break
    except OSError:
        return
    finally:
        sock.close()

    if not buf:
        return
    payload = json.loads(buf.split(b"\x03", 1)[0].decode("utf-8", "replace"))
    status = payload.get("result", {}).get("status", {})
    if (
        status.get("print_stats", {}).get("state") == "printing"
        or status.get("idle_timeout", {}).get("state") == "Printing"
    ):
        raise RuntimeError("Printer is actively printing; refusing OTA update")


def download_and_verify_image(
    manifest: dict[str, Any],
    dest_dir: Path,
    *,
    allow_http: bool = False,
    timeout: float = 30.0,
) -> Path:
    """Download .img to temporary path, verify exact size, SHA-256, and CRC64-ECMA, then rename."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    final_path = dest_dir / manifest["filename"]
    tmp_path = dest_dir / f".{manifest['filename']}.part"

    # Reuse already verified image if present.
    if final_path.is_file() and final_path.stat().st_size == manifest["size"]:
        h_exist = hashlib.sha256()
        crc_exist = 0
        with final_path.open("rb") as fh:
            while True:
                chunk = fh.read(256 * 1024)
                if not chunk:
                    break
                h_exist.update(chunk)
                crc_exist = crc64_ecma_update(crc_exist, chunk)
        if h_exist.hexdigest() == manifest["sha256"] and str(crc_exist) == manifest["crc64ecma"]:
            return final_path

    st = os.statvfs(str(dest_dir))
    free_bytes = st.f_bavail * st.f_frsize
    needed = manifest["size"] + MIN_EXTRA_FREE_BYTES
    if free_bytes < needed:
        raise RuntimeError(
            f"Insufficient free space in {dest_dir}: {free_bytes} < {needed} bytes"
        )

    url = _validate_url(manifest["download_url"], allow_http=allow_http)
    req = urllib.request.Request(url, headers={"User-Agent": "k2-mainline-ota/1.0"})
    ctx = _ssl_context() if url.startswith("https://") else None

    sha = hashlib.sha256()
    crc = 0
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp, tmp_path.open("wb") as out:
            while True:
                chunk = resp.read(STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > manifest["size"]:
                    raise RuntimeError(
                        f"Downloaded payload exceeded manifest size ({total} > {manifest['size']})"
                    )
                out.write(chunk)
                sha.update(chunk)
                crc = crc64_ecma_update(crc, chunk)
            out.flush()
            os.fsync(out.fileno())

        if total != manifest["size"]:
            raise RuntimeError(f"Size mismatch: got {total}, expected {manifest['size']}")
        if sha.hexdigest() != manifest["sha256"]:
            raise RuntimeError(
                f"SHA-256 mismatch: got {sha.hexdigest()}, expected {manifest['sha256']}"
            )
        if str(crc) != manifest["crc64ecma"]:
            raise RuntimeError(
                f"CRC64-ECMA mismatch: got {crc}, expected {manifest['crc64ecma']}"
            )
        os.replace(tmp_path, final_path)
        return final_path
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


class ManifestCache:
    """Thread-safe TTL cache for GitHub Releases manifest.json."""

    def __init__(self, manifest_url: str, *, ttl_sec: int, allow_http: bool) -> None:
        self._manifest_url = manifest_url
        self._ttl_sec = ttl_sec
        self._allow_http = allow_http
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._fetched_at: float = 0.0

    def get(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if not force and self._cached is not None and (now - self._fetched_at) < self._ttl_sec:
                return self._cached
        fresh = fetch_manifest(self._manifest_url, allow_http=self._allow_http)
        with self._lock:
            self._cached = fresh
            self._fetched_at = time.monotonic()
        return fresh


def build_creality_no_update_response() -> dict[str, Any]:
    return {
        "code": 0,
        "msg": "ok",
        "reqId": "k2-mainline-ota",
        "result": {"count": 0, "list": []},
    }


def build_creality_firmware_response(
    request_body: dict[str, Any],
    manifest: dict[str, Any] | None,
    *,
    proxy_base_url: str,
    fallback_current_version: str = "",
) -> dict[str, Any]:
    """Translate GitHub Releases manifest into Creality /api/cxy/v2/firmware/list response."""
    if manifest is None:
        return build_creality_no_update_response()

    items = request_body.get("items")
    if not isinstance(items, list) or not items:
        return build_creality_no_update_response()

    matched_current: str | None = None
    for item in items:
        if not isinstance(item, dict):
            continue
        # Only respond to main Linux system firmware checks (type=2, subType=F008).
        # Explicitly return empty list for CFS (type=27) and all other targets.
        if item.get("type") != 2:
            continue
        sub_type = str(item.get("subType", "")).strip()
        hw_ver = str(item.get("hardwareVersionNumber", "")).strip()
        if sub_type and sub_type != manifest["model"]:
            continue
        if hw_ver and hw_ver != manifest["board"]:
            continue
        matched_current = str(item.get("firmwareVersion", "")).strip() or fallback_current_version
        break

    if matched_current is None:
        return build_creality_no_update_response()

    if not is_newer_version(manifest["version"], matched_current):
        return build_creality_no_update_response()

    download_url = f"{proxy_base_url.rstrip('/')}/download/{manifest['filename']}"
    return {
        "code": 0,
        "msg": "ok",
        "reqId": "k2-mainline-ota",
        "result": {
            "count": 1,
            "list": [
                {
                    "id": f"k2-mainline-{manifest['version']}",
                    "type": 2,
                    "name": manifest["filename"],
                    "versionNumber": manifest["version"],
                    "hardwareVersionNumber": manifest["board"],
                    "downloadUrl": download_url,
                    "firmwareSize": manifest["size"],
                    "isForce": False,
                    "updateDescription": manifest["release_notes"],
                    "ignoreOta": False,
                }
            ],
        },
    }


def make_proxy_server(cfg: dict[str, Any]) -> http.server.ThreadingHTTPServer:
    """Create loopback HTTP proxy server speaking Creality's OTA protocol."""
    cache = ManifestCache(
        cfg["manifest_url"],
        ttl_sec=int(cfg["cache_ttl"]),
        allow_http=bool(cfg["allow_http"]),
    )

    class OtaProxyHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write(f"[k2-ota-proxy] {fmt % args}\n")
            sys.stderr.flush()

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/health":
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "enabled": bool(cfg["enabled"]),
                        "repo": cfg["repo"],
                        "manifest_url": cfg["manifest_url"],
                    },
                )
                return

            if parsed.path.startswith("/download/"):
                self._handle_download(parsed.path[len("/download/") :])
                return

            self._send_json(404, {"code": 404, "msg": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/api/cxy/v2/firmware/list":
                self._send_json(404, {"code": 404, "msg": "not found"})
                return

            length = int(self.headers.get("Content-Length", "0") or "0")
            if length < 0 or length > 64 * 1024:
                self._send_json(400, build_creality_no_update_response())
                return
            raw_body = self.rfile.read(length) if length else b"{}"
            try:
                req_json = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                req_json = {}

            if not cfg["enabled"]:
                self._send_json(200, build_creality_no_update_response())
                return

            manifest: dict[str, Any] | None = None
            try:
                manifest = cache.get()
            except Exception as exc:
                self.log_message("manifest fetch failed: %s", exc)
                self._send_json(200, build_creality_no_update_response())
                return

            host_port = f"http://{cfg['listen_host']}:{self.server.server_port}"
            resp = build_creality_firmware_response(
                req_json,
                manifest,
                proxy_base_url=host_port,
                fallback_current_version=read_local_version(),
            )
            self._send_json(200, resp)

        def _handle_download(self, requested_name: str) -> None:
            if not cfg["enabled"]:
                self._send_json(403, {"code": 403, "msg": "ota disabled"})
                return
            try:
                manifest = cache.get()
            except Exception as exc:
                self.log_message("download manifest fetch failed: %s", exc)
                self._send_json(502, {"code": 502, "msg": "manifest unavailable"})
                return

            if requested_name != manifest["filename"]:
                self._send_json(404, {"code": 404, "msg": "filename mismatch"})
                return

            upstream_url = _validate_url(manifest["download_url"], allow_http=bool(cfg["allow_http"]))
            req = urllib.request.Request(upstream_url, headers={"User-Agent": "k2-mainline-ota/1.0"})
            ctx = _ssl_context() if upstream_url.startswith("https://") else None

            try:
                upstream = urllib.request.urlopen(req, timeout=30.0, context=ctx)
            except Exception as exc:
                self.log_message("upstream open failed: %s", exc)
                self._send_json(502, {"code": 502, "msg": "upstream unavailable"})
                return

            with upstream:
                expected_size = int(manifest["size"])
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(expected_size))
                self.send_header(
                    "Content-Disposition",
                    f'attachment;filename="{manifest["filename"]}"',
                )
                self.send_header("x-oss-hash-crc64ecma", manifest["crc64ecma"])
                self.send_header("Connection", "close")
                self.end_headers()

                sha = hashlib.sha256()
                crc = 0
                total = 0
                tail = bytearray()

                # Hold back the last VERIFY_TAIL_BYTES bytes until EOF so a SHA-256 mismatch
                # aborts the HTTP transfer before Content-Length completes.
                while True:
                    chunk = upstream.read(STREAM_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > expected_size:
                        self.log_message(
                            "aborting stream: upstream exceeded expected size (%d > %d)",
                            total,
                            expected_size,
                        )
                        self.close_connection = True
                        return
                    sha.update(chunk)
                    crc = crc64_ecma_update(crc, chunk)
                    tail.extend(chunk)
                    if len(tail) > VERIFY_TAIL_BYTES:
                        emit = bytes(tail[:-VERIFY_TAIL_BYTES])
                        del tail[:-VERIFY_TAIL_BYTES]
                        try:
                            self.wfile.write(emit)
                        except OSError:
                            self.close_connection = True
                            return

                if (
                    total != expected_size
                    or sha.hexdigest() != manifest["sha256"]
                    or str(crc) != manifest["crc64ecma"]
                ):
                    self.log_message(
                        "aborting stream on integrity mismatch: size=%d/%d sha=%s/%s crc=%d/%s",
                        total,
                        expected_size,
                        sha.hexdigest(),
                        manifest["sha256"],
                        crc,
                        manifest["crc64ecma"],
                    )
                    self.close_connection = True
                    return

                if tail:
                    self.wfile.write(bytes(tail))
                    self.wfile.flush()

    class _ReusableThreadingHTTPServer(http.server.ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    return _ReusableThreadingHTTPServer(
        (str(cfg["listen_host"]), int(cfg["listen_port"])),
        OtaProxyHandler,
    )


def cmd_check(cfg: dict[str, Any], *, as_json: bool) -> int:
    current = read_local_version()
    manifest = fetch_manifest(cfg["manifest_url"], allow_http=bool(cfg["allow_http"]))
    update_available = is_newer_version(manifest["version"], current)
    result = {
        "current_version": current,
        "latest_version": manifest["version"],
        "update_available": update_available,
        "filename": manifest["filename"],
        "size": manifest["size"],
        "sha256": manifest["sha256"],
        "crc64ecma": manifest["crc64ecma"],
        "download_url": manifest["download_url"],
        "release_notes": manifest["release_notes"],
    }
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Current version : {current or 'unknown'}")
        print(f"Latest release  : {manifest['version']} ({manifest['filename']})")
        print(f"Update available: {'yes' if update_available else 'no'}")
    return 0


def cmd_apply(
    cfg: dict[str, Any],
    *,
    force: bool,
    no_reboot: bool,
    upgrade_dir: Path,
    local_ota_bin: Path,
) -> int:
    board = read_local_board()
    if board and board != DEFAULT_BOARD:
        raise RuntimeError(f"Unsupported board {board!r}; expected {DEFAULT_BOARD!r}")
    check_klipper_idle()

    current = read_local_version()
    manifest = fetch_manifest(cfg["manifest_url"], allow_http=bool(cfg["allow_http"]))
    if not force and not is_newer_version(manifest["version"], current):
        print(f"Already up to date (current={current}, latest={manifest['version']}).")
        return 0

    print(f"Downloading {manifest['filename']} ({manifest['size']} bytes)...")
    img_path = download_and_verify_image(
        manifest,
        upgrade_dir,
        allow_http=bool(cfg["allow_http"]),
    )
    print(f"Verified SHA-256 ({manifest['sha256']}) and CRC64-ECMA ({manifest['crc64ecma']}).")

    if not local_ota_bin.is_file():
        raise FileNotFoundError(f"Missing OTA installer script: {local_ota_bin}")

    print(f"Installing {img_path} via {local_ota_bin}...")
    proc = subprocess.run([str(local_ota_bin), str(img_path)], check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{local_ota_bin} exited with status {proc.returncode}")

    print("OTA update installed cleanly.")
    if not no_reboot:
        subprocess.run(["sync"], check=False)
        subprocess.run(["reboot"], check=False)
    return 0


def cmd_serve(cfg: dict[str, Any]) -> int:
    server = make_proxy_server(cfg)
    sys.stderr.write(
        f"[k2-ota-proxy] listening on http://{cfg['listen_host']}:{server.server_port} "
        f"-> {cfg['manifest_url']}\n"
    )
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Check GitHub Releases for a newer K2 Mainline image")
    mode.add_argument("--apply", action="store_true", help="Download, verify, and install the latest K2 Mainline image")
    mode.add_argument("--serve", action="store_true", help="Run loopback HTTP proxy for stock K2 Plus screen OTA")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Optional k2_ota.conf path")
    parser.add_argument("--manifest-url", default="", help="Override manifest.json URL")
    parser.add_argument("--port", type=int, default=0, help="Override proxy listen port (with --serve)")
    parser.add_argument("--json", action="store_true", help="Emit JSON output (with --check)")
    parser.add_argument("--force", action="store_true", help="Install even if version <= current (with --apply)")
    parser.add_argument("--no-reboot", action="store_true", help="Do not reboot automatically after install")
    parser.add_argument("--upgrade-dir", type=Path, default=DEFAULT_UPGRADE_DIR, help="Download target directory")
    parser.add_argument("--local-ota-bin", type=Path, default=DEFAULT_LOCAL_OTA_BIN, help="SWUpdate wrapper path")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.manifest_url:
        cfg["manifest_url"] = args.manifest_url
    if args.port > 0:
        cfg["listen_port"] = args.port

    if args.check:
        return cmd_check(cfg, as_json=args.json)
    if args.apply:
        return cmd_apply(
            cfg,
            force=args.force,
            no_reboot=args.no_reboot,
            upgrade_dir=args.upgrade_dir,
            local_ota_bin=args.local_ota_bin,
        )
    return cmd_serve(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
