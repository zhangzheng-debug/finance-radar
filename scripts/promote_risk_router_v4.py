#!/usr/bin/env python3
"""Promote a blind-v3-qualified v4 candidate to the production SHADOW artifact."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
CANDIDATE = ARTIFACTS / "risk_router_v4_candidate.joblib"
CANDIDATE_CARD = ARTIFACTS / "risk_router_v4_candidate_model_card.json"
CANDIDATE_MANIFEST = ARTIFACTS / "risk_router_v4_candidate_manifest.jsonl"
DEV_REPORT = ARTIFACTS / "risk_router_v4_candidate_dev_report.json"
BLIND_REPORT = ARTIFACTS / "risk_router_external_blind_v3_report.json"
V2_REPORT = ARTIFACTS / "risk_router_external_blind_v2_report.json"
PRODUCTION = ARTIFACTS / "risk_router.joblib"


def main() -> int:
    blind = json.loads(BLIND_REPORT.read_text(encoding="utf-8"))
    development = json.loads(DEV_REPORT.read_text(encoding="utf-8"))
    v2 = json.loads(V2_REPORT.read_text(encoding="utf-8"))
    candidate_card = json.loads(CANDIDATE_CARD.read_text(encoding="utf-8"))
    candidate_sha256 = hashlib.sha256(CANDIDATE.read_bytes()).hexdigest()
    if not blind.get("gate_pass") or blind.get("promotion_decision") != "QUALIFIED_SHADOW":
        raise ValueError("blind-v3 has not qualified the candidate")
    if not development.get("gate_pass"):
        raise ValueError("development gate has not passed")
    if v2.get("gate_pass") is not False:
        raise ValueError("blind-v2 failure evidence must remain preserved")
    if blind.get("model_artifact_sha256") != candidate_sha256:
        raise ValueError("blind-v3 report does not describe the candidate bytes")
    if candidate_card.get("artifact_sha256") != candidate_sha256:
        raise ValueError("candidate model card hash mismatch")

    shutil.copy2(CANDIDATE, PRODUCTION)
    production_sha256 = hashlib.sha256(PRODUCTION.read_bytes()).hexdigest()
    if production_sha256 != candidate_sha256:
        raise RuntimeError("production artifact copy failed hash verification")
    (ARTIFACTS / "risk_router.sha256").write_text(
        f"{production_sha256}  risk_router.joblib\n", encoding="ascii"
    )
    shutil.copy2(CANDIDATE_MANIFEST, ARTIFACTS / "risk_router_training_manifest.jsonl")

    card = dict(candidate_card)
    card["artifact_sha256"] = production_sha256
    card["promotion"] = {
        "decision": "QUALIFIED_SHADOW",
        "blind_report": BLIND_REPORT.name,
        "blind_freeze_id": blind["freeze_id"],
        "promoted_artifact_name": PRODUCTION.name,
        "no_trading": True,
        "shadow": True,
    }
    card["predecessor_failure"] = {
        "report": V2_REPORT.name,
        "freeze_id": v2["freeze_id"],
        "gate_pass": False,
        "lesson": "Evidence state is a structured gate, not a text class.",
    }
    (ARTIFACTS / "risk_router_model_card.json").write_text(
        json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (ARTIFACTS / "risk_router_model_card.md").write_text(
        "\n".join(
            [
                "# Finance Radar Risk Router Model Card",
                "",
                f"- Model: `{blind['model_version']}`",
                f"- Artifact SHA-256: `{production_sha256}`",
                "- Architecture: structured evidence gate + high-precision semantic policy + binary small model",
                "- Status: `QUALIFIED_SHADOW`; no trading and no automatic verification",
                "- Labels: AI rubric adjudications, explicitly not human labels",
                f"- Development macro F1 / risk recall: `{development['selected_metrics']['macro_f1']:.3f}` / `{development['selected_metrics']['risk_recall']:.3f}`",
                f"- Blind-v3 full accuracy / macro F1: `{blind['full_layered_metrics']['accuracy']:.3f}` / `{blind['full_layered_metrics']['macro_f1']:.3f}`",
                f"- Blind-v3 risk recall / normal false-risk: `{blind['full_layered_metrics']['risk_recall']:.3f}` / `{blind['full_layered_metrics']['non_target_false_risk_rate']:.3f}`",
                f"- Blind-v3 ABSTAIN recall: `{blind['full_layered_metrics']['abstain_recall']:.3f}`",
                "- Blind-v2 FAIL remains published as predecessor failure evidence.",
                "",
                "## Limitations",
                "",
                *[f"- {item}" for item in card["limitations"]],
                "",
            ]
        ),
        encoding="utf-8",
    )
    (ARTIFACTS / "risk_router_metrics.json").write_text(
        json.dumps(
            {
                "schema_version": 4,
                "model_version": blind["model_version"],
                "development": development["selected_metrics"],
                "external_blind_v3": blind["full_layered_metrics"],
                "semantic_blind_v3": blind["semantic_substantive_metrics"],
                "promotion_decision": blind["promotion_decision"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (ARTIFACTS / "risk_router_feature_schema.json").write_text(
        json.dumps(
            {
                "schema_version": 4,
                "architecture": blind["architecture"],
                "structured_input": ["evidence_state", "exact_evidence_text"],
                "semantic_features": ["word TF-IDF 1-2 grams", "character TF-IDF 3-5 grams"],
                "policy_gate": blind["semantic_policy_version"],
                "outputs": ["RISK_REVIEW", "NON_TARGET", "ABSTAIN"],
                "abstain_owner": "structured evidence gate",
                "prohibited": [
                    "event status", "manual grade", "event taxonomy shortcut", "source identity shortcut",
                    "post-event price", "return", "position", "order", "account data",
                ],
                "no_trading": True,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (ARTIFACTS / "risk_router_data_card.json").write_text(
        json.dumps(
            {
                "schema_version": 4,
                "label_provenance": blind["label_provenance"],
                "human_labels_claimed": False,
                "development": {
                    "rows": development["development_rows"],
                    "label_counts": development["label_counts"],
                    "dataset_sha256": development["development_dataset_sha256"],
                },
                "external_blind_v3": {
                    "rows": blind["rows"],
                    "label_counts": blind["label_counts"],
                    "source_counts": blind["source_counts"],
                    "dataset_sha256": blind["dataset_sha256"],
                    "overlap_audit": blind["overlap_audit"],
                },
                "predecessor_blind_v2_failure_preserved": True,
                "no_trading": True,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (ARTIFACTS / "risk_router_data_card.md").write_text(
        "\n".join(
            [
                "# Finance Radar Risk Router Data Card",
                "",
                f"- Development rows: `{development['development_rows']}` `{development['label_counts']}`",
                f"- Blind-v3 rows: `{blind['rows']}` `{blind['label_counts']}`",
                f"- Development/blind overlap: `{sum(blind['overlap_audit'].values())}`",
                "- AI rubric adjudications are explicitly not human labels.",
                "- ABSTAIN is determined by structured evidence state, not guessed from prose.",
                "- Post-event market and trading/account fields are prohibited.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps({
        "model_version": blind["model_version"],
        "production_artifact": str(PRODUCTION),
        "artifact_sha256": production_sha256,
        "promotion_decision": blind["promotion_decision"],
        "shadow": True,
        "no_trading": True,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
