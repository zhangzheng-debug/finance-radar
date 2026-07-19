from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from scripts.prepare_migration_restore import prepare_restore, render_markdown


STAMP = "20260718T010203Z"
RELEASE = "20260718T000000Z"


def _write(path: Path, content: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _plain_fixture(tmp_path: Path, *, tamper: bool = False) -> tuple[Path, str]:
    root = tmp_path / f"finance-radar-migration-{STAMP}"
    release = root / "releases" / RELEASE
    _write(root / "CURRENT_RELEASE.txt", f"/opt/finance-radar/releases/{RELEASE}\n")
    _write(release / "app/api/main.py", "app = 'api'\n")
    _write(release / "app/web/Home.py", "page = 'home'\n")
    _write(release / "requirements.txt", "fastapi\n")
    _write(root / "shared/data/finance_radar.sqlite3", b"ledger")
    _write(root / "shared/data/finance_radar_operations.sqlite3", b"operations")
    _write(root / "shared/reports/status.md", "verified\n")
    _write(root / "config/etc/finance-radar.env", "FINANCE_RADAR_DB=/opt/finance-radar/shared/data/finance_radar.sqlite3\n")
    for name in (
        "finance-radar-api.service",
        "finance-radar-web.service",
        "finance-radar-worker.service",
        "finance-radar-backup.service",
        "finance-radar-backup.timer",
    ):
        _write(root / "config/etc/systemd/system" / name, "[Unit]\nDescription=test\n")

    manifest = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        manifest.append(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  ./{path.relative_to(root).as_posix()}"
        )
    _write(root / "MANIFEST.sha256", "\n".join(manifest) + "\n")
    if tamper:
        _write(release / "app/api/main.py", "app = 'tampered'\n")

    archive_path = tmp_path / f"finance-radar-migration-{STAMP}.tgz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(root, arcname=root.name)
        for name in ("data", "reports"):
            member = tarfile.TarInfo(f"{root.name}/releases/{RELEASE}/{name}")
            member.type = tarfile.SYMTYPE
            member.linkname = f"/opt/finance-radar/shared/{name}"
            archive.addfile(member)
    return archive_path, hashlib.sha256(archive_path.read_bytes()).hexdigest()


def test_full_archive_is_prepared_without_following_archive_links(tmp_path: Path) -> None:
    archive, digest = _plain_fixture(tmp_path)
    destination = tmp_path / "prepared"
    report = prepare_restore(
        archive,
        destination,
        expected_release=RELEASE,
        expected_sha256=digest,
    )
    assert report["status"] == "PREPARED_NOT_ACTIVATED"
    assert report["manifest_entries_verified"] >= 10
    assert report["symlinks_skipped"] == 2
    assert (destination / "releases" / RELEASE / "app" / "api" / "main.py").is_file()
    assert not (destination / "releases" / RELEASE / "data").exists()
    plan = json.loads((destination / "SYMLINK_PLAN.json").read_text(encoding="utf-8"))
    assert plan == [
        {"path": f"releases/{RELEASE}/data", "target": "/opt/finance-radar/shared/data"},
        {"path": f"releases/{RELEASE}/reports", "target": "/opt/finance-radar/shared/reports"},
    ]
    prepared = json.loads((destination / "PREPARED_RESTORE.json").read_text(encoding="utf-8"))
    assert prepared["boundaries"]["archive_symlinks_followed"] is False
    markdown = render_markdown(prepared)
    assert "Result: **PREPARED_NOT_ACTIVATED**" in markdown
    assert "Trading project included: `False`" in markdown


def test_prepare_refuses_existing_destination_and_manifest_tampering(tmp_path: Path) -> None:
    archive, digest = _plain_fixture(tmp_path)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError, match="already exists"):
        prepare_restore(archive, existing, expected_release=RELEASE, expected_sha256=digest)

    tampered, tampered_digest = _plain_fixture(tmp_path / "tampered", tamper=True)
    with pytest.raises(ValueError, match="manifest verification failed"):
        prepare_restore(
            tampered,
            tmp_path / "tampered-prepared",
            expected_release=RELEASE,
            expected_sha256=tampered_digest,
        )


def test_prepare_rejects_path_traversal_and_forbidden_target(tmp_path: Path) -> None:
    archive = tmp_path / f"finance-radar-migration-{STAMP}.tgz"
    with tarfile.open(archive, "w:gz") as output:
        payload = b"escape"
        member = tarfile.TarInfo(f"finance-radar-migration-{STAMP}/../escape")
        member.size = len(payload)
        output.addfile(member, io.BytesIO(payload))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="unsafe archive member path"):
        prepare_restore(
            archive,
            tmp_path / "unsafe",
            expected_release=RELEASE,
            expected_sha256=digest,
        )

    valid, valid_digest = _plain_fixture(tmp_path / "valid")
    with pytest.raises(ValueError, match="forbidden project"):
        prepare_restore(
            valid,
            tmp_path / "forbidden",
            expected_release=RELEASE,
            expected_sha256=valid_digest,
            target_base="/opt/ethusdc-pivot-bot",
        )


def test_activation_script_has_explicit_gates_and_stops_before_nginx() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "deployment"
        / "systemd"
        / "activate_prepared_restore.sh"
    ).read_text(encoding="utf-8")
    assert '[ "$CONFIRM" = "--activate" ]' in script
    assert '[ ! -e "$BASE" ]' in script
    assert "refusing to overwrite existing" in script
    assert "PREPARED_RESTORE.json" in script
    assert "systemctl is-active --quiet" in script
    assert "nginx_tls=pending" in script


def test_windows_cutover_orchestrator_defaults_to_audit_only_and_blocks_current_vps() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "restore_migration_to_vps.ps1"
    ).read_text(encoding="utf-8")
    assert "if (-not $Activate)" in script
    assert "AUDIT_ONLY_PASS" in script
    assert "refusing the current VPS" in script
    assert "replacement VPS is not clean" in script
    assert "replacement_vps_preflight.py" in script
    assert "--require-edge-tools" in script
    assert "replacement_vps_preflight_latest.json" in script
    assert "PublicWebUrl must be an HTTPS URL" in script
    assert "--activate" in script
    assert "PENDING_SEPARATE_CUTOVER" in script
