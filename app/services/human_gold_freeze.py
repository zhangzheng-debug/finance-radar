"""Deterministically freeze returned human-only gold annotations.

This is an artifact-only gate.  It does not import reviews into production,
change canonical events, retrain a model or promote a model.  The split is
strictly chronological after the human labels are finalized, while issuer,
event-chain, exact-text and near-duplicate overlap are required to be zero.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

from app.models.risk_label_contract import LABELS, validate_annotation
from app.services.adjudication import AdjudicationService, normalize_source_family
from app.services.human_gold_review import OFFLINE_GOLD_CONTRACT_VERSION


FREEZE_CONTRACT_VERSION = "human-gold-chronological-freeze-v1"
DEFAULT_SPLIT_SIZES = {"TRAIN": 420, "VALIDATION": 120, "HUMAN_BLIND": 180}
DEFAULT_LABEL_MINIMUMS = {
    "TRAIN": {"RISK_REVIEW": 120, "NON_TARGET": 120, "ABSTAIN": 40},
    "VALIDATION": {"RISK_REVIEW": 30, "NON_TARGET": 30, "ABSTAIN": 10},
    "HUMAN_BLIND": {"RISK_REVIEW": 50, "NON_TARGET": 50, "ABSTAIN": 20},
}


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _as_of(row: dict[str, Any]) -> datetime:
    content = row.get("content")
    if not isinstance(content, dict):
        raise ValueError("annotation content must be an object")
    raw = str(content.get("as_of") or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("annotation content.as_of must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("annotation content.as_of must include a timezone")
    return parsed.astimezone(timezone.utc)


def _content_boundaries(row: dict[str, Any]) -> list[str]:
    content = row.get("content") or {}
    issues: list[str] = []
    if content.get("contract_version") != "human-blind-v3.1":
        issues.append("content contract is not human-blind-v3.1")
    if content.get("post_event_market_data_included") is not False:
        issues.append("post-event market data is present")
    if content.get("model_output_included") is not False:
        issues.append("model output is present")
    if content.get("target_label_hidden") is not True:
        issues.append("target label was not hidden during review")
    if content.get("source_identity_hidden") is not True:
        issues.append("source identity was not hidden during review")
    return issues


def assess_freeze_readiness(
    annotations: Iterable[dict[str, Any]],
    *,
    split_sizes: dict[str, int] | None = None,
    label_minimums: dict[str, dict[str, int]] | None = None,
    minimum_source_families: int = 4,
) -> dict[str, Any]:
    sizes = dict(split_sizes or DEFAULT_SPLIT_SIZES)
    minimums = {
        split: dict(values)
        for split, values in (label_minimums or DEFAULT_LABEL_MINIMUMS).items()
    }
    if tuple(sizes) != ("TRAIN", "VALIDATION", "HUMAN_BLIND"):
        raise ValueError("split_sizes must be ordered TRAIN, VALIDATION, HUMAN_BLIND")
    if any(int(value) < 1 for value in sizes.values()):
        raise ValueError("every split size must be positive")
    rows = [dict(row) for row in annotations]
    issues: list[str] = []
    seen_samples: set[str] = set()
    seen_events: set[str] = set()
    seen_entities: set[str] = set()
    seen_chains: set[str] = set()
    seen_text: set[str] = set()
    signatures: list[tuple[frozenset[str], frozenset[str]]] = []
    for index, row in enumerate(rows):
        prefix = f"annotations[{index}]"
        annotation_issues = validate_annotation(row)
        issues.extend(f"{prefix}:{issue}" for issue in annotation_issues)
        issues.extend(f"{prefix}:{issue}" for issue in _content_boundaries(row))
        for field, seen in (
            ("sample_id", seen_samples),
            ("event_id", seen_events),
            ("entity_group", seen_entities),
            ("event_chain_group", seen_chains),
            ("text_sha256", seen_text),
        ):
            value = str(row.get(field) or "")
            if value in seen:
                issues.append(f"{prefix}:duplicate {field}")
            seen.add(value)
        signature = AdjudicationService._near_duplicate_signature(row)
        if any(AdjudicationService._is_near_duplicate(signature, prior) for prior in signatures):
            issues.append(f"{prefix}:near-duplicate content")
        signatures.append(signature)
        try:
            row["_as_of"] = _as_of(row)
        except ValueError as exc:
            issues.append(f"{prefix}:{exc}")

    expected_count = sum(int(value) for value in sizes.values())
    if len(rows) != expected_count:
        issues.append(f"annotation count must equal {expected_count}, got {len(rows)}")
    rows.sort(
        key=lambda row: (
            row.get("_as_of") or datetime.max.replace(tzinfo=timezone.utc),
            str(row.get("sample_id") or ""),
        )
    )
    cursor = 0
    assigned: list[dict[str, Any]] = []
    split_rows: dict[str, list[dict[str, Any]]] = {}
    for split, size in sizes.items():
        selected = rows[cursor : cursor + int(size)]
        cursor += int(size)
        split_rows[split] = selected
        assigned.extend(
            {
                **{key: value for key, value in row.items() if key != "_as_of"},
                "split": split,
            }
            for row in selected
        )

    label_counts = {
        split: dict(sorted(Counter(row.get("label") for row in selected).items()))
        for split, selected in split_rows.items()
    }
    label_deficits: dict[str, dict[str, int]] = {}
    for split, required in minimums.items():
        actual = label_counts.get(split, {})
        label_deficits[split] = {
            label: max(0, int(count) - int(actual.get(label, 0)))
            for label, count in required.items()
        }
        for label in set(actual) - LABELS:
            issues.append(f"{split}:unsupported label {label}")

    source_families = {
        split: {
            normalize_source_family(row.get("source_id"))
            for row in selected
            if normalize_source_family(row.get("source_id"))
        }
        for split, selected in split_rows.items()
    }
    all_source_families = set().union(*source_families.values()) if source_families else set()
    held_out = source_families.get("HUMAN_BLIND", set()) - (
        source_families.get("TRAIN", set()) | source_families.get("VALIDATION", set())
    )
    if len(all_source_families) < int(minimum_source_families):
        issues.append(
            f"source families must be at least {minimum_source_families}, got {len(all_source_families)}"
        )
    if not held_out:
        issues.append("HUMAN_BLIND has no fully held-out source family")
    for split, deficits in label_deficits.items():
        for label, deficit in deficits.items():
            if deficit:
                issues.append(f"{split}:{label} deficit={deficit}")

    temporal = {
        split: {
            "min_as_of": min((row.get("_as_of") for row in selected), default=None),
            "max_as_of": max((row.get("_as_of") for row in selected), default=None),
        }
        for split, selected in split_rows.items()
    }
    temporal_serialized = {
        split: {
            key: value.isoformat() if value is not None else None
            for key, value in bounds.items()
        }
        for split, bounds in temporal.items()
    }
    ready = not issues
    freeze_payload = {
        "contract_version": FREEZE_CONTRACT_VERSION,
        "source_contract_version": OFFLINE_GOLD_CONTRACT_VERSION,
        "rows": assigned,
    }
    dataset_sha256 = sha256_json(freeze_payload) if ready else None
    return {
        "schema_version": 1,
        "contract_version": FREEZE_CONTRACT_VERSION,
        "status": "READY_TO_FREEZE" if ready else "NOT_READY_TO_FREEZE",
        "issues": issues,
        "annotation_count": len(rows),
        "expected_annotation_count": expected_count,
        "split_sizes": sizes,
        "label_counts": label_counts,
        "label_minimums": minimums,
        "label_deficits": label_deficits,
        "source_families": {split: sorted(values) for split, values in source_families.items()},
        "all_source_family_count": len(all_source_families),
        "fully_held_out_blind_source_families": sorted(held_out),
        "temporal_bounds": temporal_serialized,
        "issuer_overlap_count": len(rows) - len(seen_entities),
        "event_chain_overlap_count": len(rows) - len(seen_chains),
        "exact_text_overlap_count": len(rows) - len(seen_text),
        "near_duplicate_overlap_count": sum("near-duplicate content" in issue for issue in issues),
        "dataset_sha256": dataset_sha256,
        "freeze_id": f"human-gold-{dataset_sha256[:12]}" if dataset_sha256 else None,
        "rows": assigned if ready else [],
        "human_only": True,
        "ai_labels_included": False,
        "post_event_market_data_included": False,
        "canonical_event_state_changed": False,
        "production_model_changed": False,
        "no_trading": True,
    }
