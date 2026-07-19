from __future__ import annotations

import json
from pathlib import Path
from typing import Any


QUEUE_FIELDS = (
    "queue_rank",
    "family_rank",
    "research_queue_id",
    "event_candidate_id",
    "stable_id",
    "ticker_at_event",
    "company_name",
    "exchange",
    "cik",
    "sec_filings_url",
    "sector",
    "industry",
    "event_date",
    "event_family",
    "event_type",
    "detection_rule",
    "detection_value",
    "severity_raw",
    "source_table",
    "priority_score",
    "selection_strategy",
    "provisional_grade_cap",
    "required_evidence",
    "evidence_search_query",
    "selection_status",
    "allowed_use",
)

REQUIRED_FIELDS = (
    "event_candidate_id",
    "stable_id",
    "ticker_at_event",
    "company_name",
    "event_date",
    "event_family",
    "event_type",
    "detection_rule",
    "detection_value",
    "priority_score",
    "provisional_grade_cap",
    "required_evidence",
    "evidence_search_query",
)


def _required_text(item: dict[str, Any], field: str) -> str:
    value = str(item.get(field, "")).strip()
    if not value:
        raise ValueError(
            f"{item.get('event_candidate_id', '<missing>')} manual finding requires {field}"
        )
    return value


def load_manual_findings(path: Path) -> list[dict[str, str]]:
    """Load human-discovered official events as queue-compatible rows.

    These rows are deliberately separate from Sharadar discovery candidates so a
    later terminal fact cannot be backfilled onto an earlier price or corporate-
    action date.  They still require explicit adjudication and registered primary
    evidence before becoming verified.
    """

    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "manual-historical-findings-v1":
        raise ValueError("Manual findings require schema_version manual-historical-findings-v1")
    findings = payload.get("findings")
    if not isinstance(findings, list):
        raise ValueError("Manual findings config requires a findings list")

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(findings, start=1):
        if not isinstance(item, dict):
            raise ValueError("Each manual finding must be an object")
        normalized = {field: _required_text(item, field) for field in REQUIRED_FIELDS}
        candidate_id = normalized["event_candidate_id"]
        if not candidate_id.startswith("MANUAL-"):
            raise ValueError(f"Manual finding id must start with MANUAL-: {candidate_id}")
        if candidate_id in seen:
            raise ValueError(f"Duplicate manual finding: {candidate_id}")
        seen.add(candidate_id)

        cik = str(item.get("cik", "")).strip()
        row = {
            "queue_rank": str(100000 + index),
            "family_rank": str(index),
            "research_queue_id": f"RADAR-{candidate_id}",
            **normalized,
            "exchange": str(item.get("exchange", "")).strip(),
            "cik": cik,
            "sec_filings_url": str(item.get("sec_filings_url", "")).strip()
            or (
                f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
                if cik
                else ""
            ),
            "sector": str(item.get("sector", "")).strip(),
            "industry": str(item.get("industry", "")).strip(),
            "severity_raw": str(item.get("severity_raw", "3")).strip(),
            "source_table": "MANUAL_OFFICIAL_PRIMARY_DISCOVERY",
            "selection_strategy": "official_primary_event_discovery",
            "selection_status": "needs_manual_adjudication",
            "allowed_use": "manual_research_priority_only_no_trading",
        }
        rows.append({field: row.get(field, "") for field in QUEUE_FIELDS})
    return rows
