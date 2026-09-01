from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.main import create_app
from app.config import Settings
from app.models.qwen_risk_contract import (
    QWEN_RISK_CONTRACT_VERSION,
    QWEN_RISK_PROMPT_VERSION,
)
from app.services import (
    CAPTURE_INTERPRETATION_CONTRACT,
    CAPTURE_INTERPRETATION_PROMPT_SHA256,
    DEEPSEEK_CHEAP_TEXT_MODEL,
    build_qwen_risk_input_contract,
    normalized_capture_input,
)
from app.services.capture_interpretation import CAPTURE_INTERPRETATION_PROMPT_VERSION
from app.services.public_event_semantics import (
    derive_public_display_headline,
    derive_public_event_semantics,
    derive_public_source_provenance,
    project_public_qwen_semantics,
    project_public_risk_assessment,
)
from app.storage import LedgerRepository, OperationsRepository
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
        "source_provenance": {
            "classification_version": "public-source-provenance-v1",
            "access": "CLAIM_SOURCE_LINKED",
            "origin_kind": "PRIMARY",
            "displayable_source_count": 1,
            "primary_source_count": 1,
            "public_source_url_count": 1,
            "captured_text_count": 0,
            "problem_source_count": 0,
        },
        "claim_citation": {
            "ready": True,
            "supporting_passage_count": 1,
        },
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
        "source_provenance": {
            "classification_version": "public-source-provenance-v1",
            "access": "PRIMARY_SOURCE",
            "origin_kind": "PRIMARY",
            "displayable_source_count": 2,
            "primary_source_count": 1,
            "public_source_url_count": 1,
            "captured_text_count": 0,
            "problem_source_count": 0,
        },
        "claim_citation": {
            "ready": False,
            "supporting_passage_count": 2,
        },
    }

    captured = derive_public_event_semantics(
        {
            "reader_ready": 0,
            "reader_has_subject": 0,
            "reader_has_fact_summary": 0,
            "citable_evidence_count": 0,
            "captured_source_count": 1,
            "captured_text_count": 1,
        }
    )
    assert captured["evidence_posture"] == "SOURCE_CAPTURED"
    assert captured["source_provenance"]["access"] == "CAPTURE_ONLY"
    assert captured["evidence_gap_codes"] == [
        "MISSING_SUBJECT",
        "MISSING_FACT_SUMMARY",
        "MISSING_CITABLE_EVIDENCE",
    ]

    no_source = derive_public_event_semantics({})
    assert no_source["evidence_posture"] == "NO_SOURCE"
    assert no_source["source_provenance"]["access"] == "NO_PUBLIC_SOURCE"
    assert no_source["evidence_gap_codes"][-1] == "NO_CAPTURED_SOURCE"


def test_source_provenance_uses_accessible_material_not_review_progress() -> None:
    primary = derive_public_source_provenance(
        {"reader_ready": 0, "captured_source_count": 1},
        {
            "authority_tier": "P0_official",
            "canonical_url": "https://www.sec.gov/Archives/example.htm",
            "title": "Issuer filing",
        },
    )
    assert primary["access"] == "PRIMARY_SOURCE"
    assert primary["origin_kind"] == "PRIMARY"

    publisher = derive_public_source_provenance(
        {"reader_ready": 0, "captured_source_count": 1},
        {
            "authority_tier": "P2",
            "canonical_url": "https://publisher.example/story",
            "summary": "A publisher-supplied summary.",
        },
    )
    assert publisher["access"] == "PUBLIC_SOURCE"
    assert publisher["origin_kind"] == "PUBLISHER"

    saved = derive_public_source_provenance(
        {"reader_ready": 0, "captured_source_count": 1},
        {"summary": "A retained source receipt without a public URL."},
    )
    assert saved["access"] == "CAPTURE_ONLY"

    problem = derive_public_source_provenance(
        {"reader_ready": 0, "source_problem_count": 1}
    )
    assert problem["access"] == "SOURCE_PROBLEM"


def test_source_provenance_rejects_non_public_urls() -> None:
    for source_url in (
        "file:///etc/passwd",
        "http://localhost/private",
        "http://127.0.0.1/private",
        "http://10.0.0.8/private",
        "https://user" + ":secret@example.test/story",
        "https://metadata.google.internal/computeMetadata/v1/",
    ):
        provenance = derive_public_source_provenance(
            {"reader_ready": 0, "captured_source_count": 1},
            {"authority_tier": "P0", "canonical_url": source_url},
        )
        assert provenance["access"] == "NO_PUBLIC_SOURCE"
        assert provenance["public_source_url_count"] == 0
        assert provenance["primary_source_count"] == 0

    retained = derive_public_source_provenance(
        {"reader_ready": 0, "captured_source_count": 1},
        {
            "canonical_url": "http://127.0.0.1/private",
            "summary": "Retained source text remains available for inspection.",
        },
    )
    assert retained["access"] == "CAPTURE_ONLY"


def test_public_display_headline_preserves_provenance_modes() -> None:
    supported = derive_public_display_headline(
        {
            "reader_ready": 1,
            "public_fact_summary": "Example Corp 任命 Jane Doe 为首席财务官",
            "company_name": "Example Corp",
            "event_date": "2026-08-26",
        },
        {"source_title": "A different source headline"},
    )
    assert supported == {
        "display_headline": "Example Corp 任命 Jane Doe 为首席财务官",
        "headline_mode": "FACT",
        "headline_source": None,
    }

    attributed = derive_public_display_headline(
        {
            "reader_ready": 0,
            "company_name": "Example Corp",
            "event_date": "2026-08-26",
        },
        {
            "source_title": "Example Corp announces a financing update",
            "source_name": "SEC",
        },
    )
    assert attributed == {
        "display_headline": "Example Corp announces a financing update",
        "headline_mode": "ATTRIBUTED_SOURCE",
        "headline_source": "SEC",
    }

    record = derive_public_display_headline(
        {
            "reader_ready": 0,
            "company_name": "Example Corp",
            "event_date": "2026-08-26",
        }
    )
    assert record == {
        "display_headline": "Example Corp · 2026-08-26",
        "headline_mode": "RECORD",
        "headline_source": None,
    }


def test_public_display_headline_uses_complete_sec_action_without_promoting_it() -> None:
    result = derive_public_display_headline(
        {
            "reader_ready": 0,
            "reader_has_fact_summary": 1,
            "discovery_source": "sec_current_filings",
            "company_name": "Example Corp",
            "claim_subject": "Example Corp",
            "claim_action": "going_concern_financing_dependency",
            "public_fact_summary": (
                "原文中的 the Company 披露持续经营重大疑虑。"
                "系统记录阶段为 DISCLOSED；以上仅为规则抽取，尚待人工核验。"
            ),
            "event_date": "2026-08-26",
        },
        {
            "source_title": "8-K - Example Corp (0000123456) (Filer)",
            "source_name": "SEC",
        },
    )

    assert result == {
        "display_headline": "Example Corp 申报文件披露持续经营存在重大疑虑",
        "headline_mode": "ATTRIBUTED_SOURCE",
        "headline_source": "SEC",
    }
    assert "尚待" not in result["display_headline"]
    assert "规则抽取" not in result["display_headline"]

    future_action = derive_public_display_headline(
        {
            "reader_ready": 0,
            "reader_has_fact_summary": 1,
            "discovery_source": "sec_current_filings",
            "claim_subject": "Example Corp",
            "claim_action": "future_deterministic_action",
            "public_fact_summary": (
                "Example Corp 批准一项新的确定性公司行动。"
                "系统记录阶段为 DISCLOSED；以上仅为规则抽取，尚待人工核验。"
            ),
        }
    )
    assert future_action["display_headline"] == (
        "Example Corp 批准一项新的确定性公司行动。"
    )
    assert future_action["headline_mode"] == "ATTRIBUTED_SOURCE"


def test_public_display_headline_skips_generic_recovery_title() -> None:
    result = derive_public_display_headline(
        {"reader_ready": 0, "discovery_source": "historical_recovery"},
        {
            "source_title": "Accepted official evidence for ADTX",
            "source_summary": "Nasdaq suspended trading in Aditxt shares on June 25.",
            "source_name": "Nasdaq",
        },
    )

    assert result["display_headline"] == "Nasdaq suspended trading in Aditxt shares on June 25."
    assert result["headline_mode"] == "ATTRIBUTED_SOURCE"


def test_public_display_headline_prefers_event_excerpt_over_generic_filing_title() -> None:
    result = derive_public_display_headline(
        {
            "reader_ready": 0,
            "company_name": "Flushing Financial Corp",
            "event_date": "2026-08-26",
            "discovery_source": "sharadar_active_research",
        },
        {
            "source_title": "SEC 8-K FFIC",
            "source_summary": "Each Flushing share was converted into 0.85 shares under the completed merger.",
        },
    )

    assert result["display_headline"].startswith("Each Flushing share was converted")
    assert result["headline_mode"] == "ATTRIBUTED_SOURCE"
    assert result["headline_source"] is None


def test_public_display_headline_rejects_form_25_and_discovery_boilerplate() -> None:
    base_event = {
        "reader_ready": 0,
        "company_name": "Example Corp",
        "event_date": "2026-08-26",
    }
    for summary in (
        "certifies that it has reasonable grounds to believe that it meets all of the requirements for filing the Form 25",
        "action in delisted/voluntarydelisting; value=delisted",
    ):
        result = derive_public_display_headline(
            base_event,
            {"source_title": "SEC 25-NSE EXM", "source_summary": summary},
        )
        assert result["headline_mode"] == "RECORD"
        assert result["display_headline"] == "Example Corp · 2026-08-26"


def test_public_display_headline_rejects_sec_directory_metadata() -> None:
    result = derive_public_display_headline(
        {
            "reader_ready": 0,
            "company_name": "Nano Dimension Ltd.",
            "event_date": "2026-08-18",
        },
        {
            "source_title": "8-K - Nano Dimension Ltd. (0001643303) (Filer)",
            "source_summary": (
                "Filed: 2026-08-18 AccNo: 0001193125-26-354738 Size: 207 KB "
                "Item 5.02: Departure of Directors or Certain Officers"
            ),
        },
    )

    assert result["headline_mode"] == "RECORD"
    assert result["display_headline"] == "Nano Dimension Ltd. · 2026-08-18"


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
        "publication_state": "PUBLIC_APPROVED",
        "current_input": True,
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
    assert projected["publication_state"] == "PUBLIC_APPROVED"
    assert projected["shadow"] is False
    assert project_public_qwen_semantics(run, current_version=4) is None
    assert (
        project_public_qwen_semantics(
            {**run, "publication_state": "SHADOW_ACCEPTED"},
            current_version=3,
        )
        is None
    )


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
        assert event["display_headline"] == "Semantic Corp filing report"
        assert event["headline_mode"] == "ATTRIBUTED_SOURCE"
        assert event["headline_source"] == "Source"


def test_public_sec_list_keeps_semantic_headline_without_exposing_claim_slots(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "ledger.sqlite3"
    _captured_event_ledger(ledger_path)
    structured_fact = {
        "public_fact_summary": (
            "原文中的 the Company 披露持续经营重大疑虑。"
            "系统记录阶段为 DISCLOSED；以上仅为规则抽取，尚待人工核验。"
        ),
        "claim_subject": "Semantic Corp",
        "claim_action": "going_concern_financing_dependency",
        "claim_stage": "DISCLOSED",
        "known_at": "2026-08-23T00:00:00+00:00",
    }
    with sqlite3.connect(ledger_path) as connection:
        connection.execute(
            "UPDATE canonical_events SET discovery_source='sec_current_filings' "
            "WHERE event_id='semantic-event'"
        )
        connection.execute(
            "UPDATE event_versions SET facts_json=? WHERE event_id='semantic-event'",
            (json.dumps(structured_fact, sort_keys=True),),
        )
        connection.commit()

    application = create_app(_api_settings(tmp_path, ledger_path))
    with TestClient(application) as client:
        response = client.get("/api/v1/events?source=sec_current_filings")

    assert response.status_code == 200
    event = response.json()["data"]["items"][0]
    assert event["display_headline"] == (
        "Semantic Corp 申报文件披露持续经营存在重大疑虑"
    )
    assert event["headline_mode"] == "ATTRIBUTED_SOURCE"
    assert event["headline_source"] == "SEC"
    assert event["citation_ready"] is False
    assert event["public_fact_summary"] is None
    assert event["claim_action"] is None
    assert "尚待" not in event["display_headline"]


def test_qwen_publication_is_closed_until_approved_and_current_input_matches(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "ledger.sqlite3"
    _captured_event_ledger(ledger_path)
    settings = _api_settings(tmp_path, ledger_path)
    ledger = LedgerRepository(ledger_path)
    operations = OperationsRepository(settings.operations_db)
    item = ledger.shadow_batch(event_ids=["semantic-event"], order="event_id")[0]
    adapter = "a" * 64
    model_version = "qwen-risk-" + adapter[:16]
    contract = build_qwen_risk_input_contract(
        item["detail"], item["evidence"], model_version=model_version
    )
    result = {
        **contract,
        "model_task": "QWEN_RISK_SEMANTICS",
        "adapter_sha256": adapter,
        "event_version": 1,
        "event_status": "candidate",
        "polarity": "ADVERSE",
        "materiality": "MATERIAL_ADVERSE",
        "adverse_strength": "HIGH",
        "semantic_priority": "PRIORITY_REVIEW",
        "label": "PRIORITY_REVIEW",
        "confidence": 0.0,
        "confidence_applicable": False,
        "latency_ms": 1.0,
        "shadow": True,
        "no_trading": True,
    }
    operations.record_model_run("semantic-event", result)
    application = create_app(settings)

    with TestClient(application) as client:
        assert client.get("/api/v1/events/semantic-event").json()["data"][
            "event"
        ]["semantic_assessment"] is None

        operations.set_state(
            "qwen_risk_publication_v1",
            {
                "state": "PUBLIC_APPROVED",
                "model_version": model_version,
                "adapter_sha256": adapter,
                "contract_version": QWEN_RISK_CONTRACT_VERSION,
                "prompt_version": QWEN_RISK_PROMPT_VERSION,
                "approval_receipt_sha256": "b" * 64,
                "approved_at": "2026-08-25T01:00:00+00:00",
            },
        )
        approved = client.get("/api/v1/events/semantic-event").json()["data"][
            "event"
        ]["semantic_assessment"]
        assert approved["publication_state"] == "PUBLIC_APPROVED"
        assert approved["shadow"] is False

        # A source revision invalidates the previously approved input even when
        # the canonical event version has not changed.
        with sqlite3.connect(ledger_path) as connection:
            connection.execute(
                """UPDATE raw_observations
                   SET title='Revised source title',content_sha256=?
                   WHERE observation_id='semantic-observation'""",
                ("2" * 64,),
            )
            connection.commit()
        invalidated = client.get("/api/v1/events/semantic-event").json()["data"][
            "event"
        ]["semantic_assessment"]
        assert invalidated is None


def _approve_qwen_publication(
    operations: OperationsRepository,
    *,
    adapter_sha256: str,
) -> str:
    model_version = "qwen-risk-" + adapter_sha256[:16]
    operations.set_state(
        "qwen_risk_publication_v1",
        {
            "state": "PUBLIC_APPROVED",
            "model_version": model_version,
            "adapter_sha256": adapter_sha256,
            "contract_version": QWEN_RISK_CONTRACT_VERSION,
            "prompt_version": QWEN_RISK_PROMPT_VERSION,
            "approval_receipt_sha256": "b" * 64,
            "approved_at": "2026-08-25T01:00:00+00:00",
        },
    )
    return model_version


def _record_current_qwen_assessment(
    ledger: LedgerRepository,
    operations: OperationsRepository,
    *,
    model_version: str,
    adapter_sha256: str,
) -> None:
    item = ledger.shadow_batch(event_ids=["semantic-event"], order="event_id")[0]
    contract = build_qwen_risk_input_contract(
        item["detail"], item["evidence"], model_version=model_version
    )
    operations.record_model_run(
        "semantic-event",
        {
            **contract,
            "model_task": "QWEN_RISK_SEMANTICS",
            "adapter_sha256": adapter_sha256,
            "event_version": 1,
            "event_status": "candidate",
            "polarity": "ADVERSE",
            "materiality": "MATERIAL_ADVERSE",
            "adverse_strength": "HIGH",
            "semantic_priority": "PRIORITY_REVIEW",
            "training_basis": "DUAL_REVIEW_AI_CONSENSUS",
            "label": "PRIORITY_REVIEW",
            "confidence": 0.0,
            "confidence_applicable": False,
            "latency_ms": 1.0,
            "shadow": True,
            "no_trading": True,
        },
    )


def _semantic_assessment_payload(client: TestClient) -> dict:
    response = client.get("/api/v1/events/semantic-event/semantic-assessment")
    assert response.status_code == 200
    payload = response.json()["data"]
    assert set(payload) == {"state", "assessment", "cache_only"}
    assert payload["state"] in {"READY", "PROCESSING", "NOT_APPLICABLE"}
    assert payload["cache_only"] is True
    serialized = json.dumps(payload, ensure_ascii=False)
    for private_marker in (
        "input_sha256",
        "source_identity_sha256",
        "evidence_identity_sha256",
        "evidence_context_sha256",
        "adapter_sha256",
        "approval_receipt_sha256",
        "error",
        "traceback",
        "STALE",
        "UNPROCESSED",
        "INPUT_INSUFFICIENT",
    ):
        assert private_marker not in serialized
    return payload


def test_semantic_assessment_endpoint_returns_only_current_public_qwen_result(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "ledger.sqlite3"
    _captured_event_ledger(ledger_path)
    settings = _api_settings(tmp_path, ledger_path)
    ledger = LedgerRepository(ledger_path)
    operations = OperationsRepository(settings.operations_db)
    adapter = "a" * 64
    model_version = _approve_qwen_publication(
        operations,
        adapter_sha256=adapter,
    )
    _record_current_qwen_assessment(
        ledger,
        operations,
        model_version=model_version,
        adapter_sha256=adapter,
    )

    with TestClient(create_app(settings)) as client:
        payload = _semantic_assessment_payload(client)

    assert payload["state"] == "READY"
    assessment = payload["assessment"]
    assert assessment is not None
    assert assessment["polarity"] == "ADVERSE"
    assert assessment["materiality"] == "MATERIAL_ADVERSE"
    assert assessment["adverse_strength"] == "HIGH"
    assert assessment["semantic_priority"] == "PRIORITY_REVIEW"
    assert assessment["publication_state"] == "PUBLIC_APPROVED"
    assert assessment["current"] is True

    # A changed source invalidates the completed input immediately, but the
    # public endpoint must collapse that internal stale condition into the
    # same non-alarming PROCESSING state used while the worker catches up.
    with sqlite3.connect(ledger_path) as connection:
        connection.execute(
            "UPDATE raw_observations SET title='Revised source title',content_sha256=? "
            "WHERE observation_id='semantic-observation'",
            ("2" * 64,),
        )
        connection.commit()
    with TestClient(create_app(settings)) as client:
        invalidated = _semantic_assessment_payload(client)
    assert invalidated == {
        "state": "PROCESSING",
        "assessment": None,
        "cache_only": True,
    }


def test_semantic_assessment_endpoint_is_cache_only_while_qwen_is_unprocessed(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "ledger.sqlite3"
    _captured_event_ledger(ledger_path)
    settings = _api_settings(tmp_path, ledger_path)
    operations = OperationsRepository(settings.operations_db)
    _approve_qwen_publication(operations, adapter_sha256="a" * 64)
    with sqlite3.connect(settings.operations_db) as connection:
        before = connection.execute("SELECT COUNT(*) FROM model_runs").fetchone()[0]

    with TestClient(create_app(settings)) as client:
        first = _semantic_assessment_payload(client)
        second = _semantic_assessment_payload(client)

    assert first == second == {
        "state": "PROCESSING",
        "assessment": None,
        "cache_only": True,
    }
    with sqlite3.connect(settings.operations_db) as connection:
        after = connection.execute("SELECT COUNT(*) FROM model_runs").fetchone()[0]
    assert after == before == 0


def test_semantic_assessment_endpoint_marks_empty_input_not_applicable(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "ledger.sqlite3"
    _captured_event_ledger(ledger_path)
    settings = _api_settings(tmp_path, ledger_path)
    operations = OperationsRepository(settings.operations_db)
    _approve_qwen_publication(operations, adapter_sha256="a" * 64)
    with sqlite3.connect(ledger_path) as connection:
        connection.execute(
            "UPDATE raw_observations SET title='',summary='' "
            "WHERE observation_id='semantic-observation'"
        )
        connection.commit()

    with TestClient(create_app(settings)) as client:
        payload = _semantic_assessment_payload(client)

    assert payload == {
        "state": "NOT_APPLICABLE",
        "assessment": None,
        "cache_only": True,
    }
    with sqlite3.connect(settings.operations_db) as connection:
        count = connection.execute("SELECT COUNT(*) FROM model_runs").fetchone()[0]
    assert count == 0


def test_semantic_assessment_endpoint_hides_model_store_errors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ledger_path = tmp_path / "ledger.sqlite3"
    _captured_event_ledger(ledger_path)
    settings = _api_settings(tmp_path, ledger_path)
    operations = OperationsRepository(settings.operations_db)
    _approve_qwen_publication(operations, adapter_sha256="a" * 64)

    def unavailable_model_store(*_args, **_kwargs):
        raise sqlite3.OperationalError("SECRET_INTERNAL_DATABASE_PATH is locked")

    monkeypatch.setattr(
        OperationsRepository,
        "latest_qwen_risk_runs_for_versions",
        unavailable_model_store,
    )
    with TestClient(create_app(settings)) as client:
        payload = _semantic_assessment_payload(client)

    assert payload == {
        "state": "PROCESSING",
        "assessment": None,
        "cache_only": True,
    }
    assert "SECRET_INTERNAL_DATABASE_PATH" not in json.dumps(payload)


def test_capture_explanation_exposes_real_queue_retry_and_terminal_states(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "ledger.sqlite3"
    _captured_event_ledger(ledger_path)
    settings = _api_settings(tmp_path, ledger_path)
    ledger = LedgerRepository(ledger_path)
    operations = OperationsRepository(settings.operations_db)
    detail = ledger.event_detail("semantic-event")
    capture = ledger.captured_sources("semantic-event")[0]
    normalized = normalized_capture_input(detail["event"], capture)
    interpretation_id, inserted = operations.enqueue_capture_interpretation(
        "semantic-event",
        str(capture["observation_id"]),
        normalized,
        contract_version=CAPTURE_INTERPRETATION_CONTRACT,
        prompt_version=CAPTURE_INTERPRETATION_PROMPT_VERSION,
        prompt_sha256=CAPTURE_INTERPRETATION_PROMPT_SHA256,
        provider="deepseek",
        model_snapshot=DEEPSEEK_CHEAP_TEXT_MODEL,
        external_call=True,
    )
    assert inserted is True
    application = create_app(settings)
    endpoint = "/api/v1/events/semantic-event/capture-explanation"
    with TestClient(application) as client:
        queued = client.get(endpoint).json()["data"]
        assert queued["state"] == "QUEUED"
        assert queued["attempts"] == 0

        with sqlite3.connect(settings.operations_db) as connection:
            connection.execute(
                """UPDATE capture_interpretation_runs
                   SET status='PENDING',attempts=1,available_at=?
                   WHERE interpretation_id=?""",
                ("2026-08-25T02:00:00+00:00", interpretation_id),
            )
            connection.commit()
        retry = client.get(endpoint).json()["data"]
        assert retry["state"] == "RETRY_WAIT"
        assert retry["attempts"] == 1
        assert retry["next_retry_at"] == "2026-08-25T02:00:00+00:00"

        with sqlite3.connect(settings.operations_db) as connection:
            connection.execute(
                """UPDATE capture_interpretation_runs
                   SET status='FAILED',updated_at='2026-08-25T02:01:00+00:00'
                   WHERE interpretation_id=?""",
                (interpretation_id,),
            )
            connection.commit()
        terminal = client.get(endpoint).json()["data"]
        assert terminal["state"] == "FAILED_TERMINAL"
        assert terminal["source"]["capture_receipt_sha256"] == capture[
            "capture_receipt_sha256"
        ]


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
