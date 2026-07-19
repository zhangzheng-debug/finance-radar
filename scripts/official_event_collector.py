#!/usr/bin/env python3
"""Poll free official market-event sources into the immutable event inbox."""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from event_ledger import (
    enqueue_observation_job,
    get_source_cursor,
    open_ledger,
    record_source_observation,
    record_source_poll,
    stable_json,
    upsert_source,
    utc_now,
)
from telegram_mtproto_listener import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_ENV = ROOT / ".env"
BLS_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"


@dataclass(frozen=True)
class FeedSpec:
    source_id: str
    name: str
    url: str
    format: str
    priority: int
    min_interval_seconds: int
    max_entry_age_days: int | None = None
    source_type: str = "official_primary_feed"
    authority_tier: str = "P0_official"


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes
    headers: dict[str, str]


Fetcher = Callable[[str, dict[str, str], float, bytes | None], HttpResult]


FED_FEED = FeedSpec(
    source_id="federal_reserve_press",
    name="Federal Reserve Board press releases",
    url="https://www.federalreserve.gov/feeds/press_all.xml",
    format="rss",
    priority=92,
    min_interval_seconds=300,
)
SEC_FEED = FeedSpec(
    source_id="sec_current_filings",
    name="SEC EDGAR latest filings",
    url=(
        "https://www.sec.gov/cgi-bin/browse-edgar?"
        "action=getcurrent&owner=exclude&output=atom&count=100"
    ),
    format="atom",
    priority=88,
    min_interval_seconds=300,
)

CFTC_ENFORCEMENT_FEED = FeedSpec(
    source_id="cftc_enforcement",
    name="CFTC enforcement press releases",
    url="https://www.cftc.gov/RSS/RSSENF/rssenf.xml",
    format="rss",
    priority=93,
    min_interval_seconds=300,
    max_entry_age_days=45,
)
FDA_MEDWATCH_FEED = FeedSpec(
    source_id="fda_medwatch",
    name="FDA MedWatch safety alerts",
    url=(
        "https://www.fda.gov/about-fda/contact-fda/stay-informed/"
        "rss-feeds/medwatch/rss.xml"
    ),
    format="rss",
    priority=93,
    min_interval_seconds=300,
    max_entry_age_days=14,
)
FTC_PRESS_FEED = FeedSpec(
    source_id="ftc_press",
    name="Federal Trade Commission press releases",
    url="https://www.ftc.gov/feeds/press-release.xml",
    format="rss",
    priority=89,
    min_interval_seconds=300,
    max_entry_age_days=14,
)
SEC_LITIGATION_FEED = FeedSpec(
    source_id="sec_litigation_releases",
    name="SEC litigation releases",
    url="https://www.sec.gov/enforcement-litigation/litigation-releases/rss",
    format="rss",
    priority=94,
    min_interval_seconds=300,
    max_entry_age_days=14,
)
SEC_TRADING_SUSPENSION_FEED = FeedSpec(
    source_id="sec_trading_suspensions",
    name="SEC trading suspension releases",
    url="https://www.sec.gov/enforcement-litigation/trading-suspensions/rss",
    format="rss",
    priority=95,
    min_interval_seconds=300,
    max_entry_age_days=90,
)
FDIC_PRESS_FEED = FeedSpec(
    source_id="fdic_press_releases",
    name="FDIC press releases",
    url="https://public.govdelivery.com/topics/USFDIC_26/feed.rss",
    format="rss",
    priority=94,
    min_interval_seconds=300,
    max_entry_age_days=30,
)
NVIDIA_OFFICIAL_FEED = FeedSpec(
    source_id="nvidia_official_news",
    name="NVIDIA official newsroom",
    url="https://nvidianews.nvidia.com/releases.xml",
    format="rss",
    priority=82,
    min_interval_seconds=600,
    max_entry_age_days=30,
    source_type="issuer_official_feed",
    authority_tier="P1_issuer_official",
)
ECB_PRESS_FEED = FeedSpec(
    source_id="ecb_press",
    name="European Central Bank press and speeches",
    url="https://www.ecb.europa.eu/rss/press.html",
    format="rss",
    priority=88,
    min_interval_seconds=600,
    max_entry_age_days=30,
)
ECB_STATISTICAL_PRESS_FEED = FeedSpec(
    source_id="ecb_statistical_press",
    name="European Central Bank statistical press releases",
    url="https://www.ecb.europa.eu/rss/statpress.html",
    format="rss",
    priority=87,
    min_interval_seconds=600,
    max_entry_age_days=30,
)
EIA_PRESS_FEED = FeedSpec(
    source_id="eia_press",
    name="U.S. Energy Information Administration press releases",
    url="https://www.eia.gov/rss/press_rss.xml",
    format="rss",
    priority=86,
    min_interval_seconds=600,
    max_entry_age_days=90,
)

ADDITIONAL_OFFICIAL_FEEDS = (
    CFTC_ENFORCEMENT_FEED,
    FDA_MEDWATCH_FEED,
    FTC_PRESS_FEED,
    SEC_LITIGATION_FEED,
    SEC_TRADING_SUSPENSION_FEED,
    FDIC_PRESS_FEED,
    NVIDIA_OFFICIAL_FEED,
    ECB_PRESS_FEED,
    ECB_STATISTICAL_PRESS_FEED,
    EIA_PRESS_FEED,
)

SEC_EVENT_FORMS = {
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
    "NT 10-Q",
    "NT 10-K",
    "25",
    "25-NSE",
    "15-12B",
    "15-12G",
}

BLS_SERIES = {
    "CUUR0000SA0": ("consumer_price_index", "CPI-U unadjusted", 94),
    "CUSR0000SA0": ("consumer_price_index", "CPI-U seasonally adjusted", 94),
    "CUUR0000SA0L1E": ("consumer_price_index", "core CPI unadjusted", 94),
    "CUSR0000SA0L1E": ("consumer_price_index", "core CPI seasonally adjusted", 94),
    "WPUFD4": ("producer_price_index", "final demand PPI unadjusted", 90),
    "WPSFD4": ("producer_price_index", "final demand PPI seasonally adjusted", 90),
    "CES0000000001": ("employment_situation", "nonfarm payrolls", 94),
    "LNS14000000": ("employment_situation", "unemployment rate", 94),
    "JTS000000000000000JOL": ("job_openings", "job openings", 88),
}

BLS_RELEASES = {
    "consumer_price_index": (
        "BLS Consumer Price Index latest official series snapshot",
        "https://www.bls.gov/cpi/",
    ),
    "producer_price_index": (
        "BLS Producer Price Index latest official series snapshot",
        "https://www.bls.gov/ppi/",
    ),
    "employment_situation": (
        "BLS Employment Situation latest official series snapshot",
        "https://www.bls.gov/ces/",
    ),
    "job_openings": (
        "BLS Job Openings and Labor Turnover latest official series snapshot",
        "https://www.bls.gov/jlt/",
    ),
}


def _numeric(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _pct_change(current: Any, previous: Any) -> float | None:
    current_number = _numeric(current)
    previous_number = _numeric(previous)
    if current_number is None or previous_number in {None, 0.0}:
        return None
    return round((current_number / previous_number - 1.0) * 100.0, 1)


def bls_derived_metrics(
    release_key: str, points: list[dict[str, Any]]
) -> tuple[dict[str, Any], str]:
    by_series = {str(row["seriesID"]): row for row in points}
    metrics: dict[str, Any] = {}

    def monthly(series_id: str) -> float | None:
        row = by_series.get(series_id) or {}
        return _pct_change(row.get("value"), (row.get("previous") or {}).get("value"))

    def yearly(series_id: str) -> float | None:
        row = by_series.get(series_id) or {}
        return _pct_change(row.get("value"), (row.get("year_ago") or {}).get("value"))

    if release_key == "consumer_price_index":
        metrics = {
            "all_items_monthly_sa_pct": monthly("CUSR0000SA0"),
            "all_items_12m_unadjusted_pct": yearly("CUUR0000SA0"),
            "core_monthly_sa_pct": monthly("CUSR0000SA0L1E"),
            "core_12m_unadjusted_pct": yearly("CUUR0000SA0L1E"),
        }
    elif release_key == "producer_price_index":
        metrics = {
            "final_demand_monthly_sa_pct": monthly("WPSFD4"),
            "final_demand_12m_unadjusted_pct": yearly("WPUFD4"),
        }
    elif release_key == "employment_situation":
        payroll = by_series.get("CES0000000001") or {}
        current = _numeric(payroll.get("value"))
        previous = _numeric((payroll.get("previous") or {}).get("value"))
        metrics = {
            "nonfarm_payroll_monthly_change_thousands": (
                round(current - previous, 0)
                if current is not None and previous is not None
                else None
            ),
            "unemployment_rate_pct": _numeric(
                (by_series.get("LNS14000000") or {}).get("value")
            ),
            "previous_unemployment_rate_pct": _numeric(
                ((by_series.get("LNS14000000") or {}).get("previous") or {}).get("value")
            ),
        }
    elif release_key == "job_openings":
        row = by_series.get("JTS000000000000000JOL") or {}
        metrics = {
            "job_openings_thousands": _numeric(row.get("value")),
            "previous_job_openings_thousands": _numeric(
                (row.get("previous") or {}).get("value")
            ),
        }

    summary = "; ".join(
        f"{key}: {value}" for key, value in metrics.items() if value is not None
    )
    return metrics, summary


def fetch_http(
    url: str,
    headers: dict[str, str],
    timeout: float,
    data: bytes | None = None,
) -> HttpResult:
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResult(
                status=int(response.status),
                body=response.read(),
                headers={key.casefold(): value for key, value in response.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return HttpResult(
                status=304,
                body=b"",
                headers={key.casefold(): value for key, value in exc.headers.items()},
            )
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {url}: {detail[:240]}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"request failed for {url}: {exc}") from exc


def strip_markup(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"<br\s*/?>", " ", value, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html.unescape(text).split())


def normalize_rss_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return value.strip() or None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat()


def entry_is_recent(value: str | None, *, max_age_days: int | None) -> bool:
    if max_age_days is None or not value:
        return True
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)
    return parsed.astimezone(dt.timezone.utc) >= cutoff


def element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


XML_INVALID_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
XML_UNSAFE_AMPERSAND = re.compile(
    r"&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9A-Fa-f]+;)"
)


def parse_xml_root(body: bytes) -> tuple[ET.Element, bool]:
    """Parse strictly first, then repair only common upstream feed defects."""
    try:
        return ET.fromstring(body), False
    except ET.ParseError as original:
        text = body.decode("utf-8-sig", errors="replace")
        repaired = XML_INVALID_CONTROL.sub("", text)
        repaired = XML_UNSAFE_AMPERSAND.sub("&amp;", repaired)
        if repaired == text:
            raise original
        return ET.fromstring(repaired.encode("utf-8")), True


def parse_rss_root(root: ET.Element) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        title = element_text(item.find("title"))
        link = element_text(item.find("link")) or None
        guid = element_text(item.find("guid"))
        description = element_text(item.find("description"))
        published = element_text(item.find("pubDate"))
        category = element_text(item.find("category"))
        external_id = guid or link or hashlib.sha256(
            f"{title}|{published}".encode("utf-8")
        ).hexdigest()
        entries.append(
            {
                "external_id": external_id,
                "title": strip_markup(title),
                "summary": strip_markup(description) or strip_markup(title),
                "canonical_url": link,
                "published_at": normalize_rss_date(published),
                "category": category,
            }
        )
    return entries


def parse_rss(body: bytes) -> list[dict[str, Any]]:
    root, _repaired = parse_xml_root(body)
    return parse_rss_root(root)


def parse_sec_title(title: str) -> tuple[str, str | None, str | None]:
    form, separator, remainder = title.partition(" - ")
    if not separator:
        return title.strip(), None, None
    cik_match = re.search(r"\((\d{10})\)", remainder)
    company = re.sub(r"\s*\(\d{10}\).*?$", "", remainder).strip() or None
    return form.strip(), company, cik_match.group(1) if cik_match else None


def parse_atom_root(root: ET.Element) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for entry in root.findall("{*}entry"):
        title = element_text(entry.find("{*}title"))
        external_id = element_text(entry.find("{*}id"))
        updated = element_text(entry.find("{*}updated")) or None
        summary_html = element_text(entry.find("{*}summary"))
        link = next(
            (
                node.attrib.get("href")
                for node in entry.findall("{*}link")
                if node.attrib.get("rel", "alternate") == "alternate"
            ),
            None,
        )
        form, company, cik = parse_sec_title(title)
        items = sorted(set(re.findall(r"Item\s+([0-9.]+)", summary_html, re.I)))
        entries.append(
            {
                "external_id": external_id or link or hashlib.sha256(title.encode()).hexdigest(),
                "title": strip_markup(title),
                "summary": strip_markup(summary_html),
                "canonical_url": link,
                "published_at": updated,
                "form": form,
                "company": company,
                "cik": cik,
                "items": items,
            }
        )
    return entries


def parse_atom(body: bytes) -> list[dict[str, Any]]:
    root, _repaired = parse_xml_root(body)
    return parse_atom_root(root)


def should_poll(cursor: Any, *, min_interval_seconds: int) -> bool:
    if cursor is None or not cursor["last_polled_at"] or min_interval_seconds <= 0:
        return True
    try:
        last = dt.datetime.fromisoformat(cursor["last_polled_at"])
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - last).total_seconds() >= min_interval_seconds


def conditional_headers(cursor: Any) -> dict[str, str]:
    headers: dict[str, str] = {}
    if cursor is not None and cursor["etag"]:
        headers["If-None-Match"] = cursor["etag"]
    if cursor is not None and cursor["last_modified"]:
        headers["If-Modified-Since"] = cursor["last_modified"]
    return headers


def collect_feed(
    connection: Any,
    spec: FeedSpec,
    *,
    user_agent: str,
    fetcher: Fetcher = fetch_http,
    timeout: float = 30.0,
    force: bool = False,
) -> dict[str, Any]:
    upsert_source(
        connection,
        source_id=spec.source_id,
        name=spec.name,
        source_type=spec.source_type,
        authority_tier=spec.authority_tier,
    )
    cursor = get_source_cursor(connection, spec.source_id)
    if not force and not should_poll(cursor, min_interval_seconds=spec.min_interval_seconds):
        return {"source_id": spec.source_id, "skipped_interval": 1, "items": 0, "jobs": 0}

    headers = {
        "User-Agent": user_agent,
        "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml",
    }
    headers.update(conditional_headers(cursor))
    try:
        response = fetcher(spec.url, headers, timeout, None)
        if response.status == 304:
            record_source_poll(
                connection,
                source_id=spec.source_id,
                cursor_type="http_conditional",
                cursor_value=None,
                status="NOT_MODIFIED",
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
            )
            connection.commit()
            return {"source_id": spec.source_id, "not_modified": 1, "items": 0, "jobs": 0}
        if response.status != 200:
            raise RuntimeError(f"unexpected HTTP status {response.status} for {spec.url}")
        root, xml_repaired = parse_xml_root(response.body)
        entries = parse_rss_root(root) if spec.format == "rss" else parse_atom_root(root)
    except (RuntimeError, ET.ParseError, ValueError) as exc:
        record_source_poll(
            connection,
            source_id=spec.source_id,
            cursor_type="http_conditional",
            cursor_value=None,
            status="ERROR",
            error=str(exc)[:500],
        )
        connection.commit()
        raise RuntimeError(f"{spec.source_id}: {exc}") from exc

    received_at = utc_now()
    counts: dict[str, Any] = {
        "source_id": spec.source_id,
        "xml_repaired": int(xml_repaired),
        "items": 0,
        "filtered": 0,
        "new_revisions": 0,
        "jobs": 0,
    }
    for entry in entries:
        if not entry_is_recent(
            entry.get("published_at"), max_age_days=spec.max_entry_age_days
        ):
            counts["filtered"] += 1
            continue
        if spec.source_id == SEC_FEED.source_id and entry.get("form") not in SEC_EVENT_FORMS:
            counts["filtered"] += 1
            continue
        counts["items"] += 1
        raw_json = stable_json({"provider": spec.source_id, "item": entry})
        observation_id, inserted_revision = record_source_observation(
            connection,
            source_id=spec.source_id,
            external_id=str(entry["external_id"]),
            source_published_at=entry.get("published_at"),
            local_received_at=received_at,
            title=str(entry["title"]),
            summary=str(entry["summary"]),
            canonical_url=entry.get("canonical_url"),
            content_sha256=hashlib.sha256(raw_json.encode("utf-8")).hexdigest(),
            raw_json=raw_json,
            revision_kind="edit",
            revision_at=received_at,
        )
        counts["new_revisions"] += int(inserted_revision)
        priority = spec.priority
        if spec.source_id == SEC_FEED.source_id and entry.get("form") in {"8-K", "6-K"}:
            priority = 91
        if enqueue_observation_job(
            connection,
            observation_id=observation_id,
            job_type="extract_live_event_candidate",
            priority=priority,
            payload={
                "source": spec.source_id,
                "authority_tier": spec.authority_tier,
                "form": entry.get("form"),
            },
        ):
            counts["jobs"] += 1

    cursor_value = str(entries[0]["external_id"]) if entries else None
    record_source_poll(
        connection,
        source_id=spec.source_id,
        cursor_type="http_conditional",
        cursor_value=cursor_value,
        status="SUCCESS",
        etag=response.headers.get("etag"),
        last_modified=response.headers.get("last-modified"),
        polled_at=received_at,
    )
    connection.commit()
    return counts


def collect_bls(
    connection: Any,
    *,
    fetcher: Fetcher = fetch_http,
    timeout: float = 30.0,
    force: bool = False,
    min_interval_seconds: int = 5400,
) -> dict[str, Any]:
    source_id = "bls_key_indicators"
    upsert_source(
        connection,
        source_id=source_id,
        name="BLS key economic indicators API",
        source_type="official_primary_api",
        authority_tier="P0_official",
    )
    cursor = get_source_cursor(connection, source_id)
    if not force and not should_poll(cursor, min_interval_seconds=min_interval_seconds):
        return {"source_id": source_id, "skipped_interval": 1, "items": 0, "jobs": 0}

    request_body = stable_json({"seriesid": list(BLS_SERIES)}).encode("utf-8")
    headers = {"User-Agent": "FinanceRadar/1.0", "Content-Type": "application/json"}
    try:
        response = fetcher(BLS_API_URL, headers, timeout, request_body)
        if response.status != 200:
            raise RuntimeError(f"unexpected HTTP status {response.status} for BLS API")
        payload = json.loads(response.body.decode("utf-8"))
        if payload.get("status") != "REQUEST_SUCCEEDED":
            raise RuntimeError(f"BLS API failed: {payload.get('message')}")
        results = payload.get("Results") or {}
        series_rows = results.get("series") if isinstance(results, dict) else None
        if not isinstance(series_rows, list):
            raise ValueError("BLS response did not contain Results.series")
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        record_source_poll(
            connection,
            source_id=source_id,
            cursor_type="latest_release_periods",
            cursor_value=None,
            status="ERROR",
            error=str(exc)[:500],
        )
        connection.commit()
        raise RuntimeError(f"{source_id}: {exc}") from exc

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    latest_periods: dict[str, str] = {}
    for series in series_rows:
        series_id = str(series.get("seriesID") or "")
        if series_id not in BLS_SERIES:
            continue
        data = series.get("data") or []
        if not data:
            continue
        point = dict(data[0])
        previous = dict(data[1]) if len(data) > 1 else None
        year, period = str(point.get("year") or ""), str(point.get("period") or "")
        if not year or not period:
            continue
        release_key, label, _priority = BLS_SERIES[series_id]
        year_ago = next(
            (
                dict(candidate)
                for candidate in data[1:]
                if str(candidate.get("year") or "") == str(int(year) - 1)
                and str(candidate.get("period") or "") == period
            ),
            None,
        )
        point.update(
            {
                "seriesID": series_id,
                "label": label,
                "previous": previous,
                "year_ago": year_ago,
                "revision_status": (
                    "preliminary"
                    if any(
                        str(note.get("code") or "").upper() == "P"
                        for note in point.get("footnotes") or []
                        if isinstance(note, dict)
                    )
                    else "not_marked_preliminary"
                ),
            }
        )
        grouped.setdefault((release_key, year, period), []).append(point)
        latest_periods[series_id] = f"{year}-{period}"

    received_at = utc_now()
    counts: dict[str, Any] = {
        "source_id": source_id,
        "items": 0,
        "new_revisions": 0,
        "jobs": 0,
    }
    for (release_key, year, period), points in grouped.items():
        title_base, canonical_url = BLS_RELEASES[release_key]
        period_name = next((str(row.get("periodName")) for row in points if row.get("periodName")), period)
        title = f"{title_base} for {period_name} {year}"
        derived_metrics, summary = bls_derived_metrics(release_key, points)
        if not summary:
            summary = "; ".join(
                f"{row['label']}: {row.get('value')}"
                for row in sorted(points, key=lambda value: value["seriesID"])
            )
        entry = {
            "release_key": release_key,
            "reference_year": year,
            "reference_period": period,
            "reference_period_name": period_name,
            "series": sorted(points, key=lambda row: row["seriesID"]),
            "derived_metrics": derived_metrics,
            "source_publication_timestamp": None,
            "source_publication_timestamp_status": "unavailable_from_bls_public_api",
            "event_time_basis": "local_first_observation_until_release_page_confirmation",
            "market_expectation": None,
            "market_expectation_status": "N/A_no_free_official_consensus_source",
        }
        raw_json = stable_json({"provider": source_id, "item": entry})
        observation_id, inserted_revision = record_source_observation(
            connection,
            source_id=source_id,
            external_id=f"{release_key}:{year}:{period}",
            source_published_at=None,
            local_received_at=received_at,
            title=title,
            summary=summary,
            canonical_url=canonical_url,
            content_sha256=hashlib.sha256(raw_json.encode("utf-8")).hexdigest(),
            raw_json=raw_json,
            revision_kind="edit",
            revision_at=received_at,
        )
        counts["items"] += 1
        counts["new_revisions"] += int(inserted_revision)
        priority = max(BLS_SERIES[row["seriesID"]][2] for row in points)
        if enqueue_observation_job(
            connection,
            observation_id=observation_id,
            job_type="extract_live_event_candidate",
            priority=priority,
            payload={
                "source": source_id,
                "authority_tier": "P0_official",
                "release_key": release_key,
            },
        ):
            counts["jobs"] += 1

    record_source_poll(
        connection,
        source_id=source_id,
        cursor_type="latest_release_periods",
        cursor_value=stable_json(latest_periods),
        status="SUCCESS",
        polled_at=received_at,
    )
    connection.commit()
    return counts


def collect_all(
    connection: Any,
    *,
    sec_user_agent: str | None,
    timeout: float = 30.0,
    force: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {"sources": {}, "errors": []}
    collectors: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        (FED_FEED.source_id, lambda: collect_feed(
            connection,
            FED_FEED,
            user_agent="FinanceRadar/1.0",
            timeout=timeout,
            force=force,
        )),
        ("bls_key_indicators", lambda: collect_bls(
            connection,
            timeout=timeout,
            force=force,
        )),
    ]
    for spec in ADDITIONAL_OFFICIAL_FEEDS:
        if spec.source_id.startswith("sec_") and not sec_user_agent:
            result["errors"].append(
                f"{spec.source_id}: SEC_USER_AGENT missing; SEC polling skipped"
            )
            continue
        feed_user_agent = (
            sec_user_agent
            if spec.source_id.startswith("sec_")
            else "FinanceRadar/1.0"
        )
        collectors.append(
            (
                spec.source_id,
                lambda spec=spec, feed_user_agent=feed_user_agent: collect_feed(
                    connection,
                    spec,
                    user_agent=feed_user_agent,
                    timeout=timeout,
                    force=force,
                ),
            )
        )
    if sec_user_agent:
        collectors.insert(
            1,
            (SEC_FEED.source_id, lambda: collect_feed(
                connection,
                SEC_FEED,
                user_agent=sec_user_agent,
                timeout=timeout,
                force=force,
            )),
        )
    else:
        result["errors"].append(
            "sec_current_filings: SEC_USER_AGENT missing; SEC polling skipped"
        )
    for source_id, collector in collectors:
        try:
            result["sources"][source_id] = collector()
        except RuntimeError as exc:
            result["errors"].append(str(exc))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    load_dotenv(args.env_file)
    sec_user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not sec_user_agent:
        raise SystemExit("SEC_USER_AGENT is required for compliant SEC access")
    connection = open_ledger(args.db)
    try:
        result = collect_all(
            connection,
            sec_user_agent=sec_user_agent,
            timeout=args.timeout,
            force=args.force,
        )
    finally:
        connection.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
