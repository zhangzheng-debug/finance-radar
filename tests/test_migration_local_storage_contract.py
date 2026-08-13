from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_offhost_migration_backup_defaults_large_local_material_to_d_drive() -> None:
    source = (ROOT / "scripts" / "pull_server_migration_backup.ps1").read_text(encoding="utf-8")
    audit = (ROOT / "scripts" / "audit_migration_restore.py").read_text(encoding="utf-8")

    assert '$DestinationRoot = "D:\\FinanceRadarBackups"' in source
    assert '$auditWorkspaceRoot = [System.IO.Path]::GetFullPath("D:\\FinanceRadarScratch\\migration-audit")' in source
    assert "--workspace-root $auditWorkspaceRoot" in source
    assert 'Path(r"D:\\FinanceRadarScratch\\migration-audit")' in audit


def test_restore_orchestrator_keeps_its_large_worktree_off_the_system_temp_drive() -> None:
    source = (ROOT / "scripts" / "restore_migration_to_vps.ps1").read_text(encoding="utf-8")

    assert '$localWorkRoot = [System.IO.Path]::GetFullPath("D:\\FinanceRadarScratch\\migration-restore")' in source
    assert "--workspace-root $localWork" in source


def test_backup_retry_and_verification_default_to_the_same_d_drive_backup_root() -> None:
    retry = (ROOT / "scripts" / "finalize_offhost_backup_retry.ps1").read_text(encoding="utf-8")
    verify = (ROOT / "scripts" / "verify_encrypted_offhost_backup.ps1").read_text(encoding="utf-8")

    assert '[string]$BackupRoot = "D:\\FinanceRadarBackups"' in retry
    assert "--workspace-root $auditWorkspaceRoot" in retry
    assert '[string]$BackupRoot = "D:\\FinanceRadarBackups"' in verify
