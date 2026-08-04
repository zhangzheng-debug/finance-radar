from __future__ import annotations

import json
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from app.api.main import create_app
from app.config import Settings
from app.storage import LedgerRepository, OperationsRepository
from app.storage.ledger import PUBLIC_EVENT_STATE_CTE
from event_ledger import open_ledger


def _settings(root: Path, ledger_path: Path) -> Settings:
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


def _insert_event(
    connection,
    event_id: str,
    status: str,
    *,
    event_date: str,
    first_seen_at: str,
    last_updated_at: str,
    company_name: str,
) -> None:
    connection.execute(
        """INSERT INTO canonical_events VALUES (
           ?,1,?,?, 'regulatory','filing', ?,?,?,NULL,NULL,?,NULL,NULL,'src',1)""",
        (
            event_id,
            status,
            status,
            event_date,
            first_seen_at,
            last_updated_at,
            company_name,
        ),
    )


def _insert_rough_review(
    connection,
    event_id: str,
    outcome: str | None,
    *,
    reviewed_at: str,
) -> None:
    rough_review = {
        "reviewed_at": reviewed_at,
        "formal_verification": False,
        **(
            {"outcome": outcome}
            if outcome is not None
            else {"decision_status": "INSUFFICIENT"}
        ),
    }
    payload = json.dumps(
        {"rough_review": rough_review},
        sort_keys=True,
    )
    connection.execute(
        """INSERT INTO pipeline_jobs VALUES (
           ?,?,'live_primary_evidence_review','COMPLETED_AUTHORIZED_ROUGH_REVIEW',
           50,0,?,NULL,?,?,?)""",
        (f"job-{event_id}", event_id, reviewed_at, payload, reviewed_at, reviewed_at),
    )


def _populated_ledger(root: Path) -> Path:
    ledger_path = root / "ledger.sqlite3"
    connection = open_ledger(ledger_path)
    anchor = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    anchor_text = anchor.isoformat()
    connection.execute(
        "INSERT INTO sources VALUES ('src','Source','official_primary','P0',1,1,?,?)",
        (anchor_text, anchor_text),
    )

    for index in range(2):
        _insert_event(
            connection,
            f"verified-{index}",
            "verified",
            event_date=f"2026-08-0{index + 1}",
            first_seen_at=(anchor + timedelta(minutes=index)).isoformat(),
            last_updated_at=(anchor + timedelta(hours=index + 1)).isoformat(),
            company_name=f"Verified {index}",
        )
    _insert_event(
        connection,
        "excluded-0",
        "rejected",
        event_date="2026-07-31",
        first_seen_at=(anchor - timedelta(days=1)).isoformat(),
        last_updated_at=(anchor + timedelta(hours=3)).isoformat(),
        company_name="Excluded",
    )
    _insert_event(
        connection,
        "weak-0",
        "weak",
        event_date="2026-07-30",
        first_seen_at=(anchor - timedelta(days=2)).isoformat(),
        last_updated_at=(anchor + timedelta(hours=4)).isoformat(),
        company_name="Weak Evidence",
    )

    for index in range(14):
        event_id = f"rough-insufficient-{index:02d}"
        _insert_event(
            connection,
            event_id,
            "candidate",
            event_date=f"2026-07-{index + 1:02d}",
            first_seen_at=(anchor - timedelta(days=3, minutes=index)).isoformat(),
            last_updated_at=(anchor + timedelta(minutes=index)).isoformat(),
            company_name=f"Insufficient {index:02d}",
        )
        _insert_rough_review(
            connection,
            event_id,
            "ROUGH_INSUFFICIENT" if index < 13 else None,
            reviewed_at=(anchor + timedelta(days=1, minutes=index)).isoformat(),
        )

    _insert_event(
        connection,
        "rough-accepted-0",
        "candidate",
        event_date="2026-08-03",
        first_seen_at=(anchor + timedelta(minutes=30)).isoformat(),
        last_updated_at=(anchor + timedelta(hours=5)).isoformat(),
        company_name="Alpha Rough Reviewed",
    )
    _insert_rough_review(
        connection,
        "rough-accepted-0",
        "ROUGH_ACCEPTED",
        reviewed_at=(anchor + timedelta(days=1, hours=1)).isoformat(),
    )
    _insert_event(
        connection,
        "pending-0",
        "candidate",
        event_date="2026-08-04",
        first_seen_at=(anchor + timedelta(hours=2)).isoformat(),
        last_updated_at=(anchor + timedelta(hours=6)).isoformat(),
        company_name="Zulu Pending",
    )
    pending_at = (anchor + timedelta(hours=2)).isoformat()
    connection.execute(
        """INSERT INTO pipeline_jobs VALUES (
           'job-pending','pending-0','live_primary_evidence_review','PENDING_HUMAN_REVIEW',
           50,0,?,NULL,'{}',?,?)""",
        (pending_at, pending_at, pending_at),
    )
    connection.commit()
    connection.close()
    return ledger_path


def test_repository_public_funnel_exposes_rough_insufficient_as_an_exhaustive_partition() -> None:
    with tempfile.TemporaryDirectory() as directory:
        ledger_path = _populated_ledger(Path(directory))
        repository = LedgerRepository(ledger_path)
        overview = repository.overview(run_integrity_check=False)
        funnel = overview["public_funnel"]

        assert funnel["total"] == 20
        assert {
            key: funnel[key]
            for key in (
                "verified",
                "excluded",
                "insufficient",
                "pending_verification",
                "rough_reviewed",
            )
        } == {
            "verified": 2,
            "excluded": 1,
            "insufficient": 15,
            "pending_verification": 1,
            "rough_reviewed": 1,
        }
        assert funnel["partition_total"] == funnel["total"]
        assert funnel["partition_complete"] is True
        assert funnel["insufficient_breakdown"] == {
            "rough_review": 14,
            "canonical_weak_without_rough_insufficient": 1,
        }
        assert overview["event_status"]["weak"] == 1
        assert overview["rough_reviewed"] == 15

        insufficient = repository.list_events(public_state="insufficient", limit=200)
        assert insufficient["total"] == 15
        assert {item["public_state"] for item in insufficient["items"]} == {"insufficient"}
        rough_item = next(
            item for item in insufficient["items"] if item["event_id"] == "rough-insufficient-00"
        )
        assert rough_item["reviewed_at"] == "2026-08-02T12:00:00+00:00"
        detail = repository.event_detail("rough-insufficient-00")
        assert detail is not None
        assert detail["event"]["public_state"] == "insufficient"
        assert detail["event"]["reviewed_at"] == rough_item["reviewed_at"]

        connection = open_ledger(ledger_path)
        connection.execute(
            "UPDATE canonical_events SET status='candidate',label_status='candidate' WHERE event_id='weak-0'"
        )
        connection.commit()
        connection.close()
        without_canonical_weak = repository.overview(run_integrity_check=False)
        assert without_canonical_weak["event_status"].get("weak", 0) == 0
        assert without_canonical_weak["public_funnel"]["insufficient"] == 14
        assert without_canonical_weak["public_funnel"]["insufficient_breakdown"]["rough_review"] == 14


def test_repository_public_filters_dates_sorting_and_legacy_status_compose_safely() -> None:
    with tempfile.TemporaryDirectory() as directory:
        repository = LedgerRepository(_populated_ledger(Path(directory)))

        combined = repository.list_events(
            status="candidate",
            public_state="insufficient",
            date_from="2026-07-05",
            date_to="2026-07-09",
            sort="subject",
            limit=200,
        )
        assert combined["total"] == 5
        assert combined["public_state"] == "insufficient"
        assert combined["date_from"] == "2026-07-05"
        assert combined["date_to"] == "2026-07-09"
        assert combined["sort"] == "subject"
        assert [item["company_name"] for item in combined["items"]] == [
            "Insufficient 04",
            "Insufficient 05",
            "Insufficient 06",
            "Insufficient 07",
            "Insufficient 08",
        ]

        latest = repository.list_events(sort="latest", limit=3)["items"]
        assert [item["event_id"] for item in latest] == [
            "pending-0",
            "rough-accepted-0",
            "weak-0",
        ]
        default_order = repository.list_events(limit=2)["items"]
        assert [item["event_id"] for item in default_order] == ["pending-0", "rough-accepted-0"]

        with pytest.raises(ValueError, match="unsupported sort"):
            repository.list_events(sort="latest; DROP TABLE canonical_events")
        with pytest.raises(ValueError, match="unsupported public_state"):
            repository.list_events(public_state="verified' OR 1=1 --")
        with pytest.raises(ValueError, match="date_from"):
            repository.list_events(date_from="2026-99-99")


def test_api_contract_separates_new_event_updates_and_last_successful_worker_cycle() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ledger_path = root / "ledger.sqlite3"
        connection = open_ledger(ledger_path)
        now = datetime.now(timezone.utc)
        connection.execute(
            "INSERT INTO sources VALUES ('src','Source','official_primary','P0',1,1,?,?)",
            (now.isoformat(), now.isoformat()),
        )
        _insert_event(
            connection,
            "freshness-event",
            "candidate",
            event_date=now.date().isoformat(),
            first_seen_at=(now - timedelta(hours=3)).isoformat(),
            last_updated_at=(now - timedelta(minutes=5)).isoformat(),
            company_name="Freshness Event",
        )
        connection.commit()
        connection.close()

        settings = _settings(root, ledger_path)
        application = create_app(settings)
        operations = OperationsRepository(settings.operations_db)
        with closing(operations.connect()) as connection:
            success_started = now - timedelta(minutes=31)
            success_finished = now - timedelta(minutes=30)
            failed_started = now - timedelta(minutes=2)
            failed_finished = now - timedelta(minutes=1)
            connection.execute(
                "INSERT INTO worker_cycles VALUES (?,?,?,?,?,?)",
                (
                    "success-cycle",
                    success_started.isoformat(),
                    success_finished.isoformat(),
                    "SUCCESS",
                    "{}",
                    None,
                ),
            )
            connection.execute(
                "INSERT INTO worker_cycles VALUES (?,?,?,?,?,?)",
                (
                    "failed-cycle",
                    failed_started.isoformat(),
                    failed_finished.isoformat(),
                    "FAILED",
                    "{}",
                    "fixture failure",
                ),
            )
            connection.commit()

        with TestClient(application) as client:
            response = client.get("/api/v1/overview")
            filtered = client.get(
                "/api/v1/events",
                params={
                    "public_state": "pending_verification",
                    "date_from": now.date().isoformat(),
                    "date_to": now.date().isoformat(),
                    "sort": "latest",
                },
            )
            detail_response = client.get("/api/v1/events/freshness-event")
            bad_state = client.get("/api/v1/events", params={"public_state": "not-a-state"})
            bad_sort = client.get("/api/v1/events", params={"sort": "latest;select"})
            bad_range = client.get(
                "/api/v1/events",
                params={"date_from": "2026-08-02", "date_to": "2026-08-01"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        timing = data["timing"]
        assert data["public_funnel"]["pending_verification"] == 1
        assert timing["latest_new_event_at"] == (now - timedelta(hours=3)).isoformat()
        assert 2.9 * 3600 <= timing["latest_new_event_age_seconds"] <= 3.1 * 3600
        assert timing["latest_event_update_at"] == (now - timedelta(minutes=5)).isoformat()
        assert 4 * 60 <= timing["latest_event_update_age_seconds"] <= 6 * 60
        assert timing["latest_event_age_seconds"] == timing["latest_event_update_age_seconds"]
        assert timing["latest_worker_finished_at"] == failed_finished.isoformat()
        assert timing["latest_worker_success_at"] == success_finished.isoformat()
        assert 29 * 60 <= timing["latest_worker_success_age_seconds"] <= 31 * 60
        assert timing["worker_cycle_duration_seconds"] == 60.0

        assert filtered.status_code == 200
        filtered_data = filtered.json()["data"]
        assert filtered_data["total"] == 1
        assert filtered_data["items"][0]["public_state"] == "pending_verification"
        assert filtered_data["sort"] == "latest"
        assert filtered_data["date_from"] == now.date().isoformat()
        assert filtered_data["date_to"] == now.date().isoformat()
        assert detail_response.status_code == 200
        detail_event = detail_response.json()["data"]["event"]
        assert detail_event["public_state"] == "pending_verification"
        assert detail_event["reviewed_at"] is None
        assert bad_state.status_code == 422
        assert bad_sort.status_code == 422
        assert bad_range.status_code == 422


def test_public_state_query_materializes_latest_rough_once_without_correlated_job_lookup() -> None:
    assert "ROW_NUMBER() OVER" in PUBLIC_EVENT_STATE_CTE
    assert "LEFT JOIN ranked_rough_reviews" in PUBLIC_EVENT_STATE_CTE
    assert "SELECT latest_rough.job_id" not in PUBLIC_EVENT_STATE_CTE

    with tempfile.TemporaryDirectory() as directory:
        ledger_path = _populated_ledger(Path(directory))
        connection = open_ledger(ledger_path)
        plan = connection.execute(
            "EXPLAIN QUERY PLAN "
            + PUBLIC_EVENT_STATE_CTE
            + " SELECT COUNT(*) FROM event_public e WHERE public_state=?",
            ("insufficient",),
        ).fetchall()
        connection.close()

    details = "\n".join(str(row[3]) for row in plan)
    assert "CORRELATED SCALAR SUBQUERY" not in details
    assert "ranked_rough_reviews" in details


def test_large_public_state_page_has_bounded_query_work() -> None:
    class CountingRepository(LedgerRepository):
        progress_callbacks = 0

        def connect(self):
            connection = super().connect()

            def record_progress() -> int:
                self.progress_callbacks += 1
                return 0

            connection.set_progress_handler(record_progress, 1000)
            return connection

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ledger_path = root / "large.sqlite3"
        connection = open_ledger(ledger_path)
        timestamp = "2026-08-01T12:00:00+00:00"
        connection.execute(
            "INSERT INTO sources VALUES ('src','Source','official_primary','P0',1,1,?,?)",
            (timestamp, timestamp),
        )
        event_rows = []
        job_rows = []
        for index in range(5000):
            event_id = f"large-{index:05d}"
            event_rows.append(
                (
                    event_id,
                    "2026-08-01",
                    timestamp,
                    f"2026-08-01T12:{index % 60:02d}:00+00:00",
                    f"Company {index:05d}",
                )
            )
            if index % 2 == 0:
                payload = json.dumps(
                    {
                        "rough_review": {
                            "outcome": "ROUGH_INSUFFICIENT",
                            "reviewed_at": timestamp,
                        }
                    },
                    sort_keys=True,
                )
                job_rows.append(
                    (
                        f"large-job-{index:05d}",
                        event_id,
                        timestamp,
                        payload,
                        timestamp,
                        timestamp,
                    )
                )
        connection.executemany(
            """INSERT INTO canonical_events VALUES (
               ?,1,'candidate','candidate','regulatory','filing',?,?,?,NULL,NULL,?,NULL,NULL,'src',1)""",
            event_rows,
        )
        connection.executemany(
            """INSERT INTO pipeline_jobs VALUES (
               ?,?,'live_primary_evidence_review','COMPLETED_AUTHORIZED_ROUGH_REVIEW',
               50,0,?,NULL,?,?,?)""",
            job_rows,
        )
        connection.commit()
        connection.close()

        repository = CountingRepository(ledger_path)
        started = perf_counter()
        page = repository.list_events(
            public_state="insufficient",
            sort="latest",
            limit=48,
        )
        elapsed = perf_counter() - started

    assert page["total"] == 2500
    assert len(page["items"]) == 48
    assert repository.progress_callbacks < 1500
    assert elapsed < 2.0
