import json
from pathlib import Path

from app.models.risk_label_contract import (
    EVIDENCE_STATES,
    LABELS,
    MATERIALITY,
    POLARITIES,
)
from app.services.light_verification import (
    AUTO_FORMAL_EVENT_TYPES,
    LIGHT_VERIFICATION_VERSION,
)


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "owner_intent_policy_v1.json"
DOCTRINE_PATH = ROOT / "docs" / "OWNER_INTENT_AND_SYSTEM_DOCTRINE.md"
PUBLIC_EVENT_CONTRACT_PATH = ROOT / "docs" / "PUBLIC_READER_EVENT_QUALITY_GATE.md"
SOURCE_RECOVERY_PATH = ROOT / "docs" / "SOURCE_CAPTURE_AND_EVIDENCE_RECOVERY.md"

EXPECTED_FORBIDDEN_OUTPUTS = {
    "LONG",
    "SHORT",
    "price_direction",
    "target_price",
    "expected_return",
    "expected_drawdown",
    "timing",
    "holding_period",
    "position_size",
    "leverage",
    "stop_loss",
    "alert_permission",
    "order_permission",
}

MANDATORY_HARD_GATE_IDS = {
    "FR-BAK-002",
    "FR-BAK-006",
    "FR-COST-002",
    "FR-EVD-001",
    "FR-EVD-004",
    "FR-EVD-008",
    "FR-EVD-009",
    "FR-EVD-010",
    "FR-EVT-005",
    "FR-GRADE-005",
    "FR-LOCAL-002",
    "FR-MDL-001",
    "FR-MDL-002",
    "FR-MDL-003",
    "FR-MDL-007",
    "FR-MKT-004",
    "FR-NOTIFY-002",
    "FR-PROD-001",
    "FR-PROD-008",
    "FR-REL-006",
    "FR-REV-002",
    "FR-REV-006",
    "FR-REV-007",
    "FR-REV-013",
    "FR-ROLE-001",
    "FR-ROLE-002",
    "FR-SEC-001",
    "FR-SEC-003",
    "FR-SHORT-001",
    "FR-SHORT-003",
    "FR-SHORT-004",
    "FR-SRC-002",
    "FR-STATE-001",
}


def _policy() -> dict:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def test_owner_intent_policy_has_safe_short_research_contract() -> None:
    policy = _policy()
    specialization = policy["short_research_specialization"]

    assert policy["contract_scope"] == (
        "repository_governance_audit_not_runtime_decision_source"
    )
    assert specialization["collection_polarity"] == "all_polarity"
    assert specialization["fact_verification_polarity"] == "all_polarity"
    assert specialization["allowed_decisions"] == [
        "RISK_REVIEW",
        "NON_TARGET",
        "ABSTAIN",
    ]
    assert specialization["execution_capability"] == "prohibited"
    assert specialization["legacy_asset_direction_contract"] == "ABSTAIN_ONLY"
    assert set(specialization["forbidden_outputs"]) == EXPECTED_FORBIDDEN_OUTPUTS
    assert specialization["maximum_authority_without_new_owner_decision"] == (
        "advisory_shadow_review_routing_only"
    )
    assert specialization["formal_fact_authority"] == "prohibited"
    assert specialization["auto_verification_authority"] == "prohibited"


def test_policy_keeps_status_namespaces_distinct_and_matches_label_contract() -> None:
    labels = _policy()["labels"]
    adjudication = labels["adjudication"]

    assert labels["canonical_event_statuses"] == [
        "candidate",
        "verified",
        "weak",
        "rejected",
    ]
    assert set(adjudication["labels"]) == LABELS
    assert set(adjudication["materiality"]) == MATERIALITY
    assert set(adjudication["polarity"]) == POLARITIES
    assert set(adjudication["evidence_state"]) == EVIDENCE_STATES
    assert labels["evidence_agent_statuses"] == [
        "EVIDENCE_READY",
        "INSUFFICIENT",
        "HUMAN_REVIEW",
    ]
    assert set(labels["risk_router_evidence_gate_states"]) == {
        "CONFLICTED",
        "PRIMARY_SUPPORTED_REVIEWED",
        "PRIMARY_SUPPORTED_LIGHT_VERIFIED",
        "PRIMARY_SUPPORTED_MACHINE_OFFICIAL",
        "DISCOVERY_ONLY",
        "INSUFFICIENT",
        "NOT_PROVIDED",
    }
    assert labels["namespaces_are_not_interchangeable"] is True

    non_primary = _policy()["model_evidence_gate"]["non_primary_supported"]
    assert non_primary == {
        "decision": "ABSTAIN",
        "decision_source": "DETERMINISTIC_EVIDENCE_GATE",
        "semantic_model_invoked": False,
        "confidence_applicable": False,
        "ui_shows_confidence": False,
    }


def test_public_contract_keeps_visibility_citation_evidence_and_risk_separate() -> None:
    contract = _policy()["public_event_contract"]

    assert contract["canonical_visibility"] == "ALL"
    assert set(contract["visibility_gates_forbidden"]) == {
        "canonical_status",
        "public_state",
        "rough_review",
        "light_verification",
        "dual_human_review",
        "citation_ready",
    }
    assert contract["citation_ready"] == {
        "kind": "deterministic_current_version_projection",
        "manual_approval": False,
        "visibility_gate": False,
        "formal_claim_gate": True,
    }
    assert contract["evidence_postures"] == [
        "PRIMARY_SUPPORTED",
        "PRIMARY_SOURCE_AVAILABLE",
        "SOURCE_CAPTURED",
        "NO_SOURCE",
    ]
    assert set(contract["evidence_gap_codes"]) == {
        "MISSING_SUBJECT",
        "MISSING_FACT_SUMMARY",
        "MISSING_CITABLE_EVIDENCE",
        "NO_CAPTURED_SOURCE",
    }
    assert set(contract["public_risk_assessment_fields"]) == {
        "route",
        "confidence",
        "confidence_applicable",
        "model_version",
        "decision_source",
        "evidence_state",
        "evaluated_at",
        "shadow",
        "current",
    }
    assert contract["workflow_status_public_semantics"] == "prohibited"
    assert contract["legacy_public_state_role"] == (
        "compatibility_disposition_only"
    )


def test_public_event_documents_do_not_restore_the_old_reader_ready_gate() -> None:
    public_contract = PUBLIC_EVENT_CONTRACT_PATH.read_text(encoding="utf-8")
    source_recovery = SOURCE_RECOVERY_PATH.read_text(encoding="utf-8")

    assert "Every canonical event is browseable" in public_contract
    assert "not a per-event publication queue" in public_contract
    assert all(
        posture in public_contract
        for posture in (
            "PRIMARY_SUPPORTED",
            "PRIMARY_SOURCE_AVAILABLE",
            "SOURCE_CAPTURED",
            "NO_SOURCE",
        )
    )
    assert "默认事件主流仍只展示 `reader_ready`" not in source_recovery
    assert "所有 canonical 事件进入同一个可浏览事件流" in source_recovery
    assert "`citation_ready` 是当前版本的自动派生属性" in source_recovery


def test_light_review_policy_matches_runtime_formalization_contract() -> None:
    review = _policy()["review_levels"]
    rough = review["rough_review"]
    light = review["light_detailed_review"]

    assert rough == {
        "may_change_canonical_label": False,
        "may_be_training_truth": False,
        "must_disclose_formal_verification_false": True,
    }
    assert light["review_output_may_mutate_canonical"] is False
    assert light["requires_scoped_expiring_authorization_for_formalization"] is True
    assert light["continuous_worker_may_apply"] is False
    assert light["implementation_contract_version"] == LIGHT_VERIFICATION_VERSION
    assert set(light["formalization_event_type_allowlist"]) == AUTO_FORMAL_EVENT_TYPES
    assert set(light["authorization_required_fields"]) == {
        "authorization_id",
        "actor",
        "purpose",
        "expires_at",
        "batch_id",
    }
    assert set(light["scope_entry_required_fields"]) == {
        "event_id",
        "current_version",
        "evidence_fingerprint",
    }
    assert set(light["formalization_preconditions"]) == {
        "candidate_status",
        "SUPPORTED",
        "no_blocking_conflict",
        "evidence_ids_unchanged",
        "transactional_current_recheck",
        "compare_and_swap",
    }
    assert light["fail_closed"] is True


def test_formal_label_policy_requires_real_independent_blind_humans() -> None:
    formal = _policy()["review_levels"]["formal_training_label"]

    assert set(formal["purposes"]) == {
        "training_gold_set",
        "evaluation_gold_set",
        "threshold_calibration",
        "accuracy_sampling",
        "drift_monitoring",
        "disagreement_and_high_risk_exception_review",
    }
    assert formal["per_event_publication_gate"] is False
    assert formal["human_only"] is True
    assert formal["independent_reviewers"] == 2
    assert formal["reviewer_identities_must_differ"] is True
    assert formal["third_arbiter_on_disagreement"] is True
    assert formal["arbiter_identity_must_differ"] is True
    assert formal["arbiter_only_on_disagreement"] is True
    assert formal["human_submits_target_label"] is False
    assert formal["target_label_derived_by_pure_function"] is True
    assert formal["split_before_validation"] == "UNASSIGNED"
    assert formal["near_duplicate_requires_similarity_check_not_only_equality_hash"] is True
    assert formal["freeze_requires_at_least_one_fully_held_out_source_family"] is True
    assert formal["freeze_receipt_and_sample_state_same_transaction"] is True
    assert formal["freeze_retry"] == "exact_receipt_only_idempotent"
    assert set(formal["freeze_authorization_contract_fields"]) == {
        "action",
        "authorization_id",
        "actor",
        "purpose",
        "expires_at",
        "freeze_id",
        "dataset_sha256",
        "sample_ids_sha256",
        "sample_count",
        "held_out_source_families",
    }
    assert set(formal["required_submission_fields"]) == {
        "exact_passage",
        "rationale",
        "materiality",
        "polarity",
        "evidence_state",
    }
    assert set(formal["hidden_inputs"]) == {
        "model_output",
        "peer_answer",
        "old_label",
        "post_event_market_outcome",
    }


def test_owner_intent_policy_preserves_network_operations_and_authority() -> None:
    policy = _policy()
    roles = policy["roles"]
    operations = policy["operations"]
    authority = policy["authority"]

    assert roles["public_api_exposure"] is False
    assert roles["api_network_boundary"] == {
        "systemd": "host_loopback",
        "compose": "private_container_network",
        "internet_exposed": False,
    }
    assert roles["compose_production_eligible"] is False
    assert operations["on_host_verified_daily_copies"] == 1
    assert operations["on_host_weekly_copies"] == 0
    assert operations["delete_previous_on_host_only_after_new_full_restore_verification"] is True
    assert operations["offhost_retention_policy"] == "separate_explicit_operator_policy"
    assert operations["public_repo_may_store_production_recovery_assets"] is False
    assert operations["large_windows_artifact_drive"] == "D"
    assert authority["old_approval_is_evergreen"] is False
    assert {
        "formal_light_verification_batch",
        "canonical_event_status_mutation",
    } <= set(authority["requires_separate_action_authorization"])


def test_every_mandatory_hard_gate_is_explained_in_doctrine() -> None:
    policy = _policy()
    doctrine = DOCTRINE_PATH.read_text(encoding="utf-8")
    hard_gates = policy["hard_gates"]
    ids = [gate["id"] for gate in hard_gates]

    assert len(ids) == len(set(ids))
    assert set(ids) == MANDATORY_HARD_GATE_IDS
    assert all(str(gate["rule"]).strip() for gate in hard_gates)
    for rule_id in ids:
        assert f"`{rule_id} · P`" in doctrine
