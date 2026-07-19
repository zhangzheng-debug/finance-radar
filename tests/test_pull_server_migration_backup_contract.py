from __future__ import annotations

from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "pull_server_migration_backup.ps1"


def test_migration_pull_retries_connection_and_preserves_failing_exit_code() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"ConnectionAttempts=5"' in source
    assert "$remoteExitCode = $LASTEXITCODE" in source
    assert "exit code $remoteExitCode" in source
