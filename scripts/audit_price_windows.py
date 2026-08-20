#!/usr/bin/env python3
"""Audit whether post-event market windows mean what their labels say.

This audit answers four questions, in the order that matters.  The first two
need no market data at all, which is why the module can be built and defended
before a single quote provider is connected:

1. Anchor correctness.  ``t_plus_30m`` is only meaningful relative to a stated
   zero point.  The observer currently anchors on the first successful quote
   capture, which drifts with collector latency; the playbook declares what each
   event family's anchor should be.  A mismatch is reported, never silently
   relabelled.
2. Window fulfilment.  Scheduled versus captured versus missed, with an honest
   denominator: a window that was never scheduled is still a window the product
   did not deliver.
3. Capture lateness.  A quote actually obtained at T+11m must not be presented
   under a T+5m label, so captures outside their grace period are counted.
4. No backfill.  A missed window must stay missed.  This proves no snapshot was
   written to a job after it was marked ``MISSED_WINDOW``.

The audit reads only.  It never writes market data and never repairs a record.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.models.event_playbook import time_anchor_for_family  # noqa: E402

DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_JSON = ROOT / "reports" / "price_window_audit.json"
DEFAULT_MARKDOWN = ROOT / "reports" / "price_window_audit.md"

# Mirrors observe_live_event_markets.HORIZON_WINDOWS.  Kept as plain seconds so
# the audit stays readable and does not import the collector's runtime.
WINDOW_GRACE_SECONDS = {"t_plus_5m": 120, "t_plus_30m": 300, "t_plus_1d": 1800}
OBSERVED_ANCHOR = "first_capture"
TERMINAL_STATUSES = frozenset({"COMPLETED", "MISSED_WINDOW"})


def _as_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * fraction))))
    return round(ordered[index], 1)


def audit_anchor_declaration(connection: sqlite3.Connection) -> dict[str, Any]:
    """Compare the anchor the observer used against the anchor each family declares."""

    rows = connection.execute(
        """SELECT e.event_family AS event_family, COUNT(DISTINCT j.event_id) AS events
           FROM market_jobs j JOIN canonical_events e ON e.event_id=j.event_id
           GROUP BY e.event_family"""
    ).fetchall()

    families: list[dict[str, Any]] = []
    undeclared = 0
    mismatched = 0
    for row in rows:
        family = str(row["event_family"] or "")
        declared = time_anchor_for_family(family)
        matches = declared == OBSERVED_ANCHOR if declared else None
        if declared is None:
            undeclared += 1
        elif not matches:
            mismatched += 1
        families.append(
            {
                "event_family": family,
                "events_with_windows": int(row["events"]),
                "declared_anchor": declared,
                "observed_anchor": OBSERVED_ANCHOR,
                "anchor_matches_declaration": matches,
            }
        )

    return {
        "observed_anchor": OBSERVED_ANCHOR,
        "observed_anchor_is_degraded": True,
        "families": families,
        "families_without_declared_anchor": undeclared,
        "families_with_anchor_mismatch": mismatched,
        "interpretation": (
            "The observer anchors every window on the first successful quote capture. "
            "Any family whose declared anchor differs is measured from a later, "
            "latency-dependent zero point, so its window labels overstate their precision."
        ),
    }


def audit_fulfilment(connection: sqlite3.Connection) -> dict[str, Any]:
    """Scheduled versus captured versus missed, per window, with an honest denominator."""

    rows = connection.execute(
        "SELECT observation_window, status, COUNT(*) AS n FROM market_jobs GROUP BY observation_window, status"
    ).fetchall()

    per_window: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        per_window[str(row["observation_window"])][str(row["status"])] += int(row["n"])

    windows: list[dict[str, Any]] = []
    total_scheduled = 0
    total_completed = 0
    for window in sorted(per_window):
        counts = per_window[window]
        scheduled = sum(counts.values())
        completed = counts.get("COMPLETED", 0)
        total_scheduled += scheduled
        total_completed += completed
        windows.append(
            {
                "observation_window": window,
                "scheduled": scheduled,
                "completed": completed,
                "missed": counts.get("MISSED_WINDOW", 0),
                "still_open": counts.get("PENDING", 0) + counts.get("RETRY", 0),
                "fulfilment_pct": round(100.0 * completed / scheduled, 2) if scheduled else None,
            }
        )

    return {
        "windows": windows,
        "scheduled_total": total_scheduled,
        "completed_total": total_completed,
        "fulfilment_pct": round(100.0 * total_completed / total_scheduled, 2) if total_scheduled else None,
        "denominator_note": "Scheduled counts every job row, including ones still open. Open jobs are not removed from the denominator to flatter the rate.",
    }


def audit_capture_lateness(connection: sqlite3.Connection) -> dict[str, Any]:
    """Distribution of capture lag, and captures that landed outside their grace period."""

    rows = connection.execute(
        """SELECT j.observation_window AS observation_window, j.scheduled_at AS scheduled_at,
                  MIN(s.captured_at) AS captured_at
           FROM market_jobs j JOIN market_snapshots s ON s.market_job_id=j.market_job_id
           WHERE j.status='COMPLETED'
           GROUP BY j.market_job_id"""
    ).fetchall()

    lags: dict[str, list[float]] = defaultdict(list)
    outside_grace: Counter[str] = Counter()
    for row in rows:
        scheduled = _as_utc(row["scheduled_at"])
        captured = _as_utc(row["captured_at"])
        if scheduled is None or captured is None:
            continue
        window = str(row["observation_window"])
        lag = (captured - scheduled).total_seconds()
        lags[window].append(lag)
        grace = WINDOW_GRACE_SECONDS.get(window)
        if grace is not None and lag > grace:
            outside_grace[window] += 1

    windows = [
        {
            "observation_window": window,
            "captures": len(values),
            "lag_p50_seconds": _percentile(values, 0.50),
            "lag_p95_seconds": _percentile(values, 0.95),
            "lag_max_seconds": round(max(values), 1),
            "grace_seconds": WINDOW_GRACE_SECONDS.get(window),
            "captured_outside_grace": outside_grace.get(window, 0),
        }
        for window, values in sorted(lags.items())
    ]

    return {
        "windows": windows,
        "captured_outside_grace_total": sum(outside_grace.values()),
        "interpretation": (
            "A capture outside its grace period is real data, but it is not the window it is "
            "labelled as. These must be surfaced with their true lag rather than shown as on-time."
        ),
    }


def audit_no_backfill(connection: sqlite3.Connection) -> dict[str, Any]:
    """Prove that a missed window was never later filled with a quote obtained afterwards."""

    violations = connection.execute(
        """SELECT j.market_job_id AS market_job_id, j.event_id AS event_id,
                  j.observation_window AS observation_window,
                  j.completed_at AS marked_missed_at, s.captured_at AS captured_at
           FROM market_jobs j JOIN market_snapshots s ON s.market_job_id=j.market_job_id
           WHERE j.status='MISSED_WINDOW'
           ORDER BY s.captured_at"""
    ).fetchall()

    missed_total = int(
        connection.execute("SELECT COUNT(*) FROM market_jobs WHERE status='MISSED_WINDOW'").fetchone()[0]
    )
    return {
        "missed_windows": missed_total,
        "missed_windows_carrying_snapshots": len(violations),
        "backfill_violations": [
            {
                "market_job_id": str(row["market_job_id"]),
                "event_id": str(row["event_id"]),
                "observation_window": str(row["observation_window"]),
                "marked_missed_at": str(row["marked_missed_at"] or ""),
                "captured_at": str(row["captured_at"] or ""),
            }
            for row in violations[:50]
        ],
        "no_backfill_holds": not violations,
        "claim": "A missed window stays missed. No quote obtained after the fact is substituted for it.",
    }


def audit_leakage_isolation(connection: sqlite3.Connection) -> dict[str, Any]:
    """Confirm post-event market metrics never became a model feature or a ranking input."""

    row = connection.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN allowed_as_model_feature!=0 THEN 1 ELSE 0 END) AS as_feature,
                  SUM(CASE WHEN allowed_for_discovery_rank!=0 THEN 1 ELSE 0 END) AS as_rank,
                  SUM(CASE WHEN metric_scope!='post_event_audit_only' THEN 1 ELSE 0 END) AS wrong_scope
           FROM event_market_metrics"""
    ).fetchone()
    total = int(row["total"] or 0)
    as_feature = int(row["as_feature"] or 0)
    as_rank = int(row["as_rank"] or 0)
    wrong_scope = int(row["wrong_scope"] or 0)
    return {
        "post_event_metrics": total,
        "used_as_model_feature": as_feature,
        "used_for_discovery_rank": as_rank,
        "outside_audit_only_scope": wrong_scope,
        "isolation_holds": as_feature == 0 and as_rank == 0 and wrong_scope == 0,
        "enforcement": "SQLite CHECK constraints on event_market_metrics reject these values at insert time; this audit confirms the stored rows agree.",
    }


def build_report(db_path: Path) -> dict[str, Any]:
    if not db_path.is_file():
        raise FileNotFoundError(f"ledger database not found: {db_path}")
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "database": db_path.name,
            "anchor": audit_anchor_declaration(connection),
            "fulfilment": audit_fulfilment(connection),
            "lateness": audit_capture_lateness(connection),
            "backfill": audit_no_backfill(connection),
            "leakage": audit_leakage_isolation(connection),
        }
    finally:
        connection.close()
    report["status"] = (
        "PASS"
        if report["backfill"]["no_backfill_holds"]
        and report["leakage"]["isolation_holds"]
        and report["anchor"]["families_with_anchor_mismatch"] == 0
        else "ATTENTION"
    )
    return report


def render_markdown(report: dict[str, Any]) -> str:
    anchor = report["anchor"]
    fulfilment = report["fulfilment"]
    lateness = report["lateness"]
    backfill = report["backfill"]
    leakage = report["leakage"]

    lines = [
        "# Price window audit",
        "",
        f"- Generated at: {report['generated_at']}",
        f"- Database: `{report['database']}`",
        f"- Status: **{report['status']}**",
        "",
        "## 1. Anchor correctness",
        "",
        f"Observed anchor: `{anchor['observed_anchor']}` (degraded: {anchor['observed_anchor_is_degraded']})",
        "",
        "| Event family | Events with windows | Declared anchor | Matches |",
        "|---|---|---|---|",
    ]
    for item in anchor["families"]:
        lines.append(
            f"| {item['event_family'] or '(unset)'} | {item['events_with_windows']} | "
            f"{item['declared_anchor'] or '(undeclared)'} | {item['anchor_matches_declaration']} |"
        )
    lines += [
        "",
        f"Families with a mismatch: **{anchor['families_with_anchor_mismatch']}** · "
        f"without a declared anchor: **{anchor['families_without_declared_anchor']}**",
        "",
        "## 2. Window fulfilment",
        "",
        "| Window | Scheduled | Completed | Missed | Still open | Fulfilment |",
        "|---|---|---|---|---|---|",
    ]
    for item in fulfilment["windows"]:
        pct = "n/a" if item["fulfilment_pct"] is None else f"{item['fulfilment_pct']}%"
        lines.append(
            f"| {item['observation_window']} | {item['scheduled']} | {item['completed']} | "
            f"{item['missed']} | {item['still_open']} | {pct} |"
        )
    overall = "n/a" if fulfilment["fulfilment_pct"] is None else f"{fulfilment['fulfilment_pct']}%"
    lines += [
        "",
        f"Overall fulfilment: **{overall}** ({fulfilment['completed_total']}/{fulfilment['scheduled_total']})",
        "",
        "## 3. Capture lateness",
        "",
        "| Window | Captures | p50 (s) | p95 (s) | Max (s) | Grace (s) | Outside grace |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in lateness["windows"]:
        lines.append(
            f"| {item['observation_window']} | {item['captures']} | {item['lag_p50_seconds']} | "
            f"{item['lag_p95_seconds']} | {item['lag_max_seconds']} | {item['grace_seconds']} | "
            f"{item['captured_outside_grace']} |"
        )
    lines += [
        "",
        f"Captured outside grace: **{lateness['captured_outside_grace_total']}**",
        "",
        "## 4. No backfill",
        "",
        f"- Missed windows: {backfill['missed_windows']}",
        f"- Missed windows carrying snapshots: {backfill['missed_windows_carrying_snapshots']}",
        f"- No-backfill guarantee holds: **{backfill['no_backfill_holds']}**",
        "",
        "## 5. Leakage isolation",
        "",
        f"- Post-event metrics: {leakage['post_event_metrics']}",
        f"- Used as model feature: {leakage['used_as_model_feature']}",
        f"- Used for discovery rank: {leakage['used_for_discovery_rank']}",
        f"- Isolation holds: **{leakage['isolation_holds']}**",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MARKDOWN)
    args = parser.parse_args()

    report = build_report(args.db)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.markdown_out.write_text(render_markdown(report), encoding="utf-8")
    print(f"status={report['status']} json={args.json_out} markdown={args.markdown_out}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
