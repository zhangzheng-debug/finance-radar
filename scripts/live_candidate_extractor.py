#!/usr/bin/env python3
"""Turn pending live RawObservations into auditable, unverified event candidates."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from event_ledger import open_ledger, stable_id, stable_json, utc_now


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_REPORT = ROOT / "reports" / "live_candidate_extraction_latest.md"


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
    ("iran", re.compile(r"\biran(?:ian)?\b", re.I)),
    ("russia", re.compile(r"\brussia(?:n)?\b", re.I)),
    ("ecb", re.compile(r"\b(?:ecb|european central bank)\b", re.I)),
    ("federal_reserve", re.compile(r"\b(?:federal reserve|fed)\b", re.I)),
    ("sec", re.compile(r"\b(?:u\.s\.\s+)?sec\b", re.I)),
    ("binance", re.compile(r"\bbinance\b", re.I)),
    ("coinbase", re.compile(r"\bcoinbase\b", re.I)),
)
CROSS_SOURCE_CLUSTER_ENTITIES = {"ostium", "binance", "coinbase"}

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
        return classify(discovery_text) if discovery_text else None

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
    elif entity:
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
           WHERE e.event_date=? AND e.event_type=?
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
    """Close review jobs for P2-only candidates that no longer pass discovery rules."""
    rows = connection.execute(
        """SELECT e.event_id,r.source_id,r.source_published_at,r.local_received_at,
                  r.title,r.summary,r.canonical_url,r.raw_json,s.authority_tier
           FROM canonical_events e
           JOIN pipeline_jobs j ON j.event_id=e.event_id
           JOIN event_observations eo ON eo.event_id=e.event_id
           JOIN latest_source_content r ON r.observation_id=eo.observation_id
           JOIN sources s ON s.source_id=r.source_id
           WHERE e.status='candidate'
             AND e.discovery_source='opennews_free'
             AND j.job_type='live_primary_evidence_review'
             AND j.status='PENDING_PRIMARY_EVIDENCE'
           ORDER BY e.event_id"""
    ).fetchall()
    by_event: dict[str, list[Any]] = {}
    for row in rows:
        by_event.setdefault(str(row["event_id"]), []).append(row)
    now = utc_now()
    retracted = 0
    for event_id, observations in by_event.items():
        has_primary_source = any(
            str(row["authority_tier"]).startswith(("P0", "P1"))
            for row in observations
        )
        still_matches = any(classify_observation(row) is not None for row in observations)
        if has_primary_source or still_matches:
            continue
        cursor = connection.execute(
            """UPDATE pipeline_jobs
               SET status='COMPLETED_DISCOVERY_FILTERED',
                   last_error='aggregated_story_no_longer_passes_event_semantic_gate',
                   updated_at=?
               WHERE event_id=? AND job_type='live_primary_evidence_review'
                 AND status='PENDING_PRIMARY_EVIDENCE'""",
            (now, event_id),
        )
        retracted += cursor.rowcount
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


def extract_symbol(raw_json: str) -> str | None:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    item = payload.get("item") if isinstance(payload, dict) else None
    if not isinstance(item, dict):
        return None
    coins = item.get("coins")
    if isinstance(coins, list) and coins:
        first = coins[0]
        if isinstance(first, dict):
            return str(first.get("symbol") or "").strip() or None
        return str(first).strip() or None
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


def provisional_grade_cap(authority_tier: str) -> str:
    if authority_tier.startswith("P0"):
        return "A_P0_official_candidate"
    if authority_tier.startswith("P1"):
        return "A_P1_primary_candidate"
    return "B_P2_discovery_only"


def process_pending(connection: Any, *, limit: int) -> dict[str, Any]:
    rows = connection.execute(
        """
        SELECT j.*,r.source_id,r.source_published_at,r.local_received_at,r.title,r.summary,
               r.canonical_url,r.raw_json,s.authority_tier
        FROM observation_jobs j
        JOIN latest_source_content r ON r.observation_id=j.observation_id
        JOIN sources s ON s.source_id=r.source_id
        WHERE j.job_type='extract_live_event_candidate' AND j.status='PENDING'
        ORDER BY j.priority DESC,j.available_at,j.job_id LIMIT ?
        """,
        (limit,),
    ).fetchall()
    now = utc_now()
    result: dict[str, Any] = {
        "processed": 0,
        "candidates": 0,
        "no_candidate": 0,
        "new_events": 0,
        "linked_observations": 0,
        "by_type": {},
        "event_ids": [],
    }
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

        event_id = existing_story_event_id(connection, row, matched) or live_event_id(row, matched)
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
        existing = connection.execute(
            "SELECT 1 FROM canonical_events WHERE event_id=?", (event_id,)
        ).fetchone()
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
                extract_symbol(row["raw_json"]),
                extract_company(row["raw_json"]),
                grade_cap,
                row["source_id"],
            ),
        )
        if existing is None:
            result["new_events"] += 1
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
    result["retracted_events"] = retract_filtered_opennews_candidates(connection)
    result["duplicate_events_reconciled"] = reconcile_verified_opennews_duplicates(connection)
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
