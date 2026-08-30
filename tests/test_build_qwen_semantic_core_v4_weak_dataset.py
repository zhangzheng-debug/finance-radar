from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.models.qwen_risk_contract import expected_semantic_payload
from app.models.qwen_weak_supervision_contract import (
    QWEN_WEAK_MODEL_OUTPUT_CONTRACT,
    QWEN_WEAK_PROMPT_SHA256,
    QWEN_WEAK_PROMPT_VERSION,
    QWEN_WEAK_SYSTEM_PROMPT,
)
from scripts import build_qwen_semantic_core_v4_weak_dataset as builder
from scripts.build_qwen_semantic_core_v4_weak_dataset import build_dataset, stable_json
from scripts.qwen_supervision_leakage_guard import (
    post_event_supervision_reasons,
)


def _row(sample: str, event: str, entity: str, chain: str, materiality: str, polarity: str) -> dict:
    content = {
        "as_of": "2026-01-01T00:00:00Z",
        "event_date": "2026-01-01",
        "headline": f"headline {sample}",
        "summary": f"summary {sample}",
        "passages": [],
    }
    target = expected_semantic_payload(materiality, polarity)
    return {
        "messages": [
            {"role": "system", "content": "old"},
            {"role": "user", "content": stable_json(content)},
            {"role": "assistant", "content": stable_json(target)},
        ],
        "metadata": {
            "sample_id": sample, "event_id": event, "entity_group": entity,
            "event_chain_group": chain,
        },
    }


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(stable_json(row) + "\n" for row in rows), encoding="utf-8")


def _frozen_registry_row(row: dict, split: str) -> dict:
    content = json.loads(row["messages"][1]["content"])
    content_sha256 = hashlib.sha256(stable_json(content).encode("utf-8")).hexdigest()
    metadata = row["metadata"]
    return {
        "schema_version": 1,
        "sample_id": metadata["sample_id"],
        "event_id": metadata["event_id"],
        "entity_group": metadata["entity_group"],
        "event_chain_group": metadata["event_chain_group"],
        "content_sha256": content_sha256,
        "exposure_split": split,
    }


def test_builder_excludes_strict_entities_and_conflicting_duplicates(tmp_path: Path) -> None:
    dual = tmp_path / "dual.jsonl"
    ai = tmp_path / "ai.jsonl"
    weak = tmp_path / "weak.jsonl"
    strict = tmp_path / "strict.jsonl"
    _write(dual, [
        _row("s1", "e1", "issuer:clean", "chain:1", "NOT_MATERIAL_ADVERSE", "NEUTRAL"),
        _row("s2", "e2", "issuer:sealed", "chain:2", "MATERIAL_ADVERSE", "ADVERSE"),
        _row("s3", "e3", "issuer:conflict", "chain:3", "MATERIAL_ADVERSE", "ADVERSE"),
    ])
    _write(ai, [_row("s3", "e3", "issuer:conflict", "chain:3", "NOT_MATERIAL_ADVERSE", "NEUTRAL")])
    _write(weak, [_row("s4", "e4", "issuer:clean2", "chain:4", "NOT_MATERIAL_ADVERSE", "POSITIVE")])
    _write(strict, [{"sample_id": "other", "source_event_id": "other", "entity_group": "issuer:sealed", "event_chain_group": "sealed-chain"}])
    output = tmp_path / "output"
    manifest = build_dataset(
        dual_consensus=[dual], ai_assisted=[ai], deterministic_weak=[weak],
        strict_indices=[strict], output_dir=output,
    )
    assert manifest["candidate_rows"] == 5
    assert manifest["leakage_excluded_rows"] == 1
    assert manifest["conflict_excluded_rows"] == 2
    assert manifest["unique_rows"] == 2
    all_rows = (output / "qwen_core_v4_train_unique.jsonl").read_text() + (output / "qwen_core_v4_dev.jsonl").read_text()
    assert "issuer:sealed" not in all_rows
    assert "issuer:conflict" not in all_rows
    assert "WEAK_SUPERVISION_NOT_HUMAN_GOLD" in all_rows
    prepared = [
        json.loads(line)
        for path in (
            output / "qwen_core_v4_train_unique.jsonl",
            output / "qwen_core_v4_dev.jsonl",
        )
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert all(row["metadata"]["target_contract"] == "core-v1" for row in prepared)
    assert manifest["target_contract"] == "core-v1"


def test_builder_excludes_post_event_market_literals_in_source_text(tmp_path: Path) -> None:
    dual = tmp_path / "dual.jsonl"
    strict = tmp_path / "strict.jsonl"
    leaked = _row(
        "market-leak", "event-leak", "issuer:leak", "chain:leak",
        "MATERIAL_ADVERSE", "ADVERSE",
    )
    leaked_content = json.loads(leaked["messages"][1]["content"])
    leaked_content["headline"] = "CERO volume_crash candidate"
    leaked_content["summary"] = (
        "ret_1d <= -15%; value=ret_1d=-0.946927;volume_ratio=362.157"
    )
    leaked["messages"][1]["content"] = stable_json(leaked_content)

    ordinary = _row(
        "ordinary-price", "event-price", "issuer:price", "chain:price",
        "NOT_MATERIAL_ADVERSE", "NEUTRAL",
    )
    ordinary_content = json.loads(ordinary["messages"][1]["content"])
    ordinary_content["summary"] = "Shares closed at $10 after the filing."
    ordinary["messages"][1]["content"] = stable_json(ordinary_content)

    _write(dual, [leaked, ordinary])
    _write(strict, [])
    output = tmp_path / "output"
    manifest = build_dataset(
        dual_consensus=[dual], ai_assisted=[], deterministic_weak=[],
        strict_indices=[strict], output_dir=output,
    )

    assert manifest["post_event_supervision_excluded_rows"] == 1
    audit = (output / "leakage_exclusions.jsonl").read_text(encoding="utf-8")
    assert "post_event_return_threshold" in audit
    assert "post_event_volume_crash_candidate" in audit
    assert "post_event_volume_ratio" in audit
    outputs = (
        (output / "qwen_core_v4_train_unique.jsonl").read_text(encoding="utf-8")
        + (output / "qwen_core_v4_dev.jsonl").read_text(encoding="utf-8")
    )
    assert '"sample_id":"market-leak"' not in outputs
    assert '"sample_id":"ordinary-price"' in outputs


@pytest.mark.parametrize(
    "field",
    (
        "ret_5m",
        "return_30m",
        "price_change_2h",
        "next_close_return",
        "abnormal_return_1d",
        "relative_return_5d",
    ),
)
def test_recursive_guard_rejects_structured_market_windows(field: str) -> None:
    reasons = post_event_supervision_reasons(
        {"nested": [{"audit": {field: -0.125}}]}
    )

    assert "post_event_structured_metric" in reasons


def test_recursive_guard_preserves_ordinary_source_price_facts() -> None:
    assert post_event_supervision_reasons(
        {
            "headline": "Shares closed at $10 after the filing.",
            "summary": "The filing says revenue was $25 million.",
        }
    ) == []


def test_component_split_keeps_same_entity_together(tmp_path: Path) -> None:
    dual = tmp_path / "dual.jsonl"
    strict = tmp_path / "strict.jsonl"
    _write(dual, [
        _row("s1", "e1", "issuer:same", "chain:1", "NOT_MATERIAL_ADVERSE", "NEUTRAL"),
        _row("s2", "e2", "issuer:same", "chain:2", "NOT_MATERIAL_ADVERSE", "POSITIVE"),
        _row("s3", "e3", "issuer:other", "chain:3", "MATERIAL_ADVERSE", "ADVERSE"),
    ])
    _write(strict, [])
    output = tmp_path / "output"
    build_dataset(
        dual_consensus=[dual], ai_assisted=[], deterministic_weak=[],
        strict_indices=[strict], output_dir=output,
    )
    locations: dict[str, str] = {}
    for split, name in (("TRAIN", "qwen_core_v4_train_unique.jsonl"), ("DEV", "qwen_core_v4_dev.jsonl")):
        for line in (output / name).read_text().splitlines():
            row = json.loads(line)
            if row["metadata"]["entity_group"] == "issuer:same":
                locations[row["metadata"]["sample_id"]] = split
    assert locations["s1"] == locations["s2"]
    assert (output / "manifest.json.sha256").exists()


def test_explicit_canonical_mapping_exclusion_is_audited(tmp_path: Path) -> None:
    dual = tmp_path / "dual.jsonl"
    strict = tmp_path / "strict.jsonl"
    exclusion = tmp_path / "exclusion.json"
    _write(dual, [
        _row("weak-1", "event-1", "legacy-name", "legacy-chain", "MATERIAL_ADVERSE", "ADVERSE"),
        _row("keep", "event-2", "issuer:other", "chain:2", "NOT_MATERIAL_ADVERSE", "NEUTRAL"),
    ])
    _write(strict, [])
    exclusion.write_text(stable_json([{"hardcase_sample_id": "weak-1", "event_id": "event-1"}]), encoding="utf-8")
    output = tmp_path / "output"
    manifest = build_dataset(
        dual_consensus=[dual], ai_assisted=[], deterministic_weak=[], strict_indices=[strict],
        output_dir=output, explicit_exclusions=[exclusion],
    )
    assert manifest["leakage_excluded_rows"] == 1
    assert manifest["explicit_exclusion_counts"] == {"sample_id": 1, "event_id": 1}
    audit = (output / "leakage_exclusions.jsonl").read_text()
    assert "explicit_sample_id" in audit and "explicit_event_id" in audit


def test_quality_exclusion_is_separate_from_leakage(tmp_path: Path) -> None:
    dual = tmp_path / "dual.jsonl"
    strict = tmp_path / "strict.jsonl"
    quality = tmp_path / "quality.json"
    _write(dual, [
        _row("bad-label", "event-1", "issuer:1", "chain:1", "MATERIAL_ADVERSE", "ADVERSE"),
        _row("keep", "event-2", "issuer:2", "chain:2", "NOT_MATERIAL_ADVERSE", "NEUTRAL"),
    ])
    _write(strict, [])
    quality.write_text(stable_json([{"sample_id": "bad-label"}]), encoding="utf-8")
    output = tmp_path / "output"
    manifest = build_dataset(
        dual_consensus=[dual], ai_assisted=[], deterministic_weak=[], strict_indices=[strict],
        output_dir=output, quality_exclusions=[quality],
    )
    assert manifest["leakage_excluded_rows"] == 0
    assert manifest["quality_excluded_rows"] == 1
    assert "rationale_source_sanity_sample_id" in (output / "quality_exclusions.jsonl").read_text()


def test_legacy_hardcase_is_rejoined_to_canonical_entity_before_split(tmp_path: Path) -> None:
    weak = tmp_path / "weak.jsonl"
    strict = tmp_path / "strict.jsonl"
    source_map = tmp_path / "source-map.jsonl"
    pool = tmp_path / "pool.jsonl"
    _write(weak, [_row("weak-1", "event-1", "LEGACY NAME", "legacy", "MATERIAL_ADVERSE", "ADVERSE")])
    _write(strict, [])
    _write(source_map, [{"event_id": "event-1", "sample_id": "canonical-1"}])
    _write(pool, [{"sample_id": "canonical-1", "entity_group": "issuer:hash", "event_chain_group": "chain:hash"}])
    output = tmp_path / "output"
    manifest = build_dataset(
        dual_consensus=[], ai_assisted=[], deterministic_weak=[weak], strict_indices=[strict],
        output_dir=output, canonical_source_map=source_map, canonical_pool=pool,
    )
    assert manifest["canonical_rejoined_rows"] == 1
    all_rows = (output / "qwen_core_v4_train_unique.jsonl").read_text() + (output / "qwen_core_v4_dev.jsonl").read_text()
    assert "issuer:hash" in all_rows
    assert "LEGACY NAME" not in all_rows


def test_provisional_canonical_issuer_is_excluded_from_training(tmp_path: Path) -> None:
    dual = tmp_path / "dual.jsonl"
    strict = tmp_path / "strict.jsonl"
    issuer_map = tmp_path / "issuer-map.jsonl"
    _write(dual, [
        _row("provisional", "event-p", "issuer:p", "chain:p", "NOT_MATERIAL_ADVERSE", "NEUTRAL"),
        _row("strong", "event-s", "issuer:s", "chain:s", "MATERIAL_ADVERSE", "ADVERSE"),
    ])
    _write(strict, [])
    _write(issuer_map, [
        {
            "sample_id": "provisional", "event_id": "event-p",
            "canonical_issuer_key": "issuer:v1:raw_ticker:TEST",
            "resolution_quality": "PROVISIONAL_RAW_TICKER",
        },
        {
            "sample_id": "strong", "event_id": "event-s",
            "canonical_issuer_key": "issuer:v1:sec_cik:0000000001",
            "resolution_quality": "STRONG_CIK",
        },
    ])
    output = tmp_path / "output"
    manifest = build_dataset(
        dual_consensus=[dual], ai_assisted=[], deterministic_weak=[],
        strict_indices=[strict], canonical_issuer_map=issuer_map, output_dir=output,
    )
    assert manifest["canonical_provisional_excluded_rows"] == 1
    audit = (output / "leakage_exclusions.jsonl").read_text(encoding="utf-8")
    assert "canonical_issuer_not_strong" in audit
    outputs = (
        (output / "qwen_core_v4_train_unique.jsonl").read_text(encoding="utf-8")
        + (output / "qwen_core_v4_dev.jsonl").read_text(encoding="utf-8")
    )
    assert '"sample_id":"provisional"' not in outputs
    assert '"sample_id":"strong"' in outputs


def test_policy_disagreement_is_diagnostic_and_row_is_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ai = tmp_path / "ai.jsonl"
    strict = tmp_path / "strict.jsonl"
    row = _row(
        "ai-routine", "event-ai", "issuer:ai", "chain:ai",
        "NOT_MATERIAL_ADVERSE", "NEUTRAL",
    )
    _write(ai, [row])
    _write(strict, [])
    monkeypatch.setattr(builder, "_risk_first_policy", lambda _text: ("RISK_REVIEW", "legacy"))

    output = tmp_path / "output"
    manifest = build_dataset(
        dual_consensus=[], ai_assisted=[ai], deterministic_weak=[],
        strict_indices=[strict], output_dir=output,
    )

    assert manifest["policy_excluded_rows"] == 0
    assert manifest["policy_disagreement_rows"] == 1
    assert (output / "policy_exclusions.jsonl").read_text(encoding="utf-8") == ""
    diagnostic = json.loads(
        (output / "policy_disagreements.jsonl").read_text(encoding="utf-8").strip()
    )
    assert diagnostic["action"] == "RETAINED_DIAGNOSTIC_ONLY"
    assert diagnostic["retained_in_dataset"] is True
    all_rows = (
        (output / "qwen_core_v4_train_unique.jsonl").read_text(encoding="utf-8")
        + (output / "qwen_core_v4_dev.jsonl").read_text(encoding="utf-8")
    )
    assert '"sample_id":"ai-routine"' in all_rows


def test_v11_axes_prompt_provenance_and_effective_counts_are_explicit(tmp_path: Path) -> None:
    dual = tmp_path / "dual.jsonl"
    strict = tmp_path / "strict.jsonl"
    frozen = tmp_path / "frozen.jsonl"
    rows = [
        _row("ma", "e-ma", "issuer:ma", "chain:ma", "MATERIAL_ADVERSE", "ADVERSE"),
        _row("nma-a", "e-a", "issuer:a", "chain:a", "NOT_MATERIAL_ADVERSE", "ADVERSE"),
        _row("nma-m", "e-m", "issuer:m", "chain:m", "NOT_MATERIAL_ADVERSE", "MIXED"),
        _row("nma-p", "e-p", "issuer:p", "chain:p", "NOT_MATERIAL_ADVERSE", "POSITIVE"),
        _row("unclear", "e-u", "issuer:u", "chain:u", "UNCLEAR", "UNCLEAR"),
    ]
    _write(dual, rows)
    _write(strict, [])
    _write(frozen, [_frozen_registry_row(row, "TRAIN") for row in rows])

    output = tmp_path / "output"
    manifest = build_dataset(
        dual_consensus=[dual], ai_assisted=[], deterministic_weak=[],
        strict_indices=[strict], output_dir=output, frozen_split_registry=frozen,
    )

    train_unique = [
        json.loads(line)
        for line in (output / "qwen_core_v4_train_unique.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(train_unique) == 5
    assert manifest["model_output_contract"] == QWEN_WEAK_MODEL_OUTPUT_CONTRACT
    assert manifest["prompt_version"] == QWEN_WEAK_PROMPT_VERSION
    assert manifest["prompt_sha256"] == QWEN_WEAK_PROMPT_SHA256
    assert hashlib.sha256(QWEN_WEAK_SYSTEM_PROMPT.encode("utf-8")).hexdigest() == QWEN_WEAK_PROMPT_SHA256
    for prepared in train_unique:
        assistant = json.loads(prepared["messages"][-1]["content"])
        assert set(assistant) == {"materiality", "polarity"}
        metadata = prepared["metadata"]
        assert metadata["target_contract"] == "core-v1"
        assert metadata["model_output_contract"] == "core-axes-v1"
        assert set(metadata["semantic_target"]) == {
            "materiality", "polarity", "adverse_strength", "semantic_priority",
        }
        assert metadata["prompt_version"] == QWEN_WEAK_PROMPT_VERSION
        assert metadata["prompt_sha256"] == QWEN_WEAK_PROMPT_SHA256

    assert manifest["train_unique_rows"] == 5
    assert manifest["train_effective_rows"] == 12
    assert manifest["train_repeat_rows"] == 7
    assert manifest["train_pair_counts_unique"]["UNCLEAR|UNCLEAR"] == 1
    assert manifest["train_pair_counts_effective"]["UNCLEAR|UNCLEAR"] == 1
    assert manifest["training_file_roles"]["train_unique"].startswith("UNIQUE_")
    assert manifest["training_file_roles"]["train_balanced"].startswith("EFFECTIVE_")
    assert manifest["frozen_split_registry"]["moved_rows"] == 0


def test_pair_multiplier_override_is_frozen_and_unclear_cannot_repeat(tmp_path: Path) -> None:
    dual = tmp_path / "dual.jsonl"
    strict = tmp_path / "strict.jsonl"
    frozen = tmp_path / "frozen.jsonl"
    row = _row("ma", "event-ma", "issuer:ma", "chain:ma", "MATERIAL_ADVERSE", "ADVERSE")
    _write(dual, [row])
    _write(strict, [])
    _write(frozen, [_frozen_registry_row(row, "TRAIN")])

    manifest = build_dataset(
        dual_consensus=[dual], ai_assisted=[], deterministic_weak=[],
        strict_indices=[strict], output_dir=tmp_path / "output",
        frozen_split_registry=frozen,
        pair_multipliers={("MATERIAL_ADVERSE", "ADVERSE"): 3},
    )
    assert manifest["pair_multipliers"] == {"MATERIAL_ADVERSE|ADVERSE": 3}
    assert manifest["train_unique_rows"] == 1
    assert manifest["train_effective_rows"] == 3
    assert len(manifest["pair_multiplier_sha256"]) == 64

    with pytest.raises(ValueError, match=r"UNCLEAR\|UNCLEAR must not be oversampled"):
        build_dataset(
            dual_consensus=[dual], ai_assisted=[], deterministic_weak=[],
            strict_indices=[strict], output_dir=tmp_path / "invalid",
            pair_multipliers={("UNCLEAR", "UNCLEAR"): 2},
        )


def test_frozen_exposure_registry_preserves_existing_dev_assignments(tmp_path: Path) -> None:
    dual = tmp_path / "dual.jsonl"
    strict = tmp_path / "strict.jsonl"
    frozen = tmp_path / "frozen.jsonl"
    dev_row = _row("old-dev", "event-dev", "issuer:dev", "chain:dev", "NOT_MATERIAL_ADVERSE", "NEUTRAL")
    train_row = _row("old-train", "event-train", "issuer:train", "chain:train", "MATERIAL_ADVERSE", "ADVERSE")
    _write(dual, [dev_row, train_row])
    _write(strict, [])
    _write(frozen, [
        _frozen_registry_row(dev_row, "DEV"),
        _frozen_registry_row(train_row, "TRAIN"),
    ])

    output = tmp_path / "output"
    manifest = build_dataset(
        dual_consensus=[dual], ai_assisted=[], deterministic_weak=[],
        strict_indices=[strict], output_dir=output, frozen_split_registry=frozen,
    )
    dev = (output / "qwen_core_v4_dev.jsonl").read_text(encoding="utf-8")
    train = (output / "qwen_core_v4_train_unique.jsonl").read_text(encoding="utf-8")
    assert '"sample_id":"old-dev"' in dev
    assert '"sample_id":"old-dev"' not in train
    assert '"sample_id":"old-train"' in train
    assert manifest["frozen_split_registry"]["split_counts"] == {"DEV": 1, "TRAIN": 1}


def test_frozen_dev_membership_is_exact_and_connected_new_rows_fail_closed(tmp_path: Path) -> None:
    dual = tmp_path / "dual.jsonl"
    strict = tmp_path / "strict.jsonl"
    frozen = tmp_path / "frozen.jsonl"
    issuer_map = tmp_path / "issuer-map.jsonl"
    old_dev = _row(
        "old-dev", "event-dev", "issuer:dev", "chain:dev",
        "NOT_MATERIAL_ADVERSE", "NEUTRAL",
    )
    connected_new = _row(
        "connected-new", "event-new", "issuer:alias", "chain:new",
        "NOT_MATERIAL_ADVERSE", "POSITIVE",
    )
    fresh_new = _row(
        "fresh-new", "event-fresh", "issuer:fresh", "chain:fresh",
        "MATERIAL_ADVERSE", "ADVERSE",
    )
    _write(dual, [old_dev, connected_new, fresh_new])
    _write(strict, [])
    _write(frozen, [_frozen_registry_row(old_dev, "DEV")])
    _write(issuer_map, [
        {
            "sample_id": "old-dev", "event_id": "event-dev",
            "canonical_issuer_key": "issuer:v1:sec_cik:0000000001",
            "resolution_quality": "STRONG_CIK",
        },
        {
            "sample_id": "connected-new", "event_id": "event-new",
            "canonical_issuer_key": "issuer:v1:sec_cik:0000000001",
            "resolution_quality": "STRONG_CIK",
        },
        {
            "sample_id": "fresh-new", "event_id": "event-fresh",
            "canonical_issuer_key": "issuer:v1:sec_cik:0000000002",
            "resolution_quality": "STRONG_CIK",
        },
    ])

    output = tmp_path / "output"
    manifest = build_dataset(
        dual_consensus=[dual], ai_assisted=[], deterministic_weak=[],
        strict_indices=[strict], output_dir=output,
        canonical_issuer_map=issuer_map, frozen_split_registry=frozen,
    )

    dev_rows = [
        json.loads(line)
        for line in (output / "qwen_core_v4_dev.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    train = (output / "qwen_core_v4_train_unique.jsonl").read_text(encoding="utf-8")
    assert {
        (
            row["metadata"]["sample_id"],
            row["metadata"]["event_id"],
            row["metadata"]["content_sha256"],
        )
        for row in dev_rows
    } == {
        (
            frozen_row["sample_id"],
            frozen_row["event_id"],
            frozen_row["content_sha256"],
        )
        for frozen_row in [_frozen_registry_row(old_dev, "DEV")]
    }
    assert '"sample_id":"fresh-new"' in train
    assert '"sample_id":"connected-new"' not in train
    assert manifest["frozen_dev_boundary_excluded_rows"] == 1
    assert manifest["frozen_split_registry"]["dev_membership_exact_match"] is True
    assert manifest["train_dev_canonical_issuer_overlap"] == 0
    boundary = json.loads(
        (output / "frozen_dev_boundary_exclusions.jsonl").read_text(encoding="utf-8").strip()
    )
    assert boundary["sample_id"] == "connected-new"
    assert boundary["action"] == "EXCLUDED_FAIL_CLOSED"
