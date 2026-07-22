from __future__ import annotations

import re
from dataclasses import asdict, dataclass


GATE_VERSION = "risk-scope-gate-v2"


@dataclass(frozen=True)
class RiskScopeAssessment:
    """Deterministic admission decision in front of the downside-risk model."""

    decision: str
    reason_codes: tuple[str, ...]
    risk_cues: tuple[str, ...]
    positive_cues: tuple[str, ...]
    gate_version: str = GATE_VERSION

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes)
        payload["risk_cues"] = list(self.risk_cues)
        payload["positive_cues"] = list(self.positive_cues)
        return payload


def _pattern(expression: str) -> re.Pattern[str]:
    return re.compile(expression, re.I | re.S)


RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bankruptcy", _pattern(r"\b(?:chapter\s*(?:7|11)|bankrupt(?:cy)?|insolven(?:cy|t)|liquidation|receivership)\b|破产|清算")),
    ("listing_halt", _pattern(r"\b(?:delist(?:ed|ing)?|trading\s+suspension|trading\s+(?:is\s+)?suspended|notice\s+of\s+noncompliance)\b|退市|停牌")),
    ("credit_distress", _pattern(r"\b(?:debt\s+default|default(?:ed|s|ing)?\s+on|missed\s+(?:debt|interest)\s+payment|going\s+concern|covenant\s+breach)\b|债务违约|持续经营疑虑")),
    (
        "enforcement",
        _pattern(
            r"\b(?:(?:sec|cftc|ftc|doj|regulator)\s+(?:charges?|sues?|fines?|orders?|investigates?)|"
            r"enforcement\s+action|cease\s+and\s+desist|civil\s+(?:money\s+)?penalt(?:y|ies)|"
            r"disgorgement|final\s+(?:consent\s+)?judgment|permanent(?:ly)?\s+enjoin|"
            r"complaint\s+(?:alleges?|seeks?|charges?)|fraud(?:ulent)?\s+(?:charges?|scheme)|"
            r"trading\s+ban)\b|执法行动|监管罚款|民事罚款|追缴"
        ),
    ),
    ("product_safety", _pattern(r"\b(?:product|safety|drug|device|vehicle)?\s*recall(?:s|ed|ing)?\b|产品召回|安全召回")),
    ("security_incident", _pattern(r"\b(?:security\s+breach|data\s+breach|cyberattack|ransomware|wallet\s+(?:hack|drain)|funds?\s+(?:stolen|drained)|protocol\s+exploit|bridge\s+exploit|smart\s+contract\s+exploit|zero[- ]day\s+exploit)\b|黑客攻击|资金被盗")),
    ("workforce", _pattern(r"\b(?:mass\s+layoffs?|cut(?:s|ting)?\s+\d[\d,]*\s+jobs?|workforce\s+reduction)\b|大规模裁员")),
    ("negative_guidance", _pattern(r"\b(?:profit\s+warning|guidance\s+(?:cut|lowered|withdrawn)|revenue\s+forecast\s+(?:cut|lowered)|miss(?:es|ed)\s+(?:estimates|expectations))\b|盈利预警|下调(?:业绩)?指引")),
    ("capital_impairment", _pattern(r"\b(?:share\s+dilution|dilutive\s+offering|impairment\s+charge|reverse\s+split)\b|股权稀释|资产减值")),
    ("sanctions_blockade", _pattern(r"\b(?:new\s+sanctions?|sanctions?\s+(?:imposed|announced)|export\s+controls?|shipping\s+disruption|blockade|invasion|airstrike|missile\s+attack)\b|制裁|出口管制|航运中断|封锁|空袭")),
    ("policy_tightening", _pattern(r"\b(?:rate\s+hike|raises?\s+(?:the\s+)?(?:policy\s+)?rate|emergency\s+rate\s+decision)\b|加息")),
)

POSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("record_results", _pattern(r"\b(?:record\s+revenue|record\s+profit|profit\s+growth)\b")),
    ("positive_surprise", _pattern(r"\b(?:beat(?:s|en)?\s+(?:estimates|expectations)|above\s+expectations)\b")),
    ("raised_guidance", _pattern(r"\b(?:guidance\s+raised|raises?\s+(?:full[- ]year\s+)?guidance)\b")),
    ("capital_return", _pattern(r"\b(?:dividend\s+increase|share\s+repurchase|stock\s+buyback)\b")),
    ("approval_award", _pattern(r"\b(?:regulatory\s+approval|contract\s+award|rating\s+upgrade)\b")),
)

FALSE_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ai_security_test_not_operational_breach",
        _pattern(
            r"\b(?:ai\s+(?:model|system)s?|model)s?\b.{0,140}"
            r"\b(?:test(?:ing)?\s+(?:environment|sandbox)|benchmark|evaluation|cheat)\b|"
            r"\b(?:test(?:ing)?\s+(?:environment|sandbox)|benchmark|evaluation)\b.{0,140}"
            r"\b(?:ai\s+(?:model|system)s?|model)s?\b"
        ),
    ),
    (
        "risk_claim_explicitly_negated",
        _pattern(
            r"\b(?:den(?:y|ies|ied)|false|withdrawn|selected\s+in\s+error|has\s+not|did\s+not|no)\b"
            r".{0,100}\b(?:bankrupt(?:cy)?|default(?:ed)?|petition|breach|recall)\b"
        ),
    ),
)


def assess_risk_scope(text: str) -> RiskScopeAssessment:
    """Decide whether text is suitable for the specialized downside-risk router.

    This is deliberately conservative. It does not decide that an event is true;
    it only prevents unrelated, positive, or content-poor text from being forced
    through a model trained on adverse-event examples.
    """

    normalized = " ".join((text or "").replace("_", " ").split())[:20000]
    if not normalized or len(normalized) < 18:
        return RiskScopeAssessment(
            "ABSTAIN_INSUFFICIENT",
            ("empty_or_too_short",),
            (),
            (),
        )

    false_hits = tuple(code for code, pattern in FALSE_RISK_PATTERNS if pattern.search(normalized))
    if false_hits:
        return RiskScopeAssessment("REJECT_NOISE", false_hits, (), ())

    risk_hits = tuple(code for code, pattern in RISK_PATTERNS if pattern.search(normalized))
    positive_hits = tuple(code for code, pattern in POSITIVE_PATTERNS if pattern.search(normalized))
    if len(positive_hits) >= 2 and not risk_hits:
        return RiskScopeAssessment(
            "REJECT_NON_TARGET",
            ("clearly_positive_without_adverse_cue",),
            (),
            positive_hits,
        )
    if risk_hits:
        return RiskScopeAssessment(
            "ADMIT_RISK_SCOPE",
            ("explicit_material_adverse_cue",),
            risk_hits,
            positive_hits,
        )
    return RiskScopeAssessment(
        "ADMIT_CONTEXT",
        ("no_explicit_material_adverse_cue",),
        (),
        positive_hits,
    )
