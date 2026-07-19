#!/usr/bin/env python3
"""Audit a frozen risk-router artifact for schema-language shortcuts.

This is diagnostic only. It never trains, mutates or promotes a model.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import joblib
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT = ROOT / "artifacts" / "risk_router.joblib"
DEFAULT_CARD = ROOT / "artifacts" / "risk_router_model_card.json"
DEFAULT_BLIND = ROOT / "artifacts" / "risk_router_external_blind_v1_report.json"
DEFAULT_OUTPUT = ROOT / "artifacts" / "risk_router_v1_shortcut_audit.json"
DEFAULT_MARKDOWN = ROOT / "artifacts" / "risk_router_v1_shortcut_audit.md"


def _top_features(pipeline: Any, limit: int = 30) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    feature_names = pipeline.named_steps["features"].get_feature_names_out()
    classifier = pipeline.named_steps["classifier"]
    coefficients = np.mean(
        [calibrated.estimator.coef_[0] for calibrated in classifier.calibrated_classifiers_],
        axis=0,
    )
    toward_risk = [
        {"feature": str(feature_names[index]), "coefficient": round(float(coefficients[index]), 6)}
        for index in np.argsort(coefficients)[-limit:][::-1]
    ]
    toward_non_target = [
        {"feature": str(feature_names[index]), "coefficient": round(float(coefficients[index]), 6)}
        for index in np.argsort(coefficients)[:limit]
    ]
    return toward_risk, toward_non_target


def audit(artifact_path: Path, card_path: Path, blind_path: Path) -> dict[str, Any]:
    bundle = joblib.load(artifact_path)
    card = json.loads(card_path.read_text(encoding="utf-8"))
    blind = json.loads(blind_path.read_text(encoding="utf-8"))
    toward_risk, toward_non_target = _top_features(bundle["pipeline"])
    family_markers = set((card.get("dataset") or {}).get("top_event_families") or {})
    internal_markers = family_markers | {
        "candidate official",
        "official sec",
        "source_metadata_control",
        "bankruptcyliquidation",
        "bankruptcy_liquidation",
    }
    suspected = []
    for direction, rows in (("RISK_REVIEW", toward_risk), ("NON_TARGET", toward_non_target)):
        for row in rows:
            word = row["feature"].removeprefix("word_tfidf__")
            matches = sorted(marker for marker in internal_markers if marker in word)
            if matches:
                suspected.append({**row, "direction": direction, "matched_markers": matches})
    predictions = blind.get("predictions") or []
    route_counts = Counter(row.get("predicted_label") for row in predictions)
    risk_confidences = [
        float(row["confidence"])
        for row in predictions
        if row.get("predicted_label") == "RISK_REVIEW"
    ]
    return {
        "schema_version": 1,
        "audit_type": "post_blind_diagnostic_no_training",
        "model_version": bundle.get("model_version"),
        "artifact_sha256": card.get("artifact_sha256"),
        "external_blind_freeze_id": blind.get("freeze_id"),
        "external_blind_gate_pass": blind.get("gate_pass"),
        "external_blind_route_distribution": dict(route_counts),
        "mean_risk_route_confidence": (
            sum(risk_confidences) / len(risk_confidences) if risk_confidences else None
        ),
        "suspected_shortcut_features": suspected,
        "top_toward_risk": toward_risk,
        "top_toward_non_target": toward_non_target,
        "findings": [
            "The in-domain grouped holdout did not expose the cross-source failure seen on the external set.",
            "Training text includes event_family, event_type and discovery_source-derived language; top coefficients contain internal taxonomy/control markers.",
            "The NON_TARGET class is composed of rejected candidates and controls, not a representative sample of ordinary official company and macro news.",
            "All frozen blind rows used the trained artifact; the positive keyword guardrail was not a general-domain solution.",
        ],
        "required_v2_controls": [
            "Do not reuse external-blind-v1 as a promotion test after diagnosis.",
            "Build development hard negatives from separate ordinary official company and macro news.",
            "Train only on publish-time content fields; remove event_family, event_type, discovery_source and internal control strings from model input.",
            "Add a coefficient shortcut audit and source-held-out development split before freezing model v2.",
            "Freeze a new label-first external-blind-v2 only after the v2 artifact and thresholds are locked.",
        ],
        "model_or_labels_mutated": False,
        "promotion_decision": "REMAIN_SHADOW",
        "no_trading": True,
    }


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Risk Router v1 shortcut audit",
        "",
        f"- Model: `{report['model_version']}`",
        f"- External freeze: `{report['external_blind_freeze_id']}`",
        f"- External gate: `{'PASS' if report['external_blind_gate_pass'] else 'FAIL'}`",
        f"- Blind routes: `{json.dumps(report['external_blind_route_distribution'], sort_keys=True)}`",
        f"- Mean confidence among RISK_REVIEW routes: {float(report['mean_risk_route_confidence'] or 0):.1%}",
        "- This audit did not train, tune or mutate model v1 or the frozen labels.",
        "",
        "## Findings",
        "",
        *[f"- {item}" for item in report["findings"]],
        "",
        "## Suspected shortcut features",
        "",
    ]
    for item in report["suspected_shortcut_features"]:
        lines.append(
            f"- `{item['feature']}` -> {item['direction']} ({item['coefficient']:+.4f}); "
            f"markers={','.join(item['matched_markers'])}"
        )
    lines.extend(["", "## Locked v2 protocol", ""])
    lines.extend(f"- {item}" for item in report["required_v2_controls"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--card", type=Path, default=DEFAULT_CARD)
    parser.add_argument("--blind", type=Path, default=DEFAULT_BLIND)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    args = parser.parse_args()
    report = audit(args.artifact, args.card, args.blind)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, args.markdown)
    print(json.dumps({key: report[key] for key in ("model_version", "external_blind_gate_pass", "external_blind_route_distribution", "suspected_shortcut_features", "promotion_decision")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
