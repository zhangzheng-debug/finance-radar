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
ranked_light_followups AS (
    SELECT job_id,event_id,status,payload_json,updated_at,
           ROW_NUMBER() OVER (
               PARTITION BY event_id
               ORDER BY updated_at DESC,job_id DESC
           ) AS followup_rank
    FROM pipeline_jobs
    WHERE job_type='light_verification_followup'
      AND status IN ('PENDING_EVIDENCE_REVIEW','PENDING_HUMAN_REVIEW')
),
event_reader_evidence AS (
    SELECT ev.event_id,
           COUNT(*) AS citable_evidence_count
    FROM event_evidence ev
    JOIN canonical_events ce ON ce.event_id=ev.event_id
    JOIN event_evidence_relations rel
      ON rel.event_id=ev.event_id
     AND rel.evidence_id=ev.evidence_id
     AND rel.event_version=ce.current_version
    JOIN raw_observations ro ON ro.observation_id=ev.observation_id
    JOIN sources src ON src.source_id=ro.source_id
    WHERE TRIM(COALESCE(ev.evidence_url,''))!=''
      AND LENGTH(TRIM(COALESCE(ev.evidence_passage,'')))>=40
      AND ev.evidence_status IN (
          'machine_extracted_unreviewed','candidate_passage',
          'confirmed_primary','accepted_manual_primary_evidence'
      )
      AND rel.relation_status IN ('SCOPED_MATCH','HUMAN_CONFIRMED')
      AND rel.subject_match=1
      AND rel.event_claim_supported=1
      AND rel.date_coherent=1
      AND UPPER(src.authority_tier) GLOB 'P[01]*'
    GROUP BY ev.event_id
),
event_public AS (
    SELECT canonical.*,
           light.status AS light_followup_status,
           light.updated_at AS light_followup_updated_at,
           CASE WHEN json_valid(light.payload_json)
             THEN json_extract(light.payload_json,'$.light_verification_followup.expected_next_action')
           END AS light_followup_next_action,
           CASE
             WHEN canonical.status='rejected' THEN 'excluded'
             WHEN light.job_id IS NOT NULL AND canonical.status!='weak' THEN 'pending_verification'
             WHEN canonical.status='verified' THEN 'verified'
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
           ) AS reviewed_at,
           COALESCE(reader_evidence.citable_evidence_count,0) AS citable_evidence_count,
           CASE WHEN json_valid(current_version.facts_json)
             THEN COALESCE(
               NULLIF(TRIM(json_extract(current_version.facts_json,'$.public_fact_summary')),''),
               NULLIF(TRIM(json_extract(current_version.facts_json,'$.fact_summary')),''),
               NULLIF(TRIM(json_extract(current_version.facts_json,'$.evidence_summary')),'')
              )
            END AS public_fact_summary,
            CASE WHEN json_valid(current_version.facts_json)
              THEN json_extract(current_version.facts_json,'$.claim_subject')
            END AS claim_subject,
            CASE WHEN json_valid(current_version.facts_json)
              THEN json_extract(current_version.facts_json,'$.claim_action')
            END AS claim_action,
            CASE WHEN json_valid(current_version.facts_json)
              THEN json_extract(current_version.facts_json,'$.claim_stage')
            END AS claim_stage,
            CASE WHEN json_valid(current_version.facts_json)
              THEN json_extract(current_version.facts_json,'$.known_at')
            END AS known_at,
           CASE WHEN COALESCE(
             NULLIF(TRIM(canonical.company_name),''),
             NULLIF(TRIM(canonical.ticker_at_event),''),
             ''
           )!=''
             THEN 1 ELSE 0
           END AS reader_has_subject,
           CASE WHEN json_valid(current_version.facts_json) AND LENGTH(COALESCE(
             NULLIF(TRIM(json_extract(current_version.facts_json,'$.public_fact_summary')),''),
             NULLIF(TRIM(json_extract(current_version.facts_json,'$.fact_summary')),''),
             NULLIF(TRIM(json_extract(current_version.facts_json,'$.evidence_summary')),''),
              ''
            ))>=20
              AND LENGTH(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.claim_subject'),''
              )))>=2
              AND LENGTH(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.claim_action'),''
              )))>=3
              AND UPPER(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.claim_stage'),''
              ))) IN ('PROPOSED','FILED','DISCLOSED','EFFECTIVE','ONGOING','COMPLETED')
              AND LENGTH(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.known_at'),''
              )))>=20
              THEN 1 ELSE 0 END AS reader_has_fact_summary,
           CASE WHEN
             COALESCE(
               NULLIF(TRIM(canonical.company_name),''),
               NULLIF(TRIM(canonical.ticker_at_event),''),
               ''
             )!=''
             AND COALESCE(reader_evidence.citable_evidence_count,0)>0
             AND json_valid(current_version.facts_json)
              AND LENGTH(COALESCE(
               NULLIF(TRIM(json_extract(current_version.facts_json,'$.public_fact_summary')),''),
               NULLIF(TRIM(json_extract(current_version.facts_json,'$.fact_summary')),''),
               NULLIF(TRIM(json_extract(current_version.facts_json,'$.evidence_summary')),''),
               ''
              ))>=20
              AND LENGTH(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.claim_subject'),''
              )))>=2
              AND LENGTH(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.claim_action'),''
              )))>=3
              AND UPPER(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.claim_stage'),''
              ))) IN ('PROPOSED','FILED','DISCLOSED','EFFECTIVE','ONGOING','COMPLETED')
              AND LENGTH(TRIM(COALESCE(
                  json_extract(current_version.facts_json,'$.known_at'),''
              )))>=20
              THEN 1 ELSE 0
           END AS reader_ready
    FROM canonical_events canonical
    LEFT JOIN ranked_rough_reviews rough
      ON rough.event_id=canonical.event_id AND rough.rough_rank=1
    LEFT JOIN ranked_light_followups light
      ON light.event_id=canonical.event_id AND light.followup_rank=1
    LEFT JOIN event_reader_evidence reader_evidence
      ON reader_evidence.event_id=canonical.event_id
    LEFT JOIN event_versions current_version
      ON current_version.event_id=canonical.event_id
     AND current_version.version=canonical.current_version
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
            review_counts = connection.execute(
                PUBLIC_EVENT_STATE_CTE
                + """
                   SELECT COUNT(DISTINCT j.event_id) AS review_queue,
                          COUNT(DISTINCT CASE WHEN e.reader_ready=1 THEN j.event_id END)
                            AS reader_review_queue
                   FROM pipeline_jobs j
                   JOIN event_public e ON e.event_id=j.event_id
                   WHERE e.status IN ('candidate','weak')
                     AND j.status IN (
                         'PENDING_PRIMARY_EVIDENCE',
                         'PENDING_EVIDENCE_REVIEW',
                         'PENDING_HUMAN_REVIEW'
                     )"""
            ).fetchone()
            review_queue = int(review_counts["review_queue"] or 0)
            reader_review_queue = int(review_counts["reader_review_queue"] or 0)
            alert_status = {
                row["status"]: row["n"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS n FROM alert_outbox GROUP BY status"
                )
            }
            public_funnel = self._public_funnel(connection)
            reader_quality = self._reader_quality(connection)
        return {
            **health,
            "public_funnel": public_funnel,
            "reader_funnel": reader_quality["reader_funnel"],
            "reader_quality": reader_quality,
            "review_queue": review_queue,
            "reader_review_queue": reader_review_queue,
            "discovery_backlog": max(0, review_queue - reader_review_queue),
            "rough_reviewed": int(job_status.get("COMPLETED_AUTHORIZED_ROUGH_REVIEW", 0)),
            "job_status": job_status,
            "alert_status": alert_status,
            "recent_events": self.list_events(status="verified", limit=recent_limit)["items"],
            "source_health": self.list_source_health(),
        }

    def product_metrics(
        self,
        *,
        now: datetime | None = None,
        window_days: int = 30,
    ) -> dict[str, Any]:
        """Measure user-value signals without substituting engineering health.

        Every metric declares its sample and source. Metrics that need a human
        sampling programme remain explicitly unavailable instead of receiving a
        proxy score from model output or test counts.
        """
        measured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        window_days = min(365, max(1, int(window_days)))
        cutoff = measured_at.timestamp() - window_days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        now_iso = measured_at.isoformat()

        def percentile(values: list[float], fraction: float) -> float | None:
            if not values:
                return None
            ordered = sorted(values)
            index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.5)))
            return ordered[index]

        with closing(self.connect()) as connection:
            latency_rows = connection.execute(
                """SELECT (julianday(local_received_at)-julianday(source_published_at))*86400.0 AS seconds
                   FROM raw_observations
                   WHERE source_published_at IS NOT NULL
                     AND TRIM(source_published_at)!=''
                     AND local_received_at>=?
                     AND julianday(local_received_at)>=julianday(source_published_at)""",
                (cutoff_iso,),
            ).fetchall()
            latencies = [float(row["seconds"]) for row in latency_rows if row["seconds"] is not None]
            linked_observations = int(
                connection.execute("SELECT COUNT(*) FROM event_observations").fetchone()[0]
            )
            linked_events = int(
                connection.execute("SELECT COUNT(DISTINCT event_id) FROM event_observations").fetchone()[0]
            )
            total_events = int(connection.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0])
            cited_events = int(
                connection.execute(
                    """SELECT COUNT(*) FROM canonical_events e
                       WHERE EXISTS (
                         SELECT 1 FROM event_evidence ev
                         WHERE ev.event_id=e.event_id
                           AND ev.evidence_url IS NOT NULL AND TRIM(ev.evidence_url)!=''
                           AND ev.evidence_passage IS NOT NULL AND TRIM(ev.evidence_passage)!=''
                       )"""
                ).fetchone()[0]
            )
            closed_events = int(
                connection.execute(
                    "SELECT COUNT(*) FROM canonical_events WHERE status IN ('verified','rejected')"
                ).fetchone()[0]
            )
            conflict_events = int(
                connection.execute(
                    """SELECT COUNT(DISTINCT event_id) FROM event_evidence
                       WHERE LOWER(evidence_status) LIKE '%conflict%'
                          OR LOWER(evidence_status) LIKE '%disput%'"""
                ).fetchone()[0]
            )
            queue_rows = connection.execute(
                """SELECT (julianday(?)-julianday(MIN(j.created_at)))*86400.0 AS seconds
                   FROM pipeline_jobs j
                   JOIN canonical_events e ON e.event_id=j.event_id
                   WHERE e.status IN ('candidate','weak')
                     AND j.status IN (
                       'PENDING_PRIMARY_EVIDENCE',
                       'PENDING_EVIDENCE_REVIEW',
                       'PENDING_HUMAN_REVIEW'
                     )
                   GROUP BY j.event_id""",
                (now_iso,),
            ).fetchall()
            queue_ages = [max(0.0, float(row["seconds"])) for row in queue_rows if row["seconds"] is not None]
            trust_violations = int(
                connection.execute(
                    """SELECT
                         (SELECT COUNT(*) FROM canonical_events WHERE no_trading!=1) +
                         (SELECT COUNT(*) FROM event_evidence WHERE auto_verification_allowed!=0) +
                         (SELECT COUNT(*) FROM event_market_metrics WHERE allowed_as_model_feature!=0)"""
                ).fetchone()[0]
            )

        def measured(
            metric_id: str,
            value: float,
            unit: str,
            sample_size: int,
            source: str,
        ) -> dict[str, Any]:
            return {
                "id": metric_id,
                "status": "MEASURED",
                "value": round(value, 2),
                "unit": unit,
                "sample_size": sample_size,
                "source": source,
            }

        def unavailable(metric_id: str, reason: str, source: str) -> dict[str, Any]:
            return {
                "id": metric_id,
                "status": "UNAVAILABLE",
                "value": None,
                "unit": None,
                "sample_size": 0,
                "source": source,
                "reason": reason,
            }

        metrics: list[dict[str, Any]] = []
        p50 = percentile(latencies, 0.50)
        p95 = percentile(latencies, 0.95)
        metrics.append(
            measured("capture_latency_p50", p50, "seconds", len(latencies), "raw_observations")
            if p50 is not None
            else unavailable("capture_latency_p50", "no comparable source and receipt timestamps", "raw_observations")
        )
        metrics.append(
            measured("capture_latency_p95", p95, "seconds", len(latencies), "raw_observations")
            if p95 is not None
            else unavailable("capture_latency_p95", "no comparable source and receipt timestamps", "raw_observations")
        )
        metrics.append(
            measured(
                "duplicate_compression_rate",
                100.0 * max(0, linked_observations - linked_events) / linked_observations,
                "percent",
                linked_observations,
                "event_observations",
            )
            if linked_observations
            else unavailable("duplicate_compression_rate", "no linked observations", "event_observations")
        )
        metrics.append(
            measured(
                "citable_evidence_coverage",
                100.0 * cited_events / total_events,
                "percent",
                total_events,
                "canonical_events+event_evidence",
            )
            if total_events
            else unavailable("citable_evidence_coverage", "no canonical events", "canonical_events+event_evidence")
        )
        metrics.append(
            measured(
                "evidence_closure_rate",
                100.0 * closed_events / total_events,
                "percent",
                total_events,
                "canonical_events",
            )
            if total_events
            else unavailable("evidence_closure_rate", "no canonical events", "canonical_events")
        )
        metrics.append(
            measured(
                "evidence_conflict_rate",
                100.0 * conflict_events / total_events,
                "percent",
                total_events,
                "canonical_events+event_evidence",
            )
            if total_events
            else unavailable("evidence_conflict_rate", "no canonical events", "canonical_events+event_evidence")
        )
        queue_p95 = percentile(queue_ages, 0.95)
        metrics.append(
            measured("review_queue_age_p95", queue_p95, "seconds", len(queue_ages), "pipeline_jobs")
            if queue_p95 is not None
            else unavailable("review_queue_age_p95", "no open review jobs", "pipeline_jobs")
        )
        metrics.append(measured("boundary_violations", float(trust_violations), "count", total_events, "ledger_constraints"))
        metrics.append(
            unavailable(
                "formal_conclusion_accuracy",
                "requires an independent human sample; model output and test counts are not substitutes",
                "human_quality_sample",
            )
        )
        metrics.append(
            unavailable(
                "reader_time_to_source",
                "client-side interaction telemetry is not enabled",
                "browser_interaction_measurement",
            )
        )
        return {
            "measured_at": measured_at.isoformat(),
            "window": {"days": window_days, "starts_at": cutoff_iso},
            "metrics": metrics,
            "engineering_health_is_not_product_quality": True,
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
    def _public_state(
        status: Any,
        rough_outcome: str | None,
        light_followup_status: str | None = None,
    ) -> str:
        normalized = str(status or "candidate").lower()
        if normalized == "rejected":
            return "excluded"
        if light_followup_status in {"PENDING_EVIDENCE_REVIEW", "PENDING_HUMAN_REVIEW"}:
            # A known weak record remains honestly insufficient.  Any other
            # active follow-up means a previous formal-looking disposition is
            # being reconciled and cannot be presented as settled verification.
            return "insufficient" if normalized == "weak" else "pending_verification"
        if normalized == "verified":
            return "verified"
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
        light_followup_by_event: dict[str, str] = {}
        for row in connection.execute(
            """SELECT event_id,status
               FROM (
                   SELECT event_id,status,
                          ROW_NUMBER() OVER (
                              PARTITION BY event_id ORDER BY updated_at DESC,job_id DESC
                          ) AS followup_rank
                   FROM pipeline_jobs
                   WHERE job_type='light_verification_followup'
                     AND status IN ('PENDING_EVIDENCE_REVIEW','PENDING_HUMAN_REVIEW')
               )
               WHERE followup_rank=1"""
        ):
            light_followup_by_event[str(row["event_id"])] = str(row["status"])

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
            public_state = LedgerRepository._public_state(
                status,
                rough_outcome,
                light_followup_by_event.get(event_id),
            )
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
            "active_light_followups": len(light_followup_by_event),
            "light_followup_statuses": {
                followup_status: sum(
                    1 for value in light_followup_by_event.values() if value == followup_status
                )
                for followup_status in ("PENDING_EVIDENCE_REVIEW", "PENDING_HUMAN_REVIEW")
            },
            "definitions": {
                "verified": "formally verified canonical events",
                "excluded": "canonically rejected events",
                "insufficient": (
                    "canonical weak events or events whose latest authorized rough review "
                    "concluded ROUGH_INSUFFICIENT"
                ),
                "pending_verification": (
                    "candidate events without a completed rough-review disposition or any event with an "
                    "active evidence/human light-verification follow-up"
                ),
                "rough_reviewed": (
                    "candidate events with an authorized rough review completed without an "
                    "insufficient outcome; not formal verification"
                ),
            },
        }

    @staticmethod
    def _reader_quality(connection: sqlite3.Connection) -> dict[str, Any]:
        """Measure which canonical records are useful in the public reader.

        A discovery candidate remains preserved even when this gate fails.  The
        gate changes only public browsing: it requires a named subject, a
        structured statement of what happened, and a citable source passage.
        """

        rows = connection.execute(
            PUBLIC_EVENT_STATE_CTE
            + """
               SELECT public_state,
                      COUNT(*) AS total,
                      SUM(reader_ready) AS reader_ready,
                      SUM(CASE WHEN reader_has_subject=0 THEN 1 ELSE 0 END)
                        AS missing_subject,
                      SUM(CASE WHEN reader_has_fact_summary=0 THEN 1 ELSE 0 END)
                        AS missing_fact_summary,
                      SUM(CASE WHEN citable_evidence_count=0 THEN 1 ELSE 0 END)
                        AS missing_citable_evidence
               FROM event_public
               GROUP BY public_state"""
        ).fetchall()
        state_counts = {
            "verified": 0,
            "excluded": 0,
            "insufficient": 0,
            "pending_verification": 0,
            "rough_reviewed": 0,
        }
        total = 0
        ready = 0
        missing_subject = 0
        missing_fact_summary = 0
        missing_citable_evidence = 0
        for row in rows:
            state = str(row["public_state"])
            state_ready = int(row["reader_ready"] or 0)
            if state in state_counts:
                state_counts[state] = state_ready
            total += int(row["total"] or 0)
            ready += state_ready
            missing_subject += int(row["missing_subject"] or 0)
            missing_fact_summary += int(row["missing_fact_summary"] or 0)
            missing_citable_evidence += int(row["missing_citable_evidence"] or 0)
        return {
            "schema_version": 1,
            "definition": (
                "named subject + subject-action-stage-known_at fact + current supported P0/P1 passage"
            ),
            "total": total,
            "reader_ready": ready,
            "discovery_only": max(0, total - ready),
            "gap_counts_nonexclusive": {
                "missing_subject": missing_subject,
                "missing_fact_summary": missing_fact_summary,
                "missing_citable_evidence": missing_citable_evidence,
            },
            "reader_funnel": {
                "schema_version": 1,
                "total": ready,
                **state_counts,
                "partition_total": sum(state_counts.values()),
                "partition_complete": sum(state_counts.values()) == ready,
                "definition": "current-version evidence-supported reader subset of the canonical ledger",
            },
            "read_only": True,
            "canonical_mutation": False,
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
        reader_ready: bool | None = None,
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
        if reader_ready is not None:
            where.append("e.reader_ready=?")
            params.append(int(reader_ready))
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
            "reader_ready": reader_ready,
            "sort": sort,
        }

    def event_facets(self, *, reader_ready: bool | None = None) -> dict[str, Any]:
        """Return bounded, live filter suggestions without exposing event content."""
        source_table = "canonical_events" if reader_ready is None else "event_public"
        where = "" if reader_ready is None else " AND reader_ready=?"
        params: tuple[Any, ...] = () if reader_ready is None else (int(reader_ready),)
        with closing(self.connect()) as connection:
            families = [
                {"value": row["value"], "count": int(row["n"])}
                for row in connection.execute(
                    ((PUBLIC_EVENT_STATE_CTE + " ") if reader_ready is not None else "")
                    + f"""SELECT event_family AS value, COUNT(*) AS n
                       FROM {source_table}
                       WHERE event_family IS NOT NULL AND TRIM(event_family) != ''
                       {where}
                       GROUP BY event_family
                       ORDER BY n DESC, value ASC
                       LIMIT 100""",
                    params,
                )
            ]
            sources = [
                {"value": row["value"], "count": int(row["n"])}
                for row in connection.execute(
                    ((PUBLIC_EVENT_STATE_CTE + " ") if reader_ready is not None else "")
                    + f"""SELECT discovery_source AS value, COUNT(*) AS n
                       FROM {source_table}
                       WHERE discovery_source IS NOT NULL AND TRIM(discovery_source) != ''
                       {where}
                       GROUP BY discovery_source
                       ORDER BY n DESC, value ASC
                       LIMIT 100""",
                    params,
                )
            ]
        return {
            "families": families,
            "sources": sources,
            "reader_ready": reader_ready,
            "read_only": True,
            "no_trading": True,
        }

    def event_detail(self, event_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            event = _dict(
                connection.execute(
                    PUBLIC_EVENT_STATE_CTE
                    + " SELECT * FROM event_public WHERE event_id=?",
                    (event_id,),
                ).fetchone()
            )
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
            light_followup_row = connection.execute(
                """SELECT status,payload_json,updated_at
                   FROM pipeline_jobs
                   WHERE event_id=? AND job_type='light_verification_followup'
                     AND status IN ('PENDING_EVIDENCE_REVIEW','PENDING_HUMAN_REVIEW')
                   ORDER BY updated_at DESC,job_id DESC LIMIT 1""",
                (event_id,),
            ).fetchone()
            light_followup: dict[str, Any] | None = None
            if light_followup_row is not None:
                payload = _json(light_followup_row["payload_json"], {})
                details = (
                    payload.get("light_verification_followup")
                    if isinstance(payload, dict)
                    else None
                )
                details = details if isinstance(details, dict) else {}
                light_followup = {
                    "status": str(light_followup_row["status"]),
                    "updated_at": str(light_followup_row["updated_at"]),
                    "last_attempted_at": details.get("last_attempted_at"),
                    "expected_next_action": details.get("expected_next_action"),
                    "gap_reasons": details.get("gap_reasons", []),
                    "legacy_reconciliation": bool(details.get("legacy_reconciliation")),
                    "formal_verification": False,
                    "no_trading": True,
                }
            event["public_state"] = self._public_state(
                event.get("status"),
                str(rough["outcome"]) if rough else None,
                str(light_followup["status"]) if light_followup else None,
            )
            event["reviewed_at"] = rough.get("reviewed_at") if rough else None
            event["light_followup"] = light_followup
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
                    """SELECT j.market_job_id,j.event_id,j.asset_id,j.provider,
                              j.observation_window,j.status,j.scheduled_at,j.completed_at,
                              j.attempts,j.last_error,j.no_trading,
                              anchor.event_version AS anchor_event_version,
                              anchor.declared_anchor_kind,anchor.reaction_anchor_at,
                              anchor.known_at,anchor.timestamp_precision,
                              anchor.anchor_status,anchor.reason_code AS anchor_reason_code,
                              anchor.unsupported_windows_json,
                              link.offset_seconds,link.window_contract_version
                       FROM market_jobs j
                       LEFT JOIN market_job_anchor_links link
                         ON link.market_job_id=j.market_job_id
                       LEFT JOIN market_event_anchors anchor
                         ON anchor.anchor_id=link.anchor_id
                       WHERE j.event_id=?
                       ORDER BY j.asset_id,j.scheduled_at,j.observation_window""",
                    (event_id,),
                )
            ]
            for job in market_jobs:
                job["unsupported_windows"] = _json(
                    job.pop("unsupported_windows_json"), []
                )
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
                "baseline": "version_bound_exact_event_anchor",
                "anchor_contract": "market-anchor-v1",
                "known_at_rule": "max_source_published_at_local_received_at",
                "windows": [
                    "t_plus_5m",
                    "t_plus_30m",
                    "t_plus_2h",
                    "next_close",
                    "t_plus_1d",
                    "t_plus_5d",
                ],
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
                           s.authority_tier, s.source_type,rel.event_version AS relation_event_version,
                           rel.relation_status,rel.subject_match,rel.event_claim_supported,
                           rel.date_coherent,rel.modality,rel.evidence_fingerprint,
                           rel.contract_version AS relation_contract_version
                    FROM event_evidence ev
                    JOIN raw_observations o ON o.observation_id=ev.observation_id
                    JOIN sources s ON s.source_id=o.source_id
                    LEFT JOIN canonical_events ce ON ce.event_id=ev.event_id
                    LEFT JOIN event_evidence_relations rel
                      ON rel.event_id=ev.event_id AND rel.evidence_id=ev.evidence_id
                     AND rel.event_version=ce.current_version
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
