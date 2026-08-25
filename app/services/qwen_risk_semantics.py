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
    validate_semantic_payload,
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
    return {
        "as_of": event.get("last_updated_at"),
        "event_date": event.get("event_date"),
        "headline": " ".join(
            str(source.get("title") or facts.get("source_title") or "").split()
        )[:500],
        "summary": " ".join(
            str(source.get("summary") or facts.get("source_summary") or "").split()
        )[:2000],
        "passages": passages,
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

    def input_contract(
        self, detail: dict[str, Any], evidence: list[dict[str, Any]]
    ) -> dict[str, Any]:
        content = build_qwen_risk_input(detail, evidence)
        evidence_context = derive_evidence_context(evidence)
        scope = assessment_scope(str(evidence_context.get("state") or ""))
        payload = {
            "model_task": QWEN_RISK_MODEL_TASK,
            "contract_version": QWEN_RISK_CONTRACT_VERSION,
            "prompt_version": QWEN_RISK_PROMPT_VERSION,
            "model_version": self.model_version,
            "assessment_scope": scope,
            "content": content,
        }
        return {
            **payload,
            "input_sha256": _sha256(_stable_json(payload)),
            "evidence_context_sha256": _sha256(_stable_json(evidence_context)),
        }

    def assess(
        self, detail: dict[str, Any], evidence: list[dict[str, Any]]
    ) -> dict[str, Any]:
        contract = self.input_contract(detail, evidence)
        response_schema = {
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
        request_payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": int(self.max_tokens),
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "finance_radar_qwen_risk_semantics",
                    "strict": True,
                    "schema": response_schema,
                },
            },
            "messages": [
                {"role": "system", "content": QWEN_RISK_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _stable_json(contract["content"]),
                },
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
            content = body["choices"][0]["message"]["content"]
            raw = content if isinstance(content, dict) else json.loads(content)
        except (httpx.HTTPError, TimeoutError, OSError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise QwenRiskContractError("QWEN_RISK_MODEL_REQUEST_FAILED") from exc
        issues = validate_semantic_payload(raw)
        if issues:
            raise QwenRiskContractError("QWEN_RISK_INVALID_OUTPUT:" + ",".join(issues))
        event = detail.get("event") if isinstance(detail.get("event"), dict) else {}
        return {
            **raw,
            **contract,
            "label": raw["semantic_priority"],
            "confidence": 0.0,
            "confidence_applicable": False,
            "event_version": int(event.get("current_version") or 0),
            "event_status": str(event.get("status") or "unknown"),
            "decision_source": "HUMAN_GOLD_TRAINED_QWEN",
            "call_kind": QWEN_RISK_MODEL_TASK,
            "semantic_model_invoked": True,
            "conditional_language_required": contract["assessment_scope"] == "SOURCE_CONDITIONAL",
            "adapter_sha256": self.adapter_sha256.strip().casefold(),
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "shadow": True,
            "no_trading": True,
        }
