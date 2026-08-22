from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.api.main import create_app
from app.api.snapshot import PrecomputedSnapshot
from app.config import Settings
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
    )


def test_overview_requests_only_read_the_startup_snapshot(tmp_path: Path) -> None:
    application = create_app(_settings(tmp_path))
    original_overview = application.state.ledger.overview
    calls = 0

    def counted_overview(*, run_integrity_check: bool = False):
        nonlocal calls
        calls += 1
        return original_overview(run_integrity_check=run_integrity_check)

    application.state.ledger.overview = counted_overview
    with TestClient(application) as client:
        first = client.get("/api/v1/overview")
        second = client.get("/api/v1/overview")

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls == 1
    assert first.json()["data"]["overview_snapshot"]["status"] == "READY"
    assert first.json()["data"]["overview_snapshot"]["generation"] == 1


def test_refresh_failure_keeps_last_good_snapshot() -> None:
    state = {"fail": False}

    def factory() -> dict[str, int]:
        if state["fail"]:
            raise RuntimeError("private upstream detail")
        return {"count": 7}

    snapshot = PrecomputedSnapshot(
        factory,
        refresh_interval_seconds=30,
        name="test-overview",
    )
    assert snapshot.refresh() is True
    first, first_status = snapshot.read()
    first["count"] = 99

    state["fail"] = True
    assert snapshot.refresh() is False
    second, second_status = snapshot.read()

    assert first_status["status"] == "READY"
    assert second == {"count": 7}
    assert second_status["status"] == "STALE_AFTER_REFRESH_ERROR"
    assert second_status["generation"] == 1
    assert second_status["last_refresh_error_code"] == "RuntimeError"
    assert "private upstream detail" not in str(second_status)


def test_home_allows_twenty_seconds_for_the_first_overview_read() -> None:
    source = (ROOT / "app" / "web" / "Home.py").read_text(encoding="utf-8")
    overview_call = source[source.index('cached_api_get(\n        "/api/v1/overview"') :]
    overview_call = overview_call[: overview_call.index("    )")]
    assert "timeout_seconds=20" in overview_call
