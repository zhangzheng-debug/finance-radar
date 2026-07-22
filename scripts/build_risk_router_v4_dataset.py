#!/usr/bin/env python3
"""Freeze blind-v3 and build a binary semantic corpus after the v2 architecture failure.

Blind-v2 remains immutable failure evidence. Its substantive rows may now be
used as exposed development data, but blind-v3 excludes every blind-v2 event,
entity, chain and near-duplicate before any v4 model inference exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_ai_risk_router_v3_dataset import (  # noqa: E402
    AI_ADJUDICATOR,
    AI_REVIEWER_TYPE,
    DEFAULT_DB,
    DEFAULT_OVERRIDES,
    adjudicate,
    load_ai_overrides,
    load_rows,
    near_duplicate_key,
    source_balanced_order,
    stable_json,
    strict_evidence_row,
    training_eligible,
    write_jsonl,
)


DEFAULT_EXPOSED_V2 = ROOT / "artifacts" / "risk_router_external_blind_v2.jsonl"
DEFAULT_EXPOSED_V2_REPORT = ROOT / "artifacts" / "risk_router_external_blind_v2_report.json"
DEFAULT_DEV = ROOT / "artifacts" / "risk_router_v4_semantic_dev.jsonl"
DEFAULT_BLIND = ROOT / "artifacts" / "risk_router_external_blind_v3.jsonl"
DEFAULT_FREEZE = ROOT / "artifacts" / "risk_router_external_blind_v3_freeze.json"
DEFAULT_REPORT = ROOT / "artifacts" / "risk_router_v4_dataset_audit.json"
DEFAULT_MARKDOWN = ROOT / "artifacts" / "risk_router_v4_dataset_audit.md"

BLIND_TARGETS = {"RISK_REVIEW": 30, "NON_TARGET": 30, "ABSTAIN": 20}
BLIND_MIN_EVENT_DATE = "2020-01-01"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def select_new_blind(
    rows: list[dict[str, Any]], exposed_v2: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    exposed_ids = {row["event_id"] for row in exposed_v2}
    exposed_entities = {row["entity_group"] for row in exposed_v2}
    exposed_chains = {row.get("event_chain_group") or "" for row in exposed_v2} - {""}
    exposed_near = {near_duplicate_key(row) for row in exposed_v2}
    eligible = []
    for row in rows:
        if row["event_id"] in exposed_ids:
            continue
        if row["entity_group"] in exposed_entities:
            continue
        if row.get("event_chain_group") and row["event_chain_group"] in exposed_chains:
            continue
        if near_duplicate_key(row) in exposed_near:
            continue
        if row["event_date"] < BLIND_MIN_EVENT_DATE or row["source_group"] == "sharadar_active_research":
            continue
        if row["adjudication_confidence"] < 0.86:
            continue
        if row["label"] in {"RISK_REVIEW", "NON_TARGET"} and strict_evidence_row(row):
            eligible.append(row)
        elif row["label"] == "ABSTAIN" and row["axes"]["evidence_state"] in {
            "DISCOVERY_ONLY", "CONFLICTED", "INSUFFICIENT"
        } and len(row["text"]) >= 20:
            eligible.append(row)

    by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in eligible:
        by_label[row["label"]].append(row)
    blind: list[dict[str, Any]] = []
    used_entities: set[str] = set()
    used_chains: set[str] = set()
    used_near: set[str] = set()
    for label, target in BLIND_TARGETS.items():
        selected: list[dict[str, Any]] = []
        for row in source_balanced_order(by_label[label], f"v3-{label}"):
            duplicate = near_duplicate_key(row)
            if row["entity_group"] in used_entities or duplicate in used_near:
                continue
            if row.get("event_chain_group") and row["event_chain_group"] in used_chains:
                continue
            selected.append(row)
            used_entities.add(row["entity_group"])
            used_near.add(duplicate)
            if row.get("event_chain_group"):
                used_chains.add(row["event_chain_group"])
            if len(selected) == target:
                break
        if len(selected) < target:
            raise RuntimeError(f"insufficient blind-v3 rows for {label}: {len(selected)}/{target}")
        blind.extend(selected)

    blind_ids = {row["event_id"] for row in blind}
    blind_entities = {row["entity_group"] for row in blind}
    blind_chains = {row["event_chain_group"] for row in blind if row.get("event_chain_group")}
    blind_near = {near_duplicate_key(row) for row in blind}
    development = [
        row for row in rows
        if row["label"] in {"RISK_REVIEW", "NON_TARGET"}
        and training_eligible(row)
        and row["event_id"] not in blind_ids
        and row["entity_group"] not in blind_entities
        and (not row.get("event_chain_group") or row["event_chain_group"] not in blind_chains)
        and near_duplicate_key(row) not in blind_near
    ]
    # A near-identical text carrying opposite semantic labels is not a learning
    # example; it is a policy conflict. Exclude the whole text family.
    labels_by_near: dict[str, set[str]] = defaultdict(set)
    for row in development:
        labels_by_near[near_duplicate_key(row)].add(row["label"])
    conflicted_near = {key for key, labels in labels_by_near.items() if len(labels) > 1}
    development = [row for row in development if near_duplicate_key(row) not in conflicted_near]
    return sorted(development, key=lambda row: row["sample_id"]), sorted(blind, key=lambda row: row["sample_id"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES)
    parser.add_argument("--exposed-v2", type=Path, default=DEFAULT_EXPOSED_V2)
    parser.add_argument("--exposed-v2-report", type=Path, default=DEFAULT_EXPOSED_V2_REPORT)
    parser.add_argument("--dev", type=Path, default=DEFAULT_DEV)
    parser.add_argument("--blind", type=Path, default=DEFAULT_BLIND)
    parser.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    args = parser.parse_args()

    v2_report = json.loads(args.exposed_v2_report.read_text(encoding="utf-8"))
    if v2_report.get("gate_pass") is not False or v2_report.get("promotion_decision") != "HOLD_SHADOW":
        raise ValueError("blind-v2 must be preserved as a failed exposed diagnostic before v4 work")
    exposed_v2 = read_jsonl(args.exposed_v2)
    overrides = load_ai_overrides(args.overrides)
    adjudications = [adjudicate(row, overrides) for row in load_rows(args.db)]
    development, blind = select_new_blind(adjudications, exposed_v2)
    write_jsonl(args.dev, development)
    blind_rows = [
        {**dict(row), "expected_label": row["label"], "prediction": None}
        for row in blind
    ]
    for row in blind_rows:
        row.pop("label", None)
    write_jsonl(args.blind, blind_rows)
    dataset_sha256 = hashlib.sha256(args.blind.read_bytes()).hexdigest()
    development_sha256 = hashlib.sha256(args.dev.read_bytes()).hexdigest()
    exposed_v2_ids = {row["event_id"] for row in exposed_v2}
    exposed_v2_substantive_used = sum(row["event_id"] in exposed_v2_ids for row in development)
    overlap = {
        "event_id": len({row["event_id"] for row in development} & {row["event_id"] for row in blind}),
        "entity_group": len({row["entity_group"] for row in development} & {row["entity_group"] for row in blind}),
        "event_chain_group": len(
            {row["event_chain_group"] for row in development if row.get("event_chain_group")}
            & {row["event_chain_group"] for row in blind if row.get("event_chain_group")}
        ),
        "near_duplicate": len({near_duplicate_key(row) for row in development} & {near_duplicate_key(row) for row in blind}),
        "exposed_v2_event": len(exposed_v2_ids & {row["event_id"] for row in blind}),
        "exposed_v2_entity": len({row["entity_group"] for row in exposed_v2} & {row["entity_group"] for row in blind}),
        "exposed_v2_near_duplicate": len({near_duplicate_key(row) for row in exposed_v2} & {near_duplicate_key(row) for row in blind}),
    }
    if sum(overlap.values()):
        raise RuntimeError(f"blind-v3 leakage detected: {overlap}")
    freeze = {
        "schema_version": 3,
        "freeze_id": f"external-blind-v3-{dataset_sha256[:12]}",
        "frozen_at": utc_now(),
        "architecture": "structured_evidence_gate_plus_binary_semantic_router",
        "dataset_sha256": dataset_sha256,
        "development_dataset_sha256": development_sha256,
        "rows": len(blind_rows),
        "label_counts": dict(Counter(row["expected_label"] for row in blind_rows)),
        "source_counts": dict(Counter(row["source_group"] for row in blind_rows)),
        "blind_min_event_date": BLIND_MIN_EVENT_DATE,
        "adjudicator": AI_ADJUDICATOR,
        "reviewer_type": AI_REVIEWER_TYPE,
        "human_labels_claimed": False,
        "label_policy_locked_before_inference": True,
        "predictions_present": False,
        "predecessor_blind_v2": {
            "freeze_id": v2_report["freeze_id"],
            "dataset_sha256": v2_report["dataset_sha256"],
            "gate_pass": False,
            "role": "EXPOSED_FAILURE_DIAGNOSTIC_NOT_A_BLIND_TEST_ANYMORE",
        },
        "exposed_v2_substantive_rows_used_in_development": exposed_v2_substantive_used,
        "overlap_audit": overlap,
        "no_trading": True,
    }
    args.freeze.write_text(json.dumps(freeze, ensure_ascii=False, indent=2), encoding="utf-8")
    report = {
        "schema_version": 1,
        "created_at": utc_now(),
        "architecture_change": "ABSTAIN moved to deterministic structured evidence gate; semantic model is binary.",
        "development_rows": len(development),
        "development_label_counts": dict(Counter(row["label"] for row in development)),
        "development_source_counts": dict(Counter(row["source_group"] for row in development)),
        "development_dataset_sha256": development_sha256,
        "blind_rows": len(blind_rows),
        "blind_label_counts": freeze["label_counts"],
        "blind_source_counts": freeze["source_counts"],
        "blind_dataset_sha256": dataset_sha256,
        "overlap_audit": overlap,
        "exposed_v2_substantive_rows_used": exposed_v2_substantive_used,
        "quality_checks": {
            "development_binary_only": set(row["label"] for row in development) == {"RISK_REVIEW", "NON_TARGET"},
            "blind_targets_met": freeze["label_counts"] == BLIND_TARGETS,
            "blind_predictions_absent": all(row["prediction"] is None for row in blind_rows),
            "blind_internal_entity_duplicates": len(blind_rows) - len({row["entity_group"] for row in blind_rows}),
            "blind_internal_near_duplicates": len(blind_rows) - len({near_duplicate_key(row) for row in blind_rows}),
            "all_overlap_zero": sum(overlap.values()) == 0,
        },
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.markdown.write_text(
        "\n".join(
            [
                "# Risk Router v4 dataset audit",
                "",
                "- Architecture: structured evidence gate + binary semantic router.",
                "- Blind-v2 is preserved as an exposed failed diagnostic and is not reused as a blind test.",
                f"- Development rows: `{len(development)}` `{dict(Counter(row['label'] for row in development))}`",
                f"- Exposed v2 substantive rows reused in development: `{exposed_v2_substantive_used}`",
                f"- Frozen blind-v3: `{freeze['freeze_id']}`; rows `{len(blind_rows)}`; labels `{freeze['label_counts']}`",
                f"- Blind-v3 source groups: `{len(freeze['source_counts'])}`",
                f"- All leakage counts: `{overlap}`",
                "- Labels are auditable AI rubric adjudications, explicitly not human labels.",
                "- Mode remains SHADOW / NO TRADING.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
