from __future__ import annotations

import hashlib

from app.models.risk_label_contract import deterministic_source_lane
from app.services.human_gold_freeze import assess_freeze_readiness


AXES = {
    "RISK_REVIEW": ("MATERIAL_ADVERSE", "ADVERSE", "PRIMARY_SUPPORTED"),
    "NON_TARGET": ("NOT_MATERIAL_ADVERSE", "NEUTRAL", "PRIMARY_SUPPORTED"),
    "ABSTAIN": ("UNCLEAR", "UNCLEAR", "INSUFFICIENT"),
}


def annotation(index: int, *, label: str, source_id: str) -> dict:
    materiality, polarity, evidence_state = AXES[label]
    unique_words = " ".join(
        "token" + hashlib.sha256(f"{index}:{word}".encode()).hexdigest()
        for word in range(30)
    )
    content = {
        "contract_version": "human-blind-v3.1",
        "as_of": f"2026-08-{index + 1:02d}T00:00:00+00:00",
        "headline": f"Unique issuer event {index}",
        "summary": unique_words,
        "passages": [{"passage": unique_words}],
        "source_identity_hidden": True,
        "target_label_hidden": True,
        "post_event_market_data_included": False,
        "model_output_included": False,
    }
    return {
        "sample_id": f"sample-{index}",
        "event_id": f"event-{index}",
        "text_sha256": hashlib.sha256(unique_words.encode()).hexdigest(),
        "content_present": True,
        "content": content,
        "source_id": source_id,
        "authority_tier": "P0",
        "source_lane": deterministic_source_lane("P0", evidence_state),
        "entity_group": f"issuer:{index}",
        "event_chain_group": f"chain:{index}",
        "label": label,
        "materiality": materiality,
        "polarity": polarity,
        "evidence_state": evidence_state,
        "rationale": "Two independent human reviewers reached this evidence-based conclusion.",
        "adjudicator_id": f"human-a-{index}",
        "reviewer_id": f"human-b-{index}",
        "adjudicated_at": f"2026-08-{index + 1:02d}T01:00:00+00:00",
        "source_used_as_label": False,
        "split": "UNASSIGNED",
        "resolution": "CONSENSUS",
    }


def _rows(*, blind_source: str = "cftc_releases") -> list[dict]:
    labels = ["RISK_REVIEW", "NON_TARGET", "ABSTAIN"] * 3
    sources = [
        "sec_current_filings",
        "federal_reserve_press",
        "ecb_press",
        "sec_current_filings",
        "federal_reserve_press",
        "ecb_press",
        blind_source,
        blind_source,
        blind_source,
    ]
    return [
        annotation(index, label=label, source_id=sources[index])
        for index, label in enumerate(labels)
    ]


def test_freeze_is_chronological_group_disjoint_and_source_held_out() -> None:
    result = assess_freeze_readiness(
        _rows(),
        split_sizes={"TRAIN": 3, "VALIDATION": 3, "HUMAN_BLIND": 3},
        label_minimums={
            split: {label: 1 for label in AXES}
            for split in ("TRAIN", "VALIDATION", "HUMAN_BLIND")
        },
        minimum_source_families=4,
    )

    assert result["status"] == "READY_TO_FREEZE"
    assert result["fully_held_out_blind_source_families"] == ["cftc"]
    assert result["issuer_overlap_count"] == 0
    assert result["event_chain_overlap_count"] == 0
    assert result["exact_text_overlap_count"] == 0
    assert result["near_duplicate_overlap_count"] == 0
    assert [row["split"] for row in result["rows"]] == [
        "TRAIN",
        "TRAIN",
        "TRAIN",
        "VALIDATION",
        "VALIDATION",
        "VALIDATION",
        "HUMAN_BLIND",
        "HUMAN_BLIND",
        "HUMAN_BLIND",
    ]


def test_freeze_fails_closed_without_a_real_source_holdout() -> None:
    result = assess_freeze_readiness(
        _rows(blind_source="sec_current_filings"),
        split_sizes={"TRAIN": 3, "VALIDATION": 3, "HUMAN_BLIND": 3},
        label_minimums={
            split: {label: 1 for label in AXES}
            for split in ("TRAIN", "VALIDATION", "HUMAN_BLIND")
        },
        minimum_source_families=3,
    )

    assert result["status"] == "NOT_READY_TO_FREEZE"
    assert result["rows"] == []
    assert "HUMAN_BLIND has no fully held-out source family" in result["issues"]
