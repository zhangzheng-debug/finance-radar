#!/usr/bin/env python3
"""Convert a frozen dual-human dataset into leakage-safe router inputs.

The model-facing development file contains only TRAIN/VALIDATION rows.  The
HUMAN_BLIND output is a hash-only manifest: labels and content never cross the
training boundary.  This script does not train, evaluate the blind set, promote
a model, mutate events, or read post-event prices.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from app.models.risk_label_contract import coherent_label


CONTRACT_VERSION = "human-gold-router-inputs-v1"


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_sidecar(dataset: Path) -> str:
    sidecar = dataset.with_suffix(dataset.suffix + ".sha256")
    if not sidecar.is_file():
        raise ValueError(f"frozen dataset SHA-256 sidecar is required: {sidecar}")
    parts = sidecar.read_text(encoding="ascii").strip().split()
    if len(parts) != 2 or parts[1] != dataset.name or len(parts[0]) != 64:
        raise ValueError("frozen dataset SHA-256 sidecar is invalid")
    return parts[0].lower()


def _text(content: dict[str, Any]) -> str:
    passages = content.get("passages") or []
    parts = [str(content.get("headline") or ""), str(content.get("summary") or "")]
    parts.extend(
        str(item.get("passage") or "")
        for item in passages
        if isinstance(item, dict)
    )
    return "\n".join(part.strip() for part in parts if part and part.strip())


def prepare(frozen_dataset: Path, output_dir: Path) -> dict[str, Any]:
    raw = frozen_dataset.read_bytes()
    expected = _read_sidecar(frozen_dataset)
    actual = sha256_bytes(raw)
    if actual != expected:
        raise ValueError("frozen dataset SHA-256 mismatch")
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    if not rows:
        raise ValueError("frozen dataset is empty")

    development: list[dict[str, Any]] = []
    abstain_gate: list[dict[str, Any]] = []
    blind_manifest: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        sample_id = str(row.get("sample_id") or "")
        if not sample_id or sample_id in seen:
            raise ValueError(f"invalid or duplicate sample_id at row {index}")
        seen.add(sample_id)
        split = str(row.get("split") or "")
        if split not in {"TRAIN", "VALIDATION", "HUMAN_BLIND"}:
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
        text = _text(content)
        if not text:
            raise ValueError(f"empty content for {sample_id}")
        content_sha256 = sha256_bytes(text.encode("utf-8"))
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
        prepared = {
            "sample_id": sample_id,
            "event_id": row.get("event_id"),
            "text": text,
            "text_sha256": row.get("text_sha256"),
            "content_sha256": content_sha256,
            "entity_group": row.get("entity_group"),
            "event_chain_group": row.get("event_chain_group"),
            "split": split,
            "label": label,
            "axes": {
                "materiality": row.get("materiality"),
                "polarity": row.get("polarity"),
                "evidence_state": row.get("evidence_state"),
            },
            "label_provenance": "INDEPENDENT_DUAL_HUMAN_OR_ARBITRATED",
            "post_event_market_data_included": False,
            "model_output_included_in_review": False,
        }
        if label == "ABSTAIN":
            abstain_gate.append(prepared)
        else:
            development.append(prepared)

    output_dir.mkdir(parents=True, exist_ok=True)
    dev_path = output_dir / "human_gold_router_development.jsonl"
    abstain_path = output_dir / "human_gold_abstain_gate.jsonl"
    blind_path = output_dir / "human_gold_blind_manifest.jsonl"
    for path, values in (
        (dev_path, development),
        (abstain_path, abstain_gate),
        (blind_path, blind_manifest),
    ):
        path.write_text("".join(stable_json(value) + "\n" for value in values), encoding="utf-8")

    manifest = {
        "schema_version": 1,
        "contract_version": CONTRACT_VERSION,
        "frozen_dataset_sha256": actual,
        "input_rows": len(rows),
        "development_rows": len(development),
        "abstain_gate_rows": len(abstain_gate),
        "human_blind_rows": len(blind_manifest),
        "development_labels": dict(sorted(Counter(row["label"] for row in development).items())),
        "development_splits": dict(sorted(Counter(row["split"] for row in development).items())),
        "outputs": {
            path.name: sha256_bytes(path.read_bytes())
            for path in (dev_path, abstain_path, blind_path)
        },
        "human_blind_labels_exported": False,
        "human_blind_content_exported": False,
        "ai_rubric_labels_included": False,
        "post_event_market_data_included": False,
        "production_model_changed": False,
        "no_trading": True,
    }
    manifest_path = output_dir / "human_gold_router_inputs_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
