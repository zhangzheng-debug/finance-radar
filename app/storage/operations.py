from __future__ import annotations

import json
import hashlib
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


OPS_SCHEMA_VERSION = 10
DEMO_MODES = {"LIVE", "RECENT_CAPTURE", "REPLAY"}
FORMAL_MUTATION_KIND_LIGHT_VERIFICATION = "LIGHT_VERIFICATION"
FORMAL_MUTATION_STATES = {
    "PREPARED",
    "LEDGER_COMMITTED",
    "RECOVERED",
    "ABANDONED",
    "RECOVERY_CONFLICT",
}
QWEN_RISK_PUBLICATION_STATE_KEY = "qwen_risk_publication_v1"
QWEN_RISK_PUBLICATION_STATES = {
    "CANDIDATE",
    "SHADOW_ACCEPTED",
    "PUBLIC_APPROVED",
    "REVOKED",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_sha256(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _safe_json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


class OperationsRepository:
    """Mutable operational state kept separate from the immutable research ledger."""

    def __init__(self, path: str | Path, *, initialize: bool = True):
        self.path = Path(path)
        # A protected deployment bridge needs to take a recovery snapshot with
        # candidate code *before* that candidate is allowed to migrate shared
        # state.  Keep the default unchanged for every normal caller, but let
        # that bridge open an already-existing database without creating paths,
        # tables, indexes, or additive columns.
        if initialize:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def initialize(self) -> None:
        with closing(self.connect()) as connection:
            # WAL is a database-level setting, not a per-request setting.  Reissuing
            # ``PRAGMA journal_mode=WAL`` on every short-lived read connection can
            # wait behind an active writer and serially turn a handful of cheap
            # overview queries into a multi-second request.  Establish it once
            # while initializing a repository, and let normal connections remain
            # read-only until their caller explicitly writes.
            current_journal_mode = str(
                connection.execute("PRAGMA journal_mode").fetchone()[0]
            ).lower()
            if current_journal_mode != "wal":
                connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS operations_schema(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runtime_state(
                    key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS replay_runs(
                    run_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, status TEXT NOT NULL,
                    mode TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
                    result_json TEXT NOT NULL, model_version TEXT, error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_replay_case ON replay_runs(case_id, started_at DESC);
                CREATE TABLE IF NOT EXISTS model_runs(
                    run_id TEXT PRIMARY KEY, event_id TEXT, event_version INTEGER,
                    input_sha256 TEXT NOT NULL,
                    model_version TEXT NOT NULL, output_label TEXT NOT NULL,
                    confidence REAL NOT NULL, latency_ms REAL NOT NULL,
                    shadow INTEGER NOT NULL CHECK(shadow IN (0,1)), created_at TEXT NOT NULL,
                    output_json TEXT NOT NULL,
                    idempotency_key TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_model_event ON model_runs(event_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS worker_cycles(
                    cycle_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                    status TEXT NOT NULL, result_json TEXT NOT NULL, error TEXT
                );
                CREATE TABLE IF NOT EXISTS backup_runs(
                    backup_id TEXT PRIMARY KEY, backup_path TEXT NOT NULL, source_bytes INTEGER NOT NULL,
                    backup_bytes INTEGER, quick_check TEXT, restored_count_json TEXT,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, verified_at TEXT, error TEXT,
                    manifest_path TEXT, components_json TEXT, snapshot_kind TEXT NOT NULL DEFAULT 'ledger_only'
                );
                CREATE TABLE IF NOT EXISTS agent_decisions(
                    decision_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, trace_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL, prompt_version TEXT NOT NULL, model_provider TEXT NOT NULL,
                    model_snapshot TEXT NOT NULL, output_json TEXT NOT NULL,
                    guardrails_json TEXT NOT NULL, tool_calls_json TEXT NOT NULL,
                    evidence_ids_json TEXT NOT NULL, latency_ms REAL NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_event ON agent_decisions(event_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS capture_interpretation_runs(
                    interpretation_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    observation_id TEXT NOT NULL,
                    capture_receipt_sha256 TEXT NOT NULL,
                    semantic_content_sha256 TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    contract_version TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    prompt_sha256 TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model_snapshot TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'PENDING','RUNNING','COMPLETED','FAILED','BUDGET_BLOCKED'
                    )),
                    output_json TEXT NOT NULL,
                    guardrails_json TEXT NOT NULL,
                    usage_json TEXT NOT NULL,
                    latency_ms REAL,
                    external_call INTEGER NOT NULL CHECK(external_call IN (0,1)),
                    canonical_mutation_allowed INTEGER NOT NULL DEFAULT 0
                        CHECK(canonical_mutation_allowed=0),
                    no_trading INTEGER NOT NULL DEFAULT 1 CHECK(no_trading=1),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL DEFAULT '',
                    lease_token TEXT,
                    lease_expires_at TEXT,
                    claimed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_capture_interpretation_event
                    ON capture_interpretation_runs(event_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_capture_interpretation_receipt
                    ON capture_interpretation_runs(capture_receipt_sha256, updated_at DESC);
                CREATE TABLE IF NOT EXISTS capture_interpretation_attempts(
                    attempt_id TEXT PRIMARY KEY,
                    interpretation_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('RUNNING','COMPLETED','FAILED')),
                    lease_token TEXT NOT NULL,
                    reserved_cny REAL NOT NULL DEFAULT 0 CHECK(reserved_cny >= 0),
                    usage_json TEXT NOT NULL,
                    error_class TEXT,
                    error TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    FOREIGN KEY(interpretation_id)
                        REFERENCES capture_interpretation_runs(interpretation_id)
                );
                CREATE INDEX IF NOT EXISTS idx_capture_attempt_day
                    ON capture_interpretation_attempts(provider, started_at);
                CREATE INDEX IF NOT EXISTS idx_capture_attempt_job
                    ON capture_interpretation_attempts(interpretation_id, started_at DESC);
                CREATE TABLE IF NOT EXISTS light_verification_runs(
                    run_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    before_version INTEGER NOT NULL,
                    after_version INTEGER,
                    evidence_ids_json TEXT NOT NULL,
                    budget_json TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    before_model_json TEXT NOT NULL,
                    after_model_json TEXT NOT NULL,
                    applied INTEGER NOT NULL CHECK(applied IN (0,1)),
                    created_at TEXT NOT NULL,
                    mutation_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_light_verification_event
                    ON light_verification_runs(event_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_light_verification_batch
                    ON light_verification_runs(batch_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS formal_mutation_audits(
                    mutation_id TEXT PRIMARY KEY,
                    mutation_kind TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    before_version INTEGER NOT NULL,
                    after_version INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'PREPARED','LEDGER_COMMITTED','RECOVERED','ABANDONED','RECOVERY_CONFLICT'
                    )),
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    ledger_committed_at TEXT,
                    reconciled_at TEXT,
                    last_error TEXT,
                    UNIQUE(mutation_kind,event_id,after_version)
                );
                CREATE INDEX IF NOT EXISTS idx_formal_mutation_state
                    ON formal_mutation_audits(state, created_at);
                CREATE INDEX IF NOT EXISTS idx_formal_mutation_event
                    ON formal_mutation_audits(event_id, after_version DESC);
                CREATE TABLE IF NOT EXISTS evidence_objects(
                    object_sha256 TEXT PRIMARY KEY, relative_path TEXT NOT NULL, mime_type TEXT NOT NULL,
                    byte_length INTEGER NOT NULL, source_url TEXT NOT NULL, fetched_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence_object_links(
                    event_id TEXT NOT NULL, evidence_id TEXT NOT NULL, object_sha256 TEXT NOT NULL,
                    linked_at TEXT NOT NULL,
                    object_kind TEXT NOT NULL DEFAULT 'EXACT_EXCERPT',
                    PRIMARY KEY(event_id, evidence_id, object_sha256),
                    FOREIGN KEY(object_sha256) REFERENCES evidence_objects(object_sha256)
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_objects_event
                    ON evidence_object_links(event_id, linked_at DESC);
                CREATE TABLE IF NOT EXISTS human_overrides(
                    override_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, decision_id TEXT,
                    actor TEXT NOT NULL, reason TEXT NOT NULL, before_json TEXT NOT NULL,
                    after_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(decision_id) REFERENCES agent_decisions(decision_id)
                );
                CREATE INDEX IF NOT EXISTS idx_override_event ON human_overrides(event_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS adjudication_samples(
                    sample_id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
                    text_sha256 TEXT NOT NULL, content_json TEXT NOT NULL,
                    source_id TEXT NOT NULL, authority_tier TEXT NOT NULL,
                    entity_group TEXT NOT NULL, event_chain_group TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'OPEN','IN_REVIEW','CONFLICT','READY','FROZEN'
                    )),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    freeze_id TEXT,
                    UNIQUE(event_id,text_sha256)
                );
                CREATE INDEX IF NOT EXISTS idx_adjudication_sample_status
                    ON adjudication_samples(status, created_at);
                CREATE TABLE IF NOT EXISTS adjudication_reviews(
                    review_id TEXT PRIMARY KEY, sample_id TEXT NOT NULL,
                    reviewer_id TEXT NOT NULL,
                    review_role TEXT NOT NULL CHECK(review_role IN ('REVIEWER','ARBITER')),
                    materiality TEXT NOT NULL, polarity TEXT NOT NULL,
                    evidence_state TEXT NOT NULL, rationale TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(sample_id,reviewer_id),
                    FOREIGN KEY(sample_id) REFERENCES adjudication_samples(sample_id)
                );
                CREATE INDEX IF NOT EXISTS idx_adjudication_review_sample
                    ON adjudication_reviews(sample_id, created_at);
                CREATE TABLE IF NOT EXISTS adjudication_freezes(
                    freeze_id TEXT PRIMARY KEY,
                    dataset_sha256 TEXT NOT NULL UNIQUE,
                    sample_ids_sha256 TEXT NOT NULL,
                    sample_count INTEGER NOT NULL CHECK(sample_count > 0),
                    authorization_id TEXT NOT NULL,
                    authorization_sha256 TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('COMMITTED')),
                    created_at TEXT NOT NULL,
                    committed_at TEXT NOT NULL
                );
                """
            )
            link_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(evidence_object_links)")
            }
            if "object_kind" not in link_columns:
                connection.execute(
                    "ALTER TABLE evidence_object_links ADD COLUMN object_kind TEXT NOT NULL DEFAULT 'EXACT_EXCERPT'"
                )
                connection.execute(
                    """UPDATE evidence_object_links
                       SET object_kind='SOURCE_SNAPSHOT'
                       WHERE object_sha256 IN (
                           SELECT object_sha256 FROM evidence_objects
                           WHERE mime_type IN ('text/html','application/pdf','application/json')
                       )"""
                )
            model_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(model_runs)")
            }
            model_event_version_migration_needed = (
                "event_version" not in model_columns
                or connection.execute(
                    "SELECT 1 FROM operations_schema WHERE version=?",
                    (OPS_SCHEMA_VERSION,),
                ).fetchone()
                is None
            )
            if "idempotency_key" not in model_columns:
                connection.execute("ALTER TABLE model_runs ADD COLUMN idempotency_key TEXT")
            if "event_version" not in model_columns:
                connection.execute("ALTER TABLE model_runs ADD COLUMN event_version INTEGER")

            # Schema 9 stored the immutable event version only inside output_json.
            # Materialize that value once during the additive upgrade so current-
            # version lookups can use a bounded indexed read.  Rows without a
            # usable positive version remain NULL and keep their legacy audit
            # payload intact; they cannot safely be projected as current.
            if model_event_version_migration_needed:
                legacy_model_versions: list[tuple[int, str]] = []
                for row in connection.execute(
                    """SELECT run_id,output_json FROM model_runs
                       WHERE event_id IS NOT NULL AND event_version IS NULL"""
                ).fetchall():
                    output = _safe_json(row["output_json"], {})
                    if not isinstance(output, dict):
                        continue
                    try:
                        event_version = int(output.get("event_version") or 0)
                    except (TypeError, ValueError):
                        continue
                    if event_version > 0:
                        legacy_model_versions.append((event_version, str(row["run_id"])))
                if legacy_model_versions:
                    connection.executemany(
                        "UPDATE model_runs SET event_version=? WHERE run_id=?",
                        legacy_model_versions,
                    )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_model_runs_idempotency
                   ON model_runs(idempotency_key) WHERE idempotency_key IS NOT NULL"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_model_event_version_created
                   ON model_runs(event_id,event_version,created_at DESC)"""
            )
            light_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(light_verification_runs)")
            }
            if "mutation_id" not in light_columns:
                connection.execute("ALTER TABLE light_verification_runs ADD COLUMN mutation_id TEXT")
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_light_verification_mutation
                   ON light_verification_runs(mutation_id) WHERE mutation_id IS NOT NULL"""
            )
            backup_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(backup_runs)")
            }
            for column, definition in (
                ("manifest_path", "TEXT"),
                ("components_json", "TEXT"),
                ("snapshot_kind", "TEXT NOT NULL DEFAULT 'ledger_only'"),
            ):
                if column not in backup_columns:
                    connection.execute(f"ALTER TABLE backup_runs ADD COLUMN {column} {definition}")
            capture_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(capture_interpretation_runs)")
            }
            for column, definition in (
                ("attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("available_at", "TEXT NOT NULL DEFAULT ''"),
                ("lease_token", "TEXT"),
                ("lease_expires_at", "TEXT"),
                ("claimed_at", "TEXT"),
            ):
                if column not in capture_columns:
                    connection.execute(
                        f"ALTER TABLE capture_interpretation_runs ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                """UPDATE capture_interpretation_runs
                   SET available_at=created_at WHERE available_at=''"""
            )
            connection.execute("DROP INDEX IF EXISTS idx_capture_interpretation_queue")
            connection.execute(
                """CREATE INDEX idx_capture_interpretation_queue
                   ON capture_interpretation_runs(status, available_at, created_at)"""
            )
            connection.execute(
                "INSERT OR IGNORE INTO operations_schema(version,applied_at) VALUES (?,?)",
                (OPS_SCHEMA_VERSION, utc_now()),
            )
            connection.commit()

    def get_state(self, key: str, default: Any = None) -> Any:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT value_json FROM runtime_state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else default

    def set_state(self, key: str, value: Any) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                """INSERT INTO runtime_state(key,value_json,updated_at) VALUES (?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
                (key, _stable_json(value), utc_now()),
            )
            connection.commit()

    def demo_mode(self, fallback: str = "RECENT_CAPTURE") -> str:
        mode = str(self.get_state("demo_mode", fallback)).upper()
        return mode if mode in DEMO_MODES else fallback

    def set_demo_mode(self, mode: str) -> str:
        mode = mode.upper()
        if mode not in DEMO_MODES:
            raise ValueError(f"unsupported demo mode: {mode}")
        self.set_state("demo_mode", mode)
        return mode

    def create_replay_run(self, case_id: str) -> str:
        run_id = f"replay-{uuid.uuid4().hex}"
        with closing(self.connect()) as connection:
            connection.execute(
                "INSERT INTO replay_runs VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, case_id, "RUNNING", "REPLAY", utc_now(), None, "{}", None, None),
            )
            connection.commit()
        return run_id

    def finish_replay_run(self, run_id: str, result: dict[str, Any], model_version: str | None) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE replay_runs SET status='COMPLETED',finished_at=?,result_json=?,model_version=? WHERE run_id=?",
                (utc_now(), _stable_json(result), model_version, run_id),
            )
            connection.commit()

    def fail_replay_run(self, run_id: str, error: str) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE replay_runs SET status='FAILED',finished_at=?,error=? WHERE run_id=?",
                (utc_now(), error[:2000], run_id),
            )
            connection.commit()

    def reset_replays(self, case_id: str) -> int:
        with closing(self.connect()) as connection:
            cursor = connection.execute("DELETE FROM replay_runs WHERE case_id=?", (case_id,))
            connection.commit()
            return cursor.rowcount

    def replay_runs(self, case_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        sql = "SELECT * FROM replay_runs"
        params: list[Any] = []
        if case_id:
            sql += " WHERE case_id=?"
            params.append(case_id)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(max(1, min(limit, 200)))
        with closing(self.connect()) as connection:
            rows = [dict(row) for row in connection.execute(sql, params)]
        for row in rows:
            row["result"] = json.loads(row.pop("result_json"))
        return rows

    def record_model_run(self, event_id: str | None, result: dict[str, Any]) -> str:
        run_id = f"model-{uuid.uuid4().hex}"
        event_version = self._normalized_model_event_version(result)
        with closing(self.connect()) as connection:
            connection.execute(
                """INSERT INTO model_runs(
                       run_id,event_id,event_version,input_sha256,model_version,output_label,confidence,
                       latency_ms,shadow,created_at,output_json,idempotency_key
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    event_id,
                    event_version,
                    result["input_sha256"],
                    result["model_version"],
                    result["label"],
                    float(result["confidence"]),
                    float(result["latency_ms"]),
                    1,
                    utc_now(),
                    _stable_json(result),
                    None,
                ),
            )
            connection.commit()
        return run_id

    @staticmethod
    def _normalized_model_event_version(result: dict[str, Any]) -> int | None:
        try:
            event_version = int(result.get("event_version") or 0)
        except (TypeError, ValueError):
            return None
        return event_version if event_version > 0 else None

    @staticmethod
    def _model_run_idempotency_key(event_id: str, result: dict[str, Any]) -> str:
        """One audit row per immutable event/model/input decision invocation."""
        return "model-input-" + _stable_sha256(
            {
                "event_id": event_id,
                "event_version": int(result.get("event_version") or 0),
                "input_sha256": str(result["input_sha256"]),
                "model_version": str(result["model_version"]),
                "call_kind": str(result.get("call_kind") or result.get("decision_source") or "unknown"),
            }
        )[:40]

    def record_model_run_once(
        self,
        event_id: str,
        result: dict[str, Any],
    ) -> tuple[str, bool]:
        """Persist one shadow result per event version/input/model combination."""
        idempotency_key = self._model_run_idempotency_key(event_id, result)
        run_id = f"model-{idempotency_key.removeprefix('model-input-')}"
        event_version = self._normalized_model_event_version(result)
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """INSERT OR IGNORE INTO model_runs(
                       run_id,event_id,event_version,input_sha256,model_version,output_label,confidence,
                       latency_ms,shadow,created_at,output_json,idempotency_key
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    event_id,
                    event_version,
                    result["input_sha256"],
                    result["model_version"],
                    result["label"],
                    float(result["confidence"]),
                    float(result["latency_ms"]),
                    1,
                    utc_now(),
                    _stable_json(result),
                    idempotency_key,
                ),
            )
            if cursor.rowcount:
                connection.commit()
                return run_id, True
            row = connection.execute(
                "SELECT run_id FROM model_runs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            connection.commit()
        if row is not None:
            return str(row["run_id"]), False
        # A legacy row without the new idempotency key may exist.  Preserve its
        # old semantics rather than duplicating it during the schema transition.
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT run_id,output_json FROM model_runs
                   WHERE event_id=? AND input_sha256=? AND model_version=?
                   ORDER BY created_at DESC LIMIT 10""",
                (event_id, result["input_sha256"], result["model_version"]),
            ).fetchall()
        event_version = int(event_version or 0)
        for legacy in rows:
            previous = _safe_json(legacy["output_json"], {})
            if int(previous.get("event_version") or 0) == event_version:
                return str(legacy["run_id"]), False
        raise RuntimeError("model run idempotency insert did not return a row")

    def model_runs(self, event_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        sql = "SELECT * FROM model_runs"
        params: list[Any] = []
        if event_id:
            sql += " WHERE event_id=?"
            params.append(event_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 200)))
        with closing(self.connect()) as connection:
            rows = [dict(row) for row in connection.execute(sql, params)]
        for row in rows:
            row["output"] = json.loads(row.pop("output_json"))
        return rows

    def latest_model_runs_for_versions(
        self,
        event_versions: dict[str, int],
    ) -> dict[str, dict[str, Any]]:
        """Return the latest current-version model run for each requested event.

        Public event pages are loaded in batches of up to 200 rows.  Reading one
        model result per item would otherwise add an avoidable N+1 query pattern.
        Version matching is performed before selecting the newest row so a
        recently revised event never inherits a stale shadow assessment.
        """

        requested: dict[str, int] = {}
        for event_id, version in event_versions.items():
            normalized_id = str(event_id or "").strip()
            if not normalized_id:
                continue
            try:
                normalized_version = int(version)
            except (TypeError, ValueError):
                continue
            if normalized_version > 0:
                requested[normalized_id] = normalized_version
        if not requested:
            return {}

        selected: dict[str, dict[str, Any]] = {}
        requested_items = list(requested.items())
        # Stay below SQLite's conservative host-parameter limit while retaining
        # one connection and one bounded read per public page in normal use.
        # The persisted event_version and composite index keep this query off
        # unrelated historical versions instead of decoding every output_json.
        with closing(self.connect()) as connection:
            for start in range(0, len(requested_items), 400):
                chunk = requested_items[start : start + 400]
                requested_values = ",".join("(?,?)" for _ in chunk)
                params: list[Any] = []
                for event_id, event_version in chunk:
                    params.extend((event_id, event_version))
                rows = connection.execute(
                    f"""WITH requested(event_id,event_version) AS (
                            VALUES {requested_values}
                        )
                        SELECT model_runs.*
                        FROM requested
                        JOIN model_runs
                          ON model_runs.run_id = (
                              SELECT candidate.run_id
                              FROM model_runs AS candidate
                              WHERE candidate.event_id=requested.event_id
                                AND candidate.event_version=requested.event_version
                                AND candidate.model_version NOT LIKE 'qwen-risk-%'
                              ORDER BY candidate.created_at DESC,candidate.run_id DESC
                              LIMIT 1
                          )""",
                    params,
                ).fetchall()
                for sqlite_row in rows:
                    row = dict(sqlite_row)
                    event_id = str(row.get("event_id") or "")
                    output = _safe_json(row.pop("output_json", None), {})
                    if not isinstance(output, dict):
                        continue
                    if int(row.get("event_version") or 0) != requested.get(event_id):
                        continue
                    row["output"] = output
                    selected[event_id] = row
        return selected

    def latest_qwen_risk_runs_for_versions(
        self,
        event_versions: dict[str, int],
    ) -> dict[str, dict[str, Any]]:
        """Return current-version Qwen semantic runs without replacing routers.

        Qwen estimates polarity/materiality while the historical risk router
        emits queue routes.  Both are stored in the immutable model audit table,
        but their newest rows are never interchangeable.
        """

        requested: dict[str, int] = {}
        for event_id, version in event_versions.items():
            normalized_id = str(event_id or "").strip()
            try:
                normalized_version = int(version)
            except (TypeError, ValueError):
                continue
            if normalized_id and normalized_version > 0:
                requested[normalized_id] = normalized_version
        if not requested:
            return {}

        selected: dict[str, dict[str, Any]] = {}
        requested_items = list(requested.items())
        with closing(self.connect()) as connection:
            for start in range(0, len(requested_items), 400):
                chunk = requested_items[start : start + 400]
                values = ",".join("(?,?)" for _ in chunk)
                params: list[Any] = []
                for event_id, event_version in chunk:
                    params.extend((event_id, event_version))
                rows = connection.execute(
                    f"""WITH requested(event_id,event_version) AS (
                            VALUES {values}
                        )
                        SELECT model_runs.*
                        FROM requested
                        JOIN model_runs
                          ON model_runs.run_id = (
                              SELECT candidate.run_id
                              FROM model_runs AS candidate
                              WHERE candidate.event_id=requested.event_id
                                AND candidate.event_version=requested.event_version
                                AND candidate.model_version LIKE 'qwen-risk-%'
                              ORDER BY candidate.created_at DESC,candidate.run_id DESC
                              LIMIT 1
                          )""",
                    params,
                ).fetchall()
                for sqlite_row in rows:
                    row = dict(sqlite_row)
                    event_id = str(row.get("event_id") or "")
                    output = _safe_json(row.pop("output_json", None), {})
                    if not isinstance(output, dict):
                        continue
                    if output.get("model_task") != "QWEN_RISK_SEMANTICS":
                        continue
                    if int(row.get("event_version") or 0) != requested.get(event_id):
                        continue
                    row["output"] = output
                    selected[event_id] = row
        return selected

    def qwen_risk_publication(self) -> dict[str, Any]:
        """Return a fail-closed model-publication contract.

        Persisted model rows are always shadow rows.  They become eligible for
        public projection only after a separate, explicit approval receipt pins
        the exact model, adapter, prompt, and contract.  Missing or malformed
        state therefore resolves to ``CANDIDATE`` rather than inheriting an old
        result accidentally.
        """

        default = {
            "state": "CANDIDATE",
            "public_approved": False,
            "model_version": None,
            "adapter_sha256": None,
            "contract_version": None,
            "prompt_version": None,
            "approval_receipt_sha256": None,
            "approved_at": None,
        }
        raw = self.get_state(QWEN_RISK_PUBLICATION_STATE_KEY, {})
        if not isinstance(raw, dict):
            return default
        state = str(raw.get("state") or "CANDIDATE").strip().upper()
        if state not in QWEN_RISK_PUBLICATION_STATES:
            return default
        result = {**default, **{key: raw.get(key) for key in default}}
        result["state"] = state
        result["public_approved"] = False
        if state != "PUBLIC_APPROVED":
            return result
        required_text = (
            "model_version",
            "contract_version",
            "prompt_version",
            "approved_at",
        )
        if any(not str(result.get(key) or "").strip() for key in required_text):
            return default
        for key in ("adapter_sha256", "approval_receipt_sha256"):
            digest = str(result.get(key) or "").strip().casefold()
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                return default
            result[key] = digest
        result["public_approved"] = True
        return result

    def qwen_risk_run_health(self) -> dict[str, Any]:
        """Return aggregate shadow coverage without exposing model outputs."""

        with closing(self.connect()) as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS runs,COUNT(DISTINCT event_id) AS events,
                          SUM(CASE WHEN shadow=1 THEN 1 ELSE 0 END) AS shadow_runs,
                          MAX(created_at) AS latest_at
                   FROM model_runs WHERE model_version LIKE 'qwen-risk-%'"""
            ).fetchone()
        return {
            "runs": int(row["runs"] or 0),
            "events": int(row["events"] or 0),
            "shadow_runs": int(row["shadow_runs"] or 0),
            "latest_at": row["latest_at"],
        }

    @staticmethod
    def _capture_interpretation_idempotency_key(
        event_id: str,
        observation_id: str,
        input_payload: dict[str, Any],
        *,
        contract_version: str,
        prompt_version: str,
        prompt_sha256: str,
        provider: str,
        model_snapshot: str,
    ) -> str:
        return "capture-int-" + _stable_sha256(
            {
                "event_id": event_id,
                "observation_id": observation_id,
                "capture_receipt_sha256": input_payload["capture_receipt_sha256"],
                "semantic_content_sha256": input_payload["semantic_content_sha256"],
                "input_sha256": input_payload["input_sha256"],
                "contract_version": contract_version,
                "prompt_version": prompt_version,
                "prompt_sha256": prompt_sha256,
                "provider": provider,
                "model_snapshot": model_snapshot,
            }
        )[:40]

    def enqueue_capture_interpretation(
        self,
        event_id: str,
        observation_id: str,
        input_payload: dict[str, Any],
        *,
        contract_version: str,
        prompt_version: str,
        prompt_sha256: str,
        provider: str,
        model_snapshot: str,
        external_call: bool,
    ) -> tuple[str, bool]:
        """Create one bounded interpretation job per immutable capture/config."""

        idempotency_key = self._capture_interpretation_idempotency_key(
            event_id,
            observation_id,
            input_payload,
            contract_version=contract_version,
            prompt_version=prompt_version,
            prompt_sha256=prompt_sha256,
            provider=provider,
            model_snapshot=model_snapshot,
        )
        interpretation_id = idempotency_key
        now = utc_now()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """INSERT OR IGNORE INTO capture_interpretation_runs(
                       interpretation_id,event_id,observation_id,capture_receipt_sha256,
                       semantic_content_sha256,input_sha256,contract_version,prompt_version,
                       prompt_sha256,provider,model_snapshot,status,output_json,guardrails_json,
                       usage_json,latency_ms,external_call,canonical_mutation_allowed,no_trading,
                       idempotency_key,created_at,updated_at,error
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'PENDING','{}','{}','{}',NULL,?,0,1,?,?,?,NULL)""",
                (
                    interpretation_id,
                    event_id,
                    observation_id,
                    str(input_payload["capture_receipt_sha256"]),
                    str(input_payload["semantic_content_sha256"]),
                    str(input_payload["input_sha256"]),
                    contract_version,
                    prompt_version,
                    prompt_sha256,
                    provider,
                    model_snapshot,
                    int(external_call),
                    idempotency_key,
                    now,
                    now,
                ),
            )
            connection.commit()
        return interpretation_id, bool(cursor.rowcount)

    def complete_capture_interpretation(
        self,
        interpretation_id: str,
        output: dict[str, Any],
        *,
        guardrails: dict[str, Any],
        usage: dict[str, Any] | None = None,
        latency_ms: float = 0.0,
    ) -> None:
        """Persist advisory output without any canonical mutation capability."""

        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT status,capture_receipt_sha256,contract_version,prompt_sha256
                   FROM capture_interpretation_runs WHERE interpretation_id=?""",
                (interpretation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"capture interpretation not found: {interpretation_id}")
            if str(output.get("capture_receipt_sha256") or "") != str(
                row["capture_receipt_sha256"]
            ):
                raise ValueError("capture interpretation receipt changed before completion")
            if str(output.get("contract_version") or "") != str(row["contract_version"]):
                raise ValueError("capture interpretation contract changed before completion")
            if str(output.get("prompt_sha256") or "") != str(row["prompt_sha256"]):
                raise ValueError("capture interpretation prompt changed before completion")
            persisted_output = dict(output)
            persisted_output["persisted"] = True
            connection.execute(
                """UPDATE capture_interpretation_runs
                   SET status='COMPLETED',output_json=?,guardrails_json=?,usage_json=?,
                       latency_ms=?,updated_at=?,error=NULL
                   WHERE interpretation_id=?""",
                (
                    _stable_json(persisted_output),
                    _stable_json(guardrails),
                    _stable_json(usage or {}),
                    float(latency_ms),
                    utc_now(),
                    interpretation_id,
                ),
            )
            connection.commit()

    def fail_capture_interpretation(self, interpretation_id: str, error: str) -> None:
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                """UPDATE capture_interpretation_runs
                   SET status='FAILED',updated_at=?,error=? WHERE interpretation_id=?""",
                (utc_now(), str(error)[:2000], interpretation_id),
            )
            if not cursor.rowcount:
                raise KeyError(f"capture interpretation not found: {interpretation_id}")
            connection.commit()

    @staticmethod
    def _capture_attempt_usage_rows(rows: list[sqlite3.Row]) -> dict[str, Any]:
        estimated_cny = 0.0
        reserved_cny = 0.0
        by_status: dict[str, int] = {}
        for row in rows:
            status = str(row["status"])
            by_status[status] = by_status.get(status, 0) + 1
            reservation = max(0.0, float(row["reserved_cny"] or 0.0))
            usage = _safe_json(row["usage_json"], {})
            actual = max(0.0, float(usage.get("estimated_cny") or 0.0))
            estimated_cny += actual
            # A provider may bill a request even when transport or contract
            # validation fails before usage counters are returned. Charge its
            # reservation until an actual estimate is available.
            reserved_cny += actual if actual > 0 else reservation
        return {
            "requests": len(rows),
            "estimated_cny": round(estimated_cny, 8),
            "chargeable_cny": round(reserved_cny, 8),
            "by_status": dict(sorted(by_status.items())),
        }

    @staticmethod
    def _utc_day_bounds(day_utc: str | None = None) -> tuple[str, str, str]:
        if day_utc:
            day = datetime.strptime(day_utc, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            now = datetime.now(timezone.utc)
            day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        next_day = day + timedelta(days=1)
        return day.date().isoformat(), day.isoformat(), next_day.isoformat()

    def capture_interpretation_daily_usage(
        self, provider: str, *, day_utc: str | None = None
    ) -> dict[str, Any]:
        day, start, end = self._utc_day_bounds(day_utc)
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT status,reserved_cny,usage_json
                   FROM capture_interpretation_attempts
                   WHERE provider=? AND started_at>=? AND started_at<?
                   ORDER BY started_at""",
                (provider, start, end),
            ).fetchall()
        return {"date_utc": day, **self._capture_attempt_usage_rows(list(rows))}

    def claim_capture_interpretation(
        self,
        *,
        provider: str,
        daily_request_cap: int,
        daily_cny_cap: float,
        reserve_cny: float,
        lease_seconds: int = 180,
        max_attempts: int = 4,
        interpretation_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically account for usage and lease one external interpretation job.

        A non-positive daily cap explicitly means unlimited. Reservations are
        still mandatory so usage remains auditable even when no ceiling is set.
        """

        if reserve_cny <= 0:
            return {"claimed": False, "reason": "INVALID_COST_RESERVATION"}
        lease_seconds = max(30, min(int(lease_seconds), 3600))
        max_attempts = max(1, min(int(max_attempts), 20))
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        lease_expires = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        day, day_start, day_end = self._utc_day_bounds()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            stale = connection.execute(
                """SELECT interpretation_id,attempts,lease_token
                   FROM capture_interpretation_runs
                   WHERE status='RUNNING' AND lease_expires_at IS NOT NULL
                     AND lease_expires_at<=?""",
                (now,),
            ).fetchall()
            for row in stale:
                token = str(row["lease_token"] or "")
                if token:
                    connection.execute(
                        """UPDATE capture_interpretation_attempts
                           SET status='FAILED',finished_at=?,error_class='LEASE_EXPIRED',
                               error='worker lease expired before completion'
                           WHERE interpretation_id=? AND lease_token=? AND status='RUNNING'""",
                        (now, row["interpretation_id"], token),
                    )
                next_status = "PENDING" if int(row["attempts"] or 0) < max_attempts else "FAILED"
                connection.execute(
                    """UPDATE capture_interpretation_runs
                       SET status=?,available_at=?,lease_token=NULL,lease_expires_at=NULL,
                           claimed_at=NULL,updated_at=?,error='LEASE_EXPIRED'
                       WHERE interpretation_id=?""",
                    (next_status, now, now, row["interpretation_id"]),
                )

            attempt_rows = connection.execute(
                """SELECT status,reserved_cny,usage_json
                   FROM capture_interpretation_attempts
                   WHERE provider=? AND started_at>=? AND started_at<?""",
                (provider, day_start, day_end),
            ).fetchall()
            usage = self._capture_attempt_usage_rows(list(attempt_rows))
            if daily_request_cap > 0 and usage["requests"] >= int(daily_request_cap):
                connection.commit()
                return {"claimed": False, "reason": "DAILY_REQUEST_CAP_REACHED", "usage": {"date_utc": day, **usage}}
            if (
                daily_cny_cap > 0
                and usage["chargeable_cny"] + float(reserve_cny) > float(daily_cny_cap)
            ):
                connection.commit()
                return {"claimed": False, "reason": "DAILY_CNY_CAP_REACHED", "usage": {"date_utc": day, **usage}}

            params: list[Any] = [provider, now, max_attempts]
            exact = ""
            if interpretation_id:
                exact = " AND interpretation_id=?"
                params.append(interpretation_id)
            row = connection.execute(
                """SELECT * FROM capture_interpretation_runs
                   WHERE provider=? AND external_call=1
                     AND status IN ('PENDING','BUDGET_BLOCKED')
                     AND (available_at='' OR available_at<=?) AND attempts<?"""
                + exact
                + " ORDER BY created_at,interpretation_id LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                connection.commit()
                return {"claimed": False, "reason": "NO_ELIGIBLE_JOB", "usage": {"date_utc": day, **usage}}

            attempt_id = "capture-attempt-" + uuid.uuid4().hex
            lease_token = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO capture_interpretation_attempts(
                       attempt_id,interpretation_id,provider,status,lease_token,reserved_cny,
                       usage_json,error_class,error,started_at,finished_at
                   ) VALUES (?,?,?,'RUNNING',?,?,'{}',NULL,NULL,?,NULL)""",
                (
                    attempt_id,
                    row["interpretation_id"],
                    provider,
                    lease_token,
                    float(reserve_cny),
                    now,
                ),
            )
            cursor = connection.execute(
                """UPDATE capture_interpretation_runs
                   SET status='RUNNING',attempts=attempts+1,lease_token=?,
                       lease_expires_at=?,claimed_at=?,updated_at=?,error=NULL
                   WHERE interpretation_id=? AND status IN ('PENDING','BUDGET_BLOCKED')""",
                (lease_token, lease_expires, now, now, row["interpretation_id"]),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return {"claimed": False, "reason": "CLAIM_RACE_LOST"}
            connection.commit()
            result = dict(row)
            result.update(
                {
                    "claimed": True,
                    "attempt_id": attempt_id,
                    "lease_token": lease_token,
                    "lease_expires_at": lease_expires,
                    "usage_before_claim": {"date_utc": day, **usage},
                }
            )
            for field in ("output_json", "guardrails_json", "usage_json"):
                result[field.removesuffix("_json")] = _safe_json(result.pop(field), {})
            return result

    def complete_claimed_capture_interpretation(
        self,
        interpretation_id: str,
        attempt_id: str,
        lease_token: str,
        output: dict[str, Any],
        *,
        guardrails: dict[str, Any],
        usage: dict[str, Any],
        latency_ms: float,
    ) -> None:
        now = utc_now()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT status,lease_token,capture_receipt_sha256,contract_version,prompt_sha256
                   FROM capture_interpretation_runs WHERE interpretation_id=?""",
                (interpretation_id,),
            ).fetchone()
            attempt = connection.execute(
                """SELECT status,lease_token FROM capture_interpretation_attempts
                   WHERE attempt_id=? AND interpretation_id=?""",
                (attempt_id, interpretation_id),
            ).fetchone()
            if row is None or attempt is None:
                raise KeyError("capture interpretation lease not found")
            if row["status"] != "RUNNING" or attempt["status"] != "RUNNING":
                raise ValueError("capture interpretation lease is not running")
            if row["lease_token"] != lease_token or attempt["lease_token"] != lease_token:
                raise ValueError("capture interpretation lease token mismatch")
            if str(output.get("capture_receipt_sha256") or "") != str(row["capture_receipt_sha256"]):
                raise ValueError("capture interpretation receipt changed before completion")
            if str(output.get("contract_version") or "") != str(row["contract_version"]):
                raise ValueError("capture interpretation contract changed before completion")
            if str(output.get("prompt_sha256") or "") != str(row["prompt_sha256"]):
                raise ValueError("capture interpretation prompt changed before completion")
            persisted = dict(output)
            persisted["persisted"] = True
            connection.execute(
                """UPDATE capture_interpretation_attempts
                   SET status='COMPLETED',usage_json=?,finished_at=?,error_class=NULL,error=NULL
                   WHERE attempt_id=?""",
                (_stable_json(usage), now, attempt_id),
            )
            connection.execute(
                """UPDATE capture_interpretation_runs
                   SET status='COMPLETED',output_json=?,guardrails_json=?,usage_json=?,
                       latency_ms=?,lease_token=NULL,lease_expires_at=NULL,claimed_at=NULL,
                       updated_at=?,error=NULL WHERE interpretation_id=?""",
                (
                    _stable_json(persisted),
                    _stable_json(guardrails),
                    _stable_json(usage),
                    float(latency_ms),
                    now,
                    interpretation_id,
                ),
            )
            connection.commit()

    def fail_claimed_capture_interpretation(
        self,
        interpretation_id: str,
        attempt_id: str,
        lease_token: str,
        *,
        error: str,
        error_class: str,
        usage: dict[str, Any] | None = None,
        retryable: bool,
        max_attempts: int = 4,
        backoff_seconds: int = 60,
    ) -> str:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT status,attempts,lease_token FROM capture_interpretation_runs
                   WHERE interpretation_id=?""",
                (interpretation_id,),
            ).fetchone()
            attempt = connection.execute(
                """SELECT status,lease_token FROM capture_interpretation_attempts
                   WHERE attempt_id=? AND interpretation_id=?""",
                (attempt_id, interpretation_id),
            ).fetchone()
            if row is None or attempt is None:
                raise KeyError("capture interpretation lease not found")
            if row["lease_token"] != lease_token or attempt["lease_token"] != lease_token:
                raise ValueError("capture interpretation lease token mismatch")
            should_retry = retryable and int(row["attempts"] or 0) < max(1, int(max_attempts))
            next_status = "PENDING" if should_retry else "FAILED"
            available_at = (
                now_dt + timedelta(seconds=max(1, min(int(backoff_seconds), 86400)))
            ).isoformat()
            connection.execute(
                """UPDATE capture_interpretation_attempts
                   SET status='FAILED',usage_json=?,finished_at=?,error_class=?,error=?
                   WHERE attempt_id=?""",
                (
                    _stable_json(usage or {}),
                    now,
                    str(error_class)[:120],
                    str(error)[:2000],
                    attempt_id,
                ),
            )
            connection.execute(
                """UPDATE capture_interpretation_runs
                   SET status=?,usage_json=?,available_at=?,lease_token=NULL,
                       lease_expires_at=NULL,claimed_at=NULL,updated_at=?,error=?
                   WHERE interpretation_id=?""",
                (
                    next_status,
                    _stable_json(usage or {}),
                    available_at if should_retry else now,
                    now,
                    str(error)[:2000],
                    interpretation_id,
                ),
            )
            connection.commit()
        return next_status

    def capture_interpretation_queue_health(
        self,
        provider: str,
        *,
        contract_version: str | None = None,
        prompt_version: str | None = None,
        prompt_sha256: str | None = None,
        model_snapshot: str | None = None,
    ) -> dict[str, Any]:
        where = ["provider=?"]
        params: list[Any] = [provider]
        for column, value in (
            ("contract_version", contract_version),
            ("prompt_version", prompt_version),
            ("prompt_sha256", prompt_sha256),
            ("model_snapshot", model_snapshot),
        ):
            if value is not None:
                where.append(f"{column}=?")
                params.append(value)
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT status,COUNT(*) AS count,MIN(created_at) AS oldest
                   FROM capture_interpretation_runs WHERE """
                + " AND ".join(where)
                + " GROUP BY status ORDER BY status",
                params,
            ).fetchall()
            recent_where = list(where)
            recent_params = list(params)
            recent_where.append("created_at>=?")
            recent_params.append(
                (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            )
            recent_rows = connection.execute(
                """SELECT r.created_at,r.updated_at,r.latency_ms,
                          (SELECT MIN(a.started_at)
                           FROM capture_interpretation_attempts a
                           WHERE a.interpretation_id=r.interpretation_id) AS first_claimed_at
                   FROM capture_interpretation_runs r WHERE """
                + " AND ".join(recent_where)
                + " AND r.status='COMPLETED' ORDER BY r.created_at",
                recent_params,
            ).fetchall()

        def percentile(values: list[float], fraction: float) -> float | None:
            if not values:
                return None
            ordered = sorted(values)
            index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction)))
            return round(ordered[index], 3)

        provider_latencies = [
            float(row["latency_ms"])
            for row in recent_rows
            if row["latency_ms"] is not None
        ]
        queue_waits: list[float] = []
        for row in recent_rows:
            try:
                if not row["first_claimed_at"]:
                    continue
                created = datetime.fromisoformat(str(row["created_at"]))
                claimed = datetime.fromisoformat(
                    str(row["first_claimed_at"])
                )
            except (TypeError, ValueError):
                continue
            queue_waits.append(max(0.0, (claimed - created).total_seconds()))
        by_status = {str(row["status"]): int(row["count"]) for row in rows}
        oldest_pending = next(
            (str(row["oldest"]) for row in rows if row["status"] in {"PENDING", "BUDGET_BLOCKED"}),
            None,
        )
        return {
            "provider": provider,
            "by_status": by_status,
            "oldest_pending_at": oldest_pending,
            "last_24h_latency": {
                "completed": len(recent_rows),
                "queue_wait_seconds_p50": percentile(queue_waits, 0.50),
                "queue_wait_seconds_p95": percentile(queue_waits, 0.95),
                "provider_latency_ms_p50": percentile(provider_latencies, 0.50),
                "provider_latency_ms_p95": percentile(provider_latencies, 0.95),
            },
            "daily": self.capture_interpretation_daily_usage(provider),
        }

    def capture_interpretation_runs(
        self,
        event_id: str | None = None,
        *,
        capture_receipt_sha256: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if event_id:
            where.append("event_id=?")
            params.append(event_id)
        if capture_receipt_sha256:
            where.append("capture_receipt_sha256=?")
            params.append(capture_receipt_sha256)
        sql = "SELECT * FROM capture_interpretation_runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with closing(self.connect()) as connection:
            rows = [dict(row) for row in connection.execute(sql, params)]
        for row in rows:
            row["output"] = _safe_json(row.pop("output_json"), {})
            row["guardrails"] = _safe_json(row.pop("guardrails_json"), {})
            row["usage"] = _safe_json(row.pop("usage_json"), {})
        return rows

    def latest_capture_interpretation(
        self,
        event_id: str,
        capture_receipt_sha256: str,
    ) -> dict[str, Any] | None:
        rows = self.capture_interpretation_runs(
            event_id,
            capture_receipt_sha256=capture_receipt_sha256,
            limit=20,
        )
        completed = [row for row in rows if row["status"] == "COMPLETED"]
        if not completed:
            return None
        completed.sort(
            key=lambda row: (
                int(row.get("external_call") or 0),
                str(row.get("updated_at") or ""),
            ),
            reverse=True,
        )
        return completed[0]

    def latest_capture_interpretations(
        self,
        event_id: str,
        capture_receipt_sha256s: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Return the preferred completed run for each requested capture.

        A public dossier may contain many captures.  Looking them up through
        ``latest_capture_interpretation`` opened one SQLite connection per
        capture.  This event-scoped query keeps the same preference order
        (external generation first, then newest update) with one connection
        and one statement.  ``json_each`` keeps the statement bounded without
        depending on SQLite's positional-parameter limit.
        """

        receipts = sorted(
            {
                str(receipt).strip()
                for receipt in capture_receipt_sha256s
                if str(receipt).strip()
            }
        )
        if not receipts:
            return {}
        with closing(self.connect()) as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM capture_interpretation_runs
                       WHERE event_id=? AND status='COMPLETED'
                         AND capture_receipt_sha256 IN (
                           SELECT value FROM json_each(?)
                         )
                       ORDER BY capture_receipt_sha256,
                                external_call DESC,updated_at DESC""",
                    (event_id, json.dumps(receipts, ensure_ascii=False)),
                )
            ]
        selected: dict[str, dict[str, Any]] = {}
        for row in rows:
            receipt = str(row.get("capture_receipt_sha256") or "")
            if receipt in selected:
                continue
            row["output"] = _safe_json(row.pop("output_json"), {})
            row["guardrails"] = _safe_json(row.pop("guardrails_json"), {})
            row["usage"] = _safe_json(row.pop("usage_json"), {})
            selected[receipt] = row
        return selected

    def latest_capture_interpretation_run(
        self,
        event_id: str,
        capture_receipt_sha256: str,
    ) -> dict[str, Any] | None:
        """Return the newest run regardless of outcome for scheduler decisions."""

        rows = self.capture_interpretation_runs(
            event_id,
            capture_receipt_sha256=capture_receipt_sha256,
            limit=1,
        )
        return rows[0] if rows else None

    def capture_interpretation_terminal_keys(
        self,
        *,
        provider: str,
        contract_version: str,
        prompt_version: str,
        prompt_sha256: str,
        model_snapshot: str,
    ) -> dict[tuple[str, str, int], str]:
        """Bulk-load immutable terminal receipts for one model generation.

        The scheduler previously opened a new SQLite connection for every
        historical capture.  Loading the complete terminal key set once keeps
        backlog discovery linear and makes completed history a zero-call cache.
        Event version is part of the key so a canonical revision cannot reuse
        an interpretation generated for an older event meaning.
        """

        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT event_id,capture_receipt_sha256,status,updated_at,output_json
                   FROM capture_interpretation_runs
                   WHERE provider=? AND contract_version=? AND prompt_version=?
                     AND prompt_sha256=? AND model_snapshot=?
                     AND status IN ('COMPLETED','FAILED')
                   ORDER BY updated_at DESC""",
                (
                    provider,
                    contract_version,
                    prompt_version,
                    prompt_sha256,
                    model_snapshot,
                ),
            ).fetchall()
        result: dict[tuple[str, str, int], str] = {}
        for row in rows:
            output = _safe_json(row["output_json"], {})
            if str(row["status"]) == "FAILED":
                # Historical failed rows predate event-version persistence.
                # Keep them terminal through a dedicated wildcard without
                # allowing an old successful answer to satisfy a new version.
                event_version = -1
            else:
                try:
                    event_version = int(output.get("bound_event_version") or 0)
                except (AttributeError, TypeError, ValueError):
                    event_version = 0
            key = (
                str(row["event_id"]),
                str(row["capture_receipt_sha256"]),
                event_version,
            )
            result.setdefault(key, str(row["status"]))
        return result

    def record_evidence_object(
        self,
        event_id: str,
        evidence_id: str,
        metadata: dict[str, Any],
        *,
        source_url: str,
        fetched_at: str | None = None,
        object_kind: str = "EXACT_EXCERPT",
    ) -> None:
        if object_kind not in {"EXACT_EXCERPT", "SOURCE_SNAPSHOT"}:
            raise ValueError(f"invalid evidence object kind: {object_kind}")
        with closing(self.connect()) as connection:
            connection.execute(
                """INSERT OR IGNORE INTO evidence_objects(
                       object_sha256,relative_path,mime_type,byte_length,source_url,fetched_at
                   ) VALUES (?,?,?,?,?,?)""",
                (
                    metadata["sha256"],
                    metadata["relative_path"],
                    metadata["mime_type"],
                    int(metadata["byte_length"]),
                    source_url,
                    fetched_at or utc_now(),
                ),
            )
            connection.execute(
                """INSERT INTO evidence_object_links(
                       event_id,evidence_id,object_sha256,linked_at,object_kind
                   ) VALUES (?,?,?,?,?)
                   ON CONFLICT(event_id,evidence_id,object_sha256) DO UPDATE SET
                       object_kind=CASE
                           WHEN excluded.object_kind='SOURCE_SNAPSHOT' THEN 'SOURCE_SNAPSHOT'
                           ELSE evidence_object_links.object_kind
                       END""",
                (event_id, evidence_id, metadata["sha256"], utc_now(), object_kind),
            )
            connection.commit()

    def evidence_objects(self, event_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    """SELECT o.*,l.event_id,l.evidence_id,l.linked_at,l.object_kind
                       FROM evidence_object_links l
                       JOIN evidence_objects o ON o.object_sha256=l.object_sha256
                       WHERE l.event_id=? ORDER BY l.linked_at DESC LIMIT ?""",
                    (event_id, max(1, min(limit, 500))),
                )
            ]
        return rows

    def has_source_snapshot(self, event_id: str, evidence_id: str) -> bool:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """SELECT 1
                   FROM evidence_object_links l
                   JOIN evidence_objects o ON o.object_sha256=l.object_sha256
                   WHERE l.event_id=? AND l.evidence_id=?
                     AND l.object_kind='SOURCE_SNAPSHOT'
                   LIMIT 1""",
                (event_id, evidence_id),
            ).fetchone()
        return row is not None

    def source_snapshot_pairs(self) -> set[tuple[str, str]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT DISTINCT event_id,evidence_id
                   FROM evidence_object_links
                   WHERE object_kind='SOURCE_SNAPSHOT'"""
            ).fetchall()
        return {(str(row["event_id"]), str(row["evidence_id"])) for row in rows}

    def source_snapshot_failure_pairs(self) -> dict[str, set[tuple[str, str]]]:
        state = self.get_state("source_snapshot_failures_v1", {})
        terminal: set[tuple[str, str]] = set()
        retry_pending: set[tuple[str, str]] = set()
        if not isinstance(state, dict):
            return {"terminal_policy": terminal, "retry_pending": retry_pending}
        for key, item in state.items():
            if not isinstance(item, dict) or ":" not in str(key):
                continue
            event_id, evidence_id = str(key).split(":", 1)
            target = terminal if item.get("terminal_policy") is True else retry_pending
            target.add((event_id, evidence_id))
        return {"terminal_policy": terminal, "retry_pending": retry_pending}

    def evidence_archive_summary(self, limit: int = 20) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            totals = connection.execute(
                """SELECT COUNT(*) AS objects,COALESCE(SUM(byte_length),0) AS archived_bytes
                   FROM evidence_objects"""
            ).fetchone()
            kinds = connection.execute(
                """SELECT
                       COUNT(DISTINCT CASE WHEN object_kind='SOURCE_SNAPSHOT' THEN object_sha256 END) AS source_snapshots,
                       COUNT(DISTINCT CASE WHEN object_kind='EXACT_EXCERPT' THEN object_sha256 END) AS exact_excerpts,
                       COUNT(DISTINCT CASE WHEN object_kind='SOURCE_SNAPSHOT'
                                          THEN event_id || ':' || evidence_id END) AS source_snapshot_links
                   FROM evidence_object_links"""
            ).fetchone()
            by_mime = {
                row["mime_type"]: {
                    "objects": int(row["objects"]),
                    "bytes": int(row["bytes"]),
                }
                for row in connection.execute(
                    """SELECT mime_type,COUNT(*) AS objects,COALESCE(SUM(byte_length),0) AS bytes
                       FROM evidence_objects GROUP BY mime_type ORDER BY mime_type"""
                )
            }
            recent = [
                dict(row)
                for row in connection.execute(
                    """SELECT o.*,l.event_id,l.evidence_id,l.linked_at,l.object_kind
                       FROM evidence_object_links l
                       JOIN evidence_objects o ON o.object_sha256=l.object_sha256
                       ORDER BY l.linked_at DESC LIMIT ?""",
                    (max(1, min(int(limit), 100)),),
                )
            ]
        return {
            "objects": int(totals["objects"] or 0),
            "archived_bytes": int(totals["archived_bytes"] or 0),
            "source_snapshots": int(kinds["source_snapshots"] or 0),
            "source_snapshot_links": int(kinds["source_snapshot_links"] or 0),
            "exact_excerpts": int(kinds["exact_excerpts"] or 0),
            "by_mime": by_mime,
            "recent_objects": recent,
            "policy": {
                "immutable": True,
                "content_address": "sha256",
                "raw_snapshot_mime_types": ["text/html", "text/plain", "application/pdf", "application/json"],
                "no_trading": True,
                "allowed_as_model_feature": False,
            },
        }

    def record_agent_decision(self, result: dict[str, Any]) -> str:
        decision_id = f"agent-{uuid.uuid4().hex}"
        evidence_ids = [edge["evidence_id"] for edge in result.get("evidence_edges", [])]
        with closing(self.connect()) as connection:
            connection.execute(
                """INSERT INTO agent_decisions(
                       decision_id,event_id,trace_id,status,prompt_version,model_provider,
                       model_snapshot,output_json,guardrails_json,tool_calls_json,
                       evidence_ids_json,latency_ms,created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision_id,
                    result["event_id"],
                    result["trace_id"],
                    result["status"],
                    result["prompt_version"],
                    result["model_provider"],
                    result["model_snapshot"],
                    _stable_json(result),
                    _stable_json(result.get("guardrails", {})),
                    _stable_json(result.get("tool_calls", [])),
                    _stable_json(evidence_ids),
                    float(result["latency_ms"]),
                    utc_now(),
                ),
            )
            connection.commit()
        return decision_id

    def agent_decisions(self, event_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM agent_decisions WHERE event_id=? ORDER BY created_at DESC LIMIT ?",
                    (event_id, max(1, min(limit, 200))),
                )
            ]
        for row in rows:
            row["output"] = json.loads(row.pop("output_json"))
            row["guardrails"] = json.loads(row.pop("guardrails_json"))
            row["tool_calls"] = json.loads(row.pop("tool_calls_json"))
            row["evidence_ids"] = json.loads(row.pop("evidence_ids_json"))
        return rows

    @staticmethod
    def _light_mutation_payload(result: dict[str, Any]) -> dict[str, Any]:
        """Return the durable, deterministic audit payload for a formal mutation.

        The ledger write is deliberately *not* represented by a random run id.
        One event/version may have only one formal conclusion, so retries must
        resolve to the same identity and reject a materially different payload.
        """
        before_version = int(result["before_version"])
        after_version = int(result.get("after_version") or before_version + 1)
        decision = str(result["decision"])
        if decision not in {"SUPPORTED", "INSUFFICIENT"}:
            raise ValueError(f"unsupported formal light-verification decision: {decision}")
        if after_version != before_version + 1:
            raise ValueError("light-verification after_version must equal before_version + 1")
        return {
            "audit_contract_version": "formal-mutation-audit-v1",
            "mutation_kind": FORMAL_MUTATION_KIND_LIGHT_VERIFICATION,
            "event_id": str(result["event_id"]),
            "batch_id": str(result["batch_id"]),
            "decision": decision,
            "before_version": before_version,
            "after_version": after_version,
            "evidence_ids": sorted(str(item) for item in result.get("evidence_ids", [])),
            "budget": result.get("budget", {}),
            "rationale": str(result.get("rationale") or ""),
            "checks": result.get("checks", []),
            "before_model": result.get("before_model", {}),
            "after_model": result.get("after_model", {}),
            "no_trading": bool(result.get("no_trading", True)),
        }

    @staticmethod
    def _light_mutation_identity(payload: dict[str, Any]) -> str:
        """Identity is tied to the immutable ledger version, never a retry token."""
        return "formal-light-" + _stable_sha256(
            {
                "contract": payload["audit_contract_version"],
                "kind": payload["mutation_kind"],
                "event_id": payload["event_id"],
                "after_version": payload["after_version"],
            }
        )[:40]

    @staticmethod
    def _light_mutation_content_hash(payload: dict[str, Any]) -> str:
        return _stable_sha256(payload)

    def prepare_light_verification_mutation(self, result: dict[str, Any]) -> str:
        """Durably record audit intent *before* the caller commits the ledger write.

        Callers must invoke this before ``BEGIN IMMEDIATE`` on the ledger.  If the
        process dies after the ledger commit, the prepared record remains durable
        and can be reconciled instead of silently losing its audit trail.
        """
        payload = self._light_mutation_payload(result)
        mutation_id = self._light_mutation_identity(payload)
        content_sha256 = self._light_mutation_content_hash(payload)
        now = utc_now()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT content_sha256 FROM formal_mutation_audits WHERE mutation_id=?",
                (mutation_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["content_sha256"]) != content_sha256:
                    connection.rollback()
                    raise ValueError(
                        "formal mutation identity collision: event/version already has different audit content"
                    )
                connection.commit()
                return mutation_id
            try:
                connection.execute(
                    """INSERT INTO formal_mutation_audits(
                           mutation_id,mutation_kind,event_id,before_version,after_version,
                           decision,content_sha256,state,payload_json,created_at,updated_at,
                           ledger_committed_at,reconciled_at,last_error
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        mutation_id,
                        FORMAL_MUTATION_KIND_LIGHT_VERIFICATION,
                        payload["event_id"],
                        payload["before_version"],
                        payload["after_version"],
                        payload["decision"],
                        content_sha256,
                        "PREPARED",
                        _stable_json(payload),
                        now,
                        now,
                        None,
                        None,
                        None,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # A unique event/version collision must never be converted into a
                # second conclusion with a random retry id.
                collision = connection.execute(
                    """SELECT mutation_id,content_sha256 FROM formal_mutation_audits
                       WHERE mutation_kind=? AND event_id=? AND after_version=?""",
                    (
                        FORMAL_MUTATION_KIND_LIGHT_VERIFICATION,
                        payload["event_id"],
                        payload["after_version"],
                    ),
                ).fetchone()
                connection.rollback()
                if collision is not None and str(collision["content_sha256"]) == content_sha256:
                    return str(collision["mutation_id"])
                raise ValueError("formal mutation event/version identity collision") from exc
            connection.commit()
        return mutation_id

    def _upsert_light_verification_run(
        self,
        connection: sqlite3.Connection,
        mutation_id: str | None,
        result: dict[str, Any],
    ) -> str:
        run_id = str(result.get("run_id") or mutation_id or f"light-{uuid.uuid4().hex}")
        if mutation_id:
            existing = connection.execute(
                "SELECT run_id FROM light_verification_runs WHERE mutation_id=?",
                (mutation_id,),
            ).fetchone()
            if existing is not None:
                run_id = str(existing["run_id"])
        connection.execute(
            """INSERT INTO light_verification_runs(
                   run_id,batch_id,event_id,decision,before_version,after_version,
                   evidence_ids_json,budget_json,rationale,before_model_json,
                   after_model_json,applied,created_at,mutation_id
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET
                   batch_id=excluded.batch_id,event_id=excluded.event_id,
                   decision=excluded.decision,before_version=excluded.before_version,
                   after_version=excluded.after_version,evidence_ids_json=excluded.evidence_ids_json,
                   budget_json=excluded.budget_json,rationale=excluded.rationale,
                   before_model_json=excluded.before_model_json,
                   after_model_json=excluded.after_model_json,applied=excluded.applied,
                   created_at=excluded.created_at,mutation_id=COALESCE(excluded.mutation_id,light_verification_runs.mutation_id)""",
            (
                run_id,
                str(result["batch_id"]),
                str(result["event_id"]),
                str(result["decision"]),
                int(result["before_version"]),
                int(result["after_version"]) if result.get("after_version") is not None else None,
                _stable_json(result.get("evidence_ids", [])),
                _stable_json(result.get("budget", {})),
                str(result.get("rationale") or ""),
                _stable_json(result.get("before_model", {})),
                _stable_json(result.get("after_model", {})),
                int(bool(result.get("applied"))),
                str(result.get("created_at") or utc_now()),
                mutation_id,
            ),
        )
        return run_id

    def confirm_light_verification_mutation(
        self,
        mutation_id: str,
        result: dict[str, Any],
        *,
        recovered: bool = False,
    ) -> str:
        """Mark a pre-written audit intent as committed after the ledger commits.

        This method is idempotent.  It never writes the ledger, and therefore a
        replay after a process crash is safe as long as the caller observed the
        committed ledger version.
        """
        payload = self._light_mutation_payload(result)
        expected_mutation_id = self._light_mutation_identity(payload)
        if mutation_id != expected_mutation_id:
            raise ValueError("mutation_id does not match the supplied event/version payload")
        content_sha256 = self._light_mutation_content_hash(payload)
        now = utc_now()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT content_sha256,state FROM formal_mutation_audits WHERE mutation_id=?",
                (mutation_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(f"formal mutation was not prepared: {mutation_id}")
            if str(row["content_sha256"]) != content_sha256:
                connection.rollback()
                raise ValueError("formal mutation payload changed after prepare")
            if str(row["state"]) == "ABANDONED":
                connection.rollback()
                raise ValueError("cannot confirm an abandoned formal mutation")
            stored_result = {
                **result,
                "after_version": payload["after_version"],
                "applied": True,
            }
            run_id = self._upsert_light_verification_run(connection, mutation_id, stored_result)
            state = "RECOVERED" if recovered else "LEDGER_COMMITTED"
            connection.execute(
                """UPDATE formal_mutation_audits
                   SET state=?,payload_json=?,updated_at=?,ledger_committed_at=COALESCE(ledger_committed_at,?),
                       reconciled_at=CASE WHEN ? THEN COALESCE(reconciled_at,?) ELSE reconciled_at END,
                       last_error=NULL
                   WHERE mutation_id=?""",
                (
                    state,
                    _stable_json(payload),
                    now,
                    now,
                    int(recovered),
                    now,
                    mutation_id,
                ),
            )
            connection.commit()
        return run_id

    def abandon_light_verification_mutation(self, mutation_id: str, error: str) -> None:
        """Close a prepared intent only when its ledger transaction did not commit."""
        with closing(self.connect()) as connection:
            connection.execute(
                """UPDATE formal_mutation_audits
                   SET state='ABANDONED',updated_at=?,last_error=?
                   WHERE mutation_id=? AND state='PREPARED'""",
                (utc_now(), error[:2000], mutation_id),
            )
            connection.commit()

    def record_light_verification(self, result: dict[str, Any]) -> str:
        """Persist a light-verification audit idempotently.

        New formal mutations should use ``prepare_*`` then ``confirm_*`` around
        the ledger transaction.  This compatibility path preserves historical
        callers while still assigning a deterministic audit identity.
        """
        if (
            bool(result.get("applied"))
            and str(result.get("decision")) in {"SUPPORTED", "INSUFFICIENT"}
            and result.get("after_version") is not None
        ):
            mutation_id = self.prepare_light_verification_mutation(result)
            return self.confirm_light_verification_mutation(mutation_id, result)
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            run_id = self._upsert_light_verification_run(connection, None, result)
            connection.commit()
        return run_id

    def light_verification_runs(self, event_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM light_verification_runs"
        params: list[Any] = []
        if event_id:
            sql += " WHERE event_id=?"
            params.append(event_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        with closing(self.connect()) as connection:
            rows = [dict(row) for row in connection.execute(sql, params)]
        for row in rows:
            row["evidence_ids"] = json.loads(row.pop("evidence_ids_json"))
            row["budget"] = json.loads(row.pop("budget_json"))
            row["before_model"] = json.loads(row.pop("before_model_json"))
            row["after_model"] = json.loads(row.pop("after_model_json"))
        return rows

    def formal_mutation_audits(self, event_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM formal_mutation_audits"
        params: list[Any] = []
        if event_id:
            sql += " WHERE event_id=?"
            params.append(event_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with closing(self.connect()) as connection:
            rows = [dict(row) for row in connection.execute(sql, params)]
        for row in rows:
            row["payload"] = _safe_json(row.pop("payload_json"), {})
        return rows

    def audit_reconciliation_status(self) -> dict[str, Any]:
        """Return durable-audit health without changing either database."""
        with closing(self.connect()) as connection:
            counts = {
                str(row["state"]): int(row["n"])
                for row in connection.execute(
                    "SELECT state,COUNT(*) AS n FROM formal_mutation_audits GROUP BY state"
                )
            }
        prepared = int(counts.get("PREPARED", 0))
        conflicts = int(counts.get("RECOVERY_CONFLICT", 0))
        return {
            "status": "ok" if not prepared and not conflicts else "degraded",
            "states": counts,
            "pending_reconciliation": prepared,
            "recovery_conflicts": conflicts,
            "contract": "formal-mutation-audit-v1",
            "mutations_are_idempotent": True,
        }

    @staticmethod
    def _ledger_light_verification_rows(ledger_db: str | Path) -> list[dict[str, Any]]:
        """Read formal light-verification versions without relying on JSON1 extensions."""
        path = Path(ledger_db)
        if not path.is_file():
            raise FileNotFoundError(path)
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            rows = [
                dict(row)
                for row in connection.execute(
                    """SELECT event_id,version,changed_at,status,facts_json,change_reason
                       FROM event_versions
                       WHERE change_reason LIKE 'light_evidence_verification_v%'
                       ORDER BY changed_at,event_id,version"""
                )
            ]
        return rows

    @staticmethod
    def _result_from_ledger_light_version(row: dict[str, Any]) -> dict[str, Any] | None:
        facts = _safe_json(row.get("facts_json"), {})
        light = facts.get("light_verification") if isinstance(facts, dict) else None
        if not isinstance(light, dict):
            return None
        conclusion = str(light.get("formal_conclusion") or "")
        if conclusion == "verified":
            decision = "SUPPORTED"
        elif conclusion == "weak":
            decision = "INSUFFICIENT"
        else:
            return None
        before_version = int(row["version"]) - 1
        if before_version < 0:
            return None
        return {
            "batch_id": str(light.get("batch_id") or f"recovered-{row['event_id']}-{row['version']}"),
            "event_id": str(row["event_id"]),
            "decision": decision,
            "before_version": before_version,
            "after_version": int(row["version"]),
            "evidence_ids": light.get("evidence_ids", []),
            "budget": light.get("budget", {}),
            "rationale": str(light.get("rationale") or ""),
            "checks": light.get("checks", []),
            "before_model": (light.get("model_reassessment") or {}).get("before", {}),
            "after_model": (light.get("model_reassessment") or {}).get("after", {}),
            "created_at": str(row.get("changed_at") or utc_now()),
            "applied": True,
            "no_trading": bool(light.get("no_trading", True)),
        }

    def reconcile_light_verification_mutations(
        self,
        ledger_db: str | Path,
        *,
        limit: int = 500,
        include_legacy: bool = True,
    ) -> dict[str, int]:
        """Recover prepared audit intents and optionally backfill legacy committed rows.

        The reconciler never changes the ledger.  It only promotes a prepared
        audit row after observing its exact immutable event version in the ledger.
        This makes a crash between ledger commit and ops confirmation recoverable.
        """
        ledger_rows = self._ledger_light_verification_rows(ledger_db)
        by_identity: dict[tuple[str, int], dict[str, Any]] = {}
        for row in ledger_rows:
            result = self._result_from_ledger_light_version(row)
            if result is not None:
                by_identity[(result["event_id"], int(result["after_version"]))] = result

        counters = {
            "prepared_seen": 0,
            "recovered": 0,
            "still_pending": 0,
            "conflicts": 0,
            "legacy_backfilled": 0,
        }
        with closing(self.connect()) as connection:
            pending = [
                dict(row)
                for row in connection.execute(
                    """SELECT mutation_id,event_id,after_version,payload_json
                       FROM formal_mutation_audits
                       WHERE state='PREPARED'
                       ORDER BY created_at LIMIT ?""",
                    (max(1, min(int(limit), 5000)),),
                )
            ]
            existing = {
                (str(row["event_id"]), int(row["after_version"]))
                for row in connection.execute(
                    """SELECT event_id,after_version FROM formal_mutation_audits
                       WHERE mutation_kind=?""",
                    (FORMAL_MUTATION_KIND_LIGHT_VERIFICATION,),
                )
            }
        for row in pending:
            counters["prepared_seen"] += 1
            actual = by_identity.get((str(row["event_id"]), int(row["after_version"])))
            if actual is None:
                counters["still_pending"] += 1
                continue
            try:
                self.confirm_light_verification_mutation(str(row["mutation_id"]), actual, recovered=True)
                counters["recovered"] += 1
            except (KeyError, ValueError):
                with closing(self.connect()) as connection:
                    connection.execute(
                        """UPDATE formal_mutation_audits
                           SET state='RECOVERY_CONFLICT',updated_at=?,last_error=?
                           WHERE mutation_id=?""",
                        (utc_now(), "ledger version does not match prepared audit payload", row["mutation_id"]),
                    )
                    connection.commit()
                counters["conflicts"] += 1

        if include_legacy:
            for identity, result in by_identity.items():
                if identity in existing:
                    continue
                try:
                    mutation_id = self.prepare_light_verification_mutation(result)
                    self.confirm_light_verification_mutation(mutation_id, result, recovered=True)
                    counters["legacy_backfilled"] += 1
                except (KeyError, ValueError):
                    counters["conflicts"] += 1
        return counters

    def record_human_override(
        self,
        event_id: str,
        decision_id: str | None,
        *,
        actor: str,
        reason: str,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> str:
        override_id = f"override-{uuid.uuid4().hex}"
        with closing(self.connect()) as connection:
            connection.execute(
                """INSERT INTO human_overrides(
                       override_id,event_id,decision_id,actor,reason,before_json,after_json,created_at
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    override_id,
                    event_id,
                    decision_id,
                    actor,
                    reason,
                    _stable_json(before),
                    _stable_json(after),
                    utc_now(),
                ),
            )
            connection.commit()
        return override_id

    def human_overrides(self, event_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM human_overrides WHERE event_id=? ORDER BY created_at DESC LIMIT ?",
                    (event_id, max(1, min(limit, 200))),
                )
            ]
        for row in rows:
            row["before"] = json.loads(row.pop("before_json"))
            row["after"] = json.loads(row.pop("after_json"))
        return rows

    def create_adjudication_sample(self, sample: dict[str, Any]) -> tuple[str, bool]:
        """Persist a pre-freeze sample without storing a target label."""
        required = {
            "sample_id",
            "event_id",
            "text_sha256",
            "content",
            "source_id",
            "authority_tier",
            "entity_group",
            "event_chain_group",
        }
        missing = sorted(required - set(sample))
        if missing:
            raise ValueError(f"missing adjudication sample fields: {', '.join(missing)}")
        now = utc_now()
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO adjudication_samples(
                       sample_id,event_id,text_sha256,content_json,source_id,authority_tier,
                       entity_group,event_chain_group,status,created_at,updated_at,freeze_id
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)""",
                (
                    sample["sample_id"],
                    sample["event_id"],
                    sample["text_sha256"],
                    _stable_json(sample["content"]),
                    sample["source_id"],
                    sample["authority_tier"],
                    sample["entity_group"],
                    sample["event_chain_group"],
                    "OPEN",
                    now,
                    now,
                ),
            )
            connection.commit()
            created = cursor.rowcount == 1
            row = connection.execute(
                "SELECT sample_id FROM adjudication_samples WHERE event_id=? AND text_sha256=?",
                (sample["event_id"], sample["text_sha256"]),
            ).fetchone()
        return str(row["sample_id"]), created

    @staticmethod
    def _decode_adjudication_sample(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["content"] = json.loads(item.pop("content_json"))
        return item

    def adjudication_sample(self, sample_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM adjudication_samples WHERE sample_id=?", (sample_id,)
            ).fetchone()
        return self._decode_adjudication_sample(row) if row is not None else None

    def adjudication_samples(
        self,
        *,
        statuses: set[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        sql = "SELECT * FROM adjudication_samples"
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql += f" WHERE status IN ({placeholders})"
            params.extend(sorted(statuses))
        sql += " ORDER BY created_at, sample_id LIMIT ?"
        params.append(max(1, min(int(limit), 5000)))
        with closing(self.connect()) as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._decode_adjudication_sample(row) for row in rows]

    def adjudication_reviews(self, sample_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM adjudication_reviews WHERE sample_id=? ORDER BY created_at,review_id",
                    (sample_id,),
                )
            ]

    def record_adjudication_review(
        self,
        sample_id: str,
        *,
        reviewer_id: str,
        review_role: str,
        materiality: str,
        polarity: str,
        evidence_state: str,
        rationale: str,
    ) -> dict[str, Any]:
        """Atomically record one independent review and advance workflow state."""
        role = review_role.upper()
        if role not in {"REVIEWER", "ARBITER"}:
            raise ValueError("review_role must be REVIEWER or ARBITER")
        review_id = f"review-{uuid.uuid4().hex}"
        now = utc_now()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            sample = connection.execute(
                "SELECT * FROM adjudication_samples WHERE sample_id=?", (sample_id,)
            ).fetchone()
            if sample is None:
                raise KeyError(sample_id)
            if sample["status"] == "FROZEN":
                raise ValueError("frozen sample cannot be reviewed")
            reviews = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM adjudication_reviews WHERE sample_id=? ORDER BY created_at,review_id",
                    (sample_id,),
                )
            ]
            if any(item["reviewer_id"] == reviewer_id for item in reviews):
                raise ValueError("reviewer already submitted this sample")
            reviewer_rows = [item for item in reviews if item["review_role"] == "REVIEWER"]
            arbiter_rows = [item for item in reviews if item["review_role"] == "ARBITER"]
            if role == "REVIEWER" and len(reviewer_rows) >= 2:
                raise ValueError("two independent reviews already exist")
            if role == "ARBITER":
                if len(reviewer_rows) != 2:
                    raise ValueError("arbitration requires two independent reviews")
                if arbiter_rows:
                    raise ValueError("arbitration already completed")
                axes = ("materiality", "polarity", "evidence_state")
                if all(reviewer_rows[0][field] == reviewer_rows[1][field] for field in axes):
                    raise ValueError("matching reviews do not require arbitration")
            connection.execute(
                """INSERT INTO adjudication_reviews(
                       review_id,sample_id,reviewer_id,review_role,materiality,polarity,
                       evidence_state,rationale,content_sha256,created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    review_id,
                    sample_id,
                    reviewer_id,
                    role,
                    materiality,
                    polarity,
                    evidence_state,
                    rationale,
                    sample["text_sha256"],
                    now,
                ),
            )
            if role == "ARBITER":
                status = "READY"
            else:
                reviewer_rows.append(
                    {
                        "materiality": materiality,
                        "polarity": polarity,
                        "evidence_state": evidence_state,
                    }
                )
                if len(reviewer_rows) == 1:
                    status = "IN_REVIEW"
                else:
                    axes = ("materiality", "polarity", "evidence_state")
                    status = (
                        "READY"
                        if all(reviewer_rows[0][field] == reviewer_rows[1][field] for field in axes)
                        else "CONFLICT"
                    )
            connection.execute(
                "UPDATE adjudication_samples SET status=?,updated_at=? WHERE sample_id=?",
                (status, now, sample_id),
            )
            connection.commit()
        return {
            "review_id": review_id,
            "sample_id": sample_id,
            "reviewer_id": reviewer_id,
            "review_role": role,
            "status": status,
            "created_at": now,
        }

    def commit_adjudication_freeze(
        self,
        sample_ids: list[str],
        freeze_id: str,
        *,
        dataset_sha256: str,
        sample_ids_sha256: str,
        authorization: dict[str, Any],
        dataset_path: str,
        manifest_path: str,
    ) -> dict[str, Any]:
        """Commit the immutable set and its full receipt in one transaction.

        An exact retry is idempotent so a crash after the database commit but
        before the filesystem manifest update can be reconciled safely. Any
        mismatch is a conflicting freeze and fails closed.
        """

        ordered = sorted(set(str(item).strip() for item in sample_ids if str(item).strip()))
        freeze_id = str(freeze_id).strip()
        dataset_sha256 = str(dataset_sha256).strip().lower()
        sample_ids_sha256 = str(sample_ids_sha256).strip().lower()
        if not ordered or not freeze_id:
            raise ValueError("freeze requires sample IDs and a freeze ID")
        if len(dataset_sha256) != 64 or len(sample_ids_sha256) != 64:
            raise ValueError("freeze requires full dataset and sample-set hashes")
        required_authorization = {
            "schema_version",
            "action",
            "approved",
            "authorization_id",
            "actor",
            "purpose",
            "expires_at",
            "freeze_id",
            "dataset_sha256",
            "sample_ids_sha256",
            "sample_count",
            "held_out_source_families",
        }
        if set(authorization) != required_authorization:
            raise ValueError("freeze authorization fields do not match the v1 contract")
        authorization_id = str(authorization.get("authorization_id") or "").strip()
        if not authorization_id:
            raise ValueError("freeze authorization ID is required")
        expected_sample_ids_sha256 = hashlib.sha256(
            _stable_json(ordered).encode("utf-8")
        ).hexdigest()
        if expected_sample_ids_sha256 != sample_ids_sha256:
            raise ValueError("freeze sample-set hash does not match the selected sample IDs")
        if (
            authorization.get("schema_version") != 1
            or authorization.get("action") != "FREEZE_HUMAN_BLIND_V3"
            or authorization.get("approved") is not True
            or authorization.get("freeze_id") != freeze_id
            or authorization.get("dataset_sha256") != dataset_sha256
            or authorization.get("sample_ids_sha256") != sample_ids_sha256
            or authorization.get("sample_count") != len(ordered)
        ):
            raise ValueError("freeze authorization does not bind the selected dataset")
        held_out_sources = authorization.get("held_out_source_families")
        if (
            not isinstance(held_out_sources, list)
            or not held_out_sources
            or any(not isinstance(item, str) or not item.strip() for item in held_out_sources)
            or len(set(held_out_sources)) != len(held_out_sources)
        ):
            raise ValueError("freeze authorization requires distinct held-out source families")
        try:
            expires_at = datetime.fromisoformat(
                str(authorization.get("expires_at") or "").replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError("freeze authorization expiry is invalid") from exc
        if expires_at.tzinfo is None or expires_at.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            raise ValueError("freeze authorization has expired")
        if len(str(authorization.get("actor") or "").strip()) < 3 or len(
            str(authorization.get("purpose") or "").strip()
        ) < 20:
            raise ValueError("freeze authorization actor or purpose is incomplete")
        authorization_sha256 = _stable_sha256(authorization)
        receipt = {
            "schema_version": 1,
            "freeze_id": freeze_id,
            "dataset_sha256": dataset_sha256,
            "sample_ids_sha256": sample_ids_sha256,
            "sample_count": len(ordered),
            "sample_ids": ordered,
            "authorization": authorization,
            "authorization_sha256": authorization_sha256,
            "dataset_path": str(dataset_path),
            "manifest_path": str(manifest_path),
            "state": "COMMITTED",
        }
        receipt_json = _stable_json(receipt)
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM adjudication_freezes WHERE freeze_id=?",
                (freeze_id,),
            ).fetchone()
            placeholders = ",".join("?" for _ in ordered)
            rows = connection.execute(
                f"SELECT sample_id,status,freeze_id FROM adjudication_samples WHERE sample_id IN ({placeholders})",
                ordered,
            ).fetchall()
            if len(rows) != len(ordered):
                raise ValueError("freeze sample set changed before commit")
            if existing is not None:
                if (
                    str(existing["dataset_sha256"]) != dataset_sha256
                    or str(existing["sample_ids_sha256"]) != sample_ids_sha256
                    or int(existing["sample_count"]) != len(ordered)
                    or str(existing["authorization_sha256"]) != authorization_sha256
                    or str(existing["receipt_json"]) != receipt_json
                    or any(
                        row["status"] != "FROZEN" or row["freeze_id"] != freeze_id
                        for row in rows
                    )
                ):
                    raise ValueError("freeze retry conflicts with the committed receipt")
                connection.rollback()
                return {
                    "frozen_samples": len(ordered),
                    "idempotent": True,
                    "receipt": receipt,
                }
            invalid = [
                str(row["sample_id"])
                for row in rows
                if row["status"] != "READY" or row["freeze_id"] not in (None, "")
            ]
            if invalid:
                raise ValueError("freeze requires unfrozen READY samples: " + ", ".join(invalid))
            now = utc_now()
            connection.execute(
                """INSERT INTO adjudication_freezes(
                       freeze_id,dataset_sha256,sample_ids_sha256,sample_count,
                       authorization_id,authorization_sha256,receipt_json,state,
                       created_at,committed_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    freeze_id,
                    dataset_sha256,
                    sample_ids_sha256,
                    len(ordered),
                    authorization_id,
                    authorization_sha256,
                    receipt_json,
                    "COMMITTED",
                    now,
                    now,
                ),
            )
            connection.execute(
                f"""UPDATE adjudication_samples
                    SET status='FROZEN',freeze_id=?,updated_at=?
                    WHERE sample_id IN ({placeholders})""",
                (freeze_id, now, *ordered),
            )
            connection.commit()
        return {
            "frozen_samples": len(ordered),
            "idempotent": False,
            "receipt": receipt,
        }

    def adjudication_freeze(self, freeze_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM adjudication_freezes WHERE freeze_id=?",
                (freeze_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["receipt"] = json.loads(result.pop("receipt_json"))
        return result

    def start_worker_cycle(self) -> str:
        cycle_id = f"cycle-{uuid.uuid4().hex}"
        with closing(self.connect()) as connection:
            connection.execute(
                "INSERT INTO worker_cycles VALUES (?,?,?,?,?,?)",
                (cycle_id, utc_now(), None, "RUNNING", "{}", None),
            )
            connection.commit()
        return cycle_id

    def finish_worker_cycle(self, cycle_id: str, status: str, result: dict[str, Any], error: str | None = None) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE worker_cycles SET finished_at=?,status=?,result_json=?,error=? WHERE cycle_id=?",
                (utc_now(), status, _stable_json(result), error, cycle_id),
            )
            connection.commit()

    def latest_worker_cycle(self) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT * FROM worker_cycles ORDER BY started_at DESC LIMIT 1").fetchone()
        if row is None:
            return None
        result = dict(row)
        result["result"] = json.loads(result.pop("result_json"))
        return result

    def latest_worker_cycle_summary(self) -> dict[str, Any] | None:
        """Return the latest cycle clock without loading its potentially large report.

        The public overview needs only cycle state and timestamps.  Keeping the
        full collector report out of the published snapshot prevents one large
        API-source payload from being copied and hashed on every dashboard
        refresh.  Authenticated operator views may still call
        :meth:`latest_worker_cycle` when they explicitly need diagnostics.
        """

        with closing(self.connect()) as connection:
            row = connection.execute(
                """SELECT cycle_id,started_at,finished_at,status
                   FROM worker_cycles ORDER BY started_at DESC LIMIT 1"""
            ).fetchone()
        return dict(row) if row is not None else None

    def latest_successful_worker_cycle(self) -> dict[str, Any] | None:
        """Return the newest completed SUCCESS cycle, ignoring newer failures/runs."""
        with closing(self.connect()) as connection:
            row = connection.execute(
                """SELECT * FROM worker_cycles
                   WHERE status='SUCCESS' AND finished_at IS NOT NULL
                   ORDER BY finished_at DESC,started_at DESC LIMIT 1"""
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["result"] = json.loads(result.pop("result_json"))
        return result

    def latest_successful_worker_cycle_summary(self) -> dict[str, Any] | None:
        """Return the latest successful cycle clock without its report body."""

        with closing(self.connect()) as connection:
            row = connection.execute(
                """SELECT cycle_id,started_at,finished_at,status
                   FROM worker_cycles
                   WHERE status='SUCCESS' AND finished_at IS NOT NULL
                   ORDER BY finished_at DESC,started_at DESC LIMIT 1"""
            ).fetchone()
        return dict(row) if row is not None else None

    def worker_window(
        self,
        *,
        hours: int = 24,
        expected_interval_seconds: int = 300,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Compute an honest runtime-evidence gate from persisted worker cycles."""
        with closing(self.connect()) as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT started_at,finished_at,status FROM worker_cycles "
                    "WHERE finished_at IS NOT NULL ORDER BY started_at"
                )
            ]
        if not rows:
            return {
                "status": "NO_DATA",
                "target_hours": hours,
                "observed_hours": 0.0,
                "cycles": 0,
                "complete": False,
            }

        def parse(value: str) -> datetime:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

        current = now or datetime.now(timezone.utc)
        starts = [parse(row["started_at"]) for row in rows]
        finishes = [parse(row["finished_at"]) for row in rows]
        latest = max(finishes)
        cutoff = latest.timestamp() - hours * 3600
        window_rows = [row for row in rows if parse(row["started_at"]).timestamp() >= cutoff]
        window_starts = [parse(row["started_at"]) for row in window_rows]
        observed_seconds = max(0.0, (latest - min(starts)).total_seconds())
        gaps = [
            (right - left).total_seconds()
            for left, right in zip(window_starts, window_starts[1:])
        ]
        success = sum(row["status"] == "SUCCESS" for row in window_rows)
        failed = sum(row["status"] not in {"SUCCESS", "DEGRADED"} for row in window_rows)
        degraded = sum(row["status"] == "DEGRADED" for row in window_rows)
        success_rate = success / len(window_rows) if window_rows else 0.0
        latest_age = max(0.0, (current.astimezone(timezone.utc) - latest.astimezone(timezone.utc)).total_seconds())
        complete = all(
            (
                observed_seconds >= hours * 3600,
                success_rate >= 0.95,
                failed == 0,
                latest_age <= expected_interval_seconds * 3,
                max(gaps, default=0.0) <= expected_interval_seconds * 3,
            )
        )
        return {
            "status": "PASS" if complete else "PARTIAL",
            "target_hours": hours,
            "observed_hours": round(observed_seconds / 3600, 3),
            "first_started_at": min(starts).isoformat(),
            "latest_finished_at": latest.isoformat(),
            "latest_age_seconds": round(latest_age, 3),
            "cycles": len(window_rows),
            "success_cycles": success,
            "degraded_cycles": degraded,
            "failed_cycles": failed,
            "success_rate": round(success_rate, 6),
            "max_start_gap_seconds": round(max(gaps, default=0.0), 3),
            "expected_interval_seconds": expected_interval_seconds,
            "complete": complete,
        }

    def create_backup_run(
        self,
        backup_path: Path,
        source_bytes: int,
        *,
        manifest_path: Path | None = None,
        snapshot_kind: str = "ledger_only",
    ) -> str:
        backup_id = f"backup-{uuid.uuid4().hex}"
        with closing(self.connect()) as connection:
            connection.execute(
                """INSERT INTO backup_runs(
                       backup_id,backup_path,source_bytes,backup_bytes,quick_check,restored_count_json,
                       status,created_at,verified_at,error,manifest_path,components_json,snapshot_kind
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    backup_id,
                    str(backup_path),
                    source_bytes,
                    None,
                    None,
                    None,
                    "RUNNING",
                    utc_now(),
                    None,
                    None,
                    str(manifest_path) if manifest_path else None,
                    None,
                    snapshot_kind,
                ),
            )
            connection.commit()
        return backup_id

    def finish_backup_run(
        self,
        backup_id: str,
        *,
        backup_bytes: int,
        quick_check: str,
        counts: dict[str, int],
        manifest_path: Path | None = None,
        components: dict[str, Any] | None = None,
        snapshot_kind: str | None = None,
    ) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                """UPDATE backup_runs SET backup_bytes=?,quick_check=?,restored_count_json=?,
                   status='VERIFIED',verified_at=?,manifest_path=COALESCE(?,manifest_path),
                   components_json=COALESCE(?,components_json),snapshot_kind=COALESCE(?,snapshot_kind)
                   WHERE backup_id=?""",
                (
                    backup_bytes,
                    quick_check,
                    _stable_json(counts),
                    utc_now(),
                    str(manifest_path) if manifest_path else None,
                    _stable_json(components) if components is not None else None,
                    snapshot_kind,
                    backup_id,
                ),
            )
            connection.commit()

    def fail_backup_run(self, backup_id: str, error: str) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE backup_runs SET status='FAILED',error=?,verified_at=? WHERE backup_id=?",
                (error[:2000], utc_now(), backup_id),
            )
            connection.commit()

    def reconcile_abandoned_backup_runs(self, *, exclusive_owner: str) -> dict[str, Any]:
        """Close orphaned ``RUNNING`` backup receipts under an external lock.

        The caller must already hold the one-per-backup-root workflow lock.
        The database transaction is deliberately immediate so every stale row
        receives the same terminal timestamp and error receipt atomically; it
        never touches a record in any terminal status.
        """
        if not isinstance(exclusive_owner, str) or not exclusive_owner.strip():
            raise ValueError("an exclusive backup workflow owner is required")
        reconciled_at = utc_now()
        error = (
            "ABANDONED_RUNNING_BACKUP_RECONCILED "
            f"at={reconciled_at}; exclusive_owner={exclusive_owner.strip()}"
        )[:2000]
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            backup_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT backup_id FROM backup_runs WHERE status='RUNNING' ORDER BY created_at,backup_id"
                ).fetchall()
            ]
            if backup_ids:
                connection.executemany(
                    """UPDATE backup_runs
                       SET status='FAILED',error=?,verified_at=?
                       WHERE backup_id=? AND status='RUNNING'""",
                    [(error, reconciled_at, backup_id) for backup_id in backup_ids],
                )
            connection.commit()
        return {
            "reconciled": len(backup_ids),
            "backup_ids": backup_ids,
            "reconciled_at": reconciled_at,
            "exclusive_owner": exclusive_owner.strip(),
        }

    def _backup_row(self, where: str = "", params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                f"SELECT * FROM backup_runs {where} ORDER BY created_at DESC LIMIT 1",
                params,
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["restored_counts"] = json.loads(item.pop("restored_count_json")) if item["restored_count_json"] else None
        item["components"] = _safe_json(item.pop("components_json", None), None)
        return item

    def latest_backup(self) -> dict[str, Any] | None:
        """Return the latest attempt, including failures, for operator diagnostics."""
        return self._backup_row()

    def _backup_row_summary(
        self,
        where: str = "",
        params: tuple[Any, ...] = (),
    ) -> dict[str, Any] | None:
        """Return backup clocks and integrity facts without its file inventory.

        Recovery-bundle ``components_json`` is an operator artifact and can be
        several megabytes.  A dashboard snapshot only needs the terminal state,
        verification clock and integrity counters, so it must not deserialize or
        duplicate that protected inventory on every publication.
        """

        with closing(self.connect()) as connection:
            row = connection.execute(
                f"""SELECT backup_id,backup_path,source_bytes,backup_bytes,
                           quick_check,restored_count_json,status,created_at,
                           verified_at,error,manifest_path,snapshot_kind
                    FROM backup_runs {where}
                    ORDER BY created_at DESC LIMIT 1""",
                params,
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["restored_counts"] = (
            json.loads(item.pop("restored_count_json"))
            if item["restored_count_json"]
            else None
        )
        return item

    def latest_backup_summary(self) -> dict[str, Any] | None:
        """Return the latest backup attempt without protected component inventory."""

        return self._backup_row_summary()

    def latest_verified_backup(self) -> dict[str, Any] | None:
        """Return the latest usable recovery point rather than a later failed attempt."""
        return self._backup_row("WHERE status='VERIFIED'")

    def latest_verified_backup_summary(self) -> dict[str, Any] | None:
        """Return the latest recovery point without protected component inventory."""

        return self._backup_row_summary("WHERE status='VERIFIED'")

    def backup_summary(self) -> dict[str, Any]:
        """Separate historical run records from files retained on this host."""
        with closing(self.connect()) as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT backup_path,status FROM backup_runs ORDER BY created_at DESC"
            )]
        retained_daily: set[str] = set()
        protected_daily = 0
        for row in rows:
            if not row.get("backup_path"):
                continue
            path = Path(str(row["backup_path"]))
            try:
                path.stat()
            except PermissionError:
                protected_daily += 1
                continue
            except OSError:
                continue
            retained_daily.add(str(path.resolve()))
        weekly_files: set[str] = set()
        parent_dirs: set[Path] = set()
        for row in rows:
            if not row.get("backup_path"):
                continue
            path = Path(str(row["backup_path"]))
            # New bundles store the manifest inside one direct daily directory;
            # weekly snapshots remain siblings of that directory.
            parent_dirs.add(path.parent.parent if path.name == "manifest.json" else path.parent)
        for parent in parent_dirs:
            weekly_dir = parent / "weekly"
            if not weekly_dir.is_dir():
                continue
            weekly_files.update(str(path.resolve()) for path in weekly_dir.glob("*.sqlite3"))
        return {
            "historical_runs": len(rows),
            "verified_runs": sum(row.get("status") == "VERIFIED" for row in rows),
            "failed_runs": sum(row.get("status") == "FAILED" for row in rows),
            "running_runs": sum(row.get("status") == "RUNNING" for row in rows),
            "retained_daily_files": None if protected_daily else len(retained_daily),
            "retained_daily_files_observable": not protected_daily,
            "protected_daily_records": protected_daily,
            "retained_weekly_files": len(weekly_files),
            "retention_policy": "latest_verified_daily_bundle_only",
        }

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
                    "replay_runs",
                    "model_runs",
                    "worker_cycles",
                    "backup_runs",
                    "agent_decisions",
                    "capture_interpretation_runs",
                    "capture_interpretation_attempts",
                    "light_verification_runs",
                    "formal_mutation_audits",
                    "evidence_objects",
                    "human_overrides",
                    "adjudication_samples",
                    "adjudication_reviews",
                    "adjudication_freezes",
                )
            }
        audit_reconciliation = self.audit_reconciliation_status()
        return {
            "status": "ok"
            if (quick_check == "ok" or not run_integrity_check)
            and audit_reconciliation["status"] == "ok"
            else "degraded",
            "database": str(self.path),
            "schema_version": OPS_SCHEMA_VERSION,
            "quick_check": quick_check,
            "integrity_check_source": "live_scan" if run_integrity_check else "not_run",
            "counts": counts,
            "demo_mode": self.demo_mode(),
            "latest_worker_cycle": self.latest_worker_cycle(),
            "worker_window_24h": self.worker_window(),
            "latest_backup": self.latest_backup(),
            "latest_verified_backup": self.latest_verified_backup(),
            "backup_summary": self.backup_summary(),
            "audit_reconciliation": audit_reconciliation,
        }
