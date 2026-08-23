from __future__ import annotations

import json
import sys
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import event_ledger
from app.api.main import create_app
from app.config import Settings
from app.evidence_policy import canonicalize_human_fact_claim
from app.services.event_fact_review import (
    _EVENT_EVIDENCE_SQL,
    _event_evidence,
    apply_consensus,
    build_assignment,
    build_authorization_template,
    merge_submissions,
    select_reviewable_events,
    sha256_json,
    validate_submission,
)
from app.storage.ledger import LedgerRepository


def queue_row(candidate_id: str, ticker: str, family: str = "bankruptcy_or_distress") -> dict[str, str]:
    return {
        "queue_rank": "1",
        "event_candidate_id": candidate_id,
        "stable_id": f"permaticker:{candidate_id}",
        "ticker_at_event": ticker,
        "company_name": f"{ticker} Corporation",
        "event_date": "2026-01-01",
        "event_family": family,
        "event_type": "bankruptcy_liquidation" if "bankruptcy" in family else "delisted",
        "detection_rule": "fixture candidate",
        "detection_value": "fixture",
        "priority_score": "100",
        "provisional_grade_cap": "A++_candidate",
        "sec_filings_url": f"https://www.sec.gov/{candidate_id}",
    }


def passage(candidate_id: str, ticker: str, family: str = "bankruptcy_or_distress") -> dict[str, str]:
    if family == "delisting_or_suspension":
        evidence_passage = (
            f"{ticker} Corporation delisted its common stock from Nasdaq "
            "on January 1, 2026."
        )
        items = "3.01"
        matched_keywords = "delisted common stock"
    else:
        evidence_passage = (
            f"{ticker} Corporation filed a voluntary petition under Chapter 11 "
            "in the United States Bankruptcy Court on January 1, 2026."
        )
        items = "1.03"
        matched_keywords = "chapter 11"
    return {
        "event_candidate_id": candidate_id,
        "accession_number": f"0001-26-{candidate_id}",
        "filing_date": "2026-01-01",
        "form": "8-K",
        "items": items,
        "filing_document_url": f"https://www.sec.gov/{candidate_id}/document",
        "text_sha256": (candidate_id.lower() * 64)[:64],
        "evidence_passage": evidence_passage,
        "matched_keywords": matched_keywords,
        "passage_score": "10",
        "passage_status": "candidate_passage",
    }


@pytest.fixture()
def ledger_path(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.sqlite3"
    connection = event_ledger.open_ledger(path)
    try:
        queues = [
            queue_row("C1", "AAA"),
            queue_row("C2", "BBB", "delisting_or_suspension"),
        ]
        passages = [
            passage("C1", "AAA"),
            passage("C2", "BBB", "delisting_or_suspension"),
        ]
        event_ledger.import_active_research(
            connection,
            queue_rows=queues,
            passage_rows=passages,
            adjudication_rows=[],
            market_rows=[],
        )
    finally:
        connection.close()
    return path


def submission(assignment: dict, reviewer_id: str, decision: str = "CONFIRM_EVENT") -> dict:
    results = []
    for event in assignment["events"]:
        selected = event["evidence"][0]
        human_fact_claim = None
        if decision == "CONFIRM_EVENT":
            passage_text = selected["evidence_passage"]
            if event["event_type"] == "delisted":
                fact_predicate = "DELISTED_OR_SUSPENDED"
                action_quote = "delisted"
                object_quote = "its common stock"
                stage = "EFFECTIVE"
            else:
                fact_predicate = "BANKRUPTCY_PETITION_FILED"
                action_quote = "filed a voluntary petition"
                object_quote = "under Chapter 11"
                stage = "FILED"
            human_fact_claim = {
                "contract_version": "human-fact-claim-v1",
                "subject": event["company_name"],
                "subject_basis": "EXACT_IN_PASSAGE",
                "predicate": event["event_type"],
                "fact_predicate": fact_predicate,
                "action_quote": action_quote,
                "object_quote": object_quote,
                "stage": stage,
                "modality": "REALIZED",
                "fact_sentence_quote": passage_text,
                "fact_sentence_start": 0,
                "fact_sentence_end": len(passage_text),
                "evidence_passage_sha256": hashlib.sha256(
                    passage_text.encode("utf-8")
                ).hexdigest(),
                "event_date_or_effective_date": "January 1, 2026",
            }
        row = {
            "event_id": event["event_id"],
            "event_version": event["current_version"],
            "evidence_fingerprint": event["evidence_fingerprint"],
            "checks": {
                "source_accessible": "YES",
                "subject_match": "YES",
                "event_claim_supported": "YES",
                "date_coherent": "YES",
                "primary_evidence": "YES",
                "conflict_found": "NO",
            },
            "modality": "REALIZED",
            "decision": decision,
            "reason_code": {
                "CONFIRM_EVENT": "PRIMARY_EVIDENCE_DIRECTLY_SUPPORTS",
                "NEEDS_EVIDENCE": "NO_EXACT_PASSAGE",
                "REJECT_CANDIDATE": "WRONG_EVENT",
            }[decision],
            "selected_evidence_id": event["evidence"][0]["evidence_id"],
            "severity": "",
            "rationale": f"{reviewer_id} independently checked the exact primary passage and event identity.",
            "human_fact_claim": human_fact_claim,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": 90,
        }
        results.append(row)
    return {
        "schema_version": 1,
        "contract_version": assignment["contract_version"],
        "batch_id": assignment["batch_id"],
        "reviewer_slot": assignment["reviewer_slot"],
        "assignment_sha256": assignment["assignment_sha256"],
        "reviewer_id": reviewer_id,
        "attestation": True,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "complete": True,
        "results": results,
        "no_model_output": True,
        "no_market_outcome": True,
        "no_trading": True,
    }


def human_claim_input(
    *,
    event: dict,
    evidence: dict,
    fact_predicate: str,
    action_quote: str,
    object_quote: str,
    stage: str,
    fact_sentence_quote: str | None = None,
    subject: str | None = None,
    subject_basis: str = "EXACT_IN_PASSAGE",
    modality: str = "REALIZED",
    event_date_or_effective_date: str = "",
) -> dict:
    passage_text = evidence["evidence_passage"]
    sentence = fact_sentence_quote if fact_sentence_quote is not None else passage_text
    start = passage_text.find(sentence)
    return {
        "contract_version": "human-fact-claim-v1",
        "subject": subject or event["company_name"],
        "subject_basis": subject_basis,
        "predicate": event["event_type"],
        "fact_predicate": fact_predicate,
        "action_quote": action_quote,
        "object_quote": object_quote,
        "stage": stage,
        "modality": modality,
        "fact_sentence_quote": sentence,
        "fact_sentence_start": start,
        "fact_sentence_end": start + len(sentence),
        "evidence_passage_sha256": hashlib.sha256(
            passage_text.encode("utf-8")
        ).hexdigest(),
        "event_date_or_effective_date": event_date_or_effective_date,
    }


def assignments(ledger_path: Path) -> tuple[dict, dict]:
    events = select_reviewable_events(ledger_path, limit=2)
    expiry = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    return (
        build_assignment(events, batch_id="EFR-TEST", reviewer_slot="A", expires_at=expiry),
        build_assignment(events, batch_id="EFR-TEST", reviewer_slot="B", expires_at=expiry),
    )


def legacy_assignment(assignment: dict) -> dict:
    legacy = json.loads(json.dumps(assignment))
    legacy["contract_version"] = "event-fact-review-v1"
    legacy.pop("assignment_sha256", None)
    legacy["assignment_sha256"] = sha256_json(legacy)
    return legacy


def make_reader_ready(ledger_path: Path) -> None:
    connection = event_ledger.open_ledger(ledger_path)
    try:
        now = datetime.now(timezone.utc).isoformat()
        events = connection.execute(
            "SELECT event_id,current_version,company_name FROM canonical_events"
        ).fetchall()
        for event in events:
            evidence = connection.execute(
                """SELECT evidence_id FROM event_evidence
                   WHERE event_id=? ORDER BY evidence_id LIMIT 1""",
                (event["event_id"],),
            ).fetchone()
            facts = {
                "public_fact_summary": (
                    f"{event['company_name']} filed a specific primary-source event "
                    "that is ready for independent human verification."
                ),
                "claim_subject": event["company_name"],
                "claim_action": "filed a primary-source event",
                "claim_stage": "FILED",
                "known_at": now,
            }
            connection.execute(
                """UPDATE event_versions SET facts_json=?
                   WHERE event_id=? AND version=?""",
                (
                    json.dumps(facts, ensure_ascii=False),
                    event["event_id"],
                    event["current_version"],
                ),
            )
            connection.execute(
                """INSERT INTO event_evidence_relations(
                       event_id,evidence_id,event_version,relation_status,subject_match,
                       event_claim_supported,date_coherent,modality,evidence_fingerprint,
                       contract_version,assessed_by,created_at
                   ) VALUES (?,?,?,'SCOPED_MATCH',1,1,1,'REALIZED',
                             'fixture-fingerprint','fixture-v1','fixture',?)""",
                (
                    event["event_id"],
                    evidence["evidence_id"],
                    event["current_version"],
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO event_fact_workflow(
                       event_id,event_version,workflow_state,reason_codes_json,
                       evidence_fingerprint,contract_version,updated_at
                   ) VALUES (?,?,'NEEDS_HUMAN','[]','fixture-fingerprint',
                             'fixture-v1',?)""",
                (event["event_id"], event["current_version"], now),
            )
        connection.commit()
    finally:
        connection.close()


def test_selection_is_evidence_ready_and_balanced(ledger_path: Path) -> None:
    events = select_reviewable_events(ledger_path, limit=2)
    assert {event["event_family"] for event in events} == {
        "bankruptcy_or_distress",
        "delisting_or_suspension",
    }
    assert all(event["evidence_fingerprint"] for event in events)
    assert all(event["evidence"][0]["authority_tier"] == "P0" for event in events)


def test_offline_reviewer_assets_emit_v2_controlled_fact_claims() -> None:
    schema = json.loads((ROOT / "review_kit" / "submission.schema.json").read_text("utf-8"))
    assert schema["properties"]["contract_version"]["const"] == "event-fact-review-v2"
    result_schema = schema["properties"]["results"]["items"]
    assert "human_fact_claim" in result_schema["required"]
    claim_variants = result_schema["properties"]["human_fact_claim"]["oneOf"]
    claim_schema = next(item for item in claim_variants if item.get("type") == "object")
    assert claim_schema["properties"]["modality"]["const"] == "REALIZED"
    assert claim_schema["properties"]["subject_basis"]["const"] == "EXACT_IN_PASSAGE"
    assert {
        "fact_predicate",
        "fact_sentence_start",
        "fact_sentence_end",
        "evidence_passage_sha256",
    } <= set(claim_schema["required"])
    assert "summary" not in claim_schema["properties"]

    app_source = (ROOT / "review_kit" / "reviewer_app.html").read_text("utf-8")
    assert "contract_version:assignment.contract_version" in app_source
    assert "human-fact-claim-v1" in app_source
    assert "fact_predicate" in app_source
    assert "fact_sentence_quote" in app_source
    assert "fact_sentence_start" in app_source
    assert "fact_sentence_end" in app_source
    assert "evidence_passage_sha256" in app_source
    assert "const subjectBases=['EXACT_IN_PASSAGE']" in app_source
    assert "left>=0&&!/[.?!\\n]/.test" in app_source
    assert "left>=0&&!/[.?!:;\\n]/.test" not in app_source
    assert "event-fact-review-v1'" not in app_source


@pytest.mark.parametrize("tier", ("P0_official", "P1_issuer_official"))
def test_qualified_primary_tiers_are_selectable_and_applicable(
    ledger_path: Path,
    tier: str,
) -> None:
    with event_ledger.open_ledger(ledger_path) as connection:
        connection.execute(
            "UPDATE sources SET authority_tier=? WHERE source_id='sec_edgar'",
            (tier,),
        )
        connection.commit()

    assignment_a, assignment_b = assignments(ledger_path)
    assert all(
        event["evidence"][0]["authority_tier"] == tier
        for event in assignment_a["events"]
    )
    merged = merge_submissions(
        assignment_a,
        submission(assignment_a, "reviewer-a"),
        assignment_b,
        submission(assignment_b, "reviewer-b"),
    )
    authorization = build_authorization_template(merged)
    authorization.update(
        {
            "approved": True,
            "authorization_id": f"AUTH-EFR-{tier}",
            "actor": "owner",
            "purpose": "Apply consensus using a canonical qualified primary tier.",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
    )
    assert apply_consensus(ledger_path, merged, authorization)["applied"] == 2


@pytest.mark.parametrize("tier", ("P2", "P00", "P01_official", "P10"))
def test_discovery_and_lookalike_primary_tiers_are_not_reviewable(
    ledger_path: Path,
    tier: str,
) -> None:
    with event_ledger.open_ledger(ledger_path) as connection:
        connection.execute(
            "UPDATE sources SET authority_tier=? WHERE source_id='sec_edgar'",
            (tier,),
        )
        connection.commit()

    with pytest.raises(ValueError, match="only 0 evidence-ready events"):
        select_reviewable_events(ledger_path, limit=1)


def test_dual_human_evidence_status_requires_human_confirmed_current_receipt(
    ledger_path: Path,
) -> None:
    make_reader_ready(ledger_path)
    with event_ledger.open_ledger(ledger_path) as connection:
        connection.execute(
            """UPDATE event_evidence
               SET evidence_status='accepted_dual_human_primary_evidence'"""
        )
        connection.commit()

    repository = LedgerRepository(ledger_path)
    assert len(repository.list_events(reader_ready=True, limit=20)["items"]) == 0


@pytest.mark.parametrize("source_id", ("sec_edgar", "non_sec_issuer_news"))
def test_legacy_generic_standard_receipts_are_hidden_for_every_source(
    ledger_path: Path,
    source_id: str,
) -> None:
    make_reader_ready(ledger_path)
    with event_ledger.open_ledger(ledger_path) as connection:
        if source_id != "sec_edgar":
            event_ledger.upsert_source(
                connection,
                source_id=source_id,
                name="Non-SEC issuer source",
                source_type="official_primary",
                authority_tier="P1",
            )
            connection.execute(
                "UPDATE raw_observations SET source_id=? WHERE source_id='sec_edgar'",
                (source_id,),
            )
            connection.execute(
                "UPDATE source_revisions SET source_id=? WHERE source_id='sec_edgar'",
                (source_id,),
            )
        for row in connection.execute("SELECT event_id,version,facts_json FROM event_versions"):
            facts = json.loads(row["facts_json"])
            facts["public_fact_summary"] = (
                "An official source mentioned a category related to this event."
            )
            connection.execute(
                "UPDATE event_versions SET facts_json=? WHERE event_id=? AND version=?",
                (json.dumps(facts), row["event_id"], row["version"]),
            )
        connection.commit()
    repository = LedgerRepository(ledger_path)
    assert repository.list_events(reader_ready=True, limit=20)["total"] == 0
    event_id = repository.list_events(limit=1)["items"][0]["event_id"]
    assert all(item["reader_eligible"] == 0 for item in repository.event_evidence(event_id))

    with event_ledger.open_ledger(ledger_path) as connection:
        connection.execute(
            """UPDATE event_evidence_relations
               SET relation_status='HUMAN_CONFIRMED'
               WHERE event_version=1"""
        )
        connection.commit()
    # A status/relationship rewrite alone cannot manufacture a dual-human
    # verification.  The apply path must also freeze the selected-evidence
    # receipt into the current event version.
    assert len(repository.list_events(reader_ready=True, limit=20)["items"]) == 0


def test_event_evidence_uses_latest_revision_without_materializing_full_view(
    ledger_path: Path,
) -> None:
    connection = event_ledger.open_ledger(ledger_path)
    try:
        source = connection.execute(
            """SELECT ee.event_id,ro.source_id,ro.external_id,ro.source_published_at,
                      ro.local_received_at,ro.title,ro.summary,ro.canonical_url,ro.raw_json
               FROM event_evidence ee
               JOIN raw_observations ro ON ro.observation_id=ee.observation_id
               ORDER BY ee.event_id,ee.evidence_id LIMIT 1"""
        ).fetchone()
        event_ledger.record_source_observation(
            connection,
            source_id=source["source_id"],
            external_id=source["external_id"],
            source_published_at=source["source_published_at"],
            local_received_at=source["local_received_at"],
            title=source["title"],
            summary=source["summary"],
            canonical_url=source["canonical_url"],
            content_sha256="latest-revision-content-sha256",
            raw_json=source["raw_json"],
            revision_kind="edit",
        )
        connection.commit()

        expected_rows = connection.execute(
            """SELECT ee.evidence_id,ee.evidence_url,ee.filing_date,ee.form,ee.items,
                      ee.evidence_passage,ee.passage_score,ee.evidence_status,
                      ro.content_sha256,ro.source_id,s.name AS source_name,
                      s.authority_tier,s.source_type
               FROM event_evidence ee
               LEFT JOIN latest_source_content ro ON ro.observation_id=ee.observation_id
               LEFT JOIN sources s ON s.source_id=ro.source_id
               WHERE ee.event_id=?
               ORDER BY CASE WHEN s.authority_tier='P0' THEN 0
                             WHEN s.authority_tier='P1' THEN 1 ELSE 2 END,
                        ee.passage_score DESC,ee.updated_at DESC,ee.evidence_id""",
            (source["event_id"],),
        ).fetchall()
        actual_rows = _event_evidence(connection, source["event_id"])
        assert len(actual_rows) == len(expected_rows)
        for actual, expected_row in zip(actual_rows, expected_rows, strict=True):
            expected = dict(expected_row)
            assert actual["evidence_id"] == expected["evidence_id"]
            assert actual["content_sha256"] == expected["content_sha256"]
            assert actual["source_id"] == expected["source_id"]
            assert actual["source_name"] == expected["source_name"]
            assert actual["authority_tier"] == expected["authority_tier"]
            assert actual["source_type"] == expected["source_type"]
        assert actual_rows[0]["content_sha256"] == "latest-revision-content-sha256"

        plan = connection.execute(
            "EXPLAIN QUERY PLAN " + _EVENT_EVIDENCE_SQL,
            (source["event_id"],),
        ).fetchall()
        details = [str(row[3]) for row in plan]
        assert not any("MATERIALIZE" in detail for detail in details)
        assert not any(detail.startswith("SCAN ") for detail in details)
        assert any("idx_evidence_event" in detail for detail in details)
        assert any("idx_source_revisions_observation" in detail for detail in details)
    finally:
        connection.close()


def test_submission_validation_rejects_copied_reasons_and_bad_confirmation(ledger_path: Path) -> None:
    assignment_a, _ = assignments(ledger_path)
    valid = submission(assignment_a, "reviewer-a")
    assert validate_submission(assignment_a, valid)["valid"] is True

    invalid = json.loads(json.dumps(valid))
    invalid["results"][0]["checks"]["subject_match"] = "UNCLEAR"
    report = validate_submission(assignment_a, invalid)
    assert report["valid"] is False
    assert any("five affirmative checks" in issue for issue in report["issues"])

    invalid_severity = json.loads(json.dumps(valid))
    invalid_severity["results"][0]["severity"] = "S"
    report = validate_submission(assignment_a, invalid_severity)
    assert report["valid"] is False
    assert any("severity must remain blank" in issue for issue in report["issues"])

    invalid_extra = json.loads(json.dumps(valid))
    invalid_extra["results"][0]["market_return"] = -0.45
    report = validate_submission(assignment_a, invalid_extra)
    assert report["valid"] is False
    assert any("unsupported fields" in issue for issue in report["issues"])


def test_v1_nonconfirm_is_preserved_but_v1_confirm_requires_v2_addendum(
    ledger_path: Path,
) -> None:
    assignment_a, _ = assignments(ledger_path)
    legacy = legacy_assignment(assignment_a)
    needs = submission(legacy, "reviewer-a", decision="NEEDS_EVIDENCE")
    assert validate_submission(legacy, needs)["valid"] is True

    confirmation = submission(legacy, "reviewer-a", decision="CONFIRM_EVENT")
    report = validate_submission(legacy, confirmation)
    assert report["valid"] is False
    assert report["legacy_v1_confirm_requires_fact_claim_addendum"] == 2
    assert report["required_action"] == (
        "REISSUE_EVENT_IN_EVENT_FACT_REVIEW_V2_FOR_INDEPENDENT_FACT_CLAIM_ADDENDUM"
    )
    assert any("V1_CONFIRM_REQUIRES_FACT_CLAIM_ADDENDUM" in issue for issue in report["issues"])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("modality", "PROPOSED_OR_CONDITIONAL", "modality must be REALIZED"),
        ("stage", "PROPOSED", "stage cannot be PROPOSED"),
        ("action_quote", "not present in passage", "exact contiguous substring"),
    ),
)
def test_v2_confirmation_rejects_conditional_or_unquoted_claims(
    ledger_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    assignment_a, _ = assignments(ledger_path)
    review = submission(assignment_a, "reviewer-a")
    review["results"][0]["human_fact_claim"][field] = value
    report = validate_submission(assignment_a, review)
    assert report["valid"] is False
    assert any(message in issue for issue in report["issues"])


def test_short_ticker_cannot_bind_inside_an_unrelated_word() -> None:
    event = {
        "company_name": "Citigroup Inc.",
        "ticker_at_event": "C",
        "event_type": "management_departure",
    }
    evidence = {
        "evidence_passage": "Microsoft's CFO resigned effective immediately.",
        "authority_tier": "P0",
    }
    claim = human_claim_input(
        event=event,
        evidence=evidence,
        fact_predicate="OFFICER_DEPARTED",
        action_quote="resigned",
        object_quote="CFO",
        stage="DISCLOSED",
        subject="C",
    )
    with pytest.raises(ValueError, match="minimal clause"):
        canonicalize_human_fact_claim(claim, event=event, evidence=evidence)

    evidence["evidence_passage"] = "$C terminated its chief financial officer."
    claim = human_claim_input(
        event=event,
        evidence=evidence,
        fact_predicate="OFFICER_DEPARTED",
        action_quote="terminated",
        object_quote="its chief financial officer",
        stage="DISCLOSED",
        subject="C",
    )
    normalized = canonicalize_human_fact_claim(claim, event=event, evidence=evidence)
    assert normalized["public_fact_summary"].startswith("C：$C terminated")


def test_document_issuer_is_fail_closed_even_with_matching_cik() -> None:
    event = {
        "company_name": "Example Corp",
        "ticker_at_event": "EXM",
        "event_type": "merger_completed",
        "canonical_issuer_identity_type": "CIK",
        "canonical_issuer_identity_value": "123",
    }
    evidence = {
        "evidence_passage": "The Company completed a merger on August 20, 2026.",
        "evidence_url": "https://www.sec.gov/Archives/edgar/data/123/filing.htm",
        "authority_tier": "P0",
        "document_issuer_identity_type": "CIK",
        "document_issuer_identity_value": "123",
    }
    claim = human_claim_input(
        event=event,
        evidence=evidence,
        fact_predicate="MERGER_OR_ACQUISITION_COMPLETED",
        action_quote="completed",
        object_quote="a merger",
        stage="COMPLETED",
        subject_basis="DOCUMENT_ISSUER",
        event_date_or_effective_date="August 20, 2026",
    )
    with pytest.raises(ValueError, match="DOCUMENT_ISSUER is not publishable"):
        canonicalize_human_fact_claim(claim, event=event, evidence=evidence)


@pytest.mark.parametrize(
    "fact_sentence",
    (
        "Target Corp completed a merger with Example Corp.",
        "Target Corp told Example Corp that Target Corp completed a merger.",
        "Example Corp was advised that Target Corp completed a merger.",
    ),
)
def test_subject_must_control_action_in_minimal_fact_clause(fact_sentence: str) -> None:
    event = {
        "company_name": "Example Corp",
        "ticker_at_event": "EXM",
        "event_type": "merger_completed",
    }
    passage_text = "Example Corp is mentioned as background. " + fact_sentence
    evidence = {"evidence_passage": passage_text}
    claim = human_claim_input(
        event=event,
        evidence=evidence,
        fact_predicate="MERGER_OR_ACQUISITION_COMPLETED",
        action_quote="completed",
        object_quote="a merger",
        stage="COMPLETED",
        fact_sentence_quote=fact_sentence,
    )
    with pytest.raises(ValueError, match="minimal clause"):
        canonicalize_human_fact_claim(
            claim,
            event=event,
            evidence=evidence,
        )


@pytest.mark.parametrize(
    ("fact_sentence", "action_quote"),
    (
        ("Example Corp will issue common stock.", "issue"),
        ("Example Corp shall issue common stock.", "issue"),
        ("Example Corp was expected to issue common stock.", "expected to issue"),
        ("Example Corp denied issuing common stock.", "denied issuing"),
        ("Example Corp didn't issue common stock.", "didn't issue"),
        ("Example Corp failed to issue common stock.", "failed to issue"),
        ("Example Corp declined to issue common stock.", "declined to issue"),
        ("Example Corp cancelled the common stock offering.", "cancelled"),
        ("Example Corp canceled the common stock offering.", "canceled"),
        ("Example Corp abandoned the common stock offering.", "abandoned"),
        ("Example Corp rescinded the common stock offering.", "rescinded"),
        ("Example Corp is considering issuing common stock.", "considering issuing"),
        ("Example Corp is exploring issuing common stock.", "exploring issuing"),
        ("Example Corp seeks to issue common stock.", "seeks to issue"),
        ("Example Corp is scheduled to issue common stock.", "scheduled to issue"),
        ("Example Corp is set to issue common stock.", "set to issue"),
        ("Example Corp attempted to issue common stock.", "attempted to issue"),
        ("Example Corp issued common stock in a rumor.", "issued"),
        ("Example Corp issued common stock as an example.", "issued"),
        ("Example Corp issued common stock in a headline.", "issued"),
    ),
)
def test_realized_claim_rejects_future_negative_and_epistemic_language(
    fact_sentence: str,
    action_quote: str,
) -> None:
    event = {
        "company_name": "Example Corp",
        "ticker_at_event": "EXM",
        "event_type": "financing",
    }
    evidence = {"evidence_passage": fact_sentence}
    claim = human_claim_input(
        event=event,
        evidence=evidence,
        fact_predicate="SECURITIES_ISSUED_OR_SOLD",
        action_quote=action_quote,
        object_quote="common stock",
        stage="DISCLOSED",
        fact_sentence_quote=fact_sentence,
    )
    with pytest.raises(
        ValueError,
        match="minimal clause|future, conditional, negative, or epistemic",
    ):
        canonicalize_human_fact_claim(
            claim,
            event=event,
            evidence=evidence,
        )


def test_unknown_narrow_event_type_cannot_inherit_a_family_fact() -> None:
    fact_sentence = "Example Corp filed a voluntary petition under Chapter 11."
    event = {
        "company_name": "Example Corp",
        "ticker_at_event": "EXM",
        "event_type": "old_common_cancelled_without_consideration",
        "event_family": "bankruptcy_or_distress",
    }
    evidence = {"evidence_passage": fact_sentence}
    claim = human_claim_input(
        event=event,
        evidence=evidence,
        fact_predicate="BANKRUPTCY_PETITION_FILED",
        action_quote="filed",
        object_quote="a voluntary petition under Chapter 11",
        stage="FILED",
        fact_sentence_quote=fact_sentence,
    )
    with pytest.raises(ValueError, match="not compatible with event_type"):
        canonicalize_human_fact_claim(claim, event=event, evidence=evidence)


def test_merge_requires_independent_reviewers_and_separates_conflict(ledger_path: Path) -> None:
    assignment_a, assignment_b = assignments(ledger_path)
    review_a = submission(assignment_a, "reviewer-a")
    review_b = submission(assignment_b, "reviewer-b")
    review_b["results"][0]["decision"] = "NEEDS_EVIDENCE"
    review_b["results"][0]["reason_code"] = "NO_EXACT_PASSAGE"
    review_b["results"][0]["human_fact_claim"] = None
    merged = merge_submissions(assignment_a, review_a, assignment_b, review_b)
    assert merged["consensus_count"] == 1
    assert merged["conflict_count"] == 1
    assert merged["formal_application"] is False

    duplicate_identity = submission(assignment_b, "reviewer-a")
    with pytest.raises(ValueError, match="independent reviewer"):
        merge_submissions(assignment_a, review_a, assignment_b, duplicate_identity)


def test_merge_requires_the_same_canonical_human_fact_claim(ledger_path: Path) -> None:
    assignment_a, assignment_b = assignments(ledger_path)
    review_a = submission(assignment_a, "reviewer-a")
    review_b = submission(assignment_b, "reviewer-b")
    review_b["results"][0]["human_fact_claim"]["action_quote"] = "filed"
    merged = merge_submissions(assignment_a, review_a, assignment_b, review_b)
    assert merged["conflict_count"] == 1
    conflict = next(
        row for row in merged["conflicts"] if row["reason"] == "HUMAN_FACT_CLAIM_DISAGREEMENT"
    )
    assert conflict["reviewer_a_claim_sha256"] != conflict["reviewer_b_claim_sha256"]


def test_apply_is_authorized_atomic_and_stale_safe(ledger_path: Path) -> None:
    make_reader_ready(ledger_path)
    assignment_a, assignment_b = assignments(ledger_path)
    merged = merge_submissions(
        assignment_a,
        submission(assignment_a, "reviewer-a"),
        assignment_b,
        submission(assignment_b, "reviewer-b"),
    )
    authorization = build_authorization_template(merged)
    authorization.update(
        {
            "approved": True,
            "authorization_id": "AUTH-EFR-TEST",
            "actor": "owner",
            "purpose": "Apply independently reviewed fixture facts.",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
    )
    repository = LedgerRepository(ledger_path)
    assert len(repository.list_events(reader_ready=True, limit=20)["items"]) == 0

    result = apply_consensus(ledger_path, merged, authorization)
    assert result["applied"] == 2
    assert result["already_applied"] == 0
    connection = event_ledger.open_ledger(ledger_path)
    try:
        rows = connection.execute(
            "SELECT status,current_version,no_trading FROM canonical_events ORDER BY event_id"
        ).fetchall()
        assert {(row["status"], row["current_version"], row["no_trading"]) for row in rows} == {
            ("verified", 2, 1)
        }
        assert connection.execute(
            "SELECT COUNT(*) FROM event_versions WHERE change_reason='dual_human_fact_review_v2'"
        ).fetchone()[0] == 2
        relations = connection.execute(
            """SELECT rel.event_id,rel.event_version,rel.relation_status,
                      rel.subject_match,rel.event_claim_supported,rel.date_coherent,
                      rel.modality,ev.evidence_status
               FROM event_evidence_relations rel
               JOIN event_evidence ev ON ev.evidence_id=rel.evidence_id
               WHERE rel.event_version=2 ORDER BY rel.event_id"""
        ).fetchall()
        assert len(relations) == 2
        assert all(
            (
                row["relation_status"],
                row["subject_match"],
                row["event_claim_supported"],
                row["date_coherent"],
                row["modality"],
                row["evidence_status"],
            )
            == (
                "HUMAN_CONFIRMED",
                1,
                1,
                1,
                "REALIZED",
                "accepted_dual_human_primary_evidence",
            )
            for row in relations
        )
        workflows = connection.execute(
            """SELECT event_id,event_version,workflow_state,contract_version
               FROM event_fact_workflow WHERE event_version=2 ORDER BY event_id"""
        ).fetchall()
        assert len(workflows) == 2
        assert all(row["workflow_state"] == "EVIDENCE_READY" for row in workflows)
        assert all(row["contract_version"] == "event-fact-review-v2" for row in workflows)
        fact_rows = connection.execute(
            "SELECT facts_json FROM event_versions WHERE version=2 ORDER BY event_id"
        ).fetchall()
        for fact_row in fact_rows:
            stored = json.loads(fact_row["facts_json"])
            assert stored["human_fact_claim"]["contract_version"] == "human-fact-claim-v1"
            assert stored["public_fact_summary"].startswith(stored["claim_subject"] + "：")
            expected_phrase = {
                "BANKRUPTCY_PETITION_FILED": "filed a voluntary petition",
                "DELISTED_OR_SUSPENDED": "delisted its common stock",
            }[stored["human_fact_claim"]["fact_predicate"]]
            assert expected_phrase in stored["public_fact_summary"]
            assert stored["dual_human_fact_review"]["canonical_claim_sha256"] == (
                stored["human_fact_claim"]["canonical_claim_sha256"]
            )
    finally:
        connection.close()

    assert len(repository.list_events(reader_ready=True, limit=20)["items"]) == 2
    repeated = apply_consensus(ledger_path, merged, authorization)
    assert repeated["applied"] == 0
    assert repeated["already_applied"] == 2
    with event_ledger.open_ledger(ledger_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM event_versions WHERE change_reason='dual_human_fact_review_v2'"
        ).fetchone()[0] == 2
        connection.execute(
            """UPDATE event_fact_workflow SET contract_version='tampered'
               WHERE event_id=(SELECT event_id FROM canonical_events ORDER BY event_id LIMIT 1)
                 AND event_version=2"""
        )
        connection.commit()
    with pytest.raises(ValueError, match="STALE_REVIEW"):
        apply_consensus(ledger_path, merged, authorization)


def test_sec_current_dual_human_consensus_is_reader_ready_without_machine_slots(
    ledger_path: Path,
) -> None:
    """Human closure must not inherit the SEC machine-admission slot gate."""

    make_reader_ready(ledger_path)
    with event_ledger.open_ledger(ledger_path) as connection:
        event_ledger.upsert_source(
            connection,
            source_id="sec_current_filings",
            name="SEC current filings",
            source_type="official_primary",
            authority_tier="P0",
        )
        connection.execute(
            "UPDATE raw_observations SET source_id='sec_current_filings' "
            "WHERE source_id='sec_edgar'"
        )
        connection.execute(
            "UPDATE source_revisions SET source_id='sec_current_filings' "
            "WHERE source_id='sec_edgar'"
        )
        connection.commit()

    assignment_a, assignment_b = assignments(ledger_path)
    merged = merge_submissions(
        assignment_a,
        submission(assignment_a, "reviewer-a"),
        assignment_b,
        submission(assignment_b, "reviewer-b"),
    )
    authorization = build_authorization_template(merged)
    authorization.update(
        {
            "approved": True,
            "authorization_id": "AUTH-EFR-SEC-CURRENT",
            "actor": "owner",
            "purpose": "Close historical SEC facts through independent human review.",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
    )
    repository = LedgerRepository(ledger_path)
    assert repository.list_events(reader_ready=True, limit=20)["total"] == 0

    assert apply_consensus(ledger_path, merged, authorization)["applied"] == 2
    ready = repository.list_events(reader_ready=True, limit=20)
    assert {row["event_id"] for row in ready["items"]} == {
        row["event_id"] for row in merged["consensus"]
    }
    for consensus in merged["consensus"]:
        selected = next(
            row
            for row in repository.event_evidence(consensus["event_id"])
            if row["evidence_id"] == consensus["selected_evidence_id"]
        )
        assert selected["source_id"] == "sec_current_filings"
        assert selected["relation_status"] == "HUMAN_CONFIRMED"
        assert selected["dual_human_receipt_consistent"] == 1
        assert selected["reader_eligible"] == 1
    with event_ledger.open_ledger(ledger_path) as connection:
        facts_rows = connection.execute(
            """SELECT facts_json FROM event_versions
               WHERE version=2 ORDER BY event_id"""
        ).fetchall()
    assert all("claim_fact_slots" not in json.loads(row["facts_json"]) for row in facts_rows)


def test_document_issuer_cik_assignment_still_requires_needs_evidence(
    ledger_path: Path,
) -> None:
    passage_text = (
        "The Company filed a voluntary petition under Chapter 11 "
        "in the United States Bankruptcy Court on January 1, 2026."
    )
    with event_ledger.open_ledger(ledger_path) as connection:
        for index, event in enumerate(
            connection.execute("SELECT event_id,current_version FROM canonical_events"),
            start=1,
        ):
            row = connection.execute(
                "SELECT facts_json FROM event_versions WHERE event_id=? AND version=?",
                (event["event_id"], event["current_version"]),
            ).fetchone()
            facts = json.loads(row["facts_json"])
            facts["cik"] = f"00000012{index}"
            connection.execute(
                "UPDATE event_versions SET facts_json=? WHERE event_id=? AND version=?",
                (json.dumps(facts), event["event_id"], event["current_version"]),
            )
            connection.execute(
                """UPDATE event_evidence
                   SET evidence_url=?,evidence_passage=? WHERE event_id=?""",
                (
                    f"https://www.sec.gov/Archives/edgar/data/12{index}/filing.htm",
                    passage_text,
                    event["event_id"],
                ),
            )
        connection.commit()
    assignment_a, assignment_b = assignments(ledger_path)
    review_a = submission(assignment_a, "reviewer-a")
    review_b = submission(assignment_b, "reviewer-b")
    for assignment, review in ((assignment_a, review_a), (assignment_b, review_b)):
        by_id = {row["event_id"]: row for row in assignment["events"]}
        for result in review["results"]:
            event = by_id[result["event_id"]]
            claim = result["human_fact_claim"]
            claim["subject_basis"] = "DOCUMENT_ISSUER"
            claim["fact_sentence_quote"] = passage_text
            assert event["canonical_issuer_identity_type"] == "CIK"
            assert event["canonical_issuer_identity_value"] == next(
                evidence["document_issuer_identity_value"]
                for evidence in event["evidence"]
                if evidence["evidence_id"] == result["selected_evidence_id"]
            )
    for assignment, review in ((assignment_a, review_a), (assignment_b, review_b)):
        report = validate_submission(assignment, review)
        assert report["valid"] is False
        assert any("DOCUMENT_ISSUER is not publishable" in issue for issue in report["issues"])
    with pytest.raises(ValueError, match="submission is invalid"):
        merge_submissions(assignment_a, review_a, assignment_b, review_b)
    assert LedgerRepository(ledger_path).list_events(reader_ready=True, limit=20)["total"] == 0


@pytest.mark.parametrize(
    "tamper",
    (
        "generic_summary",
        "claim_action",
        "fact_sentence_outside_passage",
        "selected_receipt_sha256",
        "canonical_claim_sha256",
        "public_summary_sha256",
    ),
)
def test_reader_hides_tampered_v2_human_fact_receipt(
    ledger_path: Path,
    tamper: str,
) -> None:
    assignment_a, assignment_b = assignments(ledger_path)
    merged = merge_submissions(
        assignment_a,
        submission(assignment_a, "reviewer-a"),
        assignment_b,
        submission(assignment_b, "reviewer-b"),
    )
    authorization = build_authorization_template(merged)
    authorization.update(
        {
            "approved": True,
            "authorization_id": f"AUTH-EFR-TAMPER-{tamper}",
            "actor": "owner",
            "purpose": "Prove public human fact receipts fail closed after tampering.",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
    )
    assert apply_consensus(ledger_path, merged, authorization)["applied"] == 2
    repository = LedgerRepository(ledger_path)
    assert repository.list_events(reader_ready=True, limit=20)["total"] == 2
    event_id = merged["consensus"][0]["event_id"]
    with event_ledger.open_ledger(ledger_path) as connection:
        row = connection.execute(
            "SELECT facts_json FROM event_versions WHERE event_id=? AND version=2",
            (event_id,),
        ).fetchone()
        facts = json.loads(row["facts_json"])
        if tamper == "generic_summary":
            facts["public_fact_summary"] = "An official document mentioned this event category."
        elif tamper == "claim_action":
            facts["human_fact_claim"]["action_quote"] = "invented action"
        elif tamper == "fact_sentence_outside_passage":
            facts["human_fact_claim"]["fact_sentence_quote"] = (
                "This sentence was never in the selected passage."
            )
        elif tamper == "selected_receipt_sha256":
            facts["dual_human_fact_review"]["selected_evidence_receipt"][
                "receipt_sha256"
            ] = "0" * 64
        elif tamper == "canonical_claim_sha256":
            forged = "0" * 64
            facts["human_fact_claim"]["canonical_claim_sha256"] = forged
            facts["dual_human_fact_review"]["canonical_claim_sha256"] = forged
            receipt = facts["dual_human_fact_review"]["selected_evidence_receipt"]
            receipt["canonical_claim_sha256"] = forged
            receipt_payload = dict(receipt)
            receipt_payload.pop("receipt_sha256", None)
            receipt["receipt_sha256"] = sha256_json(receipt_payload)
        else:
            forged = "0" * 64
            facts["human_fact_claim"]["public_fact_summary_sha256"] = forged
            facts["dual_human_fact_review"]["public_fact_summary_sha256"] = forged
            receipt = facts["dual_human_fact_review"]["selected_evidence_receipt"]
            receipt["public_fact_summary_sha256"] = forged
            receipt_payload = dict(receipt)
            receipt_payload.pop("receipt_sha256", None)
            receipt["receipt_sha256"] = sha256_json(receipt_payload)
        connection.execute(
            "UPDATE event_versions SET facts_json=? WHERE event_id=? AND version=2",
            (json.dumps(facts, ensure_ascii=False), event_id),
        )
        connection.commit()
    assert event_id not in {
        row["event_id"]
        for row in repository.list_events(reader_ready=True, limit=20)["items"]
    }
    selected = next(
        row
        for row in repository.event_evidence(event_id)
        if row["evidence_id"] == merged["consensus"][0]["selected_evidence_id"]
    )
    assert selected["reader_eligible"] == 0


def test_reader_rechecks_subject_action_binding_with_matching_forged_digests(
    ledger_path: Path,
) -> None:
    """Hash equality cannot make a counterparty sentence reader-eligible."""

    invalid_sentence = "Target Corp completed a merger with AAA Corporation."
    with event_ledger.open_ledger(ledger_path) as connection:
        event_id = connection.execute(
            "SELECT event_id FROM canonical_events WHERE company_name='AAA Corporation'"
        ).fetchone()[0]
        original = connection.execute(
            "SELECT evidence_passage FROM event_evidence WHERE event_id=?",
            (event_id,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE event_evidence SET evidence_passage=? WHERE event_id=?",
            (original + " " + invalid_sentence, event_id),
        )
        connection.commit()

    assignment_a, assignment_b = assignments(ledger_path)
    review_a = submission(assignment_a, "reviewer-a")
    review_b = submission(assignment_b, "reviewer-b")
    for review in (review_a, review_b):
        result = next(row for row in review["results"] if row["event_id"] == event_id)
        result["human_fact_claim"]["fact_sentence_quote"] = original
        result["human_fact_claim"]["fact_sentence_start"] = 0
        result["human_fact_claim"]["fact_sentence_end"] = len(original)
    merged = merge_submissions(
        assignment_a,
        review_a,
        assignment_b,
        review_b,
    )
    authorization = build_authorization_template(merged)
    authorization.update(
        {
            "approved": True,
            "authorization_id": "AUTH-EFR-BINDER-READER",
            "actor": "owner",
            "purpose": "Prove the public SQL rechecks the subject-action clause.",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
    )
    assert apply_consensus(ledger_path, merged, authorization)["applied"] == 2
    repository = LedgerRepository(ledger_path)
    assert repository.list_events(reader_ready=True, limit=20)["total"] == 2

    with event_ledger.open_ledger(ledger_path) as connection:
        row = connection.execute(
            "SELECT facts_json FROM event_versions WHERE event_id=? AND version=2",
            (event_id,),
        ).fetchone()
        facts = json.loads(row["facts_json"])
        claim = facts["human_fact_claim"]
        claim.update(
            {
                "action_quote": "completed",
                "object_quote": "a merger",
                "stage": "COMPLETED",
                "fact_sentence_quote": invalid_sentence,
                "event_date_or_effective_date": "",
                # These internally consistent-looking derived fields are
                # deliberately false for the quoted Target Corp sentence.
                "subject_surface_quote": claim["subject"],
                "subject_action_gap_quote": " ",
                "subject_action_gap_normalized": "",
                "subject_action_prefix_quote": claim["subject"] + " completed",
            }
        )
        summary = f"{claim['subject']}：{invalid_sentence}"
        claim["public_fact_summary"] = summary
        claim["public_fact_summary_sha256"] = hashlib.sha256(
            summary.encode("utf-8")
        ).hexdigest()
        canonical_payload = dict(claim)
        canonical_payload.pop("canonical_claim_sha256", None)
        canonical_payload.pop("public_fact_summary", None)
        canonical_payload.pop("public_fact_summary_sha256", None)
        claim["canonical_claim_sha256"] = sha256_json(canonical_payload)
        facts["claim_action"] = "completed"
        facts["claim_stage"] = "COMPLETED"
        facts["public_fact_summary"] = summary
        dual = facts["dual_human_fact_review"]
        dual["canonical_claim_sha256"] = claim["canonical_claim_sha256"]
        dual["public_fact_summary_sha256"] = claim["public_fact_summary_sha256"]
        receipt = dual["selected_evidence_receipt"]
        receipt["canonical_claim_sha256"] = claim["canonical_claim_sha256"]
        receipt["public_fact_summary_sha256"] = claim["public_fact_summary_sha256"]
        receipt_payload = dict(receipt)
        receipt_payload.pop("receipt_sha256", None)
        receipt["receipt_sha256"] = sha256_json(receipt_payload)
        connection.execute(
            "UPDATE event_versions SET facts_json=? WHERE event_id=? AND version=2",
            (json.dumps(facts, ensure_ascii=False), event_id),
        )
        connection.commit()

    assert event_id not in {
        item["event_id"]
        for item in repository.list_events(reader_ready=True, limit=20)["items"]
    }
    selected = next(
        item
        for item in repository.event_evidence(event_id)
        if item["evidence_status"] == "accepted_dual_human_primary_evidence"
    )
    assert selected["reader_eligible"] == 0


def test_needs_evidence_keeps_followup_route_open(ledger_path: Path) -> None:
    assignment_a, assignment_b = assignments(ledger_path)
    merged = merge_submissions(
        assignment_a,
        submission(assignment_a, "reviewer-a", decision="NEEDS_EVIDENCE"),
        assignment_b,
        submission(assignment_b, "reviewer-b", decision="NEEDS_EVIDENCE"),
    )
    authorization = build_authorization_template(merged)
    authorization.update(
        {
            "approved": True,
            "authorization_id": "AUTH-EFR-WEAK",
            "actor": "owner",
            "purpose": "Record the evidence gap without hiding follow-up work.",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
    )
    apply_consensus(ledger_path, merged, authorization)
    connection = event_ledger.open_ledger(ledger_path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM canonical_events WHERE status='weak'"
        ).fetchone()[0] == 2
        statuses = {
            row[0]
            for row in connection.execute(
                "SELECT status FROM pipeline_jobs WHERE event_id IS NOT NULL"
            ).fetchall()
        }
        assert statuses == {"PENDING_PRIMARY_EVIDENCE"}
        workflows = connection.execute(
            """SELECT event_version,workflow_state,reason_codes_json
               FROM event_fact_workflow WHERE event_version=2"""
        ).fetchall()
        assert len(workflows) == 2
        assert all(row["workflow_state"] == "NEEDS_EVIDENCE" for row in workflows)
        assert all("NO_EXACT_PASSAGE" in row["reason_codes_json"] for row in workflows)
        assert connection.execute(
            "SELECT COUNT(*) FROM event_evidence_relations WHERE event_version=2"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_rejected_consensus_closes_current_workflow_without_supportive_relation(
    ledger_path: Path,
) -> None:
    assignment_a, assignment_b = assignments(ledger_path)
    merged = merge_submissions(
        assignment_a,
        submission(assignment_a, "reviewer-a", decision="REJECT_CANDIDATE"),
        assignment_b,
        submission(assignment_b, "reviewer-b", decision="REJECT_CANDIDATE"),
    )
    authorization = build_authorization_template(merged)
    authorization.update(
        {
            "approved": True,
            "authorization_id": "AUTH-EFR-REJECTED",
            "actor": "owner",
            "purpose": "Record two independent rejections without citable support.",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
    )
    apply_consensus(ledger_path, merged, authorization)
    with event_ledger.open_ledger(ledger_path) as connection:
        workflows = connection.execute(
            """SELECT workflow_state,reason_codes_json FROM event_fact_workflow
               WHERE event_version=2"""
        ).fetchall()
        assert len(workflows) == 2
        assert all(row["workflow_state"] == "EXCLUDED" for row in workflows)
        assert all("WRONG_EVENT" in row["reason_codes_json"] for row in workflows)
        assert connection.execute(
            "SELECT COUNT(*) FROM event_evidence_relations WHERE event_version=2"
        ).fetchone()[0] == 0


def test_apply_rolls_back_whole_batch_on_new_version_relation_conflict(
    ledger_path: Path,
) -> None:
    assignment_a, assignment_b = assignments(ledger_path)
    merged = merge_submissions(
        assignment_a,
        submission(assignment_a, "reviewer-a"),
        assignment_b,
        submission(assignment_b, "reviewer-b"),
    )
    authorization = build_authorization_template(merged)
    authorization.update(
        {
            "approved": True,
            "authorization_id": "AUTH-EFR-CONFLICT",
            "actor": "owner",
            "purpose": "Exercise relation conflict rollback semantics.",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
    )
    poisoned = merged["consensus"][1]
    with event_ledger.open_ledger(ledger_path) as connection:
        connection.execute(
            """INSERT INTO event_evidence_relations(
                   event_id,evidence_id,event_version,relation_status,subject_match,
                   event_claim_supported,date_coherent,modality,evidence_fingerprint,
                   contract_version,assessed_by,created_at
               ) VALUES (?,?,2,'INSUFFICIENT',1,0,1,'UNCLEAR','poisoned',
                         'fixture-v1','fixture',?)""",
            (
                poisoned["event_id"],
                poisoned["selected_evidence_id"],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        connection.commit()

    with pytest.raises(ValueError, match="EVIDENCE_RELATION_CONFLICT"):
        apply_consensus(ledger_path, merged, authorization)

    with event_ledger.open_ledger(ledger_path) as connection:
        versions = connection.execute(
            "SELECT current_version,status FROM canonical_events ORDER BY event_id"
        ).fetchall()
        assert [(row["current_version"], row["status"]) for row in versions] == [
            (1, "candidate"),
            (1, "candidate"),
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM event_versions WHERE change_reason='dual_human_fact_review_v2'"
        ).fetchone()[0] == 0


def test_verified_consensus_rejects_uncitable_selected_evidence_atomically(
    ledger_path: Path,
) -> None:
    """A reviewer attestation cannot bypass the public evidence contract."""

    make_reader_ready(ledger_path)
    with event_ledger.open_ledger(ledger_path) as connection:
        now = datetime.now(timezone.utc).isoformat()
        event_id = connection.execute(
            "SELECT event_id FROM canonical_events ORDER BY event_id LIMIT 1"
        ).fetchone()[0]
        source_id = connection.execute(
            "SELECT source_id FROM sources WHERE authority_tier='P0' LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO raw_observations(
                   observation_id,source_id,external_id,source_published_at,
                   local_received_at,title,summary,canonical_url,content_sha256,
                   raw_json,observation_status
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "uncitable-observation",
                source_id,
                "uncitable-external",
                now,
                now,
                "Uncitable official record",
                "This fixture deliberately lacks a citable selected passage.",
                "https://example.test/uncitable",
                "e" * 64,
                "{}",
                "captured",
            ),
        )
        connection.execute(
            """INSERT INTO event_evidence(
                   evidence_id,event_id,observation_id,evidence_url,filing_date,
                   form,items,evidence_passage,matched_keywords,passage_score,
                   evidence_status,auto_verification_allowed,created_at,updated_at
               ) VALUES (?,?,?,'',NULL,NULL,NULL,'tiny','',999,
                         'candidate_passage',0,?,?)""",
            ("uncitable-evidence", event_id, "uncitable-observation", now, now),
        )
        connection.commit()

    assignment_a, assignment_b = assignments(ledger_path)
    review_a = submission(assignment_a, "reviewer-a")
    review_b = submission(assignment_b, "reviewer-b")
    for review in (review_a, review_b):
        for result in review["results"]:
            if result["event_id"] == event_id:
                result["selected_evidence_id"] = "uncitable-evidence"
    with pytest.raises(ValueError, match="exact contiguous substring"):
        merge_submissions(assignment_a, review_a, assignment_b, review_b)
    with event_ledger.open_ledger(ledger_path) as connection:
        states = connection.execute(
            "SELECT current_version,status FROM canonical_events ORDER BY event_id"
        ).fetchall()
        assert [(row["current_version"], row["status"]) for row in states] == [
            (1, "candidate"),
            (1, "candidate"),
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM event_versions WHERE change_reason='dual_human_fact_review_v2'"
        ).fetchone()[0] == 0


def test_source_revision_invalidates_dual_receipt_and_exact_retry(
    ledger_path: Path,
) -> None:
    make_reader_ready(ledger_path)
    assignment_a, assignment_b = assignments(ledger_path)
    merged = merge_submissions(
        assignment_a,
        submission(assignment_a, "reviewer-a"),
        assignment_b,
        submission(assignment_b, "reviewer-b"),
    )
    authorization = build_authorization_template(merged)
    authorization.update(
        {
            "approved": True,
            "authorization_id": "AUTH-EFR-REVISION",
            "actor": "owner",
            "purpose": "Verify that a later official revision invalidates the frozen receipt.",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
    )
    assert apply_consensus(ledger_path, merged, authorization)["applied"] == 2

    revised_event_id = merged["consensus"][0]["event_id"]
    connection = event_ledger.open_ledger(ledger_path)
    try:
        source = connection.execute(
            """SELECT ro.* FROM event_evidence ee
               JOIN raw_observations ro ON ro.observation_id=ee.observation_id
               WHERE ee.event_id=? AND ee.evidence_id=?""",
            (revised_event_id, merged["consensus"][0]["selected_evidence_id"]),
        ).fetchone()
        amended = json.dumps(
            {"revision": "amended primary content", "event_id": revised_event_id},
            sort_keys=True,
        )
        event_ledger.record_source_observation(
            connection,
            source_id=source["source_id"],
            external_id=source["external_id"],
            source_published_at=source["source_published_at"],
            local_received_at=datetime.now(timezone.utc).isoformat(),
            title=source["title"] + " amended",
            summary=source["summary"] + " amended",
            canonical_url=source["canonical_url"],
            content_sha256=hashlib.sha256(amended.encode("utf-8")).hexdigest(),
            raw_json=amended,
            revision_kind="edit",
        )
        connection.commit()
    finally:
        connection.close()

    repository = LedgerRepository(ledger_path)
    reader_ready_ids = {
        row["event_id"]
        for row in repository.list_events(reader_ready=True, limit=20)["items"]
    }
    assert revised_event_id not in reader_ready_ids
    revised_evidence = repository.event_evidence(revised_event_id)
    selected = next(
        row
        for row in revised_evidence
        if row["evidence_id"] == merged["consensus"][0]["selected_evidence_id"]
    )
    assert selected["dual_human_receipt_consistent"] == 0
    assert selected["reader_eligible"] == 0
    with pytest.raises(ValueError, match="STALE_REVIEW"):
        apply_consensus(ledger_path, merged, authorization)


def test_public_api_exposes_only_deidentified_dual_human_method(
    ledger_path: Path,
    tmp_path: Path,
) -> None:
    make_reader_ready(ledger_path)
    assignment_a, assignment_b = assignments(ledger_path)
    merged = merge_submissions(
        assignment_a,
        submission(assignment_a, "reviewer-a"),
        assignment_b,
        submission(assignment_b, "reviewer-b"),
    )
    authorization = build_authorization_template(merged)
    authorization.update(
        {
            "approved": True,
            "authorization_id": "AUTH-EFR-PUBLIC-METHOD",
            "actor": "owner-internal",
            "purpose": "Verify that the public API de-identifies dual review provenance.",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
    )
    assert apply_consensus(ledger_path, merged, authorization)["applied"] == 2
    event_id = merged["consensus"][0]["event_id"]
    settings = Settings(
        ledger_db=ledger_path,
        operations_db=tmp_path / "operations.sqlite3",
        artifact_dir=tmp_path / "artifacts",
        evidence_object_dir=tmp_path / "evidence_objects",
        replay_dir=ROOT / "replay" / "cases",
        demo_mode="RECENT_CAPTURE",
        api_base_url="http://testserver",
        web_base_url="http://testserver",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get(f"/api/v1/events/{event_id}")

    assert response.status_code == 200
    payload = response.json()["data"]
    method = payload["verification_method"]
    assert method["kind"] == "dual_human_fact_review"
    assert method["version"] == "event-fact-review-v2"
    assert method["independent_reviews"] == 2
    assert method["evidence_ids"] == [merged["consensus"][0]["selected_evidence_id"]]
    encoded = json.dumps(payload, sort_keys=True)
    assert "reviewer-a" not in encoded
    assert "reviewer-b" not in encoded
    assert "owner-internal" not in encoded
    assert authorization["authorization_id"] not in encoded
    assert merged["consensus_sha256"] not in encoded
