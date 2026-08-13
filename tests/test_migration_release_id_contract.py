from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.release_audit import DEFAULT_CRITICAL_FILES, build_release_manifest
from scripts.release_identity import validate_release_id


ROOT = Path(__file__).parents[1]
DEFAULT_STYLE_RELEASE = "20260804T010203Z-aaaaaaaaaaaa"
DEFAULT_STYLE_GIT = {"available": True, "commit": "a" * 40, "dirty": False}


def test_release_audit_default_identity_is_accepted_by_the_shared_migration_contract(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "release.txt").write_text("release contract\n", encoding="utf-8")

    manifest = build_release_manifest(
        root,
        release_id=None,
        critical_files=("release.txt",),
        git_state=DEFAULT_STYLE_GIT,
        generated_at=datetime(2026, 8, 4, 1, 2, 3, tzinfo=timezone.utc),
    )

    assert manifest["release"]["id"] == DEFAULT_STYLE_RELEASE
    assert validate_release_id(manifest["release"]["id"]) == DEFAULT_STYLE_RELEASE
    assert "scripts/release_identity.py" in DEFAULT_CRITICAL_FILES


@pytest.mark.parametrize(
    "release_id",
    (
        "20260804T010203Z-aaaaaaaaaaaa/../../escape",
        "20260804T010203Z-aaaaaaaaaaaa\\escape",
        "20260804T010203Z-aaaaaaaaaaaa'quoted",
        "../escape",
        "",
    ),
)
def test_shared_release_identity_rejects_path_and_command_injection_characters(
    release_id: str,
) -> None:
    with pytest.raises(ValueError, match="release id must use"):
        validate_release_id(release_id)


def test_bash_and_powershell_migration_entrypoints_use_the_shared_safe_alphabet() -> None:
    expected_pattern = "^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"

    activator = (ROOT / "deployment/systemd/activate_prepared_restore.sh").read_text(
        encoding="utf-8"
    )
    restore = (ROOT / "scripts/restore_migration_to_vps.ps1").read_text(encoding="utf-8")
    pull = (ROOT / "scripts/pull_server_migration_backup.ps1").read_text(encoding="utf-8")
    retry = (ROOT / "scripts/finalize_offhost_backup_retry.ps1").read_text(encoding="utf-8")
    verify = (ROOT / "scripts/verify_encrypted_offhost_backup.ps1").read_text(encoding="utf-8")

    assert expected_pattern in activator
    assert expected_pattern in restore
    assert "[A-Za-z0-9][A-Za-z0-9._-]{0,95}" in pull
    assert expected_pattern in retry
    assert expected_pattern in verify
