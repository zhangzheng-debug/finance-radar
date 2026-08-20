#!/usr/bin/env python3
"""Extract review-only evidence passages from captured official RSS article pages."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

try:
    from pypdf import PdfReader
except ImportError:  # HTML evidence enrichment must remain usable without PDF support.
    PdfReader = None

from event_ledger import open_ledger, stable_id, utc_now
from extract_sec_evidence_text import sentence_chunks, visible_text
from telegram_mtproto_listener import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_ENV = ROOT / ".env"
DEFAULT_CACHE = ROOT / "data" / "cache" / "official_primary_pages"
DEFAULT_REPORT = ROOT / "reports" / "official_primary_page_enrichment_latest.md"

SUPPORTED_SOURCES = {
    "cftc_enforcement",
    "fda_medwatch",
    "ftc_press",
    "sec_litigation_releases",
    "sec_trading_suspensions",
    "fdic_press_releases",
    "federal_reserve_press",
    "ecb_press",
    "ecb_statistical_press",
    "eia_press",
    "nvidia_official_news",
}

SOURCE_HOST_SUFFIXES = {
    "cftc_enforcement": ("cftc.gov",),
    "fda_medwatch": ("fda.gov",),
    "ftc_press": ("ftc.gov",),
    "sec_litigation_releases": ("sec.gov",),
    "sec_trading_suspensions": ("sec.gov",),
    "fdic_press_releases": ("fdic.gov", "govdelivery.com"),
    "federal_reserve_press": ("federalreserve.gov",),
    "ecb_press": ("ecb.europa.eu",),
    "ecb_statistical_press": ("ecb.europa.eu",),
    "eia_press": ("eia.gov",),
    "nvidia_official_news": ("nvidia.com",),
}

KEYWORDS_BY_EVENT_TYPE = {
    "cftc_enforcement_action": (
        "charges",
        "order",
        "fraud",
        "penalty",
        "civil monetary penalty",
        "disgorgement",
        "respondent",
        "defendant",
        "settlement",
    ),
    "enforcement_action": (
        "complaint",
        "order",
        "settlement",
        "penalty",
        "allegations",
        "violated",
        "prohibited",
        "civil penalty",
    ),
    "sec_litigation_release": (
        "securities and exchange commission",
        "complaint",
        "charged",
        "alleged",
        "fraud",
        "defendant",
        "injunction",
        "disgorgement",
        "civil penalty",
    ),
    "trading_suspension": (
        "suspension of trading",
        "suspend trading",
        "trading suspension",
        "potential manipulation",
        "artificially inflate",
        "social media",
        "accuracy",
        "adequacy",
        "public interest",
        "securities",
    ),
    "product_safety_alert": (
        "recall",
        "correction",
        "early alert",
        "risk",
        "injuries",
        "deaths",
        "remove",
        "device",
        "patients",
    ),
    "product_recall": (
        "recall",
        "removes",
        "risk",
        "injuries",
        "deaths",
        "affected product",
        "customers",
    ),
    "bank_receivership": (
        "closed",
        "receiver",
        "assumes all deposits",
        "insured deposits",
        "failed bank",
        "deposit insurance",
        "assets",
    ),
    "bank_regulatory_update": (
        "guidance",
        "final rule",
        "proposal",
        "effective date",
        "banking agencies",
    ),
    "bank_enforcement_orders_digest": (
        "enforcement orders",
        "consent order",
        "civil money penalty",
        "prompt corrective action",
        "prohibition order",
    ),
    "monetary_policy": (
        "monetary policy",
        "interest rates",
        "deposit facility",
        "governing council",
        "inflation",
        "rate decision",
        "policy rates",
    ),
    "earnings_or_guidance": (
        "revenue",
        "earnings",
        "guidance",
        "quarter",
        "fiscal year",
        "operating income",
    ),
    "energy_supply_update": (
        "production",
        "inventory",
        "supply",
        "crude oil",
        "natural gas",
        "refinery",
    ),
}

BOILERPLATE = re.compile(
    r"(?:skip to main content|share on facebook|follow us|subscribe|breadcrumb|"
    r"privacy policy|accessibility|contact us|back to top|javascript enabled)",
    re.I,
)
MATERIAL_FACT = re.compile(
    r"\b(?:entered|filed|requires?|must|ordered?|pay|penalt(?:y|ies)|"
    r"issued\s+(?:a\s+)?letter|affected|reported|received|classified|"
    r"risk\s+of|may\s+cause|can\s+cause|remove[sd]?|do\s+not\s+use|"
    r"injur(?:y|ies)|deaths?|fatalit(?:y|ies)|malfunction(?:s|ed)?|"
    r"complaints?|units?|devices?|customers?|patients?)\b",
    re.I,
)
STOPWORDS = {
    "about",
    "after",
    "against",
    "from",
    "into",
    "press",
    "release",
    "the",
    "this",
    "that",
    "their",
    "with",
}


@dataclass(frozen=True)
class FetchResult:
    body: bytes
    final_url: str


@dataclass(frozen=True)
class Passage:
    text: str
    matched_keywords: tuple[str, ...]
    score: int


Fetcher = Callable[[str, str, float], FetchResult]


def document_text(payload: bytes, url: str) -> str:
    if payload.startswith(b"%PDF") or urllib.parse.urlsplit(url).path.casefold().endswith(".pdf"):
        if PdfReader is None:
            raise ValueError("PDF evidence requires the optional 'pypdf' package")
        try:
            reader = PdfReader(io.BytesIO(payload))
            extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
            # Regulatory PDFs commonly wrap every visual line. Joining whitespace
            # restores complete factual sentences before passage scoring.
            return re.sub(r"\s+", " ", extracted.replace("\ufffd\ufffd", '"')).strip()
        except Exception as exc:
            raise ValueError(f"unable to extract PDF text: {exc}") from exc
    return visible_text(payload)


def host_allowed(source_id: str, url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username
        or parsed.password
        or port not in {None, 443}
    ):
        return False
    host = (parsed.hostname or "").casefold().rstrip(".")
    return any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in SOURCE_HOST_SUFFIXES.get(source_id, ())
    )


def canonical_official_url(source_id: str, url: str) -> str | None:
    """Upgrade registered official HTTP links to fetch-only HTTPS.

    Some government feeds still publish ``http://`` item links even though the
    same pages are served over HTTPS.  Only already-registered source domains
    may be upgraded; user info and non-default ports remain rejected.
    """

    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if parsed.username or parsed.password:
        return None
    host = (parsed.hostname or "").casefold().rstrip(".")
    if not any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in SOURCE_HOST_SUFFIXES.get(source_id, ())
    ):
        return None
    scheme = parsed.scheme.casefold()
    if scheme == "https" and port in {None, 443}:
        return urllib.parse.urlunsplit(("https", host, parsed.path, parsed.query, ""))
    if scheme == "http" and port in {None, 80}:
        return urllib.parse.urlunsplit(("https", host, parsed.path, parsed.query, ""))
    return None


def official_fetch_urls(source_id: str, url: str) -> tuple[str, ...]:
    """Return bounded official URL fallbacks for known upstream alias defects.

    SEC litigation RSS occasionally publishes a Drupal alias ending in ``-0``
    even though the live canonical page omits that suffix.  The fallback stays
    on the already allowlisted SEC host and is attempted only after the feed URL
    returns HTTP 404; the original observation remains unchanged in the ledger.
    """

    parsed = urllib.parse.urlsplit(url)
    if (
        source_id == "sec_litigation_releases"
        and re.fullmatch(
            r"/enforcement-litigation/litigation-releases/lr-\d+-0", parsed.path
        )
    ):
        canonical_path = parsed.path[:-2]
        fallback = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, canonical_path, parsed.query, "")
        )
        return (url, fallback)
    return (url,)


def fetch_page(url: str, user_agent: str, timeout: float) -> FetchResult:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,text/plain,application/pdf;q=0.9",
            "Accept-Encoding": "identity",
        },
    )
    retryable = {429, 500, 502, 503, 504}
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return FetchResult(response.read(), response.geturl())
        except urllib.error.HTTPError as exc:
            if exc.code not in retryable or attempt == 2:
                raise
            time.sleep(0.5 * (2**attempt))
    raise RuntimeError("unreachable fetch retry state")


def title_tokens(title: str) -> tuple[str, ...]:
    return tuple(
        token
        for token in re.findall(r"[a-z0-9][a-z0-9'-]{3,}", title.casefold())
        if token not in STOPWORDS
    )


def select_passage(text: str, *, title: str, event_type: str, max_chars: int) -> Passage:
    chunks = sentence_chunks(text)
    keywords = KEYWORDS_BY_EVENT_TYPE.get(event_type, ("official", "order", "action"))
    tokens = title_tokens(title)
    normalized_title = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
    scored: list[tuple[int, int, tuple[str, ...]]] = []
    for index, chunk in enumerate(chunks):
        lowered = chunk.casefold()
        if BOILERPLATE.search(lowered):
            continue
        matched = tuple(keyword for keyword in keywords if keyword in lowered)
        token_hits = sum(1 for token in tokens if token in lowered)
        fact_hits = len(MATERIAL_FACT.findall(lowered))
        normalized_chunk = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
        title_like = bool(
            normalized_title
            and normalized_title in normalized_chunk
            and len(normalized_chunk) <= len(normalized_title) + 90
        )
        if title_like and fact_hits == 0:
            continue
        score = len(matched) * 4 + min(token_hits, 6)
        score += min(fact_hits, 5) * 3
        if re.search(r"\b(?:million|billion|\$[0-9]|percent|%|20[0-9]{2})\b", lowered):
            score += 2
        if matched and len(chunk) >= 60:
            score += 2
        if matched and score:
            scored.append((score, index, matched))
    if not scored:
        return Passage("", (), 0)
    score, index, matched = max(scored, key=lambda item: (item[0], -item[1]))
    context = " ".join(chunks[max(0, index - 1) : min(len(chunks), index + 3)])
    context = re.sub(r"\s+", " ", context).strip()
    if len(context) > max_chars:
        original_length = len(context)
        best_chunk = chunks[index]
        anchor = context.find(best_chunk)
        start = max(0, anchor - max_chars // 4)
        end = min(len(context), start + max_chars)
        start = max(0, end - max_chars)
        context = ("…" if start else "") + context[start:end].strip()
        if end < original_length:
            context += "…"
    return Passage(context, tuple(sorted(set(matched))), score)


def pending_rows(connection: Any, *, limit: int, refresh: bool = False) -> list[Any]:
    placeholders = ",".join("?" for _ in SUPPORTED_SOURCES)
    evidence_filter = "" if refresh else "AND ev.evidence_id IS NULL"
    return connection.execute(
        f"""
        SELECT e.event_id,e.event_type,e.event_date,e.status,
               r.observation_id,r.source_id,r.title,r.canonical_url,r.source_published_at,
               CASE WHEN EXISTS (
                   SELECT 1
                   FROM pipeline_jobs light
                   WHERE light.event_id=e.event_id
                     AND light.job_type='light_verification_followup'
                     AND light.status='PENDING_EVIDENCE_REVIEW'
               ) THEN 1 ELSE 0 END AS light_followup_pending
        FROM canonical_events e
        JOIN event_observations eo ON eo.event_id=e.event_id
        JOIN latest_source_content r ON r.observation_id=eo.observation_id
        LEFT JOIN event_evidence ev
          ON ev.event_id=e.event_id AND ev.observation_id=r.observation_id
        WHERE (
              e.status='candidate'
              OR EXISTS (
                  SELECT 1
                  FROM pipeline_jobs explicit_light_followup
                  WHERE explicit_light_followup.event_id=e.event_id
                    AND explicit_light_followup.job_type='light_verification_followup'
                    AND explicit_light_followup.status='PENDING_EVIDENCE_REVIEW'
              )
          )
          AND r.source_id IN ({placeholders})
          AND r.canonical_url IS NOT NULL
          {evidence_filter}
        -- Follow-up work gets first access to the same bounded official-source
        -- enrichment path.  This only prioritizes evidence collection; it
        -- never closes or promotes the follow-up on its own.
        ORDER BY light_followup_pending DESC,r.source_published_at DESC,e.event_id
        LIMIT ?
        """,
        (*sorted(SUPPORTED_SOURCES), limit),
    ).fetchall()


def advance_existing_evidence_jobs(connection: Any) -> int:
    """Repair stale state when official evidence already exists in the ledger."""
    placeholders = ",".join("?" for _ in SUPPORTED_SOURCES)
    rows = connection.execute(
        f"""SELECT DISTINCT e.event_id
            FROM canonical_events e
            JOIN pipeline_jobs j ON j.event_id=e.event_id
            JOIN event_evidence ev ON ev.event_id=e.event_id
            JOIN raw_observations r ON r.observation_id=ev.observation_id
            WHERE e.status='candidate'
              AND j.job_type='live_primary_evidence_review'
              AND j.status='PENDING_PRIMARY_EVIDENCE'
              AND r.source_id IN ({placeholders})""",
        tuple(sorted(SUPPORTED_SOURCES)),
    ).fetchall()
    now = utc_now()
    for row in rows:
        connection.execute(
            """UPDATE pipeline_jobs
               SET status='PENDING_EVIDENCE_REVIEW',last_error=NULL,updated_at=?
               WHERE event_id=? AND job_type='live_primary_evidence_review'
                 AND status='PENDING_PRIMARY_EVIDENCE'""",
            (now, row["event_id"]),
        )
    connection.commit()
    return len(rows)


def cache_path(cache_dir: Path, url: str) -> Path:
    return cache_dir / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.html"


def enrich(
    connection: Any,
    *,
    cache_dir: Path,
    user_agent: str,
    limit: int,
    timeout: float,
    max_chars: int,
    refresh: bool = False,
    fetcher: Fetcher = fetch_page,
) -> dict[str, Any]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "selected": 0,
        "inserted": 0,
        "passages": 0,
        "link_only": 0,
        "errors": [],
        "by_type": {},
        "http_upgraded_to_https": 0,
        "canonical_url_fallbacks": 0,
        "jobs_advanced": advance_existing_evidence_jobs(connection),
        "light_followup_selected": 0,
    }
    rows = pending_rows(connection, limit=limit, refresh=refresh)
    result["selected"] = len(rows)
    for row in rows:
        result["light_followup_selected"] += int(row["light_followup_pending"] or 0)
        source_id = str(row["source_id"])
        original_url = str(row["canonical_url"])
        url = canonical_official_url(source_id, original_url)
        if url is None or not host_allowed(source_id, url):
            result["errors"].append(
                f"{row['event_id']}: disallowed host for {original_url}"
            )
            continue
        if urllib.parse.urlsplit(original_url).scheme.casefold() == "http":
            result["http_upgraded_to_https"] += 1
        try:
            payload: bytes | None = None
            final_url = url
            fetch_urls = official_fetch_urls(source_id, url)
            for fetch_index, fetch_url in enumerate(fetch_urls):
                path = cache_path(cache_dir, fetch_url)
                if path.is_file():
                    payload = path.read_bytes()
                    final_url = fetch_url
                    if fetch_index:
                        result["canonical_url_fallbacks"] += 1
                    break
                try:
                    fetched = fetcher(fetch_url, user_agent, timeout)
                except urllib.error.HTTPError as exc:
                    if exc.code == 404 and fetch_index + 1 < len(fetch_urls):
                        continue
                    raise
                payload = fetched.body
                final_url = fetched.final_url
                if not host_allowed(source_id, final_url):
                    raise ValueError(f"redirected to disallowed host: {final_url}")
                path.write_bytes(payload)
                if fetch_index:
                    result["canonical_url_fallbacks"] += 1
                break
            if payload is None:
                raise ValueError("official page fetch produced no payload")
            passage = select_passage(
                document_text(payload, final_url),
                title=str(row["title"]),
                event_type=str(row["event_type"]),
                max_chars=max_chars,
            )
        except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            result["errors"].append(
                f"{row['event_id']}: {type(exc).__name__}: {str(exc)[:240]}"
            )
            continue
        now = utc_now()
        evidence_id = stable_id("EVID", str(row["event_id"]), str(row["observation_id"]))
        status = (
            "machine_extracted_unreviewed"
            if passage.text
            else "link_only_no_relevant_passage"
        )
        connection.execute(
            """
            INSERT INTO event_evidence(
                evidence_id,event_id,observation_id,evidence_url,filing_date,form,items,
                evidence_passage,matched_keywords,passage_score,evidence_status,
                auto_verification_allowed,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_id,observation_id) DO UPDATE SET
                evidence_url=excluded.evidence_url,
                evidence_passage=excluded.evidence_passage,
                matched_keywords=excluded.matched_keywords,
                passage_score=excluded.passage_score,
                evidence_status=excluded.evidence_status,
                auto_verification_allowed=0,
                updated_at=excluded.updated_at
            """,
            (
                evidence_id,
                row["event_id"],
                row["observation_id"],
                final_url,
                (row["source_published_at"] or row["event_date"] or "")[:10],
                row["source_id"],
                "",
                passage.text,
                ";".join(passage.matched_keywords),
                passage.score,
                status,
                0,
                now,
                now,
            ),
        )
        advanced = connection.execute(
            """UPDATE pipeline_jobs
               SET status='PENDING_EVIDENCE_REVIEW',last_error=NULL,updated_at=?
               WHERE event_id=? AND job_type='live_primary_evidence_review'
                 AND status='PENDING_PRIMARY_EVIDENCE'""",
            (now, row["event_id"]),
        ).rowcount
        result["jobs_advanced"] += int(advanced)
        result["inserted"] += 1
        result["passages"] += int(bool(passage.text))
        result["link_only"] += int(not passage.text)
        result["by_type"][row["event_type"]] = result["by_type"].get(row["event_type"], 0) + 1
    connection.commit()
    return result


def write_report(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Official Primary Page Enrichment",
        "",
        f"- Selected official candidate pages: `{result['selected']}`",
        f"- Evidence rows inserted: `{result['inserted']}`",
        f"- Relevant passages extracted: `{result['passages']}`",
        f"- Link-only rows: `{result['link_only']}`",
        f"- Canonical URL fallbacks: `{result.get('canonical_url_fallbacks', 0)}`",
        f"- Review jobs advanced: `{result.get('jobs_advanced', 0)}`",
        f"- Errors: `{len(result['errors'])}`",
        "- Safety: extracted text is review-only; status and severity are never auto-promoted.",
        "",
        "## By event type",
        "",
    ]
    for event_type, count in sorted(result["by_type"].items()):
        lines.append(f"- `{event_type}`: `{count}`")
    if result["errors"]:
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {error}" for error in result["errors"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--max-chars", type=int, default=1200)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-score already extracted official pages using the current passage rules.",
    )
    args = parser.parse_args()
    load_dotenv(args.env_file)
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent or "@" not in user_agent:
        raise SystemExit("SEC_USER_AGENT with a contact email is required")
    connection = open_ledger(args.db)
    try:
        result = enrich(
            connection,
            cache_dir=args.cache_dir,
            user_agent=user_agent,
            limit=args.limit,
            timeout=args.timeout,
            max_chars=args.max_chars,
            refresh=args.refresh,
        )
    finally:
        connection.close()
    write_report(args.report, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print(f"REPORT={args.report}")
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
