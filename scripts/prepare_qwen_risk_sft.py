#!/usr/bin/env python3
"""Build leakage-safe Qwen SFT files from a frozen dual-human gold dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from app.models.qwen_risk_contract import (
    QWEN_RISK_CONTRACT_VERSION,
    QWEN_RISK_PROMPT_VERSION,
    expected_semantic_payload,
)
from app.models.risk_label_contract import coherent_label


SYSTEM_PROMPT = (
    "你是金融雷达的语义风险分类器。只判断所给文本表达的极性与做空风险重大性，"
    "不判断证据真假，不补充外部事实，不给投资建议。仅输出指定 JSON。"
)
CONTRACT_VERSION = "qwen-risk-sft-dataset-v1"
DEVELOPMENT_SPLITS = frozenset({"TRAIN", "VALIDATION"})
FINALIZABLE_EVIDENCE = frozenset({"PRIMARY_SUPPORTED", "MULTI_SOURCE_SUPPORTED"})


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


def _review_input(content: dict[str, Any]) -> dict[str, Any]:
    passages = []
    for item in content.get("passages") or []:
        if not isinstance(item, dict):
            continue
        passage = " ".join(str(item.get("passage") or "").split())
        if not passage:
            continue
        passages.append(
            {
                "document_type": str(item.get("document_type") or "")[:80],
                "item_section": str(item.get("item_section") or "")[:120],
                "published_at": item.get("published_at"),
                "passage": passage[:6000],
            }
        )
    return {
        "as_of": content.get("as_of"),
        "event_date": content.get("event_date"),
        "headline": " ".join(str(content.get("headline") or "").split())[:500],
        "summary": " ".join(str(content.get("summary") or "").split())[:2000],
        "passages": passages[:5],
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> str:
    path.write_text("".join(_stable_json(row) + "\n" for row in rows), encoding="utf-8")
    return _sha256(path.read_bytes())


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
    evidence_gate_manifest: list[dict[str, Any]] = []
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

        review_input = _review_input(content)
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

        evidence_state = str(row.get("evidence_state") or "")
        if evidence_state not in FINALIZABLE_EVIDENCE:
            evidence_gate_manifest.append(
                {
                    "sample_id": sample_id,
                    "event_id": row.get("event_id"),
                    "content_sha256": content_sha256,
                    "evidence_state": evidence_state,
                    "split": split,
                    "qwen_training_included": False,
                }
            )
            continue

        assistant_payload = expected_semantic_payload(
            str(row.get("materiality") or ""),
            str(row.get("polarity") or ""),
        )
        prepared = {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
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
    validation_path = output_dir / "qwen_risk_sft_validation.jsonl"
    blind_path = output_dir / "qwen_risk_blind_manifest.jsonl"
    gate_path = output_dir / "qwen_risk_evidence_gate_manifest.jsonl"
    outputs = {
        train_path.name: _write_jsonl(train_path, development["TRAIN"]),
        validation_path.name: _write_jsonl(validation_path, development["VALIDATION"]),
        blind_path.name: _write_jsonl(blind_path, blind_manifest),
        gate_path.name: _write_jsonl(gate_path, evidence_gate_manifest),
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
        "validation_rows": len(development["VALIDATION"]),
        "evidence_gate_rows": len(evidence_gate_manifest),
        "human_blind_rows": len(blind_manifest),
        "semantic_label_combinations": {
            "|".join(key): value for key, value in sorted(labels.items())
        },
        "outputs": outputs,
        "strength_scale": ["NONE", "LOW", "HIGH", "UNCLEAR"],
        "strength_is_derived_from_human_axes": True,
        "evidence_state_used_as_model_target": False,
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
