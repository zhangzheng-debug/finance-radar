#!/usr/bin/env python3
"""Apply explicit human-reviewed historical event adjudications idempotently.

This command does not infer a label.  It validates reviewed JSON decisions against
the current research queue and extracted primary-source passages, then upserts the
durable CSV ledger used by the historical research pipeline.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Iterable

from manual_historical_findings import load_manual_findings


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "active_event_adjudication_additions.json"
DEFAULT_QUEUE = ROOT / "data" / "research" / "active_event_research_queue.csv"
DEFAULT_PASSAGES = ROOT / "data" / "research" / "active_event_sec_evidence_passages.csv"
DEFAULT_EXTERNAL_EVIDENCE = ROOT / "config" / "active_event_external_evidence_additions.json"
DEFAULT_MANUAL_FINDINGS = ROOT / "config" / "active_event_manual_findings.json"
DEFAULT_OUTPUT = ROOT / "reports" / "active_event_adjudications.csv"

CSV_FIELDS = [
    "event_candidate_id",
    "stable_id",
    "ticker_at_event",
    "event_date",
    "detected_event_type",
    "evidence_date",
    "evidence_form",
    "evidence_item",
    "evidence_url",
    "evidence_summary",
    "label_status",
    "canonical_event_family",
    "canonical_event_type",
    "R",
    "L",
    "E",
    "C",
    "P",
    "X",
    "score_total",
    "manual_grade",
    "training_role",
    "adjudication_note",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def heuristic_grade(total: int) -> str:
    if total >= 11:
        return "S"
    if total >= 8:
        return "A++"
    if total >= 5:
        return "A"
    if total >= 2:
        return "B"
    return "C"


def validate_scores(row: dict[str, Any]) -> tuple[dict[str, int], int]:
    scores = row.get("scores")
    if not isinstance(scores, dict) or set(scores) != {"R", "L", "E", "C", "P", "X"}:
        raise ValueError(f"{row.get('event_candidate_id', '<missing>')} requires exact R/L/E/C/P/X scores")
    normalized = {key: int(value) for key, value in scores.items()}
    for key in ("R", "L", "E", "C", "P"):
        if not 0 <= normalized[key] <= 3:
            raise ValueError(f"{row['event_candidate_id']} {key} score is out of range")
    if not -3 <= normalized["X"] <= 0:
        raise ValueError(f"{row['event_candidate_id']} X score is out of range")
    total = sum(normalized.values())
    status = row.get("label_status")
    grade = row.get("manual_grade")
    if status == "verified" and grade not in {"S", "A++", "A", "B", "C"}:
        raise ValueError(
            f"{row['event_candidate_id']} verified decision requires a valid manual grade"
        )
    # D:/short treats the score-derived grade as review priority only.  A reviewed
    # manual grade may deliberately conflict with it; that conflict is a valuable
    # boundary case and must never be auto-resolved by this importer.
    if status == "rejected" and (grade != "rejected" or total != 0):
        raise ValueError(
            f"{row['event_candidate_id']} rejected control requires grade rejected and score total 0"
        )
    return normalized, total


def require_text(row: dict[str, Any], field: str) -> str:
    value = str(row.get(field, "")).strip()
    if not value:
        raise ValueError(f"{row.get('event_candidate_id', '<missing>')} requires {field}")
    return value


def read_external_evidence(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("External evidence config requires an evidence list")
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in evidence:
        if not isinstance(item, dict):
            raise ValueError("Each external evidence entry must be an object")
        row = {
            field: require_text(item, field)
            for field in (
                "event_candidate_id",
                "evidence_date",
                "evidence_form",
                "evidence_url",
                "evidence_summary",
                "source_name",
            )
        }
        key = (row["event_candidate_id"], row["evidence_url"])
        if key in seen:
            raise ValueError(f"Duplicate external evidence for {key[0]}: {key[1]}")
        seen.add(key)
        normalized.append(row)
    return normalized


def prior_adjudication_context(
    adjudications: Iterable[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Recover immutable candidate identity and accepted evidence across queue rotations.

    The active queue is intentionally replaced as research advances.  A durable
    adjudication must nevertheless remain re-applicable and editable after its
    source row leaves that queue.  Existing adjudications are trusted only for
    their previously validated identity and evidence URL; any new URL still has
    to exist in the current passage corpus or external-evidence registry.
    """
    queue_rows: list[dict[str, str]] = []
    passage_rows: list[dict[str, str]] = []
    for row in adjudications:
        candidate_id = row.get("event_candidate_id", "").strip()
        if not candidate_id:
            continue
        queue_rows.append(
            {
                "event_candidate_id": candidate_id,
                "stable_id": row.get("stable_id", ""),
                "ticker_at_event": row.get("ticker_at_event", ""),
                "event_date": row.get("event_date", ""),
                "event_type": row.get("detected_event_type", ""),
            }
        )
        evidence_url = row.get("evidence_url", "").strip()
        if evidence_url:
            passage_rows.append(
                {
                    "event_candidate_id": candidate_id,
                    "filing_document_url": evidence_url,
                }
            )
    return queue_rows, passage_rows


def build_rows(
    decisions: Iterable[dict[str, Any]],
    queue_rows: Iterable[dict[str, str]],
    passage_rows: Iterable[dict[str, str]],
    external_evidence_rows: Iterable[dict[str, str]] = (),
) -> list[dict[str, str]]:
    queue_by_id = {row["event_candidate_id"]: row for row in queue_rows}
    passage_urls = {
        (row["event_candidate_id"], row["filing_document_url"])
        for row in passage_rows
        if row.get("filing_document_url")
    }
    passage_urls.update(
        (row["event_candidate_id"], row["evidence_url"])
        for row in external_evidence_rows
        if row.get("evidence_url")
    )
    built: list[dict[str, str]] = []
    seen: set[str] = set()
    for decision in decisions:
        candidate_id = require_text(decision, "event_candidate_id")
        if candidate_id in seen:
            raise ValueError(f"Duplicate decision for {candidate_id}")
        seen.add(candidate_id)
        queue = queue_by_id.get(candidate_id)
        if queue is None:
            raise ValueError(f"Unknown queue candidate: {candidate_id}")
        status = decision.get("label_status")
        if status not in {"verified", "rejected"}:
            raise ValueError(f"{candidate_id} label_status must be verified or rejected")
        scores, total = validate_scores(decision)
        evidence_url = require_text(decision, "evidence_url")
        if (candidate_id, evidence_url) not in passage_urls:
            raise ValueError(f"{candidate_id} evidence_url is not an extracted candidate passage")
        row = {
            "event_candidate_id": candidate_id,
            "stable_id": queue["stable_id"],
            "ticker_at_event": queue["ticker_at_event"],
            "event_date": queue["event_date"],
            "detected_event_type": queue["event_type"],
            "evidence_date": require_text(decision, "evidence_date"),
            "evidence_form": require_text(decision, "evidence_form"),
            "evidence_item": str(decision.get("evidence_item", "")).strip(),
            "evidence_url": evidence_url,
            "evidence_summary": require_text(decision, "evidence_summary"),
            "label_status": status,
            "canonical_event_family": require_text(decision, "canonical_event_family"),
            "canonical_event_type": require_text(decision, "canonical_event_type"),
            **{key: str(scores[key]) for key in ("R", "L", "E", "C", "P", "X")},
            "score_total": str(total),
            "manual_grade": require_text(decision, "manual_grade"),
            "training_role": require_text(decision, "training_role"),
            "adjudication_note": require_text(decision, "adjudication_note"),
        }
        built.append(row)
    return built


def upsert_rows(path: Path, additions: Iterable[dict[str, str]]) -> dict[str, int]:
    existing = read_csv(path)
    additions = list(additions)
    by_id = {row["event_candidate_id"]: row for row in additions}
    replaced = sum(1 for row in existing if row["event_candidate_id"] in by_id)
    unchanged = sum(
        1
        for row in existing
        if row["event_candidate_id"] in by_id and row == by_id[row["event_candidate_id"]]
    )
    merged = [row for row in existing if row["event_candidate_id"] not in by_id]
    merged.extend(additions)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(merged)
    temporary.replace(path)
    return {
        "requested": len(additions),
        "inserted": len(additions) - replaced,
        "replaced": replaced - unchanged,
        "unchanged": unchanged,
        "total": len(merged),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--passages", type=Path, default=DEFAULT_PASSAGES)
    parser.add_argument("--external-evidence", type=Path, default=DEFAULT_EXTERNAL_EVIDENCE)
    parser.add_argument("--manual-findings", type=Path, default=DEFAULT_MANUAL_FINDINGS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = json.loads(args.config.read_text(encoding="utf-8"))
    decisions = payload.get("adjudications")
    if not isinstance(decisions, list):
        raise ValueError("Config requires an adjudications list")
    existing = read_csv(args.output)
    prior_queue, prior_passages = prior_adjudication_context(existing)
    rows = build_rows(
        decisions,
        [*prior_queue, *read_csv(args.queue), *load_manual_findings(args.manual_findings)],
        [*prior_passages, *read_csv(args.passages)],
        read_external_evidence(args.external_evidence),
    )
    result = upsert_rows(args.output, rows)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
