#!/usr/bin/env python3
"""Fail-closed readiness audit for a clean Finance Radar replacement VPS."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


APP_PORTS = (18000, 18501, 18601)
CORE_COMMANDS = ("python3", "tar", "sha256sum", "systemctl", "curl")
EDGE_COMMANDS = ("nginx", "certbot", "openssl")
MIN_MEMORY_BYTES = 1024 * 1024 * 1024
MIN_DISK_BYTES = 4 * 1024 * 1024 * 1024


def valid_public_web_url(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.path.rstrip("/").endswith("/radar")
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    )


def required_disk_bytes(expected_unpacked_bytes: int) -> int:
    return max(MIN_DISK_BYTES, max(0, expected_unpacked_bytes) * 2 + 512 * 1024 * 1024)


def available_memory_bytes(meminfo: Path = Path("/proc/meminfo")) -> int:
    try:
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return 0
    return 0


def port_is_available(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def command_result(command: list[str]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (completed.stdout or completed.stderr or "").strip()
    return completed.returncode == 0, output[:500]


def collect_preflight(
    *,
    expected_unpacked_bytes: int,
    public_web_url: str,
    require_edge_tools: bool,
) -> dict[str, Any]:
    disk_root = Path("/opt") if Path("/opt").is_dir() else Path("/")
    disk = shutil.disk_usage(disk_root)
    memory = available_memory_bytes()
    required_disk = required_disk_bytes(expected_unpacked_bytes)
    architecture = platform.machine().lower()
    required_commands = CORE_COMMANDS + (EDGE_COMMANDS if require_edge_tools else ())
    commands = {name: shutil.which(name) for name in required_commands}
    units = sorted(str(path) for path in Path("/etc/systemd/system").glob("finance-radar-*.service"))
    python_ok, python_detail = command_result(
        ["python3", "-c", "import ensurepip, sqlite3, ssl, venv; print('python-runtime-ok')"]
    ) if commands.get("python3") else (False, "python3 unavailable")
    systemd_ok = Path("/run/systemd/system").is_dir()
    if systemd_ok and commands.get("systemctl"):
        running_ok, running_detail = command_result(["systemctl", "is-system-running"])
        systemd_ok = running_ok or running_detail in {"degraded", "starting"}
    else:
        running_detail = "systemd runtime directory unavailable"
    ports = {str(port): port_is_available(port) for port in APP_PORTS}
    checks = {
        "running_as_root": os.geteuid() == 0,
        "linux_x86_64": platform.system() == "Linux" and architecture in {"x86_64", "amd64"},
        "public_web_url_https_radar": valid_public_web_url(public_web_url),
        "required_commands_present": all(commands.values()),
        "python_runtime_modules": python_ok,
        "systemd_operational": systemd_ok,
        "clean_application_root": not Path("/opt/finance-radar").exists(),
        "no_existing_service_units": not units,
        "application_ports_available": all(ports.values()),
        "memory_headroom": memory >= MIN_MEMORY_BYTES,
        "disk_headroom": disk.free >= required_disk,
    }
    missing_commands = [name for name, location in commands.items() if not location]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "host": {
            "hostname": socket.gethostname(),
            "system": platform.system(),
            "architecture": architecture,
            "public_web_url": public_web_url,
        },
        "resources": {
            "memory_available_bytes": memory,
            "memory_required_bytes": MIN_MEMORY_BYTES,
            "disk_root": str(disk_root),
            "disk_available_bytes": disk.free,
            "disk_required_bytes": required_disk,
            "expected_unpacked_bytes": expected_unpacked_bytes,
        },
        "runtime": {
            "commands": commands,
            "missing_commands": missing_commands,
            "python_detail": python_detail,
            "systemd_detail": running_detail,
            "application_ports": ports,
            "existing_service_units": units,
            "application_root_exists": Path("/opt/finance-radar").exists(),
        },
        "boundaries": {
            "clean_host_required": True,
            "trading_project_touched": False,
            "activation_performed": False,
            "edge_tools_required": require_edge_tools,
        },
    }


def write_report(report: dict[str, Any], path: Path | None) -> None:
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(path)
    print(rendered, end="")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-unpacked-bytes", type=int, required=True)
    parser.add_argument("--public-web-url", required=True)
    parser.add_argument("--require-edge-tools", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = collect_preflight(
        expected_unpacked_bytes=args.expected_unpacked_bytes,
        public_web_url=args.public_web_url,
        require_edge_tools=args.require_edge_tools,
    )
    write_report(report, args.report)
    return 0 if report["status"] == "PASS" else 6


if __name__ == "__main__":
    raise SystemExit(main())
