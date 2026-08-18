from __future__ import annotations

import json
import hashlib
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.api.main import create_app
from app.config import Settings
from app.models.risk_label_contract import validate_annotation
from app.services import AdjudicationService
from app.storage import LedgerRepository, OperationsRepository
from event_ledger import open_ledger, utc_now


def principal(name: str) -> str:
    return hashlib.sha256(
        f"finance-radar-reviewer-principal-v1:{name.casefold()}".encode("utf-8")
    ).hexdigest()


def build_ledger(path: Path) -> None:
    connection = open_ledger(path)
    now = utc_now()
    connection.execute(
        "INSERT INTO sources VALUES ('sec','SEC','official_primary','P0',1,1,?,?)",
        (now, now),
    )
    for index, event_id in enumerate(("evt-a", "evt-b"), 1):
        observation_id = f"obs-{index}"
        connection.execute(
            """INSERT INTO raw_observations VALUES (
               ?,'sec',?, '2026-07-17',?,'Issuer update','The issuer filed a material 8-K.',
               ?,?, '{}','captured')""",
            (
                observation_id,
                f"filing-{index}",
                now,
                f"https://sec.example/{index}",
                f"hash-{index}",
            ),
        )
        connection.execute(
            """INSERT INTO canonical_events VALUES (
               ?,1,'verified','verified','corporate','filing','2026-07-17',
               ?,?,?,'TST','Test Company','A++','A++','test',1)""",
            (event_id, now, now, f"stable-{index}"),
        )
        connection.execute(
            """INSERT INTO event_versions VALUES (
               ?,1,?,'verified','verified','corporate','filing','A++',?,'fixture')""",
            (
                event_id,
                now,
                json.dumps(
                    {
                        "evidence_summary": "The filing contains an independently reviewable material disclosure.",
                        "confirmed_facts": ["The issuer filed an 8-K with an exact source passage."],
                    }
                ),
            ),
        )
        connection.execute(
            "INSERT INTO event_observations VALUES (?,?, 'primary',?)",
            (event_id, observation_id, now),
        )
        entity_id = f"issuer-{index}"
        connection.execute(
            "INSERT INTO entities VALUES (?,?,?,?,?,?)",
            (entity_id, "ISSUER", f"Test Company {index}", "[]", now, now),
        )
        connection.execute(
            "INSERT INTO event_entities VALUES (?,?,?,?,?)",
            (event_id, entity_id, "SUBJECT", 1.0, now),
        )
        chain_id = f"chain-{index}"
        connection.execute(
            "INSERT INTO event_chains VALUES (?,?,?,?,?,?,1)",
            (chain_id, "issuer_event", chain_id, event_id, now, now),
        )
        connection.execute(
            "INSERT INTO event_chain_members VALUES (?,?, 'primary_event',1,?,?)",
            (chain_id, event_id, "fixture primary event", now),
        )
        connection.execute(
            """INSERT INTO event_evidence VALUES (
               ?,?,?,?,'2026-07-17','8-K','1.03',?, 'material filing',10,
               'confirmed',0,?,?)""",
            (
                f"evidence-{index}",
                event_id,
                observation_id,
                f"https://sec.example/{index}",
                "The issuer disclosed a material event in an exact primary-source passage.",
                now,
                now,
            ),
        )
    connection.commit()
    connection.close()


@pytest.fixture()
def workflow():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        ledger_path = root / "ledger.sqlite3"
        build_ledger(ledger_path)
        operations = OperationsRepository(root / "operations.sqlite3")
        service = AdjudicationService(LedgerRepository(ledger_path), operations)
        yield root, ledger_path, operations, service


def review(service: AdjudicationService, sample_id: str, reviewer_id: str, **overrides):
    payload = {
        "reviewer_id": principal(reviewer_id),
        "role": "REVIEWER",
        "materiality": "MATERIAL_ADVERSE",
        "polarity": "ADVERSE",
        "evidence_state": "PRIMARY_SUPPORTED",
        "rationale": "Exact primary evidence supports a material adverse review decision.",
    }
    payload.update(overrides)
    return service.submit_review(sample_id, **payload)


def test_matching_dual_review_derives_label_without_preassignment(workflow) -> None:
    _root, _ledger, operations, service = workflow
    created = service.create_sample_from_event("evt-a")
    assert created["created"] is True
    sample_id = created["sample_id"]
    sample = operations.adjudication_sample(sample_id)
    assert "label" not in sample
    queue_a = service.queue(principal("reviewer-a"))
    assert queue_a["items"][0]["peer_answers_hidden"] is True
    assert "source_id" not in queue_a["items"][0]
    assert queue_a["items"][0]["no_model_prediction_shown"] is True
    assert queue_a["items"][0]["no_market_outcome_shown"] is True

    first = review(service, sample_id, "reviewer-a")
    assert first["status"] == "IN_REVIEW"
    assert service.queue(principal("reviewer-a"))["items"] == []
    queue_b = service.queue(principal("reviewer-b"))
    assert len(queue_b["items"]) == 1
    assert "conflict_options" not in queue_b["items"][0]

    second = review(service, sample_id, "reviewer-b")
    assert second["status"] == "READY"
    assert second["derived_label"] == "RISK_REVIEW"
    assert second["resolution"] == "CONSENSUS"
    annotation = service.annotation(sample_id)
    assert validate_annotation(annotation) == []
    assert annotation["source_used_as_label"] is False
    assert annotation["split"] == "UNASSIGNED"
    assert annotation["adjudicator_id"] != annotation["reviewer_id"]


def test_conflict_requires_distinct_third_arbiter(workflow) -> None:
    _root, _ledger, _operations, service = workflow
    sample_id = service.create_sample_from_event("evt-b")["sample_id"]
    review(service, sample_id, "reviewer-a")
    second = review(
        service,
        sample_id,
        "reviewer-b",
        materiality="NOT_MATERIAL_ADVERSE",
        polarity="POSITIVE",
    )
    assert second["status"] == "CONFLICT"
    arbiter_queue = service.queue(principal("reviewer-c"), role="ARBITER")
    assert len(arbiter_queue["items"]) == 1
    assert len(arbiter_queue["items"][0]["conflict_options"]) == 2
    with pytest.raises(ValueError, match="already submitted"):
        service.submit_review(
            sample_id,
            reviewer_id=principal("reviewer-a"),
            role="ARBITER",
            materiality="UNCLEAR",
            polarity="UNCLEAR",
            evidence_state="CONFLICTED",
            rationale="The evidence remains conflicted after independent review.",
        )
    resolved = service.submit_review(
        sample_id,
        reviewer_id=principal("reviewer-c"),
        role="ARBITER",
        materiality="UNCLEAR",
        polarity="UNCLEAR",
        evidence_state="CONFLICTED",
        rationale="The two readings conflict, so the evidence must remain unresolved.",
    )
    assert resolved["status"] == "READY"
    assert resolved["derived_label"] == "ABSTAIN"
    assert resolved["resolution"] == "ARBITRATED"


def test_pre_freeze_report_stays_blocked_without_real_minimums(workflow) -> None:
    _root, _ledger, _operations, service = workflow
    sample_id = service.create_sample_from_event("evt-a")["sample_id"]
    review(service, sample_id, "reviewer-a")
    review(service, sample_id, "reviewer-b")
    report = service.pre_freeze_report()
    assert report["status"] == "NOT_READY_FOR_FREEZE"
    assert report["valid_annotations"] == 1
    assert report["label_counts"] == {"RISK_REVIEW": 1}
    assert report["split"] == "UNASSIGNED"
    assert report["production_changed"] is False
    assert report["blind_v2_frozen"] is False


def test_freeze_candidate_is_zero_overlap_and_commit_is_one_way(workflow) -> None:
    _root, _ledger, operations, service = workflow
    sample_id = service.create_sample_from_event("evt-a")["sample_id"]
    review(service, sample_id, "reviewer-a")
    review(service, sample_id, "reviewer-b")

    candidate = service.build_freeze_candidate(
        minimums={"RISK_REVIEW": 1},
        minimum_source_groups=1,
    )
    assert candidate["row_count"] == 1
    assert candidate["label_counts"] == {"RISK_REVIEW": 1}
    assert candidate["entity_overlap_count"] == 0
    assert candidate["event_chain_overlap_count"] == 0
    assert candidate["near_duplicate_overlap_count"] == 0
    assert candidate["model_predictions_included"] is False
    assert candidate["rows"][0]["content"]["contract_version"] == "human-blind-v3.1"

    assert operations.freeze_adjudication_samples(
        [sample_id], candidate["freeze_id"]
    ) == 1
    assert operations.adjudication_sample(sample_id)["status"] == "FROZEN"
    with pytest.raises(ValueError, match="frozen sample"):
        review(service, sample_id, "reviewer-c")


def test_api_exposes_guarded_queue_and_review_contract(workflow) -> None:
    root, ledger_path, _operations, _service = workflow
    settings = Settings(
        ledger_db=ledger_path,
        operations_db=root / "api-operations.sqlite3",
        artifact_dir=root / "artifacts",
        evidence_object_dir=root / "objects",
        replay_dir=ROOT / "replay" / "cases",
        admin_token="test-secret",
        reviewer_principals=(
            ("reviewer-a", "REVIEWER", "reviewer-a-bound-token-000001"),
            ("reviewer-b", "REVIEWER", "reviewer-b-bound-token-000002"),
            ("reviewer-c", "ARBITER", "reviewer-c-bound-token-000003"),
        ),
    )
    with TestClient(create_app(settings)) as client:
        denied = client.post("/api/v1/adjudication/samples/from-event/evt-a")
        assert denied.status_code == 403
        created = client.post(
            "/api/v1/adjudication/samples/from-event/evt-a",
            headers={"X-Admin-Token": "test-secret"},
        )
        assert created.status_code == 200
        sample_id = created.json()["data"]["sample_id"]
        queue = client.get(
            "/api/v1/adjudication/queue",
            headers={"X-Reviewer-Token": "reviewer-a-bound-token-000001"},
        )
        assert queue.status_code == 200
        item = queue.json()["data"]["items"][0]
        assert "source_id" not in item
        assert "label" not in item
        response = client.post(
            f"/api/v1/adjudication/samples/{sample_id}/reviews",
            headers={"X-Reviewer-Token": "reviewer-a-bound-token-000001"},
            json={
                "materiality": "MATERIAL_ADVERSE",
                "polarity": "ADVERSE",
                "evidence_state": "PRIMARY_SUPPORTED",
                "rationale": "Exact primary evidence supports a material adverse review decision.",
            },
        )
        assert response.status_code == 200
        assert response.json()["data"]["target_label_submitted"] is False
        assert response.json()["data"]["credential_bound"] is True
        assert response.json()["data"]["reviewer_principal"].startswith("human-")
        admin_cannot_impersonate = client.get(
            "/api/v1/adjudication/queue",
            headers={"X-Admin-Token": "test-secret"},
        )
        assert admin_cannot_impersonate.status_code == 403
        spoofed = client.post(
            f"/api/v1/adjudication/samples/{sample_id}/reviews",
            headers={"X-Reviewer-Token": "reviewer-b-bound-token-000002"},
            json={
                "reviewer_id": "reviewer-c",
                "role": "ARBITER",
                "materiality": "MATERIAL_ADVERSE",
                "polarity": "ADVERSE",
                "evidence_state": "PRIMARY_SUPPORTED",
                "rationale": "A client supplied alias must never select the authenticated principal.",
            },
        )
        assert spoofed.status_code == 422
        health = client.get("/api/v1/health").json()["data"]
        assert health["operations"]["schema_version"] == 6
        assert health["operations"]["counts"]["adjudication_samples"] == 1
