from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any


SUBJECTLESS_SQL = (
    "TRIM(COALESCE(company_name,''))='' "
    "AND TRIM(COALESCE(ticker_at_event,''))=''"
)


@dataclass(frozen=True)
class SubjectlessCleanupPlan:
    event_count: int
    event_ids_sha256: str
    by_status: dict[str, int]
    by_source: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SubjectlessCleanupResult:
    planned: int
    deleted: int
    child_rows_deleted: dict[str, int]
    raw_observations_preserved: int
    foreign_key_violations_added: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _count_map(connection: sqlite3.Connection, column: str) -> dict[str, int]:
    return {
        str(row[0] or "unknown"): int(row[1])
        for row in connection.execute(
            f"SELECT {column},COUNT(*) FROM canonical_events "
            f"WHERE {SUBJECTLESS_SQL} GROUP BY {column} ORDER BY COUNT(*) DESC"
        )
    }


def plan_subjectless_cleanup(connection: sqlite3.Connection) -> SubjectlessCleanupPlan:
    event_ids = [
        str(row[0])
        for row in connection.execute(
            f"SELECT event_id FROM canonical_events WHERE {SUBJECTLESS_SQL} ORDER BY event_id"
        )
    ]
    digest = hashlib.sha256("\n".join(event_ids).encode("utf-8")).hexdigest()
    return SubjectlessCleanupPlan(
        event_count=len(event_ids),
        event_ids_sha256=digest,
        by_status=_count_map(connection, "status"),
        by_source=_count_map(connection, "discovery_source"),
    )


def _delete_where_targeted(
    connection: sqlite3.Connection, table: str, *, column: str = "event_id"
) -> int:
    if not _table_exists(connection, table) or column not in _columns(connection, table):
        return 0
    before = connection.total_changes
    connection.execute(
        f'DELETE FROM "{table}" WHERE "{column}" IN '
        "(SELECT event_id FROM temp.subjectless_event_targets)"
    )
    return connection.total_changes - before


def purge_subjectless_events(connection: sqlite3.Connection) -> SubjectlessCleanupResult:
    """Remove subjectless canonical records while preserving source observations.

    The operation is intentionally scoped to rows where both canonical subject
    fields are empty.  Evidence source bytes remain in raw_observations and
    source_revisions; only event-shaped projections and their dependent jobs
    are removed.
    """

    plan = plan_subjectless_cleanup(connection)
    if not plan.event_count:
        return SubjectlessCleanupResult(0, 0, {}, 0, 0)

    fk_before = {tuple(row) for row in connection.execute("PRAGMA foreign_key_check")}
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DROP TABLE IF EXISTS temp.subjectless_event_targets")
        connection.execute(
            "CREATE TEMP TABLE subjectless_event_targets(event_id TEXT PRIMARY KEY)"
        )
        connection.execute(
            f"INSERT INTO temp.subjectless_event_targets "
            f"SELECT event_id FROM canonical_events WHERE {SUBJECTLESS_SQL}"
        )
        raw_observations_preserved = int(
            connection.execute(
                """SELECT COUNT(DISTINCT observation_id) FROM event_observations
                   WHERE event_id IN (SELECT event_id FROM temp.subjectless_event_targets)"""
            ).fetchone()[0]
        ) if _table_exists(connection, "event_observations") else 0

        deleted: dict[str, int] = {}
        if _table_exists(connection, "alert_outbox"):
            for table in (
                "alert_delivery_attempts",
                "alert_delivery_cleanup",
                "alert_delivery_leases",
            ):
                if not _table_exists(connection, table):
                    continue
                before = connection.total_changes
                connection.execute(
                    f'DELETE FROM "{table}" WHERE outbox_id IN ('
                    "SELECT outbox_id FROM alert_outbox WHERE event_id IN "
                    "(SELECT event_id FROM temp.subjectless_event_targets))"
                )
                deleted[table] = connection.total_changes - before

        if _table_exists(connection, "market_snapshots") and _table_exists(connection, "market_jobs"):
            before = connection.total_changes
            connection.execute(
                """DELETE FROM market_snapshots
                   WHERE event_id IN (SELECT event_id FROM temp.subjectless_event_targets)
                      OR market_job_id IN (
                         SELECT market_job_id FROM market_jobs
                         WHERE event_id IN (SELECT event_id FROM temp.subjectless_event_targets)
                      )"""
            )
            deleted["market_snapshots"] = connection.total_changes - before

        if _table_exists(connection, "event_evidence_relations"):
            before = connection.total_changes
            connection.execute(
                """DELETE FROM event_evidence_relations
                   WHERE event_id IN (SELECT event_id FROM temp.subjectless_event_targets)
                      OR evidence_id IN (
                         SELECT evidence_id FROM event_evidence
                         WHERE event_id IN (SELECT event_id FROM temp.subjectless_event_targets)
                      )"""
            )
            deleted["event_evidence_relations"] = connection.total_changes - before

        if _table_exists(connection, "discovery_leads"):
            connection.execute(
                """UPDATE discovery_leads
                   SET canonical_event_id=NULL,status='EXCLUDED'
                   WHERE canonical_event_id IN (
                     SELECT event_id FROM temp.subjectless_event_targets
                   )"""
            )
        if _table_exists(connection, "event_chains"):
            connection.execute(
                """UPDATE event_chains SET primary_event_id=NULL
                   WHERE primary_event_id IN (
                     SELECT event_id FROM temp.subjectless_event_targets
                   )"""
            )

        priority = {
            "event_evidence_relations": 0,
            "market_snapshots": 0,
            "event_evidence": 1,
            "market_jobs": 1,
            "alert_outbox": 1,
        }
        event_tables = []
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ):
            table = str(row[0])
            if table != "canonical_events" and "event_id" in _columns(connection, table):
                event_tables.append(table)
        for table in sorted(event_tables, key=lambda value: (priority.get(value, 2), value)):
            if table in deleted and priority.get(table, 2) == 0:
                continue
            deleted[table] = deleted.get(table, 0) + _delete_where_targeted(connection, table)

        before = connection.total_changes
        connection.execute(
            "DELETE FROM canonical_events WHERE event_id IN "
            "(SELECT event_id FROM temp.subjectless_event_targets)"
        )
        canonical_deleted = connection.total_changes - before
        if canonical_deleted != plan.event_count:
            raise RuntimeError(
                f"subjectless cleanup CAS mismatch: planned={plan.event_count} deleted={canonical_deleted}"
            )
        remaining = connection.execute(
            f"SELECT COUNT(*) FROM canonical_events WHERE {SUBJECTLESS_SQL}"
        ).fetchone()[0]
        if remaining:
            raise RuntimeError(f"subjectless cleanup incomplete: remaining={remaining}")
        fk_after = {tuple(row) for row in connection.execute("PRAGMA foreign_key_check")}
        added = fk_after - fk_before
        if added:
            raise RuntimeError(f"subjectless cleanup introduced foreign-key violations: {len(added)}")
        connection.commit()
        return SubjectlessCleanupResult(
            planned=plan.event_count,
            deleted=canonical_deleted,
            child_rows_deleted=deleted,
            raw_observations_preserved=raw_observations_preserved,
            foreign_key_violations_added=0,
        )
    except Exception:
        connection.rollback()
        raise
