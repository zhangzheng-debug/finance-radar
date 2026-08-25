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
    rows = _rows(blind_source="sec_current_filings")
    for row in rows:
        row["source_id"] = "sec_current_filings"
    result = assess_freeze_readiness(
        rows,
        split_sizes={"TRAIN": 3, "VALIDATION": 3, "HUMAN_BLIND": 3},
        label_minimums={
            split: {label: 1 for label in AXES}
            for split in ("TRAIN", "VALIDATION", "HUMAN_BLIND")
        },
        minimum_source_families=1,
    )

    assert result["status"] == "NOT_READY_TO_FREEZE"
    assert result["rows"] == []
    assert (
        "no source family is eligible for a metadata-only HUMAN_BLIND holdout"
        in result["issues"]
    )


def test_freeze_moves_an_early_predeclared_source_family_into_blind() -> None:
    rows = [
        annotation(
            index,
            label=("RISK_REVIEW", "NON_TARGET", "ABSTAIN")[index % 3],
            source_id="fda_enforcement" if index < 2 else "sec_current_filings",
        )
        for index in range(12)
    ]

    result = assess_freeze_readiness(
        rows,
        split_sizes={"TRAIN": 6, "VALIDATION": 3, "HUMAN_BLIND": 3},
        label_minimums={
            split: {} for split in ("TRAIN", "VALIDATION", "HUMAN_BLIND")
        },
        minimum_source_families=2,
        holdout_source_family="fda",
        minimum_holdout_family_rows=2,
    )

    assert result["status"] == "READY_TO_FREEZE"
    assert result["fully_held_out_blind_source_families"] == ["fda"]
    policy = result["source_holdout_policy"]
    assert policy["selected_source_family"] == "fda"
    assert policy["selected_source_family_rows"] == 2
    assert policy["blind_chronological_core_rows"] == 1
    assert policy["source_family_counts"] == {"fda": 2, "sec": 10}
    assert policy["selection_basis"] == "SOURCE_METADATA_ONLY_PRE_LABELS"
    assert policy["minimum_rows"] == 2
    assert policy["non_holdout_core_is_chronological"] is True
    assert policy["chronological_core_bounds"] == {
        "TRAIN": {
            "min_as_of": "2026-08-03T00:00:00+00:00",
            "max_as_of": "2026-08-08T00:00:00+00:00",
        },
        "VALIDATION": {
            "min_as_of": "2026-08-09T00:00:00+00:00",
            "max_as_of": "2026-08-11T00:00:00+00:00",
        },
        "HUMAN_BLIND": {
            "min_as_of": "2026-08-12T00:00:00+00:00",
            "max_as_of": "2026-08-12T00:00:00+00:00",
        },
    }
    blind_rows = [row for row in result["rows"] if row["split"] == "HUMAN_BLIND"]
    assert {row["source_id"] for row in blind_rows} == {
        "fda_enforcement",
        "sec_current_filings",
    }
