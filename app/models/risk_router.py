from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import joblib

from .evidence_policy import is_conflicting_evidence_status
from .risk_scope_gate import GATE_VERSION, assess_risk_scope
from .semantic_policy_gate import SEMANTIC_POLICY_VERSION, assess_semantic_policy


NEGATIVE_TERMS = {
    "bankruptcy", "chapter 11", "delisting", "suspension", "default", "recall",
    "fraud", "investigation", "liquidation", "insolvency", "reverse split",
    "dilution", "impairment", "closure", "receivership", "breach", "layoff",
}
POSITIVE_TERMS = {
    "approval", "beat estimates", "record revenue", "dividend", "buyback",
    "partnership", "contract award", "upgrade", "profit growth", "guidance raised",
}

EVIDENCE_GATE_VERSION = "structured-evidence-gate-v1"
INPUT_CONTRACT_VERSION = "risk-router-decision-input-v2"
MACHINE_PRIMARY_SOURCES = {
    "bls_key_indicators", "cftc_enforcement", "ecb_press", "ecb_statistical_press",
    "fda_medwatch", "fdic_press_releases", "federal_reserve_press", "ftc_press",
    "sec_litigation_releases", "sec_trading_suspensions",
}


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def derive_evidence_context(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize decision-grade evidence without event taxonomy or market outcomes."""
    statuses = {str(item.get("evidence_status") or "") for item in evidence}
    if any(is_conflicting_evidence_status(status) for status in statuses):
        state = "CONFLICTED"
        reasons = ["contradictory_primary_evidence"]
    elif statuses & {"confirmed_primary", "accepted_manual_primary_evidence"}:
        state = "PRIMARY_SUPPORTED_REVIEWED"
        reasons = ["reviewed_primary_exact_passage"]
    elif "accepted_light_primary_evidence" in statuses:
        state = "PRIMARY_SUPPORTED_LIGHT_VERIFIED"
        reasons = ["bounded_light_primary_exact_passage"]
    elif any(
        str(item.get("evidence_status") or "") == "machine_extracted_unreviewed"
        and str(item.get("source_id") or "") in MACHINE_PRIMARY_SOURCES
        and str(item.get("authority_tier") or "").startswith(("P0", "P1"))
        and 60 <= len(" ".join(str(item.get("evidence_passage") or "").split())) <= 6000
        for item in evidence
    ):
        state = "PRIMARY_SUPPORTED_MACHINE_OFFICIAL"
        reasons = ["official_primary_machine_exact_passage"]
    elif any(str(item.get("authority_tier") or "").startswith("P2") for item in evidence):
        state = "DISCOVERY_ONLY"
        reasons = ["discovery_only_evidence"]
    else:
        state = "INSUFFICIENT"
        reasons = ["no_decision_grade_primary_passage"]
    return {
        "version": EVIDENCE_GATE_VERSION,
        "state": state,
        "reason_codes": reasons,
        "evidence_count": len(evidence),
    }


class RiskRouter:
    """CPU-only shadow classifier. It never emits orders, positions or returns."""

    def __init__(self, artifact_path: str | Path, model_card_path: str | Path | None = None):
        self.artifact_path = Path(artifact_path)
        self.model_card_path = Path(model_card_path) if model_card_path else self.artifact_path.with_name("risk_router_model_card.json")
        self.bundle: dict[str, Any] | None = None
        self.load_error: str | None = None
        self.artifact_sha256: str | None = None
        if self.artifact_path.is_file():
            try:
                self.artifact_sha256 = hashlib.sha256(self.artifact_path.read_bytes()).hexdigest()
                self.bundle = joblib.load(self.artifact_path)
            except Exception as exc:  # model corruption must degrade visibly, not crash the API
                self.load_error = f"{type(exc).__name__}: {exc}"

    def _fallback(self, text: str) -> tuple[str, float, dict[str, float]]:
        normalized = text.lower()
        negative_hits = sum(term in normalized for term in NEGATIVE_TERMS)
        positive_hits = sum(term in normalized for term in POSITIVE_TERMS)
        if negative_hits >= 2:
            confidence = min(0.58 + 0.08 * negative_hits, 0.94)
            return "RISK_REVIEW", confidence, {"RISK_REVIEW": confidence, "NON_TARGET": 1 - confidence}
        if positive_hits and not negative_hits:
            confidence = min(0.62 + 0.06 * positive_hits, 0.90)
            return "NON_TARGET", confidence, {"RISK_REVIEW": 1 - confidence, "NON_TARGET": confidence}
        return "ABSTAIN", 0.5, {"RISK_REVIEW": 0.5, "NON_TARGET": 0.5}

    def predict(self, text: str, evidence_context: dict[str, Any] | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        text = " ".join((text or "").split())[:20000]
        scope_gate = assess_risk_scope(text)
        normalized = text.lower()
        negative_hits = sum(term in normalized for term in NEGATIVE_TERMS)
        positive_hits = sum(term in normalized for term in POSITIVE_TERMS)
        model_version = str(self.bundle.get("model_version")) if self.bundle else "risk-router-keyword-fallback-v1"
        architecture = str(self.bundle.get("architecture") or "legacy_text_router") if self.bundle else "legacy_text_router"
        evidence_gate = evidence_context or {
            "version": EVIDENCE_GATE_VERSION,
            "state": "NOT_PROVIDED",
            "reason_codes": ["legacy_call_without_structured_evidence"],
            "evidence_count": None,
        }
        # ``input_sha256`` is an audit identity, not merely a text hash.  It
        # includes every value that can affect a gate/model decision so a changed
        # evidence state or threshold can never be silently deduplicated.
        decision_configuration = {
            "model_version": model_version,
            "artifact_sha256": self.artifact_sha256,
            "architecture": architecture,
            "abstain_threshold": self.bundle.get("abstain_threshold") if self.bundle else 0.62,
            "risk_rescue_floor": self.bundle.get("risk_rescue_floor") if self.bundle else None,
            "risk_rescue_margin": self.bundle.get("risk_rescue_margin") if self.bundle else None,
            "semantic_risk_threshold": self.bundle.get("semantic_risk_threshold") if self.bundle else None,
            "scope_gate_version": GATE_VERSION,
            "semantic_policy_gate_version": SEMANTIC_POLICY_VERSION,
            "evidence_gate_version": EVIDENCE_GATE_VERSION,
        }
        input_contract = {
            "version": INPUT_CONTRACT_VERSION,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "text_characters": len(text),
            "evidence_context_sha256": _sha256_json(evidence_gate),
            "decision_configuration": decision_configuration,
        }
        input_hash = _sha256_json(
            {
                "input_contract_version": INPUT_CONTRACT_VERSION,
                "normalized_text": text,
                "evidence_context": evidence_gate,
                "decision_configuration": decision_configuration,
            }
        )
        call_counts = {
            "scope_gate_calls": 1,
            "evidence_gate_calls": 0,
            "semantic_policy_gate_calls": 0,
            "trained_model_calls": 0,
            "fallback_heuristic_calls": 0,
            "external_model_calls": 0,
        }
        evidence_primary = str(evidence_gate.get("state") or "").startswith("PRIMARY_SUPPORTED")
        if architecture == "structured_evidence_gate_plus_binary_semantic_router_v1" and not evidence_primary:
            call_counts["evidence_gate_calls"] = 1
            confidence = 1.0
            label = "ABSTAIN"
            probability_map = {"RISK_REVIEW": 0.0, "NON_TARGET": 0.0, "ABSTAIN": 1.0}
            runtime = "structured_evidence_gate"
            decision_source = "DETERMINISTIC_EVIDENCE_GATE"
            semantic_model_invoked = False
            confidence_applicable = False
        elif architecture == "structured_evidence_gate_plus_binary_semantic_router_v1":
            call_counts["evidence_gate_calls"] = 1
            call_counts["semantic_policy_gate_calls"] = 1
            semantic_policy = assess_semantic_policy(text)
            if semantic_policy.decision != "DEFER_TO_MODEL":
                label = semantic_policy.decision
                confidence = 0.99
                probability_map = {
                    "RISK_REVIEW": 0.99 if label == "RISK_REVIEW" else 0.01,
                    "NON_TARGET": 0.99 if label == "NON_TARGET" else 0.01,
                }
                runtime = "semantic_policy_gate"
                decision_source = "DETERMINISTIC_SEMANTIC_POLICY_GATE"
                semantic_model_invoked = False
                confidence_applicable = False
            elif self.bundle:
                pipeline = self.bundle["pipeline"]
                probabilities = pipeline.predict_proba([text])[0]
                call_counts["trained_model_calls"] = 1
                classes = [str(item) for item in pipeline.classes_]
                probability_map = {item_label: float(value) for item_label, value in zip(classes, probabilities)}
                risk_probability = probability_map.get("RISK_REVIEW", 0.0)
                semantic_threshold = float(self.bundle.get("semantic_risk_threshold", 0.5))
                label = "RISK_REVIEW" if risk_probability >= semantic_threshold else "NON_TARGET"
                confidence = probability_map[label]
                runtime = "trained_semantic_artifact"
                decision_source = "TRAINED_SEMANTIC_MODEL"
                semantic_model_invoked = True
                confidence_applicable = True
            else:  # pragma: no cover
                label, confidence, probability_map = self._fallback(text)
                call_counts["fallback_heuristic_calls"] = 1
                runtime = "fallback"
                decision_source = "KEYWORD_FALLBACK"
                semantic_model_invoked = False
                confidence_applicable = True
        elif scope_gate.decision in {"REJECT_NOISE", "REJECT_NON_TARGET"}:
            confidence = 0.94 if scope_gate.decision == "REJECT_NOISE" else min(
                0.68 + 0.05 * max(positive_hits, len(scope_gate.positive_cues)), 0.93
            )
            label = "NON_TARGET"
            probability_map = {"RISK_REVIEW": 1 - confidence, "NON_TARGET": confidence}
            runtime = "scope_guardrail"
            decision_source = "LEGACY_SCOPE_GUARDRAIL"
            semantic_model_invoked = False
            confidence_applicable = False
        elif scope_gate.decision in {"ABSTAIN_INSUFFICIENT", "ADMIT_CONTEXT"}:
            confidence = 0.5
            label = "ABSTAIN"
            probability_map = {"RISK_REVIEW": 0.5, "NON_TARGET": 0.5}
            runtime = "scope_guardrail"
            decision_source = "LEGACY_SCOPE_GUARDRAIL"
            semantic_model_invoked = False
            confidence_applicable = False
        elif self.bundle:
            pipeline = self.bundle["pipeline"]
            probabilities = pipeline.predict_proba([text])[0]
            call_counts["trained_model_calls"] = 1
            classes = [str(item) for item in pipeline.classes_]
            probability_map = {label: float(value) for label, value in zip(classes, probabilities)}
            best_label = max(probability_map, key=probability_map.get)
            confidence = probability_map[best_label]
            threshold = float(self.bundle.get("abstain_threshold", 0.62))
            risk_floor = self.bundle.get("risk_rescue_floor")
            risk_margin = self.bundle.get("risk_rescue_margin")
            risk_probability = probability_map.get("RISK_REVIEW", 0.0)
            if (
                risk_floor is not None
                and risk_margin is not None
                and risk_probability >= float(risk_floor)
                and risk_probability >= confidence - float(risk_margin)
            ):
                label = "RISK_REVIEW"
                confidence = risk_probability
            else:
                label = best_label if best_label == "ABSTAIN" or confidence >= threshold else "ABSTAIN"
            runtime = "trained_artifact"
            decision_source = "TRAINED_SEMANTIC_MODEL"
            semantic_model_invoked = True
            confidence_applicable = True
        else:
            label, confidence, probability_map = self._fallback(text)
            call_counts["fallback_heuristic_calls"] = 1
            runtime = "fallback"
            decision_source = "KEYWORD_FALLBACK"
            semantic_model_invoked = False
            # The keyword fallback is a deterministic queueing heuristic, not
            # a calibrated semantic model.  Keep its score for diagnostics but
            # never present it as an applicable model confidence.
            confidence_applicable = False
        latency_ms = (time.perf_counter() - started) * 1000
        return {
            "label": label,
            "confidence": round(float(confidence), 6),
            "probabilities": {key: round(value, 6) for key, value in probability_map.items()},
            "model_version": model_version,
            "runtime": runtime,
            "scope_gate": scope_gate.as_dict(),
            "evidence_gate": evidence_gate,
            "architecture": architecture,
            "decision_source": decision_source,
            "semantic_model_invoked": semantic_model_invoked,
            "confidence_applicable": confidence_applicable,
            "call_kind": decision_source,
            "call_counts": call_counts,
            "model_call_count": int(call_counts["trained_model_calls"]),
            "input_contract": input_contract,
            "shadow": True,
            "no_trading": True,
            "input_sha256": input_hash,
            "latency_ms": round(latency_ms, 3),
        }

    def status(self) -> dict[str, Any]:
        card: dict[str, Any] | None = None
        if self.model_card_path.is_file():
            try:
                card = json.loads(self.model_card_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                card = None
        artifact_hash = None
        if self.artifact_path.is_file():
            artifact_hash = hashlib.sha256(self.artifact_path.read_bytes()).hexdigest()
        robustness: dict[str, Any] | None = None
        robustness_path = self.artifact_path.with_name("risk_router_robustness.json")
        if robustness_path.is_file():
            try:
                robustness = json.loads(robustness_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                robustness = None
        external_blind: dict[str, Any] | None = None
        external_blind_path = next(
            (
                path for path in (
                    self.artifact_path.with_name("risk_router_external_blind_v3_report.json"),
                    self.artifact_path.with_name("risk_router_external_blind_v2_report.json"),
                    self.artifact_path.with_name("risk_router_external_blind_v1_report.json"),
                )
                if path.is_file()
            ),
            self.artifact_path.with_name("risk_router_external_blind_v3_report.json"),
        )
        if external_blind_path.is_file():
            try:
                report = json.loads(external_blind_path.read_text(encoding="utf-8"))
                # Keep the status endpoint compact: the immutable full report retains
                # every prediction while the product only needs governance evidence.
                external_blind = {
                    key: report.get(key)
                    for key in (
                        "evaluation_type",
                        "freeze_id",
                        "dataset_sha256",
                        "rows",
                        "label_counts",
                        "source_counts",
                        "model_version",
                        "model_artifact_sha256",
                        "training_dataset_sha256",
                        "overlap_audit",
                        "metrics",
                        "direct_metrics",
                        "runtime_metrics",
                        "full_layered_metrics",
                        "semantic_substantive_metrics",
                        "source_metrics",
                        "thresholds",
                        "gates",
                        "gate_pass",
                        "promotion_decision",
                        "no_trading",
                    )
                }
                if external_blind.get("metrics") is None:
                    external_blind["metrics"] = (
                        report.get("full_layered_metrics")
                        or report.get("direct_metrics")
                        or report.get("runtime_metrics")
                    )
            except (OSError, json.JSONDecodeError):
                external_blind = None
        return {
            "status": "ready" if self.bundle else "fallback",
            "artifact_path": str(self.artifact_path),
            "artifact_sha256": artifact_hash,
            "model_version": self.bundle.get("model_version") if self.bundle else "risk-router-keyword-fallback-v1",
            "architecture": self.bundle.get("architecture") if self.bundle else "legacy_text_router",
            "abstain_threshold": self.bundle.get("abstain_threshold") if self.bundle else 0.62,
            "risk_rescue_floor": self.bundle.get("risk_rescue_floor") if self.bundle else None,
            "risk_rescue_margin": self.bundle.get("risk_rescue_margin") if self.bundle else None,
            "semantic_risk_threshold": self.bundle.get("semantic_risk_threshold") if self.bundle else None,
            "structured_evidence_gate": {
                "version": EVIDENCE_GATE_VERSION,
                "required_for_v4": True,
            },
            "semantic_policy_gate": {
                "version": SEMANTIC_POLICY_VERSION,
                "enforced_for_v4": True,
            },
            "operational_scope_gate": {
                "version": GATE_VERSION,
                "enforced": True,
                "purpose": "reject noise and abstain outside the downside-risk input contract",
                "artifact_unchanged": True,
            },
            "shadow": True,
            "no_trading": True,
            "load_error": self.load_error,
            "model_card": card,
            "robustness": robustness,
            "external_blind": external_blind,
        }
