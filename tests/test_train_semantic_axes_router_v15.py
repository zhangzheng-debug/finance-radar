from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from scripts import train_semantic_axes_router_v15 as subject


def _row(
    sample_id: str,
    *,
    split: str,
    materiality: str = "NOT_MATERIAL_ADVERSE",
    polarity: str = "NEUTRAL",
    entity_group: str = "entity-a",
    event_chain_group: str = "chain-a",
    human_gold_claimed: bool = False,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "sample_id": sample_id,
        "event_id": f"event-{sample_id}",
        "entity_group": entity_group,
        "event_chain_group": event_chain_group,
        "content_sha256": f"content-{sample_id}",
        "human_gold_claimed": human_gold_claimed,
        "label_classification": "AI_REVIEW_NOT_HUMAN_GOLD",
        "label_provenance": (
            "INDEPENDENT_AI_REVIEW_CONSENSUS"
            if split == "TRAIN"
            else "DEEPSEEK_ISOLATED_MULTIVIEW_ARBITRATION"
        ),
        "split": split,
        "prompt_version": subject.PROMPT_VERSION,
        "prompt_sha256": subject.PROMPT_SHA256,
        "model_output_contract": subject.MODEL_OUTPUT_CONTRACT,
        "target_contract": subject.TARGET_CONTRACT,
        "evidence_state_used_as_model_target": False,
        "post_event_market_data_included": False,
        "qwen_prediction_included": False,
        "semantic_target": {
            "materiality": materiality,
            "polarity": polarity,
        },
    }
    if split == "TRAIN":
        metadata["overlay_contract_version"] = subject.TRAIN_MANIFEST_CONTRACT
        metadata["overlay_view"] = "UNIQUE_AUDIT"
        metadata["source_payload_binding_verified"] = True
        metadata["quality_exclusion"] = None
        metadata["training_eligibility"] = {"eligible": True}
    return {
        "metadata": metadata,
        "messages": [
            {"role": "system", "content": "classify"},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "headline": f"Company update {sample_id}",
                        "summary": "The company disclosed a concrete business event.",
                        "passages": [],
                    }
                ),
            },
            {
                "role": "assistant",
                "content": json.dumps(
                    {"materiality": materiality, "polarity": polarity}
                ),
            },
        ],
    }


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def _write_manifest(
    path: Path, *, role: str, dataset_sha256: str, row_count: int
) -> str:
    if role == "TRAIN":
        manifest = {
            "contract_version": subject.TRAIN_MANIFEST_CONTRACT,
            "human_gold_claimed": False,
            "label_classification": subject.LABEL_CLASSIFICATION,
            "model_output_contract": subject.MODEL_OUTPUT_CONTRACT,
            "outputs": {"unique_audit": {"sha256": dataset_sha256, "row_count": row_count}},
            "distributions": {"trainable_unique": {"row_count": row_count}},
            "isolation": {
                "dev_metrics_read": False,
                "market_results_read": False,
                "qwen_predictions_read": False,
                "sealed_benchmark_read": False,
            },
        }
    else:
        manifest = {
            "contract_version": subject.DEV_MANIFEST_CONTRACT,
            "dataset_role": "DEV_SELECTION_ONLY",
            "human_gold_claimed": False,
            "label_classification": subject.LABEL_CLASSIFICATION,
            "membership_policy": "UNION_OF_PRE_FROZEN_COMPONENTS_NO_LABEL_FILTERING",
            "output": {"sha256": dataset_sha256, "row_count": row_count},
            "row_count": row_count,
            "zero_cross_component_overlap": {
                "sample_id": True,
                "entity_group": True,
                "event_chain_group": True,
                "source_content_sha256": True,
            },
        }
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return subject.sha256_path(path)


def test_load_rows_enforces_ai_review_provenance(tmp_path: Path) -> None:
    path = tmp_path / "train.jsonl"
    _write(path, [_row("one", split="TRAIN", human_gold_claimed=True)])

    with pytest.raises(ValueError, match="claims human gold"):
        subject.load_rows(path, role="TRAIN")


def test_load_rows_filters_ineligible_train_rows(tmp_path: Path) -> None:
    eligible = _row("eligible", split="TRAIN")
    ineligible = _row("ineligible", split="TRAIN")
    ineligible["metadata"]["training_eligibility"] = {"eligible": False}  # type: ignore[index]
    path = tmp_path / "train.jsonl"
    _write(path, [eligible, ineligible])

    rows = subject.load_rows(path, role="TRAIN")

    assert [row["sample_id"] for row in rows] == ["eligible"]


def test_overlap_audit_blocks_any_identity_leak() -> None:
    train = [
        {
            "sample_id": "train",
            "event_id": "event-shared",
            "entity_group": "entity-train",
            "event_chain_group": "chain-train",
            "content_sha256": "hash-train",
        }
    ]
    dev = [
        {
            "sample_id": "dev",
            "event_id": "event-shared",
            "entity_group": "entity-dev",
            "event_chain_group": "chain-dev",
            "content_sha256": "hash-dev",
        }
    ]

    with pytest.raises(ValueError, match="overlap detected"):
        subject.overlap_audit(train, dev)


def test_connected_groups_keep_entity_and_chain_relations_together() -> None:
    rows = [
        {
            "sample_id": "one",
            "entity_group": "entity-shared",
            "event_chain_group": "chain-one",
        },
        {
            "sample_id": "two",
            "entity_group": "entity-shared",
            "event_chain_group": "chain-two",
        },
        {
            "sample_id": "three",
            "entity_group": "entity-three",
            "event_chain_group": "chain-three",
        },
    ]

    groups = subject.connected_groups(rows)

    assert groups[0] == groups[1]
    assert groups[0] != groups[2]


def test_metrics_cover_pair_accuracy_priority_recall_and_fpr() -> None:
    result = subject.metrics(
        ["MATERIAL_ADVERSE", "MATERIAL_ADVERSE", "NOT_MATERIAL_ADVERSE"],
        ["ADVERSE", "NEUTRAL", "POSITIVE"],
        ["MATERIAL_ADVERSE", "NOT_MATERIAL_ADVERSE", "MATERIAL_ADVERSE"],
        ["ADVERSE", "NEUTRAL", "POSITIVE"],
    )

    assert result["exact_pair_accuracy"] == pytest.approx(1 / 3)
    assert result["priority_recall"] == pytest.approx(0.5)
    assert result["non_priority_false_positive_rate"] == pytest.approx(1.0)
    assert result["confusion"] == {"tp": 1, "fn": 1, "fp": 1, "tn": 0}


def test_classifier_contract_is_fixed() -> None:
    classifier = subject.build_classifier()

    assert classifier.estimator.solver == "liblinear"
    assert classifier.estimator.C == 2.0
    assert classifier.estimator.class_weight == "balanced"
    assert classifier.estimator.random_state == 42


def test_classifier_supports_three_class_one_vs_rest_fit() -> None:
    classifier = subject.build_classifier()
    features = np.asarray(
        [
            [2.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 2.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 2.0],
            [0.0, 0.0, 1.0],
        ]
    )

    classifier.fit(features, ["a", "a", "b", "b", "c", "c"])

    assert classifier.classes_.tolist() == ["a", "b", "c"]
    assert classifier.predict(features).shape == (6,)


def test_manifest_sha_and_dataset_binding_fail_closed(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest_sha = _write_manifest(
        manifest, role="DEV", dataset_sha256="dataset-sha", row_count=2
    )

    with pytest.raises(ValueError, match="manifest SHA256 mismatch"):
        subject.load_manifest(
            manifest,
            role="DEV",
            expected_sha256="0" * 64,
            dataset_sha256="dataset-sha",
        )

    with pytest.raises(ValueError, match="does not bind dataset"):
        subject.load_manifest(
            manifest,
            role="DEV",
            expected_sha256=manifest_sha,
            dataset_sha256="different-sha",
        )


def test_end_to_end_consumed_diagnostic_writes_bound_output_manifest(
    tmp_path: Path,
) -> None:
    materiality = ["MATERIAL_ADVERSE", "NOT_MATERIAL_ADVERSE", "UNCLEAR"]
    polarity = ["ADVERSE", "MIXED", "NEUTRAL", "POSITIVE", "UNCLEAR"]
    train_rows = []
    dev_rows = []
    index = 0
    for repeat in range(3):
        for left in materiality:
            for right in polarity:
                index += 1
                train_rows.append(
                    _row(
                        f"train-{index}",
                        split="TRAIN",
                        materiality=left,
                        polarity=right,
                        entity_group=f"train-entity-{index}",
                        event_chain_group=f"train-chain-{index}",
                    )
                )
    for left in materiality:
        for right in polarity:
            index += 1
            dev_rows.append(
                _row(
                    f"dev-{index}",
                    split="DEV",
                    materiality=left,
                    polarity=right,
                    entity_group=f"dev-entity-{index}",
                    event_chain_group=f"dev-chain-{index}",
                )
            )
    train = tmp_path / "train.jsonl"
    dev = tmp_path / "dev.jsonl"
    _write(train, train_rows)
    _write(dev, dev_rows)
    train_sha = subject.sha256_path(train)
    dev_sha = subject.sha256_path(dev)
    train_manifest = tmp_path / "train-manifest.json"
    dev_manifest = tmp_path / "dev-manifest.json"
    train_manifest_sha = _write_manifest(
        train_manifest, role="TRAIN", dataset_sha256=train_sha, row_count=45
    )
    dev_manifest_sha = _write_manifest(
        dev_manifest, role="DEV", dataset_sha256=dev_sha, row_count=15
    )
    output = tmp_path / "output"

    report = subject.train_and_evaluate(
        train,
        train_manifest,
        dev,
        dev_manifest,
        output,
        expected_train_sha256=train_sha,
        expected_train_manifest_sha256=train_manifest_sha,
        expected_dev_sha256=dev_sha,
        expected_dev_manifest_sha256=dev_manifest_sha,
        evaluation_status="CONSUMED_DIAGNOSTIC_REPRODUCTION",
    )

    assert report["selection_decision"] == "REJECTED_CONSUMED_DEV_DIAGNOSTIC"
    assert report["development_evaluation"]["selection_allowed"] is False
    receipt = json.loads((output / "output_manifest.json").read_text(encoding="utf-8"))
    for filename, metadata in receipt["files"].items():
        assert metadata["sha256"] == subject.sha256_path(output / filename)
        assert metadata["bytes"] == (output / filename).stat().st_size
