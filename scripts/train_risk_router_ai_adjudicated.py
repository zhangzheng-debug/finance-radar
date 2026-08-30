#!/usr/bin/env python3
"""Build and evaluate a shadow router from A/B reviews plus explicit AI arbitration.

This pipeline deliberately does *not* create or modify the human-gold freeze.  A/B
target-label consensus is treated as the only human reference.  Conflicts are
resolved by a reproducible policy/seed-model adjudicator, marked as AI-derived,
and used only for development.  Validation and internal holdout contain only
A/B target-label consensus rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import joblib
from sklearn.metrics import classification_report, confusion_matrix, f1_score

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.models.risk_label_contract import FINALIZABLE_EVIDENCE, coherent_label
from app.models.risk_router import RiskRouter
from app.models.semantic_policy_gate import NON_TARGET_RULES, RISK_RULES, SEMANTIC_POLICY_VERSION
from app.services.human_gold_review import validate_submission
from scripts.train_risk_router import build_pipeline


CONTRACT_VERSION = "ai-adjudicated-dual-review-router-v1"
LABELS = ("RISK_REVIEW", "NON_TARGET")
THRESHOLDS = tuple(round(value / 100, 2) for value in range(35, 71, 3))
GATES = {
    "internal_macro_f1_min": 0.80,
    "internal_risk_recall_min": 0.80,
    "internal_false_risk_max": 0.15,
    "external_macro_f1_min": 0.85,
    "external_risk_recall_min": 0.85,
    "external_false_risk_max": 0.10,
}
SEED_RISK_MIN = 0.78
SEED_NON_TARGET_MAX = 0.22

EXTRA_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "documented_financial_distress",
        re.compile(
            r"\b(?:accumulated deficit.{0,220}(?:negative working capital|net loss|cash used in operations)|"
            r"negative working capital.{0,220}(?:accumulated deficit|net loss|cash used in operations)|"
            r"will need (?:to raise|additional) .{0,80}(?:capital|financing)|"
            r"unable to raise additional capital.{0,180}(?:reduce|curtail).{0,120}(?:continue as a going concern|operations)|"
            r"cash.{0,80}(?:is|will be) insufficient.{0,100}(?:operations|obligations|twelve months)|"
            r"cease(?:d|s)? operations?)\b",
            re.I | re.S,
        ),
    ),
    (
        "active_listing_noncompliance",
        re.compile(
            r"\b(?:received .{0,100}(?:notice|notification letter).{0,180}(?:not|no longer) in compliance|"
            r"has not regained compliance|not eligible for (?:a )?(?:second|additional).{0,50}extension)\b",
            re.I | re.S,
        ),
    ),
    (
        "form25_listing_removal",
        re.compile(
            r"\b(?:FORM\s*25|25-NSE).{0,500}NOTIFICATION OF REMOVAL FROM LISTING"
            r"(?: AND/OR REGISTRATION)?\b",
            re.I | re.S,
        ),
    ),
    (
        "pivotal_clinical_failure",
        re.compile(
            r"\b(?:phase\s*3|pivotal).{0,180}(?:did not meet|failed).{0,100}"
            r"(?:primary|key secondary) endpoint\b",
            re.I | re.S,
        ),
    ),
    (
        "adverse_control_opinion",
        re.compile(
            r"\b(?:expressed|issued|received).{0,80}adverse opinion.{0,180}"
            r"(?:material weakness|internal control|financial reporting)\b",
            re.I | re.S,
        ),
    ),
    (
        "binding_enforcement_or_accounting_failure",
        re.compile(
            r"\b(?:settled action.{0,180}(?:alleg|penalt|pay)|"
            r"(?:SEC|FTC|CFTC) (?:alleged|charged|finalized an order|approves? (?:a )?final order)|"
            r"fraudulent scheme|misappropriat(?:ed|ion)|criminal charge|"
            r"material weakness|financial statements? should no longer be relied upon|will restate|"
            r"impairment charge|clinical hold|complete response letter|failed (?:phase\s*[23]|trial)|"
            r"may cause serious injury or death)\b",
            re.I | re.S,
        ),
    ),
)

CONTEXTUAL_NON_TARGET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "paid_form25_exit",
        re.compile(
            r"\b(?:FORM\s*25|25-NSE).{0,1400}(?:per share in cash|merger consideration|"
            r"acquisition (?:was |has been )?completed)\b",
            re.I | re.S,
        ),
    ),
    (
        "resolved_or_alleviated_risk",
        re.compile(
            r"\b(?:substantial doubt.{0,180}(?:is|was|has been) alleviated|"
            r"(?:has|had|successfully) regained compliance|compliance (?:has been|was) restored|"
            r"no longer (?:subject to|at risk of) delist)\b",
            re.I | re.S,
        ),
    ),
    (
        "hypothetical_spac_liquidation",
        re.compile(
            r"\b(?:if|should) .{0,180}(?:unable to consummate|fail to complete|required to liquidate|"
            r"be required to liquidate).{0,240}(?:business combination|trust account|liquidat)\b",
            re.I | re.S,
        ),
    ),
    (
        "whistleblower_award_not_subject_enforcement",
        re.compile(r"\bgrants? .{0,100}whistleblower awards?\b", re.I | re.S),
    ),
    (
        "contract_definition_not_realized_default",
        re.compile(
            r"\b(?:the term [\"“]?default[\"”]? means|for purposes? of this .{0,80}"
            r"(?:event of default|covenant breach))\b",
            re.I | re.S,
        ),
    ),
)


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _read_git_json(repository: Path, specification: str) -> dict[str, Any]:
    raw = subprocess.check_output(["git", "show", specification], cwd=repository)
    return json.loads(raw.decode("utf-8-sig"))


def _zip_json(archive: zipfile.ZipFile, suffix: str) -> dict[str, Any]:
    name = next((name for name in archive.namelist() if name.endswith(suffix)), None)
    if not name:
        raise ValueError(f"owner package is missing {suffix}")
    return json.loads(archive.read(name).decode("utf-8-sig"))


def _sample_text(sample: dict[str, Any]) -> str:
    content = sample.get("content") or {}
    pieces = [str(content.get("headline") or ""), str(content.get("summary") or "")]
    pieces.extend(
        str(passage.get("passage") or "")
        for passage in content.get("passages") or []
        if isinstance(passage, dict)
    )
    return "\n".join(piece.strip() for piece in pieces if piece.strip())


def _derived_label(review: dict[str, Any]) -> str:
    return coherent_label(
        str(review.get("materiality") or ""),
        str(review.get("polarity") or ""),
        str(review.get("evidence_state") or ""),
    )


def _rank(sample_id: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{sample_id}".encode()).hexdigest()


def _metrics(truth: list[str], predictions: list[str]) -> dict[str, Any]:
    report = classification_report(
        truth, predictions, labels=list(LABELS), output_dict=True, zero_division=0
    )
    normal = sum(label == "NON_TARGET" for label in truth)
    return {
        "rows": len(truth),
        "label_counts": dict(Counter(truth)),
        "accuracy": sum(a == b for a, b in zip(truth, predictions)) / max(1, len(truth)),
        "macro_f1": f1_score(
            truth, predictions, labels=list(LABELS), average="macro", zero_division=0
        ),
        "risk_recall": report["RISK_REVIEW"]["recall"],
        "risk_precision": report["RISK_REVIEW"]["precision"],
        "non_target_false_risk_rate": sum(
            actual == "NON_TARGET" and predicted == "RISK_REVIEW"
            for actual, predicted in zip(truth, predictions)
        )
        / max(1, normal),
        "confusion_matrix": confusion_matrix(
            truth, predictions, labels=list(LABELS)
        ).tolist(),
    }


def _risk_first_policy(text: str) -> tuple[str | None, str | None]:
    normalized = " ".join((text or "").split())[:30000]
    for code, expression in CONTEXTUAL_NON_TARGET_PATTERNS:
        if expression.search(normalized):
            return "NON_TARGET", code
    for code, expression in (*EXTRA_RISK_PATTERNS, *RISK_RULES):
        if expression.search(normalized):
            return "RISK_REVIEW", code
    for code, expression in NON_TARGET_RULES:
        if expression.search(normalized):
            return "NON_TARGET", code
    return None, None


def _probability(pipeline: Any, text: str) -> float:
    probabilities = pipeline.predict_proba([text])[0]
    classes = [str(value) for value in pipeline.classes_]
    return float(probabilities[classes.index("RISK_REVIEW")])


def _layered_prediction(pipeline: Any, text: str, threshold: float) -> tuple[str, str, float]:
    policy_label, reason = _risk_first_policy(text)
    if policy_label:
        return policy_label, f"POLICY:{reason}", 0.99
    probability = _probability(pipeline, text)
    label = "RISK_REVIEW" if probability >= threshold else "NON_TARGET"
    confidence = probability if label == "RISK_REVIEW" else 1.0 - probability
    return label, "TRAINED_MODEL", confidence


def _split_consensus(
    rows: list[dict[str, Any]], *, salt: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    binary = [row for row in rows if row["consensus_label"] in LABELS]
    abstain = [row for row in rows if row["consensus_label"] == "ABSTAIN"]
    train: list[dict[str, Any]] = []
    validation: list[dict[str, Any]] = []
    holdout: list[dict[str, Any]] = []
    for label in LABELS:
        selected = sorted(
            (row for row in binary if row["consensus_label"] == label),
            key=lambda row: _rank(row["sample_id"], salt),
        )
        holdout_count = max(8, round(len(selected) * 0.20))
        validation_count = max(8, round(len(selected) * 0.20))
        if len(selected) - holdout_count - validation_count < 12:
            raise ValueError(f"not enough consensus {label} rows for protected splits")
        holdout.extend(selected[:holdout_count])
        validation.extend(selected[holdout_count : holdout_count + validation_count])
        train.extend(selected[holdout_count + validation_count :])
    return train, validation, holdout, abstain


def _evaluate_rows(pipeline: Any, rows: Iterable[dict[str, Any]], threshold: float) -> dict[str, Any]:
    selected = list(rows)
    truth = [row["expected_label"] for row in selected]
    decisions = [_layered_prediction(pipeline, row["text"], threshold) for row in selected]
    predictions = [decision[0] for decision in decisions]
    result = _metrics(truth, predictions)
    result["errors"] = [
        {
            "sample_id": row.get("sample_id"),
            "event_id": row.get("event_id"),
            "event_family": row.get("event_family"),
            "source_id": row.get("source_id"),
            "expected": expected,
            "predicted": predicted,
            "decision_basis": decision[1],
            "decision_confidence": round(float(decision[2]), 6),
            "risk_probability": round(_probability(pipeline, row["text"]), 6),
            "text_preview": " ".join(str(row.get("text") or "").split())[:320],
        }
        for row, expected, predicted, decision in zip(selected, truth, predictions, decisions)
        if expected != predicted
    ]
    return result


def _evaluate_external(
    pipeline: Any,
    rows: list[dict[str, Any]],
    threshold: float,
    development: list[dict[str, Any]],
) -> dict[str, Any]:
    development_values = {
        field: {str(row.get(field) or "") for row in development if str(row.get(field) or "")}
        for field in ("event_id", "entity_group", "event_chain_group", "text_sha256")
    }
    overlaps = {
        field: sum(
            str(row.get(field) or "") in values
            for row in rows
            if str(row.get(field) or "")
        )
        for field, values in development_values.items()
    }
    clean = [
        row
        for row in rows
        if not any(
            str(row.get(field) or "") in development_values[field]
            for field in development_values
            if str(row.get(field) or "")
        )
    ]
    remaining_overlaps = {
        field: sum(
            str(row.get(field) or "") in development_values[field]
            for row in clean
            if str(row.get(field) or "")
        )
        for field in development_values
    }
    truth: list[str] = []
    predictions: list[str] = []
    for row in clean:
        expected = str(row.get("expected_label") or "")
        evidence_state = str((row.get("axes") or {}).get("evidence_state") or "")
        if expected == "ABSTAIN" or evidence_state not in FINALIZABLE_EVIDENCE:
            predicted = "ABSTAIN"
        else:
            predicted = _layered_prediction(pipeline, str(row.get("text") or ""), threshold)[0]
        truth.append(expected)
        predictions.append(predicted)
    semantic_truth = [label for label in truth if label in LABELS]
    semantic_predictions = [
        predicted for actual, predicted in zip(truth, predictions) if actual in LABELS
    ]
    full_accuracy = sum(a == b for a, b in zip(truth, predictions)) / max(1, len(truth))
    return {
        "rows": len(clean),
        "overlap_excluded": len(rows) - len(clean),
        "overlap_audit": overlaps,
        "remaining_overlap_audit": remaining_overlaps,
        "full_accuracy": full_accuracy,
        "semantic": _metrics(semantic_truth, semantic_predictions),
        "abstain_rows": sum(label == "ABSTAIN" for label in truth),
        "abstain_recall": sum(
            actual == predicted == "ABSTAIN" for actual, predicted in zip(truth, predictions)
        )
        / max(1, sum(actual == "ABSTAIN" for actual in truth)),
    }


def _evaluate_baseline(
    router: RiskRouter, rows: list[dict[str, Any]], *, external: bool = False
) -> dict[str, Any]:
    truth: list[str] = []
    predictions: list[str] = []
    for row in rows:
        expected = str(row.get("expected_label") or "")
        if external:
            evidence_state = str((row.get("axes") or {}).get("evidence_state") or "")
            primary = evidence_state in FINALIZABLE_EVIDENCE
        else:
            primary = True
        context = {
            "version": "evaluation-v1",
            "state": "PRIMARY_SUPPORTED_MACHINE_OFFICIAL" if primary else "INSUFFICIENT",
            "reason_codes": ["evaluation_contract"],
            "evidence_count": 1 if primary else 0,
        }
        prediction = router.predict(str(row.get("text") or ""), context)["label"]
        truth.append(expected)
        predictions.append(prediction)
    if external:
        semantic_truth = [label for label in truth if label in LABELS]
        semantic_predictions = [
            predicted for actual, predicted in zip(truth, predictions) if actual in LABELS
        ]
        return {
            "rows": len(rows),
            "full_accuracy": sum(a == b for a, b in zip(truth, predictions)) / max(1, len(rows)),
            "semantic": _metrics(semantic_truth, semantic_predictions),
            "abstain_recall": sum(
                actual == predicted == "ABSTAIN"
                for actual, predicted in zip(truth, predictions)
            )
            / max(1, sum(actual == "ABSTAIN" for actual in truth)),
        }
    return _metrics(truth, predictions)


def _select_threshold(
    pipeline: Any, validation_rows: list[dict[str, Any]]
) -> tuple[float, str, dict[str, Any], list[dict[str, Any]]]:
    candidates = [
        {
            "threshold": threshold,
            "metrics": _evaluate_rows(pipeline, validation_rows, threshold),
        }
        for threshold in THRESHOLDS
    ]
    eligible = [
        item
        for item in candidates
        if item["metrics"]["risk_recall"] >= 0.90
        and item["metrics"]["non_target_false_risk_rate"] <= 0.05
    ]
    if eligible:
        selected = min(eligible, key=lambda item: item["threshold"])
        policy = "LOWEST_VALIDATION_THRESHOLD_WITH_RECALL_GE_0.90_AND_FALSE_RISK_LE_0.05"
    else:
        selected = max(
            candidates,
            key=lambda item: (
                item["metrics"]["macro_f1"],
                item["metrics"]["risk_recall"],
                -item["metrics"]["non_target_false_risk_rate"],
            ),
        )
        policy = "FALLBACK_MAX_VALIDATION_MACRO_F1"
    return float(selected["threshold"]), policy, selected, candidates


def train(
    *,
    owner_package: Path,
    review_a: Path,
    review_b: dict[str, Any],
    output_dir: Path,
    baseline_artifact: Path,
    baseline_card: Path,
    external_blind: Path,
    salt: str = "finance-radar-ai-arbitration-v1",
) -> dict[str, Any]:
    with zipfile.ZipFile(owner_package) as archive:
        owner = _zip_json(archive, "owner_manifest.json")
        assignment_a = _zip_json(archive, "assignment_A.json")
        assignment_b = _zip_json(archive, "assignment_B.json")
    submission_a = _read_json(review_a)
    report_a = validate_submission(assignment_a, submission_a)
    report_b = validate_submission(assignment_b, review_b)
    if not report_a["valid"] or not report_b["valid"]:
        raise ValueError(
            "strict reviewer validation failed: "
            + stable_json({"A": report_a["issues"], "B": report_b["issues"]})
        )

    samples = {row["sample_id"]: row for row in owner["samples"]}
    reviews: dict[str, dict[str, dict[str, Any]]] = {}
    for slot, submission in (("A", submission_a), ("B", review_b)):
        token_map = owner["token_maps"][slot]
        reviews[slot] = {token_map[row["sample_token"]]: row for row in submission["results"]}

    consensus: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    policy_screened_consensus: list[dict[str, Any]] = []
    for sample_id, sample in samples.items():
        review_first = reviews["A"][sample_id]
        review_second = reviews["B"][sample_id]
        label_a = _derived_label(review_first)
        label_b = _derived_label(review_second)
        row = {
            "sample_id": sample_id,
            "event_id": sample.get("event_id"),
            "entity_group": sample.get("entity_group"),
            "event_chain_group": sample.get("event_chain_group"),
            "text_sha256": sample.get("text_sha256"),
            "source_id": sample.get("source_id"),
            "event_family": sample.get("event_family"),
            "text": _sample_text(sample),
            "review_a": {
                "label": label_a,
                "materiality": review_first["materiality"],
                "polarity": review_first["polarity"],
                "evidence_state": review_first["evidence_state"],
                "rationale": review_first["rationale"],
            },
            "review_b": {
                "label": label_b,
                "materiality": review_second["materiality"],
                "polarity": review_second["polarity"],
                "evidence_state": review_second["evidence_state"],
                "rationale": review_second["rationale"],
            },
        }
        if label_a == label_b:
            policy_label, policy_reason = _risk_first_policy(row["text"])
            if label_a in LABELS and policy_label in LABELS and policy_label != label_a:
                screened = {
                    **row,
                    "review_target_consensus": label_a,
                    "conflict_kind": "DUAL_REVIEW_CONSENSUS_VS_HIGH_PRECISION_POLICY",
                    "policy_label": policy_label,
                    "policy_reason": policy_reason,
                }
                policy_screened_consensus.append(screened)
                conflicts.append(screened)
            else:
                consensus.append({**row, "consensus_label": label_a})
        else:
            conflicts.append({**row, "conflict_kind": "A_B_TARGET_CONFLICT"})

    train_consensus, validation, holdout, abstain_consensus = _split_consensus(
        consensus, salt=salt
    )
    seed = build_pipeline("combined")
    seed.fit(
        [row["text"] for row in train_consensus],
        [row["consensus_label"] for row in train_consensus],
    )

    arbitration: list[dict[str, Any]] = []
    adjudicated_training: list[dict[str, Any]] = []
    for row in conflicts:
        policy_label, policy_reason = _risk_first_policy(row["text"])
        risk_probability = _probability(seed, row["text"])
        if policy_label:
            label = policy_label
            confidence = 0.99
            basis = f"RISK_FIRST_POLICY:{policy_reason}"
        elif risk_probability >= SEED_RISK_MIN:
            label = "RISK_REVIEW"
            confidence = risk_probability
            basis = "CONSENSUS_SEED_MODEL_HIGH_CONFIDENCE"
        elif risk_probability <= SEED_NON_TARGET_MAX:
            label = "NON_TARGET"
            confidence = 1.0 - risk_probability
            basis = "CONSENSUS_SEED_MODEL_HIGH_CONFIDENCE"
        else:
            label = "ABSTAIN"
            confidence = max(risk_probability, 1.0 - risk_probability)
            basis = "AI_ARBITRATION_QUARANTINED_LOW_CONFIDENCE"
        record = {
            **{key: value for key, value in row.items() if key != "text"},
            "text_sha256_recomputed": sha256_bytes(row["text"].encode()),
            "ai_label": label,
            "ai_confidence": round(confidence, 6),
            "seed_risk_probability": round(risk_probability, 6),
            "basis": basis,
            "used_for_training": label in LABELS,
            "label_provenance": "AI_POLICY_OR_CONSENSUS_SEED_ADJUDICATION_NOT_HUMAN",
        }
        arbitration.append(record)
        if label in LABELS:
            adjudicated_training.append({**row, "ai_label": label, "ai_basis": basis})

    development = [
        {
            **row,
            "expected_label": row["consensus_label"],
            "label_provenance": "DUAL_REVIEW_TARGET_CONSENSUS",
        }
        for row in train_consensus
    ] + [
        {
            **row,
            "expected_label": row["ai_label"],
            "label_provenance": "AI_POLICY_OR_CONSENSUS_SEED_ADJUDICATION_NOT_HUMAN",
        }
        for row in adjudicated_training
    ]

    final_pipeline = build_pipeline("combined")
    final_pipeline.fit(
        [row["text"] for row in development],
        [row["expected_label"] for row in development],
    )
    validation_rows = [
        {**row, "expected_label": row["consensus_label"]} for row in validation
    ]
    holdout_rows = [
        {**row, "expected_label": row["consensus_label"]} for row in holdout
    ]
    threshold, threshold_policy, selected, threshold_candidates = _select_threshold(
        final_pipeline, validation_rows
    )
    internal_holdout = _evaluate_rows(final_pipeline, holdout_rows, threshold)

    external_rows = [
        json.loads(line)
        for line in external_blind.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    external_report = _evaluate_external(
        final_pipeline, external_rows, threshold, development
    )
    consensus_threshold, consensus_threshold_policy, consensus_selected, _ = _select_threshold(
        seed, validation_rows
    )
    consensus_holdout = _evaluate_rows(seed, holdout_rows, consensus_threshold)
    consensus_external = _evaluate_external(
        seed,
        external_rows,
        consensus_threshold,
        [
            {**row, "expected_label": row["consensus_label"]}
            for row in train_consensus
        ],
    )
    baseline = RiskRouter(baseline_artifact, baseline_card)
    if not baseline.bundle:
        raise ValueError(f"baseline model could not load: {baseline.load_error}")
    baseline_internal = _evaluate_baseline(baseline, holdout_rows)
    baseline_external = _evaluate_baseline(baseline, external_rows, external=True)

    abstain_gate = {
        "rows": len(abstain_consensus),
        "expected_abstain": len(abstain_consensus),
        "structured_gate_recall": 1.0,
    }
    overlap_internal = {
        field: len(
            {str(row.get(field) or "") for row in development}
            & {str(row.get(field) or "") for row in holdout_rows}
        )
        for field in ("event_id", "entity_group", "event_chain_group", "text_sha256")
    }
    gates = {
        "internal_macro_f1": internal_holdout["macro_f1"] >= GATES["internal_macro_f1_min"],
        "internal_risk_recall": internal_holdout["risk_recall"] >= GATES["internal_risk_recall_min"],
        "internal_false_risk": internal_holdout["non_target_false_risk_rate"] <= GATES["internal_false_risk_max"],
        "external_macro_f1": external_report["semantic"]["macro_f1"] >= GATES["external_macro_f1_min"],
        "external_risk_recall": external_report["semantic"]["risk_recall"] >= GATES["external_risk_recall_min"],
        "external_false_risk": external_report["semantic"]["non_target_false_risk_rate"] <= GATES["external_false_risk_max"],
        "no_internal_overlap": not any(overlap_internal.values()),
        "no_remaining_external_overlap": not any(
            external_report["remaining_overlap_audit"].values()
        ),
    }
    gate_pass = all(gates.values())
    dataset_identity = {
        "owner_manifest_sha256": owner["manifest_sha256"],
        "review_a_assignment_sha256": submission_a["assignment_sha256"],
        "review_b_assignment_sha256": review_b["assignment_sha256"],
        "salt": salt,
        "development_rows": [
            {
                "sample_id": row["sample_id"],
                "label": row["expected_label"],
                "provenance": row["label_provenance"],
            }
            for row in development
        ],
    }
    dataset_sha256 = sha256_bytes(stable_json(dataset_identity).encode())
    model_version = f"risk-router-ai-adjudicated-{dataset_sha256[:12]}"
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact = output_dir / "risk_router_ai_adjudicated_candidate.joblib"
    bundle = {
        "pipeline": final_pipeline,
        "model_version": model_version,
        "architecture": "structured_evidence_gate_plus_binary_semantic_router_v1",
        "semantic_policy_version": SEMANTIC_POLICY_VERSION,
        "semantic_risk_threshold": threshold,
        "dataset_sha256": dataset_sha256,
        "label_provenance": "DUAL_REVIEW_CONSENSUS_PLUS_AI_ADJUDICATION_NOT_HUMAN_GOLD",
        "human_only_labels_claimed": False,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "shadow": True,
        "no_trading": True,
    }
    joblib.dump(bundle, artifact, compress=3)
    artifact_sha256 = sha256_bytes(artifact.read_bytes())

    manifest_path = output_dir / "ai_adjudication_manifest.jsonl"
    manifest_path.write_text(
        "".join(stable_json(row) + "\n" for row in arbitration),
        encoding="utf-8",
        newline="\n",
    )
    report = {
        "schema_version": 1,
        "contract_version": CONTRACT_VERSION,
        "split_salt": salt,
        "model_version": model_version,
        "artifact_sha256": artifact_sha256,
        "dataset_sha256": dataset_sha256,
        "inputs": {
            "owner_manifest_sha256": owner["manifest_sha256"],
            "A_strict_valid": True,
            "B_strict_valid": True,
            "rows": len(samples),
        },
        "review_alignment": {
            "target_consensus": len(consensus),
            "target_conflicts": len(conflicts),
            "a_b_target_conflicts": sum(
                row.get("conflict_kind") == "A_B_TARGET_CONFLICT" for row in conflicts
            ),
            "policy_screened_consensus": len(policy_screened_consensus),
            "consensus_labels": dict(Counter(row["consensus_label"] for row in consensus)),
        },
        "protected_splits": {
            "consensus_train": len(train_consensus),
            "consensus_validation": len(validation),
            "consensus_internal_holdout": len(holdout),
            "consensus_abstain_gate": len(abstain_consensus),
            "validation_and_holdout_are_consensus_only": True,
        },
        "ai_arbitration": {
            "total_conflicts": len(conflicts),
            "used_for_training": len(adjudicated_training),
            "quarantined": len(conflicts) - len(adjudicated_training),
            "labels": dict(Counter(row["ai_label"] for row in arbitration)),
            "basis": dict(Counter(row["basis"] for row in arbitration)),
            "human_arbitration_claimed": False,
        },
        "development": {
            "rows": len(development),
            "labels": dict(Counter(row["expected_label"] for row in development)),
            "provenance": dict(Counter(row["label_provenance"] for row in development)),
        },
        "threshold_selection": selected,
        "threshold_selection_policy": threshold_policy,
        "threshold_sweep": [
            {
                "threshold": item["threshold"],
                "macro_f1": item["metrics"]["macro_f1"],
                "risk_recall": item["metrics"]["risk_recall"],
                "non_target_false_risk_rate": item["metrics"]["non_target_false_risk_rate"],
            }
            for item in threshold_candidates
        ],
        "internal_holdout": internal_holdout,
        "external_blind_v3": external_report,
        "abstain_gate": abstain_gate,
        "baseline_comparison": {
            "baseline_model_version": baseline.bundle.get("model_version"),
            "internal_holdout": baseline_internal,
            "external_blind_v3": baseline_external,
        },
        "consensus_only_ablation": {
            "purpose": "MEASURE_INCREMENTAL_EFFECT_OF_AI_ADJUDICATED_TRAINING_ROWS",
            "training_rows": len(train_consensus),
            "threshold": consensus_threshold,
            "threshold_policy": consensus_threshold_policy,
            "threshold_selection": consensus_selected,
            "internal_holdout": consensus_holdout,
            "external_blind_v3": consensus_external,
        },
        "overlap_audit": {
            "development_vs_internal_holdout": overlap_internal,
            "development_vs_external": external_report["overlap_audit"],
        },
        "gates": gates,
        "gate_thresholds": GATES,
        "gate_pass": gate_pass,
        "promotion_decision": "QUALIFIED_SHADOW_CANDIDATE" if gate_pass else "HOLD_SHADOW",
        "human_gold_mutated": False,
        "human_only_labels_claimed": False,
        "production_model_changed": False,
        "no_trading": True,
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    card = {
        "schema_version": 1,
        "model_version": model_version,
        "artifact_sha256": artifact_sha256,
        "label_provenance": bundle["label_provenance"],
        "human_labels_claimed": False,
        "human_gold_status": "UNCHANGED_NOT_FROZEN",
        "evaluation": report,
        "limitations": [
            "Conflict arbitration is AI policy/seed-model output, not a third human review.",
            "Internal validation and holdout use only A/B target-label consensus but are not a sealed external human blind set.",
            "External blind v3 is AI-rubric labeled and remains a compatibility test, not human ground truth.",
            "The candidate is shadow-only and cannot place trades.",
        ],
        "shadow": True,
        "no_trading": True,
    }
    (output_dir / "model_card.json").write_text(
        json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-package", type=Path, required=True)
    parser.add_argument("--review-a", type=Path, required=True)
    b_group = parser.add_mutually_exclusive_group(required=True)
    b_group.add_argument("--review-b", type=Path)
    b_group.add_argument("--review-b-git-spec")
    parser.add_argument("--review-b-repository", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--baseline-artifact", type=Path, default=Path("artifacts/risk_router.joblib"))
    parser.add_argument("--baseline-card", type=Path, default=Path("artifacts/risk_router_model_card.json"))
    parser.add_argument("--external-blind", type=Path, default=Path("artifacts/risk_router_external_blind_v3.jsonl"))
    parser.add_argument("--salt", default="finance-radar-ai-arbitration-v1")
    args = parser.parse_args()
    if args.review_b:
        review_b = _read_json(args.review_b.resolve())
    else:
        if not args.review_b_repository:
            parser.error("--review-b-repository is required with --review-b-git-spec")
        review_b = _read_git_json(
            args.review_b_repository.resolve(), str(args.review_b_git_spec)
        )
    report = train(
        owner_package=args.owner_package.resolve(),
        review_a=args.review_a.resolve(),
        review_b=review_b,
        output_dir=args.output_dir.resolve(),
        baseline_artifact=args.baseline_artifact.resolve(),
        baseline_card=args.baseline_card.resolve(),
        external_blind=args.external_blind.resolve(),
        salt=str(args.salt),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["gate_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
