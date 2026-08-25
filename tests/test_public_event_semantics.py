from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.main import create_app
from app.config import Settings
from app.services.public_event_semantics import (
    derive_public_event_semantics,
    project_public_qwen_semantics,
    project_public_risk_assessment,
)
from app.storage import OperationsRepository
from scripts.event_ledger import open_ledger


ROOT = Path(__file__).resolve().parents[1]


def _model_result(
    *,
    event_version: int,
    label: str = "RISK_REVIEW",
    confidence: float = 0.88,
    confidence_applicable: bool = True,
    input_marker: str = "a",
) -> dict[str, object]:
    return {
        "event_version": event_version,
        "label": label,
        "confidence": confidence,
        "model_version": "router-test-v1",
        "decision_source": (
            "TRAINED_SEMANTIC_MODEL"
            if confidence_applicable
            else "DETERMINISTIC_EVIDENCE_GATE"
        ),
        "confidence_applicable": confidence_applicable,
        "evidence_gate": {"state": "INSUFFICIENT"},
        "shadow": True,
        "no_trading": True,
        "input_sha256": input_marker * 64,
        "latency_ms": 1.0,
    }


def test_public_evidence_semantics_are_independent_of_workflow_state() -> None:
    ready = derive_public_event_semantics(
        {
            "reader_ready": 1,
            "reader_has_subject": 1,
            "reader_has_fact_summary": 1,
            "citable_evidence_count": 1,
            "captured_source_count": 1,
            "public_state": "pending_verification",
        }
    )
    assert ready == {
        "citation_ready": True,
        "evidence_posture": "PRIMARY_SUPPORTED",
        "evidence_gap_codes": [],
    }

    primary_available = derive_public_event_semantics(
        {
            "reader_ready": 0,
            "reader_has_subject": 0,
            "reader_has_fact_summary": 1,
            "citable_evidence_count": 2,
            "captured_source_count": 2,
            "public_state": "verified",
        }
    )
    assert primary_available == {
        "citation_ready": False,
        "evidence_posture": "PRIMARY_SOURCE_AVAILABLE",
        "evidence_gap_codes": ["MISSING_SUBJECT"],
    }

    captured = derive_public_event_semantics(
        {
            "reader_ready": 0,
            "reader_has_subject": 0,
            "reader_has_fact_summary": 0,
            "citable_evidence_count": 0,
            "captured_source_count": 1,
        }
    )
    assert captured["evidence_posture"] == "SOURCE_CAPTURED"
    assert captured["evidence_gap_codes"] == [
        "MISSING_SUBJECT",
        "MISSING_FACT_SUMMARY",
        "MISSING_CITABLE_EVIDENCE",
    ]

    no_source = derive_public_event_semantics({})
    assert no_source["evidence_posture"] == "NO_SOURCE"
    assert no_source["evidence_gap_codes"][-1] == "NO_CAPTURED_SOURCE"


def test_public_risk_projection_requires_current_version_and_applicable_confidence() -> None:
    run = {
        "output_label": "RISK_REVIEW",
        "confidence": 0.88,
        "model_version": "router-test-v1",
        "shadow": 1,
        "created_at": "2026-08-23T01:02:03+00:00",
        "output": _model_result(event_version=3),
    }
    projected = project_public_risk_assessment(run, current_version=3)
    assert projected == {
        "route": "RISK_REVIEW",
        "confidence": 0.88,
        "confidence_applicable": True,
        "model_version": "router-test-v1",
        "decision_source": "TRAINED_SEMANTIC_MODEL",
        "evidence_state": "INSUFFICIENT",
        "evaluated_at": "2026-08-23T01:02:03+00:00",
        "shadow": True,
        "current": True,
    }
    assert project_public_risk_assessment(run, current_version=4) is None

    gated_run = {
        **run,
        "output_label": "ABSTAIN",
        "confidence": 1.0,
        "output": _model_result(
            event_version=3,
            label="ABSTAIN",
            confidence=1.0,
            confidence_applicable=False,
        ),
    }
    gated = project_public_risk_assessment(gated_run, current_version=3)
    assert gated is not None
    assert gated["route"] == "ABSTAIN"
    assert gated["confidence"] is None
    assert gated["confidence_applicable"] is False
    assert gated["decision_source"] == "DETERMINISTIC_EVIDENCE_GATE"


def test_qwen_semantics_are_separate_from_fact_confirmation() -> None:
    run = {
        "model_version": "qwen-risk-abc",
        "created_at": "2026-08-25T01:02:03+00:00",
        "output": {
            "model_task": "QWEN_RISK_SEMANTICS",
            "event_version": 3,
            "polarity": "ADVERSE",
            "materiality": "MATERIAL_ADVERSE",
            "adverse_strength": "HIGH",
            "semantic_priority": "PRIORITY_REVIEW",
            "assessment_scope": "SOURCE_CONDITIONAL",
            "model_version": "qwen-risk-abc",
        },
    }
    projected = project_public_qwen_semantics(run, current_version=3)
    assert projected is not None
    assert projected["conditional_language_required"] is True
    assert projected["confirms_event_fact"] is False
    assert projected["confidence"] is None
    assert project_public_qwen_semantics(run, current_version=4) is None


def test_latest_model_runs_are_batched_and_match_requested_event_versions(
    tmp_path: Path,
) -> None:
    operations = OperationsRepository(tmp_path / "operations.sqlite3")
    operations.record_model_run(
        "event-a", _model_result(event_version=1, input_marker="a")
    )
    operations.record_model_run(
        "event-a", _model_result(event_version=2, input_marker="b")
    )
    operations.record_model_run(
        "event-b",
        _model_result(event_version=4, label="NON_TARGET", input_marker="c"),
    )
    qwen = {
        **_model_result(event_version=1, input_marker="qwen"),
        "model_task": "QWEN_RISK_SEMANTICS",
        "model_version": "qwen-risk-abc",
        "label": "PRIORITY_REVIEW",
    }
    operations.record_model_run("event-a", qwen)

    selected = operations.latest_model_runs_for_versions(
        {"event-a": 1, "event-b": 4, "event-missing": 1}
    )

    assert set(selected) == {"event-a", "event-b"}
    assert selected["event-a"]["event_version"] == 1
    assert selected["event-a"]["model_version"] == "router-test-v1"
    assert selected["event-a"]["output"]["event_version"] == 1
    assert selected["event-b"]["output"]["label"] == "NON_TARGET"
    qwen_selected = operations.latest_qwen_risk_runs_for_versions({"event-a": 1})
    assert qwen_selected["event-a"]["model_version"] == "qwen-risk-abc"

    with sqlite3.connect(operations.path) as connection:
        plan = connection.execute(
            """EXPLAIN QUERY PLAN
               SELECT run_id FROM model_runs
               WHERE event_id=? AND event_version=?
               ORDER BY created_at DESC,run_id DESC LIMIT 1""",
            ("event-a", 1),
        ).fetchall()
    assert any("idx_model_event_version_created" in str(row[-1]) for row in plan)


def test_schema_9_model_versions_are_backfilled_for_indexed_current_reads(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-operations.sqlite3"
    legacy_columns = """
        run_id TEXT PRIMARY KEY, event_id TEXT, input_sha256 TEXT NOT NULL,
        model_version TEXT NOT NULL, output_label TEXT NOT NULL,
        confidence REAL NOT NULL, latency_ms REAL NOT NULL,
        shadow INTEGER NOT NULL, created_at TEXT NOT NULL,
        output_json TEXT NOT NULL, idempotency_key TEXT
    """
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE operations_schema(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO operations_schema VALUES (9,'2026-08-22T00:00:00+00:00')"
        )
        connection.execute(f"CREATE TABLE model_runs({legacy_columns})")
        for run_id, version, created_at in (
            ("legacy-v1", 1, "2026-08-22T01:00:00+00:00"),
            ("legacy-v2", 2, "2026-08-22T02:00:00+00:00"),
        ):
            output = _model_result(event_version=version, input_marker=str(version))
            connection.execute(
                """INSERT INTO model_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    "legacy-event",
                    output["input_sha256"],
                    output["model_version"],
                    output["label"],
                    output["confidence"],
                    output["latency_ms"],
                    1,
                    created_at,
                    json.dumps(output, sort_keys=True),
                    None,
                ),
            )
        connection.execute(
            """INSERT INTO model_runs VALUES (
                   'legacy-unversioned','legacy-event',?,'router-test-v1','ABSTAIN',
                   0.0,1.0,1,'2026-08-22T03:00:00+00:00','{}',NULL
               )""",
            ("f" * 64,),
        )
        connection.commit()

    operations = OperationsRepository(path)

    with sqlite3.connect(path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(model_runs)")
        }
        versions = dict(
            connection.execute(
                "SELECT run_id,event_version FROM model_runs ORDER BY run_id"
            ).fetchall()
        )
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(model_runs)")
        }
        schema_version = connection.execute(
            "SELECT MAX(version) FROM operations_schema"
        ).fetchone()[0]

    assert "event_version" in columns
    assert versions == {
        "legacy-unversioned": None,
        "legacy-v1": 1,
        "legacy-v2": 2,
    }
    assert "idx_model_event_version_created" in indexes
    assert schema_version == 10

    selected = operations.latest_model_runs_for_versions({"legacy-event": 1})
    assert selected["legacy-event"]["run_id"] == "legacy-v1"
    assert selected["legacy-event"]["output"]["event_version"] == 1


def _api_settings(root: Path, ledger_path: Path) -> Settings:
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


def _captured_event_ledger(path: Path) -> None:
    timestamp = "2026-08-23T00:00:00+00:00"
    connection = open_ledger(path)
    try:
        connection.execute(
            "INSERT INTO sources VALUES ('src','Source','professional_media','P2',1,1,?,?)",
            (timestamp, timestamp),
        )
        connection.execute(
            """INSERT INTO canonical_events VALUES (
               'semantic-event',1,'candidate','candidate','regulatory','filing',
               '2026-08-23',?,?,NULL,'SEM','Semantic Corp',NULL,NULL,'src',1)""",
            (timestamp, timestamp),
        )
        connection.execute(
            """INSERT INTO event_versions VALUES (
               'semantic-event',1,?,'candidate','candidate','regulatory','filing',
               NULL,?,'seed')""",
            (timestamp, json.dumps({}, sort_keys=True)),
        )
        connection.execute(
            """INSERT INTO raw_observations(
               observation_id,source_id,external_id,source_published_at,local_received_at,
               title,summary,canonical_url,content_sha256,raw_json,observation_status
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "semantic-observation",
                "src",
                "semantic-external",
                timestamp,
                timestamp,
                "Semantic Corp filing report",
                "A captured report that still needs a citable primary passage.",
                "https://example.test/report",
                "1" * 64,
                "{}",
                "captured",
            ),
        )
        connection.execute(
            """INSERT INTO event_observations VALUES (
               'semantic-event','semantic-observation','discovery',?)""",
            (timestamp,),
        )
        connection.commit()
    finally:
        connection.close()


def test_public_list_detail_and_dossier_share_current_semantics(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.sqlite3"
    _captured_event_ledger(ledger_path)
    settings = _api_settings(tmp_path, ledger_path)
    operations = OperationsRepository(settings.operations_db)
    operations.record_model_run(
        "semantic-event",
        _model_result(
            event_version=1,
            label="ABSTAIN",
            confidence=1.0,
            confidence_applicable=False,
        ),
    )

    application = create_app(settings)
    with TestClient(application) as client:
        feed_response = client.get("/api/v1/events")
        detail_response = client.get("/api/v1/events/semantic-event")
        dossier_response = client.get("/api/v1/events/semantic-event/dossier")

    assert feed_response.status_code == 200
    assert detail_response.status_code == 200
    assert dossier_response.status_code == 200
    assert feed_response.json()["schema_version"] == "1.3"

    feed_event = feed_response.json()["data"]["items"][0]
    detail_event = detail_response.json()["data"]["event"]
    dossier_event = dossier_response.json()["data"]["detail"]["event"]
    for event in (feed_event, detail_event, dossier_event):
        assert event["citation_ready"] is False
        assert event["evidence_posture"] == "SOURCE_CAPTURED"
        assert event["risk_assessment"]["route"] == "ABSTAIN"
        assert event["risk_assessment"]["confidence"] is None
        assert event["risk_assessment"]["confidence_applicable"] is False
        assert event["risk_assessment"]["current"] is True


def test_public_events_remain_available_when_model_store_read_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ledger_path = tmp_path / "ledger.sqlite3"
    _captured_event_ledger(ledger_path)
    settings = _api_settings(tmp_path, ledger_path)

    def unavailable_model_store(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        OperationsRepository,
        "latest_model_runs_for_versions",
        unavailable_model_store,
    )
    application = create_app(settings)
    with TestClient(application) as client:
        responses = (
            client.get("/api/v1/overview"),
            client.get("/api/v1/events"),
            client.get("/api/v1/events/semantic-event"),
            client.get("/api/v1/events/semantic-event/dossier"),
        )

    assert all(response.status_code == 200 for response in responses)
    overview, feed, detail, dossier = (response.json()["data"] for response in responses)
    assert isinstance(overview["recent_events"], list)
    assert feed["items"][0]["risk_assessment"] is None
    assert detail["event"]["risk_assessment"] is None
    assert dossier["detail"]["event"]["risk_assessment"] is None
