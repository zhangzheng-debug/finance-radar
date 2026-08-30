"""High-precision semantic anchors used by the production Qwen hybrid.

The trained adapter handles ordinary language.  These deliberately narrow
rules cover a small set of mechanisms where the frozen validation showed that
an exact financial/legal phrase was more reliable than the adapter alone.
They never inspect evidence state, price outcomes, or prior model output.
"""

from __future__ import annotations

import re
from typing import Any

from app.models.qwen_risk_contract import (
    expected_semantic_payload,
    normalize_qwen_risk_content,
)


QWEN_HYBRID_POLICY_VERSION = "qwen-v3-narrow-anchors-v1"


def _pattern(value: str) -> re.Pattern[str]:
    return re.compile(value, re.I | re.S)


_NEUTRAL = ("NOT_MATERIAL_ADVERSE", "NEUTRAL")
_POSITIVE = ("NOT_MATERIAL_ADVERSE", "POSITIVE")
_LOW_ADVERSE = ("NOT_MATERIAL_ADVERSE", "ADVERSE")
_PRIORITY = ("MATERIAL_ADVERSE", "ADVERSE")


CONTRAST_RULES: tuple[tuple[str, tuple[str, str], re.Pattern[str]], ...] = (
    (
        "paid_or_completed_listing_exit",
        _NEUTRAL,
        _pattern(
            r"\b(?:form\s*25|25-nse|delist).{0,1800}(?:per share in cash|merger consideration|"
            r"acquisition (?:was |has been )?completed|cash merger|received .{0,80}cash consideration)\b"
        ),
    ),
    (
        "resolved_financial_or_listing_risk",
        _POSITIVE,
        _pattern(
            r"\b(?:substantial doubt.{0,220}(?:is|was|has been) alleviated|"
            r"(?:has|had|successfully) regained compliance|compliance (?:has been|was) restored|"
            r"no longer (?:subject to|at risk of) delist|cured (?:the )?(?:default|breach))\b"
        ),
    ),
    (
        "hypothetical_liquidation_or_default",
        _NEUTRAL,
        _pattern(
            r"\b(?:if|should|could|may) .{0,220}(?:unable to consummate|fail to complete|"
            r"be required to liquidate|constitute an event of default).{0,260}"
            r"(?:business combination|trust account|liquidat|agreement|indenture)\b"
        ),
    ),
    (
        "spac_going_concern_is_lifecycle_risk",
        _NEUTRAL,
        _pattern(
            r"\b(?:blank check|acquisition corp|initial business combination|trust account)\b"
            r".{0,1800}\bsubstantial doubt.{0,180}(?:going concern|ability to continue)\b|"
            r"\bsubstantial doubt.{0,180}(?:going concern|ability to continue)\b"
            r".{0,1800}\b(?:blank check|acquisition corp|initial business combination|trust account)\b"
        ),
    ),
    (
        "contract_definition_not_realized",
        _NEUTRAL,
        _pattern(
            r"\b(?:the term [\"“]?default[\"”]? means|events? of default include|"
            r"for purposes? of this .{0,100}(?:event of default|covenant breach))\b"
        ),
    ),
)


PRIORITY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "bankruptcy_restructuring_or_equity_cancellation",
        _pattern(
            r"\b(?:filed|commenced|petitioned|entered|confirmed|effective).{0,180}"
            r"chapter\s*(?:7|11)|chapter\s*(?:7|11).{0,260}"
            r"(?:bankruptcy court|plan|petition|cancelled|canceled|discharged)|"
            r"(?:common stock|ordinary shares?|equity interests?).{0,180}"
            r"(?:cancelled|canceled|discharged).{0,100}(?:no recovery|no force and effect)\b"
        ),
    ),
    (
        "capital_exhaustion_or_operating_curtailment",
        _pattern(
            r"\b(?:unable to raise additional capital|if we are unable to raise additional capital|"
            r"failure to obtain (?:additional )?(?:capital|financing)|cash.{0,90}(?:is|will be) insufficient)"
            r".{0,260}(?:reduce|curtail|suspend|cease|continue as a going concern|operations?|obligations?)\b"
        ),
    ),
    (
        "going_concern_or_realized_default",
        _pattern(
            r"\b(?:substantial doubt.{0,160}(?:going concern|ability to continue)|"
            r"missed (?:a )?(?:debt|interest|principal) payment|maturity[- ]default|"
            r"(?:is|was|are|were) in default.{0,120}(?:loan|note|debt|credit)|"
            r"breached .{0,100}(?:financial )?covenant)\b"
        ),
    ),
    (
        "binding_listing_removal_or_suspension",
        _pattern(
            r"(?:^|\n|\")(?:(?:headline|document_type)\"?:\"?)?\s*(?:form\s*)?25(?:\s|-|\"|$)|"
            r"\b(?:notification of removal from listing|delisting determination|determined to delist|"
            r"ordered .{0,70}trading suspended|trading (?:was |is )?suspended|"
            r"scheduled .{0,70}suspension)\b"
        ),
    ),
    (
        "binding_enforcement_fraud_or_restatement",
        _pattern(
            r"\b(?:filed (?:a )?(?:civil )?complaint.{0,160}(?:alleg|fraud|violation)|"
            r"final (?:consent )?judgment|civil (?:monetary )?penalt|disgorgement|"
            r"fraudulent scheme|misappropriat(?:ed|ion)|criminally charged|"
            r"financial statements? should no longer be relied upon|will restate|"
            r"adverse opinion.{0,180}(?:internal control|financial reporting))\b"
        ),
    ),
    (
        "pivotal_clinical_or_serious_safety_failure",
        _pattern(
            r"\b(?:(?:phase\s*3|pivotal).{0,200}(?:did not meet|failed).{0,120}"
            r"(?:primary|key secondary) endpoint|clinical hold|complete response letter|"
            r"may cause serious injury or death|most serious recall type)\b"
        ),
    ),
)


ROUTINE_RULES: tuple[tuple[str, tuple[str, str], re.Pattern[str]], ...] = (
    (
        "clearly_positive_operating_result",
        _POSITIVE,
        _pattern(
            r"\b(?:record revenue|record profit|record throughput|record gold sold|"
            r"beat(?:s|en)? (?:estimates|expectations)|guidance raised|raises? (?:full[- ]year )?guidance|"
            r"successful(?:ly)? met .{0,100}(?:primary|key) endpoint)\b"
        ),
    ),
    (
        "routine_governance_or_administration",
        _NEUTRAL,
        _pattern(
            r"\b(?:annual meeting results?|board re[- ]election|committee appointment|"
            r"appoints? .{0,100}(?:director|chair|cfo|auditor)|planned ceo retirement|"
            r"internal succession|routine form nt|closes? .{0,60}(?:spac )?ipo)\b"
        ),
    ),
    (
        "ordinary_adverse_result",
        _LOW_ADVERSE,
        _pattern(
            r"\b(?:revenue (?:declined|decreased|fell)|net loss (?:increased|widened)|"
            r"lowered (?:full[- ]year )?guidance|missed (?:estimates|expectations))\b"
        ),
    ),
)


def semantic_text(content: dict[str, Any]) -> str:
    normalized = normalize_qwen_risk_content(content)
    pieces = [str(normalized.get("headline") or ""), str(normalized.get("summary") or "")]
    pieces.extend(str(item.get("passage") or "") for item in normalized.get("passages") or [])
    return " ".join(" ".join(piece.split()) for piece in pieces if piece).strip()


def classify_qwen_hybrid_anchor(content: dict[str, Any]) -> tuple[dict[str, str], str] | None:
    normalized = " ".join(semantic_text(content).split())[:30000]
    for name, target, expression in CONTRAST_RULES:
        if expression.search(normalized):
            return expected_semantic_payload(*target), name
    for name, expression in PRIORITY_RULES:
        if expression.search(normalized):
            return expected_semantic_payload(*_PRIORITY), name
    for name, target, expression in ROUTINE_RULES:
        if expression.search(normalized):
            return expected_semantic_payload(*target), name
    return None


def apply_qwen_hybrid_anchor(
    content: dict[str, Any], prediction: dict[str, str]
) -> tuple[dict[str, str], str, str | None]:
    anchored = classify_qwen_hybrid_anchor(content)
    if anchored is None:
        return prediction, "QWEN_ADAPTER", None
    payload, rule = anchored
    return payload, "DETERMINISTIC_HARDCASE_ANCHOR", rule
