from __future__ import annotations

import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.main import _backup_artifact_visibility, create_app
from app.config import Settings
from app.storage import LedgerRepository, OperationsRepository
from event_ledger import open_ledger


ROOT = Path(__file__).resolve().parents[1]


def _settings(root: Path) -> Settings:
    ledger_path = root / "ledger.sqlite3"
    open_ledger(ledger_path).close()
    return Settings(
        ledger_db=ledger_path,
        operations_db=root / "operations.sqlite3",
        artifact_dir=root / "artifacts",
        evidence_object_dir=root / "evidence_objects",
        replay_dir=ROOT / "replay" / "cases",
        demo_mode="RECENT_CAPTURE",
        admin_token="test-secret",
        api_base_url="http://testserver",
        web_base_url="http://testserver",
    )


def test_repository_can_defer_request_time_integrity_scan() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        settings = _settings(Path(temp_dir))
        health = LedgerRepository(settings.ledger_db).health(run_integrity_check=False)

    assert health["status"] == "ok"
    assert health["quick_check"] == "deferred"
    assert health["integrity_check_source"] == "not_run"


def test_operations_repository_can_defer_request_time_integrity_scan() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        repository = OperationsRepository(Path(temp_dir) / "operations.sqlite3")
        health = repository.health(run_integrity_check=False)

    assert health["status"] == "ok"
    assert health["quick_check"] == "deferred"
    assert health["integrity_check_source"] == "not_run"


def test_api_health_defers_operations_integrity_to_verified_backup_workflow() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        settings = _settings(Path(temp_dir))
        with TestClient(create_app(settings)) as client:
            response = client.get("/api/v1/health")

    assert response.status_code == 200
    operations = response.json()["data"]["operations"]
    assert operations["status"] == "ok"
    assert operations["quick_check"] == "deferred"
    assert operations["integrity_check_source"] == "not_run"


def test_backup_visibility_distinguishes_protected_from_missing() -> None:
    protected = Path("protected-backup")
    with patch.object(Path, "stat", side_effect=PermissionError("least privilege")):
        available, visibility = _backup_artifact_visibility(protected)

    assert available is None
    assert visibility == "protected"


def test_health_bounds_inventory_but_does_not_claim_protected_artifact_is_fresh() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        settings = _settings(Path(temp_dir))
        application = create_app(settings)
        backup_path = Path(temp_dir) / "protected" / "manifest.json"
        backup_path.parent.mkdir()
        backup_path.write_text("{}", encoding="utf-8")
        backup_id = application.state.operations.create_backup_run(
            backup_path,
            source_bytes=1,
            manifest_path=backup_path,
            snapshot_kind="recovery_bundle",
        )
        application.state.operations.finish_backup_run(
            backup_id,
            backup_bytes=1,
            quick_check="ok",
            counts={},
            manifest_path=backup_path,
            components={
                "ledger": {"file_inventory": ["x" * 250_000]},
                "operations": {"file_inventory": ["y" * 250_000]},
            },
            snapshot_kind="recovery_bundle",
        )
        real_stat = Path.stat

        def protected_stat(path: Path, *args, **kwargs):
            if path == backup_path:
                raise PermissionError("root-only recovery bundle")
            return real_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", protected_stat):
            response = TestClient(application).get("/api/v1/health")

    assert response.status_code == 200
    assert len(response.content) < 100_000
    health = response.json()["data"]
    assert health["status"] == "degraded"
    snapshot = health["ledger"]["backup_snapshot"]
    assert snapshot["status"] == "UNVERIFIABLE_PROTECTED"
    assert snapshot["fresh"] is False
    assert snapshot["path_available"] is None
    assert snapshot["artifact_visibility"] == "protected"
    assert snapshot["artifact_verification_source"] == "unprivileged_path_probe_inconclusive"
    assert health["ledger"]["quick_check"] == "unknown"
    assert health["ledger"]["integrity_check_source"] == "unverifiable_protected_backup"
    for key in ("latest_backup", "latest_verified_backup"):
        public_backup = health["operations"][key]
        assert "components" not in public_backup
        assert public_backup["component_summary"] == {
            "count": 2,
            "names": ["ledger", "operations"],
        }
        assert public_backup["backup_path"] == "manifest.json"
        assert public_backup["manifest_path"] == "manifest.json"
    summary = health["operations"]["backup_summary"]
    assert summary["retained_daily_files"] is None
    assert summary["retained_daily_files_observable"] is False
    assert summary["protected_daily_records"] == 1


def test_overview_uses_timestamped_verified_backup_integrity() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        settings = _settings(Path(temp_dir))
        application = create_app(settings)
        backup_path = Path(temp_dir) / "verified.sqlite3"
        backup_path.write_bytes(b"recorded fixture path")
        backup_id = application.state.operations.create_backup_run(
            backup_path,
            source_bytes=1,
        )
        application.state.operations.finish_backup_run(
            backup_id,
            backup_bytes=1,
            quick_check="ok",
            counts={},
        )

        response = TestClient(application).get("/api/v1/overview")

    assert response.status_code == 200
    health = response.json()["data"]
    assert health["status"] == "ok"
    assert health["quick_check"] == "ok"
    assert health["integrity_check_source"] == "latest_verified_backup"
    assert health["integrity_checked_at"]
    assert health["current_db_liveness"]["quick_check"] == "deferred"
    assert health["backup_snapshot"]["status"] == "FRESH"
    assert health["backup_snapshot"]["path_available"] is True
    assert health["backup_snapshot"]["artifact_visibility"] == "visible"
    assert (
        health["backup_snapshot"]["artifact_verification_source"]
        == "live_path_stat_and_latest_verified_backup_record"
    )


def test_stale_or_missing_backup_degrades_without_claiming_current_db_is_corrupt() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        settings = _settings(Path(temp_dir))
        application = create_app(settings)
        backup_path = Path(temp_dir) / "old.sqlite3"
        backup_path.write_bytes(b"old fixture path")
        backup_id = application.state.operations.create_backup_run(backup_path, source_bytes=1)
        application.state.operations.finish_backup_run(
            backup_id,
            backup_bytes=1,
            quick_check="ok",
            counts={},
        )
        stale_at = (datetime.now(timezone.utc) - timedelta(hours=37)).isoformat()
        with closing(application.state.operations.connect()) as connection:
            connection.execute("UPDATE backup_runs SET verified_at=? WHERE backup_id=?", (stale_at, backup_id))
            connection.commit()
        with TestClient(application) as client:
            response = client.get("/api/v1/health")

    assert response.status_code == 200
    health = response.json()["data"]
    assert health["status"] == "degraded"
    assert health["ledger"]["current_db_liveness"]["status"] == "ok"
    assert health["ledger"]["backup_snapshot"]["status"] == "STALE"
    assert health["ledger"]["integrity_check_source"] == "stale_or_missing_verified_backup"
