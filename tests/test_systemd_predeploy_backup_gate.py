from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


INSTALLER = Path(__file__).parents[1] / "deployment" / "systemd" / "install_remote.sh"


def _bash() -> str:
    candidates = [
        shutil.which("bash"),
        "C:/Program Files/Git/bin/bash.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    pytest.skip("bash is required to exercise the systemd installer gate")


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _predeploy_gate_source() -> str:
    source = INSTALLER.read_text(encoding="utf-8")
    start = source.index('PREDEPLOY_BACKUP_ID=""')
    end = source.index("# Preserve the previous release intact", start)
    return source[start:end]


def _make_gate_driver(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "systemctl",
        """#!/usr/bin/env bash
set -euo pipefail
if [ "$1" = "is-active" ]; then
    exit 1
fi
if [ "$1" = "start" ] && [ "${2:-}" = "finance-radar-backup.service" ]; then
    if [ "${FAKE_BACKUP_START_FAIL:-0}" = "1" ]; then
        exit 70
    fi
    bundle="$BACKUP_ROOT/finance_radar_29990101T000000Z_abcdef12"
    mkdir -p "$bundle"
    cat > "$bundle/manifest.json" <<'JSON'
{"format":"finance-radar-recovery-bundle-v1","snapshot_id":"finance_radar_29990101T000000Z_abcdef12","created_at":"2999-01-01T00:00:00+00:00"}
JSON
    exit 0
fi
if [ "$1" = "show" ]; then
    if [ "${2:-}" = "finance-radar-admin" ]; then
        if [ "${FAKE_ADMIN_ENABLED:-0}" = "1" ]; then
            printf 'enabled\\n'
        else
            printf 'static\\n'
        fi
        exit 0
    fi
    if [ "${2:-}" = "finance-radar-backup.service" ]; then
        printf 'success\\n'
        exit 0
    fi
fi
printf 'unexpected systemctl call: %s\\n' "$*" >&2
exit 99
""",
    )
    python_executable = Path(sys.executable).as_posix()
    _write_executable(
        fake_bin / "python3",
        f"#!/usr/bin/env bash\nexec '{python_executable}' \"$@\"\n",
    )
    driver = tmp_path / "run-gate.sh"
    driver.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'ROOT="$(cd "$(dirname "$0")" && pwd)"\n'
        'PATH="$ROOT/fake-bin:$PATH"\n'
        'SHARED="$ROOT/shared"\n'
        'BACKUP_ROOT="$SHARED/data/operational_backups"\n'
        "export BACKUP_ROOT\n"
        + _predeploy_gate_source()
        + "require_predeploy_verified_backup\n"
        + 'printf "receipt=%s:%s\\n" "$PREDEPLOY_BACKUP_ID" "$PREDEPLOY_BACKUP_MANIFEST_SHA256"\n',
        encoding="utf-8",
        newline="\n",
    )
    driver.chmod(0o755)
    return driver


def test_predeploy_backup_gate_requires_new_bundle_and_records_receipt(tmp_path: Path) -> None:
    driver = _make_gate_driver(tmp_path)

    result = subprocess.run(
        [_bash(), driver.as_posix()],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "predeploy_backup=VERIFIED snapshot_id=finance_radar_29990101T000000Z_abcdef12" in result.stdout
    assert "receipt=finance_radar_29990101T000000Z_abcdef12:" in result.stdout
    assert len(result.stdout.rsplit(":", 1)[1].strip()) == 64


def test_predeploy_backup_gate_refuses_an_enabled_admin_ui(tmp_path: Path) -> None:
    driver = _make_gate_driver(tmp_path)

    result = subprocess.run(
        [_bash(), driver.as_posix()],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env={**os.environ, "FAKE_ADMIN_ENABLED": "1"},
    )

    assert result.returncode != 0
    assert "finance-radar-admin is boot-enabled (enabled)" in result.stderr
    backup_root = tmp_path / "shared" / "data" / "operational_backups"
    assert not backup_root.exists()


def test_predeploy_backup_gate_stops_when_the_backup_service_fails(tmp_path: Path) -> None:
    driver = _make_gate_driver(tmp_path)

    result = subprocess.run(
        [_bash(), driver.as_posix()],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env={**os.environ, "FAKE_BACKUP_START_FAIL": "1"},
    )

    assert result.returncode != 0
    assert "predeploy backup service failed" in result.stderr
    backup_root = tmp_path / "shared" / "data" / "operational_backups"
    assert not backup_root.exists()
