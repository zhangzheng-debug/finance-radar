from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from scripts.build_offline_demo import APP_RUNTIME_ROOTS, FORBIDDEN_NAME_PARTS, SOURCE_FILES


ROOT = Path(__file__).resolve().parents[1]


def test_offline_runtime_omits_collectors_workers_and_broker_adapters() -> None:
    assert set(APP_RUNTIME_ROOTS) == {"api", "models", "services", "storage", "web"}
    names = "\n".join(SOURCE_FILES).lower()
    assert "collector" not in names
    assert "worker" not in names
    assert "telegram" not in names
    assert "binance" not in names
    assert "ibkr" not in names
    assert {"collector", "worker", "telegram_mtproto", "binance", "ibkr"}.issubset(
        set(FORBIDDEN_NAME_PARTS)
    )


def test_sitecustomize_blocks_external_resolution_but_allows_loopback_bind() -> None:
    guard_dir = ROOT / "deployment" / "offline"
    code = """
import socket
blocked = False
try:
    socket.getaddrinfo('example.com', 443)
except OSError as exc:
    blocked = 'offline guard blocked' in str(exc)
s = socket.socket()
s.bind(('127.0.0.1', 0))
s.close()
raise SystemExit(0 if blocked else 7)
"""
    environment = dict(os.environ)
    environment["FINANCE_RADAR_OFFLINE_NETWORK_GUARD"] = "1"
    environment["PYTHONPATH"] = str(guard_dir)
    completed = subprocess.run(
        [sys.executable, "-c", code],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_offline_launcher_enables_guard_and_clears_external_credentials() -> None:
    launcher = (ROOT / "deployment" / "offline" / "start_offline_demo.ps1").read_text(
        encoding="utf-8"
    )
    assert '$env:FINANCE_RADAR_OFFLINE_NETWORK_GUARD = "1"' in launcher
    assert '$env:FINANCE_RADAR_REVIEW_UI_ENABLED = "0"' in launcher
    for variable in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_API_HASH",
        "BINANCE_API_SECRET",
        "IBKR_ACCOUNT",
        "FINANCE_RADAR_EVIDENCE_LLM_URL",
    ):
        assert variable in launcher
