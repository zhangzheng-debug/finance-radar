#!/usr/bin/env python3
"""Freeze an AI-assisted semantic reference set without claiming human gold.

The third adjudicator is intentionally evaluated only against the two sealed,
independent reviewer submissions.  A row enters the reference set only when
the adjudicator's complete ``(materiality, polarity)`` pair matches reviewer A
or reviewer B.  Rows matching neither reviewer are excluded rather than being
turned into synthetic truth.

This script never accepts or reads Qwen predictions.  It is a dataset-freeze
step, not model evaluation and not a production mutation.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.qwen_risk_contract import (  # noqa: E402
    QWEN_RISK_CONTRACT_VERSION,
    expected_semantic_payload,
    normalize_qwen_risk_content,
)
from app.models.risk_label_contract import MATERIALITY, POLARITIES  # noqa: E402
from app.services.human_gold_review import stable_json  # noqa: E402
from scripts.build_qwen_semantic_blind_benchmark import (  # noqa: E402
    _owner_inputs,
    _read_object,
    _resolve_commit,
    _sample_ids_from_v3,
    _sha256_bytes,
    _validate_owner_sample,
    _validated_reviews,
)
from scripts.prepare_qwen_semantic_consensus_sft import (  # noqa: E402
    EXPERIMENT_PROMPT_VERSION,
    EXPERIMENT_SYSTEM_PROMPT,
)


CONTRACT_VERSION = "qwen-semantic-ai-assisted-reference-v1"
REFERENCE_STATUS = "AI_ASSISTED_REFERENCE_NOT_HUMAN_GOLD"
DATASET_NAME = "qwen_semantic_ai_assisted_reference.jsonl"
MANIFEST_NAME = "manifest.json"


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], bytes]:
    raw = path.read_bytes()
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"third adjudication row must be an object: {path}:{line_number}")
        rows.append(value)
    if not rows:
        raise ValueError("third adjudication JSONL is empty")
    return rows, raw


def _third_adjudications(
    rows: list[dict[str, Any]],
    *,
    expected_content_hashes: dict[str, str],
) -> dict[str, dict[str, Any]]:
    mapped: dict[str, dict[str, Any]] = {}
    required = {
        "sample_id",
        "materiality",
        "polarity",
        "rationale",
        "model",
        "input_sha256",
    }
    for index, row in enumerate(rows, 1):
        prohibited = sorted(
            set(row)
            & {
                "qwen_prediction",
                "qwen_predictions",
                "model_prediction",
                "candidate_prediction",
                "market_outcome",
            }
        )
        if prohibited:
            raise ValueError(
                f"third adjudication row {index} contains prohibited fields: "
                + ",".join(prohibited)
            )
        missing = sorted(required - set(row))
        if missing:
            raise ValueError(
                f"third adjudication row {index} missing fields: {','.join(missing)}"
            )
        sample_id = str(row.get("sample_id") or "").strip()
        if not sample_id or sample_id in mapped:
            raise ValueError(f"third adjudication has blank or duplicate sample_id at row {index}")
        if sample_id not in expected_content_hashes:
            raise ValueError(f"third adjudication contains unexpected sample_id: {sample_id}")
        materiality = str(row.get("materiality") or "").strip().upper()
        polarity = str(row.get("polarity") or "").strip().upper()
        if materiality not in MATERIALITY:
            raise ValueError(f"third adjudication has invalid materiality: {sample_id}")
        if polarity not in POLARITIES:
            raise ValueError(f"third adjudication has invalid polarity: {sample_id}")
        rationale = " ".join(str(row.get("rationale") or "").split())
        if len(rationale) < 20:
            raise ValueError(f"third adjudication rationale is too short: {sample_id}")
        model = str(row.get("model") or "").strip()
        if not model:
            raise ValueError(f"third adjudication model is blank: {sample_id}")
        input_sha256 = str(row.get("input_sha256") or "").strip().lower()
        if input_sha256 != expected_content_hashes[sample_id]:
            raise ValueError(f"third adjudication input_sha256 mismatch: {sample_id}")
        mapped[sample_id] = {
            "sample_id": sample_id,
            "materiality": materiality,
            "polarity": polarity,
            "rationale": rationale,
            "model": model,
            "input_sha256": input_sha256,
        }
    if set(mapped) != set(expected_content_hashes):
        missing = sorted(set(expected_content_hashes) - set(mapped))
        raise ValueError(
            "third adjudication does not cover the frozen remaining pool exactly: "
            + missing[0]
        )
    return mapped


def _reference_row(
    *,
    sample: dict[str, Any],
    target_pair: tuple[str, str],
    provenance: str,
    matched_reviewer_slots: list[str],
    adjudicator: dict[str, Any],
) -> dict[str, Any]:
    content = normalize_qwen_risk_content(sample["content"])
    expected = expected_semantic_payload(*target_pair)
    return {
        "messages": [
            {"role": "system", "content": EXPERIMENT_SYSTEM_PROMPT},
            {"role": "user", "content": stable_json(content)},
            {"role": "assistant", "content": stable_json(expected)},
        ],
        "expected": expected,
        "metadata": {
            "sample_id": sample["sample_id"],
            "event_id": sample.get("event_id"),
            "entity_group": sample.get("entity_group"),
            "event_chain_group": sample.get("event_chain_group"),
            "content_sha256": adjudicator["input_sha256"],
            "split": "AI_ASSISTED_BLIND_REFERENCE",
            "reference_status": REFERENCE_STATUS,
            "label_provenance": provenance,
            "matched_reviewer_slots": matched_reviewer_slots,
            "third_adjudicator_model": adjudicator["model"],
            "human_gold_claimed": False,
            "qwen_prediction_included": False,
            "evidence_state_used_as_model_target": False,
            "post_event_market_data_included": False,
        },
    }


def _write_atomic(output_dir: Path, files: dict[str, bytes]) -> None:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.with_name(f".{output_dir.name}.{uuid.uuid4().hex}.tmp")
    staging.mkdir(exist_ok=False)
    try:
        for name, raw in files.items():
            path = staging / name
            path.write_bytes(raw)
            if os.name != "nt":
                path.chmod(0o600)
        if output_dir.exists():
            raise FileExistsError(f"output directory appeared during write: {output_dir}")
        staging.rename(output_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def freeze(
    *,
    owner_package: Path,
    review_a: Path,
    review_b: Path,
    v3_train: Path,
    v3_validation: Path,
    v3_owner_holdout: Path,
    third_adjudication: Path,
    output_dir: Path,
    candidate_commit: str | None = None,
) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    input_paths = (
        owner_package,
        review_a,
        review_b,
        v3_train,
        v3_validation,
        v3_owner_holdout,
        third_adjudication,
    )
    missing = [str(path) for path in input_paths if not path.is_file()]
    if missing:
        raise ValueError("required input is missing: " + ", ".join(missing))

    commit = _resolve_commit(candidate_commit)
    owner, assignment_a, assignment_b = _owner_inputs(owner_package)
    submission_a, review_a_raw = _read_object(review_a)
    submission_b, review_b_raw = _read_object(review_b)
    reviews = _validated_reviews(
        owner, assignment_a, assignment_b, submission_a, submission_b
    )

    split_specs = (
        ("train", v3_train, "TRAIN"),
        ("validation", v3_validation, "VALIDATION"),
        ("owner_holdout", v3_owner_holdout, "OWNER_HOLDOUT"),
    )
    excluded: set[str] = set()
    split_hashes: dict[str, str] = {}
    split_counts: dict[str, int] = {}
    for name, path, split in split_specs:
        sample_ids, digest = _sample_ids_from_v3(path, split)
        overlap = excluded & sample_ids
        if overlap:
            raise ValueError(f"v3 splits overlap at sample_id {sorted(overlap)[0]}")
        excluded.update(sample_ids)
        split_hashes[name] = digest
        split_counts[name] = len(sample_ids)

    owner_samples = {
        str(sample["sample_id"]): sample
        for sample in owner.get("samples") or []
        if isinstance(sample, dict) and sample.get("sample_id")
    }
    if len(owner_samples) != len(owner.get("samples") or []):
        raise ValueError("owner sample_id values must be unique and nonblank")
    if excluded - set(owner_samples):
        raise ValueError("v3 split contains sample_id outside owner package")
    remaining_ids = sorted(set(owner_samples) - excluded)
    if not remaining_ids:
        raise ValueError("no unused owner samples remain for AI-assisted reference")

    content_hashes: dict[str, str] = {}
    for sample_id in remaining_ids:
        sample = owner_samples[sample_id]
        _validate_owner_sample(sample)
        normalized = normalize_qwen_risk_content(sample["content"])
        content_hashes[sample_id] = _sha256_bytes(stable_json(normalized).encode("utf-8"))

    third_rows, third_raw = _read_jsonl(third_adjudication)
    adjudications = _third_adjudications(
        third_rows, expected_content_hashes=content_hashes
    )

    accepted: list[dict[str, Any]] = []
    excluded_unmatched: list[str] = []
    consensus_ids: list[str] = []
    consensus_confirmed_ids: list[str] = []
    selected_reviewer_counts: Counter[str] = Counter()
    model_counts: Counter[str] = Counter()
    for sample_id in remaining_ids:
        first = reviews["A"][sample_id]
        second = reviews["B"][sample_id]
        adjudicator = adjudications[sample_id]
        pair_a = (str(first["materiality"]), str(first["polarity"]))
        pair_b = (str(second["materiality"]), str(second["polarity"]))
        pair_third = (adjudicator["materiality"], adjudicator["polarity"])
        human_consensus = pair_a == pair_b
        if human_consensus:
            consensus_ids.append(sample_id)
        matched_slots = [
            slot
            for slot, pair in (("A", pair_a), ("B", pair_b))
            if pair_third == pair
        ]
        if not matched_slots:
            excluded_unmatched.append(sample_id)
            continue
        if human_consensus:
            consensus_confirmed_ids.append(sample_id)
            provenance = "DUAL_HUMAN_CONSENSUS_PLUS_INDEPENDENT_AI_CONFIRMATION"
        else:
            provenance = "ONE_HUMAN_REVIEWER_PLUS_INDEPENDENT_AI_ADJUDICATION"
        selected_reviewer_counts["+".join(matched_slots)] += 1
        model_counts[adjudicator["model"]] += 1
        accepted.append(
            _reference_row(
                sample=owner_samples[sample_id],
                target_pair=pair_third,
                provenance=provenance,
                matched_reviewer_slots=matched_slots,
                adjudicator=adjudicator,
            )
        )

    accepted.sort(key=lambda row: row["metadata"]["sample_id"])
    dataset_bytes = b"".join(
        (stable_json(row) + "\n").encode("utf-8") for row in accepted
    )
    dataset_sha256 = _sha256_bytes(dataset_bytes)
    sidecar_bytes = f"{dataset_sha256}  {DATASET_NAME}\n".encode("ascii")

    pair_counts: Counter[str] = Counter()
    materiality_counts: Counter[str] = Counter()
    polarity_counts: Counter[str] = Counter()
    priority_counts: Counter[str] = Counter()
    for row in accepted:
        expected = row["expected"]
        pair_counts[f"{expected['materiality']}|{expected['polarity']}"] += 1
        materiality_counts[expected["materiality"]] += 1
        polarity_counts[expected["polarity"]] += 1
        priority_counts[expected["semantic_priority"]] += 1
    priority_support = priority_counts["PRIORITY_REVIEW"]
    eligibility_checks = {
        "rows_ge_120": len(accepted) >= 120,
        "priority_support_ge_20": priority_support >= 20,
    }
    owner_raw = owner_package.read_bytes()
    inputs = {
        "owner_package": {
            "filename": owner_package.name,
            "sha256": _sha256_bytes(owner_raw),
        },
        "owner_manifest_sha256": owner["manifest_sha256"],
        "review_a": {
            "filename": review_a.name,
            "sha256": _sha256_bytes(review_a_raw),
        },
        "review_b": {
            "filename": review_b.name,
            "sha256": _sha256_bytes(review_b_raw),
        },
        "v3_splits": {
            name: {
                "filename": path.name,
                "sha256": split_hashes[name],
                "row_count": split_counts[name],
            }
            for name, path, _ in split_specs
        },
        "third_adjudication": {
            "filename": third_adjudication.name,
            "sha256": _sha256_bytes(third_raw),
            "row_count": len(third_rows),
        },
    }
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "contract_version": CONTRACT_VERSION,
        "reference_status": REFERENCE_STATUS,
        "candidate_commit": commit,
        "qwen_contract_version": QWEN_RISK_CONTRACT_VERSION,
        "prompt_version": EXPERIMENT_PROMPT_VERSION,
        "inputs": inputs,
        "owner_sample_count": len(owner_samples),
        "excluded_previously_used_count": len(excluded),
        "remaining_blind_pool_count": len(remaining_ids),
        "third_adjudication_coverage_count": len(adjudications),
        "accepted_reference_count": len(accepted),
        "accepted_reference_coverage_rate": len(accepted) / len(remaining_ids),
        "excluded_third_matches_neither_human_count": len(excluded_unmatched),
        "excluded_third_matches_neither_human_ids_sha256": _sha256_bytes(
            stable_json(excluded_unmatched).encode("utf-8")
        ),
        "human_consensus_audit": {
            "count": len(consensus_ids),
            "third_confirmed_count": len(consensus_confirmed_ids),
            "sample_ids_sha256": _sha256_bytes(
                stable_json(consensus_ids).encode("utf-8")
            ),
            "used_as_blanket_human_gold_claim": False,
        },
        "selected_reviewer_match_counts": dict(sorted(selected_reviewer_counts.items())),
        "third_adjudicator_model_counts": dict(sorted(model_counts.items())),
        "label_distribution": {
            "semantic_pair": dict(sorted(pair_counts.items())),
            "materiality": dict(sorted(materiality_counts.items())),
            "polarity": dict(sorted(polarity_counts.items())),
            "semantic_priority": dict(sorted(priority_counts.items())),
        },
        "priority_support": priority_support,
        "evaluation_eligibility": {
            "checks": eligibility_checks,
            "passed": all(eligibility_checks.values()),
        },
        "dataset": {
            "filename": DATASET_NAME,
            "sha256": dataset_sha256,
            "sidecar": DATASET_NAME + ".sha256",
            "sidecar_sha256": _sha256_bytes(sidecar_bytes),
            "row_count": len(accepted),
        },
        "human_gold_claimed": False,
        "full_dual_human_consensus_claimed": False,
        "ai_assistance_disclosed": True,
        "qwen_predictions_read": False,
        "qwen_predictions_used_for_selection_or_adjudication": False,
        "post_event_market_data_included": False,
        "canonical_event_state_changed": False,
        "production_model_changed": False,
        "production_ledger_changed": False,
        "no_trading": True,
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    manifest_sha256 = _sha256_bytes(manifest_bytes)
    manifest_sidecar = f"{manifest_sha256}  {MANIFEST_NAME}\n".encode("ascii")
    _write_atomic(
        output_dir,
        {
            DATASET_NAME: dataset_bytes,
            DATASET_NAME + ".sha256": sidecar_bytes,
            MANIFEST_NAME: manifest_bytes,
            MANIFEST_NAME + ".sha256": manifest_sidecar,
        },
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-package", type=Path, required=True)
    parser.add_argument("--review-a", type=Path, required=True)
    parser.add_argument("--review-b", type=Path, required=True)
    parser.add_argument("--v3-train", type=Path, required=True)
    parser.add_argument("--v3-validation", type=Path, required=True)
    parser.add_argument("--v3-owner-holdout", type=Path, required=True)
    parser.add_argument("--third-adjudication", type=Path, required=True)
    parser.add_argument("--candidate-commit")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    manifest = freeze(
        owner_package=args.owner_package.resolve(),
        review_a=args.review_a.resolve(),
        review_b=args.review_b.resolve(),
        v3_train=args.v3_train.resolve(),
        v3_validation=args.v3_validation.resolve(),
        v3_owner_holdout=args.v3_owner_holdout.resolve(),
        third_adjudication=args.third_adjudication.resolve(),
        output_dir=args.output_dir,
        candidate_commit=args.candidate_commit,
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
