#!/usr/bin/env python3
"""Fetch SEC filing index/primary documents and refine unverified live candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from event_ledger import open_ledger, stable_id, stable_json, utc_now
from extract_sec_evidence_text import visible_text
from telegram_mtproto_listener import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_ENV = ROOT / ".env"
DEFAULT_REPORT = ROOT / "reports" / "sec_filing_enrichment_latest.md"
SEC_BASE = "https://www.sec.gov"
ADMINISTRATIVE_NON_EVENT_TYPES = {
    "routine_nav_and_leverage_update",
    "routine_board_committee_appointment",
    "equity_incentive_plan_share_reserve_reduction",
    "routine_nt_10q_extension_request",
    "auditor_change_without_disagreement",
    "pro_forma_merger_financial_statement_amendment",
}


@dataclass(frozen=True)
class FilingDocument:
    sequence: str
    description: str
    document: str
    document_type: str
    size: str
    url: str


@dataclass(frozen=True)
class Classification:
    event_family: str | None
    event_type: str | None
    keywords: tuple[str, ...]
    confidence: float


class FilingIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_documents_table = False
        self.in_row = False
        self.in_cell = False
        self.cell_parts: list[str] = []
        self.cell_href: str | None = None
        self.row: list[tuple[str, str | None]] = []
        self.rows: list[list[tuple[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag.lower() == "table" and values.get("summary") == "Document Format Files":
            self.in_documents_table = True
        elif self.in_documents_table and tag.lower() == "tr":
            self.in_row = True
            self.row = []
        elif self.in_row and tag.lower() in {"td", "th"}:
            self.in_cell = True
            self.cell_parts = []
            self.cell_href = None
        elif self.in_cell and tag.lower() == "a" and values.get("href"):
            self.cell_href = values["href"]

    def handle_endtag(self, tag: str) -> None:
        if self.in_cell and tag.lower() in {"td", "th"}:
            self.row.append((" ".join("".join(self.cell_parts).split()), self.cell_href))
            self.in_cell = False
        elif self.in_row and tag.lower() == "tr":
            if self.row:
                self.rows.append(self.row)
            self.in_row = False
        elif self.in_documents_table and tag.lower() == "table":
            self.in_documents_table = False

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.cell_parts.append(data)


def canonical_document_url(href: str) -> str:
    parsed = urllib.parse.urlsplit(href)
    query = urllib.parse.parse_qs(parsed.query)
    if parsed.path == "/ix" and query.get("doc"):
        return urllib.parse.urljoin(SEC_BASE, query["doc"][0])
    return urllib.parse.urljoin(SEC_BASE, href)


def parse_filing_index(payload: bytes) -> list[FilingDocument]:
    parser = FilingIndexParser()
    parser.feed(payload.decode("utf-8", errors="replace"))
    documents: list[FilingDocument] = []
    for row in parser.rows:
        if len(row) < 5 or row[0][0].casefold() == "seq":
            continue
        href = row[2][1]
        if not href or "complete submission text file" in row[1][0].casefold():
            continue
        documents.append(
            FilingDocument(
                sequence=row[0][0],
                description=row[1][0],
                document=row[2][0].split()[0],
                document_type=row[3][0],
                size=row[4][0],
                url=canonical_document_url(href),
            )
        )
    return documents


RULES: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "audit_governance",
        "auditor_change_without_disagreement",
        (),
        ("declined to stand for reappointment", "no disagreements"),
    ),
    (
        "capital_return",
        "share_repurchase_authorization_expansion",
        (),
        ("share repurchase program", "aggregate authorization"),
    ),
    (
        "compensation_administration",
        "employee_warrant_grant",
        (),
        ("warrants to certain employees",),
    ),
    (
        "corporate_transaction",
        "business_combination_shareholder_approval",
        (),
        ("approved each of the business combination proposal",),
    ),
    (
        "reporting_compliance",
        "routine_nt_10q_extension_request",
        (),
        ("within the five-day extension period", "finalizing the financial statements"),
    ),
    (
        "operational_disruption",
        "precautionary_wildfire_evacuation_and_exploration_suspension",
        (),
        ("all personnel safely evacuated", "exploration activities temporarily suspended"),
    ),
    (
        "fund_reporting",
        "routine_nav_and_leverage_update",
        (),
        ("net asset value", "debt-to-equity ratio"),
    ),
    (
        "governance",
        "chief_financial_officer_appointment",
        (),
        ("appointment of chief financial officer",),
    ),
    (
        "listing_compliance",
        "minimum_bid_price_deficiency_notice",
        (),
        ("minimum bid price deficiency", "not of imminent delisting"),
    ),
    (
        "operating_results",
        "positive_preliminary_healthcare_operating_kpis",
        (),
        ("preliminary key performance indicators",),
    ),
    (
        "debt_financing",
        "credit_facility_expansion_extension_and_margin_reduction",
        (),
        ("extend and increase its revolving credit facility",),
    ),
    (
        "biopharma_regulatory",
        "nda_resubmission_regulatory_process_update",
        (),
        ("potential resubmission of the new drug application",),
    ),
    (
        "shareholder_administration",
        "annual_meeting_voting_report",
        (),
        ("voting results of the annual and special meeting",),
    ),
    (
        "transaction_accounting",
        "pro_forma_merger_financial_statement_amendment",
        (),
        ("unaudited pro forma condensed combined", "pro forma financial information"),
    ),
    (
        "governance_administration",
        "routine_board_committee_appointment",
        (),
        ("appointed to serve as a member of the management development and compensation committee",),
    ),
    (
        "compensation_administration",
        "equity_incentive_plan_share_reserve_reduction",
        (),
        ("reduce the number of shares of our common stock authorized for issuance",),
    ),
    ("distress", "bankruptcy", ("item 1.03",), ("chapter 11", "bankruptcy petition", "receivership")),
    ("listing_status", "delisting", ("item 3.01",), ("notice of delisting", "delist from", "listing noncompliance")),
    ("distress", "debt_default", ("item 2.04",), ("event that accelerates", "debt default", "covenant breach")),
    ("corporate_action", "restructuring", ("item 2.05", "item 2.06"), ("exit or disposal activities", "restructuring plan", "impairment charge")),
    ("governance", "management_change", ("item 5.02",), ("chief executive officer resigned", "chief financial officer resigned")),
    ("capital_structure", "offering_or_dilution", (), ("warrant inducement", "pre-funded warrants", "new warrants")),
    ("fundamental_distress", "going_concern_financing_dependency", (), ("substantial doubt", "continue as a going concern")),
    (
        "debt_financing",
        "debt_refinancing",
        (),
        ("use the net proceeds of the offering to repay in full",),
    ),
    ("debt_financing", "convertible_debt_financing", (), ("convertible senior notes",)),
    ("debt_financing", "senior_unsecured_debt_financing", (), ("senior unsecured notes",)),
    ("debt_financing", "credit_facility_amendment", (), ("credit facility amendment",)),
    ("debt_financing", "debt_refinancing", (), ("completed the refinancing", "clo reset transaction")),
    ("spac_financing", "spac_sponsor_working_capital_note", (), ("amended and restated working capital note", "sponsor advanced an additional")),
    ("spac_capital_formation", "spac_ipo_closing", (), ("completed its ipo", "trust account")),
    ("corporate_action", "merger_or_acquisition", (), ("merger agreement", "merger consideration", "closing occurred simultaneously")),
    ("capital_structure", "offering_or_dilution", (), ("registered direct offering", "public offering", "at-the-market offering")),
    ("earnings", "earnings_or_guidance", ("item 2.02",), ("results of operations and financial condition", "financial results", "revenue guidance")),
    ("corporate_action", "material_corporate_transaction", ("item 1.01", "item 1.02", "item 2.01"), ("material definitive agreement", "completion of acquisition", "merger agreement")),
    ("product_safety", "product_recall", (), ("product recall", "safety recall")),
    ("biopharma", "clinical_trial_update", (), ("topline results", "clinical trial met", "phase 3 trial", "phase iii trial")),
)


# These phrases describe a complete, narrow event and may safely repair a broad
# live-feed candidate even when its original event type did not come from the
# previous enrichment result. Generic Item 1.01 matches are deliberately absent.
SAFE_SPECIFIC_RECLASSIFY_TYPES = frozenset(
    {
        "auditor_change_without_disagreement",
        "share_repurchase_authorization_expansion",
        "employee_warrant_grant",
        "business_combination_shareholder_approval",
        "routine_nt_10q_extension_request",
        "precautionary_wildfire_evacuation_and_exploration_suspension",
        "routine_nav_and_leverage_update",
        "chief_financial_officer_appointment",
        "minimum_bid_price_deficiency_notice",
        "positive_preliminary_healthcare_operating_kpis",
        "credit_facility_expansion_extension_and_margin_reduction",
        "nda_resubmission_regulatory_process_update",
        "annual_meeting_voting_report",
        "pro_forma_merger_financial_statement_amendment",
        "routine_board_committee_appointment",
        "equity_incentive_plan_share_reserve_reduction",
        "going_concern_financing_dependency",
        "spac_sponsor_working_capital_note",
        "spac_ipo_closing",
        "debt_refinancing",
    }
)


def phrase_is_affirmed(text: str, phrase: str) -> bool:
    """Return true when at least one phrase occurrence is not locally negated."""
    for match in re.finditer(re.escape(phrase), text):
        prefix = text[max(0, match.start() - 100) : match.start()]
        if re.search(
            r"\b(?:never|neither|no|not|without)\b[^.;:]{0,90}$",
            prefix,
            flags=re.I,
        ):
            continue
        return True
    return False


def classify_filing_text(text: str) -> Classification:
    lowered = " ".join(text.casefold().split())
    best: tuple[int, int, str, str, tuple[str, ...]] | None = None
    for order, (family, event_type, exact_items, phrases) in enumerate(RULES):
        matched_items = tuple(value for value in exact_items if value in lowered)
        matched_phrases = tuple(value for value in phrases if phrase_is_affirmed(lowered, value))
        phrase_weight = 15 if not exact_items else 3
        score = len(matched_items) * 10 + len(matched_phrases) * phrase_weight
        if not score:
            continue
        candidate = (score, -order, family, event_type, matched_items + matched_phrases)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    if best is None:
        return Classification(None, None, (), 0.0)
    score, _order, family, event_type, keywords = best
    confidence = 0.96 if score >= 10 else min(0.9, 0.68 + score * 0.04)
    return Classification(family, event_type, tuple(sorted(set(keywords))), confidence)


def repair_negated_enrichment_matches(connection: Any) -> int:
    """Clear old machine matches whose stored evidence shows only negated phrases."""
    repaired = 0
    rows = connection.execute(
        """SELECT observation_id,evidence_excerpt,matched_event_type,matched_keywords_json
           FROM sec_filing_enrichments
           WHERE status='PARSED' AND matched_event_type IS NOT NULL"""
    ).fetchall()
    now = utc_now()
    for row in rows:
        try:
            keywords = tuple(json.loads(row["matched_keywords_json"] or "[]"))
        except (json.JSONDecodeError, TypeError):
            continue
        excerpt = " ".join(str(row["evidence_excerpt"] or "").casefold().split())
        present_phrases = [
            keyword
            for keyword in keywords
            if not keyword.startswith("item ") and keyword in excerpt
        ]
        if not present_phrases:
            continue
        if any(phrase_is_affirmed(excerpt, keyword) for keyword in present_phrases):
            continue
        connection.execute(
            """UPDATE sec_filing_enrichments SET matched_event_family=NULL,
               matched_event_type=NULL,matched_keywords_json='[]',confidence=0,
               updated_at=? WHERE observation_id=?""",
            (now, row["observation_id"]),
        )
        repaired += 1
    if repaired:
        connection.commit()
    return repaired


def reclassify_parsed_enrichments(connection: Any) -> int:
    """Re-run semantic rules on stored excerpts and safely retag candidate-only events."""
    repaired = 0
    rows = connection.execute(
        """SELECT x.observation_id,x.event_id,x.evidence_excerpt,x.matched_event_family,
                  x.matched_event_type,e.current_version,e.status,e.event_family,e.event_type
           FROM sec_filing_enrichments x
           JOIN canonical_events e ON e.event_id=x.event_id
           WHERE x.status='PARSED' AND COALESCE(x.evidence_excerpt,'')!=''"""
    ).fetchall()
    for row in rows:
        classification = classify_filing_text(str(row["evidence_excerpt"] or ""))
        if not classification.event_type or not classification.event_family:
            continue
        classification_changed = not (
            classification.event_type == row["matched_event_type"]
            and classification.event_family == row["matched_event_family"]
        )
        now = utc_now()
        if classification_changed:
            connection.execute(
                """UPDATE sec_filing_enrichments SET matched_event_family=?,matched_event_type=?,
                   matched_keywords_json=?,confidence=?,updated_at=? WHERE observation_id=?""",
                (
                    classification.event_family,
                    classification.event_type,
                    stable_json(classification.keywords),
                    classification.confidence,
                    now,
                    row["observation_id"],
                ),
            )
        safe_specific_override = classification.event_type in SAFE_SPECIFIC_RECLASSIFY_TYPES
        canonical_changed = row["status"] == "candidate" and row["event_type"] != classification.event_type and (
            row["event_type"] == row["matched_event_type"] or safe_specific_override
        )
        if canonical_changed:
            new_version = int(row["current_version"]) + 1
            facts = {
                "candidate_only": True,
                "refined_from": row["event_type"],
                "matched_event_type": classification.event_type,
                "matched_keywords": classification.keywords,
                "classification_confidence": classification.confidence,
                "source_observation_id": row["observation_id"],
                "auto_verification_allowed": False,
                "no_trading": True,
            }
            connection.execute(
                """UPDATE canonical_events SET current_version=?,event_family=?,event_type=?,
                   last_updated_at=? WHERE event_id=? AND status='candidate'""",
                (
                    new_version,
                    classification.event_family,
                    classification.event_type,
                    now,
                    row["event_id"],
                ),
            )
            connection.execute(
                """INSERT INTO event_versions(
                   event_id,version,changed_at,status,label_status,event_family,event_type,
                   manual_grade,facts_json,change_reason
                   ) VALUES (?,? ,?,'candidate','candidate',?,?,NULL,?,'sec_semantic_reclassification')""",
                (
                    row["event_id"],
                    new_version,
                    now,
                    classification.event_family,
                    classification.event_type,
                    stable_json(facts),
                ),
            )
        repaired += int(classification_changed or canonical_changed)
    if repaired:
        connection.commit()
    return repaired


def evidence_excerpt(text: str, keywords: tuple[str, ...], *, max_chars: int = 1600) -> str:
    compact = " ".join(text.split())
    if not compact:
        return ""
    lowered = compact.casefold()
    candidates: list[tuple[int, int]] = []
    for keyword in keywords:
        for match in re.finditer(re.escape(keyword), lowered):
            position = match.start()
            start = max(0, position - 350)
            window = compact[start : start + max_chars]
            window_lower = window.casefold()
            quantified_facts = len(
                re.findall(
                    r"(?:\$\s*\d[\d,.]*|\b\d+(?:\.\d+)?\s*%|\b\d+(?:\.\d+)?\s+(?:million|billion))",
                    window,
                    flags=re.I,
                )
            )
            material_terms = sum(
                term in window_lower
                for term in (
                    "net income",
                    "operating income",
                    "revenue",
                    "adjusted ebitda",
                    "cash flow",
                    "default",
                    "undercapitalized",
                    "completed",
                    "issued",
                    "expects",
                    "guidance",
                    "common shares",
                    "principal amount",
                )
            )
            boilerplate_penalty = 12 * sum(
                phrase in window_lower
                for phrase in (
                    "shall not be deemed to be filed",
                    "copy of the press release is attached",
                    "does not purport to be complete",
                )
            )
            semantic_bonus = 4 if not keyword.startswith("item ") else 0
            score = quantified_facts * 5 + material_terms * 3 + semantic_bonus - boilerplate_penalty
            candidates.append((score, start))
    start = max(candidates, key=lambda item: (item[0], item[1]))[1] if candidates else 0
    excerpt = compact[start : start + max_chars]
    if start:
        excerpt = "…" + excerpt
    if start + max_chars < len(compact):
        excerpt = excerpt.rstrip() + "…"
    return excerpt


def accession_number(raw_json: str, canonical_url: str) -> str:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        payload = {}
    item = payload.get("item") if isinstance(payload, dict) else None
    external_id = str(item.get("external_id") or "") if isinstance(item, dict) else ""
    match = re.search(r"(\d{10}-\d{2}-\d{6})", external_id or canonical_url)
    return match.group(1) if match else "unknown"


class SecFilingClient:
    def __init__(
        self,
        user_agent: str,
        *,
        min_interval: float = 0.12,
        max_bytes: int = 1_500_000,
        timeout: float = 30.0,
    ) -> None:
        if not user_agent or "@" not in user_agent:
            raise ValueError("SEC_USER_AGENT must identify the application and include an email")
        self.user_agent = user_agent
        self.min_interval = min_interval
        self.max_bytes = max_bytes
        self.timeout = timeout
        self._last_request = 0.0

    def get(self, url: str) -> bytes:
        delay = self.min_interval - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,text/plain",
                "Accept-Encoding": "identity",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read(self.max_bytes + 1)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"SEC request failed for {url}: {exc}") from exc
        self._last_request = time.monotonic()
        return payload[: self.max_bytes]


def choose_documents(documents: list[FilingDocument], form: str) -> list[FilingDocument]:
    base_form = form.removesuffix("/A")
    primary = next(
        (row for row in documents if row.document_type.removesuffix("/A") == base_form),
        documents[0] if documents else None,
    )
    selected = [primary] if primary is not None else []
    selected.extend(
        row
        for row in documents
        if row not in selected and row.document_type.upper().startswith("EX-99")
    )
    return selected[:3]


def upsert_enrichment(
    connection: Any,
    *,
    row: Any,
    accession: str,
    documents: list[FilingDocument],
    primary_url: str | None,
    text: str,
    classification: Classification,
    status: str,
    attempts: int,
    error: str | None,
) -> None:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO sec_filing_enrichments(
            enrichment_id,event_id,observation_id,accession_number,form,filing_index_url,
            primary_document_url,documents_json,evidence_excerpt,text_sha256,
            matched_event_family,matched_event_type,matched_keywords_json,confidence,status,
            attempts,last_error,fetched_at,updated_at,read_only,no_trading
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,1)
        ON CONFLICT(observation_id) DO UPDATE SET
            primary_document_url=excluded.primary_document_url,
            documents_json=excluded.documents_json,
            evidence_excerpt=excluded.evidence_excerpt,
            text_sha256=excluded.text_sha256,
            matched_event_family=excluded.matched_event_family,
            matched_event_type=excluded.matched_event_type,
            matched_keywords_json=excluded.matched_keywords_json,
            confidence=excluded.confidence,status=excluded.status,attempts=excluded.attempts,
            last_error=excluded.last_error,fetched_at=excluded.fetched_at,
            updated_at=excluded.updated_at,read_only=1,no_trading=1
        """,
        (
            stable_id("SECENRICH", row["observation_id"]),
            row["event_id"],
            row["observation_id"],
            accession,
            row["form"],
            row["canonical_url"],
            primary_url,
            stable_json([document.__dict__ for document in documents]),
            evidence_excerpt(text, classification.keywords),
            hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None,
            classification.event_family,
            classification.event_type,
            stable_json(classification.keywords),
            classification.confidence,
            status,
            attempts,
            error,
            now if status == "PARSED" else None,
            now,
        ),
    )


def refine_event(connection: Any, row: Any, classification: Classification) -> bool:
    if (
        row["event_type"] != "sec_material_filing"
        or not classification.event_type
        or not classification.event_family
    ):
        return False
    now = utc_now()
    new_version = int(row["current_version"]) + 1
    facts = {
        "candidate_only": True,
        "refined_from": row["event_type"],
        "matched_event_type": classification.event_type,
        "matched_keywords": classification.keywords,
        "classification_confidence": classification.confidence,
        "source_observation_id": row["observation_id"],
        "auto_verification_allowed": False,
        "no_trading": True,
    }
    connection.execute(
        """UPDATE canonical_events SET current_version=?,event_family=?,event_type=?,
           last_updated_at=? WHERE event_id=? AND status='candidate'""",
        (
            new_version,
            classification.event_family,
            classification.event_type,
            now,
            row["event_id"],
        ),
    )
    connection.execute(
        """INSERT INTO event_versions(
           event_id,version,changed_at,status,label_status,event_family,event_type,
           manual_grade,facts_json,change_reason
           ) VALUES (?,? ,?,'candidate','candidate',?,?,NULL,?,'sec_primary_document_refinement')""",
        (
            row["event_id"],
            new_version,
            now,
            classification.event_family,
            classification.event_type,
            stable_json(facts),
        ),
    )
    return True


def _reject_sec_noise_event(connection: Any, row: Any, *, reason: str, now: str) -> bool:
    if str(row["event_status"]) != "candidate":
        return False
    version_row = connection.execute(
        "SELECT facts_json FROM event_versions WHERE event_id=? AND version=?",
        (row["event_id"], row["current_version"]),
    ).fetchone()
    try:
        facts = json.loads(version_row["facts_json"]) if version_row else {}
    except (json.JSONDecodeError, TypeError):
        facts = {}
    facts["sec_semantic_filter"] = {
        "reason": reason,
        "parsed_primary_document": True,
        "raw_observation_preserved": True,
        "filtered_at": now,
    }
    new_version = int(row["current_version"]) + 1
    connection.execute(
        """INSERT INTO event_versions(
           event_id,version,changed_at,status,label_status,event_family,event_type,
           manual_grade,facts_json,change_reason
           ) VALUES (?,?,?,'rejected','rejected',?,?,?,?,?)""",
        (
            row["event_id"],
            new_version,
            now,
            row["event_family"],
            row["event_type"],
            row["manual_grade"],
            stable_json(facts),
            reason,
        ),
    )
    connection.execute(
        """UPDATE canonical_events
           SET current_version=?,status='rejected',label_status='rejected',last_updated_at=?
           WHERE event_id=? AND status='candidate'""",
        (new_version, now, row["event_id"]),
    )
    return True


def materialize_parsed_enrichment_evidence(connection: Any) -> dict[str, int]:
    """Bridge parsed SEC primary documents into the canonical evidence state machine."""
    rows = connection.execute(
        """SELECT e.event_id,e.current_version,e.status AS event_status,e.event_family,
                  e.event_type,e.manual_grade,r.observation_id,r.source_published_at,
                  r.canonical_url,r.raw_json,x.form,x.filing_index_url,
                  x.primary_document_url,x.evidence_excerpt,x.matched_event_type,
                  x.matched_keywords_json,x.confidence
           FROM sec_filing_enrichments x
           JOIN canonical_events e ON e.event_id=x.event_id
           JOIN latest_source_content r ON r.observation_id=x.observation_id
           WHERE x.status='PARSED'"""
    ).fetchall()
    result = {
        "parsed_rows": len(rows),
        "evidence_inserted": 0,
        "evidence_updated": 0,
        "evidence_unchanged": 0,
        "reviewed_status_preserved": 0,
        "jobs_advanced": 0,
        "semantic_noise_rejected": 0,
    }
    now = utc_now()
    for row in rows:
        try:
            payload = json.loads(row["raw_json"])
        except json.JSONDecodeError:
            payload = {}
        item = payload.get("item") if isinstance(payload, dict) else None
        item_codes = item.get("items") if isinstance(item, dict) else []
        if not isinstance(item_codes, list):
            item_codes = []
        try:
            keywords = json.loads(row["matched_keywords_json"] or "[]")
        except json.JSONDecodeError:
            keywords = []
        if not isinstance(keywords, list):
            keywords = []
        evidence_url = str(
            row["primary_document_url"]
            or row["filing_index_url"]
            or row["canonical_url"]
            or ""
        )
        evidence_id = stable_id("EVID", str(row["event_id"]), str(row["observation_id"]))
        excerpt = str(row["evidence_excerpt"] or "").strip()
        filing_date = str(row["source_published_at"] or "")[:10] or None
        items_text = ";".join(str(code) for code in item_codes)
        keywords_text = ";".join(str(keyword) for keyword in keywords)
        passage_score = max(0, min(100, round(float(row["confidence"] or 0) * 100)))
        evidence_status = (
            "machine_extracted_unreviewed" if excerpt else "link_only_no_relevant_passage"
        )
        existing = connection.execute(
            """SELECT evidence_url,filing_date,form,items,evidence_passage,
                      matched_keywords,passage_score,evidence_status,auto_verification_allowed
               FROM event_evidence WHERE event_id=? AND observation_id=?""",
            (row["event_id"], row["observation_id"]),
        ).fetchone()
        machine_statuses = {"machine_extracted_unreviewed", "link_only_no_relevant_passage"}
        if existing is not None and str(existing["evidence_status"]) not in machine_statuses:
            result["reviewed_status_preserved"] += 1
        else:
            desired = (
                evidence_url,
                filing_date,
                str(row["form"] or ""),
                items_text,
                excerpt,
                keywords_text,
                passage_score,
                evidence_status,
                0,
            )
            current = None
            if existing is not None:
                current = (
                    str(existing["evidence_url"] or ""),
                    existing["filing_date"],
                    str(existing["form"] or ""),
                    str(existing["items"] or ""),
                    str(existing["evidence_passage"] or ""),
                    str(existing["matched_keywords"] or ""),
                    existing["passage_score"],
                    str(existing["evidence_status"]),
                    int(existing["auto_verification_allowed"]),
                )
            if current == desired:
                result["evidence_unchanged"] += 1
            else:
                connection.execute(
                    """INSERT INTO event_evidence(
                       evidence_id,event_id,observation_id,evidence_url,filing_date,form,items,
                       evidence_passage,matched_keywords,passage_score,evidence_status,
                       auto_verification_allowed,created_at,updated_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?)
                       ON CONFLICT(event_id,observation_id) DO UPDATE SET
                           evidence_url=excluded.evidence_url,
                           filing_date=excluded.filing_date,
                           form=excluded.form,
                           items=excluded.items,
                           evidence_passage=excluded.evidence_passage,
                           matched_keywords=excluded.matched_keywords,
                           passage_score=excluded.passage_score,
                           evidence_status=excluded.evidence_status,
                           auto_verification_allowed=0,
                           updated_at=excluded.updated_at""",
                    (
                        evidence_id,
                        row["event_id"],
                        row["observation_id"],
                        evidence_url,
                        filing_date,
                        row["form"],
                        items_text,
                        excerpt,
                        keywords_text,
                        passage_score,
                        evidence_status,
                        now,
                        now,
                    ),
                )
                result["evidence_inserted" if existing is None else "evidence_updated"] += 1

        matched_type = str(row["matched_event_type"] or "")
        semantic_noise = matched_type in ADMINISTRATIVE_NON_EVENT_TYPES or (
            str(row["event_type"]) == "sec_material_filing" and not matched_type
        )
        if semantic_noise:
            reason = (
                f"sec_primary_semantic_non_event:{matched_type}"
                if matched_type
                else "sec_primary_semantic_non_event:no_scoped_event_match"
            )
            if _reject_sec_noise_event(connection, row, reason=reason, now=now):
                result["semantic_noise_rejected"] += 1
            cursor = connection.execute(
                """UPDATE pipeline_jobs
                   SET status='COMPLETED_DISCOVERY_FILTERED',last_error=?,updated_at=?
                   WHERE event_id=? AND job_type='live_primary_evidence_review'
                     AND status='PENDING_PRIMARY_EVIDENCE'""",
                (reason, now, row["event_id"]),
            )
        else:
            cursor = connection.execute(
                """UPDATE pipeline_jobs
                   SET status='PENDING_EVIDENCE_REVIEW',last_error=NULL,updated_at=?
                   WHERE event_id=? AND job_type='live_primary_evidence_review'
                     AND status='PENDING_PRIMARY_EVIDENCE'""",
                (now, row["event_id"]),
            )
        result["jobs_advanced"] += cursor.rowcount
    connection.commit()
    return result


def pending_rows(connection: Any, *, limit: int, refresh_parsed: bool = False) -> list[Any]:
    enrichment_filter = (
        "(x.observation_id IS NULL OR x.status='PARSED' OR (x.status='ERROR' AND x.attempts<3))"
        if refresh_parsed
        else "(x.observation_id IS NULL OR (x.status='ERROR' AND x.attempts<3))"
    )
    return connection.execute(
        f"""
        SELECT e.event_id,e.current_version,e.event_type,e.event_family,
               r.observation_id,r.canonical_url,r.raw_json,
               json_extract(r.raw_json,'$.item.form') AS form,
               COALESCE(x.attempts,0) AS prior_attempts
        FROM canonical_events e
        JOIN event_observations eo ON eo.event_id=e.event_id
        JOIN latest_source_content r ON r.observation_id=eo.observation_id
        LEFT JOIN sec_filing_enrichments x ON x.observation_id=r.observation_id
        WHERE e.status='candidate' AND r.source_id='sec_current_filings'
          AND {enrichment_filter}
        ORDER BY r.source_published_at DESC,r.observation_id LIMIT ?
        """,
        (limit,),
    ).fetchall()


def enrich_pending(
    connection: Any,
    client: SecFilingClient,
    *,
    limit: int = 8,
    refresh_parsed: bool = False,
) -> dict[str, Any]:
    rows = pending_rows(connection, limit=limit, refresh_parsed=refresh_parsed)
    result: dict[str, Any] = {
        "requested": len(rows),
        "refresh_parsed": refresh_parsed,
        "parsed": 0,
        "no_document": 0,
        "errors": 0,
        "refined": 0,
        "by_type": {},
    }
    for row in rows:
        attempts = int(row["prior_attempts"]) + 1
        accession = accession_number(row["raw_json"], row["canonical_url"])
        documents: list[FilingDocument] = []
        selected: list[FilingDocument] = []
        combined_text = ""
        try:
            index_payload = client.get(row["canonical_url"])
            documents = parse_filing_index(index_payload)
            selected = choose_documents(documents, str(row["form"] or ""))
            if not selected:
                upsert_enrichment(
                    connection,
                    row=row,
                    accession=accession,
                    documents=documents,
                    primary_url=None,
                    text="",
                    classification=Classification(None, None, (), 0.0),
                    status="NO_DOCUMENT",
                    attempts=attempts,
                    error=None,
                )
                result["no_document"] += 1
                continue
            texts = [visible_text(client.get(document.url)) for document in selected]
            combined_text = "\n".join(text for text in texts if text)
            classification = classify_filing_text(combined_text)
            upsert_enrichment(
                connection,
                row=row,
                accession=accession,
                documents=documents,
                primary_url=selected[0].url,
                text=combined_text,
                classification=classification,
                status="PARSED",
                attempts=attempts,
                error=None,
            )
            result["parsed"] += 1
            if classification.event_type:
                result["by_type"][classification.event_type] = (
                    result["by_type"].get(classification.event_type, 0) + 1
                )
            result["refined"] += int(refine_event(connection, row, classification))
        except (RuntimeError, urllib.error.URLError, ValueError) as exc:
            upsert_enrichment(
                connection,
                row=row,
                accession=accession,
                documents=documents,
                primary_url=selected[0].url if selected else None,
                text=combined_text,
                classification=Classification(None, None, (), 0.0),
                status="ERROR",
                attempts=attempts,
                error=str(exc)[:500],
            )
            result["errors"] += 1
        finally:
            connection.commit()
    return result


def write_report(path: Path, result: dict[str, Any], connection: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    statuses = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT status,COUNT(*) FROM sec_filing_enrichments GROUP BY status ORDER BY status"
        )
    }
    lines = [
        "# SEC Filing Enrichment",
        "",
        f"- Requested this run: `{result['requested']}`",
        f"- Parsed: `{result['parsed']}`",
        f"- Candidate types refined: `{result['refined']}`",
        f"- Negated old machine matches repaired: `{result.get('negated_match_repairs', 0)}`",
        f"- Stored semantic classifications repaired: `{result.get('semantic_reclassifications', 0)}`",
        f"- Errors: `{result['errors']}`",
        f"- Parsed SEC rows materialized as evidence: `{result.get('evidence_materialization', {}).get('evidence_inserted', 0)}`",
        f"- Evidence jobs advanced: `{result.get('evidence_materialization', {}).get('jobs_advanced', 0)}`",
        f"- Parsed generic/administrative filings filtered: `{result.get('evidence_materialization', {}).get('semantic_noise_rejected', 0)}`",
        f"- Ledger status totals: `{json.dumps(statuses, sort_keys=True)}`",
        "- Safety: document text may refine a candidate type but cannot verify severity or enable trading.",
        "",
        "## Matched types",
        "",
    ]
    lines.extend(f"- {key}: `{value}`" for key, value in sorted(result["by_type"].items()))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument(
        "--refresh-parsed",
        action="store_true",
        help="Re-fetch parsed candidate filings to apply improved extraction and classification rules.",
    )
    args = parser.parse_args()
    load_dotenv(args.env_file)
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    client = SecFilingClient(user_agent)
    connection = open_ledger(args.db)
    try:
        result = enrich_pending(
            connection,
            client,
            limit=args.limit,
            refresh_parsed=args.refresh_parsed,
        )
        result["negated_match_repairs"] = repair_negated_enrichment_matches(connection)
        result["semantic_reclassifications"] = reclassify_parsed_enrichments(connection)
        result["evidence_materialization"] = materialize_parsed_enrichment_evidence(connection)
        write_report(args.report, result, connection)
    finally:
        connection.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print(f"REPORT={args.report}")
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
