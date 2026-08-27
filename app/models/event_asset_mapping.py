"""Deterministic, read-only event-to-market-observation mapping.

The mapper answers only which instruments are useful for observing a reported
event.  It never predicts direction, magnitude, or a trade.  A separate
evidence/time-anchor gate decides whether a mapped relation may schedule market
data work.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Pattern


ROOT = Path(__file__).resolve().parents[2]
MAPPING_PATH = ROOT / "config" / "event_asset_mapping_v1.json"
MAPPING_SCHEMA_VERSION = 1

ALLOWED_RELATION_TYPES = frozenset(
    {"PRIMARY", "SECTOR", "SUPPLIER", "CUSTOMER", "MACRO_PROXY", "ECOSYSTEM_PROXY"}
)
ALLOWED_ROLES = frozenset(
    {"DIRECT_SECURITY", "MARKET_BENCHMARK", "SECTOR_PROXY", "THEMATIC_PROXY"}
)
TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")
EXCHANGE_TICKER_PATTERN = re.compile(
    r"\b(NEW\s+YORK\s+STOCK\s+EXCHANGE|NYSE(?:\s+AMERICAN)?|"
    r"NASDAQ(?:\s+(?:CAPITAL|GLOBAL)(?:\s+SELECT)?\s+MARKET)?)"
    r"\s*[:\-]\s*([A-Z][A-Z0-9.-]{0,14})\b",
    re.IGNORECASE,
)
GENERIC_COMPANY_TOKENS = frozenset(
    {"company", "corporation", "corp", "inc", "incorporated", "limited", "ltd", "holdings"}
)

TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "policy_version",
        "max_assets_per_event",
        "direction",
        "impact_score",
        "no_trading",
        "asset_templates",
        "asset_registry",
        "rules",
    }
)
ASSET_FIELDS = frozenset(
    {
        "asset_type",
        "symbol",
        "provider_symbol",
        "venue",
        "currency",
        "relation_type",
        "role",
        "proxy_label",
        "confidence",
    }
)
TEMPLATE_FIELDS = frozenset(
    {
        "asset_type",
        "currency",
        "relation_type",
        "role",
        "proxy_label",
        "confidence",
    }
)
RULE_FIELDS = frozenset({"id", "priority", "match", "assets"})
MATCH_FIELDS = frozenset(
    {
        "company_ticker",
        "company_name_required",
        "event_family_patterns",
        "event_type_patterns",
        "any_patterns",
        "all_pattern_groups",
    }
)

TEXT_FIELDS = (
    "event_family",
    "event_type",
    "title",
    "summary",
    "subject_name",
    "company_name",
    "source_title",
    "source_summary",
    "evidence_excerpt",
    "public_fact_summary",
    "claim_subject",
    "claim_action",
    "claim_stage",
)


@dataclass(frozen=True)
class AssetDefinition:
    asset_type: str
    symbol: str
    provider_symbol: str
    venue: str
    currency: str
    relation_type: str
    role: str
    proxy_label: str
    confidence: float


@dataclass(frozen=True)
class AssetTemplate:
    asset_type: str
    currency: str
    relation_type: str
    role: str
    proxy_label: str
    confidence: float


@dataclass(frozen=True)
class MappingRule:
    id: str
    priority: int
    company_ticker: bool
    company_name_required: bool
    event_family_patterns: tuple[Pattern[str], ...]
    event_type_patterns: tuple[Pattern[str], ...]
    any_patterns: tuple[Pattern[str], ...]
    all_pattern_groups: tuple[tuple[Pattern[str], ...], ...]
    assets: tuple[str, ...]


@dataclass(frozen=True)
class AssetMappingPolicy:
    policy_version: str
    policy_sha256: str
    max_assets_per_event: int
    direction: str
    impact_score: int
    no_trading: int
    ticker_template: AssetTemplate
    asset_registry: Mapping[str, AssetDefinition]
    rules: tuple[MappingRule, ...]


def _expect_object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _reject_unknown_fields(value: Mapping[str, Any], allowed: frozenset[str], *, label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {', '.join(unknown)}")


def _required_text(value: Mapping[str, Any], field: str, *, label: str) -> str:
    text = str(value.get(field) or "").strip()
    if not text:
        raise ValueError(f"{label}.{field} must be a non-empty string")
    return text


def _confidence(value: Mapping[str, Any], *, label: str) -> float:
    try:
        result = float(value.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}.confidence must be numeric") from exc
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"{label}.confidence must be between 0 and 1")
    return result


def _relation_and_role(value: Mapping[str, Any], *, label: str) -> tuple[str, str]:
    relation_type = _required_text(value, "relation_type", label=label)
    role = _required_text(value, "role", label=label)
    if relation_type not in ALLOWED_RELATION_TYPES:
        raise ValueError(f"{label}.relation_type is unsupported: {relation_type}")
    if role not in ALLOWED_ROLES:
        raise ValueError(f"{label}.role is unsupported: {role}")
    return relation_type, role


def _parse_asset(symbol_key: str, raw: Any) -> AssetDefinition:
    label = f"asset_registry.{symbol_key}"
    value = _expect_object(raw, label=label)
    _reject_unknown_fields(value, ASSET_FIELDS, label=label)
    symbol = _required_text(value, "symbol", label=label).upper()
    if symbol != symbol_key or not TICKER_PATTERN.fullmatch(symbol):
        raise ValueError(f"{label}.symbol must equal its valid uppercase registry key")
    provider_symbol = _required_text(value, "provider_symbol", label=label).upper()
    if not TICKER_PATTERN.fullmatch(provider_symbol):
        raise ValueError(f"{label}.provider_symbol is invalid")
    relation_type, role = _relation_and_role(value, label=label)
    return AssetDefinition(
        asset_type=_required_text(value, "asset_type", label=label),
        symbol=symbol,
        provider_symbol=provider_symbol,
        venue=_required_text(value, "venue", label=label),
        currency=_required_text(value, "currency", label=label),
        relation_type=relation_type,
        role=role,
        proxy_label=_required_text(value, "proxy_label", label=label),
        confidence=_confidence(value, label=label),
    )


def _parse_template(name: str, raw: Any) -> AssetTemplate:
    label = f"asset_templates.{name}"
    value = _expect_object(raw, label=label)
    _reject_unknown_fields(value, TEMPLATE_FIELDS, label=label)
    relation_type, role = _relation_and_role(value, label=label)
    return AssetTemplate(
        asset_type=_required_text(value, "asset_type", label=label),
        currency=_required_text(value, "currency", label=label),
        relation_type=relation_type,
        role=role,
        proxy_label=_required_text(value, "proxy_label", label=label),
        confidence=_confidence(value, label=label),
    )


def _compile_patterns(raw: Any, *, label: str) -> tuple[Pattern[str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{label} must be a non-empty list")
    compiled: list[Pattern[str]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{label}[{index}] must be a non-empty string")
        try:
            compiled.append(re.compile(item, re.IGNORECASE))
        except re.error as exc:
            raise ValueError(f"{label}[{index}] is not a valid regular expression") from exc
    return tuple(compiled)


def _optional_boolean(value: Mapping[str, Any], field: str, *, label: str) -> bool:
    if field not in value:
        return False
    result = value[field]
    if not isinstance(result, bool):
        raise ValueError(f"{label}.{field} must be a boolean")
    return result


def _parse_rule(
    raw: Any,
    *,
    registry: Mapping[str, AssetDefinition],
    max_assets: int,
) -> MappingRule:
    value = _expect_object(raw, label="mapping rule")
    _reject_unknown_fields(value, RULE_FIELDS, label="mapping rule")
    rule_id = _required_text(value, "id", label="mapping rule")
    try:
        priority = int(value.get("priority"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"mapping rule {rule_id}.priority must be an integer") from exc
    match = _expect_object(value.get("match"), label=f"mapping rule {rule_id}.match")
    _reject_unknown_fields(match, MATCH_FIELDS, label=f"mapping rule {rule_id}.match")
    company_ticker = _optional_boolean(
        match, "company_ticker", label=f"mapping rule {rule_id}.match"
    )
    company_name_required = _optional_boolean(
        match, "company_name_required", label=f"mapping rule {rule_id}.match"
    )
    if company_name_required and not company_ticker:
        raise ValueError(f"mapping rule {rule_id} requires a ticker before a company name")

    event_family_patterns = _compile_patterns(
        match.get("event_family_patterns"),
        label=f"mapping rule {rule_id}.event_family_patterns",
    )
    event_type_patterns = _compile_patterns(
        match.get("event_type_patterns"), label=f"mapping rule {rule_id}.event_type_patterns"
    )
    any_patterns = _compile_patterns(
        match.get("any_patterns"), label=f"mapping rule {rule_id}.any_patterns"
    )
    raw_groups = match.get("all_pattern_groups")
    groups: tuple[tuple[Pattern[str], ...], ...] = ()
    if raw_groups is not None:
        if not isinstance(raw_groups, list) or not raw_groups:
            raise ValueError(f"mapping rule {rule_id}.all_pattern_groups must be non-empty")
        groups = tuple(
            _compile_patterns(group, label=f"mapping rule {rule_id}.all_pattern_groups[{index}]")
            for index, group in enumerate(raw_groups)
        )
    if (
        not company_ticker
        and not event_family_patterns
        and not event_type_patterns
        and not any_patterns
        and not groups
    ):
        raise ValueError(f"mapping rule {rule_id} has no match condition")

    asset_refs = value.get("assets")
    if not isinstance(asset_refs, list) or not asset_refs:
        raise ValueError(f"mapping rule {rule_id}.assets must be a non-empty list")
    if len(asset_refs) > max_assets:
        raise ValueError(f"mapping rule {rule_id} exceeds max_assets_per_event")
    normalized_refs: list[str] = []
    for index, item in enumerate(asset_refs):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"mapping rule {rule_id}.assets[{index}] is invalid")
        reference = item.strip().upper()
        if reference == "$TICKER":
            if not company_ticker:
                raise ValueError(f"mapping rule {rule_id} uses $TICKER without a ticker match")
        elif reference not in registry:
            raise ValueError(f"mapping rule {rule_id} references unknown asset {reference}")
        normalized_refs.append(reference)
    return MappingRule(
        id=rule_id,
        priority=priority,
        company_ticker=company_ticker,
        company_name_required=company_name_required,
        event_family_patterns=event_family_patterns,
        event_type_patterns=event_type_patterns,
        any_patterns=any_patterns,
        all_pattern_groups=groups,
        assets=tuple(normalized_refs),
    )


@lru_cache(maxsize=8)
def load_asset_mapping_policy(path: str | None = None) -> AssetMappingPolicy:
    """Load one strict, content-addressed asset-mapping policy."""

    source = Path(path) if path else MAPPING_PATH
    raw_bytes = source.read_bytes()
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("asset mapping policy must be valid UTF-8 JSON") from exc
    document = _expect_object(payload, label="asset mapping policy")
    _reject_unknown_fields(document, TOP_LEVEL_FIELDS, label="asset mapping policy")
    if document.get("schema_version") != MAPPING_SCHEMA_VERSION:
        raise ValueError("unsupported asset mapping schema_version")
    policy_version = _required_text(document, "policy_version", label="asset mapping policy")
    try:
        max_assets = int(document.get("max_assets_per_event"))
    except (TypeError, ValueError) as exc:
        raise ValueError("max_assets_per_event must be an integer") from exc
    if not 1 <= max_assets <= 3:
        raise ValueError("max_assets_per_event must be between 1 and 3")
    if document.get("direction") != "ABSTAIN":
        raise ValueError("asset mapping direction must be ABSTAIN")
    if type(document.get("impact_score")) is not int or document.get("impact_score") != 0:
        raise ValueError("asset mapping impact_score must be zero")
    if type(document.get("no_trading")) is not int or document.get("no_trading") != 1:
        raise ValueError("asset mapping no_trading must equal one")

    templates = _expect_object(document.get("asset_templates"), label="asset_templates")
    if set(templates) != {"TICKER"}:
        raise ValueError("asset_templates must contain exactly TICKER")
    ticker_template = _parse_template("TICKER", templates["TICKER"])
    if ticker_template.relation_type != "PRIMARY" or ticker_template.role != "DIRECT_SECURITY":
        raise ValueError("TICKER template must be a PRIMARY DIRECT_SECURITY")

    raw_registry = _expect_object(document.get("asset_registry"), label="asset_registry")
    if not raw_registry:
        raise ValueError("asset_registry must not be empty")
    registry = {
        str(symbol).strip().upper(): _parse_asset(str(symbol).strip().upper(), raw)
        for symbol, raw in raw_registry.items()
    }
    raw_rules = document.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("asset mapping rules must be a non-empty list")
    rules = tuple(
        _parse_rule(raw, registry=registry, max_assets=max_assets) for raw in raw_rules
    )
    identifiers = [rule.id for rule in rules]
    priorities = [rule.priority for rule in rules]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("asset mapping rule ids must be unique")
    if priorities != sorted(priorities) or len(set(priorities)) != len(priorities):
        raise ValueError("asset mapping rule priorities must be unique and increasing")

    return AssetMappingPolicy(
        policy_version=policy_version,
        policy_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        max_assets_per_event=max_assets,
        direction="ABSTAIN",
        impact_score=0,
        no_trading=1,
        ticker_template=ticker_template,
        asset_registry=registry,
        rules=rules,
    )


def _facts(event: Mapping[str, Any]) -> Mapping[str, Any]:
    candidates = [event.get("facts")]
    current = event.get("current_version")
    if isinstance(current, Mapping):
        candidates.append(current.get("facts"))
    candidates.append(event.get("facts_json"))
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            return candidate
        if isinstance(candidate, str) and candidate.strip():
            try:
                decoded = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                return decoded
    return {}


def _event_text(event: Mapping[str, Any]) -> str:
    facts = _facts(event)
    values: list[str] = []
    for field in TEXT_FIELDS:
        for source in (event, facts):
            value = source.get(field)
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
    combined = " ".join(values).casefold()
    return re.sub(r"[^\w]+", " ", combined, flags=re.UNICODE).strip()


def _ticker(event: Mapping[str, Any]) -> str | None:
    facts = _facts(event)
    for source in (event, facts):
        value = str(source.get("ticker_at_event") or source.get("ticker") or "").strip().upper()
        if value and TICKER_PATTERN.fullmatch(value):
            return value
    explicit = _explicit_exchange_ticker(event)
    return explicit[0] if explicit is not None else None


def _explicit_exchange_ticker(event: Mapping[str, Any]) -> tuple[str, str] | None:
    """Read a ticker only from an exchange-qualified source capture.

    A bare uppercase token in news text is never sufficient.  When a company
    name is available, the same captured field must also mention a distinctive
    company-name token (or the company name itself is the ticker).
    """

    company_name = _company_name(event)
    normalized_company = re.sub(r"[^a-z0-9]+", " ", company_name.casefold()).strip()
    company_tokens = [
        token
        for token in normalized_company.split()
        if len(token) >= 4 and token not in GENERIC_COMPANY_TOKENS
    ]
    for field in ("source_title", "source_summary"):
        text = str(event.get(field) or "").strip()
        if not text:
            continue
        for match in EXCHANGE_TICKER_PATTERN.finditer(text):
            ticker = match.group(2).upper()
            if not TICKER_PATTERN.fullmatch(ticker):
                continue
            normalized_text = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
            captured_tokens = set(normalized_text.split())
            company_matches = company_name.upper() == ticker or any(
                token in captured_tokens for token in company_tokens
            )
            if not company_matches:
                continue
            venue = re.sub(r"\s+", " ", match.group(1)).upper()
            if venue == "NEW YORK STOCK EXCHANGE":
                venue = "NYSE"
            elif venue.startswith("NASDAQ"):
                venue = "NASDAQ"
            return ticker, venue
    return None


def _company_name(event: Mapping[str, Any]) -> str:
    facts = _facts(event)
    return str(event.get("company_name") or facts.get("company_name") or "").strip()


def _rule_matches(rule: MappingRule, event: Mapping[str, Any], *, text: str) -> bool:
    if rule.company_ticker:
        if _ticker(event) is None:
            return False
        if rule.company_name_required and not _company_name(event):
            return False
    event_family = re.sub(
        r"[^a-z0-9]+", " ", str(event.get("event_family") or "").casefold()
    )
    if rule.event_family_patterns and not any(
        pattern.search(event_family) for pattern in rule.event_family_patterns
    ):
        return False
    event_type = re.sub(r"[^a-z0-9]+", " ", str(event.get("event_type") or "").casefold())
    if rule.event_type_patterns and not any(pattern.search(event_type) for pattern in rule.event_type_patterns):
        return False
    if rule.any_patterns and not any(pattern.search(text) for pattern in rule.any_patterns):
        return False
    if any(not any(pattern.search(text) for pattern in group) for group in rule.all_pattern_groups):
        return False
    return True


def _direct_asset(event: Mapping[str, Any], template: AssetTemplate) -> AssetDefinition:
    ticker = _ticker(event)
    if ticker is None:
        raise ValueError("direct asset requested without a valid ticker")
    facts = _facts(event)
    venue = str(
        event.get("exchange")
        or event.get("venue")
        or facts.get("exchange")
        or facts.get("venue")
        or ""
    ).strip()
    if not venue:
        explicit = _explicit_exchange_ticker(event)
        venue = explicit[1] if explicit is not None else ""
    return AssetDefinition(
        asset_type=template.asset_type,
        symbol=ticker,
        provider_symbol=ticker,
        venue=venue,
        currency=template.currency,
        relation_type=template.relation_type,
        role=template.role,
        proxy_label=template.proxy_label,
        confidence=template.confidence,
    )


def resolve_event_assets(
    event: Mapping[str, Any],
    *,
    policy: AssetMappingPolicy | None = None,
    config_path: str | None = None,
) -> list[dict[str, Any]]:
    """Return at most three de-duplicated, directionless observation relations.

    Rules are intentionally first-match-wins.  This keeps an energy-specific
    geopolitical rule from being diluted by the broader conflict fallback and
    prevents a company event from accumulating unrelated macro proxies.
    """

    if policy is not None and config_path is not None:
        raise ValueError("provide either policy or config_path, not both")
    selected_policy = policy or load_asset_mapping_policy(config_path)
    text = _event_text(event)
    matched_rule = next(
        (rule for rule in selected_policy.rules if _rule_matches(rule, event, text=text)),
        None,
    )
    if matched_rule is None:
        return []

    selected: list[AssetDefinition] = []
    # A reviewed ticker can itself be one of the configured benchmark ETFs
    # (for example SPY).  Venue/provider metadata must not turn that one
    # instrument into two event relations.
    seen: set[str] = set()
    for reference in matched_rule.assets:
        asset = (
            _direct_asset(event, selected_policy.ticker_template)
            if reference == "$TICKER"
            else selected_policy.asset_registry[reference]
        )
        identity = asset.provider_symbol
        if identity in seen:
            continue
        seen.add(identity)
        selected.append(asset)
        if len(selected) >= selected_policy.max_assets_per_event:
            break

    output: list[dict[str, Any]] = []
    for rank, asset in enumerate(selected, start=1):
        output.append(
            {
                "asset_type": asset.asset_type,
                "symbol": asset.symbol,
                "provider_symbol": asset.provider_symbol,
                "venue": asset.venue,
                "currency": asset.currency,
                "relation_type": asset.relation_type,
                "direction": selected_policy.direction,
                "impact_score": selected_policy.impact_score,
                "confidence": asset.confidence,
                "no_trading": selected_policy.no_trading,
                "rule_id": matched_rule.id,
                "policy_version": selected_policy.policy_version,
                "policy_sha256": selected_policy.policy_sha256,
                "rank": rank,
                "role": asset.role,
                "proxy_label": asset.proxy_label,
                "reason_codes": [
                    f"RULE:{matched_rule.id}",
                    f"POLICY:{selected_policy.policy_version}",
                    f"ROLE:{asset.role}",
                    *(
                        ["SOURCE_EXCHANGE_TICKER"]
                        if asset.role == "DIRECT_SECURITY"
                        and not str(event.get("ticker_at_event") or "").strip()
                        else []
                    ),
                ],
            }
        )
    return output
