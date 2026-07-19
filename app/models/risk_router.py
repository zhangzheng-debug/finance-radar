from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import joblib


NEGATIVE_TERMS = {
    "bankruptcy", "chapter 11", "delisting", "suspension", "default", "recall",
    "fraud", "investigation", "liquidation", "insolvency", "reverse split",
    "dilution", "impairment", "closure", "receivership", "breach", "layoff",
}
POSITIVE_TERMS = {
    "approval", "beat estimates", "record revenue", "dividend", "buyback",
    "partnership", "contract award", "upgrade", "profit growth", "guidance raised",
}


class RiskRouter:
    """CPU-only shadow classifier. It never emits orders, positions or returns."""

    def __init__(self, artifact_path: str | Path, model_card_path: str | Path | None = None):
        self.artifact_path = Path(artifact_path)
        self.model_card_path = Path(model_card_path) if model_card_path else self.artifact_path.with_name("risk_router_model_card.json")
        self.bundle: dict[str, Any] | None = None
        self.load_error: str | None = None
        if self.artifact_path.is_file():
            try:
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

    def predict(self, text: str) -> dict[str, Any]:
        started = time.perf_counter()
        text = " ".join((text or "").split())[:20000]
        input_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        normalized = text.lower()
        negative_hits = sum(term in normalized for term in NEGATIVE_TERMS)
        positive_hits = sum(term in normalized for term in POSITIVE_TERMS)
        # The training corpus is downside-risk specialized. A transparent polarity
        # guard prevents clearly positive news from being forced into that ontology.
        if positive_hits >= 2 and negative_hits == 0:
            confidence = min(0.68 + 0.05 * positive_hits, 0.93)
            label = "NON_TARGET"
            probability_map = {"RISK_REVIEW": 1 - confidence, "NON_TARGET": confidence}
            model_version = str(self.bundle.get("model_version")) if self.bundle else "risk-router-keyword-fallback-v1"
            runtime = "positive_polarity_guardrail"
        elif self.bundle:
            pipeline = self.bundle["pipeline"]
            probabilities = pipeline.predict_proba([text])[0]
            classes = [str(item) for item in pipeline.classes_]
            probability_map = {label: float(value) for label, value in zip(classes, probabilities)}
            best_label = max(probability_map, key=probability_map.get)
            confidence = probability_map[best_label]
            threshold = float(self.bundle.get("abstain_threshold", 0.62))
            label = best_label if confidence >= threshold else "ABSTAIN"
            model_version = str(self.bundle.get("model_version", "risk-router-unknown"))
            runtime = "trained_artifact"
        else:
            label, confidence, probability_map = self._fallback(text)
            model_version = "risk-router-keyword-fallback-v1"
            runtime = "fallback"
        latency_ms = (time.perf_counter() - started) * 1000
        return {
            "label": label,
            "confidence": round(float(confidence), 6),
            "probabilities": {key: round(value, 6) for key, value in probability_map.items()},
            "model_version": model_version,
            "runtime": runtime,
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
        external_blind_path = self.artifact_path.with_name("risk_router_external_blind_v1_report.json")
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
                        "source_metrics",
                        "thresholds",
                        "gates",
                        "gate_pass",
                        "promotion_decision",
                        "no_trading",
                    )
                }
            except (OSError, json.JSONDecodeError):
                external_blind = None
        return {
            "status": "ready" if self.bundle else "fallback",
            "artifact_path": str(self.artifact_path),
            "artifact_sha256": artifact_hash,
            "model_version": self.bundle.get("model_version") if self.bundle else "risk-router-keyword-fallback-v1",
            "abstain_threshold": self.bundle.get("abstain_threshold") if self.bundle else 0.62,
            "shadow": True,
            "no_trading": True,
            "load_error": self.load_error,
            "model_card": card,
            "robustness": robustness,
            "external_blind": external_blind,
        }
