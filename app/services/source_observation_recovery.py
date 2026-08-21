"""Build a read-only recovery inventory for events without citable evidence.

The capture ledger and the evidence ledger intentionally have different
authority.  This module proves what was retained at capture time and proposes
the next retrieval strategy; it never creates evidence, changes an event, or
performs a network request.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.evidence_policy import is_primary_authority_tier


CONTRACT_VERSION = "source-observation-recovery-plan-v1"
BUCKETS = (
    "SEC_OVERSIZE_REFETCH_READY",
    "OFFICIAL_REFETCH_READY",
    "P2_CAPTURE_ONLY",
    "NO_URL_RAW_ONLY",
    "SOURCE_DELETED",
    "NO_CAPTURE",
    "ORPHAN_CAPTURE_REBUILD_DISCOVERY",
)


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _read_only(path: Path) -> sqlite3.Connection:
    resolved = path.resolve(strict=True)
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _text(value: Any, limit: int) -> str | None:
    normalized = " ".join(str(value or "").split())
    if not normalized:
        return None
    return normalized if len(normalized) <= limit else normalized[: limit - 1].rstrip() + "…"


def _known_at(published_at: Any, received_at: Any) -> str | None:
    parsed: list[datetime] = []
    for value in (published_at, received_at):
        try:
            timestamp = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        parsed.append(timestamp.astimezone(timezone.utc))
    return max(parsed).isoformat() if parsed else None


def _capture(row: sqlite3.Row) -> dict[str, Any]:
    raw_json = str(row["raw_json"] or "")
    payload_sha = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
    capture = {
        "observation_id": str(row["observation_id"]),
        "source_id": str(row["source_id"]),
        "source_name": str(row["source_name"]),
        "source_type": str(row["source_type"]),
        "authority_tier": str(row["authority_tier"]),
        "external_id": str(row["external_id"]),
        "title": _text(row["title"], 500),
        "summary": _text(row["summary"], 1200),
        "canonical_url": _text(row["canonical_url"], 2048),
        "source_published_at": row["source_published_at"],
        "local_received_at": row["local_received_at"],
        "known_at": _known_at(row["source_published_at"], row["local_received_at"]),
        "semantic_content_sha256": str(row["content_sha256"] or ""),
        "raw_payload_sha256": payload_sha,
        "latest_revision_no": int(row["latest_revision_no"] or 0),
        "latest_revision_kind": str(row["latest_revision_kind"] or "new"),
        "observation_status": str(row["observation_status"] or "captured"),
        "relation_type": row["relation_type"],
    }
    capture["capture_receipt_sha256"] = sha256_json(capture)
    return capture


def _bucket(captures: list[dict[str, Any]], oversize_observations: set[str]) -> str:
    if not captures:
        return "NO_CAPTURE"
    live = [item for item in captures if item["observation_status"] != "deleted"]
    if not live:
        return "SOURCE_DELETED"
    if any(
        item["observation_id"] in oversize_observations and item.get("canonical_url")
        for item in live
    ):
        return "SEC_OVERSIZE_REFETCH_READY"
    if any(
        is_primary_authority_tier(item["authority_tier"])
        and item.get("canonical_url")
        for item in live
    ):
        return "OFFICIAL_REFETCH_READY"
    if any(item.get("canonical_url") for item in live):
        return "P2_CAPTURE_ONLY"
    return "NO_URL_RAW_ONLY"


def build_source_observation_recovery_plan(ledger_path: Path) -> dict[str, Any]:
    """Return one deterministic, mutually exclusive recovery record per item."""

    with closing(_read_only(ledger_path)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required = {
            "canonical_events",
            "event_evidence",
            "event_observations",
            "raw_observations",
            "source_revisions",
            "sources",
        }
        missing = sorted(required - tables)
        if missing:
            raise ValueError(f"not a compatible event ledger; missing: {','.join(missing)}")

        zero_events = [
            dict(row)
            for row in connection.execute(
                """SELECT ce.event_id,ce.current_version,ce.status,ce.event_family,
                          ce.event_type,ce.event_date,ce.company_name,ce.ticker_at_event
                   FROM canonical_events ce
                   WHERE NOT EXISTS (
                     SELECT 1 FROM event_evidence ee WHERE ee.event_id=ce.event_id
                   )
                   ORDER BY ce.event_id"""
            )
        ]
        event_ids = [row["event_id"] for row in zero_events]
        captures_by_event: dict[str, list[dict[str, Any]]] = {
            str(event_id): [] for event_id in event_ids
        }
        oversize_observations: set[str] = set()
        if "sec_filing_enrichments" in tables:
            oversize_observations = {
                str(row[0])
                for row in connection.execute(
                    """SELECT observation_id FROM sec_filing_enrichments
                       WHERE LOWER(COALESCE(last_error,'')) LIKE '%exceeds safe capture limit%'
                          OR LOWER(COALESCE(last_error,'')) LIKE '%exceeded safe capture limit%'"""
                )
            }

        if event_ids:
            placeholders = ",".join("?" for _ in event_ids)
            rows = connection.execute(
                f"""SELECT eo.event_id,eo.relation_type,
                            r.observation_id,r.source_id,r.external_id,
                            CASE WHEN r.source_id='opennews_free' AND json_valid(sr.raw_json)
                                 THEN COALESCE(
                                   NULLIF(TRIM(json_extract(sr.raw_json,'$.item.published_at')),''),
                                   NULLIF(TRIM(json_extract(sr.raw_json,'$.item.created_at')),''),
                                   NULLIF(TRIM(json_extract(sr.raw_json,'$.item.posted_at')),''),
                                   r.source_published_at)
                                 ELSE r.source_published_at END AS source_published_at,
                            r.local_received_at,
                            COALESCE(sr.title,r.title) AS title,
                            COALESCE(sr.summary,r.summary) AS summary,
                            CASE WHEN r.source_id='opennews_free' AND json_valid(sr.raw_json)
                                 THEN COALESCE(
                                   NULLIF(TRIM(json_extract(sr.raw_json,'$.item.link')),''),
                                   NULLIF(TRIM(json_extract(sr.raw_json,'$.item.url')),''),
                                   r.canonical_url)
                                 ELSE r.canonical_url END AS canonical_url,
                            COALESCE(sr.content_sha256,r.content_sha256) AS content_sha256,
                            COALESCE(sr.raw_json,r.raw_json) AS raw_json,
                            CASE WHEN sr.revision_kind='delete' THEN 'deleted'
                                 ELSE r.observation_status END AS observation_status,
                            COALESCE(sr.revision_no,0) AS latest_revision_no,
                            COALESCE(sr.revision_kind,'new') AS latest_revision_kind,
                            s.name AS source_name,s.source_type,s.authority_tier
                     FROM event_observations eo
                     JOIN raw_observations r ON r.observation_id=eo.observation_id
                     JOIN sources s ON s.source_id=r.source_id
                     LEFT JOIN source_revisions sr
                       ON sr.observation_id=r.observation_id
                      AND sr.revision_no=(SELECT MAX(sr2.revision_no)
                                          FROM source_revisions sr2
                                          WHERE sr2.observation_id=r.observation_id)
                     WHERE eo.event_id IN ({placeholders})
                     ORDER BY eo.event_id,r.observation_id""",
                event_ids,
            )
            for row in rows:
                captures_by_event[str(row["event_id"])].append(_capture(row))

        records: list[dict[str, Any]] = []
        for event in zero_events:
            captures = captures_by_event[str(event["event_id"])]
            bucket = _bucket(captures, oversize_observations)
            records.append(
                {
                    "record_kind": "EVENT_ZERO_EVIDENCE",
                    "event": event,
                    "bucket": bucket,
                    "captures": captures,
                    "captured_source_count": len(captures),
                    "citable_evidence_count": 0,
                    "proposed_action": {
                        "SEC_OVERSIZE_REFETCH_READY": "RETRY_ACCESSION_EXHIBITS_WITH_PER_DOCUMENT_FAILURE_ISOLATION",
                        "OFFICIAL_REFETCH_READY": "REFETCH_OFFICIAL_SOURCE_AND_EXTRACT_EXACT_PASSAGE",
                        "P2_CAPTURE_ONLY": "PRESERVE_CAPTURE_AND_SEARCH_FOR_PRIMARY_SOURCE",
                        "NO_URL_RAW_ONLY": "PRESERVE_RAW_RECEIPT_AND_ROUTE_TO_HUMAN_SEARCH",
                        "SOURCE_DELETED": "PRESERVE_TOMBSTONE_AND_DO_NOT_RECONSTRUCT_FACT",
                        "NO_CAPTURE": "HUMAN_REVIEW_OR_REMOVE_EMPTY_EVENT_PROJECTION",
                    }[bucket],
                    "canonical_mutation_allowed": False,
                }
            )

        orphan_rows = connection.execute(
            """SELECT NULL AS event_id,NULL AS relation_type,
                      r.observation_id,r.source_id,r.external_id,
                      CASE WHEN r.source_id='opennews_free' AND json_valid(sr.raw_json)
                           THEN COALESCE(
                             NULLIF(TRIM(json_extract(sr.raw_json,'$.item.published_at')),''),
                             NULLIF(TRIM(json_extract(sr.raw_json,'$.item.created_at')),''),
                             NULLIF(TRIM(json_extract(sr.raw_json,'$.item.posted_at')),''),
                             r.source_published_at)
                           ELSE r.source_published_at END AS source_published_at,
                      r.local_received_at,COALESCE(sr.title,r.title) AS title,
                      COALESCE(sr.summary,r.summary) AS summary,
                      CASE WHEN r.source_id='opennews_free' AND json_valid(sr.raw_json)
                           THEN COALESCE(
                             NULLIF(TRIM(json_extract(sr.raw_json,'$.item.link')),''),
                             NULLIF(TRIM(json_extract(sr.raw_json,'$.item.url')),''),
                             r.canonical_url)
                           ELSE r.canonical_url END AS canonical_url,
                      COALESCE(sr.content_sha256,r.content_sha256) AS content_sha256,
                      COALESCE(sr.raw_json,r.raw_json) AS raw_json,
                      CASE WHEN sr.revision_kind='delete' THEN 'deleted'
                           ELSE r.observation_status END AS observation_status,
                      COALESCE(sr.revision_no,0) AS latest_revision_no,
                      COALESCE(sr.revision_kind,'new') AS latest_revision_kind,
                      s.name AS source_name,s.source_type,s.authority_tier
               FROM raw_observations r
               JOIN sources s ON s.source_id=r.source_id
               LEFT JOIN source_revisions sr
                 ON sr.observation_id=r.observation_id
                AND sr.revision_no=(SELECT MAX(sr2.revision_no)
                                    FROM source_revisions sr2
                                    WHERE sr2.observation_id=r.observation_id)
               WHERE NOT EXISTS (
                 SELECT 1 FROM event_observations eo WHERE eo.observation_id=r.observation_id
               )
               ORDER BY r.observation_id"""
        ).fetchall()
        for row in orphan_rows:
            records.append(
                {
                    "record_kind": "ORPHAN_CAPTURE",
                    "event": None,
                    "bucket": "ORPHAN_CAPTURE_REBUILD_DISCOVERY",
                    "captures": [_capture(row)],
                    "captured_source_count": 1,
                    "citable_evidence_count": 0,
                    "proposed_action": "REPLAY_CAPTURE_THROUGH_CURRENT_DISCOVERY_ADMISSION",
                    "canonical_mutation_allowed": False,
                }
            )

    bucket_counts = Counter(record["bucket"] for record in records)
    normalized_counts = {bucket: int(bucket_counts.get(bucket, 0)) for bucket in BUCKETS}
    core = {
        "contract_version": CONTRACT_VERSION,
        "zero_evidence_event_count": len(zero_events),
        "orphan_capture_count": len(orphan_rows),
        "source_record_count": len(records),
        "bucket_counts": normalized_counts,
        "records": records,
        "partition_complete": sum(normalized_counts.values()) == len(records),
        "network_requests_performed": 0,
        "canonical_mutations_performed": 0,
    }
    return {
        **core,
        "logical_snapshot_sha256": sha256_json(core),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
