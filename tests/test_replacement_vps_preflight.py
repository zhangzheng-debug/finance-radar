from __future__ import annotations

from scripts.replacement_vps_preflight import (
    MIN_DISK_BYTES,
    required_disk_bytes,
    valid_public_web_url,
)


def test_public_web_url_requires_https_radar_path_without_query() -> None:
    assert valid_public_web_url("https://radar.example.org/radar/") is True
    assert valid_public_web_url("http://radar.example.org/radar/") is False
    assert valid_public_web_url("https://radar.example.org/") is False
    assert valid_public_web_url("https://radar.example.org/radar/?preview=1") is False


def test_disk_gate_reserves_restore_and_install_headroom() -> None:
    assert required_disk_bytes(0) == MIN_DISK_BYTES
    unpacked = 3 * 1024 * 1024 * 1024
    assert required_disk_bytes(unpacked) == unpacked * 2 + 512 * 1024 * 1024


def test_preflight_script_is_fail_closed_and_never_activates() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "replacement_vps_preflight.py"
    ).read_text(encoding="utf-8")
    assert '"status": "PASS" if all(checks.values()) else "FAIL"' in source
    assert '"activation_performed": False' in source
    assert '"trading_project_touched": False' in source
    assert "APP_PORTS = (18000, 18501, 18601)" in source
