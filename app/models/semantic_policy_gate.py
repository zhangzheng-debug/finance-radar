from __future__ import annotations

import re
from dataclasses import asdict, dataclass


SEMANTIC_POLICY_VERSION = "semantic-policy-gate-v1"


@dataclass(frozen=True)
class SemanticPolicyDecision:
    decision: str
    reason_code: str
    gate_version: str = SEMANTIC_POLICY_VERSION

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _pattern(expression: str) -> re.Pattern[str]:
    return re.compile(expression, re.I | re.S)


NON_TARGET_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "routine_sponsor_working_capital_note",
        _pattern(r"\b(?:sponsor .{0,100}working[- ]capital note|non[- ]interest[- ]bearing working[- ]capital note)\b"),
    ),
    (
        "paid_or_continuing_listing_exit",
        _pattern(
            r"\b(?:per share in cash|cash (?:trust )?redemption|pro rata trust|"
            r"underlying .{0,100}(?:shares?|h shares?).{0,100}(?:continued|remained) (?:trading|listed)|"
            r"ordinary shares? (?:continued|remained) (?:trading|listed))\b"
        ),
    ),
    (
        "routine_policy_or_statistics",
        _pattern(
            r"\b(?:consumer price index|producer price index|employment situation|job openings and labor turnover|"
            r"summary of economic projections|fomc statement|minutes of (?:the )?.{0,80}(?:meeting|committee)|"
            r"schedules? .{0,80}(?:results?|release)|research task forces?|distressed or underserved .{0,50}list|"
            r"final rule establishes|proposes? .{0,100}(?:requirements?|amendments?))\b"
        ),
    ),
    (
        "routine_corporate_governance",
        _pattern(
            r"\b(?:annual meeting results?|board re[- ]election|committee appointment|"
            r"appoints? .{0,100}(?:director|chair|cfo|auditor)|planned ceo retirement|internal succession|"
            r"routine form nt|reports? .{0,80}(?:drill results?|nav and leverage)|closes? .{0,60}(?:spac )?ipo)\b"
        ),
    ),
    (
        "clearly_positive_results",
        _pattern(
            r"\b(?:record revenue|record profit|record throughput|record gold sold|stronger .{0,40}performance|"
            r"beat(?:s|en)? (?:estimates|expectations)|guidance raised|share repurchase|stock buyback|"
            r"increases? capacity.{0,100}lowers? .{0,30}margin)\b"
        ),
    ),
    (
        "resolution_or_administrative_action",
        _pattern(
            r"\b(?:terminates? enforcement action|cease[- ]and[- ]desist order .{0,80}terminated|"
            r"grants? .{0,80}whistleblower awards?|rescinds? policy|endorses? .{0,80}proposal)\b"
        ),
    ),
)


RISK_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "bankruptcy_insolvency_or_receivership",
        _pattern(
            r"\b(?:filed .{0,50}chapter\s*(?:7|11)|chapter\s*(?:7|11) (?:case|petition)|"
            r"appointed .{0,50}(?:receiver|administrator)|winding[- ]up|unable to pay (?:its )?debts|"
            r"joint voluntary liquidators?|closed .{0,80}(?:fdic|receiver))\b"
        ),
    ),
    (
        "severe_equity_impairment",
        _pattern(
            r"\b(?:one[- ]for[- ](?:50|60|75|80|100|110|150|200|250|300|400|500|750|3000)|"
            r"1[- ]for[- ](?:50|60|75|80|100|110|150|200|250|300|400|500|750|3000)|"
            r"no recovery.{0,100}(?:common|equity)|(?:common|equity).{0,100}(?:cancelled|canceled).{0,80}no (?:recovery|consideration))\b"
        ),
    ),
    (
        "going_concern_or_default",
        _pattern(
            r"\b(?:substantial doubt.{0,120}(?:going concern|ability to continue)|"
            r"insufficient cash.{0,120}(?:obligations|twelve months|continue)|"
            r"maturity[- ]default|debt default|missed (?:debt|interest) payment|covenant breach)\b"
        ),
    ),
    (
        "forced_listing_loss",
        _pattern(
            r"\b(?:delisting determination|determined to delist|ordered .{0,50}trading suspended|"
            r"trading (?:was |is )?suspended|scheduled .{0,50}suspension|temporary trading suspension)\b"
        ),
    ),
    (
        "binding_enforcement_or_fraud",
        _pattern(
            r"\b(?:filed (?:a )?(?:civil )?complaint|complaint (?:alleges|seeks)|"
            r"final (?:consent )?judgment|civil (?:monetary )?penalt|disgorgement|"
            r"permanently? (?:enjoin|ban)|fraudulent scheme|misappropriat(?:ed|ion))\b"
        ),
    ),
    (
        "serious_product_safety",
        _pattern(
            r"\b(?:most serious recall type|recall as the most serious type|"
            r"may cause serious injury or death|reported .{0,50}serious injuries?)\b"
        ),
    ),
)


def assess_semantic_policy(text: str) -> SemanticPolicyDecision:
    normalized = " ".join((text or "").split())[:30000]
    for code, expression in NON_TARGET_RULES:
        if expression.search(normalized):
            return SemanticPolicyDecision("NON_TARGET", code)
    for code, expression in RISK_RULES:
        if expression.search(normalized):
            return SemanticPolicyDecision("RISK_REVIEW", code)
    return SemanticPolicyDecision("DEFER_TO_MODEL", "no_high_precision_policy_rule")
