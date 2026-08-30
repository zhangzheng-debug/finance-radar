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
    expected_semantic_payload,
    normalize_qwen_risk_content,
    validate_semantic_payload,
)
from app.models.qwen_risk_hybrid import (
    QWEN_HYBRID_POLICY_VERSION,
    apply_qwen_hybrid_anchor,
)
from app.models.risk_router import derive_evidence_context


QWEN_RISK_MODEL_TASK = "QWEN_RISK_SEMANTICS"
QWEN_RISK_RUNTIME_INPUT_VERSION = "qwen-risk-runtime-wire-v2"

# The canonical input above remains the audit/SFT identity.  The small CPU-only
# production model receives a separate bounded wire view so one unusually long
# filing cannot monopolize the single inference slot and stall the fair queue.
_RUNTIME_HEADLINE_UNITS = 360
_RUNTIME_SUMMARY_UNITS = 480
_RUNTIME_PASSAGE_UNITS = 760
_RUNTIME_PASSAGE_LIMIT = 2


class QwenRiskContractError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text_units(value: str) -> int:
    """Approximate tokenizer cost without importing the training runtime.

    ASCII text is roughly four characters per token for these finance sources,
    while CJK and other non-ASCII characters can each consume a token.  Counting
    them as four ASCII-equivalent units gives the runtime a conservative bound.
    """

    return sum(1 if ord(character) < 128 else 4 for character in value)


def _take_text_units(value: str, budget: int, *, reverse: bool = False) -> str:
    characters = reversed(value) if reverse else iter(value)
    selected: list[str] = []
    used = 0
    for character in characters:
        cost = 1 if ord(character) < 128 else 4
        if used + cost > budget:
            break
        selected.append(character)
        used += cost
    if reverse:
        selected.reverse()
    return "".join(selected)


def _clip_runtime_text(value: Any, budget: int) -> str:
    normalized = " ".join(str(value or "").split())
    if _text_units(normalized) <= budget:
        return normalized
    separator = " ... "
    remaining = max(1, budget - _text_units(separator))
    head_budget = max(1, int(remaining * 0.78))
    tail_budget = max(1, remaining - head_budget)
    head = _take_text_units(normalized, head_budget).rstrip()
    tail = _take_text_units(normalized, tail_budget, reverse=True).lstrip()
    return head + separator + tail


def build_qwen_risk_runtime_input(content: dict[str, Any]) -> dict[str, Any]:
    """Build the compact, deterministic view sent to the loopback model.

    This does not replace the canonical content stored in ``input_contract``.
    Headline and summary carry the main claim; the first two relevance-ordered
    evidence passages add source context with a head/tail window for qualifiers.
    """

    normalized = normalize_qwen_risk_content(content)
    passages: list[dict[str, Any]] = []
    remaining_passage_units = _RUNTIME_PASSAGE_UNITS
    selected = list(normalized.get("passages") or [])[:_RUNTIME_PASSAGE_LIMIT]
    for index, item in enumerate(selected):
        remaining_slots = len(selected) - index
        if remaining_slots <= 1:
            passage_budget = remaining_passage_units
        else:
            passage_budget = max(
                1,
                remaining_passage_units - (remaining_slots - 1) * 260,
            )
        passage = _clip_runtime_text(item.get("passage"), passage_budget)
        if not passage:
            continue
        passages.append(
            {
                "document_type": str(item.get("document_type") or "")[:80],
                "item_section": str(item.get("item_section") or "")[:120],
                "published_at": item.get("published_at"),
                "passage": passage,
            }
        )
        remaining_passage_units = max(
            0,
            remaining_passage_units - _text_units(passage),
        )
    return {
        "as_of": normalized.get("as_of"),
        "event_date": normalized.get("event_date"),
        "headline": _clip_runtime_text(
            normalized.get("headline"), _RUNTIME_HEADLINE_UNITS
        ),
        "summary": _clip_runtime_text(
            normalized.get("summary"), _RUNTIME_SUMMARY_UNITS
        ),
        "passages": passages,
    }


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
            "required": ["materiality", "polarity"],
            "properties": {
                "materiality": {
                    "type": "string",
                    "enum": ["MATERIAL_ADVERSE", "NOT_MATERIAL_ADVERSE", "UNCLEAR"],
                },
                "polarity": {
                    "type": "string",
                    "enum": ["ADVERSE", "POSITIVE", "NEUTRAL", "MIXED", "UNCLEAR"],
                },
            },
        }

    def predict_content(self, content: dict[str, Any]) -> tuple[dict[str, str], float]:
        """Return one strict semantic prediction for canonicalized content."""

        runtime_input = build_qwen_risk_runtime_input(content)
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
                {"role": "user", "content": _stable_json(runtime_input)},
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
        if not isinstance(raw, dict):
            raise QwenRiskContractError("QWEN_RISK_INVALID_OUTPUT:payload_not_object")
        try:
            prediction = expected_semantic_payload(
                str(raw.get("materiality") or ""),
                str(raw.get("polarity") or ""),
            )
        except ValueError as exc:
            raise QwenRiskContractError(
                "QWEN_RISK_INVALID_OUTPUT:invalid_materiality_or_polarity"
            ) from exc
        issues = validate_semantic_payload(prediction)
        if issues:
            raise QwenRiskContractError("QWEN_RISK_INVALID_OUTPUT:" + ",".join(issues))
        return (prediction, (time.perf_counter() - started) * 1000)

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
        runtime_input = build_qwen_risk_runtime_input(contract["content"])
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
            "runtime_input_version": QWEN_RISK_RUNTIME_INPUT_VERSION,
            "runtime_input_sha256": _sha256(_stable_json(runtime_input)),
            "conditional_language_required": contract["assessment_scope"] == "SOURCE_CONDITIONAL",
            "adapter_sha256": self.adapter_sha256.strip().casefold(),
            "latency_ms": round(latency_ms, 3),
            "shadow": True,
            "no_trading": True,
        }
