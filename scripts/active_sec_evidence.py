from __future__ import annotations

import argparse
import csv
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


SEC_SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"

FORM_PRIORITY = {
    "8-K": 100,
    "6-K": 95,
    "25": 94,
    "25-NSE": 94,
    "15-12B": 90,
    "15-12G": 90,
    "10-Q": 85,
    "10-K": 85,
    "20-F": 85,
    "40-F": 85,
    "DEF 14A": 75,
    "PRE 14A": 75,
    "DEFA14A": 70,
    "NT 10-Q": 82,
    "NT 10-K": 82,
    "S-1": 65,
    "S-3": 65,
    "F-1": 65,
    "F-3": 65,
    "424B3": 60,
    "424B5": 60,
}

REVERSE_SPLIT_FINANCING_FORMS = {"S-1", "S-3", "F-1", "F-3", "424B3", "424B5"}
PRICE_CRASH_EVENT_TYPES = {
    "volume_crash",
    "one_day_crash",
    "five_day_crash",
    "twenty_one_day_crash",
}

CORE_FINANCIAL_FORMS = {
    "8-K",
    "6-K",
    "10-Q",
    "10-K",
    "20-F",
    "40-F",
    "NT 10-Q",
    "NT 10-K",
}


def relevant_forms(event_type: str) -> set[str]:
    if event_type == "bankruptcy_liquidation":
        return CORE_FINANCIAL_FORMS | {"25", "25-NSE", "15-12B", "15-12G"}
    if event_type in {"delisted", "voluntarydelisting"}:
        return CORE_FINANCIAL_FORMS | {"25", "25-NSE", "15-12B", "15-12G"}
    if event_type == "reverse_split":
        return (
            CORE_FINANCIAL_FORMS
            | {"DEF 14A", "PRE 14A", "DEFA14A"}
            | REVERSE_SPLIT_FINANCING_FORMS
        )
    return CORE_FINANCIAL_FORMS | {"25", "25-NSE", "15-12B", "15-12G"}


def item_match_hint(event_type: str, form: str, items: str) -> tuple[int, str]:
    normalized_items = {item.strip() for item in items.split(",") if item.strip()}
    if event_type == "bankruptcy_liquidation" and "1.03" in normalized_items:
        return 60, "strong_form_item_match:8-K_1.03_bankruptcy_or_receivership"
    if event_type in {"delisted", "voluntarydelisting"}:
        if form in {"25", "25-NSE", "15-12B", "15-12G"}:
            return 55, "strong_form_match:exchange_or_registration_termination"
        if "3.01" in normalized_items:
            return 50, "strong_form_item_match:8-K_3.01_delisting_notice"
    if event_type == "reverse_split" and normalized_items.intersection({"5.03", "8.01"}):
        return 25, "possible_form_item_match:charter_or_other_event"
    if event_type == "reverse_split" and form in REVERSE_SPLIT_FINANCING_FORMS:
        return 20, "possible_financing_context_for_reverse_split"
    if event_type in PRICE_CRASH_EVENT_TYPES and "1.03" in normalized_items:
        return 55, "possible_price_cause:8-K_1.03_bankruptcy_or_receivership"
    if event_type in PRICE_CRASH_EVENT_TYPES and "3.01" in normalized_items:
        return 45, "possible_price_cause:8-K_3.01_delisting_notice"
    return 0, "relevant_form_needs_text_review"


@dataclass(frozen=True)
class EvidenceRun:
    evidence_path: Path
    report_path: Path
    manifest_path: Path
    event_count: int
    filing_count: int
    errors: tuple[str, ...]


def load_simple_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def normalize_cik(value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    if not digits:
        raise ValueError(f"Invalid CIK: {value!r}")
    return digits.zfill(10)


def rows_from_recent(recent: dict[str, list[Any]]) -> list[dict[str, Any]]:
    if not recent:
        return []
    count = max((len(values) for values in recent.values() if isinstance(values, list)), default=0)
    rows: list[dict[str, Any]] = []
    for index in range(count):
        row: dict[str, Any] = {}
        for key, values in recent.items():
            if isinstance(values, list):
                row[key] = values[index] if index < len(values) else None
        rows.append(row)
    return rows


def filing_urls(cik: str, accession: str, primary_document: str) -> tuple[str, str]:
    cik_no_zeros = str(int(normalize_cik(cik)))
    accession_compact = accession.replace("-", "")
    index_url = (
        f"{SEC_ARCHIVES_BASE}/{cik_no_zeros}/{accession_compact}/{accession}-index.html"
    )
    document_url = (
        f"{SEC_ARCHIVES_BASE}/{cik_no_zeros}/{accession_compact}/{primary_document}"
        if primary_document
        else index_url
    )
    return index_url, document_url


def event_form_bonus(event_type: str, form: str) -> int:
    if event_type == "bankruptcy_liquidation" and form in {"8-K", "6-K", "10-K", "20-F"}:
        return 30
    if event_type in {"delisted", "voluntarydelisting"} and form in {
        "25",
        "25-NSE",
        "15-12B",
        "15-12G",
        "8-K",
        "6-K",
    }:
        return 30
    if event_type in {
        "negative_equity",
        "cash_short_debt_stress",
        "revenue_collapse_yoy",
        "free_cash_flow_turn_negative",
        "gross_margin_collapse",
        "interest_coverage_below_1",
    } and form in {"10-Q", "10-K", "20-F", "40-F", "8-K", "6-K"}:
        return 25
    return 0


def select_filings(
    filings: Iterable[dict[str, Any]],
    *,
    event_date: date,
    event_type: str,
    before_days: int,
    after_days: int,
    limit: int,
) -> list[dict[str, Any]]:
    end = event_date + timedelta(days=after_days)
    selected: list[dict[str, Any]] = []
    for filing in filings:
        raw_date = str(filing.get("filingDate") or "")
        try:
            filing_date = date.fromisoformat(raw_date)
        except ValueError:
            continue
        form = str(filing.get("form") or "")
        if form not in relevant_forms(event_type):
            continue
        effective_before_days = event_lookback_days(event_type, form, before_days)
        start = event_date - timedelta(days=effective_before_days)
        if not start <= filing_date <= end:
            continue
        days_from_event = (filing_date - event_date).days
        items = str(filing.get("items") or "")
        item_bonus, match_hint = item_match_hint(event_type, form, items)
        relevance = FORM_PRIORITY.get(form, 30) + event_form_bonus(event_type, form) + item_bonus
        if event_type in PRICE_CRASH_EVENT_TYPES and days_from_event <= 0:
            relevance += 30
        relevance -= min(abs(days_from_event), 60)
        candidate = dict(filing)
        candidate["days_from_event"] = days_from_event
        candidate["evidence_relevance_score"] = relevance
        candidate["form_item_match_hint"] = match_hint
        selected.append(candidate)
    selected.sort(
        key=lambda row: (
            -int(row["evidence_relevance_score"]),
            abs(int(row["days_from_event"])),
            str(row.get("filingDate") or ""),
        )
    )
    return selected[:limit]


def event_lookback_days(event_type: str, form: str, before_days: int) -> int:
    if event_type in {"delisted", "voluntarydelisting"}:
        # Form 25 is usually the last administrative step.  The filing that
        # explains the cause often precedes it by weeks.
        return max(before_days, 45)
    if event_type == "reverse_split" and form in REVERSE_SPLIT_FINANCING_FORMS:
        return max(before_days, 60)
    if event_type in PRICE_CRASH_EVENT_TYPES:
        return max(before_days, 90)
    return before_days


def recent_submissions_cover_window(
    filings: Iterable[dict[str, Any]], earliest_needed: date
) -> bool:
    dates: list[date] = []
    for filing in filings:
        try:
            dates.append(date.fromisoformat(str(filing.get("filingDate") or "")))
        except ValueError:
            continue
    return bool(dates) and min(dates) <= earliest_needed


class SecClient:
    def __init__(
        self,
        user_agent: str,
        cache_dir: Path,
        *,
        min_interval: float = 0.15,
        timeout: float = 15,
    ) -> None:
        if not user_agent or "@" not in user_agent:
            raise ValueError("SEC_USER_AGENT must identify an application and include an email address")
        self.user_agent = user_agent
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval = min_interval
        self.timeout = timeout
        self._last_request = 0.0

    def _get_json(self, url: str, cache_path: Path) -> dict[str, Any]:
        if cache_path.is_file():
            return json.loads(cache_path.read_text(encoding="utf-8"))
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
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
                    payload = response.read().decode("utf-8")
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
        cache_path.write_text(payload, encoding="utf-8")
        return json.loads(payload)

    def submissions(self, cik: str, *, include_older_files: bool = True) -> list[dict[str, Any]]:
        normalized = normalize_cik(cik)
        primary = self._get_json(
            f"{SEC_SUBMISSIONS_BASE}/CIK{normalized}.json",
            self.cache_dir / f"CIK{normalized}.json",
        )
        filings = rows_from_recent(primary.get("filings", {}).get("recent", {}))
        if include_older_files:
            for item in primary.get("filings", {}).get("files", []):
                name = str(item.get("name") or "")
                if not name:
                    continue
                older = self._get_json(
                    f"{SEC_SUBMISSIONS_BASE}/{name}",
                    self.cache_dir / name,
                )
                filings.extend(rows_from_recent(older))
        return filings


def load_queue(
    path: Path,
    top_n: int,
    event_ids: set[str] | None = None,
) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    eligible = [
        row
        for row in rows
        if row.get("cik")
        and (not event_ids or row.get("event_candidate_id") in event_ids)
    ]
    return eligible[:top_n]


def build_evidence_rows(
    queue_rows: list[dict[str, str]],
    *,
    client: SecClient,
    before_days: int,
    after_days: int,
    filings_per_event: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    output: list[dict[str, Any]] = []
    errors: list[str] = []
    for event in queue_rows:
        cik = normalize_cik(event["cik"])
        try:
            event_date = date.fromisoformat(event["event_date"])
            filings = client.submissions(cik, include_older_files=False)
            widest_lookback = max(
                event_lookback_days(event["event_type"], form, before_days)
                for form in relevant_forms(event["event_type"])
            )
            earliest_needed = event_date - timedelta(days=widest_lookback)
            if not recent_submissions_cover_window(filings, earliest_needed):
                filings = client.submissions(cik, include_older_files=True)
            selected = select_filings(
                filings,
                event_date=event_date,
                event_type=event["event_type"],
                before_days=before_days,
                after_days=after_days,
                limit=filings_per_event,
            )
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
            errors.append(f"{event['event_candidate_id']} CIK={cik}: {type(exc).__name__}: {exc}")
            continue
        for filing in selected:
            accession = str(filing.get("accessionNumber") or "")
            primary_document = str(filing.get("primaryDocument") or "")
            index_url, document_url = filing_urls(cik, accession, primary_document)
            output.append(
                {
                    "queue_rank": event["queue_rank"],
                    "event_candidate_id": event["event_candidate_id"],
                    "ticker_at_event": event["ticker_at_event"],
                    "company_name": event["company_name"],
                    "cik": cik,
                    "event_date": event["event_date"],
                    "event_family": event["event_family"],
                    "event_type": event["event_type"],
                    "filing_date": filing.get("filingDate"),
                    "days_from_event": filing["days_from_event"],
                    "form": filing.get("form"),
                    "items": filing.get("items"),
                    "accession_number": accession,
                    "primary_document": primary_document,
                    "evidence_relevance_score": filing["evidence_relevance_score"],
                    "form_item_match_hint": filing["form_item_match_hint"],
                    "filing_index_url": index_url,
                    "filing_document_url": document_url,
                    "evidence_status": "candidate_primary_source",
                    "auto_verification_allowed": "false",
                }
            )
    return output, errors


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "queue_rank",
        "event_candidate_id",
        "ticker_at_event",
        "company_name",
        "cik",
        "event_date",
        "event_family",
        "event_type",
        "filing_date",
        "days_from_event",
        "form",
        "items",
        "accession_number",
        "primary_document",
        "evidence_relevance_score",
        "form_item_match_hint",
        "filing_index_url",
        "filing_document_url",
        "evidence_status",
        "auto_verification_allowed",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def run(
    *,
    queue_path: Path,
    output_dir: Path,
    report_dir: Path,
    cache_dir: Path,
    user_agent: str,
    top_n: int,
    before_days: int,
    after_days: int,
    filings_per_event: int,
    event_ids: set[str] | None = None,
) -> EvidenceRun:
    queue_rows = load_queue(queue_path, top_n, event_ids)
    client = SecClient(user_agent, cache_dir)
    rows, errors = build_evidence_rows(
        queue_rows,
        client=client,
        before_days=before_days,
        after_days=after_days,
        filings_per_event=filings_per_event,
    )
    evidence_path = output_dir / "active_event_sec_evidence_candidates.csv"
    manifest_path = output_dir / "active_event_sec_evidence_manifest.json"
    report_path = report_dir / "active_event_sec_evidence_latest.md"
    write_csv(evidence_path, rows)

    generated_at = datetime.now(timezone.utc).isoformat()
    events_with_filings = len({row["event_candidate_id"] for row in rows})
    manifest = {
        "schema_version": "active-event-sec-evidence-v1",
        "generated_at": generated_at,
        "queue_path": str(queue_path.resolve()),
        "event_id_filter": sorted(event_ids) if event_ids else [],
        "events_requested": len(queue_rows),
        "events_with_candidate_filings": events_with_filings,
        "filing_candidates": len(rows),
        "errors": errors,
        "window": {"before_days": before_days, "after_days": after_days},
        "filings_per_event": filings_per_event,
        "invariants": {
            "official_source": "SEC EDGAR submissions and archives",
            "candidate_filing_is_verified_event": False,
            "automatic_label_mutation": False,
            "live_trading_allowed": False,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Active Event SEC Evidence Candidates",
        "",
        f"Generated: `{generated_at}`",
        "",
        f"- Queue events requested: `{len(queue_rows)}`",
        f"- Events with filing candidates: `{events_with_filings}`",
        f"- Filing candidates: `{len(rows)}`",
        f"- Fetch errors: `{len(errors)}`",
        f"- Window: event date -{before_days} days to +{after_days} days",
        "- Boundary: a nearby SEC filing is candidate evidence, not automatic proof of event type or grade.",
        "",
        "## Candidate Filings",
        "",
        "| queue | ticker | event date | event type | filing date | form | days | score | SEC index |",
        "| ---: | --- | --- | --- | --- | --- | ---: | ---: | --- |",
    ]
    for row in rows[:100]:
        lines.append(
            "| {queue_rank} | {ticker_at_event} | {event_date} | {event_type} | "
            "{filing_date} | {form} | {days_from_event} | {evidence_relevance_score} | "
            "[open]({filing_index_url}) |".format(**row)
        )
    if errors:
        lines.extend(["", "## Fetch Errors", ""] + [f"- `{error}`" for error in errors])
    lines.extend(
        [
            "",
            "## Required Review",
            "",
            "Open the filing, identify the exact contemporaneous evidence sentence, determine whether it "
            "supports or contradicts the detected event, and only then assign verified/weak/rejected.",
            "",
        ]
    )
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return EvidenceRun(
        evidence_path,
        report_path,
        manifest_path,
        len(queue_rows),
        len(rows),
        tuple(errors),
    )


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Fetch SEC filing candidates for active event queue rows.")
    parser.add_argument("--config", type=Path, default=root / "config" / "active_event_research.json")
    parser.add_argument("--queue", type=Path, default=root / "data/research/active_event_research_queue.csv")
    parser.add_argument("--output-dir", type=Path, default=root / "data/research")
    parser.add_argument("--report-dir", type=Path, default=root / "reports")
    parser.add_argument("--cache-dir", type=Path, default=root / "data/cache/sec/submissions")
    parser.add_argument("--env", type=Path, default=root / ".env")
    parser.add_argument(
        "--event-id",
        action="append",
        default=[],
        help="Only inspect this event candidate ID; repeat for multiple events.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    env = {**load_simple_env(args.env), **os.environ}
    result = run(
        queue_path=args.queue,
        output_dir=args.output_dir,
        report_dir=args.report_dir,
        cache_dir=args.cache_dir,
        user_agent=env.get("SEC_USER_AGENT", ""),
        top_n=int(config.get("sec_evidence_top_n", 25)),
        before_days=int(config.get("sec_window_before_days", 10)),
        after_days=int(config.get("sec_window_after_days", 45)),
        filings_per_event=int(config.get("sec_filings_per_event", 5)),
        event_ids=set(args.event_id) or None,
    )
    print(result.evidence_path)
    print(result.report_path)
    print(result.manifest_path)
    print(
        f"events={result.event_count} filings={result.filing_count} errors={len(result.errors)}"
    )
    return 0 if not result.errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
