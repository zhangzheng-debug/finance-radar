from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

from active_sec_evidence import load_simple_env


KEYWORDS_BY_EVENT_TYPE: dict[str, tuple[str, ...]] = {
    "bankruptcy_liquidation": (
        "chapter 7",
        "chapter 11",
        "bankruptcy",
        "receivership",
        "voluntary petition",
        "debtor-in-possession",
        "reorganization",
        "no distribution",
        "no consideration",
        "cancelled",
        "canceled",
        "recovery",
        "trustee",
        "liquidation",
        "liquidate",
        "judicial management",
        "judicial manager",
        "interim judicial manager",
        "insolvency",
        "moratorium",
        "judicial recovery",
        "court-supervised insolvency protection",
        "events of default",
        "acceleration notice",
        "immediately due and payable",
        "limited forbearance",
        "forbearance period",
        "private disposition of collateral",
        "ucc sale notice",
        "article 9 of the uniform commercial code",
        "sell all of the collateral",
        "acquire all of the collateral",
        "final cash liquidating distribution",
        "cash liquidating distribution",
        "aggregate cash liquidating distributions",
        "plan of sale and dissolution",
        "complete liquidation and dissolution",
        "liquidating trust",
        "beneficial interest units",
        "converted into beneficial interests",
        "no debt outstanding",
        "delisting determination",
        "qualification halt",
        "suspend trading",
        "minimum bid price",
        "otcqb",
        "securities would be suspended",
        "closing bid price",
    ),
    "delisted": (
        "delist",
        "determined to delist",
        "delisting determination",
        "minimum bid price",
        "continued non-compliance",
        "expected to be suspended",
        "chapter 11",
        "bankruptcy",
        "voluntary petition",
        "bankruptcy court",
        "suspend trading",
        "withdraw from listing",
        "form 25",
        "merger consideration",
        "converted into the right",
        "listing rule",
        "units ceased trading",
        "began trading on nasdaq",
        "new trading symbol",
        "reclassified into",
        "low trading volume",
        "limited public shareholder base",
        "costs and expenses associated",
        "reporting requirements",
        "reporting obligations",
        "regulatory burdens",
        "dual listing",
        "concentrate trading",
        "substantial majority of trading volume",
        "cost and regulatory",
        "going dark transaction",
        "cash-out price",
        "administrative burden",
        "lack of an active trading market",
        "likely future non-compliance",
        "quoted on the otcqx",
        "trade over-the-counter",
        "begin to trade over-the-counter",
        "reduce our expenses",
        "costs of maintaining",
        "demands on management",
        "simplify corporate structure",
        "operational efficiency",
        "sole primary listing",
        "consolidate trading liquidity",
        "transitional period",
        "streamline regulatory reporting",
        "no assurance that trading",
        "reduced free float",
        "concentrating its float",
        "natural market",
        "six swiss exchange",
        "reduce its cost base",
        "controls more than 90 percent",
        "offers to purchase all of the outstanding",
        "compulsory redemption",
        "compulsory buy-out",
        "special purpose acquisition company",
        "initial business combination",
        "within 36 months",
        "subject to delisting",
        "will not appeal",
        "commence trading on the over-the-counter",
        "transition to otc",
        "remains fully reporting",
        "remain a fully reporting",
        "very limited trading volume",
        "continue to be listed and traded",
        "principal trading market",
        "maintain its ads program",
        "considerable costs associated with maintaining the listing",
        "below criteria average closing price",
    ),
    "voluntarydelisting": (
        "delist",
        "chapter 11",
        "bankruptcy",
        "voluntary petition",
        "bankruptcy court",
        "withdraw from listing",
        "form 25",
        "going private",
        "merger consideration",
        "units ceased trading",
        "began trading on nasdaq",
        "new trading symbol",
        "reclassified into",
        "low trading volume",
        "limited public shareholder base",
        "costs and expenses associated",
        "reporting requirements",
        "reporting obligations",
        "regulatory burdens",
        "dual listing",
        "concentrate trading",
        "substantial majority of trading volume",
        "cost and regulatory",
        "going dark transaction",
        "cash-out price",
        "administrative burden",
        "lack of an active trading market",
        "likely future non-compliance",
        "quoted on the otcqx",
        "trade over-the-counter",
        "begin to trade over-the-counter",
        "reduce our expenses",
        "costs of maintaining",
        "demands on management",
        "simplify corporate structure",
        "operational efficiency",
        "sole primary listing",
        "consolidate trading liquidity",
        "transitional period",
        "streamline regulatory reporting",
        "no assurance that trading",
        "reduced free float",
        "concentrating its float",
        "natural market",
        "six swiss exchange",
        "reduce its cost base",
        "controls more than 90 percent",
        "offers to purchase all of the outstanding",
        "compulsory redemption",
        "compulsory buy-out",
        "special purpose acquisition company",
        "initial business combination",
        "within 36 months",
        "subject to delisting",
        "will not appeal",
        "commence trading on the over-the-counter",
        "very limited trading volume",
        "continue to be listed and traded",
        "principal trading market",
        "maintain its ads program",
        "considerable costs associated with maintaining the listing",
        "below criteria average closing price",
    ),
    "reverse_split": (
        "reverse stock split",
        "reverse split",
        "share consolidation",
        "amendment to the articles",
        "minimum bid price",
        "regain compliance",
        "maintaining its listing",
        "authorized shares",
        "issued and outstanding",
        "registered direct offering",
        "at-the-market offering",
        "underwritten public offering",
        "best efforts public offering",
        "public offering",
        "sales agreement",
        "aggregate offering price",
        "pre-funded warrants",
        "public reprimand letter",
        "in excess of 19.99%",
        "restructuring plan",
        "court agreed to sanction",
        "new ordinary shares",
        "issue and allot",
        "bondholders",
        "conversion of its loan facility",
        "ratio of shares to adss",
        "agreed to sell",
        "securities purchase agreement",
        "rescinded the issuance",
        "non-payment of the proceeds",
        "alternate cashless basis",
        "cashless warrants",
        "condition to the closing",
        "converted notes",
        "notes converted",
        "change in control",
        "merger agreement",
        "name change",
        "relative interest",
        "proportionate reduction",
    ),
    "negative_equity": (
        "stockholders' deficit",
        "shareholders' deficit",
        "stockholders' equity",
        "shareholders' equity",
        "total stockholders",
        "total shareholders",
        "negative equity",
        "total deficit",
        "accumulated deficit",
        "treasury stock",
        "share repurchase",
        "net income",
        "net cash provided by operating activities",
        "net cash used in operating activities",
        "troubled debt restructuring",
        "long-term debt",
        "liquidity",
    ),
    "cash_short_debt_stress": (
        "liquidity",
        "going concern",
        "substantial doubt",
        "cash and cash equivalents",
        "cash balance",
        "available liquidity",
        "borrowing capacity",
        "revolving credit facility",
        "working capital",
        "debt obligations",
    ),
    "revenue_collapse_yoy": ("revenue", "total revenues", "net sales", "decreased", "decline", "compared to"),
    "free_cash_flow_turn_negative": ("cash flows", "operating activities", "capital expenditures"),
    "gross_margin_collapse": ("gross margin", "gross profit", "cost of revenue", "cost of sales", "decreased", "decline", "compared to"),
    "interest_coverage_below_1": (
        "interest expense",
        "operating loss",
        "default",
        "covenant",
        "defer measurement",
        "in compliance with all financial covenants",
        "was in compliance with all financial covenants",
        "in compliance with this covenant",
        "available to borrow",
        "available borrowing capacity",
        "net cash provided by operating activities",
        "net cash used in operating activities",
        "sufficient to fund our operating and capital needs",
        "will be sufficient to fund our operating and capital needs",
        "cash and cash equivalents",
        "short-term investments",
    ),
    "volume_crash": (
        "material event", "default", "investigation", "bankruptcy", "delist",
        "warrant exchange", "exchange shares", "offering", "reverse stock split",
        "listing rule", "full repayment", "matter regarding", "regained compliance",
        "going concern", "substantial doubt", "cash and cash equivalents",
        "suspended", "stock has been suspended", "specially designated nationals", "office of foreign assets control",
    ),
    "one_day_crash": (
        "material event", "default", "investigation", "bankruptcy", "delist",
        "warrant exchange", "exchange shares", "offering", "reverse stock split",
        "listing rule", "full repayment", "matter regarding", "regained compliance",
        "going concern", "substantial doubt", "cash and cash equivalents",
        "suspended", "stock has been suspended", "specially designated nationals", "office of foreign assets control",
    ),
    "five_day_crash": (
        "material event", "default", "investigation", "bankruptcy", "delist",
        "warrant exchange", "exchange shares", "offering", "reverse stock split",
        "listing rule", "full repayment", "matter regarding", "regained compliance",
        "going concern", "substantial doubt", "cash and cash equivalents",
        "suspended", "stock has been suspended", "specially designated nationals", "office of foreign assets control",
    ),
    "twenty_one_day_crash": (
        "material event", "default", "investigation", "bankruptcy", "delist",
        "warrant exchange", "exchange shares", "offering", "reverse stock split",
        "listing rule", "full repayment", "matter regarding", "regained compliance",
        "going concern", "substantial doubt", "cash and cash equivalents",
        "suspended", "stock has been suspended", "specially designated nationals", "office of foreign assets control",
    ),
}

STRONG_PHRASE_WEIGHTS = {
    "chapter 7": 10,
    "bankruptcy trustee": 10,
    "ceased operations": 10,
    "canceled for no consideration": 20,
    "cancelled for no consideration": 20,
    "no consideration": 8,
    "no distribution": 8,
    "will be canceled": 8,
    "will be cancelled": 8,
    "were canceled": 8,
    "were cancelled": 8,
    "chapter 11": 5,
    "voluntary petition": 5,
    "item 1.03": 4,
    "item 3.01": 4,
    "merger consideration": 5,
    "converted into the right": 5,
    "reverse stock split": 5,
    "minimum bid price": 5,
    "regain compliance": 7,
    "very limited trading volume": 8,
    "continue to be listed and traded": 10,
    "principal trading market": 8,
    "maintain its ads program": 8,
    "considerable costs associated with maintaining the listing": 8,
    "below criteria average closing price": 6,
    "defer measurement": 18,
    "in compliance with all financial covenants": 9,
    "was in compliance with all financial covenants": 12,
    "in compliance with this covenant": 8,
    "available to borrow": 6,
    "available borrowing capacity": 7,
    "sufficient to fund our operating and capital needs": 18,
    "will be sufficient to fund our operating and capital needs": 18,
    "maintaining its listing": 6,
    "authorized shares": 4,
    "issued and outstanding": 3,
    "registered direct offering": 8,
    "at-the-market offering": 12,
    "underwritten public offering": 8,
    "best efforts public offering": 8,
    "public offering": 5,
    "sales agreement": 7,
    "aggregate offering price": 8,
    "pre-funded warrants": 12,
    "public reprimand letter": 10,
    "in excess of 19.99%": 10,
    "restructuring plan": 12,
    "court agreed to sanction": 14,
    "new ordinary shares": 9,
    "issue and allot": 10,
    "bondholders": 8,
    "conversion of its loan facility": 10,
    "ratio of shares to adss": 8,
    "troubled debt restructuring": 12,
    "stockholders' deficit": 10,
    "shareholders' deficit": 10,
    "negative equity": 10,
    "total deficit": 8,
    "net cash used in operating activities": 8,
    "net cash provided by operating activities": 6,
    "share repurchase": 7,
    "treasury stock": 6,
    "accumulated deficit": 5,
    "agreed to sell": 6,
    "securities purchase agreement": 6,
    "rescinded the issuance": 14,
    "non-payment of the proceeds": 14,
    "alternate cashless basis": 9,
    "cashless warrants": 12,
    "condition to the closing": 8,
    "converted notes": 9,
    "notes converted": 9,
    "change in control": 10,
    "merger agreement": 7,
    "name change": 5,
    "relative interest": 5,
    "proportionate reduction": 6,
    "going concern": 4,
    "substantial doubt": 4,
    "cash and cash equivalents": 5,
    "office of foreign assets control": 12,
    "specially designated nationals": 12,
    "suspended": 8,
    "stock has been suspended": 14,
    "warrant exchange": 8,
    "exchange shares": 8,
    "full repayment": 10,
    "matter regarding": 6,
    "regained compliance": 10,
    "units ceased trading": 10,
    "began trading on nasdaq": 8,
    "new trading symbol": 8,
    "reclassified into": 6,
    "low trading volume": 6,
    "limited public shareholder base": 6,
    "costs and expenses associated": 5,
    "reporting requirements": 4,
    "reporting obligations": 4,
    "regulatory burdens": 5,
    "dual listing": 5,
    "concentrate trading": 5,
    "substantial majority of trading volume": 7,
    "cost and regulatory": 6,
    "going dark transaction": 8,
    "cash-out price": 8,
    "administrative burden": 5,
    "lack of an active trading market": 7,
    "likely future non-compliance": 8,
    "quoted on the otcqx": 6,
    "trade over-the-counter": 6,
    "begin to trade over-the-counter": 6,
    "reduce our expenses": 6,
    "costs of maintaining": 6,
    "demands on management": 5,
    "simplify corporate structure": 6,
    "operational efficiency": 5,
    "sole primary listing": 7,
    "consolidate trading liquidity": 7,
    "transitional period": 5,
    "streamline regulatory reporting": 6,
    "no assurance that trading": 7,
    "reduced free float": 7,
    "concentrating its float": 9,
    "natural market": 6,
    "six swiss exchange": 8,
    "reduce its cost base": 7,
    "controls more than 90 percent": 12,
    "offers to purchase all of the outstanding": 12,
    "compulsory redemption": 12,
    "compulsory buy-out": 12,
    "special purpose acquisition company": 8,
    "initial business combination": 10,
    "within 36 months": 7,
    "subject to delisting": 8,
    "will not appeal": 8,
    "commence trading on the over-the-counter": 7,
    "determined to delist": 12,
    "continued non-compliance": 8,
    "expected to be suspended": 9,
    "judicial management": 10,
    "judicial manager": 8,
    "interim judicial manager": 10,
    "insolvency": 7,
    "moratorium": 6,
    "judicial recovery": 9,
    "court-supervised insolvency protection": 10,
    "events of default": 8,
    "acceleration notice": 10,
    "immediately due and payable": 9,
    "limited forbearance": 7,
    "forbearance period": 6,
    "private disposition of collateral": 12,
    "ucc sale notice": 10,
    "article 9 of the uniform commercial code": 10,
    "sell all of the collateral": 12,
    "acquire all of the collateral": 12,
    "final cash liquidating distribution": 14,
    "cash liquidating distribution": 12,
    "aggregate cash liquidating distributions": 12,
    "plan of sale and dissolution": 12,
    "complete liquidation and dissolution": 12,
    "liquidating trust": 10,
    "beneficial interest units": 10,
    "converted into beneficial interests": 10,
    "no debt outstanding": 6,
    "delisting determination": 8,
    "qualification halt": 7,
    "suspend trading": 6,
    "minimum bid price": 5,
    "otcqb": 5,
    "securities would be suspended": 7,
    "closing bid price": 5,
}


@dataclass(frozen=True)
class Passage:
    text: str
    matched_keywords: tuple[str, ...]
    score: int


@dataclass(frozen=True)
class ExtractionRun:
    passages_path: Path
    report_path: Path
    manifest_path: Path
    rows: int
    errors: tuple[str, ...]


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._hidden_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._hidden_depth += 1
        elif tag.lower() in {"p", "div", "br", "tr", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._hidden_depth:
            self._hidden_depth -= 1
        elif tag.lower() in {"p", "div", "tr", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth:
            self.parts.append(data)


def visible_text(payload: bytes) -> str:
    decoded = payload.decode("utf-8", errors="replace")
    parser = VisibleTextParser()
    parser.feed(decoded)
    text = html.unescape(" ".join(parser.parts))
    text = re.sub(r"[\t\r\f\v]+", " ", text)
    text = re.sub(r" *\n+ *", "\n", text)
    text = re.sub(r"[ ]{2,}", " ", text)
    return text.strip()


def exhibit_99_urls(index_payload: bytes) -> list[str]:
    """Extract EX-99 document links from an SEC filing index page."""
    decoded = index_payload.decode("utf-8", errors="replace")
    output: list[str] = []
    for row_html in re.findall(r"<tr\b[^>]*>.*?</tr>", decoded, flags=re.I | re.S):
        row_text = re.sub(r"<[^>]+>", " ", row_html)
        if not re.search(r"\bEX-99(?:\.|\b)", html.unescape(row_text), flags=re.I):
            continue
        match = re.search(r"href=[\"']([^\"']+)[\"']", row_html, flags=re.I)
        if not match:
            continue
        url = urllib.parse.urljoin("https://www.sec.gov", html.unescape(match.group(1)))
        if url not in output:
            output.append(url)
    return output[:3]


def sentence_chunks(text: str) -> list[str]:
    chunks = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [re.sub(r"\s+", " ", chunk).strip() for chunk in chunks if len(chunk.strip()) >= 20]


def evidence_match_text(text: str) -> str:
    """Remove finance phrases that resemble insolvency or equity-death language."""
    cleaned = text.replace("’", "'").replace("‘", "'")
    for pattern in (
        r"\bliquidated damages?\b",
        r"\bliquidation preferences?\b",
        r"\bliquidation value\b",
        r"\bcancell?ed debt\b",
        r"\bdebt cancell?ed\b",
        r"\bno change in control\b",
        r"\bdoes not (?:constitute|result in) (?:a )?change in control\b",
        r"\bincluding (?:a )?change in control\b",
        r"\bcould result in (?:a )?change in control\b",
        r"\b(?:may|might|could) have the effect of (?:delaying, preventing or deterring )?(?:a )?change in control\b",
    ):
        cleaned = re.sub(pattern, "", cleaned, flags=re.I)
    return cleaned


def passage_for_event(text: str, event_type: str, max_chars: int) -> Passage:
    keywords = KEYWORDS_BY_EVENT_TYPE.get(event_type, ("material event",))
    chunks = sentence_chunks(text)
    scored: list[tuple[int, int, tuple[str, ...]]] = []
    for index, chunk in enumerate(chunks):
        lowered = evidence_match_text(chunk.lower())
        matched = tuple(keyword for keyword in keywords if keyword in lowered)
        score = len(matched) * 2
        score += sum(weight for phrase, weight in STRONG_PHRASE_WEIGHTS.items() if phrase in lowered)
        numeric_density = min(5, len(re.findall(r"(?:[$€£]\s*)?\(?\d[\d,.]*(?:\.\d+)?%?\)?", chunk)))
        if event_type == "negative_equity":
            equity_statement = any(
                phrase in lowered
                for phrase in (
                    "stockholders' deficit",
                    "shareholders' deficit",
                    "stockholders' equity",
                    "shareholders' equity",
                    "total stockholders",
                    "total shareholders",
                )
            )
            if equity_statement:
                score += 16 + numeric_density
            if "share repurchase authorization" in lowered and not equity_statement:
                score -= 10
        elif event_type == "cash_short_debt_stress":
            has_cash = any(phrase in lowered for phrase in ("cash and cash equivalents", "cash balance", "available liquidity"))
            has_debt_capacity = any(
                phrase in lowered
                for phrase in (
                    "debt obligations",
                    "borrowing capacity",
                    "revolving credit facility",
                    "working capital",
                    "current maturities",
                )
            )
            if has_cash:
                score += 8 + numeric_density
            if has_cash and has_debt_capacity:
                score += 12
        elif event_type == "revenue_collapse_yoy":
            has_revenue = "revenue" in lowered or "net sales" in lowered
            has_comparison = any(
                phrase in lowered
                for phrase in ("compared to", "decreased", "declined", "increase", "three months ended", "year ended")
            )
            if has_revenue and has_comparison:
                score += 14 + numeric_density
        elif event_type == "gross_margin_collapse":
            has_gross = "gross margin" in lowered or "gross profit" in lowered
            has_comparison = any(
                phrase in lowered
                for phrase in ("compared to", "decreased", "declined", "increase", "three months ended", "year ended")
            )
            if has_gross and has_comparison:
                score += 16 + numeric_density
        if event_type in {"delisted", "voluntarydelisting"}:
            # Option-treatment paragraphs frequently repeat merger boilerplate but
            # do not prove what old common holders received. Prefer the sentence
            # that states per-share consideration and common-share conversion.
            if any(phrase in lowered for phrase in ("company option", "option consideration")):
                score -= 8
            if "each share" in lowered and "merger consideration" in lowered:
                score += 12
        if score:
            scored.append((score, index, matched))
    if not scored:
        return Passage("", (), 0)
    score, index, matched = max(scored, key=lambda item: (item[0], -item[1]))
    context = chunks[max(0, index - 2) : min(len(chunks), index + 3)]
    passage = " ".join(context)
    if len(passage) > max_chars:
        anchors = sorted(
            set(matched),
            key=lambda phrase: (STRONG_PHRASE_WEIGHTS.get(phrase, 0), len(phrase)),
            reverse=True,
        )
        lowered = passage.lower()
        positions = [(lowered.find(anchor), anchor) for anchor in anchors if anchor in lowered]
        anchor_position = max(
            positions,
            key=lambda item: (STRONG_PHRASE_WEIGHTS.get(item[1], 0), -item[0]),
        )[0] if positions else 0
        start = max(0, anchor_position - max_chars // 3)
        end = min(len(passage), start + max_chars - 1)
        start = max(0, end - (max_chars - 1))
        prefix = "…" if start else ""
        suffix = "…" if end < len(passage) else ""
        passage = prefix + passage[start:end].strip() + suffix
    matchable_passage = evidence_match_text(passage.lower())
    all_matches = tuple(sorted({keyword for keyword in keywords if keyword in matchable_passage}))
    return Passage(passage, all_matches or matched, score)


class DocumentClient:
    def __init__(
        self,
        user_agent: str,
        cache_dir: Path,
        *,
        min_interval: float = 0.15,
        timeout: float = 15,
    ) -> None:
        if not user_agent or "@" not in user_agent:
            raise ValueError("SEC_USER_AGENT must include an email address")
        self.user_agent = user_agent
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval = min_interval
        self.timeout = timeout
        self._last_request = 0.0

    def get(self, url: str, cache_key: str) -> bytes:
        safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", cache_key)
        path = self.cache_dir / safe_key
        if path.is_file():
            return path.read_bytes()
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml,text/plain",
                "Accept-Encoding": "identity",
            },
        )
        retryable_statuses = {429, 500, 502, 503, 504}
        for attempt in range(3):
            delay = self.min_interval - (time.monotonic() - self._last_request)
            if delay > 0:
                time.sleep(delay)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = response.read()
                self._last_request = time.monotonic()
                break
            except urllib.error.HTTPError as exc:
                self._last_request = time.monotonic()
                if exc.code not in retryable_statuses or attempt == 2:
                    raise
                time.sleep(1.0 * (2**attempt))
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                self._last_request = time.monotonic()
                if attempt == 2:
                    raise
                time.sleep(1.0 * (2**attempt))
        path.write_bytes(payload)
        return payload


def selected_filing_rows(
    path: Path,
    event_limit: int,
    per_event: int,
    event_ids: set[str] | None = None,
) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows.sort(
        key=lambda row: (
            int(row["queue_rank"]),
            -int(row["evidence_relevance_score"]),
            abs(int(row["days_from_event"])),
        )
    )
    by_event: dict[str, list[dict[str, str]]] = defaultdict(list)
    event_order: list[str] = []
    for row in rows:
        event_id = row["event_candidate_id"]
        if event_ids and event_id not in event_ids:
            continue
        if event_id not in by_event:
            if len(event_order) >= event_limit:
                continue
            event_order.append(event_id)
        by_event[event_id].append(row)

    financing_forms = {"S-1", "S-3", "F-1", "F-3", "424B3", "424B5"}
    periodic_forms = {"10-Q", "10-K", "20-F", "40-F"}
    selected: list[dict[str, str]] = []
    for event_id in event_order:
        event_rows = by_event[event_id]
        chosen = event_rows[:per_event]
        if event_rows and event_rows[0].get("event_type") == "reverse_split":
            financing_row = next(
                (row for row in event_rows if row.get("form") in financing_forms),
                None,
            )
            if financing_row is not None and financing_row not in chosen:
                chosen.append(financing_row)
        if event_rows and event_rows[0].get("event_family") == "fundamental_shock":
            periodic_row = next(
                (row for row in event_rows if row.get("form") in periodic_forms),
                None,
            )
            if periodic_row is not None and periodic_row not in chosen:
                chosen.append(periodic_row)
        selected.extend(chosen)
    return selected


def extract_rows(
    filing_rows: Iterable[dict[str, str]],
    *,
    client: DocumentClient,
    max_chars: int,
) -> tuple[list[dict[str, str | int]], list[str]]:
    output: list[dict[str, str | int]] = []
    errors: list[str] = []
    for row in filing_rows:
        url = row["filing_document_url"]
        cache_key = f"{row['accession_number']}_{row['primary_document'].replace('/', '_')}"
        try:
            payload = client.get(url, cache_key)
            text = visible_text(payload)
            passage = passage_for_event(text, row["event_type"], max_chars)
            matched_url = url
            matched_payload = payload
            if (
                row["form"]
                in {
                    "8-K",
                    "8-K/A",
                    "6-K",
                    "6-K/A",
                    "10-Q",
                    "10-Q/A",
                    "10-K",
                    "10-K/A",
                    "20-F",
                    "20-F/A",
                }
                and row.get("filing_index_url")
            ):
                index_payload = client.get(
                    row["filing_index_url"], f"{row['accession_number']}_index.html"
                )
                for exhibit_index, exhibit_url in enumerate(exhibit_99_urls(index_payload), 1):
                    exhibit_payload = client.get(
                        exhibit_url, f"{row['accession_number']}_ex99_{exhibit_index}.html"
                    )
                    candidate = passage_for_event(
                        visible_text(exhibit_payload), row["event_type"], max_chars
                    )
                    if candidate.score > passage.score:
                        passage = candidate
                        matched_url = exhibit_url
                        matched_payload = exhibit_payload
        except (OSError, ValueError, urllib.error.URLError) as exc:
            errors.append(f"{row['event_candidate_id']} {url}: {type(exc).__name__}: {exc}")
            continue
        output.append(
            {
                "queue_rank": row["queue_rank"],
                "event_candidate_id": row["event_candidate_id"],
                "ticker_at_event": row["ticker_at_event"],
                "event_date": row["event_date"],
                "event_type": row["event_type"],
                "filing_date": row["filing_date"],
                "form": row["form"],
                "items": row["items"],
                "accession_number": row["accession_number"],
                "filing_document_url": matched_url,
                "form_item_match_hint": row["form_item_match_hint"],
                "text_sha256": hashlib.sha256(matched_payload).hexdigest(),
                "passage_score": passage.score,
                "matched_keywords": ";".join(passage.matched_keywords),
                "evidence_passage": passage.text,
                "passage_status": "candidate_passage" if passage.text else "no_keyword_passage",
                "auto_verification_allowed": "false",
            }
        )
    return output, errors


PASSAGE_COLUMNS = [
    "queue_rank",
    "event_candidate_id",
    "ticker_at_event",
    "event_date",
    "event_type",
    "filing_date",
    "form",
    "items",
    "accession_number",
    "filing_document_url",
    "form_item_match_hint",
    "text_sha256",
    "passage_score",
    "matched_keywords",
    "evidence_passage",
    "passage_status",
    "auto_verification_allowed",
]


def write_passages(path: Path, rows: list[dict[str, str | int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PASSAGE_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def run(
    *,
    candidates_path: Path,
    output_dir: Path,
    report_dir: Path,
    cache_dir: Path,
    user_agent: str,
    event_limit: int,
    per_event: int,
    max_chars: int,
    event_ids: set[str] | None = None,
) -> ExtractionRun:
    selected = selected_filing_rows(candidates_path, event_limit, per_event, event_ids)
    client = DocumentClient(user_agent, cache_dir)
    rows, errors = extract_rows(selected, client=client, max_chars=max_chars)
    passages_path = output_dir / "active_event_sec_evidence_passages.csv"
    manifest_path = output_dir / "active_event_sec_evidence_passages_manifest.json"
    report_path = report_dir / "active_event_sec_evidence_passages_latest.md"
    write_passages(passages_path, rows)

    generated_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        "schema_version": "active-event-sec-passages-v1",
        "generated_at": generated_at,
        "source": str(candidates_path.resolve()),
        "event_id_filter": sorted(event_ids) if event_ids else [],
        "selected_filings": len(selected),
        "passage_rows": len(rows),
        "passages_found": sum(row["passage_status"] == "candidate_passage" for row in rows),
        "errors": errors,
        "invariants": {
            "documents_cached": True,
            "passages_are_candidate_evidence": True,
            "automatic_label_mutation": False,
            "live_trading_allowed": False,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Active Event SEC Evidence Passages",
        "",
        f"Generated: `{generated_at}`",
        "",
        f"- Selected filings: `{len(selected)}`",
        f"- Extracted rows: `{len(rows)}`",
        f"- Candidate passages found: `{manifest['passages_found']}`",
        f"- Fetch/extraction errors: `{len(errors)}`",
        "- Boundary: passages are machine-selected review aids; they do not verify or grade an event.",
        "",
        "## Review Preview",
        "",
    ]
    for row in rows[:30]:
        preview = str(row["evidence_passage"]).replace("\n", " ")[:350]
        lines.extend(
            [
                f"### {row['ticker_at_event']} · {row['event_type']} · {row['form']} · {row['filing_date']}",
                "",
                f"- Match: `{row['form_item_match_hint']}`",
                f"- Keywords: `{row['matched_keywords']}`",
                f"- Source: {row['filing_document_url']}",
                f"- Candidate passage: {preview or '[no keyword passage found]'}",
                "",
            ]
        )
    if errors:
        lines.extend(["## Errors", ""] + [f"- `{error}`" for error in errors] + [""])
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return ExtractionRun(passages_path, report_path, manifest_path, len(rows), tuple(errors))


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Extract candidate evidence passages from SEC filings.")
    parser.add_argument("--config", type=Path, default=root / "config/active_event_research.json")
    parser.add_argument(
        "--candidates",
        type=Path,
        default=root / "data/research/active_event_sec_evidence_candidates.csv",
    )
    parser.add_argument("--output-dir", type=Path, default=root / "data/research")
    parser.add_argument("--report-dir", type=Path, default=root / "reports")
    parser.add_argument("--cache-dir", type=Path, default=root / "data/cache/sec/documents")
    parser.add_argument("--env", type=Path, default=root / ".env")
    parser.add_argument(
        "--event-id",
        action="append",
        default=[],
        help="Only extract filings for this event candidate ID; repeat for multiple events.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    env = {**load_simple_env(args.env), **os.environ}
    result = run(
        candidates_path=args.candidates,
        output_dir=args.output_dir,
        report_dir=args.report_dir,
        cache_dir=args.cache_dir,
        user_agent=env.get("SEC_USER_AGENT", ""),
        event_limit=int(config.get("sec_extract_events", 25)),
        per_event=int(config.get("sec_extract_filings_per_event", 2)),
        max_chars=int(config.get("sec_evidence_passage_max_chars", 700)),
        event_ids=set(args.event_id) or None,
    )
    print(result.passages_path)
    print(result.report_path)
    print(result.manifest_path)
    print(f"rows={result.rows} errors={len(result.errors)}")
    return 0 if not result.errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
