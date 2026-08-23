#!/usr/bin/env python3
"""Turn pending live RawObservations into auditable, unverified event candidates."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.risk_scope_gate import assess_risk_scope
from event_ledger import open_ledger, stable_id, stable_json, utc_now


DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_REPORT = ROOT / "reports" / "live_candidate_extraction_latest.md"
DISCOVERY_ADMISSION_CONTRACT = "event-admission-v1"


@dataclass(frozen=True)
class EventRule:
    event_family: str
    event_type: str
    pattern: re.Pattern[str]


def rule(family: str, event_type: str, expression: str) -> EventRule:
    return EventRule(family, event_type, re.compile(expression, re.I))


RULES = (
    rule("distress", "bankruptcy", r"\b(?:chapter\s*11|chapter\s*7|bankrupt(?:cy)?|insolven(?:cy|t)|files?\s+for\s+bankruptcy)\b|破产|清算"),
    rule("listing_status", "delisting", r"\b(?:delist(?:ed|ing)?|notice\s+of\s+noncompliance)\b|退市"),
    rule("security_incident", "hack_or_exploit", r"\b(?:hack(?:ed)?|exploit(?:ed)?|security\s+breach|drain(?:ed)?|stolen\s+funds?)\b|黑客|漏洞攻击"),
    rule("corporate_action", "merger_or_acquisition", r"\b(?:merger|acqui(?:re[sd]?|sition)|takeover|buyout)\b|并购|收购|合并"),
    rule("earnings", "earnings_or_guidance", r"\b(?:earnings|quarterly\s+results?|profit\s+warning|guidance|revenue\s+forecast)\b|财报|业绩预告|盈利预警"),
    rule("regulatory", "regulatory_action", r"\b(?:sec\s+(?:charges?|sues?|investigates?)|regulator\s+(?:fines?|orders?|approves?|rejects?)|antitrust)\b|监管|罚款|反垄断"),
    rule("macro_policy", "monetary_policy", r"\b(?:rate\s+(?:cut|hike|decision)|interest\s+rates?|central\s+bank|federal\s+reserve|\bfed\b|ecb|boj)\b|降息|加息|央行|美联储"),
    rule("macro_policy", "sanctions_or_tariffs", r"\b(?:sanctions?|tariffs?|export\s+controls?)\b|制裁|关税|出口管制"),
    rule("geopolitical", "conflict_or_blockade", r"\b(?:airstrike|missile\s+attack|blockade|invasion|ceasefire|war\s+with|shipping\s+disruption)\b|空袭|封锁|停火|战争"),
    rule("listing_status", "new_listing", r"\b(?:will\s+list|new\s+listing|lists?\s+(?:the\s+)?token|trading\s+will\s+open)\b|上线交易|新币上线"),
    rule("workforce", "mass_layoff", r"\b(?:mass\s+layoffs?|cut(?:s|ting)?\s+\d[\d,]*\s+jobs?|workforce\s+reduction)\b|大规模裁员"),
    rule("macro_data", "inflation_release", r"\b(?:consumer\s+price\s+index|producer\s+price\s+index|inflation\s+(?:report|release)|cpi-u|ppi)\b|消费者价格指数|生产者价格指数|通胀数据"),
    rule("macro_data", "employment_release", r"\b(?:employment\s+situation|nonfarm\s+payrolls?|unemployment\s+rate|job\s+openings|jolts)\b|非农|失业率|职位空缺"),
    rule("regulatory", "enforcement_action", r"\b(?:enforcement\s+action|cease\s+and\s+desist|consent\s+order|civil\s+money\s+penalt(?:y|ies))\b|执法行动|停止并终止令"),
    rule("product_safety", "product_recall", r"\b(?:product\s+recall|recalls?\s+[\w-]+|safety\s+recall)\b|产品召回|安全召回"),
    rule("governance", "management_change", r"\b(?:(?:ceo|cfo|chief\s+executive|chief\s+financial)\s+(?:resigns?|steps?\s+down|appointed|departure)|management\s+change)\b|高管变更|首席执行官辞职"),
    rule("distress", "debt_default", r"\b(?:debt\s+default|missed\s+(?:debt|interest)\s+payment|going\s+concern|covenant\s+breach)\b|债务违约|持续经营疑虑"),
    rule("capital_structure", "offering_or_dilution", r"\b(?:secondary\s+offering|public\s+offering|private\s+placement|at-the-market\s+offering|share\s+dilution)\b|增发|配股|股权稀释"),
    rule("corporate_action", "restructuring", r"\b(?:restructuring|reorganization|exit\s+or\s+disposal\s+activities|impairment\s+charge)\b|重组|资产减值"),
    rule("capital_return", "dividend_or_buyback", r"\b(?:dividend\s+(?:cut|suspension|increase)|share\s+repurchase|stock\s+buyback)\b|削减股息|暂停分红|股票回购"),
)

SEC_ITEM_RULES = (
    ({"1.03"}, "bankruptcy"),
    ({"3.01"}, "delisting"),
    ({"2.04"}, "debt_default"),
    ({"2.05", "2.06"}, "restructuring"),
    ({"5.02"}, "management_change"),
    ({"2.02"}, "earnings_or_guidance"),
    ({"1.01", "1.02", "2.01"}, "material_corporate_transaction"),
)

RULE_BY_TYPE = {candidate.event_type: candidate for candidate in RULES}
SEC_GENERIC_RULE = rule("regulatory_filing", "sec_material_filing", r"$^")
SEC_TRANSACTION_RULE = rule(
    "corporate_action", "material_corporate_transaction", r"$^"
)
FED_BANK_REGULATORY_RULE = rule(
    "regulatory", "bank_regulatory_update", r"$^"
)
FED_ENFORCEMENT_TERMINATION_RULE = rule(
    "regulatory_resolution", "enforcement_action_termination", r"$^"
)
CFTC_ENFORCEMENT_RULE = rule(
    "regulatory", "cftc_enforcement_action", r"$^"
)
SEC_LITIGATION_RULE = rule(
    "regulatory", "sec_litigation_release", r"$^"
)
SEC_TRADING_SUSPENSION_RULE = rule(
    "listing_status", "trading_suspension", r"$^"
)
FDA_SAFETY_ALERT_RULE = rule(
    "product_safety", "product_safety_alert", r"$^"
)
FDIC_RECEIVERSHIP_RULE = rule(
    "distress", "bank_receivership", r"$^"
)
FDIC_ENFORCEMENT_DIGEST_RULE = rule(
    "regulatory", "bank_enforcement_orders_digest", r"$^"
)

FTC_ENFORCEMENT_TRIGGER = re.compile(
    r"\b(?:sues?|charges?|settles?|settlement|final(?:izes?|\s+order)|orders?|"
    r"bans?|requires?|enforcement|takes?\s+action|approves?\s+final\s+order)\b",
    re.I,
)
CFTC_ENFORCEMENT_TRIGGER = re.compile(
    r"\b(?:charges?|orders?|judgment|resolves?\s+action|sues?|settlement|"
    r"penalt(?:y|ies)|fraud|insider\s+trading|spoofing|misappropriation)\b",
    re.I,
)
FDIC_RECEIVERSHIP_TRIGGER = re.compile(
    r"\b(?:assumes?\s+(?:(?:all|insured)\s+)?deposits?|appointed\s+(?:as\s+)?receiver|"
    r"bank\s+(?:failure|closed)|closed\s+by\s+the|receivership)\b",
    re.I,
)
FDIC_REGULATORY_TRIGGER = re.compile(
    r"\b(?:final\s+rule|proposed?\s+rule|proposal|guidance|policy\s+statement|"
    r"enforcement\s+actions?|capital\s+requirement|resolution\s+plan)\b",
    re.I,
)

ENTITY_PATTERNS = (
    ("ostium", re.compile(r"\bostium\b", re.I)),
    ("sk_hynix", re.compile(r"\b(?:sk\s*hynix|skhy)\b|SK\s*海力士", re.I)),
    ("openai", re.compile(r"\bopenai\b", re.I)),
    ("hugging_face", re.compile(r"\bhugging\s*face\b|\bhuggingface\b", re.I)),
    ("iran", re.compile(r"\biran(?:ian)?\b", re.I)),
    ("russia", re.compile(r"\brussia(?:n)?\b", re.I)),
    ("ecb", re.compile(r"\b(?:ecb|european central bank)\b", re.I)),
    ("federal_reserve", re.compile(r"\b(?:federal reserve|fed)\b", re.I)),
    ("sec", re.compile(r"\b(?:u\.s\.\s+)?sec\b", re.I)),
    ("binance", re.compile(r"\bbinance\b", re.I)),
    ("coinbase", re.compile(r"\bcoinbase\b", re.I)),
)
ENTITY_DISPLAY_NAMES = {
    "ostium": "Ostium",
    "sk_hynix": "SK Hynix",
    "openai": "OpenAI",
    "hugging_face": "Hugging Face",
    "iran": "Iran",
    "russia": "Russia",
    "ecb": "European Central Bank",
    "federal_reserve": "Federal Reserve",
    "binance": "Binance",
    "coinbase": "Coinbase",
}
LEGAL_COMPANY_NAME = re.compile(
    r"\b([A-Z][A-Za-z0-9&.'’/-]*(?:\s+[A-Z][A-Za-z0-9&.'’/-]*){0,7}\s+"
    r"(?:Corporation|Corp\.?|Incorporated|Inc\.?|Limited|Ltd\.?|LLC|PLC|Holdings?|Group))\b"
)
CROSS_SOURCE_CLUSTER_ENTITIES = {"ostium", "binance", "coinbase"}
CANDIDATE_CLUSTER_ENTITIES = {"ostium", "sk_hynix", "openai", "hugging_face"}

OPENNEWS_NON_EVENT_GENRE = re.compile(
    r"\b(?:research\s+primer|commissioned\s+by|market\s+wrap|what\s+to\s+know|"
    r"opinion|explainer|deep\s+dive)\b|\bhow\s+.{0,80}\b(?:build|work|evolv)",
    re.I | re.S,
)
OPENNEWS_CONDITIONAL = re.compile(
    r"\b(?:could|may|might|potential(?:ly)?|expected\s+to|outlook|concerns?|"
    r"bracing|investors?\s+may\s+be\s+missing|reassess\s+.{0,40}\bnext)\b",
    re.I,
)
OPENNEWS_OBSERVED_ACTION = re.compile(
    r"\b(?:file[sd]?|announc(?:e[sd]?|ing)|order(?:s|ed)?|charg(?:e[sd]?|ing)|"
    r"sue[sd]?|settle[sd]?|recall(?:s|ed|ing)?|suspend(?:s|ed|ing)?|pause[sd]?|"
    r"disable[sd]?|cut(?:s|ting)?|rais(?:e[sd]?|ing)|maintain(?:s|ed|ing)?|"
    r"report(?:s|ed|ing)?|launch(?:es|ed|ing)?|agree[sd]?|complete[sd]?|"
    r"close[sd]?|resign(?:s|ed|ing)?|appoint(?:s|ed|ing)?|default(?:s|ed|ing)?|"
    r"miss(?:es|ed|ing)?|breach(?:es|ed|ing)?|attack(?:s|ed|ing)?)\b",
    re.I,
)
OPENNEWS_NON_NEGATIVE_CONTROL = re.compile(
    r"\bmaintain(?:s|ed|ing)?\b.{0,100}\b(?:cost|production|revenue|earnings)?\s*guidance\b",
    re.I,
)
OPENNEWS_LIVE_OR_ROUNDUP = re.compile(
    r"^\s*(?:live|liveblog|live\s+updates?)\s*[:\-]|\b(?:here(?:'|’)?s\s+what\s+happened\s+today|"
    r"what\s+happened\s+today|daily\s+roundup|news\s+roundup|week\s+in\s+review)\b",
    re.I,
)
OPENNEWS_MONETARY_ACTION = re.compile(
    r"\b(?:cuts?|raise[sd]?|hikes?|holds?|keeps?|maintains?)\s+"
    r"(?:the\s+)?(?:(?:policy|interest)\s+)?rates?\b|"
    r"\b(?:released?|published?|issued?)\b.{0,80}\b(?:policy\s+meeting|meeting\s+minutes)\b|"
    r"\b(?:announc(?:e[sd]?|ing)|beg(?:an|ins)|end(?:s|ed|ing))\b.{0,80}"
    r"\b(?:quantitative\s+(?:easing|tightening)|bond\s+purchases?)\b|"
    r"\b(?:change[sd]?|raise[sd]?|cut|reduce[sd]?)\b.{0,80}\breserve\s+requirement\b|"
    r"(?:宣布|决定|实施|维持|上调|下调).{0,40}(?:降息|加息|利率|准备金)|"
    r"(?:发布|公布).{0,40}会议纪要",
    re.I,
)
OPENNEWS_FINANCIAL_TRANSMISSION = re.compile(
    r"\b(?:sanctions?|tariffs?|export\s+controls?|shipping|freight|oil|gas|lng|"
    r"strait|blockade|supply\s+disruption|market\s+clos(?:e[sd]?|ure)|capital\s+controls?)\b|"
    r"制裁|关税|出口管制|航运|油价|天然气|封锁|供应中断|休市",
    re.I,
)
OPENNEWS_OPERATIONAL_SECURITY = re.compile(
    r"\b(?:security\s+breach|data\s+breach|cyberattack|ransomware)\b|"
    r"\b(?:(?:exchange|operator|protocol|bridge|wallet|vault|funds?|tokens?|accounts?|data|network|"
    r"service|company|startup)\b.{0,90}\b(?:hack(?:ed|ing)?|exploit(?:ed|ing)?|breach|"
    r"drain(?:ed|ing)?)\b|(?:hack(?:ed|ing)?|exploit(?:ed|ing)?|breach|drain(?:ed|ing)?)\b"
    r".{0,90}(?:\$\s?\d[\d.,]*\s*(?:[mkb]|million|billion)?|million|billion|"
    r"funds?|tokens?|accounts?|data|network|service))\b|"
    r"黑客攻击|资金被盗|数据泄露",
    re.I,
)
OPENNEWS_INVALID_ASSET_TAGS = {"OPENAI"}
MACRO_ASSET_PSEUDO_SUBJECTS = {
    "BTC",
    "BITCOIN",
    "ETH",
    "ETHEREUM",
    "GOLD",
    "XAU",
    "SILVER",
    "OIL",
    "CRUDE OIL",
    "SP500",
    "S&P 500",
    "NASDAQ",
    "DOW",
}
OPENNEWS_ASSET_ALIASES = {
    "BTC": ("bitcoin",),
    "ETH": ("ethereum", "ether"),
    "SOL": ("solana",),
    "WLD": ("worldcoin",),
    "BNB": ("binance coin",),
    "XRP": ("xrp", "ripple"),
    "DOGE": ("dogecoin",),
    "CL": ("crude oil", "wti", "oil futures"),
}
DEFAULT_P2_PENDING_CAP = 200
DEFAULT_P2_CYCLE_CAP = 25
DEFAULT_P2_STALE_HOURS = 48


@dataclass(frozen=True)
class DiscoveryAdmission:
    admitted: bool
    decision: str
    reasons: tuple[str, ...]


def opennews_admission(row: Any, matched: EventRule) -> DiscoveryAdmission:
    discovery_text = opennews_discovery_text(row)
    if not discovery_text:
        return DiscoveryAdmission(False, "REJECT_NOISE", ("not_a_headline_event",))
    if OPENNEWS_LIVE_OR_ROUNDUP.search(discovery_text):
        return DiscoveryAdmission(False, "REJECT_NOISE", ("live_or_roundup_genre",))
    analysis_text = opennews_analysis_text(row) or discovery_text
    scope = assess_risk_scope(analysis_text)
    if scope.decision == "REJECT_NOISE":
        return DiscoveryAdmission(False, scope.decision, tuple(scope.reason_codes))
    if matched.event_type == "monetary_policy" and not OPENNEWS_MONETARY_ACTION.search(analysis_text):
        return DiscoveryAdmission(False, "REJECT_NOISE", ("central_bank_mention_without_policy_action",))
    if matched.event_type == "conflict_or_blockade" and not OPENNEWS_FINANCIAL_TRANSMISSION.search(discovery_text):
        return DiscoveryAdmission(False, "ADMIT_CONTEXT", ("geopolitical_story_without_financial_transmission",))
    if matched.event_type == "hack_or_exploit" and not OPENNEWS_OPERATIONAL_SECURITY.search(discovery_text):
        return DiscoveryAdmission(False, "REJECT_NOISE", ("hack_metaphor_or_non_operational_incident",))
    return DiscoveryAdmission(True, "ADMIT_EVENT_CANDIDATE", ("bounded_event_rule",))


def opennews_discovery_text(row: Any) -> str | None:
    """Return a headline-like event sentence, rejecting articles that are not events."""
    try:
        payload = json.loads(row["raw_json"])
    except json.JSONDecodeError:
        payload = {}
    item = payload.get("item") if isinstance(payload, dict) else None
    if isinstance(item, dict):
        candidate = str(item.get("title") or item.get("content") or row["title"] or "")
    else:
        candidate = str(row["title"] or "")
    candidate = html.unescape(re.sub(r"<br\s*/?>", "\n", candidate, flags=re.I))
    nonempty_lines = [line.strip() for line in candidate.splitlines() if line.strip()]
    headline = nonempty_lines[0] if nonempty_lines else candidate.strip()
    bullet_count = len(re.findall(r"(?:^|\n)\s*[•▪●*-]\s+", candidate))
    if len(candidate) > 2000 or bullet_count >= 3 or OPENNEWS_NON_EVENT_GENRE.search(candidate):
        return None
    if OPENNEWS_NON_NEGATIVE_CONTROL.search(headline):
        return None
    if OPENNEWS_CONDITIONAL.search(headline) and not OPENNEWS_OBSERVED_ACTION.search(headline):
        return None
    return headline[:800]


def opennews_analysis_text(row: Any) -> str | None:
    """Return all provider text useful for classification, never provider scores.

    The headline remains the genre/quality anchor, while source-provided
    summaries may contain the named actor or concrete action.  Ranking labels,
    signals and coin tags are deliberately excluded from semantic admission.
    """

    try:
        payload = json.loads(row["raw_json"])
    except (TypeError, json.JSONDecodeError):
        payload = {}
    item = payload.get("item") if isinstance(payload, dict) else None
    values: list[str] = [str(row["title"] or ""), str(row["summary"] or "")]
    if isinstance(item, dict):
        values.extend(
            str(item.get(key) or "")
            for key in ("title", "content", "summary_zh", "summary_en")
        )
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = " ".join(html.unescape(re.sub(r"<br\s*/?>", "\n", value, flags=re.I)).split())
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            unique.append(cleaned)
    return "\n".join(unique)[:4000] or None


def recognized_entity(text: str) -> str | None:
    return next((name for name, pattern in ENTITY_PATTERNS if pattern.search(text)), None)


def classify(text: str) -> EventRule | None:
    normalized = " ".join(text.split())
    if re.search(r"section-news|<span\b|^\s*premarket\s+movers\b", text, re.I):
        return None
    return next((candidate for candidate in RULES if candidate.pattern.search(normalized)), None)


def classify_observation(row: Any) -> EventRule | None:
    try:
        payload = json.loads(row["raw_json"])
    except json.JSONDecodeError:
        payload = {}
    item = payload.get("item") if isinstance(payload, dict) else None
    source_id = str(row["source_id"])
    text = f"{row['title']}\n{row['summary']}"

    if source_id == "opennews_free":
        discovery_text = opennews_discovery_text(row)
        analysis_text = opennews_analysis_text(row)
        return classify(analysis_text) if discovery_text and analysis_text else None

    if source_id == "cftc_enforcement":
        return CFTC_ENFORCEMENT_RULE if CFTC_ENFORCEMENT_TRIGGER.search(text) else None
    if source_id == "sec_litigation_releases":
        return SEC_LITIGATION_RULE
    if source_id == "sec_trading_suspensions":
        return SEC_TRADING_SUSPENSION_RULE
    if source_id == "fda_medwatch":
        return (
            RULE_BY_TYPE["product_recall"]
            if re.search(r"\brecall\b", text, re.I)
            else FDA_SAFETY_ALERT_RULE
        )
    if source_id == "ftc_press":
        return RULE_BY_TYPE["enforcement_action"] if FTC_ENFORCEMENT_TRIGGER.search(text) else None
    if source_id == "fdic_press_releases":
        if FDIC_RECEIVERSHIP_TRIGGER.search(text):
            return FDIC_RECEIVERSHIP_RULE
        if FDIC_REGULATORY_TRIGGER.search(text):
            return FDIC_ENFORCEMENT_DIGEST_RULE if re.search(
                r"\benforcement\s+actions?\b", text, re.I
            ) else FED_BANK_REGULATORY_RULE
        return None

    if row["source_id"] == "federal_reserve_press" and isinstance(item, dict):
        category = str(item.get("category") or "")
        if category == "Monetary Policy":
            return RULE_BY_TYPE["monetary_policy"]
        if category == "Enforcement Actions":
            if re.search(r"\bterminat(?:e[sd]?|ion)\b", f"{row['title']}\n{row['summary']}", re.I):
                return FED_ENFORCEMENT_TERMINATION_RULE
            return RULE_BY_TYPE["enforcement_action"]
        if category == "Banking and Consumer Regulatory Policy":
            return FED_BANK_REGULATORY_RULE
        return None

    matched = classify(text)
    if matched is not None or row["source_id"] != "sec_current_filings":
        return matched
    if not isinstance(item, dict):
        return None
    form = str(item.get("form") or "")
    items = {str(value) for value in item.get("items") or []}
    if form in {"25", "25-NSE", "15-12B", "15-12G"}:
        return RULE_BY_TYPE["delisting"]
    for item_codes, event_type in SEC_ITEM_RULES:
        if items & item_codes:
            if event_type == "material_corporate_transaction":
                return SEC_TRANSACTION_RULE
            return RULE_BY_TYPE[event_type]
    if form in {"10-Q", "10-Q/A", "10-K", "10-K/A", "20-F", "20-F/A"}:
        return RULE_BY_TYPE["earnings_or_guidance"]
    return SEC_GENERIC_RULE


def _opennews_coin_tags(raw_json: str) -> list[str]:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return []
    item = payload.get("item") if isinstance(payload, dict) else None
    coins = item.get("coins") if isinstance(item, dict) else None
    if not isinstance(coins, list):
        return []
    result: list[str] = []
    for coin in coins:
        value = coin.get("symbol") if isinstance(coin, dict) else coin
        value = str(value or "").strip().upper()
        if value:
            result.append(value)
    return result


def canonicalize_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urllib.parse.urlsplit(value.strip())
    if not parsed.scheme or not parsed.netloc:
        return value.strip()
    host = parsed.netloc.casefold().removeprefix("www.")
    if host in {"twitter.com", "mobile.twitter.com"}:
        host = "x.com"
    path = re.sub(r"/+$", "", parsed.path) or "/"
    return urllib.parse.urlunsplit((parsed.scheme.casefold(), host, path, "", ""))


def observation_known_at(source_published_at: str | None, local_received_at: str) -> str:
    values: list[datetime] = []
    for value in (source_published_at, local_received_at):
        if not value:
            continue
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        values.append(parsed.astimezone(timezone.utc))
    if not values:
        raise ValueError("observation requires a published or received timestamp")
    return max(values).isoformat()


def record_sec_discovery_lead(connection: Any, row: Any, matched: EventRule, *, now: str) -> str:
    """Keep an SEC filing as a lead until its documents support a scoped event.

    A filing index proves that a document was filed.  It does not by itself
    prove earnings deterioration, a management change, distress, a transaction,
    or any other event predicate.
    """

    lead_id = stable_id("LEAD", str(row["observation_id"]))
    event_date = (row["source_published_at"] or row["local_received_at"])[:10]
    company_name = extract_company(str(row["raw_json"] or ""))
    ticker = extract_symbol(
        str(row["raw_json"] or ""),
        f"{row['title']}\n{row['summary']}",
    )
    connection.execute(
        """
        INSERT INTO discovery_leads(
            lead_id,observation_id,source_id,status,proposed_event_family,
            proposed_event_type,company_name,ticker_at_event,event_date,known_at,
            claim_action,claim_stage,claim_summary,evidence_url,evidence_passage,
            evidence_status,source_content_sha256,matched_keywords_json,
            admission_reasons_json,admission_contract_version,canonical_event_id,
            created_at,updated_at,no_trading
        ) VALUES (?,?,?,'PENDING_ENRICHMENT',?,?,?,?,?,?,NULL,NULL,NULL,?,NULL,
                  'link_only_no_relevant_passage',?,'[]',?,?,NULL,?,?,1)
        ON CONFLICT(observation_id) DO UPDATE SET
            proposed_event_family=excluded.proposed_event_family,
            proposed_event_type=excluded.proposed_event_type,
            company_name=COALESCE(excluded.company_name,discovery_leads.company_name),
            ticker_at_event=COALESCE(excluded.ticker_at_event,discovery_leads.ticker_at_event),
            event_date=excluded.event_date,
            known_at=excluded.known_at,
            evidence_url=excluded.evidence_url,
            source_content_sha256=excluded.source_content_sha256,
            admission_reasons_json=excluded.admission_reasons_json,
            admission_contract_version=excluded.admission_contract_version,
            updated_at=excluded.updated_at
        """,
        (
            lead_id,
            row["observation_id"],
            row["source_id"],
            matched.event_family,
            matched.event_type,
            company_name,
            ticker,
            event_date,
            observation_known_at(row["source_published_at"], row["local_received_at"]),
            canonicalize_url(row["canonical_url"]) or "",
            str(row["content_sha256"] or ""),
            stable_json(["SEC_REQUIRES_DOCUMENT_SEMANTIC_MATCH"]),
            DISCOVERY_ADMISSION_CONTRACT,
            now,
            now,
        ),
    )
    return lead_id


def normalized_title(value: str) -> str:
    value = html.unescape(re.sub(r"<[^>]+>", " ", value))
    value = re.sub(r"https?://\S+", " ", value, flags=re.I)
    value = re.sub(
        r"^(?:aggrnews(?:\s*\([^)]*\))?|bloomberg|reuters)\s*:\s*",
        "",
        value.strip(),
        flags=re.I,
    )
    first_line = next((line.strip() for line in value.splitlines() if line.strip()), value)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", first_line.casefold()).strip()


def live_event_id(row: Any, matched: EventRule) -> str:
    title_key = normalized_title(row["title"])
    full_text = f"{row['title']}\n{row['summary']}"
    entity = recognized_entity(full_text)
    event_date = (row["source_published_at"] or row["local_received_at"])[:10]
    if str(row["authority_tier"]).startswith("P0") and row["canonical_url"]:
        key = f"{canonicalize_url(row['canonical_url']) or title_key}|{event_date}"
    elif entity in CANDIDATE_CLUSTER_ENTITIES:
        key = f"{matched.event_type}|{entity}|{event_date}"
    elif row["canonical_url"]:
        key = f"{canonicalize_url(row['canonical_url']) or title_key}|{event_date}"
    else:
        base = title_key if len(title_key) >= 24 else canonicalize_url(row["canonical_url"]) or title_key
        key = f"{base}|{event_date}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return f"FR-LIVE-{digest}"


def existing_story_event_id(connection: Any, row: Any, matched: EventRule) -> str | None:
    """Reuse a prior non-official event when a provider changes its external item ID."""
    if str(row["authority_tier"]).startswith("P0") or not row["canonical_url"]:
        return None
    canonical_url = canonicalize_url(row["canonical_url"])
    event_date = (row["source_published_at"] or row["local_received_at"])[:10]
    candidates = connection.execute(
        """SELECT e.event_id,r.canonical_url
           FROM canonical_events e
           JOIN event_observations eo ON eo.event_id=e.event_id
           JOIN latest_source_content r ON r.observation_id=eo.observation_id
           JOIN sources s ON s.source_id=r.source_id
           WHERE e.status='candidate' AND e.event_date=? AND e.event_type=?
             AND eo.relation_type!='filtered_aggregated_noise'
             AND r.canonical_url IS NOT NULL
             AND s.authority_tier NOT LIKE 'P0%'
           ORDER BY e.first_seen_at,e.event_id""",
        (event_date, matched.event_type),
    ).fetchall()
    for candidate in candidates:
        if canonicalize_url(candidate["canonical_url"]) == canonical_url:
            return str(candidate["event_id"])
    return None


def retract_filtered_opennews_candidates(connection: Any) -> int:
    """Continuously audit every active OpenNews candidate against current rules.

    Raw observations and their ledger edges remain available for audit.  An
    observation that no longer passes is marked as filtered so it cannot become
    the displayed headline or model input for a mixed, still-valid event.
    """
    rows = connection.execute(
        """SELECT e.event_id,r.source_id,r.source_published_at,r.local_received_at,
                  r.observation_id,r.title,r.summary,r.canonical_url,r.raw_json,
                  s.authority_tier,eo.relation_type
           FROM canonical_events e
           JOIN event_observations eo ON eo.event_id=e.event_id
           JOIN latest_source_content r ON r.observation_id=eo.observation_id
           JOIN sources s ON s.source_id=r.source_id
           WHERE e.status='candidate'
             AND e.discovery_source='opennews_free'
           ORDER BY e.event_id"""
    ).fetchall()
    by_event: dict[str, list[Any]] = {}
    for row in rows:
        by_event.setdefault(str(row["event_id"]), []).append(row)
    now = utc_now()
    retracted = 0
    for event_id, observations in by_event.items():
        has_primary_source = False
        admitted_observation = False
        for row in observations:
            if str(row["authority_tier"]).startswith(("P0", "P1")):
                has_primary_source = True
                continue
            matched = classify_observation(row)
            admitted = matched is not None and (
                str(row["source_id"]) != "opennews_free"
                or opennews_admission(row, matched).admitted
            )
            if admitted:
                admitted_observation = True
                if str(row["relation_type"]) == "filtered_aggregated_noise":
                    connection.execute(
                        """UPDATE event_observations
                           SET relation_type='aggregated_discovery_candidate',linked_at=?
                           WHERE event_id=? AND observation_id=?""",
                        (now, event_id, row["observation_id"]),
                    )
            elif str(row["source_id"]) == "opennews_free":
                connection.execute(
                    """UPDATE event_observations
                       SET relation_type='filtered_aggregated_noise',linked_at=?
                       WHERE event_id=? AND observation_id=?""",
                    (now, event_id, row["observation_id"]),
                )
        if has_primary_source or admitted_observation:
            continue
        connection.execute(
            """UPDATE pipeline_jobs
               SET status='COMPLETED_DISCOVERY_FILTERED',
                   last_error='aggregated_story_no_longer_passes_event_semantic_gate',
                   updated_at=?
               WHERE event_id=? AND job_type='live_primary_evidence_review'
                 AND status!='COMPLETED_DUPLICATE_CLUSTER'""",
            (now, event_id),
        )
        filtered = _filter_candidate_event(
            connection,
            event_id,
            reason="aggregated_story_no_longer_passes_event_semantic_gate",
            now=now,
        )
        retracted += int(filtered)
    connection.commit()
    return retracted


def reconcile_verified_opennews_duplicates(connection: Any) -> int:
    """Attach narrow, high-confidence P2 duplicates to an already verified event."""
    candidates = connection.execute(
        """SELECT e.event_id,e.event_family,e.event_date,r.observation_id,r.title,r.summary
           FROM canonical_events e
           JOIN pipeline_jobs j ON j.event_id=e.event_id
           JOIN event_observations eo ON eo.event_id=e.event_id
           JOIN latest_source_content r ON r.observation_id=eo.observation_id
           WHERE e.status='candidate' AND e.discovery_source='opennews_free'
             AND eo.relation_type!='filtered_aggregated_noise'
             AND j.job_type='live_primary_evidence_review'
             AND j.status='PENDING_PRIMARY_EVIDENCE'
           ORDER BY e.event_id"""
    ).fetchall()
    now = utc_now()
    reconciled_events: set[str] = set()
    for row in candidates:
        entity = recognized_entity(f"{row['title']}\n{row['summary']}")
        if entity not in CROSS_SOURCE_CLUSTER_ENTITIES:
            continue
        verified = connection.execute(
            """SELECT e.event_id,r.title,r.summary
               FROM canonical_events e
               JOIN event_observations eo ON eo.event_id=e.event_id
               JOIN latest_source_content r ON r.observation_id=eo.observation_id
               JOIN sources s ON s.source_id=r.source_id
               WHERE e.status='verified' AND e.event_family=?
                 AND ABS(julianday(e.event_date)-julianday(?))<=1
                 AND s.authority_tier LIKE 'P%'
               ORDER BY CASE WHEN s.authority_tier LIKE 'P0%' THEN 0 ELSE 1 END,
                        e.event_id""",
            (row["event_family"], row["event_date"]),
        ).fetchall()
        primary_event_id = next(
            (
                str(item["event_id"])
                for item in verified
                if recognized_entity(f"{item['title']}\n{item['summary']}") == entity
            ),
            None,
        )
        if primary_event_id is None:
            continue
        connection.execute(
            """INSERT OR IGNORE INTO event_observations(
               event_id,observation_id,relation_type,linked_at
               ) VALUES (?,?,'aggregated_duplicate_support',?)""",
            (primary_event_id, row["observation_id"], now),
        )
        connection.execute(
            """UPDATE pipeline_jobs SET status='COMPLETED_DUPLICATE_CLUSTER',
               last_error=?,updated_at=?
               WHERE event_id=? AND job_type='live_primary_evidence_review'
                 AND status='PENDING_PRIMARY_EVIDENCE'""",
            (f"duplicate_of_verified_event:{primary_event_id}", now, row["event_id"]),
        )
        reconciled_events.add(str(row["event_id"]))
    connection.commit()
    return len(reconciled_events)


def reconcile_opennews_candidate_duplicates(connection: Any) -> int:
    """Collapse same-day, same-action duplicates for a narrow entity allow-list."""
    rows = connection.execute(
        """SELECT e.event_id,e.event_type,e.event_date,e.first_seen_at,
                  r.observation_id,r.title,r.summary,
                  (SELECT COUNT(*) FROM event_evidence x WHERE x.event_id=e.event_id) AS evidence_count
           FROM canonical_events e
           JOIN pipeline_jobs j ON j.event_id=e.event_id
           JOIN event_observations eo ON eo.event_id=e.event_id
           JOIN latest_source_content r ON r.observation_id=eo.observation_id
           WHERE e.status='candidate' AND e.discovery_source='opennews_free'
             AND eo.relation_type!='filtered_aggregated_noise'
             AND j.job_type='live_primary_evidence_review'
             AND j.status='PENDING_PRIMARY_EVIDENCE'
           ORDER BY e.first_seen_at,e.event_id,r.local_received_at"""
    ).fetchall()
    groups: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = {}
    for row in rows:
        entity = recognized_entity(f"{row['title']}\n{row['summary']}")
        if entity not in CANDIDATE_CLUSTER_ENTITIES:
            continue
        key = (str(row["event_type"]), str(row["event_date"]), str(entity))
        event = groups.setdefault(key, {}).setdefault(
            str(row["event_id"]),
            {
                "event_id": str(row["event_id"]),
                "first_seen_at": str(row["first_seen_at"]),
                "evidence_count": int(row["evidence_count"] or 0),
                "observation_ids": [],
            },
        )
        event["observation_ids"].append(str(row["observation_id"]))
    now = utc_now()
    reconciled = 0
    for events in groups.values():
        if len(events) < 2:
            continue
        ordered = sorted(
            events.values(),
            key=lambda item: (-item["evidence_count"], item["first_seen_at"], item["event_id"]),
        )
        primary = ordered[0]
        for duplicate in ordered[1:]:
            for observation_id in duplicate["observation_ids"]:
                connection.execute(
                    """INSERT OR IGNORE INTO event_observations(
                       event_id,observation_id,relation_type,linked_at
                       ) VALUES (?,?,'aggregated_duplicate_support',?)""",
                    (primary["event_id"], observation_id, now),
                )
            connection.execute(
                """UPDATE pipeline_jobs
                   SET status='COMPLETED_DUPLICATE_CLUSTER',last_error=?,updated_at=?
                   WHERE event_id=? AND job_type='live_primary_evidence_review'
                     AND status='PENDING_PRIMARY_EVIDENCE'""",
                (
                    f"duplicate_of_candidate_event:{primary['event_id']}",
                    now,
                    duplicate["event_id"],
                ),
            )
            _filter_candidate_event(
                connection,
                duplicate["event_id"],
                reason=f"duplicate_of_candidate_event:{primary['event_id']}",
                now=now,
            )
            reconciled += 1
    connection.commit()
    return reconciled


def extract_symbol(raw_json: str, source_text: str = "") -> str | None:
    """Return an OpenNews asset only when the story itself identifies it.

    Provider tags are useful retrieval hints, but they are not reliable evidence
    that the tagged asset is the subject of a story.
    """
    original = html.unescape(source_text)
    normalized = " ".join(original.casefold().split())
    for provider_symbol in _opennews_coin_tags(raw_json):
        symbol = provider_symbol.removeprefix("XYZ-")
        if symbol in OPENNEWS_INVALID_ASSET_TAGS or not symbol:
            continue
        # A bare provider tag is only corroborated by a case-sensitive ticker
        # token.  This prevents ordinary prose such as "Red Sea" or "bridge"
        # from being promoted to the unrelated RED/BRIDGE assets.
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(symbol)}(?:\.[A-Z]{{1,4}})?(?![A-Za-z0-9])", original):
            return symbol
        aliases = OPENNEWS_ASSET_ALIASES.get(symbol, ())
        if any(re.search(rf"\b{re.escape(alias)}\b", normalized) for alias in aliases):
            return symbol
    return None


def extract_company(raw_json: str) -> str | None:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    item = payload.get("item") if isinstance(payload, dict) else None
    if not isinstance(item, dict):
        return None
    return str(item.get("company") or "").strip() or None


def extract_canonical_subject(
    row: Any,
    matched: EventRule | None = None,
) -> tuple[str | None, str | None]:
    """Return a displayable subject only when the source text identifies it.

    Provider metadata may omit a dedicated company field.  A conservative
    legal-name pattern and the existing bounded entity dictionary recover
    obvious subjects without turning arbitrary headline nouns into issuers.
    If both values remain empty, the observation stays preserved but must not
    enter the canonical event ledger.
    """

    source_text = (
        opennews_analysis_text(row)
        or opennews_discovery_text(row)
        or f"{str(row['title'] or '')}\n{str(row['summary'] or '')}"
    )
    ticker = extract_symbol(str(row["raw_json"] or ""), source_text)
    company = extract_company(str(row["raw_json"] or ""))
    if not company:
        legal_name = LEGAL_COMPANY_NAME.search(html.unescape(source_text))
        company = legal_name.group(1).strip() if legal_name else None
    if not company:
        entity = recognized_entity(source_text)
        company = ENTITY_DISPLAY_NAMES.get(str(entity or ""))
    if matched and matched.event_family in {"macro_policy", "macro_data", "geopolitical"}:
        # GOLD/BTC/index tags are affected assets in macro/geopolitical stories,
        # not the actor who made a policy decision or caused an event.
        provider_assets = {
            symbol.removeprefix("XYZ-")
            for symbol in _opennews_coin_tags(str(row["raw_json"] or ""))
        }
        if company and (
            company.strip().upper() in provider_assets
            or company.strip().upper() in MACRO_ASSET_PSEUDO_SUBJECTS
        ):
            actor = recognized_entity(source_text)
            company = ENTITY_DISPLAY_NAMES.get(str(actor or ""))
        ticker = None
    return company, ticker


def provisional_grade_cap(authority_tier: str) -> str:
    if authority_tier.startswith("P0"):
        return "A_P0_official_candidate"
    if authority_tier.startswith("P1"):
        return "A_P1_primary_candidate"
    return "B_P2_discovery_only"


def _filter_candidate_event(connection: Any, event_id: str, *, reason: str, now: str) -> bool:
    event = connection.execute(
        "SELECT * FROM canonical_events WHERE event_id=?", (event_id,)
    ).fetchone()
    if event is None or str(event["status"]) != "candidate":
        return False
    version_row = connection.execute(
        "SELECT facts_json FROM event_versions WHERE event_id=? AND version=?",
        (event_id, event["current_version"]),
    ).fetchone()
    try:
        facts = json.loads(version_row["facts_json"]) if version_row else {}
    except (json.JSONDecodeError, TypeError):
        facts = {}
    facts["discovery_filter"] = {
        "reason": reason,
        "filtered_at": now,
        "raw_observations_preserved": True,
    }
    new_version = int(event["current_version"]) + 1
    connection.execute(
        """INSERT INTO event_versions(
           event_id,version,changed_at,status,label_status,event_family,event_type,
           manual_grade,facts_json,change_reason
           ) VALUES (?,?,?,'rejected','rejected',?,?,?,?,?)""",
        (
            event_id,
            new_version,
            now,
            event["event_family"],
            event["event_type"],
            event["manual_grade"],
            stable_json(facts),
            reason,
        ),
    )
    connection.execute(
        """UPDATE canonical_events
           SET current_version=?,status='rejected',label_status='rejected',last_updated_at=?
           WHERE event_id=?""",
        (new_version, now, event_id),
    )
    return True


def expire_stale_opennews_candidates(
    connection: Any, *, stale_hours: int = DEFAULT_P2_STALE_HOURS
) -> int:
    now = utc_now()
    rows = connection.execute(
        """SELECT DISTINCT e.event_id
           FROM canonical_events e
           JOIN pipeline_jobs j ON j.event_id=e.event_id
           WHERE e.status='candidate' AND e.discovery_source='opennews_free'
             AND j.job_type='live_primary_evidence_review'
             AND j.status='PENDING_PRIMARY_EVIDENCE'
             AND (julianday(?) - julianday(e.last_updated_at)) * 24 >= ?
             AND NOT EXISTS (SELECT 1 FROM event_evidence x WHERE x.event_id=e.event_id)
             AND NOT EXISTS (
                 SELECT 1 FROM event_observations eo
                 JOIN latest_source_content r ON r.observation_id=eo.observation_id
                 JOIN sources s ON s.source_id=r.source_id
                 WHERE eo.event_id=e.event_id AND s.authority_tier LIKE 'P0%'
             )""",
        (now, stale_hours),
    ).fetchall()
    for row in rows:
        event_id = str(row["event_id"])
        connection.execute(
            """UPDATE pipeline_jobs
               SET status='COMPLETED_DISCOVERY_EXPIRED',last_error=?,updated_at=?
               WHERE event_id=? AND job_type='live_primary_evidence_review'
                 AND status='PENDING_PRIMARY_EVIDENCE'""",
            (f"p2_discovery_ttl_exceeded:{stale_hours}h", now, event_id),
        )
        _filter_candidate_event(
            connection,
            event_id,
            reason=f"p2_discovery_ttl_exceeded:{stale_hours}h",
            now=now,
        )
    connection.commit()
    return len(rows)


def trim_opennews_backlog(connection: Any, *, pending_cap: int = DEFAULT_P2_PENDING_CAP) -> int:
    rows = connection.execute(
        """SELECT DISTINCT e.event_id,j.priority,e.last_updated_at
           FROM canonical_events e
           JOIN pipeline_jobs j ON j.event_id=e.event_id
           WHERE e.status='candidate' AND e.discovery_source='opennews_free'
             AND j.job_type='live_primary_evidence_review'
             AND j.status='PENDING_PRIMARY_EVIDENCE'
             AND NOT EXISTS (SELECT 1 FROM event_evidence x WHERE x.event_id=e.event_id)
             AND NOT EXISTS (
                 SELECT 1 FROM event_observations eo
                 JOIN latest_source_content r ON r.observation_id=eo.observation_id
                 JOIN sources s ON s.source_id=r.source_id
                 WHERE eo.event_id=e.event_id AND s.authority_tier LIKE 'P0%'
             )
           ORDER BY j.priority DESC,e.last_updated_at DESC,e.event_id"""
    ).fetchall()
    overflow = rows[max(0, pending_cap):]
    now = utc_now()
    for row in overflow:
        event_id = str(row["event_id"])
        connection.execute(
            """UPDATE pipeline_jobs
               SET status='COMPLETED_BACKPRESSURE_EVICTED',
                   last_error='p2_pending_cap_eviction',updated_at=?
               WHERE event_id=? AND job_type='live_primary_evidence_review'
                 AND status='PENDING_PRIMARY_EVIDENCE'""",
            (now, event_id),
        )
        _filter_candidate_event(
            connection,
            event_id,
            reason="p2_pending_cap_eviction",
            now=now,
        )
    connection.commit()
    return len(overflow)


def pending_opennews_reviews(connection: Any) -> int:
    return int(
        connection.execute(
            """SELECT COUNT(*) FROM pipeline_jobs j
               JOIN canonical_events e ON e.event_id=j.event_id
               WHERE e.status='candidate' AND e.discovery_source='opennews_free'
                 AND j.job_type='live_primary_evidence_review'
                 AND j.status='PENDING_PRIMARY_EVIDENCE'"""
        ).fetchone()[0]
    )


def repair_opennews_asset_tags(connection: Any) -> int:
    """Remove provider retrieval tags that the story text does not substantiate."""
    rows = connection.execute(
        """SELECT e.event_id,e.current_version,e.event_family,e.event_type,e.manual_grade,
                  e.ticker_at_event,r.title,r.summary,r.raw_json
           FROM canonical_events e
           JOIN event_observations eo ON eo.event_id=e.event_id
           JOIN latest_source_content r ON r.observation_id=eo.observation_id
           WHERE e.status='candidate' AND e.discovery_source='opennews_free'
             AND eo.relation_type!='filtered_aggregated_noise'
             AND r.source_id='opennews_free'
           ORDER BY e.event_id,r.local_received_at DESC"""
    ).fetchall()
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(str(row["event_id"]), []).append(row)
    now = utc_now()
    repaired = 0
    for event_id, observations in grouped.items():
        event = observations[0]
        validated = next(
            (
                symbol
                for row in observations
                if (
                    symbol := extract_symbol(
                        str(row["raw_json"]),
                        opennews_discovery_text(row) or f"{row['title']}\n{row['summary']}",
                    )
                )
            ),
            None,
        )
        current = str(event["ticker_at_event"] or "").strip() or None
        if current == validated:
            continue
        version_row = connection.execute(
            "SELECT facts_json FROM event_versions WHERE event_id=? AND version=?",
            (event_id, event["current_version"]),
        ).fetchone()
        try:
            facts = json.loads(version_row["facts_json"]) if version_row else {}
        except (json.JSONDecodeError, TypeError):
            facts = {}
        facts["asset_tag_repair"] = {
            "previous_provider_tag": current,
            "validated_symbol": validated,
            "reason": "provider_tag_not_substantiated_by_story_text",
            "repaired_at": now,
        }
        new_version = int(event["current_version"]) + 1
        connection.execute(
            """INSERT INTO event_versions(
               event_id,version,changed_at,status,label_status,event_family,event_type,
               manual_grade,facts_json,change_reason
               ) VALUES (?,?,?,'candidate','candidate',?,?,?,?,
                         'opennews_asset_tag_story_validation')""",
            (
                event_id,
                new_version,
                now,
                event["event_family"],
                event["event_type"],
                event["manual_grade"],
                stable_json(facts),
            ),
        )
        connection.execute(
            """UPDATE canonical_events
               SET current_version=?,ticker_at_event=?,last_updated_at=?
               WHERE event_id=? AND status='candidate'""",
            (new_version, validated, now, event_id),
        )
        repaired += 1
    connection.commit()
    return repaired


def process_pending(
    connection: Any,
    *,
    limit: int,
    p2_pending_cap: int = DEFAULT_P2_PENDING_CAP,
    p2_cycle_cap: int = DEFAULT_P2_CYCLE_CAP,
    p2_stale_hours: int = DEFAULT_P2_STALE_HOURS,
) -> dict[str, Any]:
    retracted = retract_filtered_opennews_candidates(connection)
    expired = expire_stale_opennews_candidates(connection, stale_hours=p2_stale_hours)
    evicted = trim_opennews_backlog(connection, pending_cap=p2_pending_cap)
    candidate_duplicates = reconcile_opennews_candidate_duplicates(connection)
    asset_tag_repairs = repair_opennews_asset_tags(connection)
    p2_pending = pending_opennews_reviews(connection)
    rows = connection.execute(
        """
        SELECT j.*,r.source_id,r.source_published_at,r.local_received_at,r.title,r.summary,
               r.canonical_url,r.content_sha256,r.raw_json,s.authority_tier
        FROM observation_jobs j
        JOIN latest_source_content r ON r.observation_id=j.observation_id
        JOIN sources s ON s.source_id=r.source_id
        WHERE j.job_type='extract_live_event_candidate' AND j.status='PENDING'
        ORDER BY CASE
                   WHEN s.authority_tier LIKE 'P0%' THEN 0
                   WHEN s.authority_tier LIKE 'P1%' THEN 1
                   ELSE 2
                 END,
                 j.priority DESC,j.available_at,j.job_id LIMIT ?
        """,
        (limit,),
    ).fetchall()
    now = utc_now()
    result: dict[str, Any] = {
        "processed": 0,
        "candidates": 0,
        "discovery_leads": 0,
        "no_candidate": 0,
        "scope_filtered": 0,
        "subject_filtered": 0,
        "backpressure_filtered": 0,
        "new_events": 0,
        "linked_observations": 0,
        "by_type": {},
        "event_ids": [],
        "retracted_events": retracted,
        "expired_events": expired,
        "backlog_evicted_events": evicted,
        "candidate_duplicates_reconciled": candidate_duplicates,
        "asset_tag_repairs": asset_tag_repairs,
        "p2_pending_before_admission": p2_pending,
    }
    p2_admitted = 0
    for row in rows:
        result["processed"] += 1
        matched = classify_observation(row)
        if matched is None:
            connection.execute(
                """UPDATE observation_jobs SET status='COMPLETED_NO_CANDIDATE',attempts=attempts+1,
                   updated_at=? WHERE job_id=?""",
                (now, row["job_id"]),
            )
            result["no_candidate"] += 1
            continue

        if str(row["source_id"]) == "sec_current_filings":
            record_sec_discovery_lead(connection, row, matched, now=now)
            connection.execute(
                """UPDATE observation_jobs
                   SET status='COMPLETED_DISCOVERY_LEAD',attempts=attempts+1,
                       last_error='sec_parse_before_canonical',updated_at=?
                   WHERE job_id=?""",
                (now, row["job_id"]),
            )
            result["discovery_leads"] += 1
            continue

        company_name, ticker_at_event = extract_canonical_subject(row, matched)
        if not company_name and not ticker_at_event:
            connection.execute(
                """UPDATE observation_jobs
                   SET status='COMPLETED_SUBJECT_FILTERED',attempts=attempts+1,
                       last_error='subject_unresolved_not_canonical',updated_at=?
                   WHERE job_id=?""",
                (now, row["job_id"]),
            )
            result["subject_filtered"] += 1
            continue

        is_p2 = not str(row["authority_tier"]).startswith(("P0", "P1"))
        if str(row["source_id"]) == "opennews_free":
            admission = opennews_admission(row, matched)
            if not admission.admitted:
                connection.execute(
                    """UPDATE observation_jobs
                       SET status='COMPLETED_SCOPE_FILTERED',attempts=attempts+1,
                           last_error=?,updated_at=? WHERE job_id=?""",
                    (";".join(admission.reasons), now, row["job_id"]),
                )
                result["scope_filtered"] += 1
                continue

        event_id = existing_story_event_id(connection, row, matched) or live_event_id(row, matched)
        existing = connection.execute(
            "SELECT 1 FROM canonical_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if is_p2 and existing is None and (
            p2_pending >= p2_pending_cap or p2_admitted >= p2_cycle_cap
        ):
            connection.execute(
                """UPDATE observation_jobs
                   SET status='COMPLETED_BACKPRESSURE_FILTERED',attempts=attempts+1,
                       last_error='p2_admission_capacity_reached',updated_at=?
                   WHERE job_id=?""",
                (now, row["job_id"]),
            )
            result["backpressure_filtered"] += 1
            continue
        event_date = (row["source_published_at"] or row["local_received_at"])[:10]
        grade_cap = provisional_grade_cap(row["authority_tier"])
        relation_type = (
            "official_primary_candidate"
            if str(row["authority_tier"]).startswith("P0")
            else "aggregated_discovery_candidate"
        )
        facts = {
            "candidate_only": True,
            "matched_rule": matched.pattern.pattern,
            "source_id": row["source_id"],
            "source_title": row["title"],
            "source_summary": row["summary"],
            "canonical_url": canonicalize_url(row["canonical_url"]),
            "source_authority_tier": row["authority_tier"],
            "provisional_grade_cap": grade_cap,
            "auto_verification_allowed": False,
            "no_trading": True,
        }
        if str(row["source_id"]) == "opennews_free":
            affected_assets = sorted(
                {
                    symbol.removeprefix("XYZ-")
                    for symbol in _opennews_coin_tags(str(row["raw_json"] or ""))
                    if symbol.removeprefix("XYZ-")
                    and symbol.removeprefix("XYZ-") not in OPENNEWS_INVALID_ASSET_TAGS
                }
            )
            if affected_assets:
                facts["affected_assets"] = affected_assets
        connection.execute(
            """
            INSERT OR IGNORE INTO canonical_events(
                event_id,current_version,status,label_status,event_family,event_type,event_date,
                first_seen_at,last_updated_at,stable_id,ticker_at_event,company_name,manual_grade,
                provisional_grade_cap,discovery_source,no_trading
            ) VALUES (?,1,'candidate','candidate',?,?,?,?,?,NULL,?,?,NULL,?, ?,1)
            """,
            (
                event_id,
                matched.event_family,
                matched.event_type,
                event_date,
                row["local_received_at"],
                now,
                ticker_at_event,
                company_name,
                grade_cap,
                row["source_id"],
            ),
        )
        if existing is None:
            result["new_events"] += 1
            if is_p2:
                p2_pending += 1
                p2_admitted += 1
        connection.execute(
            """INSERT OR IGNORE INTO event_versions(
               event_id,version,changed_at,status,label_status,event_family,event_type,
               manual_grade,facts_json,change_reason
               ) VALUES (?,1,?,'candidate','candidate',?,?,NULL,?,'live_rule_candidate')""",
            (
                event_id,
                now,
                matched.event_family,
                matched.event_type,
                stable_json(facts),
            ),
        )
        before = connection.total_changes
        connection.execute(
            """INSERT OR IGNORE INTO event_observations(
               event_id,observation_id,relation_type,linked_at
               ) VALUES (?,?,?,?)""",
            (event_id, row["observation_id"], relation_type, now),
        )
        result["linked_observations"] += connection.total_changes - before
        priority = int(row["priority"])
        connection.execute(
            """INSERT OR IGNORE INTO pipeline_jobs(
               job_id,event_id,job_type,status,priority,attempts,available_at,last_error,
               payload_json,created_at,updated_at
               ) VALUES (?,?,'live_primary_evidence_review','PENDING_PRIMARY_EVIDENCE',?,0,?,NULL,?,?,?)""",
            (
                stable_id("JOB", event_id, "live_primary_evidence_review"),
                event_id,
                priority,
                now,
                stable_json({"candidate_observation_id": row["observation_id"]}),
                now,
                now,
            ),
        )
        connection.execute(
            """UPDATE observation_jobs SET status='COMPLETED_CANDIDATE',attempts=attempts+1,
               updated_at=? WHERE job_id=?""",
            (now, row["job_id"]),
        )
        result["candidates"] += 1
        result["event_ids"].append(event_id)
        result["by_type"][matched.event_type] = result["by_type"].get(matched.event_type, 0) + 1
    connection.commit()
    result["duplicate_events_reconciled"] = reconcile_verified_opennews_duplicates(connection)
    result["p2_pending_after_admission"] = pending_opennews_reviews(connection)
    result["unique_events"] = len(set(result["event_ids"]))
    return result


def write_report(path: Path, result: dict[str, Any], connection: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    status_rows = connection.execute(
        """SELECT status,COUNT(*) FROM observation_jobs
           WHERE job_type='extract_live_event_candidate' GROUP BY status ORDER BY status"""
    ).fetchall()
    lines = [
        "# Live Candidate Extraction",
        "",
        f"- Processed this run: `{result['processed']}`",
        f"- Candidate observations: `{result['candidates']}`",
        f"- Unique candidate events this run: `{result['unique_events']}`",
        f"- No-rule observations: `{result['no_candidate']}`",
        f"- Subject-unresolved observations kept outside canonical events: `{result.get('subject_filtered', 0)}`",
        f"- Retracted stale aggregated-discovery reviews: `{result.get('retracted_events', 0)}`",
        f"- Aggregated duplicates attached to verified events: `{result.get('duplicate_events_reconciled', 0)}`",
        "- Safety: every automatic output remains `candidate`; even P0 official discovery does not auto-verify severity.",
        "- Next job: `live_primary_evidence_review` against P0/P1 sources.",
        "",
        "## Event types",
        "",
    ]
    if result["by_type"]:
        lines.extend(
            f"- `{event_type}`: `{count}`"
            for event_type, count in sorted(result["by_type"].items())
        )
    else:
        lines.append("- None")
    lines.extend(["", "## Observation job status", ""])
    lines.extend(f"- `{row[0]}`: `{row[1]}`" for row in status_rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    connection = open_ledger(args.db)
    try:
        result = process_pending(connection, limit=args.limit)
        write_report(args.report, result, connection)
        print(stable_json(result))
        print(f"REPORT={args.report}")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
