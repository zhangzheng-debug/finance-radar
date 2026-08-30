"""Shared guards against outcome-derived Qwen supervision.

The guard intentionally distinguishes structured supervision fields from
ordinary source prose.  A source article may legitimately mention a quoted
price or market move; that alone is not rejected.  Machine-style post-event
return fields, price-audit containers and review rationales that explicitly
use a subsequent market reaction are rejected.
"""

from __future__ import annotations

import re
from typing import Any


_WINDOW = r"(?:5m|30m|2h|next[_ -]?close|1d|3d|5d|10d|20d|21d)"
_OUTCOME_METRIC = (
    r"(?:ret(?:urn)?|abnormal[_ -]?return|relative[_ -]?return|"
    r"market[_ -]?return|price[_ -]?(?:change|return|reaction))"
)

# These are supervision/audit containers, not ordinary source facts such as a
# quoted `price`, `close`, or `shares fell` sentence.
PROHIBITED_STRUCTURED_KEYS = frozenset(
    {
        "market_audit",
        "market_outcome",
        "market_outcomes",
        "market_results",
        "market_return",
        "abnormal_return",
        "relative_return",
        "post_event_market",
        "post_event_markets",
        "post_event_price",
        "post_event_prices",
        "post_event_return",
        "post_event_returns",
        "price_audit",
        "price_reaction_audit",
        "reaction_audit",
        "crash_candidate",
        "volume_crash_candidate",
        "volume_ratio",
    }
)
PROHIBITED_WINDOWED_KEY_RE = re.compile(
    rf"^(?:{_OUTCOME_METRIC})[_ -]?{_WINDOW}$",
    re.IGNORECASE,
)
PROHIBITED_WINDOWED_KEY_RE_REVERSED = re.compile(
    rf"^{_WINDOW}[_ -]?(?:{_OUTCOME_METRIC})$",
    re.IGNORECASE,
)

# Machine-style literals are rejected in source payload values.  Natural prose
# like "Shares closed at $10 after the filing" deliberately does not match.
PROHIBITED_SOURCE_LITERAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "post_event_return_threshold",
        re.compile(
            rf"\b{_OUTCOME_METRIC}[_ -]?{_WINDOW}\s*(?:<=|>=|=|:)\s*[+-]?\d",
            re.IGNORECASE,
        ),
    ),
    (
        "post_event_window_return_literal",
        re.compile(
            rf"\b(?:t\s*\+\s*)?{_WINDOW}\s+(?:post[- ]event\s+)?"
            rf"(?:return|price change|market reaction)\s*(?:<=|>=|=|:)\s*[+-]?\d",
            re.IGNORECASE,
        ),
    ),
    (
        "post_event_crash_candidate",
        re.compile(
            r"\b(?:one|three|five|ten|twenty[_ -]?one|1|3|5|10|20|21)"
            r"[_ -]?day[_ -]?crash\s+candidate\b",
            re.IGNORECASE,
        ),
    ),
    (
        "post_event_volume_crash_candidate",
        re.compile(r"\bvolume[_ -]?crash\s+candidate\b", re.IGNORECASE),
    ),
    (
        "post_event_volume_ratio",
        re.compile(r"\bvolume_ratio\s*(?:=|:)\s*[+-]?\d", re.IGNORECASE),
    ),
)

# Review reasons are not source prose.  They must not justify a label with a
# subsequent price reaction, so narrowly targeted natural-language patterns
# are appropriate here, including Chinese rationales.
PROHIBITED_REVIEW_OUTCOME_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "review_post_event_market_outcome_en",
        re.compile(
            r"\b(?:post[- ]event|subsequent|after(?:ward|wards)?)\b.{0,48}"
            r"\b(?:price|share price|stock|return|market reaction)\b.{0,32}"
            r"(?:[+-]?\d+(?:\.\d+)?\s*%|rose|fell|dropped|jumped|gained|lost)",
            re.IGNORECASE,
        ),
    ),
    (
        "review_post_event_market_outcome_en",
        re.compile(
            r"\b(?:price|share price|stock|return|market reaction)\b.{0,48}"
            r"(?:[+-]?\d+(?:\.\d+)?\s*%|rose|fell|dropped|jumped|gained|lost)"
            r".{0,48}\b(?:after|following|subsequent to)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "review_post_event_market_outcome_zh",
        re.compile(
            r"(?:事后|随后|此后|发布后|披露后|消息后|事件后).{0,24}"
            r"(?:股价|价格|市场|收益|回报).{0,24}"
            r"(?:上涨|上升|下跌|下降|暴涨|暴跌|涨幅|跌幅|反应|[+-]?\d+(?:\.\d+)?\s*%)",
            re.IGNORECASE,
        ),
    ),
    (
        "review_post_event_market_outcome_zh",
        re.compile(
            r"(?:股价|价格|市场|收益|回报).{0,24}"
            r"(?:在|于)?(?:事后|随后|此后|发布后|披露后|消息后|事件后).{0,24}"
            r"(?:上涨|上升|下跌|下降|暴涨|暴跌|涨幅|跌幅|反应|[+-]?\d+(?:\.\d+)?\s*%)",
            re.IGNORECASE,
        ),
    ),
)


def _normalized_key(value: Any) -> str:
    return re.sub(r"[\s-]+", "_", str(value).strip().casefold())


def _structured_key_reasons(value: Any) -> list[str]:
    reasons: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = _normalized_key(key)
            if normalized in PROHIBITED_STRUCTURED_KEYS:
                reasons.add("post_event_structured_container")
            if (
                PROHIBITED_WINDOWED_KEY_RE.fullmatch(normalized)
                or PROHIBITED_WINDOWED_KEY_RE_REVERSED.fullmatch(normalized)
            ):
                reasons.add("post_event_structured_metric")
            reasons.update(_structured_key_reasons(child))
    elif isinstance(value, list):
        for child in value:
            reasons.update(_structured_key_reasons(child))
    return sorted(reasons)


def _walk_strings(value: Any) -> list[str]:
    strings: list[str] = []
    if isinstance(value, dict):
        for child in value.values():
            strings.extend(_walk_strings(child))
    elif isinstance(value, list):
        for child in value:
            strings.extend(_walk_strings(child))
    elif isinstance(value, str):
        strings.append(value)
    return strings


def post_event_supervision_reasons(
    value: Any, *, review_reason: bool = False
) -> list[str]:
    """Return stable reason codes for prohibited outcome-derived supervision."""

    reasons = set(_structured_key_reasons(value))
    patterns = list(PROHIBITED_SOURCE_LITERAL_PATTERNS)
    if review_reason:
        patterns.extend(PROHIBITED_REVIEW_OUTCOME_PATTERNS)
    for text in _walk_strings(value):
        for reason, pattern in patterns:
            if pattern.search(text):
                reasons.add(reason)
    return sorted(reasons)
