#!/usr/bin/env python3
"""Canonical review-thread grouping shared by triage and quality reporting."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Iterable


PRICE_CRASH_CLUSTER_DAYS = 30

EVENT_FAMILY_BY_TYPE = {
    "bankruptcy_liquidation": "bankruptcy_or_distress",
    "delisted": "delisting_or_suspension",
    "voluntarydelisting": "delisting_or_suspension",
    "reverse_split": "equity_dilution",
    "negative_equity": "fundamental_shock",
    "cash_short_debt_stress": "fundamental_shock",
    "revenue_collapse_yoy": "fundamental_shock",
    "free_cash_flow_turn_negative": "fundamental_shock",
    "gross_margin_collapse": "fundamental_shock",
    "interest_coverage_below_1": "fundamental_shock",
    "volume_crash": "price_crash",
    "one_day_crash": "price_crash",
    "five_day_crash": "price_crash",
    "twenty_one_day_crash": "price_crash",
}


def expanded_completed_thread_keys(
    threads: Iterable[tuple[str, str, str]],
) -> set[tuple[str, str, str]]:
    """Expand reviewed price episodes so later queue rotations cannot reselect siblings."""

    expanded: set[tuple[str, str, str]] = set()
    for stable_id, thread_date, family in threads:
        parsed = _parse_date(thread_date)
        if family != "price_crash" or parsed is None:
            expanded.add((stable_id, thread_date, family))
            continue
        for offset in range(-PRICE_CRASH_CLUSTER_DAYS, PRICE_CRASH_CLUSTER_DAYS + 1):
            expanded.add((stable_id, (parsed + timedelta(days=offset)).isoformat(), family))
    return expanded


def _event_id(row: dict[str, str]) -> str:
    return row.get("event_candidate_id") or ""


def _security(row: dict[str, str]) -> str:
    return row.get("stable_id") or _event_id(row)


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def review_thread_assignments(
    rows: Iterable[dict[str, str]],
) -> dict[str, tuple[str, str, str]]:
    """Map every event candidate to its canonical manual-review thread.

    Sibling detectors on the same security/date/family share a thread. Price-crash
    detectors also share a thread when they fall within 30 days of the first
    detector in a contiguous review episode. The fixed episode-start window
    prevents indefinite chaining of unrelated monthly declines.
    """

    rows = list(rows)
    assignments: dict[str, tuple[str, str, str]] = {}
    price_rows: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)

    for row in rows:
        event_id = _event_id(row)
        family = row.get("event_family") or ""
        if family == "price_crash":
            price_rows[(_security(row), family)].append(row)
        else:
            assignments[event_id] = (
                _security(row),
                row.get("event_date") or "",
                family,
            )

    for (security, family), events in price_rows.items():
        events.sort(
            key=lambda row: (
                _parse_date(row.get("event_date") or "") or date.max,
                int(row.get("queue_rank") or 0),
                _event_id(row),
            )
        )
        cluster_start: date | None = None
        cluster_label = ""
        for row in events:
            event_date = _parse_date(row.get("event_date") or "")
            if (
                cluster_start is None
                or event_date is None
                or (event_date - cluster_start).days > PRICE_CRASH_CLUSTER_DAYS
            ):
                cluster_start = event_date
                cluster_label = row.get("event_date") or _event_id(row)
            assignments[_event_id(row)] = (security, cluster_label, family)

    return assignments
