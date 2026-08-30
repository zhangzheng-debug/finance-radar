"""Strict loopback runtime for the human-gold-trained Qwen risk model."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from app.models.qwen_risk_contract import (
    QWEN_RISK_CONTRACT_VERSION,
    QWEN_RISK_PROMPT_VERSION,
    QWEN_RISK_SYSTEM_PROMPT,
    assessment_scope,
    normalize_qwen_risk_content,
    validate_semantic_payload,
)
from app.models.qwen_risk_hybrid import (
    QWEN_HYBRID_POLICY_VERSION,
    apply_qwen_hybrid_anchor,
)
from app.models.risk_router import derive_evidence_context


QWEN_RISK_MODEL_TASK = "QWEN_RISK_SEMANTICS"


class QwenRiskContractError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_loopback_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        return False
    if parsed.query or parsed.fragment:
        return False
    if parsed.hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def build_qwen_risk_input(
    detail: dict[str, Any], evidence: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build the same bounded semantic shape used by the frozen SFT export."""

    event = detail.get("event") if isinstance(detail.get("event"), dict) else {}
    version = (
        detail.get("current_version")
        if isinstance(detail.get("current_version"), dict)
        else {}
    )
    facts = version.get("facts") if isinstance(version.get("facts"), dict) else {}
    source = (
        detail.get("preferred_source")
        if isinstance(detail.get("preferred_source"), dict)
        else {}
    )
    passages: list[dict[str, Any]] = []
    for item in evidence[:5]:
        passage = " ".join(str(item.get("evidence_passage") or "").split())
        if not passage:
            continue
        passages.append(
            {
                "document_type": str(item.get("form") or item.get("source_type") or "")[:80],
                "item_section": str(item.get("item_section") or "")[:120],
                "published_at": item.get("source_published_at") or item.get("filing_date"),
                "passage": passage[:6000],
            }
        )
    return normalize_qwen_risk_content({
        "as_of": event.get("last_updated_at"),
        "event_date": event.get("event_date"),
        "headline": " ".join(
            str(source.get("title") or facts.get("source_title") or "").split()
        )[:500],
        "summary": " ".join(
            str(source.get("summary") or facts.get("source_summary") or "").split()
        )[:2000],
        "passages": passages,
    })


def build_qwen_risk_input_contract(
    detail: dict[str, Any],
    evidence: list[dict[str, Any]],
    *,
    model_version: str,
) -> dict[str, Any]:
    """Bind one semantic input to the exact current source/evidence identity.

    Event version alone is insufficient: a source revision or evidence-relation
    repair can change what the model saw without incrementing that version.  The
    identity hashes below make those changes invalidate both the shadow cache
    and any later public projection.
    """

    content = build_qwen_risk_input(detail, evidence)
    source = (
        detail.get("preferred_source")
        if isinstance(detail.get("preferred_source"), dict)
        else {}
    )
    source_identity = {
        key: source.get(key)
        for key in (
            "observation_id",
            "source_id",
            "external_id",
            "canonical_url",
            "content_sha256",
            "raw_payload_sha256",
            "latest_revision_no",
            "latest_revision_kind",
            "latest_revision_at",
            "source_published_at",
            "local_received_at",
        )
    }
    evidence_identity: list[dict[str, Any]] = []
    for item in evidence[:5]:
        passage = " ".join(str(item.get("evidence_passage") or "").split())
        evidence_identity.append(
            {
                "evidence_id": item.get("evidence_id"),
                "evidence_fingerprint": item.get("evidence_fingerprint"),
                "relation_event_version": item.get("relation_event_version"),
                "relation_status": item.get("relation_status"),
                "subject_match": item.get("subject_match"),
                "event_claim_supported": item.get("event_claim_supported"),
                "date_coherent": item.get("date_coherent"),
                "modality": item.get("modality"),
                "passage_sha256": _sha256(passage),
            }
        )
    evidence_identity.sort(
        key=lambda item: (
            str(item.get("evidence_id") or ""),
            str(item.get("evidence_fingerprint") or ""),
        )
    )
    evidence_context = derive_evidence_context(evidence)
    scope = assessment_scope(str(evidence_context.get("state") or ""))
    input_sufficient = bool(
        str(content.get("headline") or "").strip()
        or str(content.get("summary") or "").strip()
        or any(str(item.get("passage") or "").strip() for item in content.get("passages") or [])
    )
    identity = {
        "source": source_identity,
        "evidence": evidence_identity,
    }
    payload = {
        "model_task": QWEN_RISK_MODEL_TASK,
        "contract_version": QWEN_RISK_CONTRACT_VERSION,
        "prompt_version": QWEN_RISK_PROMPT_VERSION,
        "model_version": model_version,
        "assessment_scope": scope,
        "content": content,
        "input_identity": identity,
        "input_sufficient": input_sufficient,
    }
    return {
        **payload,
        "input_sha256": _sha256(_stable_json(payload)),
        "source_identity_sha256": _sha256(_stable_json(source_identity)),
        "evidence_identity_sha256": _sha256(_stable_json(evidence_identity)),
        "evidence_context_sha256": _sha256(
            _stable_json({"context": evidence_context, "identity": evidence_identity})
        ),
    }


@dataclass(frozen=True)
class QwenRiskModelProvider:
    base_url: str
    model: str
    adapter_sha256: str
    timeout_seconds: float = 30.0
    max_tokens: int = 180
    request_fn: Callable[..., Any] = httpx.post

    def __post_init__(self) -> None:
        if not _is_loopback_url(self.base_url):
            raise ValueError("Qwen risk model URL must be an HTTP loopback address")
        if not self.model.strip():
            raise ValueError("Qwen risk model name is required")
        digest = self.adapter_sha256.strip().casefold()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("Qwen risk adapter SHA-256 is required")
        if not 64 <= int(self.max_tokens) <= 512:
            raise ValueError("Qwen risk max_tokens must stay between 64 and 512")

    @property
    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        return base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")

    @property
    def model_version(self) -> str:
        return "qwen-risk-" + self.adapter_sha256.strip().casefold()[:16]

    @staticmethod
    def response_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "materiality",
                "polarity",
                "adverse_strength",
                "semantic_priority",
            ],
            "properties": {
                "materiality": {
                    "type": "string",
                    "enum": ["MATERIAL_ADVERSE", "NOT_MATERIAL_ADVERSE", "UNCLEAR"],
                },
                "polarity": {
                    "type": "string",
                    "enum": ["ADVERSE", "POSITIVE", "NEUTRAL", "MIXED", "UNCLEAR"],
                },
                "adverse_strength": {
                    "type": "string",
                    "enum": ["HIGH", "LOW", "NONE", "UNCLEAR"],
                },
                "semantic_priority": {
                    "type": "string",
                    "enum": ["PRIORITY_REVIEW", "ROUTINE", "UNDECIDABLE"],
                },
            },
        }

    def predict_content(self, content: dict[str, Any]) -> tuple[dict[str, str], float]:
        """Return one strict semantic prediction for canonicalized content."""

        normalized = normalize_qwen_risk_content(content)
        request_payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": int(self.max_tokens),
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "finance_radar_qwen_risk_semantics",
                    "strict": True,
                    "schema": self.response_schema(),
                },
            },
            "messages": [
                {"role": "system", "content": QWEN_RISK_SYSTEM_PROMPT},
                {"role": "user", "content": _stable_json(normalized)},
            ],
        }
        started = time.perf_counter()
        try:
            response = self.request_fn(
                self.endpoint,
                json=request_payload,
                timeout=max(1.0, min(float(self.timeout_seconds), 120.0)),
            )
            if hasattr(response, "raise_for_status"):
                response.raise_for_status()
            body = response.json() if hasattr(response, "json") else response
            value = body["choices"][0]["message"]["content"]
            raw = value if isinstance(value, dict) else json.loads(value)
        except (
            httpx.HTTPError,
            TimeoutError,
            OSError,
            KeyError,
            IndexError,
            TypeError,
            json.JSONDecodeError,
        ) as exc:
            raise QwenRiskContractError("QWEN_RISK_MODEL_REQUEST_FAILED") from exc
        issues = validate_semantic_payload(raw)
        if issues:
            raise QwenRiskContractError("QWEN_RISK_INVALID_OUTPUT:" + ",".join(issues))
        return ({key: str(value) for key, value in raw.items()}, (time.perf_counter() - started) * 1000)

    def input_contract(
        self, detail: dict[str, Any], evidence: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return build_qwen_risk_input_contract(
            detail,
            evidence,
            model_version=self.model_version,
        )

    def assess(
        self, detail: dict[str, Any], evidence: list[dict[str, Any]]
    ) -> dict[str, Any]:
        contract = self.input_contract(detail, evidence)
        if contract.get("input_sufficient") is not True:
            raise QwenRiskContractError("QWEN_RISK_INPUT_INSUFFICIENT")
        raw, latency_ms = self.predict_content(contract["content"])
        prediction, decision_source, hybrid_rule = apply_qwen_hybrid_anchor(
            contract["content"], raw
        )
        event = detail.get("event") if isinstance(detail.get("event"), dict) else {}
        return {
            **prediction,
            **contract,
            "label": prediction["semantic_priority"],
            "confidence": 0.0,
            "confidence_applicable": False,
            "event_version": int(event.get("current_version") or 0),
            "event_status": str(event.get("status") or "unknown"),
            "decision_source": decision_source,
            "hybrid_policy_version": QWEN_HYBRID_POLICY_VERSION,
            "hybrid_rule": hybrid_rule,
            "training_basis": "DUAL_REVIEW_AI_CONSENSUS",
            "call_kind": QWEN_RISK_MODEL_TASK,
            "semantic_model_invoked": True,
            "conditional_language_required": contract["assessment_scope"] == "SOURCE_CONDITIONAL",
            "adapter_sha256": self.adapter_sha256.strip().casefold(),
            "latency_ms": round(latency_ms, 3),
            "shadow": True,
            "no_trading": True,
        }
