from __future__ import annotations

import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.main import create_app
from app.config import Settings
from app.storage import LedgerRepository
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
