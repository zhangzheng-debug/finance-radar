from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


OPS_SCHEMA_VERSION = 4
DEMO_MODES = {"LIVE", "RECENT_CAPTURE", "REPLAY"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class OperationsRepository:
    """Mutable operational state kept separate from the immutable research ledger."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def initialize(self) -> None:
        with closing(self.connect()) as connection:
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
                    run_id TEXT PRIMARY KEY, event_id TEXT, input_sha256 TEXT NOT NULL,
                    model_version TEXT NOT NULL, output_label TEXT NOT NULL,
                    confidence REAL NOT NULL, latency_ms REAL NOT NULL,
                    shadow INTEGER NOT NULL CHECK(shadow IN (0,1)), created_at TEXT NOT NULL,
                    output_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_model_event ON model_runs(event_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS worker_cycles(
                    cycle_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                    status TEXT NOT NULL, result_json TEXT NOT NULL, error TEXT
                );
                CREATE TABLE IF NOT EXISTS backup_runs(
                    backup_id TEXT PRIMARY KEY, backup_path TEXT NOT NULL, source_bytes INTEGER NOT NULL,
                    backup_bytes INTEGER, quick_check TEXT, restored_count_json TEXT,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, verified_at TEXT, error TEXT
                );
                CREATE TABLE IF NOT EXISTS agent_decisions(
                    decision_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, trace_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL, prompt_version TEXT NOT NULL, model_provider TEXT NOT NULL,
                    model_snapshot TEXT NOT NULL, output_json TEXT NOT NULL,
                    guardrails_json TEXT NOT NULL, tool_calls_json TEXT NOT NULL,
                    evidence_ids_json TEXT NOT NULL, latency_ms REAL NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_event ON agent_decisions(event_id, created_at DESC);
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
        with closing(self.connect()) as connection:
            connection.execute(
                "INSERT INTO model_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    event_id,
                    result["input_sha256"],
                    result["model_version"],
                    result["label"],
                    float(result["confidence"]),
                    float(result["latency_ms"]),
                    1,
                    utc_now(),
                    _stable_json(result),
                ),
            )
            connection.commit()
        return run_id

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

    def create_backup_run(self, backup_path: Path, source_bytes: int) -> str:
        backup_id = f"backup-{uuid.uuid4().hex}"
        with closing(self.connect()) as connection:
            connection.execute(
                "INSERT INTO backup_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                (backup_id, str(backup_path), source_bytes, None, None, None, "RUNNING", utc_now(), None, None),
            )
            connection.commit()
        return backup_id

    def finish_backup_run(self, backup_id: str, *, backup_bytes: int, quick_check: str, counts: dict[str, int]) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                """UPDATE backup_runs SET backup_bytes=?,quick_check=?,restored_count_json=?,
                   status='VERIFIED',verified_at=? WHERE backup_id=?""",
                (backup_bytes, quick_check, _stable_json(counts), utc_now(), backup_id),
            )
            connection.commit()

    def fail_backup_run(self, backup_id: str, error: str) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE backup_runs SET status='FAILED',error=?,verified_at=? WHERE backup_id=?",
                (error[:2000], utc_now(), backup_id),
            )
            connection.commit()

    def latest_backup(self) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT * FROM backup_runs ORDER BY created_at DESC LIMIT 1").fetchone()
        if row is None:
            return None
        item = dict(row)
        item["restored_counts"] = json.loads(item.pop("restored_count_json")) if item["restored_count_json"] else None
        return item

    def health(self) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "replay_runs",
                    "model_runs",
                    "worker_cycles",
                    "backup_runs",
                    "agent_decisions",
                    "evidence_objects",
                    "human_overrides",
                    "adjudication_samples",
                    "adjudication_reviews",
                )
            }
        return {
            "status": "ok" if quick_check == "ok" else "degraded",
            "database": str(self.path),
            "schema_version": OPS_SCHEMA_VERSION,
            "quick_check": quick_check,
            "counts": counts,
            "demo_mode": self.demo_mode(),
            "latest_worker_cycle": self.latest_worker_cycle(),
            "worker_window_24h": self.worker_window(),
            "latest_backup": self.latest_backup(),
        }
