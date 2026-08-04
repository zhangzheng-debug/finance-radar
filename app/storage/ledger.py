from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


EVIDENCE_SNAPSHOT_SOURCE_IDS = (
    "bls_key_indicators",
    "cftc_enforcement",
    "ecb_press",
    "ecb_statistical_press",
    "eia_press",
    "fda_medwatch",
    "fdic_press_releases",
    "federal_reserve",
    "federal_reserve_press",
    "ftc_press",
    "nvidia_official_news",
    "sec_current_filings",
    "sec_edgar",
    "sec_litigation_releases",
    "sec_trading_suspensions",
    "us_marad",
    "us_treasury",
)


PUBLIC_EVENT_STATE_CTE = """
WITH ranked_rough_reviews AS (
    SELECT job_id,event_id,payload_json,updated_at,
           ROW_NUMBER() OVER (
               PARTITION BY event_id
               ORDER BY updated_at DESC,job_id DESC
           ) AS rough_rank
    FROM pipeline_jobs
    WHERE status='COMPLETED_AUTHORIZED_ROUGH_REVIEW'
),
event_public AS (
    SELECT canonical.*,
           CASE
             WHEN canonical.status='verified' THEN 'verified'
             WHEN canonical.status='rejected' THEN 'excluded'
             WHEN canonical.status='weak' OR (
               rough.job_id IS NOT NULL
               AND CASE WHEN json_valid(rough.payload_json)
                 THEN COALESCE(
                   json_extract(rough.payload_json,'$.rough_review.outcome'),
                   CASE WHEN UPPER(COALESCE(
                     json_extract(rough.payload_json,'$.rough_review.decision_status'),''
                   ))='INSUFFICIENT' THEN 'ROUGH_INSUFFICIENT' END
                 )
               END='ROUGH_INSUFFICIENT'
             ) THEN 'insufficient'
             WHEN canonical.status='candidate' AND rough.job_id IS NOT NULL THEN 'rough_reviewed'
             ELSE 'pending_verification'
           END AS public_state,
           COALESCE(
             CASE WHEN json_valid(rough.payload_json)
               THEN json_extract(rough.payload_json,'$.rough_review.reviewed_at')
             END,
             rough.updated_at
           ) AS reviewed_at
    FROM canonical_events canonical
    LEFT JOIN ranked_rough_reviews rough
      ON rough.event_id=canonical.event_id AND rough.rough_rank=1
)
""".strip()


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _json(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default if default is not None else value


class LedgerRepository:
    """Read-only product query adapter over the existing Schema 12 ledger."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise FileNotFoundError(f"ledger database not found: {self.path}")
        connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=ro",
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def schema_version(self) -> int:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT MAX(version) AS version FROM event_ledger_schema"
            ).fetchone()
            return int(row["version"] or 0)

    def health(self, *, run_integrity_check: bool = True) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            quick_check = (
                connection.execute("PRAGMA quick_check").fetchone()[0]
                if run_integrity_check
                else "deferred"
            )
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "sources",
                    "raw_observations",
                    "canonical_events",
                    "event_versions",
                    "event_evidence",
                    "event_market_metrics",
                    "pipeline_jobs",
                    "alert_outbox",
                )
            }
            event_status = {
                row["status"]: row["n"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS n FROM canonical_events GROUP BY status"
                )
            }
            audit = {
                "trading_boundary_violations": connection.execute(
                    "SELECT COUNT(*) FROM canonical_events WHERE no_trading != 1"
                ).fetchone()[0],
                "auto_verification_violations": connection.execute(
                    "SELECT COUNT(*) FROM event_evidence WHERE auto_verification_allowed != 0"
                ).fetchone()[0],
                "market_feature_leakage_violations": connection.execute(
                    "SELECT COUNT(*) FROM event_market_metrics WHERE allowed_as_model_feature != 0"
                ).fetchone()[0],
            }
            last_event_update = connection.execute(
                "SELECT MAX(last_updated_at) FROM canonical_events"
            ).fetchone()[0]
            last_new_event_at = connection.execute(
                "SELECT MAX(first_seen_at) FROM canonical_events"
            ).fetchone()[0]
        return {
            "status": "ok"
            if (quick_check == "ok" or not run_integrity_check) and not any(audit.values())
            else "degraded",
            "database": str(self.path),
            "database_bytes": self.path.stat().st_size,
            "schema_version": self.schema_version(),
            "quick_check": quick_check,
            "integrity_check_source": "live_database" if run_integrity_check else "not_run",
            "last_event_update": last_event_update,
            "last_new_event_at": last_new_event_at,
            "counts": counts,
            "event_status": event_status,
            "audit": audit,
        }

    def overview(self, recent_limit: int = 12, *, run_integrity_check: bool = True) -> dict[str, Any]:
        health = self.health(run_integrity_check=run_integrity_check)
        with closing(self.connect()) as connection:
            job_status = {
                row["status"]: row["n"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS n FROM pipeline_jobs GROUP BY status"
                )
            }
            review_queue = connection.execute(
                """SELECT COUNT(DISTINCT j.event_id)
                   FROM pipeline_jobs j
                   JOIN canonical_events e ON e.event_id=j.event_id
                   WHERE e.status IN ('candidate','weak')
                     AND j.status IN (
                         'PENDING_PRIMARY_EVIDENCE',
                         'PENDING_EVIDENCE_REVIEW',
                         'PENDING_HUMAN_REVIEW'
                     )"""
            ).fetchone()[0]
            alert_status = {
                row["status"]: row["n"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS n FROM alert_outbox GROUP BY status"
                )
            }
            public_funnel = self._public_funnel(connection)
        return {
            **health,
            "public_funnel": public_funnel,
            "review_queue": review_queue,
            "rough_reviewed": int(job_status.get("COMPLETED_AUTHORIZED_ROUGH_REVIEW", 0)),
            "job_status": job_status,
            "alert_status": alert_status,
            "recent_events": self.list_events(status="verified", limit=recent_limit)["items"],
            "source_health": self.list_source_health(),
        }

    @staticmethod
    def _rough_review_metadata(payload_json: Any, updated_at: Any) -> dict[str, str | None]:
        payload = _json(payload_json, {})
        rough_review = payload.get("rough_review") if isinstance(payload, dict) else None
        outcome = rough_review.get("outcome") if isinstance(rough_review, dict) else None
        if not outcome and isinstance(rough_review, dict):
            decision_status = str(rough_review.get("decision_status") or "").upper()
            if decision_status == "INSUFFICIENT":
                outcome = "ROUGH_INSUFFICIENT"
        reviewed_at = rough_review.get("reviewed_at") if isinstance(rough_review, dict) else None
        return {
            "outcome": str(outcome or "ROUGH_REVIEWED"),
            "reviewed_at": str(reviewed_at or updated_at) if reviewed_at or updated_at else None,
        }

    @staticmethod
    def _public_state(status: Any, rough_outcome: str | None) -> str:
        normalized = str(status or "candidate").lower()
        if normalized == "verified":
            return "verified"
        if normalized == "rejected":
            return "excluded"
        if rough_outcome == "ROUGH_INSUFFICIENT" or normalized == "weak":
            return "insufficient"
        if normalized == "candidate" and rough_outcome is not None:
            return "rough_reviewed"
        return "pending_verification"

    @staticmethod
    def _public_funnel(connection: sqlite3.Connection) -> dict[str, Any]:
        """Build one exhaustive public disposition for every canonical event.

        Formal canonical outcomes take precedence over rough-review metadata.
        A completed rough review is intentionally not presented as verification.
        """
        events = [
            dict(row)
            for row in connection.execute(
                "SELECT event_id,status FROM canonical_events ORDER BY event_id"
            )
        ]
        rough_by_event: dict[str, dict[str, str | None]] = {}
        for row in connection.execute(
            """SELECT event_id,payload_json,updated_at
               FROM pipeline_jobs
               WHERE status='COMPLETED_AUTHORIZED_ROUGH_REVIEW'
               ORDER BY updated_at DESC,job_id DESC"""
        ):
            event_id = str(row["event_id"])
            if event_id in rough_by_event:
                continue
            rough_by_event[event_id] = LedgerRepository._rough_review_metadata(
                row["payload_json"], row["updated_at"]
            )

        buckets = {
            "verified": 0,
            "excluded": 0,
            "insufficient": 0,
            "pending_verification": 0,
            "rough_reviewed": 0,
        }
        insufficient_breakdown = {
            "rough_review": 0,
            "canonical_weak_without_rough_insufficient": 0,
        }
        for event in events:
            event_id = str(event["event_id"])
            status = str(event.get("status") or "candidate").lower()
            rough = rough_by_event.get(event_id)
            rough_outcome = str(rough["outcome"]) if rough else None
            public_state = LedgerRepository._public_state(status, rough_outcome)
            buckets[public_state] += 1
            if public_state == "insufficient" and rough_outcome == "ROUGH_INSUFFICIENT":
                insufficient_breakdown["rough_review"] += 1
            elif public_state == "insufficient" and status == "weak":
                insufficient_breakdown["canonical_weak_without_rough_insufficient"] += 1

        total = len(events)
        partition_total = sum(buckets.values())
        return {
            "schema_version": 1,
            "total": total,
            **buckets,
            "partition_total": partition_total,
            "partition_complete": partition_total == total,
            "insufficient_breakdown": insufficient_breakdown,
            "definitions": {
                "verified": "formally verified canonical events",
                "excluded": "canonically rejected events",
                "insufficient": (
                    "canonical weak events or events whose latest authorized rough review "
                    "concluded ROUGH_INSUFFICIENT"
                ),
                "pending_verification": "candidate events without a completed rough-review disposition",
                "rough_reviewed": (
                    "candidate events with an authorized rough review completed without an "
                    "insufficient outcome; not formal verification"
                ),
            },
        }

    def list_source_health(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            sources = [dict(row) for row in connection.execute("SELECT * FROM sources ORDER BY authority_tier, name")]
            cursors = [dict(row) for row in connection.execute("SELECT * FROM source_cursors ORDER BY source_id, cursor_type")]
            observation_stats = {
                row["source_id"]: {
                    "count": int(row["n"]),
                    "latest": row["latest"],
                }
                for row in connection.execute(
                    """SELECT source_id,COUNT(*) AS n,MAX(local_received_at) AS latest
                       FROM raw_observations GROUP BY source_id"""
                )
            }
        by_source: dict[str, list[dict[str, Any]]] = {}
        for cursor in cursors:
            by_source.setdefault(cursor["source_id"], []).append(cursor)
        result = []
        for source in sources:
            source_cursors = by_source.get(source["source_id"], [])
            latest = max(source_cursors, key=lambda item: item.get("updated_at") or "", default=None)
            stats = observation_stats.get(source["source_id"], {"count": 0, "latest": None})
            source["observations"] = stats["count"]
            source["cursor_status"] = (
                latest.get("status")
                if latest
                else "STATIC_IMPORTED" if stats["count"] else "REGISTERED_ONLY"
            )
            source["last_polled_at"] = latest.get("last_polled_at") if latest else None
            source["last_success_at"] = latest.get("last_success_at") if latest else stats["latest"]
            source["last_error"] = latest.get("last_error") if latest else None
            source["cursors"] = source_cursors
            result.append(source)
        return result

    def list_events(
        self,
        *,
        status: str | None = None,
        public_state: str | None = None,
        family: str | None = None,
        source: str | None = None,
        query: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        sort: str = "event_date",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        allowed_public_states = {
            "verified",
            "excluded",
            "insufficient",
            "pending_verification",
            "rough_reviewed",
        }
        if public_state and public_state not in allowed_public_states:
            raise ValueError(f"unsupported public_state: {public_state}")
        sort_orders = {
            "event_date": "e.event_date DESC, e.last_updated_at DESC, e.event_id DESC",
            "latest": "e.last_updated_at DESC, e.event_date DESC, e.event_id DESC",
            "subject": (
                "LOWER(COALESCE(e.company_name,e.ticker_at_event,e.event_id)) ASC, "
                "e.event_date DESC, e.event_id ASC"
            ),
        }
        if sort not in sort_orders:
            raise ValueError(f"unsupported sort: {sort}")
        for name, value in (("date_from", date_from), ("date_to", date_to)):
            if value is None:
                continue
            try:
                parsed_date = date.fromisoformat(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be YYYY-MM-DD") from exc
            if parsed_date.isoformat() != value:
                raise ValueError(f"{name} must be YYYY-MM-DD")
        if date_from and date_to and date_from > date_to:
            raise ValueError("date_from must not be after date_to")

        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("e.status=?")
            params.append(status)
        if public_state:
            where.append("e.public_state=?")
            params.append(public_state)
        if family:
            where.append("e.event_family=?")
            params.append(family)
        if source:
            where.append("e.discovery_source=?")
            params.append(source)
        if date_from:
            where.append("e.event_date>=?")
            params.append(date_from)
        if date_to:
            where.append("e.event_date<=?")
            params.append(date_to)
        if query:
            where.append(
                "(LOWER(COALESCE(e.company_name,'') || ' ' || COALESCE(e.ticker_at_event,'') || ' ' || "
                "e.event_type || ' ' || COALESCE(e.event_family,'') || ' ' || "
                "COALESCE(e.discovery_source,'') || ' ' || e.event_id) LIKE ? OR EXISTS ("
                "SELECT 1 FROM event_observations qeo JOIN latest_source_content qr "
                "ON qr.observation_id=qeo.observation_id WHERE qeo.event_id=e.event_id "
                "AND qeo.relation_type!='filtered_aggregated_noise' "
                "AND LOWER(COALESCE(qr.title,'') || ' ' || COALESCE(qr.summary,'')) LIKE ?))"
            )
            params.extend([f"%{query.lower()}%", f"%{query.lower()}%"])
        where_sql = " WHERE " + " AND ".join(where) if where else ""
        paged_query = (
            PUBLIC_EVENT_STATE_CTE
            + f"""
            , paged_events AS (
                SELECT e.*,COUNT(*) OVER () AS _filtered_total
                FROM event_public e
                {where_sql}
                ORDER BY {sort_orders[sort]}
                LIMIT ? OFFSET ?
            )
            SELECT e.*,
                   (SELECT COUNT(*) FROM event_evidence x WHERE x.event_id=e.event_id) AS evidence_count,
                   (SELECT severity_grade FROM event_assessments a WHERE a.event_id=e.event_id ORDER BY a.created_at DESC LIMIT 1) AS severity_grade,
                   (SELECT credibility_tier FROM event_assessments a WHERE a.event_id=e.event_id ORDER BY a.created_at DESC LIMIT 1) AS credibility_tier,
                   (SELECT evidence_passage FROM event_evidence x WHERE x.event_id=e.event_id AND evidence_passage IS NOT NULL ORDER BY passage_score DESC, updated_at DESC LIMIT 1) AS evidence_excerpt,
                   (SELECT r.title FROM event_observations eo
                    JOIN latest_source_content r ON r.observation_id=eo.observation_id
                    WHERE eo.event_id=e.event_id
                      AND eo.relation_type!='filtered_aggregated_noise'
                    ORDER BY r.local_received_at DESC,r.observation_id DESC LIMIT 1) AS source_title,
                   (SELECT r.summary FROM event_observations eo
                    JOIN latest_source_content r ON r.observation_id=eo.observation_id
                    WHERE eo.event_id=e.event_id
                      AND eo.relation_type!='filtered_aggregated_noise'
                    ORDER BY r.local_received_at DESC,r.observation_id DESC LIMIT 1) AS source_summary
            FROM paged_events e
            ORDER BY {sort_orders[sort]}
            """
        )
        with closing(self.connect()) as connection:
            rows = connection.execute(
                paged_query,
                [*params, limit, offset],
            ).fetchall()
            items = [dict(row) for row in rows]
            if items:
                total = int(items[0]["_filtered_total"])
                for item in items:
                    item.pop("_filtered_total", None)
            else:
                total = connection.execute(
                    PUBLIC_EVENT_STATE_CTE
                    + f" SELECT COUNT(*) FROM event_public e {where_sql}",
                    params,
                ).fetchone()[0]
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "public_state": public_state,
            "date_from": date_from,
            "date_to": date_to,
            "sort": sort,
        }

    def event_facets(self) -> dict[str, Any]:
        """Return bounded, live filter suggestions without exposing event content."""
        with closing(self.connect()) as connection:
            families = [
                {"value": row["value"], "count": int(row["n"])}
                for row in connection.execute(
                    """SELECT event_family AS value, COUNT(*) AS n
                       FROM canonical_events
                       WHERE event_family IS NOT NULL AND TRIM(event_family) != ''
                       GROUP BY event_family
                       ORDER BY n DESC, value ASC
                       LIMIT 100"""
                )
            ]
            sources = [
                {"value": row["value"], "count": int(row["n"])}
                for row in connection.execute(
                    """SELECT discovery_source AS value, COUNT(*) AS n
                       FROM canonical_events
                       WHERE discovery_source IS NOT NULL AND TRIM(discovery_source) != ''
                       GROUP BY discovery_source
                       ORDER BY n DESC, value ASC
                       LIMIT 100"""
                )
            ]
        return {
            "families": families,
            "sources": sources,
            "read_only": True,
            "no_trading": True,
        }

    def event_detail(self, event_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            event = _dict(connection.execute("SELECT * FROM canonical_events WHERE event_id=?", (event_id,)).fetchone())
            if event is None:
                return None
            rough_row = connection.execute(
                """SELECT payload_json,updated_at
                   FROM pipeline_jobs
                   WHERE event_id=? AND status='COMPLETED_AUTHORIZED_ROUGH_REVIEW'
                   ORDER BY updated_at DESC,job_id DESC LIMIT 1""",
                (event_id,),
            ).fetchone()
            rough = (
                self._rough_review_metadata(rough_row["payload_json"], rough_row["updated_at"])
                if rough_row is not None
                else None
            )
            event["public_state"] = self._public_state(
                event.get("status"), str(rough["outcome"]) if rough else None
            )
            event["reviewed_at"] = rough.get("reviewed_at") if rough else None
            version = _dict(
                connection.execute(
                    "SELECT * FROM event_versions WHERE event_id=? AND version=?",
                    (event_id, event["current_version"]),
                ).fetchone()
            )
            if version:
                version["facts"] = _json(version.pop("facts_json"), {})
            assessment = _dict(
                connection.execute(
                    "SELECT * FROM event_assessments WHERE event_id=? ORDER BY created_at DESC LIMIT 1",
                    (event_id,),
                ).fetchone()
            )
            assets = [
                dict(row)
                for row in connection.execute(
                    """SELECT i.*, a.asset_type, a.symbol, a.provider_symbol, a.currency,
                              a.venue, a.metadata_json
                       FROM event_asset_impacts i JOIN assets a ON a.asset_id=i.asset_id
                       WHERE i.event_id=? ORDER BY i.impact_score DESC""",
                    (event_id,),
                )
            ]
            for asset in assets:
                asset["reason_codes"] = _json(asset.pop("reason_codes_json"), [])
                asset["metadata"] = _json(asset.pop("metadata_json"), {})
            metrics = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM event_market_metrics WHERE event_id=? ORDER BY metric_name",
                    (event_id,),
                )
            ]
            snapshots = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM market_snapshots WHERE event_id=? ORDER BY captured_at DESC",
                    (event_id,),
                )
            ]
            market_jobs = [
                dict(row)
                for row in connection.execute(
                    """SELECT market_job_id,event_id,asset_id,provider,observation_window,
                              status,scheduled_at,completed_at,attempts,last_error,no_trading
                       FROM market_jobs WHERE event_id=?
                       ORDER BY asset_id,scheduled_at,observation_window""",
                    (event_id,),
                )
            ]
            preferred_source = _dict(
                connection.execute(
                    """SELECT r.title,r.summary,r.source_id,r.source_published_at,
                              r.local_received_at,eo.observation_id,eo.relation_type
                       FROM event_observations eo
                       JOIN latest_source_content r ON r.observation_id=eo.observation_id
                       WHERE eo.event_id=?
                         AND eo.relation_type!='filtered_aggregated_noise'
                       ORDER BY r.local_received_at DESC,r.observation_id DESC LIMIT 1""",
                    (event_id,),
                ).fetchone()
            )
        return {
            "event": event,
            "current_version": version,
            "assessment": assessment,
            "assets": assets,
            "market_metrics": metrics,
            "market_snapshots": snapshots,
            "market_jobs": market_jobs,
            "preferred_source": preferred_source,
        }

    def market_capabilities(self) -> dict[str, Any]:
        """Summarize observed read-only providers without exposing credentials."""
        registry = {
            "binance_public": {
                "name": "Binance Public Spot",
                "role": "PERSISTED_EVENT_OBSERVATION",
                "asset_classes": ["crypto"],
                "access": "PUBLIC_NONE_AUTH",
                "deployment": "SERVER_DIRECT",
                "activity_scope": "EVENT_TRIGGERED_SNAPSHOTS",
            },
            "twelve_data": {
                "name": "Twelve Data",
                "role": "PERSISTED_EVENT_OBSERVATION",
                "asset_classes": ["equity", "etf", "fx", "commodity_proxy"],
                "access": "API_KEY_MARKET_DATA_ONLY",
                "deployment": "SERVER_DIRECT",
                "activity_scope": "EVENT_TRIGGERED_SNAPSHOTS",
            },
            "ibkr_tws_readonly": {
                "name": "IBKR TWS Read-Only",
                "role": "CAPABILITY_PROBE_ONLY",
                "asset_classes": ["equity", "fx", "futures"],
                "access": "LOCAL_TWS_READ_ONLY",
                "deployment": "OPERATOR_DESKTOP",
                "activity_scope": "LOCAL_CAPABILITY_PROBE",
            },
        }
        with closing(self.connect()) as connection:
            job_rows = {
                row["provider"]: dict(row)
                for row in connection.execute(
                    """SELECT provider,COUNT(*) AS jobs,
                              SUM(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END) AS completed,
                              SUM(CASE WHEN status IN ('PENDING','RETRY') THEN 1 ELSE 0 END) AS pending,
                              SUM(CASE WHEN status IN ('RETRY','FAILED') AND last_error IS NOT NULL
                                       THEN 1 ELSE 0 END) AS errors,
                              MAX(completed_at) AS last_completed_at
                       FROM market_jobs GROUP BY provider"""
                )
            }
            snapshot_rows = {
                row["provider"]: dict(row)
                for row in connection.execute(
                    """SELECT provider,COUNT(*) AS snapshots,MAX(captured_at) AS last_snapshot_at
                       FROM market_snapshots GROUP BY provider"""
                )
            }
            latest_errors = {
                row["provider"]: row["last_error"]
                for row in connection.execute(
                    """SELECT j.provider,j.last_error FROM market_jobs j
                       JOIN (
                         SELECT provider,MAX(scheduled_at) AS scheduled_at
                         FROM market_jobs
                         WHERE status IN ('RETRY','FAILED') AND last_error IS NOT NULL
                         GROUP BY provider
                       ) latest ON latest.provider=j.provider AND latest.scheduled_at=j.scheduled_at
                       WHERE j.status IN ('RETRY','FAILED') AND j.last_error IS NOT NULL"""
                )
            }
            window_rows = connection.execute(
                """SELECT provider,observation_window,status,COUNT(*) AS count
                   FROM market_jobs
                   GROUP BY provider,observation_window,status
                   ORDER BY provider,observation_window,status"""
            ).fetchall()

        window_status: dict[str, dict[str, dict[str, int]]] = {}
        for row in window_rows:
            provider_windows = window_status.setdefault(row["provider"], {})
            statuses = provider_windows.setdefault(row["observation_window"], {})
            statuses[row["status"]] = int(row["count"])

        providers = []
        observed_at = datetime.now(timezone.utc)
        for provider_id, definition in registry.items():
            jobs = job_rows.get(provider_id, {})
            snapshots = snapshot_rows.get(provider_id, {})
            completed = int(jobs.get("completed") or 0)
            pending = int(jobs.get("pending") or 0)
            errors = int(jobs.get("errors") or 0)
            if provider_id == "ibkr_tws_readonly":
                status = "LOCAL_PROBE_ONLY"
            elif errors:
                status = "DEGRADED"
            elif completed:
                status = "OBSERVED"
            elif pending:
                status = "PENDING"
            else:
                status = "UNOBSERVED"
            last_snapshot_at = snapshots.get("last_snapshot_at")
            snapshot_age_seconds: int | None = None
            if last_snapshot_at:
                parsed = datetime.fromisoformat(str(last_snapshot_at).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                snapshot_age_seconds = max(0, int((observed_at - parsed.astimezone(timezone.utc)).total_seconds()))
            if provider_id == "ibkr_tws_readonly":
                freshness_status = "NOT_APPLICABLE_LOCAL_PROBE"
            elif snapshot_age_seconds is None:
                freshness_status = "NO_CAPTURE"
            elif snapshot_age_seconds <= 15 * 60:
                freshness_status = "FRESH_CAPTURE"
            elif snapshot_age_seconds <= 24 * 60 * 60:
                freshness_status = "RECENT_EVENT_CAPTURE"
            else:
                freshness_status = "STALE_EVENT_CAPTURE"
            providers.append(
                {
                    "provider_id": provider_id,
                    **definition,
                    "status": status,
                    "jobs": int(jobs.get("jobs") or 0),
                    "completed_jobs": completed,
                    "pending_jobs": pending,
                    "snapshots": int(snapshots.get("snapshots") or 0),
                    "last_snapshot_at": last_snapshot_at,
                    "snapshot_age_seconds": snapshot_age_seconds,
                    "freshness_status": freshness_status,
                    "continuous_feed": False,
                    "last_error": latest_errors.get(provider_id),
                    "observation_windows": window_status.get(provider_id, {}),
                    "read_only": True,
                    "account_data_used": False,
                    "order_endpoints_present": False,
                }
            )
        return {
            "providers": providers,
            "provider_policy": {
                "crypto": "binance_public",
                "non_crypto": "twelve_data",
                "ibkr": "local_capability_probe_only",
            },
            "horizon_policy": {
                "baseline": "first_real_observer_snapshot",
                "windows": ["t_plus_5m", "t_plus_30m", "t_plus_1d"],
                "missed_window_behavior": "record_MISSED_WINDOW_without_latest_quote_substitution",
                "return_metric_scope": "post_event_audit_only",
                "continuous_quote_feed": False,
                "freshness_disclosure": "provider capability and event-triggered snapshot freshness are reported separately",
            },
            "boundary": {
                "read_only": True,
                "no_trading": True,
                "account_data_used": False,
                "post_event_audit_only": True,
                "allowed_as_model_feature": False,
            },
        }

    def evidence_snapshot_eligible_pairs(self) -> set[tuple[str, str]]:
        placeholders = ",".join("?" for _ in EVIDENCE_SNAPSHOT_SOURCE_IDS)
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""SELECT DISTINCT ev.event_id,ev.evidence_id
                    FROM event_evidence ev
                    JOIN raw_observations r ON r.observation_id=ev.observation_id
                    WHERE r.source_id IN ({placeholders})
                      AND ev.evidence_url IS NOT NULL AND TRIM(ev.evidence_url)!=''""",
                EVIDENCE_SNAPSHOT_SOURCE_IDS,
            ).fetchall()
        return {(str(row["event_id"]), str(row["evidence_id"])) for row in rows}

    def evidence_snapshot_eligibility(self) -> dict[str, Any]:
        return {
            "eligible_links": len(self.evidence_snapshot_eligible_pairs()),
            "policy": "registered_official_sources_with_nonempty_evidence_url",
        }

    def event_evidence(self, event_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT ev.*, o.title AS observation_title, o.summary AS observation_summary,
                          o.source_published_at, o.local_received_at, s.source_id, s.name AS source_name,
                          s.authority_tier, s.source_type
                   FROM event_evidence ev
                   JOIN raw_observations o ON o.observation_id=ev.observation_id
                   JOIN sources s ON s.source_id=o.source_id
                   WHERE ev.event_id=?
                   ORDER BY ev.passage_score DESC, ev.updated_at DESC""",
                (event_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def event_timeline(self, event_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            entries: list[dict[str, Any]] = []
            for row in connection.execute(
                "SELECT * FROM event_versions WHERE event_id=? ORDER BY version", (event_id,)
            ):
                item = dict(row)
                item["facts"] = _json(item.pop("facts_json"), {})
                entries.append({"at": item["changed_at"], "kind": "event_version", "payload": item})
            for row in connection.execute(
                """SELECT o.observation_id,o.source_id,o.source_published_at,o.local_received_at,
                          o.title,o.canonical_url,eo.relation_type,eo.linked_at
                   FROM event_observations eo JOIN raw_observations o ON o.observation_id=eo.observation_id
                   WHERE eo.event_id=?""",
                (event_id,),
            ):
                item = dict(row)
                entries.append({"at": item["local_received_at"], "kind": "observation", "payload": item})
            for row in connection.execute(
                "SELECT * FROM event_assessments WHERE event_id=?", (event_id,)
            ):
                item = dict(row)
                entries.append({"at": item["created_at"], "kind": "assessment", "payload": item})
        return sorted(entries, key=lambda item: item["at"] or "")

    def event_trace(self, event_id: str) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            jobs = [dict(row) for row in connection.execute("SELECT * FROM pipeline_jobs WHERE event_id=? ORDER BY created_at", (event_id,))]
            alerts = [dict(row) for row in connection.execute("SELECT * FROM alert_outbox WHERE event_id=? ORDER BY created_at", (event_id,))]
            for item in jobs:
                item["payload"] = _json(item.pop("payload_json"), {})
            for item in alerts:
                item["payload"] = _json(item.pop("payload_json"), {})
        return {"pipeline_jobs": jobs, "alerts": alerts}
