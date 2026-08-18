from __future__ import annotations

from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "pull_server_migration_backup.ps1"


def test_migration_pull_retries_connection_and_preserves_failing_exit_code() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"ConnectionAttempts=5"' in source
    assert "$remoteExitCode = $LASTEXITCODE" in source
    assert "exit code $remoteExitCode" in source


def test_migration_pull_has_no_repository_embedded_host_or_private_key_path() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "$env:FINANCE_RADAR_SSH_HOST" in source
    assert "$env:FINANCE_RADAR_SSH_IDENTITY_FILE" in source
    assert "18.208.34.152" not in source
    assert ".ssh1\\id_ed25519" not in source


def test_migration_pull_keeps_detailed_receipt_private() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "offhost-verification.json" in source
    assert "offhost-status.json" not in source
    assert "could not publish the public off-host verification status" not in source


def test_migration_pull_can_bind_ssh_to_an_explicit_physical_ipv4_address() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert '[string]$BindAddress = ""' in source
    assert "BindAddress must be a valid IPv4 address" in source
    assert '$sshOptions += @("-o", "BindAddress=$BindAddress")' in source


def test_migration_pull_can_reuse_only_a_validated_recovery_bundle_identity() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert '[string]$ExistingBackupId = ""' in source
    assert "ExistingBackupId is invalid" in source
    assert "source_recovery_bundle" in source
