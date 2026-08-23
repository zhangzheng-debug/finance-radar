from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from dataclasses import replace
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


def test_active_light_followup_is_visible_as_pending_evidence_work() -> None:
    with tempfile.TemporaryDirectory() as directory:
        ledger_path = _populated_ledger(Path(directory))
        connection = open_ledger(ledger_path)
        followup_at = "2026-08-04T14:00:00+00:00"
        payload = json.dumps(
            {
                "light_verification_followup": {
                    "expected_next_action": "human_review",
                    "gap_reasons": ["primary passage needs human adjudication"],
                    "legacy_reconciliation": True,
                }
            },
            sort_keys=True,
        )
        connection.execute(
            """INSERT INTO pipeline_jobs VALUES (
               'light-followup-verified','verified-0','light_verification_followup',
               'PENDING_HUMAN_REVIEW',95,0,?,NULL,?,?,?)""",
            (followup_at, payload, followup_at, followup_at),
        )
        connection.commit()
        connection.close()

        repository = LedgerRepository(ledger_path)
        overview = repository.overview(run_integrity_check=False)
        pending = repository.list_events(public_state="pending_verification", limit=200)
        item = next(row for row in pending["items"] if row["event_id"] == "verified-0")
        detail = repository.event_detail("verified-0")

        assert overview["public_funnel"]["active_light_followups"] == 1
        assert overview["public_funnel"]["light_followup_statuses"] == {
            "PENDING_EVIDENCE_REVIEW": 0,
            "PENDING_HUMAN_REVIEW": 1,
        }
        assert item["public_state"] == "pending_verification"
        assert item["light_followup_status"] == "PENDING_HUMAN_REVIEW"
        assert item["light_followup_next_action"] == "human_review"
        assert detail is not None
        assert detail["event"]["public_state"] == "pending_verification"
        assert detail["event"]["light_followup"] == {
            "status": "PENDING_HUMAN_REVIEW",
            "updated_at": followup_at,
            "last_attempted_at": None,
            "expected_next_action": "human_review",
            "gap_reasons": ["primary passage needs human adjudication"],
            "legacy_reconciliation": True,
            "formal_verification": False,
            "no_trading": True,
        }


def test_reader_ready_gate_separates_citable_events_from_discovery_backlog() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ledger_path = _populated_ledger(root)
        connection = open_ledger(ledger_path)
        evidence_at = "2026-08-04T15:00:00+00:00"
        fact_summary = (
            "Zulu Pending：The exchange notice identifies the listing compliance action "
            "and states the remediation deadline."
        )
        legacy_fact_marker = "LEGACY_FACT_MARKER internal draft only"
        legacy_evidence_marker = "LEGACY_EVIDENCE_MARKER reviewer-private summary"
        passage = (
            "The exchange notice names Zulu Pending, identifies the listing compliance action, "
            "and states the remediation deadline."
        )
        facts = {
            "public_fact_summary": fact_summary,
            "fact_summary": legacy_fact_marker,
            "evidence_summary": legacy_evidence_marker,
            "claim_subject": "Zulu Pending",
            "claim_action": "delisted",
            "claim_stage": "DISCLOSED",
            "known_at": evidence_at,
            "admission_contract_version": "event-admission-v3",
            "fact_slot_contract_version": "deterministic-evidence-fact-slots-v2",
            "fact_slot_receipt_sha256": "d" * 64,
            "source_observation_id": "reader-ready-observation",
            "source_content_sha256": "a" * 64,
            "evidence_id": "reader-ready-evidence",
            "evidence_fingerprint": "b" * 64,
            "claim_fact_slots": {
                "contract_version": "deterministic-evidence-fact-slots-v2",
                "event_type": "delisted",
                "passage_sha256": "e" * 64,
                "canonical_passage_sha256": "f" * 64,
                "compatible_fact_count": 1,
                "facts": [
                    {
                        "subject": "Zulu Pending",
                        "subject_binding": "EXPLICIT_ISSUER",
                        "issuer_name_explicit_in_passage": True,
                        "predicate": "listing_compliance",
                        "action_text": "identifies the listing compliance action",
                        "object": "remediation deadline",
                        "evidence_sentence": passage,
                        "event_type_compatible": True,
                    }
                ],
            },
            "light_verification": {
                "version": "fixture-v1",
                "reviewed_at": evidence_at,
                "evidence_ids": [
                    "reader-ready-evidence",
                    "reader-unrelated-evidence",
                ],
                "score": 0.8,
                "rationale": "internal reviewer rationale must not cross the public API",
            },
        }
        connection.execute(
            """INSERT INTO event_versions VALUES (
               'pending-0',1,?,'candidate','candidate','regulatory','delisted',NULL,?,'seed')""",
            (evidence_at, json.dumps(facts, ensure_ascii=False)),
        )
        connection.execute(
            """INSERT INTO raw_observations(
               observation_id,source_id,external_id,source_published_at,local_received_at,
               title,summary,canonical_url,content_sha256,raw_json,observation_status
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "reader-ready-observation",
                "src",
                "reader-ready-external",
                evidence_at,
                evidence_at,
                "Zulu Pending listing compliance notice",
                fact_summary,
                "https://example.test/original-notice",
                "a" * 64,
                "{}",
                "captured",
            ),
        )
        connection.execute(
            "INSERT INTO event_observations VALUES ('pending-0','reader-ready-observation','primary',?)",
            (evidence_at,),
        )
        connection.execute(
            """INSERT INTO event_evidence VALUES (
               'reader-ready-evidence','pending-0','reader-ready-observation',?,NULL,'8-K','Item 3.01',
               ?,NULL,100,'candidate_passage',0,?,?)""",
            ("https://example.test/original-notice", passage, evidence_at, evidence_at),
        )
        connection.execute(
            """INSERT INTO event_evidence_relations VALUES (
               'pending-0','reader-ready-evidence',1,'SCOPED_MATCH',1,1,1,
               'DISCLOSED',?,'event-admission-v3','fixture',?)""",
            ("b" * 64, evidence_at),
        )
        connection.execute(
            "UPDATE canonical_events SET event_type='delisted' WHERE event_id='pending-0'"
        )
        connection.execute(
            """INSERT INTO raw_observations(
               observation_id,source_id,external_id,source_published_at,local_received_at,
               title,summary,canonical_url,content_sha256,raw_json,observation_status
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "reader-unrelated-observation",
                "src",
                "reader-unrelated-external",
                evidence_at,
                evidence_at,
                "High scoring but unrelated official material",
                "This official document does not support the event claim.",
                "https://example.test/unrelated",
                "c" * 64,
                "{}",
                "captured",
            ),
        )
        connection.execute(
            """INSERT INTO event_evidence VALUES (
               'reader-unrelated-evidence','pending-0','reader-unrelated-observation',?,
               NULL,NULL,NULL,?,NULL,999,'candidate_passage',0,?,?)""",
            (
                "https://example.test/unrelated",
                "This official passage is long enough and highly scored, but it does not support the event claim.",
                evidence_at,
                evidence_at,
            ),
        )
        connection.execute(
            """INSERT INTO pipeline_jobs VALUES (
               'discovery-only-job','rough-insufficient-00','reader_quality_backlog',
               'PENDING_EVIDENCE_REVIEW',50,0,?,NULL,'{}',?,?)""",
            (evidence_at, evidence_at, evidence_at),
        )
        connection.commit()
        connection.close()

        repository = LedgerRepository(ledger_path)
        overview = repository.overview(run_integrity_check=False)
        ready = repository.list_events(reader_ready=True, limit=200)
        discovery_only = repository.list_events(reader_ready=False, limit=200)
        facets = repository.event_facets(reader_ready=True)
        evidence_items = repository.event_evidence("pending-0")

        assert overview["review_queue"] == 2
        assert overview["reader_review_queue"] == 1
        assert overview["discovery_backlog"] == 19
        assert overview["reader_hidden_inventory"] == 19
        assert overview["review_queue_hidden_by_reader_gate"] == 1
        assert overview["inventory_contract"]["reader_hidden_inventory"] == {
            "authoritative": True,
            "definition": "all canonical events currently hidden by the public reader gate",
        }
        assert overview["inventory_contract"]["discovery_backlog"] == {
            "deprecated": True,
            "replacement": "reader_hidden_inventory",
            "definition": (
                "legacy numeric alias; it includes every reader-hidden canonical event, "
                "not only discovery-stage leads"
            ),
        }
        assert overview["recent_events"] == []
        assert overview["reader_quality"]["total"] == 20
        assert overview["reader_quality"]["reader_ready"] == 1
        assert overview["reader_quality"]["discovery_only"] == 19
        assert overview["reader_funnel"]["total"] == 1
        assert overview["reader_funnel"]["pending_verification"] == 1
        assert overview["reader_funnel"]["partition_complete"] is True
        assert ready["total"] == 1
        assert ready["items"][0]["event_id"] == "pending-0"
        assert ready["items"][0]["public_fact_summary"] == fact_summary
        assert ready["items"][0]["citable_evidence_count"] == 1
        assert discovery_only["total"] == 19
        assert facets["reader_ready"] is True
        assert facets["families"] == [{"value": "regulatory", "count": 1}]
        assert facets["sources"] == [{"value": "src", "count": 1}]
        assert evidence_items[0]["evidence_id"] == "reader-ready-evidence"
        assert evidence_items[0]["reader_eligible"] == 1
        assert evidence_items[1]["evidence_id"] == "reader-unrelated-evidence"
        assert evidence_items[1]["reader_eligible"] == 0

        # Formal light verification strengthens the same evidence; it must not
        # make an otherwise valid admission-v3 event disappear from the reader.
        connection = open_ledger(ledger_path)
        connection.execute(
            "UPDATE event_evidence SET evidence_status='accepted_light_primary_evidence' "
            "WHERE evidence_id='reader-ready-evidence'"
        )
        connection.commit()
        connection.close()
        assert LedgerRepository(ledger_path).list_events(reader_ready=True, limit=200)[
            "total"
        ] == 1
        assert LedgerRepository(ledger_path).event_evidence("pending-0")[0][
            "reader_eligible"
        ] == 1

        # Reuse the reader-ready fixture as a verified recent event so the API
        # overview contract can prove both gating and response-field hygiene.
        connection = open_ledger(ledger_path)
        connection.execute(
            "UPDATE canonical_events SET status='verified',label_status='verified' "
            "WHERE event_id='pending-0'"
        )
        connection.execute(
            "UPDATE event_versions SET status='verified',label_status='verified' "
            "WHERE event_id='pending-0' AND version=1"
        )
        connection.commit()
        connection.close()

        settings = replace(_settings(root, ledger_path), reviewer_token="review-secret")
        application = create_app(settings)
        with TestClient(application) as client:
            api_overview = client.get("/api/v1/overview")
            response = client.get("/api/v1/events")
            bypass_attempt = client.get(
                "/api/v1/events", params={"reader_ready": "false"}
            )
            invalid_credential = client.get(
                "/api/v1/events",
                params={"reader_ready": "false"},
                headers={"X-Reviewer-Token": "invalid"},
            )
            facet_response = client.get("/api/v1/events/facets")
            visible_detail = client.get("/api/v1/events/pending-0")
            visible_evidence = client.get("/api/v1/events/pending-0/evidence")
            hidden_public = {
                path: client.get(path)
                for path in (
                    "/api/v1/events/rough-insufficient-00",
                    "/api/v1/events/rough-insufficient-00/evidence",
                )
            }
            protected_timeline = client.get("/api/v1/events/pending-0/timeline")
            admin_all = client.get(
                "/api/v1/events", headers={"X-Admin-Token": "test-secret"}
            )
            admin_hidden = client.get(
                "/api/v1/events/rough-insufficient-00",
                headers={"X-Admin-Token": "test-secret"},
            )
            admin_evidence = client.get(
                "/api/v1/events/pending-0/evidence",
                headers={"X-Admin-Token": "test-secret"},
            )
            admin_visible_detail = client.get(
                "/api/v1/events/pending-0",
                headers={"X-Admin-Token": "test-secret"},
            )
            reviewer_hidden = client.get(
                "/api/v1/events/rough-insufficient-00",
                headers={"X-Reviewer-Token": "review-secret"},
            )
            reviewer_timeline = client.get(
                "/api/v1/events/rough-insufficient-00/timeline",
                headers={"X-Reviewer-Token": "review-secret"},
            )
        assert response.status_code == 200
        # Public browsing contains every canonical event. Evidentiary readiness
        # remains visible per item and may only be used as an authenticated
        # reviewer filter; it is no longer a public visibility switch.
        assert response.json()["data"]["total"] == 20
        assert response.json()["data"]["reader_ready"] is None
        public_event_keys = {
            "event_id",
            "current_version",
            "status",
            "public_state",
            "event_family",
            "event_type",
            "event_date",
            "first_seen_at",
            "last_updated_at",
            "ticker_at_event",
            "company_name",
            "discovery_source",
            "reviewed_at",
            "captured_source_count",
            "citable_evidence_count",
            "public_fact_summary",
            "claim_subject",
            "claim_action",
            "claim_stage",
            "known_at",
            "reader_ready",
            "no_trading",
            "unverified_capture_excerpt",
            "summary_basis",
        }
        assert set(response.json()["data"]["items"][0]) == public_event_keys
        assert api_overview.status_code == 200
        assert len(api_overview.json()["data"]["recent_events"]) == 1
        assert set(api_overview.json()["data"]["recent_events"][0]) == public_event_keys
        assert "stable_id" not in response.json()["data"]["items"][0]
        assert "manual_grade" not in response.json()["data"]["items"][0]
        assert "label_status" not in response.json()["data"]["items"][0]
        assert bypass_attempt.status_code == 200
        assert bypass_attempt.json()["data"]["total"] == 20
        assert bypass_attempt.json()["data"]["reader_ready"] is None
        assert invalid_credential.json()["data"]["total"] == 20
        assert invalid_credential.json()["data"]["reader_ready"] is None
        assert facet_response.status_code == 200
        assert facet_response.json()["data"]["reader_ready"] is None
        assert visible_detail.status_code == 200
        public_detail = visible_detail.json()["data"]
        assert set(public_detail) == {
            "event",
            "current_version",
            "preferred_source",
            "evidence_count",
            "no_trading_banner",
            "verification_method",
        }
        assert set(public_detail["current_version"]) == {"version", "facts"}
        assert set(public_detail["current_version"]["facts"]) == {
            "public_fact_summary",
            "claim_subject",
            "claim_action",
            "claim_stage",
            "known_at",
        }
        assert set(public_detail["event"]) == public_event_keys
        assert "created_reason" not in public_detail["current_version"]
        assert "assessment" not in public_detail
        assert "market_jobs" not in public_detail
        assert "model_shadow_output" not in public_detail
        public_detail_payload = json.dumps(public_detail, ensure_ascii=False)
        assert legacy_fact_marker not in public_detail_payload
        assert legacy_evidence_marker not in public_detail_payload
        assert public_detail["verification_method"]["evidence_ids"] == [
            "reader-ready-evidence"
        ]
        assert "rationale" not in public_detail["verification_method"]
        assert visible_evidence.status_code == 200
        assert [
            item["evidence_id"] for item in visible_evidence.json()["data"]["items"]
        ] == ["reader-ready-evidence"]
        assert set(visible_evidence.json()["data"]["items"][0]) == {
            "evidence_id",
            "evidence_url",
            "evidence_passage",
            "filing_date",
            "form",
            "source_name",
            "authority_tier",
            "source_type",
            "source_published_at",
            "local_received_at",
            "evidence_status",
            "relation_status",
            "subject_match",
            "event_claim_supported",
            "date_coherent",
            "dual_human_receipt_consistent",
            "reader_eligible",
        }
        assert visible_evidence.json()["data"]["items"][0]["form"] == "8-K"
        assert "observation_id" not in visible_evidence.json()["data"]["items"][0]
        assert "evidence_fingerprint" not in visible_evidence.json()["data"]["items"][0]
        assert "auto_verification_allowed" not in visible_evidence.json()["data"]["items"][0]
        assert all(response.status_code == 200 for response in hidden_public.values())
        assert hidden_public[
            "/api/v1/events/rough-insufficient-00/evidence"
        ].json()["data"]["items"] == []
        hidden_detail = hidden_public[
            "/api/v1/events/rough-insufficient-00"
        ].json()["data"]
        assert hidden_detail["event"]["reader_ready"] == 0
        assert "market_jobs" not in hidden_detail
        assert "assessment" not in hidden_detail
        assert protected_timeline.status_code == 403
        assert admin_all.status_code == 200
        assert admin_all.json()["data"]["total"] == 20
        assert admin_all.json()["data"]["reader_ready"] is None
        assert admin_hidden.status_code == 200
        assert "market_jobs" in admin_hidden.json()["data"]
        assert "stable_id" in admin_all.json()["data"]["items"][0]
        assert admin_evidence.status_code == 200
        assert "observation_id" in admin_evidence.json()["data"]["items"][0]
        assert "evidence_fingerprint" in admin_evidence.json()["data"]["items"][0]
        assert (
            admin_visible_detail.json()["data"]["current_version"]["facts"]
            ["light_verification"]["rationale"]
            == "internal reviewer rationale must not cross the public API"
        )
        assert (
            admin_visible_detail.json()["data"]["current_version"]["facts"]
            ["fact_summary"]
            == legacy_fact_marker
        )
        assert (
            admin_visible_detail.json()["data"]["current_version"]["facts"]
            ["evidence_summary"]
            == legacy_evidence_marker
        )
        assert reviewer_hidden.status_code == 200
        assert reviewer_timeline.status_code == 200

        # A later source edit is global reader state, not an SEC-only concern.
        # The old passage may not remain public unless it is still provably in
        # current content; a delete always hides it.
        connection = open_ledger(ledger_path)
        connection.execute(
            """INSERT INTO source_revisions VALUES (
               'reader-ready-edit','reader-ready-observation','src',
               'reader-ready-external',1,'edit',?,?,'Amended notice',
               'The amended source no longer contains the selected passage.','{}')""",
            (evidence_at, "9" * 64),
        )
        connection.commit()
        connection.close()
        assert repository.list_events(reader_ready=True, limit=200)["total"] == 0
        edited = next(
            item
            for item in repository.event_evidence("pending-0")
            if item["evidence_id"] == "reader-ready-evidence"
        )
        assert edited["latest_revision_kind"] == "edit"
        assert edited["reader_eligible"] == 0

        connection = open_ledger(ledger_path)
        connection.execute(
            """INSERT INTO source_revisions VALUES (
               'reader-ready-delete','reader-ready-observation','src',
               'reader-ready-external',2,'delete',?,?,'Deleted notice','','{}')""",
            (evidence_at, "8" * 64),
        )
        connection.commit()
        connection.close()
        deleted = next(
            item
            for item in repository.event_evidence("pending-0")
            if item["evidence_id"] == "reader-ready-evidence"
        )
        assert deleted["observation_status"] == "deleted"
        assert deleted["reader_eligible"] == 0


def test_unready_public_event_never_promotes_private_fact_fallbacks() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ledger_path = _populated_ledger(root)
        connection = open_ledger(ledger_path)
        captured_at = "2026-08-05T00:00:00+00:00"
        private_fact = "REVIEWER_PRIVATE_FACT_DO_NOT_PUBLISH"
        private_evidence = "INTERNAL_DETECTOR_REASON_DO_NOT_PUBLISH"
        raw_marker = "RAW_JSON_DO_NOT_PUBLISH"
        captured_excerpt = (
            "The source API reported a filing-related discovery item that still "
            "requires independent verification."
        )
        connection.execute(
            """INSERT INTO event_versions VALUES (
               'rough-insufficient-00',1,?,'candidate','candidate','regulatory',
               'filing',NULL,?,'seed')""",
            (
                captured_at,
                json.dumps(
                    {
                        "fact_summary": private_fact,
                        "evidence_summary": private_evidence,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        connection.execute(
            """INSERT INTO raw_observations(
               observation_id,source_id,external_id,source_published_at,local_received_at,
               title,summary,canonical_url,content_sha256,raw_json,observation_status
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "unready-public-capture",
                "src",
                "unready-public-external",
                captured_at,
                captured_at,
                "Captured source headline",
                captured_excerpt,
                "https://example.test/captured-source",
                "a" * 64,
                json.dumps({"private": raw_marker}),
                "captured",
            ),
        )
        connection.execute(
            "INSERT INTO event_observations VALUES (?,?,?,?)",
            (
                "rough-insufficient-00",
                "unready-public-capture",
                "primary",
                captured_at,
            ),
        )
        connection.commit()
        connection.close()

        application = create_app(_settings(root, ledger_path))
        with TestClient(application) as client:
            feed = client.get(
                "/api/v1/events", params={"q": "rough-insufficient-00"}
            )
            detail = client.get("/api/v1/events/rough-insufficient-00")
            dossier = client.get("/api/v1/events/rough-insufficient-00/dossier")

        assert feed.status_code == detail.status_code == dossier.status_code == 200
        feed_item = feed.json()["data"]["items"][0]
        detail_data = detail.json()["data"]
        dossier_data = dossier.json()["data"]
        assert feed_item["public_fact_summary"] is None
        assert feed_item["unverified_capture_excerpt"] == captured_excerpt
        assert feed_item["summary_basis"] == "UNVERIFIED_CAPTURE_EXCERPT"
        assert detail_data["event"]["unverified_capture_excerpt"] == captured_excerpt
        assert detail_data["event"]["summary_basis"] == "UNVERIFIED_CAPTURE_EXCERPT"
        assert detail_data["current_version"]["facts"] == {}
        assert dossier_data["detail"] == detail_data
        public_payload = json.dumps(
            {"feed": feed.json(), "detail": detail.json(), "dossier": dossier.json()},
            ensure_ascii=False,
        )
        assert private_fact not in public_payload
        assert private_evidence not in public_payload
        assert raw_marker not in public_payload


def test_excluded_archive_exposes_capture_without_promoting_it_to_evidence() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ledger_path = _populated_ledger(root)
        captured_at = "2026-08-19T08:09:23+00:00"
        connection = open_ledger(ledger_path)
        connection.execute(
            """INSERT INTO raw_observations(
               observation_id,source_id,external_id,source_published_at,local_received_at,
               title,summary,canonical_url,content_sha256,raw_json,observation_status
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "excluded-capture",
                "src",
                "provider-3637286",
                "2026-08-19T00:23:30+00:00",
                captured_at,
                "Markets await central-bank minutes while gold rises",
                "A source-provided discovery summary that has not been verified.",
                "https://example.test/discovery",
                "a" * 64,
                json.dumps(
                    {
                        "item": {
                            "score": 90,
                            "grade": "A+",
                            "signal": "long",
                            "private_marker": "MUST_NOT_LEAK",
                        }
                    }
                ),
                "captured",
            ),
        )
        connection.execute(
            """INSERT INTO event_observations VALUES (
               'excluded-0','excluded-capture','filtered_aggregated_noise',?)""",
            (captured_at,),
        )
        connection.commit()
        connection.close()

        application = create_app(
            replace(
                _settings(root, ledger_path),
                reviewer_token="review-secret",
                operator_token="operator-secret",
            )
        )
        with TestClient(application) as client:
            default_feed = client.get("/api/v1/events")
            archive_feed = client.get(
                "/api/v1/events", params={"public_state": "excluded"}
            )
            detail = client.get("/api/v1/events/excluded-0")
            evidence = client.get("/api/v1/events/excluded-0/evidence")
            sources = client.get("/api/v1/events/excluded-0/sources")
            interpretations = client.get(
                "/api/v1/events/excluded-0/source-interpretations"
            )
            dossier = client.get("/api/v1/events/excluded-0/dossier")
            public_mutation = client.post(
                "/api/v1/events/excluded-0/sources/excluded-capture/interpret",
                json={"audit_write_confirmed": True},
            )
            persisted = client.post(
                "/api/v1/events/excluded-0/sources/excluded-capture/interpret",
                headers={"X-Operator-Token": "operator-secret"},
                json={"audit_write_confirmed": True},
            )
            persisted_retry = client.post(
                "/api/v1/events/excluded-0/sources/excluded-capture/interpret",
                headers={"X-Operator-Token": "operator-secret"},
                json={"audit_write_confirmed": True},
            )
            interpretations_after = client.get(
                "/api/v1/events/excluded-0/source-interpretations"
            )
            hidden_without_capture = client.get("/api/v1/events/weak-0")
            internal_sources = client.get(
                "/api/v1/events/excluded-0/sources",
                headers={"X-Reviewer-Token": "review-secret"},
            )

        assert default_feed.status_code == 200
        assert any(
            item["event_id"] == "excluded-0"
            for item in default_feed.json()["data"]["items"]
        )
        assert archive_feed.status_code == 200
        assert archive_feed.json()["data"]["reader_ready"] is None
        assert archive_feed.json()["data"]["captured_source_required"] is False
        assert [
            item["event_id"] for item in archive_feed.json()["data"]["items"]
        ] == ["excluded-0"]
        assert archive_feed.json()["data"]["items"][0]["captured_source_count"] == 1
        assert detail.status_code == 200
        assert detail.json()["data"]["event"]["reader_ready"] == 0
        assert evidence.status_code == 200
        assert evidence.json()["data"]["items"] == []
        assert sources.status_code == 200
        public_sources = sources.json()["data"]
        assert public_sources["contract"] == {
            "captured_source_is_not_evidence": True,
            "canonical_state_unchanged": True,
            "no_trading": True,
        }
        assert len(public_sources["items"]) == 1
        source = public_sources["items"][0]
        assert set(source) == {
            "source_name",
            "source_type",
            "authority_tier",
            "source_title",
            "source_excerpt",
            "source_excerpt_original_length",
            "source_excerpt_truncated",
            "source_url",
            "source_published_at",
            "local_received_at",
            "latest_revision_no",
            "latest_revision_kind",
            "capture_receipt_sha256",
            "capture_status",
            "is_citable_evidence",
            "formal_verification",
            "no_trading",
        }
        assert source["capture_status"] == "FILTERED_DISCOVERY"
        assert source["is_citable_evidence"] is False
        assert source["formal_verification"] is False
        assert source["no_trading"] is True
        assert source["source_excerpt_original_length"] == len(
            "A source-provided discovery summary that has not been verified."
        )
        assert source["source_excerpt_truncated"] is False
        assert source["source_url"] == "https://example.test/discovery"
        public_payload = json.dumps(public_sources, ensure_ascii=False)
        assert "MUST_NOT_LEAK" not in public_payload
        assert '"score"' not in public_payload
        assert '"signal"' not in public_payload
        assert interpretations.status_code == 200
        interpretation_data = interpretations.json()["data"]
        assert interpretation_data["contract"] == {
            "version": "api-capture-interpretation-v1",
            "advisory_only": True,
            "canonical_mutation_allowed": False,
            "used_as_model_feature": False,
            "public_requests_are_cached_or_deterministic": True,
            "external_provider_configured": False,
            "no_trading": True,
        }
        assert len(interpretation_data["items"]) == 1
        assert dossier.status_code == 200
        dossier_data = dossier.json()["data"]
        assert dossier_data["detail"] == detail.json()["data"]
        assert dossier_data["evidence"] == evidence.json()["data"]
        assert dossier_data["sources"] == public_sources
        assert dossier_data["source_interpretations"]["contract"] == interpretation_data[
            "contract"
        ]
        assert dossier_data["source_interpretations"]["items"][0]["one_line_zh"] == (
            interpretation_data["items"][0]["one_line_zh"]
        )
        assert dossier_data["contract"] == {
            "public_projection": True,
            "consistency_scope": "bounded_multi_read_best_effort",
            "no_trading": True,
        }
        dossier_payload = json.dumps(dossier_data, ensure_ascii=False)
        assert "MUST_NOT_LEAK" not in dossier_payload
        assert "market_jobs" not in dossier_payload
        preview = interpretation_data["items"][0]
        assert preview["mode"] == "DETERMINISTIC"
        assert preview["persisted"] is False
        assert preview["external_generation_state"] == "NOT_CONFIGURED"
        assert preview["safety"]["formal_status_mutated"] is False
        assert preview["safety"]["used_as_model_feature"] is False
        assert "confidence" not in preview
        assert public_mutation.status_code in {403, 503}
        assert persisted.status_code == 200
        assert persisted.json()["data"]["created"] is True
        assert persisted.json()["data"]["external_call"] is False
        assert persisted.json()["data"]["estimated_usd"] == 0
        assert persisted_retry.status_code == 200
        assert persisted_retry.json()["data"]["created"] is False
        cached = interpretations_after.json()["data"]["items"][0]
        assert cached["persisted"] is True
        assert cached["external_generation_state"] == "NOT_CONFIGURED"
        with closing(sqlite3.connect(root / "operations.sqlite3")) as operations_connection:
            operations_connection.row_factory = sqlite3.Row
            rows = operations_connection.execute(
                "SELECT * FROM capture_interpretation_runs"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["canonical_mutation_allowed"] == 0
        assert rows[0]["no_trading"] == 1
        assert hidden_without_capture.status_code == 200
        assert hidden_without_capture.json()["data"]["event"]["reader_ready"] == 0
        assert internal_sources.status_code == 200
        assert "raw_json" in internal_sources.json()["data"]["items"][0]


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


def test_public_health_and_sources_exclude_internal_diagnostics() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ledger_path = root / "ledger.sqlite3"
        connection = open_ledger(ledger_path)
        observed_at = "2026-08-20T09:30:00+00:00"
        cursor_marker = "CURSOR_MARKER /srv/private/source.cursor"
        source_error_marker = "SOURCE_ERROR_MARKER C:\\internal\\collector.log"
        connection.execute(
            "INSERT INTO sources VALUES ('src','Public Source','official_primary','P0',1,1,?,?)",
            (observed_at, observed_at),
        )
        connection.execute(
            """INSERT INTO source_cursors VALUES (
               'src','opaque',?,'private-etag','private-last-modified',?,?,
               'FAILED',?,?)""",
            (
                cursor_marker,
                observed_at,
                observed_at,
                source_error_marker,
                observed_at,
            ),
        )
        connection.commit()
        connection.close()

        settings = replace(_settings(root, ledger_path), reviewer_token="review-secret")
        application = create_app(settings)
        artifact_marker = "ARTIFACT_PATH_MARKER D:\\models\\private.joblib"
        load_error_marker = "MODEL_LOAD_ERROR_MARKER /opt/private/model.log"
        model_card_marker = "MODEL_CARD_MARKER internal training inventory"
        robustness_marker = "ROBUSTNESS_MARKER private ablation"
        external_marker = "EXTERNAL_BLIND_MARKER private frozen rows"
        model_status = {
            "status": "fallback",
            "artifact_path": artifact_marker,
            "artifact_sha256": "private-artifact-sha",
            "model_version": "risk-router-test-v4",
            "architecture": "three_layer_downside_router",
            "abstain_threshold": 0.62,
            "risk_rescue_floor": 0.8,
            "risk_rescue_margin": 0.1,
            "semantic_risk_threshold": 0.7,
            "structured_evidence_gate": {
                "version": "structured-v1",
                "required_for_v4": True,
                "internal_path": "/private/structured-gate.json",
            },
            "semantic_policy_gate": {
                "version": "semantic-v1",
                "enforced_for_v4": True,
                "internal_error": "private semantic diagnostic",
            },
            "operational_scope_gate": {
                "version": "scope-v1",
                "enforced": True,
                "purpose": "private implementation text",
                "artifact_unchanged": True,
            },
            "shadow": True,
            "no_trading": True,
            "load_error": load_error_marker,
            "model_card": {"marker": model_card_marker},
            "robustness": {"marker": robustness_marker},
            "external_blind": {"marker": external_marker},
        }
        application.state.router.status = lambda: model_status

        with TestClient(application) as client:
            public_health = client.get("/api/v1/health")
            public_sources = client.get("/api/v1/sources/health")
            public_overview = client.get("/api/v1/overview")
            invalid_health = client.get(
                "/api/v1/health",
                headers={"X-Reviewer-Token": "invalid"},
            )
            invalid_sources = client.get(
                "/api/v1/sources/health",
                headers={"X-Reviewer-Token": "invalid"},
            )
            invalid_overview = client.get(
                "/api/v1/overview",
                headers={"X-Reviewer-Token": "invalid"},
            )
            admin_health = client.get(
                "/api/v1/health",
                headers={"X-Admin-Token": "test-secret"},
            )
            admin_sources = client.get(
                "/api/v1/sources/health",
                headers={"X-Admin-Token": "test-secret"},
            )
            admin_overview = client.get(
                "/api/v1/overview",
                headers={"X-Admin-Token": "test-secret"},
            )
            reviewer_health = client.get(
                "/api/v1/health",
                headers={"X-Reviewer-Token": "review-secret"},
            )
            reviewer_sources = client.get(
                "/api/v1/sources/health",
                headers={"X-Reviewer-Token": "review-secret"},
            )
            reviewer_overview = client.get(
                "/api/v1/overview",
                headers={"X-Reviewer-Token": "review-secret"},
            )

        public_model = public_health.json()["data"]["model"]
        assert set(public_model) == {
            "status",
            "model_version",
            "architecture",
            "structured_evidence_gate",
            "semantic_policy_gate",
            "operational_scope_gate",
            "shadow",
            "no_trading",
        }
        assert set(public_model["structured_evidence_gate"]) == {
            "version",
            "required_for_v4",
        }
        assert set(public_model["semantic_policy_gate"]) == {
            "version",
            "enforced_for_v4",
        }
        assert set(public_model["operational_scope_gate"]) == {
            "version",
            "enforced",
        }
        assert public_model["model_version"] == "risk-router-test-v4"
        assert public_model["shadow"] is True
        assert public_model["no_trading"] is True

        public_source = public_sources.json()["data"]["items"][0]
        assert public_source == {
            "name": "Public Source",
            "source_type": "official_primary",
            "authority_tier": "P0",
            "status": "FAILED",
            "last_success_at": observed_at,
        }
        assert public_overview.json()["data"]["source_health"] == [public_source]
        assert invalid_health.json()["data"]["model"] == public_model
        assert invalid_sources.json()["data"]["items"] == [public_source]
        assert invalid_overview.json()["data"]["source_health"] == [public_source]

        public_payload = json.dumps(
            {
                "health": public_health.json(),
                "sources": public_sources.json(),
                "overview": public_overview.json(),
                "invalid_health": invalid_health.json(),
                "invalid_sources": invalid_sources.json(),
                "invalid_overview": invalid_overview.json(),
            },
            ensure_ascii=False,
        )
        for marker in (
            artifact_marker,
            load_error_marker,
            model_card_marker,
            robustness_marker,
            external_marker,
            cursor_marker,
            source_error_marker,
            "/private/structured-gate.json",
            "private semantic diagnostic",
            "private implementation text",
        ):
            assert marker not in public_payload

        for protected_health in (admin_health, reviewer_health):
            assert protected_health.status_code == 200
            assert protected_health.json()["data"]["model"] == model_status
        for protected_sources in (admin_sources, reviewer_sources):
            assert protected_sources.status_code == 200
            internal_source = protected_sources.json()["data"]["items"][0]
            assert internal_source["source_id"] == "src"
            assert internal_source["last_error"] == source_error_marker
            assert internal_source["cursors"][0]["cursor_value"] == cursor_marker
        for protected_overview in (admin_overview, reviewer_overview):
            overview_internal_source = protected_overview.json()["data"]["source_health"][0]
            assert overview_internal_source["source_id"] == "src"
            assert overview_internal_source["last_error"] == source_error_marker
            assert overview_internal_source["cursors"][0]["cursor_value"] == cursor_marker


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

        settings = replace(_settings(root, ledger_path), reviewer_token="review-secret")
        application = create_app(settings)
        operations = OperationsRepository(settings.operations_db)
        error_marker = "WORKER_ERROR_MARKER C:\\internal\\collector.py"
        stdout_marker = "WORKER_STDOUT_MARKER /opt/finance-radar/private"
        stderr_marker = "WORKER_STDERR_MARKER D:\\private\\upstream.log"
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
                    json.dumps(
                        {
                            "process": {
                                "stdout_tail": stdout_marker,
                                "stderr_tail": stderr_marker,
                            },
                            "internal_report_path": "/srv/finance-radar/private/report.json",
                        }
                    ),
                    error_marker,
                ),
            )
            connection.commit()

        with TestClient(application) as client:
            response = client.get("/api/v1/overview")
            admin_overview = client.get(
                "/api/v1/overview",
                headers={"X-Admin-Token": "test-secret"},
            )
            reviewer_overview = client.get(
                "/api/v1/overview",
                headers={"X-Reviewer-Token": "review-secret"},
            )
            invalid_reviewer_overview = client.get(
                "/api/v1/overview",
                headers={"X-Reviewer-Token": "invalid"},
            )
            filtered = client.get(
                "/api/v1/events",
                params={
                    "public_state": "pending_verification",
                    "date_from": now.date().isoformat(),
                    "date_to": now.date().isoformat(),
                    "sort": "latest",
                },
                headers={"X-Admin-Token": "test-secret"},
            )
            detail_response = client.get(
                "/api/v1/events/freshness-event",
                headers={"X-Admin-Token": "test-secret"},
            )
            bad_state = client.get("/api/v1/events", params={"public_state": "not-a-state"})
            bad_sort = client.get("/api/v1/events", params={"sort": "latest;select"})
            bad_range = client.get(
                "/api/v1/events",
                params={"date_from": "2026-08-02", "date_to": "2026-08-01"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        timing = data["timing"]
        public_cycle = data["latest_worker_cycle"]
        assert set(public_cycle) == {
            "status",
            "started_at",
            "finished_at",
            "elapsed_seconds",
        }
        assert public_cycle == {
            "status": "FAILED",
            "started_at": failed_started.isoformat(),
            "finished_at": failed_finished.isoformat(),
            "elapsed_seconds": 60.0,
        }
        public_payload = json.dumps(response.json(), ensure_ascii=False)
        assert error_marker not in public_payload
        assert stdout_marker not in public_payload
        assert stderr_marker not in public_payload
        assert "/srv/finance-radar/private/report.json" not in public_payload
        assert "result" not in public_cycle
        assert "error" not in public_cycle
        assert "cycle_id" not in public_cycle
        assert invalid_reviewer_overview.status_code == 200
        assert set(
            invalid_reviewer_overview.json()["data"]["latest_worker_cycle"]
        ) == set(public_cycle)
        invalid_payload = json.dumps(
            invalid_reviewer_overview.json(),
            ensure_ascii=False,
        )
        assert error_marker not in invalid_payload
        assert stdout_marker not in invalid_payload
        assert stderr_marker not in invalid_payload

        for protected_response in (admin_overview, reviewer_overview):
            assert protected_response.status_code == 200
            protected_cycle = protected_response.json()["data"]["latest_worker_cycle"]
            assert set(protected_cycle) == {
                "cycle_id",
                "started_at",
                "finished_at",
                "status",
                "result",
                "error",
            }
            assert protected_cycle["error"] == error_marker
            assert protected_cycle["result"]["process"]["stdout_tail"] == stdout_marker
            assert protected_cycle["result"]["process"]["stderr_tail"] == stderr_marker
            assert (
                protected_cycle["result"]["internal_report_path"]
                == "/srv/finance-radar/private/report.json"
            )
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
    assert "LEFT JOIN ranked_light_followups" in PUBLIC_EVENT_STATE_CTE
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


def test_public_market_and_replay_contracts_exclude_internal_diagnostics() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ledger_path = root / "ledger.sqlite3"
        open_ledger(ledger_path).close()
        settings = replace(_settings(root, ledger_path), reviewer_token="review-secret")
        application = create_app(settings)

        market_error_marker = "MARKET_ERROR_MARKER C:\\private\\provider.log"
        market_window_marker = "MARKET_WINDOW_MARKER /srv/private/window.json"
        market_url_marker = "MARKET_URL_MARKER https://internal.example.test/jobs"
        market_payload = {
            "providers": [
                {
                    "provider_id": "fixture_provider",
                    "name": "Fixture Market Data",
                    "status": "DEGRADED",
                    "freshness_status": "STALE_EVENT_CAPTURE",
                    "last_snapshot_at": "2026-08-20T10:00:00+00:00",
                    "read_only": True,
                    "order_endpoints_present": False,
                    "last_error": market_error_marker,
                    "jobs": 41,
                    "completed_jobs": 39,
                    "pending_jobs": 1,
                    "snapshots": 120,
                    "snapshot_age_seconds": 7200,
                    "observation_windows": {
                        market_window_marker: {"FAILED": 1},
                    },
                    "internal_jobs_url": market_url_marker,
                }
            ],
            "provider_policy": {"internal": market_url_marker},
            "horizon_policy": {
                "windows": [market_window_marker],
                "internal_path": "/srv/private/horizon-policy.json",
            },
            "boundary": {
                "read_only": True,
                "no_trading": True,
                "post_event_audit_only": True,
                "account_data_used": False,
                "allowed_as_model_feature": False,
                "internal_policy_path": "/srv/private/market-boundary.json",
            },
        }

        fixture_marker = "REPLAY_FIXTURE_MARKER C:\\private\\case.json"
        observation_marker = "REPLAY_OBSERVATION_MARKER internal trace"
        replay_items = [
            {
                "case_id": "public-demo-case",
                "title": "Public replay demonstration",
                "description": "A frozen public teaching case.",
                "expected_label": "INTERNAL_EXPECTED_LABEL",
                "fixture": fixture_marker,
                "observation_count": 1,
                "observations": [
                    {
                        "at_seconds": 0,
                        "source": "issuer_release",
                        "authority_tier": "P1",
                        "title": "Issuer publishes a notice",
                        "passage": "The issuer published a frozen demonstration notice.",
                        "contradicts": False,
                        "revision_kind": "INITIAL",
                        "internal_trace": observation_marker,
                    }
                ],
            }
        ]
        run_id_marker = "REPLAY_RUN_ID_MARKER internal-run-123"
        model_version_marker = "REPLAY_MODEL_MARKER private-v9"
        replay_result_marker = "REPLAY_RESULT_MARKER /srv/private/result.json"
        replay_error_marker = "REPLAY_ERROR_MARKER C:\\private\\runner.log"
        replay_runs = [
            {
                "run_id": run_id_marker,
                "case_id": "public-demo-case",
                "status": "COMPLETED",
                "mode": "REPLAY",
                "started_at": "2026-08-20T11:00:00+00:00",
                "finished_at": "2026-08-20T11:00:02+00:00",
                "result": {"internal_result_path": replay_result_marker},
                "model_version": model_version_marker,
                "error": None,
            },
            {
                "run_id": "REPLAY_FAILED_RUN_ID_MARKER",
                "case_id": "public-demo-case",
                "status": "FAILED",
                "mode": "REPLAY",
                "started_at": "2026-08-20T12:00:00+00:00",
                "finished_at": "2026-08-20T12:00:01+00:00",
                "result": {},
                "model_version": model_version_marker,
                "error": replay_error_marker,
            },
        ]
        application.state.ledger.market_capabilities = lambda: market_payload
        application.state.replay.cases = lambda: replay_items
        application.state.operations.replay_runs = lambda: replay_runs

        with TestClient(application) as client:
            public_market = client.get("/api/v1/market/capabilities")
            public_replays = client.get("/api/v1/replays")
            invalid_market = client.get(
                "/api/v1/market/capabilities",
                headers={
                    "X-Reviewer-Token": "invalid",
                    "X-Admin-Token": "invalid",
                },
            )
            invalid_replays = client.get(
                "/api/v1/replays",
                headers={
                    "X-Reviewer-Token": "invalid",
                    "X-Admin-Token": "invalid",
                },
            )
            admin_market = client.get(
                "/api/v1/market/capabilities",
                headers={"X-Admin-Token": "test-secret"},
            )
            admin_replays = client.get(
                "/api/v1/replays",
                headers={"X-Admin-Token": "test-secret"},
            )
            reviewer_market = client.get(
                "/api/v1/market/capabilities",
                headers={"X-Reviewer-Token": "review-secret"},
            )
            reviewer_replays = client.get(
                "/api/v1/replays",
                headers={"X-Reviewer-Token": "review-secret"},
            )

        assert public_market.status_code == 200
        market_data = public_market.json()["data"]
        assert set(market_data) == {"providers", "boundary"}
        assert set(market_data["providers"][0]) == {
            "provider_id",
            "name",
            "status",
            "freshness_status",
            "last_snapshot_at",
            "read_only",
            "order_endpoints_present",
        }
        assert market_data["providers"][0] == {
            "provider_id": "fixture_provider",
            "name": "Fixture Market Data",
            "status": "DEGRADED",
            "freshness_status": "STALE_EVENT_CAPTURE",
            "last_snapshot_at": "2026-08-20T10:00:00+00:00",
            "read_only": True,
            "order_endpoints_present": False,
        }
        assert market_data["boundary"] == {
            "read_only": True,
            "no_trading": True,
            "post_event_audit_only": True,
        }
        assert invalid_market.json()["data"] == market_data

        assert public_replays.status_code == 200
        replay_data = public_replays.json()["data"]
        assert set(replay_data) == {"items", "recent_runs"}
        assert set(replay_data["items"][0]) == {
            "case_id",
            "display_name",
            "display_description",
            "observations",
        }
        assert set(replay_data["items"][0]["observations"][0]) == {
            "at_seconds",
            "source",
            "authority_tier",
            "title",
            "passage",
            "contradicts",
            "revision_kind",
        }
        assert replay_data["items"][0]["case_id"] == "public-demo-case"
        assert replay_data["items"][0]["display_name"] == (
            "Public replay demonstration"
        )
        assert len(replay_data["recent_runs"]) == 2
        for run in replay_data["recent_runs"]:
            assert set(run) == {
                "case_id",
                "display_name",
                "status",
                "started_at",
                "finished_at",
                "summary",
            }
        assert replay_data["recent_runs"][0]["summary"] == (
            "Replay completed successfully."
        )
        assert replay_data["recent_runs"][1]["summary"] == (
            "Replay did not complete; details are available to reviewers."
        )
        assert invalid_replays.json()["data"] == replay_data

        public_payload = json.dumps(
            {
                "market": public_market.json(),
                "replays": public_replays.json(),
                "invalid_market": invalid_market.json(),
                "invalid_replays": invalid_replays.json(),
            },
            ensure_ascii=False,
        )
        for marker in (
            market_error_marker,
            market_window_marker,
            market_url_marker,
            "/srv/private/horizon-policy.json",
            "/srv/private/market-boundary.json",
            fixture_marker,
            observation_marker,
            run_id_marker,
            "REPLAY_FAILED_RUN_ID_MARKER",
            model_version_marker,
            replay_result_marker,
            replay_error_marker,
        ):
            assert marker not in public_payload

        for protected_market in (admin_market, reviewer_market):
            assert protected_market.status_code == 200
            assert protected_market.json()["data"] == market_payload
        for protected_replays in (admin_replays, reviewer_replays):
            assert protected_replays.status_code == 200
            assert protected_replays.json()["data"] == {
                "items": replay_items,
                "recent_runs": replay_runs,
            }
