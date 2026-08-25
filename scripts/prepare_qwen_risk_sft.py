#!/usr/bin/env python3
"""Build leakage-safe Qwen SFT files from a frozen dual-human gold dataset."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

from app.models.qwen_risk_contract import (
    QWEN_RISK_CONTRACT_VERSION,
    QWEN_RISK_PROMPT_VERSION,
    QWEN_RISK_SYSTEM_PROMPT,
    expected_semantic_payload,
    normalize_qwen_risk_content,
)
from app.models.risk_label_contract import coherent_label


CONTRACT_VERSION = "qwen-risk-sft-dataset-v2"
DEVELOPMENT_SPLITS = frozenset({"TRAIN", "VALIDATION"})
TRAIN_PRIORITY_TARGET_FRACTION = 0.25
TRAIN_MAX_OCCURRENCES_PER_SAMPLE = 4


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sidecar_digest(dataset: Path) -> str:
    sidecar = dataset.with_suffix(dataset.suffix + ".sha256")
    if not sidecar.is_file():
        raise ValueError(f"frozen dataset SHA-256 sidecar is required: {sidecar}")
    parts = sidecar.read_text(encoding="ascii").strip().split()
    if len(parts) != 2 or parts[1] != dataset.name or len(parts[0]) != 64:
        raise ValueError("frozen dataset SHA-256 sidecar is invalid")
    return parts[0].lower()


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> str:
    path.write_text("".join(_stable_json(row) + "\n" for row in rows), encoding="utf-8")
    return _sha256(path.read_bytes())


def _semantic_priority(row: dict[str, Any]) -> str:
    payload = json.loads(str(row["messages"][-1]["content"]))
    return str(payload.get("semantic_priority") or "")


def _build_priority_balanced_train(
    rows: list[dict[str, Any]],
    *,
    target_fraction: float = TRAIN_PRIORITY_TARGET_FRACTION,
    max_occurrences_per_sample: int = TRAIN_MAX_OCCURRENCES_PER_SAMPLE,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return a deterministic TRAIN-only resample and its complete audit.

    The original one-row-per-sample dataset remains a separate output.  Only
    PRIORITY_REVIEW examples may be repeated, repeats are capped, and the
    validation and sealed blind sets never enter this function.  This keeps the
    natural evaluation distribution intact while reducing majority collapse in
    a small generative SFT dataset.
    """

    if not 0 < float(target_fraction) < 1:
        raise ValueError("priority target fraction must be between zero and one")
    if int(max_occurrences_per_sample) < 1:
        raise ValueError("max occurrences per sample must be positive")

    unique_count = len(rows)
    priority_rows = [row for row in rows if _semantic_priority(row) == "PRIORITY_REVIEW"]
    priority_count = len(priority_rows)
    required_extras = 0
    if unique_count and priority_count:
        required_extras = max(
            0,
            math.ceil(
                (float(target_fraction) * unique_count - priority_count)
                / (1.0 - float(target_fraction))
            ),
        )
    capacity = priority_count * (int(max_occurrences_per_sample) - 1)
    extra_count = min(required_extras, capacity)

    balanced: list[dict[str, Any]] = []
    for row in rows:
        item = copy.deepcopy(row)
        sample_id = str(item["metadata"]["sample_id"])
        item["metadata"].update(
            {
                "origin_sample_id": sample_id,
                "training_instance_id": sample_id,
                "oversample_repeat_index": 0,
                "oversampled": False,
            }
        )
        balanced.append(item)

    priority_rows = sorted(
        priority_rows,
        key=lambda row: (
            str(row["metadata"].get("content_sha256") or ""),
            str(row["metadata"].get("sample_id") or ""),
        ),
    )
    for index in range(extra_count):
        origin = priority_rows[index % priority_count]
        repeat_index = 1 + index // priority_count
        item = copy.deepcopy(origin)
        sample_id = str(item["metadata"]["sample_id"])
        item["metadata"].update(
            {
                "origin_sample_id": sample_id,
                "training_instance_id": f"{sample_id}#priority-repeat-{repeat_index}",
                "oversample_repeat_index": repeat_index,
                "oversampled": True,
            }
        )
        balanced.append(item)

    effective_priority = priority_count + extra_count
    effective_count = len(balanced)
    achieved_fraction = effective_priority / effective_count if effective_count else 0.0
    return balanced, {
        "policy": "TRAIN_ONLY_PRIORITY_REVIEW_CAPPED_REPEAT_V1",
        "selection_uses_human_semantic_target": True,
        "validation_resampled": False,
        "human_blind_resampled": False,
        "unique_train_rows": unique_count,
        "unique_priority_review_rows": priority_count,
        "effective_train_rows": effective_count,
        "effective_priority_review_rows": effective_priority,
        "oversampled_rows": extra_count,
        "target_priority_fraction": float(target_fraction),
        "achieved_priority_fraction": achieved_fraction,
        "max_occurrences_per_sample": int(max_occurrences_per_sample),
        "target_met": achieved_fraction >= float(target_fraction),
    }


def prepare(frozen_dataset: Path, output_dir: Path) -> dict[str, Any]:
    raw = frozen_dataset.read_bytes()
    expected_digest = _sidecar_digest(frozen_dataset)
    actual_digest = _sha256(raw)
    if actual_digest != expected_digest:
        raise ValueError("frozen dataset SHA-256 mismatch")
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    if not rows:
        raise ValueError("frozen dataset is empty")

    development: dict[str, list[dict[str, Any]]] = {"TRAIN": [], "VALIDATION": []}
    blind_manifest: list[dict[str, Any]] = []
    evidence_posture_audit: list[dict[str, Any]] = []
    seen: set[str] = set()
    groups: dict[str, dict[str, set[str]]] = {
        split: {field: set() for field in ("event_id", "entity_group", "event_chain_group", "content_sha256")}
        for split in DEVELOPMENT_SPLITS
    }

    for index, row in enumerate(rows):
        sample_id = str(row.get("sample_id") or "")
        if not sample_id or sample_id in seen:
            raise ValueError(f"invalid or duplicate sample_id at row {index}")
        seen.add(sample_id)
        split = str(row.get("split") or "")
        if split not in DEVELOPMENT_SPLITS | {"HUMAN_BLIND"}:
            raise ValueError(f"invalid frozen split for {sample_id}")
        content = row.get("content")
        if not isinstance(content, dict):
            raise ValueError(f"missing frozen content for {sample_id}")
        if (
            content.get("post_event_market_data_included") is not False
            or content.get("model_output_included") is not False
            or content.get("target_label_hidden") is not True
        ):
            raise ValueError(f"review boundary violation for {sample_id}")
        label = str(row.get("label") or "")
        if label != coherent_label(
            str(row.get("materiality") or ""),
            str(row.get("polarity") or ""),
            str(row.get("evidence_state") or ""),
        ):
            raise ValueError(f"label/axis mismatch for {sample_id}")

        review_input = normalize_qwen_risk_content(content)
        content_sha256 = _sha256(_stable_json(review_input).encode("utf-8"))
        if split == "HUMAN_BLIND":
            blind_manifest.append(
                {
                    "sample_id": sample_id,
                    "event_id": row.get("event_id"),
                    "text_sha256": row.get("text_sha256"),
                    "content_sha256": content_sha256,
                    "split": "HUMAN_BLIND",
                }
            )
            continue

        # Evidence posture is an independent deterministic axis.  It is useful
        # for auditing the 720-row corpus, but excluding DISCOVERY_ONLY or
        # INSUFFICIENT rows would remove exactly the source-only cases where the
        # semantic model is needed.  Keep posture out of messages and targets,
        # and record it only in a separate non-training audit file.
        evidence_posture_audit.append(
            {
                "sample_id": sample_id,
                "event_id": row.get("event_id"),
                "content_sha256": content_sha256,
                "evidence_state": str(row.get("evidence_state") or ""),
                "split": split,
                "qwen_training_included": True,
                "evidence_state_exposed_to_model": False,
            }
        )

        assistant_payload = expected_semantic_payload(
            str(row.get("materiality") or ""),
            str(row.get("polarity") or ""),
        )
        prepared = {
            "messages": [
                {"role": "system", "content": QWEN_RISK_SYSTEM_PROMPT},
                {"role": "user", "content": _stable_json(review_input)},
                {"role": "assistant", "content": _stable_json(assistant_payload)},
            ],
            "metadata": {
                "sample_id": sample_id,
                "event_id": row.get("event_id"),
                "entity_group": row.get("entity_group"),
                "event_chain_group": row.get("event_chain_group"),
                "content_sha256": content_sha256,
                "split": split,
                "label_provenance": "INDEPENDENT_DUAL_HUMAN_OR_ARBITRATED",
                "evidence_state_used_as_model_target": False,
                "post_event_market_data_included": False,
                "model_output_included_in_review": False,
            },
        }
        development[split].append(prepared)
        for field in groups[split]:
            groups[split][field].add(str(prepared["metadata"].get(field) or ""))

    for field in groups["TRAIN"]:
        overlap = (groups["TRAIN"][field] - {""}) & (groups["VALIDATION"][field] - {""})
        if overlap:
            raise ValueError(f"TRAIN/VALIDATION leakage in {field}")

    output_dir.mkdir(parents=True, exist_ok=True)
    train_path = output_dir / "qwen_risk_sft_train.jsonl"
    balanced_train_path = output_dir / "qwen_risk_sft_train_balanced.jsonl"
    validation_path = output_dir / "qwen_risk_sft_validation.jsonl"
    blind_path = output_dir / "qwen_risk_blind_manifest.jsonl"
    evidence_audit_path = output_dir / "qwen_risk_evidence_posture_audit.jsonl"
    balanced_train, resampling = _build_priority_balanced_train(development["TRAIN"])
    if development["TRAIN"] and not resampling["target_met"]:
        raise ValueError("TRAIN priority-review resampling target is infeasible")
    outputs = {
        train_path.name: _write_jsonl(train_path, development["TRAIN"]),
        balanced_train_path.name: _write_jsonl(balanced_train_path, balanced_train),
        validation_path.name: _write_jsonl(validation_path, development["VALIDATION"]),
        blind_path.name: _write_jsonl(blind_path, blind_manifest),
        evidence_audit_path.name: _write_jsonl(
            evidence_audit_path, evidence_posture_audit
        ),
    }
    semantic_rows = development["TRAIN"] + development["VALIDATION"]
    labels = Counter()
    for row in semantic_rows:
        payload = json.loads(row["messages"][-1]["content"])
        labels[(payload["materiality"], payload["polarity"], payload["adverse_strength"])] += 1
    manifest = {
        "schema_version": 1,
        "contract_version": CONTRACT_VERSION,
        "semantic_contract_version": QWEN_RISK_CONTRACT_VERSION,
        "prompt_version": QWEN_RISK_PROMPT_VERSION,
        "base_model": "Qwen/Qwen2.5-1.5B-Instruct",
        "frozen_dataset_sha256": actual_digest,
        "input_rows": len(rows),
        "train_rows": len(development["TRAIN"]),
        "train_effective_rows": len(balanced_train),
        "train_oversampled_rows": int(resampling["oversampled_rows"]),
        "validation_rows": len(development["VALIDATION"]),
        "evidence_posture_audit_rows": len(evidence_posture_audit),
        "human_blind_rows": len(blind_manifest),
        "semantic_label_combinations": {
            "|".join(key): value for key, value in sorted(labels.items())
        },
        "train_priority_resampling": resampling,
        "outputs": outputs,
        "strength_scale": ["NONE", "LOW", "HIGH", "UNCLEAR"],
        "strength_is_derived_from_human_axes": True,
        "evidence_state_used_as_model_target": False,
        "evidence_state_exposed_to_model": False,
        "human_blind_labels_exported": False,
        "human_blind_content_exported": False,
        "deepseek_output_included": False,
        "post_event_market_data_included": False,
        "production_model_changed": False,
        "no_trading": True,
    }
    manifest_path = output_dir / "qwen_risk_sft_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen-dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    manifest = prepare(args.frozen_dataset.resolve(), args.output_dir.resolve())
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
