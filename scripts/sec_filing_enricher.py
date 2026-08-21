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

from app.services.event_admission import (
    ADMISSION_CONTRACT_VERSION,
    FACT_SLOT_CONTRACT_VERSION,
    evaluate_event_admission,
    extract_evidence_fact_slots,
    public_fact_summary,
    requires_specific_fact_extraction,
)
from event_ledger import open_ledger, stable_id, stable_json, utc_now
from extract_sec_evidence_text import visible_text
from live_candidate_extractor import (
    EventRule,
    canonicalize_url,
    live_event_id,
    provisional_grade_cap,
)
from telegram_mtproto_listener import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_ENV = ROOT / ".env"
DEFAULT_REPORT = ROOT / "reports" / "sec_filing_enrichment_latest.md"
SEC_BASE = "https://www.sec.gov"
DOCUMENT_MANIFEST_VERSION = 2
RELEVANT_EXHIBIT_PREFIXES = ("EX-2", "EX-4", "EX-10", "EX-99")
MAX_SELECTED_DOCUMENTS = 8
ADMINISTRATIVE_NON_EVENT_TYPES = {
    "routine_nav_and_leverage_update",
    "pro_forma_merger_financial_statement_amendment",
}
EVENT_ACTION_LABELS = {
    "bankruptcy": "破产或重整程序",
    "delisting": "退市或上市资格变化",
    "debt_default": "债务违约或契约事项",
    "restructuring": "重组或退出处置活动",
    "management_change": "管理层任免或离职",
    "earnings_or_guidance": "经营结果或业绩指引",
    "material_corporate_transaction": "重大公司交易",
    "going_concern_financing_dependency": "持续经营或融资依赖",
    "share_repurchase_authorization_expansion": "股份回购授权",
}
EVENT_STAGE_BY_TYPE = {
    "bankruptcy": "FILED",
    "delisting": "DISCLOSED",
    "debt_default": "DISCLOSED",
    "restructuring": "DISCLOSED",
    "management_change": "DISCLOSED",
    "earnings_or_guidance": "DISCLOSED",
    "material_corporate_transaction": "DISCLOSED",
    "going_concern_financing_dependency": "DISCLOSED",
    "share_repurchase_authorization_expansion": "DISCLOSED",
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
    if not candidates:
        # A no-match filing still needs an honest, useful review excerpt. Do not
        # default to the SEC cover page: rank bounded windows and prefer
        # substantive disclosure text over XBRL/registrant boilerplate.
        step = max(300, max_chars // 2)
        for start in range(0, max(1, len(compact)), step):
            window = compact[start : start + max_chars]
            if not window:
                continue
            window_lower = window.casefold()
            facts = len(
                re.findall(
                    r"(?:\$\s*\d[\d,.]*|\b\d+(?:\.\d+)?\s*%|\b\d+(?:\.\d+)?\s+(?:million|billion))",
                    window,
                    flags=re.I,
                )
            )
            disclosure_terms = sum(
                term in window_lower
                for term in (
                    "item 1.01",
                    "item 2.01",
                    "item 2.02",
                    "item 2.04",
                    "item 3.01",
                    "item 4.02",
                    "item 5.02",
                    "item 7.01",
                    "item 8.01",
                    "exhibit 99",
                    "press release",
                    "revenue",
                    "guidance",
                    "agreement",
                    "resigned",
                    "default",
                    "investigation",
                )
            )
            cover_penalty = 20 * sum(
                phrase in window_lower
                for phrase in (
                    "united states securities and exchange commission washington",
                    "check the appropriate box below",
                    "pursuant to section 13 or 15",
                    "state or other jurisdiction of incorporation",
                )
            )
            candidates.append((facts * 5 + disclosure_terms * 4 - cover_penalty, start))
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
        max_bytes: int = 5_000_000,
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
        if len(payload) > self.max_bytes:
            raise RuntimeError(
                f"SEC document exceeds safe capture limit ({self.max_bytes} bytes): {url}"
            )
        return payload


def is_relevant_exhibit(document: FilingDocument) -> bool:
    document_type = document.document_type.upper()
    description = document.description.casefold()
    return (
        document_type.startswith(RELEVANT_EXHIBIT_PREFIXES)
        or "press release" in description
        or "material agreement" in description
    ) and not document_type.startswith(("EX-101", "GRAPHIC", "XML", "ZIP"))


def choose_documents(
    documents: list[FilingDocument],
    form: str,
    *,
    max_documents: int = MAX_SELECTED_DOCUMENTS,
) -> list[FilingDocument]:
    """Choose the primary filing plus decision-relevant exhibits.

    The previous implementation only kept two exhibits after the primary
    document. That was enough for many filings, but could silently omit the
    press release or material agreement that contains the actual Item 7.01/
    9.01 disclosure.
    """
    base_form = form.removesuffix("/A")
    primary = next(
        (row for row in documents if row.document_type.removesuffix("/A") == base_form),
        documents[0] if documents else None,
    )
    selected = [primary] if primary is not None else []
    selected.extend(
        row
        for row in documents
        if row not in selected and is_relevant_exhibit(row)
    )
    return selected[: max(1, max_documents)]


def item_codes_from_raw(raw_json: str) -> tuple[str, ...]:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return ()
    item = payload.get("item") if isinstance(payload, dict) else None
    values = item.get("items") if isinstance(item, dict) else None
    if not isinstance(values, list):
        return ()
    return tuple(str(value).strip() for value in values if str(value).strip())


def document_manifest(
    documents: list[FilingDocument],
    selected: list[FilingDocument],
    fetched_texts: dict[str, str],
    *,
    item_codes: tuple[str, ...],
    error: str | None = None,
) -> dict[str, Any]:
    relevant = [document for document in documents if is_relevant_exhibit(document)]
    selected_urls = {document.url for document in selected}
    fetched_urls = set(fetched_texts)
    omitted = [document.url for document in relevant if document.url not in selected_urls]
    missing = [document.url for document in selected if document.url not in fetched_urls]
    expects_exhibit = any(code == "9.01" for code in item_codes)
    if error:
        coverage_status = "INCOMPLETE_FETCH_ERROR"
    elif omitted:
        coverage_status = "INCOMPLETE_SELECTION_LIMIT"
    elif missing:
        coverage_status = "INCOMPLETE_FETCH"
    elif expects_exhibit and not relevant:
        coverage_status = "INCOMPLETE_EXPECTED_EXHIBIT"
    else:
        coverage_status = "COMPLETE"
    return {
        "manifest_version": DOCUMENT_MANIFEST_VERSION,
        "coverage_status": coverage_status,
        "item_codes": list(item_codes),
        "documents": [document.__dict__ for document in documents],
        "selected_documents": [
            {
                **document.__dict__,
                "fetched": document.url in fetched_urls,
                "text_chars": len(fetched_texts.get(document.url, "")),
                "text_sha256": (
                    hashlib.sha256(fetched_texts[document.url].encode("utf-8")).hexdigest()
                    if document.url in fetched_urls
                    else None
                ),
            }
            for document in selected
        ],
        "omitted_relevant_urls": omitted,
        "missing_selected_urls": missing,
        "error": error,
    }


def manifest_coverage(value: str) -> str:
    """Read v2 manifest coverage while remaining compatible with legacy lists."""
    try:
        payload = json.loads(value or "[]")
    except json.JSONDecodeError:
        return "LEGACY_UNKNOWN"
    if isinstance(payload, dict):
        return str(payload.get("coverage_status") or "UNKNOWN")
    return "LEGACY_UNKNOWN"


def manifest_evidence_url(value: str, default: str) -> str:
    try:
        payload = json.loads(value or "[]")
    except json.JSONDecodeError:
        return default
    if not isinstance(payload, dict):
        return default
    selected = payload.get("selected_documents")
    if not isinstance(selected, list):
        return default
    for document in selected:
        if not isinstance(document, dict) or not document.get("fetched"):
            continue
        document_type = str(document.get("document_type") or "").upper()
        if document_type.startswith(RELEVANT_EXHIBIT_PREFIXES):
            return str(document.get("url") or default)
    return default


def substantive_text_score(text: str, document: FilingDocument) -> int:
    compact = " ".join(text.split())
    lowered = compact.casefold()
    score = min(len(compact) // 80, 40)
    score += 25 if is_relevant_exhibit(document) else 0
    score += 8 * sum(
        term in lowered
        for term in (
            "revenue",
            "net income",
            "guidance",
            "default",
            "bankruptcy",
            "resigned",
            "acquisition",
            "merger",
            "investigation",
            "press release",
        )
    )
    score += 3 * len(re.findall(r"\$\s*\d|\b\d+(?:\.\d+)?\s*%", compact))
    score -= 35 * sum(
        phrase in lowered
        for phrase in (
            "united states securities and exchange commission washington",
            "check the appropriate box below",
            "pursuant to section 13 or 15",
        )
    )
    return score


def select_excerpt_source(
    fetched_documents: list[tuple[FilingDocument, str]],
    classification: Classification,
) -> str:
    if not fetched_documents:
        return ""
    ranked: list[tuple[int, str]] = []
    for document, text in fetched_documents:
        document_classification = classify_filing_text(text)
        semantic_match = int(
            bool(classification.event_type)
            and document_classification.event_type == classification.event_type
        )
        ranked.append(
            (
                semantic_match * 1000
                + len(document_classification.keywords) * 100
                + substantive_text_score(text, document),
                text,
            )
        )
    return max(ranked, key=lambda item: item[0])[1]


def upsert_enrichment(
    connection: Any,
    *,
    row: Any,
    accession: str,
    documents: list[FilingDocument],
    selected_documents: list[FilingDocument],
    fetched_texts: dict[str, str],
    primary_url: str | None,
    text: str,
    excerpt_source_text: str | None,
    classification: Classification,
    status: str,
    attempts: int,
    error: str | None,
) -> None:
    now = utc_now()
    items = item_codes_from_raw(str(row["raw_json"] or ""))
    manifest = document_manifest(
        documents,
        selected_documents,
        fetched_texts,
        item_codes=items,
        error=error,
    )
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
            stable_json(manifest),
            evidence_excerpt(
                excerpt_source_text if excerpt_source_text is not None else text,
                classification.keywords,
            ),
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


def reopen_inconclusive_sec_events(connection: Any) -> int:
    """Repair historical generic SEC rows that were rejected without a scoped match.

    A missing semantic match is not proof that a filing is a non-event,
    especially for Item 7.01/9.01 filings whose substance may live in an
    exhibit. Reopen only the exact legacy terminal reason; reviewed or
    explicitly administrative rejections remain untouched.
    """
    rows = connection.execute(
        """SELECT e.event_id,e.current_version,e.event_family,e.event_type,e.manual_grade,
                  v.facts_json,x.observation_id
           FROM canonical_events e
           JOIN event_versions v
             ON v.event_id=e.event_id AND v.version=e.current_version
           JOIN sec_filing_enrichments x ON x.event_id=e.event_id
           WHERE e.status='rejected'
             AND v.change_reason='sec_primary_semantic_non_event:no_scoped_event_match'
             AND x.matched_event_type IS NULL"""
    ).fetchall()
    now = utc_now()
    reopened = 0
    for row in rows:
        try:
            facts = json.loads(row["facts_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            facts = {}
        facts["sec_semantic_filter"] = {
            "reason": "sec_semantic_inconclusive:no_scoped_event_match",
            "terminal": False,
            "attachment_complete_required": True,
            "reopened_at": now,
        }
        new_version = int(row["current_version"]) + 1
        connection.execute(
            """INSERT INTO event_versions(
               event_id,version,changed_at,status,label_status,event_family,event_type,
               manual_grade,facts_json,change_reason
               ) VALUES (?,?,?,'candidate','candidate',?,?,?,?,?)""",
            (
                row["event_id"],
                new_version,
                now,
                row["event_family"],
                row["event_type"],
                row["manual_grade"],
                stable_json(facts),
                "sec_primary_semantic_inconclusive_reopened",
            ),
        )
        connection.execute(
            """UPDATE canonical_events
               SET current_version=?,status='candidate',label_status='candidate',last_updated_at=?
               WHERE event_id=? AND status='rejected'""",
            (new_version, now, row["event_id"]),
        )
        connection.execute(
            """UPDATE sec_filing_enrichments
               SET status='ERROR',attempts=0,
                   last_error='reprocess_after_nonterminal_no_match_policy',updated_at=?
               WHERE observation_id=?""",
            (now, row["observation_id"]),
        )
        connection.execute(
            """UPDATE pipeline_jobs
               SET status='PENDING_PRIMARY_EVIDENCE',attempts=0,available_at=?,
                   last_error='reprocess_after_nonterminal_no_match_policy',updated_at=?
               WHERE event_id=? AND job_type='live_primary_evidence_review'""",
            (now, now, row["event_id"]),
        )
        reopened += 1
    if reopened:
        connection.commit()
    return reopened


def materialize_parsed_enrichment_evidence(connection: Any) -> dict[str, int]:
    """Bridge parsed SEC primary documents into the canonical evidence state machine."""
    rows = connection.execute(
        """SELECT e.event_id,e.current_version,e.status AS event_status,e.event_family,
                  e.event_type,e.manual_grade,r.observation_id,r.source_published_at,
                  r.canonical_url,r.raw_json,x.form,x.filing_index_url,
                  x.primary_document_url,x.documents_json,x.evidence_excerpt,x.matched_event_type,
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
        "semantic_inconclusive": 0,
        "attachment_incomplete": 0,
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
        primary_evidence_url = str(
            row["primary_document_url"]
            or row["filing_index_url"]
            or row["canonical_url"]
            or ""
        )
        coverage_status = manifest_coverage(str(row["documents_json"] or ""))
        evidence_url = manifest_evidence_url(
            str(row["documents_json"] or ""),
            primary_evidence_url,
        )
        evidence_id = stable_id("EVID", str(row["event_id"]), str(row["observation_id"]))
        excerpt = str(row["evidence_excerpt"] or "").strip()
        filing_date = str(row["source_published_at"] or "")[:10] or None
        items_text = ";".join(str(code) for code in item_codes)
        keywords_text = ";".join(str(keyword) for keyword in keywords)
        passage_score = max(0, min(100, round(float(row["confidence"] or 0) * 100)))
        matched_type = str(row["matched_event_type"] or "")
        if coverage_status.startswith("INCOMPLETE"):
            evidence_status = "attachment_incomplete"
        elif excerpt and matched_type:
            evidence_status = "machine_extracted_unreviewed"
        elif excerpt:
            evidence_status = "machine_extracted_non_decision"
        else:
            evidence_status = "link_only_no_relevant_passage"
        existing = connection.execute(
            """SELECT evidence_url,filing_date,form,items,evidence_passage,
                      matched_keywords,passage_score,evidence_status,auto_verification_allowed
               FROM event_evidence WHERE event_id=? AND observation_id=?""",
            (row["event_id"], row["observation_id"]),
        ).fetchone()
        machine_statuses = {
            "machine_extracted_unreviewed",
            "machine_extracted_non_decision",
            "attachment_incomplete",
            "link_only_no_relevant_passage",
        }
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

        semantic_noise = (
            coverage_status == "COMPLETE"
            and matched_type in ADMINISTRATIVE_NON_EVENT_TYPES
        )
        if semantic_noise:
            reason = (
                f"sec_primary_semantic_non_event:{matched_type}"
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
        elif coverage_status.startswith("INCOMPLETE"):
            result["attachment_incomplete"] += 1
            reason = f"sec_evidence_incomplete:{coverage_status.casefold()}"
            cursor = connection.execute(
                """UPDATE pipeline_jobs
                   SET status='PENDING_PRIMARY_EVIDENCE',last_error=?,updated_at=?
                   WHERE event_id=? AND job_type='live_primary_evidence_review'
                     AND status IN ('PENDING_PRIMARY_EVIDENCE','PENDING_EVIDENCE_REVIEW')""",
                (reason, now, row["event_id"]),
            )
        else:
            result["semantic_inconclusive"] += int(not matched_type)
            reason = None if matched_type else "sec_semantic_inconclusive:no_scoped_event_match"
            cursor = connection.execute(
                """UPDATE pipeline_jobs
                   SET status='PENDING_EVIDENCE_REVIEW',last_error=?,updated_at=?
                   WHERE event_id=? AND job_type='live_primary_evidence_review'
                     AND status='PENDING_PRIMARY_EVIDENCE'""",
                (reason, now, row["event_id"]),
            )
        result["jobs_advanced"] += cursor.rowcount
    connection.commit()
    return result


def pending_rows(
    connection: Any,
    *,
    limit: int,
    refresh_parsed: bool = False,
    event_id: str | None = None,
) -> list[Any]:
    enrichment_filter = (
        "(x.observation_id IS NULL OR x.status='PARSED' OR (x.status='ERROR' AND x.attempts<3))"
        if refresh_parsed
        else """(
            x.observation_id IS NULL
            OR (x.status='ERROR' AND x.attempts<3)
            OR (
                x.status='PARSED' AND x.attempts<3
                AND json_extract(x.documents_json,'$.coverage_status') LIKE 'INCOMPLETE%'
            )
        )"""
    )
    event_filter = "AND e.event_id=?" if event_id else ""
    parameters: tuple[Any, ...] = (event_id, limit) if event_id else (limit,)
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
        LEFT JOIN (
            SELECT
              event_id,
              MAX(priority) AS priority,
              MIN(available_at) AS available_at,
              MAX(
                CASE WHEN last_error='reprocess_after_nonterminal_no_match_policy'
                     THEN 1 ELSE 0 END
              ) AS corrective_reprocess
            FROM pipeline_jobs
            WHERE job_type='live_primary_evidence_review'
              AND status='PENDING_PRIMARY_EVIDENCE'
            GROUP BY event_id
        ) pending_job ON pending_job.event_id=e.event_id
        WHERE e.status='candidate' AND r.source_id='sec_current_filings'
          AND {enrichment_filter}
          {event_filter}
        ORDER BY
          CASE
            WHEN pending_job.corrective_reprocess=1 THEN 0
            WHEN pending_job.event_id IS NOT NULL THEN 1
            ELSE 2
          END,
          pending_job.priority DESC,
          pending_job.available_at ASC,
          r.source_published_at DESC,
          r.observation_id
        LIMIT ?
        """,
        parameters,
    ).fetchall()


def pending_discovery_rows(connection: Any, *, limit: int) -> list[Any]:
    return connection.execute(
        """
        SELECT d.*,r.title,r.summary,r.canonical_url,r.raw_json,r.source_published_at,
               r.local_received_at,r.content_sha256,s.authority_tier,
               json_extract(r.raw_json,'$.item.form') AS form
        FROM discovery_leads d
        JOIN latest_source_content r ON r.observation_id=d.observation_id
        JOIN sources s ON s.source_id=d.source_id
        WHERE d.source_id='sec_current_filings'
          AND d.status IN ('PENDING_ENRICHMENT','NEEDS_EVIDENCE')
        ORDER BY CASE d.status WHEN 'NEEDS_EVIDENCE' THEN 0 ELSE 1 END,
                 d.updated_at,d.lead_id
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()


def _update_discovery_lead(
    connection: Any,
    row: Any,
    *,
    status: str,
    classification: Classification,
    evidence_url: str,
    evidence_passage: str,
    evidence_status: str,
    reasons: list[str],
    claim_action: str | None = None,
    claim_stage: str | None = None,
    claim_summary: str | None = None,
    canonical_event_id: str | None = None,
) -> None:
    connection.execute(
        """
        UPDATE discovery_leads
        SET status=?,proposed_event_family=COALESCE(?,proposed_event_family),
            proposed_event_type=COALESCE(?,proposed_event_type),claim_action=?,
            claim_stage=?,claim_summary=?,evidence_url=?,evidence_passage=?,
            evidence_status=?,matched_keywords_json=?,admission_reasons_json=?,
            admission_contract_version=?,canonical_event_id=?,updated_at=?
        WHERE lead_id=?
        """,
        (
            status,
            classification.event_family,
            classification.event_type,
            claim_action,
            claim_stage,
            claim_summary,
            evidence_url,
            evidence_passage,
            evidence_status,
            stable_json(list(classification.keywords)),
            stable_json(reasons),
            ADMISSION_CONTRACT_VERSION,
            canonical_event_id,
            utc_now(),
            row["lead_id"],
        ),
    )


def _promote_discovery_lead(
    connection: Any,
    row: Any,
    *,
    classification: Classification,
    evidence_url: str,
    evidence_passage: str,
    evidence_status: str,
    evidence_content_sha256: str,
) -> tuple[bool, list[str]]:
    subject = str(row["company_name"] or row["ticker_at_event"] or "").strip()
    action = str(classification.event_type or "")
    stage = EVENT_STAGE_BY_TYPE.get(action, "DISCLOSED")
    action_label = EVENT_ACTION_LABELS.get(action, action.replace("_", " "))
    fact_extraction = extract_evidence_fact_slots(
        evidence_passage=evidence_passage,
        event_type=action,
        expected_subject=subject,
    )
    summary = public_fact_summary(
        subject=subject,
        action_label=action_label,
        stage_label=stage,
        extraction=fact_extraction,
    )
    matched_rule = EventRule(
        str(classification.event_family or "other"),
        action,
        re.compile(r"$^"),
    )
    event_row = {
        "title": row["title"],
        "summary": row["summary"],
        "source_published_at": row["source_published_at"],
        "local_received_at": row["local_received_at"],
        "canonical_url": row["canonical_url"],
        "authority_tier": row["authority_tier"],
    }
    event_id = live_event_id(event_row, matched_rule)
    evidence_id = stable_id("EVID", event_id, str(row["observation_id"]))
    subject_context = " ".join(
        str(row[key] or "") for key in ("title", "summary", "raw_json")
    ).casefold()
    subject_match = bool(subject) and subject.casefold() in subject_context
    decision = evaluate_event_admission(
        event_id=event_id,
        event_version=1,
        evidence_id=evidence_id,
        subject=subject,
        action=action,
        stage=stage,
        known_at=str(row["known_at"]),
        source_authority_tier=str(row["authority_tier"]),
        evidence_url=evidence_url,
        evidence_passage=evidence_passage,
        evidence_status=evidence_status,
        content_sha256=evidence_content_sha256,
        # SEC feed metadata binds the filing to its issuer; this must still be
        # observable in the frozen title/raw item instead of inferred merely
        # from a non-empty company field.
        subject_match=subject_match,
        event_claim_supported=bool(classification.event_type and classification.keywords)
        # SEC classification is discovery routing, never event truth.  Every
        # event type, including types whose extractor is not implemented yet,
        # stays NEEDS_EVIDENCE until the exact passage yields an issuer-bound
        # fact.  This deliberately trades recall for a truthful public reader.
        and requires_specific_fact_extraction(action)
        and fact_extraction.supports_specific_fact,
        date_coherent=bool(row["event_date"]),
        fact_extraction=fact_extraction,
        public_fact_summary_text=summary,
    )
    if not decision.admitted:
        _update_discovery_lead(
            connection,
            row,
            status="NEEDS_EVIDENCE",
            classification=classification,
            evidence_url=evidence_url,
            evidence_passage=evidence_passage,
            evidence_status=evidence_status,
            reasons=list(decision.reasons),
            claim_action=action,
            claim_stage=stage,
            claim_summary=summary,
        )
        return False, list(decision.reasons)

    existing = connection.execute(
        "SELECT event_id FROM canonical_events WHERE event_id=?", (event_id,)
    ).fetchone()
    if existing is not None:
        _update_discovery_lead(
            connection,
            row,
            status="DUPLICATE",
            classification=classification,
            evidence_url=evidence_url,
            evidence_passage=evidence_passage,
            evidence_status=evidence_status,
            reasons=["CANONICAL_EVENT_ALREADY_EXISTS"],
            claim_action=action,
            claim_stage=stage,
            claim_summary=summary,
            canonical_event_id=event_id,
        )
        return False, ["CANONICAL_EVENT_ALREADY_EXISTS"]

    now = utc_now()
    grade_cap = provisional_grade_cap(str(row["authority_tier"]))
    facts = {
        "candidate_only": True,
        "public_fact_summary": summary,
        "claim_subject": subject,
        "claim_action": action,
        "claim_stage": stage,
        "claim_fact_slots": fact_extraction.as_dict(),
        "fact_slot_contract_version": FACT_SLOT_CONTRACT_VERSION,
        "fact_slot_receipt_sha256": decision.fact_slot_receipt_sha256,
        "known_at": row["known_at"],
        "source_observation_id": row["observation_id"],
        "source_content_sha256": evidence_content_sha256,
        "evidence_id": evidence_id,
        "evidence_fingerprint": decision.evidence_fingerprint,
        "admission_contract_version": ADMISSION_CONTRACT_VERSION,
        "formal_verification": False,
        "auto_verification_allowed": False,
        "no_trading": True,
    }
    connection.execute(
        """
        INSERT INTO canonical_events(
            event_id,current_version,status,label_status,event_family,event_type,event_date,
            first_seen_at,last_updated_at,stable_id,ticker_at_event,company_name,manual_grade,
            provisional_grade_cap,discovery_source,no_trading
        ) VALUES (?,1,'candidate','candidate',?,?,?,?,?,NULL,?,?,NULL,?,?,1)
        """,
        (
            event_id,
            classification.event_family,
            classification.event_type,
            row["event_date"],
            row["local_received_at"],
            now,
            row["ticker_at_event"],
            row["company_name"],
            grade_cap,
            row["source_id"],
        ),
    )
    connection.execute(
        """INSERT INTO event_versions(
               event_id,version,changed_at,status,label_status,event_family,event_type,
               manual_grade,facts_json,change_reason
           ) VALUES (?,1,?,'candidate','candidate',?,?,NULL,?,?)""",
        (
            event_id,
            now,
            classification.event_family,
            classification.event_type,
            stable_json(facts),
            "discovery_admission_scoped_primary_evidence",
        ),
    )
    connection.execute(
        "INSERT INTO event_observations VALUES (?,?,?,?)",
        (event_id, row["observation_id"], "scoped_primary_evidence_candidate", now),
    )
    connection.execute(
        """INSERT INTO event_evidence(
               evidence_id,event_id,observation_id,evidence_url,filing_date,form,items,
               evidence_passage,matched_keywords,passage_score,evidence_status,
               auto_verification_allowed,created_at,updated_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?)""",
        (
            evidence_id,
            event_id,
            row["observation_id"],
            evidence_url,
            str(row["source_published_at"] or "")[:10] or None,
            row["form"],
            "",
            evidence_passage,
            ";".join(classification.keywords),
            max(0, min(100, round(classification.confidence * 100))),
            evidence_status,
            now,
            now,
        ),
    )
    connection.execute(
        """INSERT INTO event_evidence_relations(
               event_id,evidence_id,event_version,relation_status,subject_match,
               event_claim_supported,date_coherent,modality,evidence_fingerprint,
               contract_version,assessed_by,created_at
           ) VALUES (?,?,1,'SCOPED_MATCH',1,1,1,?,?,?,?,?)""",
        (
            event_id,
            evidence_id,
            stage,
            decision.evidence_fingerprint,
            ADMISSION_CONTRACT_VERSION,
            "deterministic_sec_passage_fact_classifier",
            now,
        ),
    )
    connection.execute(
        """INSERT INTO event_fact_workflow(
               event_id,event_version,workflow_state,reason_codes_json,evidence_fingerprint,
               contract_version,updated_at
           ) VALUES (?,1,'EVIDENCE_READY','[]',?,?,?)""",
        (event_id, decision.evidence_fingerprint, ADMISSION_CONTRACT_VERSION, now),
    )
    connection.execute(
        """INSERT INTO pipeline_jobs(
               job_id,event_id,job_type,status,priority,attempts,available_at,last_error,
               payload_json,created_at,updated_at
           ) VALUES (?,?,'live_primary_evidence_review','PENDING_EVIDENCE_REVIEW',50,0,
                     ?,NULL,?,?,?)""",
        (
            stable_id("JOB", event_id, "live_primary_evidence_review"),
            event_id,
            now,
            stable_json(
                {
                    "admission_contract_version": ADMISSION_CONTRACT_VERSION,
                    "fact_slot_contract_version": FACT_SLOT_CONTRACT_VERSION,
                }
            ),
            now,
            now,
        ),
    )
    _update_discovery_lead(
        connection,
        row,
        status="PROMOTED",
        classification=classification,
        evidence_url=evidence_url,
        evidence_passage=evidence_passage,
        evidence_status=evidence_status,
        reasons=[],
        claim_action=action,
        claim_stage=stage,
        claim_summary=summary,
        canonical_event_id=event_id,
    )
    return True, []


def enrich_pending_discovery_leads(
    connection: Any,
    client: SecFilingClient,
    *,
    limit: int,
) -> dict[str, Any]:
    rows = pending_discovery_rows(connection, limit=limit)
    result = {
        "requested": len(rows),
        "promoted": 0,
        "duplicate": 0,
        "no_scoped_event": 0,
        "needs_evidence": 0,
        "excluded": 0,
        "errors": 0,
    }
    for row in rows:
        documents: list[FilingDocument] = []
        selected: list[FilingDocument] = []
        fetched_texts: dict[str, str] = {}
        try:
            documents = parse_filing_index(client.get(row["canonical_url"]))
            selected = choose_documents(documents, str(row["form"] or ""))
            if not selected:
                _update_discovery_lead(
                    connection,
                    row,
                    status="NEEDS_EVIDENCE",
                    classification=Classification(None, None, (), 0.0),
                    evidence_url=str(row["canonical_url"] or ""),
                    evidence_passage="",
                    evidence_status="link_only_no_relevant_passage",
                    reasons=["SEC_DOCUMENT_NOT_AVAILABLE"],
                )
                result["needs_evidence"] += 1
                continue
            fetched_documents: list[tuple[FilingDocument, str]] = []
            for document in selected:
                text = visible_text(client.get(document.url))
                fetched_texts[document.url] = text
                fetched_documents.append((document, text))
            combined_text = "\n".join(
                f"[DOCUMENT {document.document_type} {document.description}]\n{text}"
                for document, text in fetched_documents
                if text
            )
            classification = classify_filing_text(combined_text)
            excerpt_source = select_excerpt_source(fetched_documents, classification)
            excerpt = evidence_excerpt(excerpt_source, classification.keywords)
            manifest_json = stable_json(
                document_manifest(
                    documents,
                    selected,
                    fetched_texts,
                    item_codes=item_codes_from_raw(str(row["raw_json"] or "")),
                    error=None,
                )
            )
            coverage = manifest_coverage(manifest_json)
            evidence_url = manifest_evidence_url(
                manifest_json,
                selected[0].url,
            )
            if coverage.startswith("INCOMPLETE"):
                _update_discovery_lead(
                    connection,
                    row,
                    status="NEEDS_EVIDENCE",
                    classification=classification,
                    evidence_url=evidence_url,
                    evidence_passage=excerpt,
                    evidence_status="attachment_incomplete",
                    reasons=[f"SEC_EVIDENCE_{coverage}"],
                )
                result["needs_evidence"] += 1
            elif classification.event_type in ADMINISTRATIVE_NON_EVENT_TYPES:
                _update_discovery_lead(
                    connection,
                    row,
                    status="EXCLUDED",
                    classification=classification,
                    evidence_url=evidence_url,
                    evidence_passage=excerpt,
                    evidence_status="machine_extracted_non_decision",
                    reasons=["SEC_ADMINISTRATIVE_NON_EVENT"],
                )
                result["excluded"] += 1
            elif not classification.event_type:
                _update_discovery_lead(
                    connection,
                    row,
                    status="LEAD_NO_SCOPED_EVENT",
                    classification=classification,
                    evidence_url=evidence_url,
                    evidence_passage=excerpt,
                    evidence_status="machine_extracted_non_decision",
                    reasons=["NO_SCOPED_EVENT_MATCH"],
                )
                result["no_scoped_event"] += 1
            else:
                promoted, _ = _promote_discovery_lead(
                    connection,
                    row,
                    classification=classification,
                    evidence_url=evidence_url,
                    evidence_passage=excerpt,
                    evidence_status="machine_extracted_unreviewed",
                    evidence_content_sha256=hashlib.sha256(combined_text.encode("utf-8")).hexdigest(),
                )
                if promoted:
                    result["promoted"] += 1
                else:
                    updated_status = connection.execute(
                        "SELECT status FROM discovery_leads WHERE lead_id=?",
                        (row["lead_id"],),
                    ).fetchone()[0]
                    result["duplicate" if updated_status == "DUPLICATE" else "needs_evidence"] += 1
        except (RuntimeError, urllib.error.URLError, ValueError) as exc:
            _update_discovery_lead(
                connection,
                row,
                status="NEEDS_EVIDENCE",
                classification=Classification(None, None, (), 0.0),
                evidence_url=str(row["canonical_url"] or ""),
                evidence_passage="",
                evidence_status="link_only_no_relevant_passage",
                reasons=[f"SEC_ENRICHMENT_ERROR:{type(exc).__name__}"],
            )
            result["errors"] += 1
        finally:
            connection.commit()
    return result


def enrich_pending(
    connection: Any,
    client: SecFilingClient,
    *,
    limit: int = 8,
    refresh_parsed: bool = False,
    event_id: str | None = None,
) -> dict[str, Any]:
    discovery_result = enrich_pending_discovery_leads(
        connection,
        client,
        limit=max(1, limit),
    )
    remaining_limit = max(0, int(limit) - int(discovery_result["requested"]))
    rows = pending_rows(
        connection,
        limit=remaining_limit,
        refresh_parsed=refresh_parsed,
        event_id=event_id,
    )
    result: dict[str, Any] = {
        "discovery_admission": discovery_result,
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
        fetched_texts: dict[str, str] = {}
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
                    selected_documents=selected,
                    fetched_texts=fetched_texts,
                    primary_url=None,
                    text="",
                    excerpt_source_text="",
                    classification=Classification(None, None, (), 0.0),
                    status="NO_DOCUMENT",
                    attempts=attempts,
                    error=None,
                )
                result["no_document"] += 1
                continue
            fetched_documents: list[tuple[FilingDocument, str]] = []
            for document in selected:
                text = visible_text(client.get(document.url))
                fetched_texts[document.url] = text
                fetched_documents.append((document, text))
            combined_text = "\n".join(
                f"[DOCUMENT {document.document_type} {document.description}]\n{text}"
                for document, text in fetched_documents
                if text
            )
            classification = classify_filing_text(combined_text)
            excerpt_source = select_excerpt_source(fetched_documents, classification)
            upsert_enrichment(
                connection,
                row=row,
                accession=accession,
                documents=documents,
                selected_documents=selected,
                fetched_texts=fetched_texts,
                primary_url=selected[0].url,
                text=combined_text,
                excerpt_source_text=excerpt_source,
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
                selected_documents=selected,
                fetched_texts=fetched_texts,
                primary_url=selected[0].url if selected else None,
                text=combined_text,
                excerpt_source_text=None,
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
        f"- Legacy inconclusive terminal rejections reopened: `{result.get('inconclusive_events_reopened', 0)}`",
        f"- Negated old machine matches repaired: `{result.get('negated_match_repairs', 0)}`",
        f"- Stored semantic classifications repaired: `{result.get('semantic_reclassifications', 0)}`",
        f"- Errors: `{result['errors']}`",
        f"- Parsed SEC rows materialized as evidence: `{result.get('evidence_materialization', {}).get('evidence_inserted', 0)}`",
        f"- Evidence jobs advanced: `{result.get('evidence_materialization', {}).get('jobs_advanced', 0)}`",
        f"- Parsed generic/administrative filings filtered: `{result.get('evidence_materialization', {}).get('semantic_noise_rejected', 0)}`",
        f"- Parsed filings kept nonterminal because no scoped match was found: `{result.get('evidence_materialization', {}).get('semantic_inconclusive', 0)}`",
        f"- Parsed filings awaiting complete attachments: `{result.get('evidence_materialization', {}).get('attachment_incomplete', 0)}`",
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
        "--event-id",
        help="Process one exact SEC candidate for audited repair or operator verification.",
    )
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
        reopened = reopen_inconclusive_sec_events(connection)
        result = enrich_pending(
            connection,
            client,
            limit=args.limit,
            refresh_parsed=args.refresh_parsed,
            event_id=args.event_id,
        )
        result["inconclusive_events_reopened"] = reopened
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
