from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.evidence_policy import register_sqlite_integrity_functions


SCHEMA_VERSION = 14

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS event_ledger_schema (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    authority_tier TEXT NOT NULL,
    read_only INTEGER NOT NULL CHECK (read_only IN (0,1)),
    enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_observations (
    observation_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    source_published_at TEXT,
    local_received_at TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    canonical_url TEXT,
    content_sha256 TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    observation_status TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES sources(source_id),
    UNIQUE (source_id, external_id)
);

CREATE TABLE IF NOT EXISTS discovery_leads (
    lead_id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL UNIQUE,
    source_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'PENDING_ENRICHMENT','NEEDS_EVIDENCE','LEAD_NO_SCOPED_EVENT',
        'READY_FOR_CANONICAL','PROMOTED','DUPLICATE','EXCLUDED'
    )),
    proposed_event_family TEXT,
    proposed_event_type TEXT,
    company_name TEXT,
    ticker_at_event TEXT,
    event_date TEXT NOT NULL,
    known_at TEXT NOT NULL,
    claim_action TEXT,
    claim_stage TEXT,
    claim_summary TEXT,
    evidence_url TEXT,
    evidence_passage TEXT,
    evidence_status TEXT,
    source_content_sha256 TEXT NOT NULL,
    matched_keywords_json TEXT NOT NULL,
    admission_reasons_json TEXT NOT NULL,
    admission_contract_version TEXT NOT NULL,
    canonical_event_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    no_trading INTEGER NOT NULL DEFAULT 1 CHECK (no_trading=1),
    FOREIGN KEY (observation_id) REFERENCES raw_observations(observation_id),
    FOREIGN KEY (source_id) REFERENCES sources(source_id),
    FOREIGN KEY (canonical_event_id) REFERENCES canonical_events(event_id)
);

CREATE TABLE IF NOT EXISTS source_revisions (
    revision_id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    revision_no INTEGER NOT NULL,
    revision_kind TEXT NOT NULL CHECK (revision_kind IN ('new','edit','delete')),
    revision_at TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    FOREIGN KEY (observation_id) REFERENCES raw_observations(observation_id),
    FOREIGN KEY (source_id) REFERENCES sources(source_id),
    UNIQUE (observation_id, revision_no)
);

CREATE TABLE IF NOT EXISTS canonical_events (
    event_id TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('candidate','verified','weak','rejected')),
    label_status TEXT NOT NULL CHECK (label_status IN ('candidate','verified','weak','rejected')),
    event_family TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_date TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_updated_at TEXT NOT NULL,
    stable_id TEXT,
    ticker_at_event TEXT,
    company_name TEXT,
    manual_grade TEXT,
    provisional_grade_cap TEXT,
    discovery_source TEXT NOT NULL,
    no_trading INTEGER NOT NULL DEFAULT 1 CHECK (no_trading=1)
);

CREATE TABLE IF NOT EXISTS event_chains (
    chain_id TEXT PRIMARY KEY,
    chain_type TEXT NOT NULL,
    canonical_key TEXT NOT NULL UNIQUE,
    primary_event_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    no_trading INTEGER NOT NULL DEFAULT 1 CHECK (no_trading=1),
    FOREIGN KEY (primary_event_id) REFERENCES canonical_events(event_id)
);

CREATE TABLE IF NOT EXISTS event_chain_members (
    chain_id TEXT NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    chain_role TEXT NOT NULL CHECK (
        chain_role IN ('primary_event','same_episode_support','followup_version','consequence','administrative_control')
    ),
    counts_as_primary_event INTEGER NOT NULL CHECK (counts_as_primary_event IN (0,1)),
    rationale TEXT NOT NULL,
    linked_at TEXT NOT NULL,
    PRIMARY KEY (chain_id,event_id),
    FOREIGN KEY (chain_id) REFERENCES event_chains(chain_id),
    FOREIGN KEY (event_id) REFERENCES canonical_events(event_id)
);

CREATE TABLE IF NOT EXISTS event_versions (
    event_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    changed_at TEXT NOT NULL,
    status TEXT NOT NULL,
    label_status TEXT NOT NULL,
    event_family TEXT NOT NULL,
    event_type TEXT NOT NULL,
    manual_grade TEXT,
    facts_json TEXT NOT NULL,
    change_reason TEXT NOT NULL,
    PRIMARY KEY (event_id, version),
    FOREIGN KEY (event_id) REFERENCES canonical_events(event_id)
);

CREATE TABLE IF NOT EXISTS event_observations (
    event_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    linked_at TEXT NOT NULL,
    PRIMARY KEY (event_id, observation_id),
    FOREIGN KEY (event_id) REFERENCES canonical_events(event_id),
    FOREIGN KEY (observation_id) REFERENCES raw_observations(observation_id)
);

CREATE TABLE IF NOT EXISTS event_evidence (
    evidence_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    evidence_url TEXT NOT NULL,
    filing_date TEXT,
    form TEXT,
    items TEXT,
    evidence_passage TEXT,
    matched_keywords TEXT,
    passage_score INTEGER,
    evidence_status TEXT NOT NULL,
    auto_verification_allowed INTEGER NOT NULL DEFAULT 0 CHECK (auto_verification_allowed=0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES canonical_events(event_id),
    FOREIGN KEY (observation_id) REFERENCES raw_observations(observation_id),
    UNIQUE (event_id, observation_id)
);

CREATE TABLE IF NOT EXISTS event_evidence_relations (
    event_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    event_version INTEGER NOT NULL,
    relation_status TEXT NOT NULL CHECK (relation_status IN (
        'SCOPED_MATCH','HUMAN_CONFIRMED','CONFLICTED','INSUFFICIENT'
    )),
    subject_match INTEGER NOT NULL CHECK (subject_match IN (0,1)),
    event_claim_supported INTEGER NOT NULL CHECK (event_claim_supported IN (0,1)),
    date_coherent INTEGER NOT NULL CHECK (date_coherent IN (0,1)),
    modality TEXT NOT NULL,
    evidence_fingerprint TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    assessed_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (event_id,evidence_id,event_version),
    FOREIGN KEY (event_id) REFERENCES canonical_events(event_id),
    FOREIGN KEY (evidence_id) REFERENCES event_evidence(evidence_id)
);

CREATE TABLE IF NOT EXISTS event_fact_workflow (
    event_id TEXT NOT NULL,
    event_version INTEGER NOT NULL,
    workflow_state TEXT NOT NULL CHECK (workflow_state IN (
        'NEEDS_EVIDENCE','EVIDENCE_READY','NEEDS_HUMAN','DUPLICATE','EXCLUDED'
    )),
    reason_codes_json TEXT NOT NULL,
    evidence_fingerprint TEXT,
    contract_version TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (event_id,event_version),
    FOREIGN KEY (event_id) REFERENCES canonical_events(event_id)
);

CREATE TABLE IF NOT EXISTS event_assessments (
    assessment_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    event_version INTEGER NOT NULL,
    severity_grade TEXT NOT NULL CHECK (severity_grade IN ('S','A++','A','B','C')),
    credibility_tier TEXT NOT NULL CHECK (credibility_tier IN ('P0','P1','P2','P3')),
    r_score INTEGER NOT NULL CHECK (r_score BETWEEN 0 AND 3),
    l_score INTEGER NOT NULL CHECK (l_score BETWEEN 0 AND 3),
    e_score INTEGER NOT NULL CHECK (e_score BETWEEN 0 AND 3),
    c_score INTEGER NOT NULL CHECK (c_score BETWEEN 0 AND 3),
    p_score INTEGER NOT NULL CHECK (p_score BETWEEN 0 AND 3),
    x_score INTEGER NOT NULL CHECK (x_score BETWEEN -3 AND 0),
    score_total INTEGER NOT NULL,
    assessed_by TEXT NOT NULL,
    rationale TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES canonical_events(event_id),
    UNIQUE (event_id, event_version)
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    aliases_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT PRIMARY KEY,
    asset_type TEXT NOT NULL,
    symbol TEXT NOT NULL,
    provider_symbol TEXT NOT NULL,
    venue TEXT NOT NULL DEFAULT '',
    currency TEXT,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (asset_type, provider_symbol, venue)
);

CREATE TABLE IF NOT EXISTS event_entities (
    event_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('SUBJECT','ACTOR','TARGET','LOCATION','AUTHORITY')),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    linked_at TEXT NOT NULL,
    PRIMARY KEY (event_id, entity_id, role),
    FOREIGN KEY (event_id) REFERENCES canonical_events(event_id),
    FOREIGN KEY (entity_id) REFERENCES entities(entity_id)
);

CREATE TABLE IF NOT EXISTS event_asset_impacts (
    impact_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    relation_type TEXT NOT NULL CHECK (relation_type IN ('PRIMARY','SECTOR','SUPPLIER','CUSTOMER','MACRO_PROXY','ECOSYSTEM_PROXY')),
    direction TEXT NOT NULL CHECK (direction IN ('LONG','SHORT','NEUTRAL','ABSTAIN')),
    impact_score INTEGER NOT NULL CHECK (impact_score BETWEEN 0 AND 100),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    reason_codes_json TEXT NOT NULL,
    assessment_source TEXT NOT NULL,
    market_observation_allowed INTEGER NOT NULL DEFAULT 0 CHECK (market_observation_allowed IN (0,1)),
    no_trading INTEGER NOT NULL DEFAULT 1 CHECK (no_trading=1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES canonical_events(event_id),
    FOREIGN KEY (asset_id) REFERENCES assets(asset_id),
    UNIQUE (event_id, asset_id, relation_type)
);

CREATE TABLE IF NOT EXISTS market_jobs (
    market_job_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    observation_window TEXT NOT NULL,
    status TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    completed_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    no_trading INTEGER NOT NULL DEFAULT 1 CHECK (no_trading=1),
    FOREIGN KEY (event_id) REFERENCES canonical_events(event_id),
    FOREIGN KEY (asset_id) REFERENCES assets(asset_id),
    UNIQUE (event_id, asset_id, provider, observation_window)
);

CREATE TABLE IF NOT EXISTS market_event_anchors (
    anchor_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    event_version INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    declared_anchor_kind TEXT,
    reaction_anchor_at TEXT,
    source_published_at TEXT,
    local_received_at TEXT,
    known_at TEXT,
    timestamp_precision TEXT NOT NULL CHECK (timestamp_precision IN (
        'EXACT_TIMESTAMP','DATE_ONLY','MISSING','INVALID'
    )),
    anchor_status TEXT NOT NULL CHECK (anchor_status IN ('EXACT','UNAVAILABLE')),
    anchor_lag_seconds INTEGER,
    unsupported_windows_json TEXT NOT NULL,
    reason_code TEXT,
    contract_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    no_trading INTEGER NOT NULL DEFAULT 1 CHECK (no_trading=1),
    FOREIGN KEY (event_id) REFERENCES canonical_events(event_id),
    FOREIGN KEY (asset_id) REFERENCES assets(asset_id),
    UNIQUE (event_id,event_version,asset_id,provider)
);

CREATE TABLE IF NOT EXISTS market_job_anchor_links (
    market_job_id TEXT PRIMARY KEY,
    anchor_id TEXT NOT NULL,
    offset_seconds INTEGER NOT NULL,
    window_contract_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (market_job_id) REFERENCES market_jobs(market_job_id),
    FOREIGN KEY (anchor_id) REFERENCES market_event_anchors(anchor_id)
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    market_job_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_symbol TEXT NOT NULL,
    data_scope TEXT NOT NULL,
    price TEXT NOT NULL,
    currency TEXT,
    provider_as_of TEXT,
    captured_at TEXT NOT NULL,
    freshness_status TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    read_only INTEGER NOT NULL DEFAULT 1 CHECK (read_only=1),
    no_trading INTEGER NOT NULL DEFAULT 1 CHECK (no_trading=1),
    FOREIGN KEY (market_job_id) REFERENCES market_jobs(market_job_id),
    FOREIGN KEY (event_id) REFERENCES canonical_events(event_id),
    FOREIGN KEY (asset_id) REFERENCES assets(asset_id),
    UNIQUE (market_job_id, captured_at)
);

CREATE TABLE IF NOT EXISTS pipeline_jobs (
    job_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    last_error TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES canonical_events(event_id),
    UNIQUE (event_id, job_type)
);

CREATE TABLE IF NOT EXISTS observation_jobs (
    job_id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    last_error TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (observation_id) REFERENCES raw_observations(observation_id),
    UNIQUE (observation_id, job_type)
);

CREATE INDEX IF NOT EXISTS idx_discovery_leads_status
ON discovery_leads(status,source_id,updated_at);
CREATE INDEX IF NOT EXISTS idx_event_evidence_relations_current
ON event_evidence_relations(event_id,event_version,relation_status);
CREATE INDEX IF NOT EXISTS idx_event_fact_workflow_state
ON event_fact_workflow(workflow_state,updated_at);

CREATE TABLE IF NOT EXISTS event_market_metrics (
    metric_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    stable_id TEXT,
    ticker_at_event TEXT,
    event_date TEXT NOT NULL,
    event_trade_date TEXT,
    benchmark_ticker TEXT,
    metric_name TEXT NOT NULL,
    metric_value TEXT NOT NULL,
    metric_value_type TEXT NOT NULL,
    metric_scope TEXT NOT NULL CHECK (metric_scope='post_event_audit_only'),
    allowed_for_discovery_rank INTEGER NOT NULL DEFAULT 0 CHECK (allowed_for_discovery_rank=0),
    allowed_as_model_feature INTEGER NOT NULL DEFAULT 0 CHECK (allowed_as_model_feature=0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES canonical_events(event_id),
    UNIQUE (event_id, provider, metric_name)
);

CREATE TABLE IF NOT EXISTS alert_outbox (
    outbox_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    event_version INTEGER NOT NULL,
    message_type TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    external_message_id TEXT,
    last_error TEXT,
    FOREIGN KEY (event_id) REFERENCES canonical_events(event_id),
    UNIQUE (event_id, event_version, message_type)
);

CREATE TABLE IF NOT EXISTS alert_delivery_attempts (
    attempt_id TEXT PRIMARY KEY,
    outbox_id TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('sendMessage','editMessageText')),
    outcome TEXT NOT NULL CHECK (outcome IN ('sent','error')),
    response_json TEXT,
    error_text TEXT,
    FOREIGN KEY (outbox_id) REFERENCES alert_outbox(outbox_id)
);

CREATE TABLE IF NOT EXISTS alert_delivery_leases (
    outbox_id TEXT PRIMARY KEY,
    lease_token TEXT NOT NULL UNIQUE,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (outbox_id) REFERENCES alert_outbox(outbox_id)
);

CREATE TABLE IF NOT EXISTS alert_delivery_cleanup (
    cleanup_id TEXT PRIMARY KEY,
    outbox_id TEXT NOT NULL,
    external_message_id TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('deleted','error')),
    response_json TEXT,
    error_text TEXT,
    FOREIGN KEY (outbox_id) REFERENCES alert_outbox(outbox_id),
    UNIQUE (outbox_id, external_message_id)
);

CREATE TABLE IF NOT EXISTS runtime_leases (
    lease_name TEXT PRIMARY KEY,
    lease_token TEXT NOT NULL UNIQUE,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_cursors (
    source_id TEXT PRIMARY KEY,
    cursor_type TEXT NOT NULL,
    cursor_value TEXT,
    etag TEXT,
    last_modified TEXT,
    last_polled_at TEXT,
    last_success_at TEXT,
    status TEXT NOT NULL,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES sources(source_id)
);

CREATE TABLE IF NOT EXISTS sec_filing_enrichments (
    enrichment_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    observation_id TEXT NOT NULL UNIQUE,
    accession_number TEXT NOT NULL,
    form TEXT NOT NULL,
    filing_index_url TEXT NOT NULL,
    primary_document_url TEXT,
    documents_json TEXT NOT NULL,
    evidence_excerpt TEXT NOT NULL,
    text_sha256 TEXT,
    matched_event_family TEXT,
    matched_event_type TEXT,
    matched_keywords_json TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    status TEXT NOT NULL CHECK (status IN ('PARSED','NO_DOCUMENT','ERROR')),
    attempts INTEGER NOT NULL,
    last_error TEXT,
    fetched_at TEXT,
    updated_at TEXT NOT NULL,
    read_only INTEGER NOT NULL DEFAULT 1 CHECK (read_only=1),
    no_trading INTEGER NOT NULL DEFAULT 1 CHECK (no_trading=1),
    FOREIGN KEY (event_id) REFERENCES canonical_events(event_id),
    FOREIGN KEY (observation_id) REFERENCES raw_observations(observation_id)
);

CREATE TABLE IF NOT EXISTS event_review_triage (
    event_id TEXT PRIMARY KEY,
    event_version INTEGER NOT NULL,
    review_score INTEGER NOT NULL CHECK (review_score BETWEEN 0 AND 100),
    review_bucket TEXT NOT NULL,
    direction_status TEXT NOT NULL,
    evidence_readiness TEXT NOT NULL,
    severity_ceiling TEXT NOT NULL,
    reversibility_flag TEXT NOT NULL,
    next_action TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    no_trading INTEGER NOT NULL DEFAULT 1 CHECK (no_trading=1),
    FOREIGN KEY (event_id) REFERENCES canonical_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_raw_observations_received
    ON raw_observations(local_received_at);
CREATE INDEX IF NOT EXISTS idx_source_revisions_observation
    ON source_revisions(observation_id, revision_no);
CREATE INDEX IF NOT EXISTS idx_event_chain_members_chain
    ON event_chain_members(chain_id, chain_role);
CREATE INDEX IF NOT EXISTS idx_events_status_date
    ON canonical_events(status, event_date);
CREATE INDEX IF NOT EXISTS idx_events_stable_id
    ON canonical_events(stable_id);
CREATE INDEX IF NOT EXISTS idx_evidence_event
    ON event_evidence(event_id);
CREATE INDEX IF NOT EXISTS idx_assessments_event
    ON event_assessments(event_id, event_version);
CREATE INDEX IF NOT EXISTS idx_event_entities_event
    ON event_entities(event_id);
CREATE INDEX IF NOT EXISTS idx_event_asset_impacts_event
    ON event_asset_impacts(event_id, market_observation_allowed);
CREATE INDEX IF NOT EXISTS idx_market_jobs_status
    ON market_jobs(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_market_event_anchors_status
    ON market_event_anchors(anchor_status,event_id,event_version);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_event
    ON market_snapshots(event_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_jobs_status_priority
    ON pipeline_jobs(status, priority DESC, available_at);
CREATE INDEX IF NOT EXISTS idx_observation_jobs_status_priority
    ON observation_jobs(status, priority DESC, available_at);
CREATE INDEX IF NOT EXISTS idx_market_metrics_event
    ON event_market_metrics(event_id, metric_name);
CREATE INDEX IF NOT EXISTS idx_outbox_status
    ON alert_outbox(status, created_at);
CREATE INDEX IF NOT EXISTS idx_delivery_attempts_outbox
    ON alert_delivery_attempts(outbox_id, attempted_at);
CREATE INDEX IF NOT EXISTS idx_delivery_cleanup_outbox
    ON alert_delivery_cleanup(outbox_id, attempted_at);
CREATE INDEX IF NOT EXISTS idx_sec_enrichments_status
    ON sec_filing_enrichments(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_event_review_triage_score
    ON event_review_triage(review_bucket, review_score DESC);

CREATE VIEW IF NOT EXISTS latest_source_content AS
SELECT
    r.observation_id,
    r.source_id,
    r.external_id,
    CASE
      WHEN r.source_id='opennews_free' AND json_valid(sr.raw_json)
      THEN COALESCE(
        NULLIF(TRIM(json_extract(sr.raw_json,'$.item.published_at')),''),
        NULLIF(TRIM(json_extract(sr.raw_json,'$.item.created_at')),''),
        NULLIF(TRIM(json_extract(sr.raw_json,'$.item.posted_at')),''),
        r.source_published_at
      )
      ELSE r.source_published_at
    END AS source_published_at,
    r.local_received_at,
    COALESCE(sr.title,r.title) AS title,
    COALESCE(sr.summary,r.summary) AS summary,
    CASE
      WHEN r.source_id='opennews_free' AND json_valid(sr.raw_json)
      THEN COALESCE(
        NULLIF(TRIM(json_extract(sr.raw_json,'$.item.link')),''),
        NULLIF(TRIM(json_extract(sr.raw_json,'$.item.url')),''),
        r.canonical_url
      )
      ELSE r.canonical_url
    END AS canonical_url,
    COALESCE(sr.content_sha256,r.content_sha256) AS content_sha256,
    COALESCE(sr.raw_json,r.raw_json) AS raw_json,
    CASE WHEN sr.revision_kind='delete' THEN 'deleted' ELSE r.observation_status END AS observation_status,
    COALESCE(sr.revision_no,0) AS latest_revision_no,
    COALESCE(sr.revision_kind,'new') AS latest_revision_kind,
    COALESCE(sr.revision_at,r.local_received_at) AS latest_revision_at
FROM raw_observations r
LEFT JOIN source_revisions sr
  ON sr.observation_id=r.observation_id
 AND sr.revision_no=(
     SELECT MAX(sr2.revision_no)
     FROM source_revisions sr2
     WHERE sr2.observation_id=r.observation_id
 );
"""

LATEST_SOURCE_CONTENT_VIEW_DDL = SCHEMA[
    SCHEMA.rfind("CREATE VIEW IF NOT EXISTS latest_source_content") :
].strip()


@dataclass(frozen=True)
class ImportCounts:
    queue_rows: int
    events: int
    observations: int
    evidence_rows: int
    market_metrics: int
    adjudicated_events: int
    jobs: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:32]}"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def has_event_ledger_schema(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_ledger_schema'"
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def backup_database(path: Path, backup_dir: Path) -> Path | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"finance_radar_before_event_ledger_{stamp}.sqlite3"
    source = sqlite3.connect(path)
    target = sqlite3.connect(backup_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return backup_path


def open_ledger(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    register_sqlite_integrity_functions(connection)
    connection.executescript(SCHEMA)
    view_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='latest_source_content'"
    ).fetchone()
    view_sql = str(view_row["sql"] or "") if view_row is not None else ""
    if "$.item.published_at" not in view_sql:
        # Existing ledgers retain CREATE VIEW IF NOT EXISTS definitions.  Upgrade
        # this projection once, under a writer lock, instead of churning schema
        # on every API/worker connection.
        try:
            connection.execute("BEGIN IMMEDIATE")
            locked_view = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='view' AND name='latest_source_content'"
            ).fetchone()
            locked_sql = str(locked_view["sql"] or "") if locked_view is not None else ""
            if "$.item.published_at" not in locked_sql:
                connection.execute("DROP VIEW IF EXISTS latest_source_content")
                connection.execute(LATEST_SOURCE_CONTENT_VIEW_DDL)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    connection.execute(
        "INSERT OR IGNORE INTO event_ledger_schema(version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION, utc_now()),
    )
    connection.commit()
    return connection


def upsert_source(
    connection: sqlite3.Connection,
    *,
    source_id: str,
    name: str,
    source_type: str,
    authority_tier: str,
) -> None:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO sources(source_id,name,source_type,authority_tier,read_only,enabled,created_at,updated_at)
        VALUES (?,?,?,?,1,1,?,?)
        ON CONFLICT(source_id) DO UPDATE SET
            name=excluded.name,
            source_type=excluded.source_type,
            authority_tier=excluded.authority_tier,
            read_only=1,
            updated_at=excluded.updated_at
        """,
        (source_id, name, source_type, authority_tier, now, now),
    )


def upsert_event_chain(
    connection: sqlite3.Connection,
    *,
    chain_id: str,
    chain_type: str,
    canonical_key: str,
) -> None:
    now = utc_now()
    connection.execute(
        """INSERT INTO event_chains(
               chain_id,chain_type,canonical_key,primary_event_id,created_at,updated_at,no_trading
           ) VALUES (?,?,?,NULL,?,?,1)
           ON CONFLICT(chain_id) DO UPDATE SET
             chain_type=excluded.chain_type,
             canonical_key=excluded.canonical_key,
             updated_at=excluded.updated_at,
             no_trading=1""",
        (chain_id, chain_type, canonical_key, now, now),
    )


def link_event_chain_member(
    connection: sqlite3.Connection,
    *,
    chain_id: str,
    event_id: str,
    chain_role: str,
    counts_as_primary_event: bool,
    rationale: str,
) -> None:
    if (chain_role == "primary_event") != bool(counts_as_primary_event):
        raise ValueError(
            "Only primary_event may set counts_as_primary_event, and it must set it"
        )
    now = utc_now()
    connection.execute(
        """INSERT INTO event_chain_members(
               chain_id,event_id,chain_role,counts_as_primary_event,rationale,linked_at
           ) VALUES (?,?,?,?,?,?)
           ON CONFLICT(event_id) DO UPDATE SET
             chain_id=excluded.chain_id,
             chain_role=excluded.chain_role,
             counts_as_primary_event=excluded.counts_as_primary_event,
             rationale=excluded.rationale,
             linked_at=excluded.linked_at""",
        (
            chain_id,
            event_id,
            chain_role,
            int(counts_as_primary_event),
            rationale,
            now,
        ),
    )
    if chain_role == "primary_event":
        existing = connection.execute(
            "SELECT primary_event_id FROM event_chains WHERE chain_id=?", (chain_id,)
        ).fetchone()
        if existing is None:
            raise ValueError(f"Unknown event chain: {chain_id}")
        if existing["primary_event_id"] not in {None, event_id}:
            raise ValueError(f"Event chain {chain_id} already has a different primary event")
        connection.execute(
            "UPDATE event_chains SET primary_event_id=?,updated_at=? WHERE chain_id=?",
            (event_id, now, chain_id),
        )


def get_source_cursor(
    connection: sqlite3.Connection, source_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM source_cursors WHERE source_id=?", (source_id,)
    ).fetchone()


def record_source_poll(
    connection: sqlite3.Connection,
    *,
    source_id: str,
    cursor_type: str,
    cursor_value: str | None,
    status: str,
    etag: str | None = None,
    last_modified: str | None = None,
    error: str | None = None,
    polled_at: str | None = None,
) -> None:
    """Persist a source checkpoint without treating transport state as event data."""
    now = polled_at or utc_now()
    success_at = now if status in {"SUCCESS", "NOT_MODIFIED"} else None
    connection.execute(
        """
        INSERT INTO source_cursors(
            source_id,cursor_type,cursor_value,etag,last_modified,last_polled_at,
            last_success_at,status,last_error,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source_id) DO UPDATE SET
            cursor_type=excluded.cursor_type,
            cursor_value=COALESCE(excluded.cursor_value,source_cursors.cursor_value),
            etag=COALESCE(excluded.etag,source_cursors.etag),
            last_modified=COALESCE(excluded.last_modified,source_cursors.last_modified),
            last_polled_at=excluded.last_polled_at,
            last_success_at=COALESCE(excluded.last_success_at,source_cursors.last_success_at),
            status=excluded.status,
            last_error=excluded.last_error,
            updated_at=excluded.updated_at
        """,
        (
            source_id,
            cursor_type,
            cursor_value,
            etag,
            last_modified,
            now,
            success_at,
            status,
            error,
            now,
        ),
    )


def record_source_observation(
    connection: sqlite3.Connection,
    *,
    source_id: str,
    external_id: str,
    source_published_at: str | None,
    local_received_at: str,
    title: str,
    summary: str,
    canonical_url: str | None,
    content_sha256: str,
    raw_json: str,
    revision_kind: str,
    revision_at: str | None = None,
) -> tuple[str, bool]:
    """Store an immutable first observation and append a deduplicated revision."""
    if revision_kind not in {"new", "edit", "delete"}:
        raise ValueError(f"Unsupported revision kind: {revision_kind}")
    observation_id = stable_id("OBS", source_id, external_id)
    connection.execute(
        """
        INSERT OR IGNORE INTO raw_observations(
            observation_id,source_id,external_id,source_published_at,local_received_at,
            title,summary,canonical_url,content_sha256,raw_json,observation_status
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            observation_id,
            source_id,
            external_id,
            source_published_at,
            local_received_at,
            title,
            summary,
            canonical_url,
            content_sha256,
            raw_json,
            "captured",
        ),
    )

    latest = connection.execute(
        """SELECT revision_no, revision_kind, content_sha256
           FROM source_revisions
           WHERE observation_id=?
           ORDER BY revision_no DESC LIMIT 1""",
        (observation_id,),
    ).fetchone()
    if latest is not None:
        same_live_content = (
            latest["content_sha256"] == content_sha256
            and latest["revision_kind"] != "delete"
            and revision_kind != "delete"
        )
        if same_live_content or (
            latest["revision_kind"] == revision_kind
            and latest["content_sha256"] == content_sha256
        ):
            return observation_id, False
        revision_no = int(latest["revision_no"]) + 1
    else:
        revision_no = 1
        revision_kind = "new" if revision_kind != "delete" else revision_kind

    revision_id = stable_id(
        "REV", observation_id, str(revision_no), revision_kind, content_sha256
    )
    connection.execute(
        """
        INSERT INTO source_revisions(
            revision_id,observation_id,source_id,external_id,revision_no,revision_kind,
            revision_at,content_sha256,title,summary,raw_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            revision_id,
            observation_id,
            source_id,
            external_id,
            revision_no,
            revision_kind,
            revision_at or local_received_at,
            content_sha256,
            title,
            summary,
            raw_json,
        ),
    )
    return observation_id, True


def enqueue_observation_job(
    connection: sqlite3.Connection,
    *,
    observation_id: str,
    job_type: str,
    priority: int,
    payload: dict[str, Any],
) -> bool:
    now = utc_now()
    job_id = stable_id("OJOB", observation_id, job_type)
    existing = connection.execute(
        "SELECT status,payload_json FROM observation_jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if existing is not None:
        try:
            previous_payload = json.loads(existing["payload_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            previous_payload = {}
        previous_hash = str(previous_payload.get("source_content_sha256") or "")
        current_hash = str(payload.get("source_content_sha256") or "")
        # A semantic source revision must be extracted again.  Provider ranking
        # metadata is intentionally absent from the semantic hash, so harmless
        # score changes remain idempotent.
        if current_hash and current_hash != previous_hash:
            connection.execute(
                """UPDATE observation_jobs
                   SET status='PENDING',priority=?,attempts=0,available_at=?,
                       last_error=NULL,payload_json=?,updated_at=?
                   WHERE job_id=?""",
                (priority, now, stable_json(payload), now, job_id),
            )
            return True
        return False
    before = connection.total_changes
    connection.execute(
        """
        INSERT OR IGNORE INTO observation_jobs(
            job_id,observation_id,job_type,status,priority,attempts,available_at,
            last_error,payload_json,created_at,updated_at
        ) VALUES (?,?,?,'PENDING',?,0,?,NULL,?,?,?)
        """,
        (
            job_id,
            observation_id,
            job_type,
            priority,
            now,
            stable_json(payload),
            now,
            now,
        ),
    )
    return connection.total_changes > before


def canonical_event_id(event_candidate_id: str) -> str:
    return f"FR-HIST-{event_candidate_id}"


def import_active_research(
    connection: sqlite3.Connection,
    *,
    queue_rows: list[dict[str, str]],
    passage_rows: list[dict[str, str]],
    adjudication_rows: list[dict[str, str]],
    market_rows: list[dict[str, str]] | None = None,
) -> ImportCounts:
    upsert_source(
        connection,
        source_id="sharadar_active_research",
        name="Sharadar active historical discovery",
        source_type="historical_candidate",
        authority_tier="P2_candidate",
    )
    upsert_source(
        connection,
        source_id="sec_edgar",
        name="SEC EDGAR",
        source_type="official_primary",
        authority_tier="P0",
    )
    upsert_source(
        connection,
        source_id="external_official_primary",
        name="Registered external official primary evidence",
        source_type="official_primary",
        authority_tier="P0",
    )
    now = utc_now()
    adjudications = {row["event_candidate_id"]: row for row in adjudication_rows}
    passages_by_event: dict[str, list[dict[str, str]]] = {}
    for row in passage_rows:
        passages_by_event.setdefault(row["event_candidate_id"], []).append(row)
    market_rows = market_rows or []

    for queue in queue_rows:
        candidate_id = queue["event_candidate_id"]
        event_id = canonical_event_id(candidate_id)
        observation_id = stable_id("OBS", "sharadar_active_research", candidate_id)
        queue_payload = stable_json(queue)
        content_hash = hashlib.sha256(queue_payload.encode("utf-8")).hexdigest()
        connection.execute(
            """
            INSERT OR IGNORE INTO raw_observations(
                observation_id,source_id,external_id,source_published_at,local_received_at,
                title,summary,canonical_url,content_sha256,raw_json,observation_status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                observation_id,
                "sharadar_active_research",
                candidate_id,
                queue["event_date"],
                now,
                f"{queue['ticker_at_event']} {queue['event_type']} candidate",
                f"{queue['detection_rule']}; value={queue['detection_value']}",
                queue.get("sec_filings_url") or None,
                content_hash,
                queue_payload,
                "captured",
            ),
        )

        adjudication = adjudications.get(candidate_id)
        if adjudication:
            status = adjudication["label_status"]
            event_family = adjudication["canonical_event_family"]
            event_type = adjudication["canonical_event_type"]
            manual_grade = adjudication["manual_grade"]
        else:
            status = "candidate"
            event_family = queue["event_family"]
            event_type = queue["event_type"]
            manual_grade = None

        existing_event = connection.execute(
            """
            SELECT current_version,status,label_status,event_family,event_type,manual_grade
            FROM canonical_events
            WHERE event_id=?
            """,
            (event_id,),
        ).fetchone()
        desired_state = (
            status,
            status,
            event_family,
            event_type,
            manual_grade,
        )
        if existing_event is None:
            current_version = 2 if adjudication else 1
            adjudication_version = 2 if adjudication else None
            adjudication_change_reason = "manual_primary_evidence_adjudication"
        else:
            existing_state = (
                existing_event["status"],
                existing_event["label_status"],
                existing_event["event_family"],
                existing_event["event_type"],
                existing_event["manual_grade"],
            )
            state_changed = existing_state != desired_state
            current_version = int(existing_event["current_version"]) + int(state_changed)
            adjudication_version = current_version if adjudication and state_changed else None
            adjudication_change_reason = "manual_primary_evidence_readjudication"

        connection.execute(
            """
            INSERT INTO canonical_events(
                event_id,current_version,status,label_status,event_family,event_type,event_date,
                first_seen_at,last_updated_at,stable_id,ticker_at_event,company_name,manual_grade,
                provisional_grade_cap,discovery_source,no_trading
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
            ON CONFLICT(event_id) DO UPDATE SET
                current_version=excluded.current_version,
                status=excluded.status,
                label_status=excluded.label_status,
                event_family=excluded.event_family,
                event_type=excluded.event_type,
                last_updated_at=excluded.last_updated_at,
                manual_grade=excluded.manual_grade,
                provisional_grade_cap=excluded.provisional_grade_cap,
                no_trading=1
            """,
            (
                event_id,
                current_version,
                status,
                status,
                event_family,
                event_type,
                queue["event_date"],
                now,
                now,
                queue.get("stable_id"),
                queue.get("ticker_at_event"),
                queue.get("company_name"),
                manual_grade,
                queue.get("provisional_grade_cap"),
                "sharadar_active_research",
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO event_versions(
                event_id,version,changed_at,status,label_status,event_family,event_type,
                manual_grade,facts_json,change_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id,
                1,
                now,
                "candidate",
                "candidate",
                queue["event_family"],
                queue["event_type"],
                None,
                queue_payload,
                "active_historical_discovery",
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO event_observations(event_id,observation_id,relation_type,linked_at)
            VALUES (?,?,?,?)
            """,
            (event_id, observation_id, "discovery_candidate", now),
        )
        if adjudication and adjudication_version is not None:
            connection.execute(
                """
                INSERT OR IGNORE INTO event_versions(
                    event_id,version,changed_at,status,label_status,event_family,event_type,
                    manual_grade,facts_json,change_reason
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_id,
                    adjudication_version,
                    now,
                    status,
                    status,
                    event_family,
                    event_type,
                    manual_grade,
                    stable_json(adjudication),
                    adjudication_change_reason,
                ),
            )

        event_passages = passages_by_event.get(candidate_id, [])
        for passage in event_passages:
            accession = passage["accession_number"]
            sec_observation_id = stable_id("OBS", "sec_edgar", accession)
            sec_payload = stable_json(passage)
            sec_hash = passage.get("text_sha256") or hashlib.sha256(sec_payload.encode("utf-8")).hexdigest()
            connection.execute(
                """
                INSERT OR IGNORE INTO raw_observations(
                    observation_id,source_id,external_id,source_published_at,local_received_at,
                    title,summary,canonical_url,content_sha256,raw_json,observation_status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    sec_observation_id,
                    "sec_edgar",
                    accession,
                    passage.get("filing_date"),
                    now,
                    f"SEC {passage.get('form')} {queue['ticker_at_event']}",
                    passage.get("evidence_passage", ""),
                    passage.get("filing_document_url"),
                    sec_hash,
                    sec_payload,
                    "candidate_evidence",
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO event_observations(event_id,observation_id,relation_type,linked_at)
                VALUES (?,?,?,?)
                """,
                (event_id, sec_observation_id, "candidate_primary_evidence", now),
            )
            evidence_id = stable_id("EVID", event_id, accession)
            connection.execute(
                """
                INSERT INTO event_evidence(
                    evidence_id,event_id,observation_id,evidence_url,filing_date,form,items,
                    evidence_passage,matched_keywords,passage_score,evidence_status,
                    auto_verification_allowed,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?)
                ON CONFLICT(evidence_id) DO UPDATE SET
                    evidence_passage=excluded.evidence_passage,
                    matched_keywords=excluded.matched_keywords,
                    passage_score=excluded.passage_score,
                    evidence_status=excluded.evidence_status,
                    auto_verification_allowed=0,
                    updated_at=excluded.updated_at
                """,
                (
                    evidence_id,
                    event_id,
                    sec_observation_id,
                    passage.get("filing_document_url", ""),
                    passage.get("filing_date"),
                    passage.get("form"),
                    passage.get("items"),
                    passage.get("evidence_passage"),
                    passage.get("matched_keywords"),
                    int(passage.get("passage_score") or 0),
                    passage.get("passage_status") or "candidate_passage",
                    now,
                    now,
                ),
            )

        # Some reviewed decisions use an official filing or court/exchange source
        # registered outside the bounded SEC passage window.  Preserve that accepted
        # evidence in the ledger instead of leaving the manual verdict with only a
        # facts_json reference.  Existing extracted passages remain the preferred
        # representation and are not duplicated.
        adjudication_url = adjudication.get("evidence_url", "").strip() if adjudication else ""
        passage_urls = {
            passage.get("filing_document_url", "").strip() for passage in event_passages
        }
        if adjudication and adjudication_url and adjudication_url not in passage_urls:
            evidence_source_id = (
                "sec_edgar" if "sec.gov/" in adjudication_url.lower()
                else "external_official_primary"
            )
            external_id = stable_id("EXT", adjudication_url)
            manual_observation_id = stable_id(
                "OBS", evidence_source_id, external_id
            )
            manual_payload = stable_json(adjudication)
            manual_hash = hashlib.sha256(manual_payload.encode("utf-8")).hexdigest()
            connection.execute(
                """
                INSERT OR IGNORE INTO raw_observations(
                    observation_id,source_id,external_id,source_published_at,local_received_at,
                    title,summary,canonical_url,content_sha256,raw_json,observation_status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    manual_observation_id,
                    evidence_source_id,
                    external_id,
                    adjudication.get("evidence_date") or queue["event_date"],
                    now,
                    f"Accepted official evidence for {queue['ticker_at_event']}",
                    adjudication.get("evidence_summary", ""),
                    adjudication_url,
                    manual_hash,
                    manual_payload,
                    "accepted_manual_evidence",
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO event_observations(event_id,observation_id,relation_type,linked_at)
                VALUES (?,?,?,?)
                """,
                (event_id, manual_observation_id, "accepted_manual_primary_evidence", now),
            )
            manual_evidence_id = stable_id("EVID", event_id, adjudication_url)
            connection.execute(
                """
                INSERT INTO event_evidence(
                    evidence_id,event_id,observation_id,evidence_url,filing_date,form,items,
                    evidence_passage,matched_keywords,passage_score,evidence_status,
                    auto_verification_allowed,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?)
                ON CONFLICT(evidence_id) DO UPDATE SET
                    evidence_passage=excluded.evidence_passage,
                    evidence_status=excluded.evidence_status,
                    auto_verification_allowed=0,
                    updated_at=excluded.updated_at
                """,
                (
                    manual_evidence_id,
                    event_id,
                    manual_observation_id,
                    adjudication_url,
                    adjudication.get("evidence_date") or queue["event_date"],
                    adjudication.get("evidence_form", "official primary"),
                    adjudication.get("evidence_item", ""),
                    adjudication.get("evidence_summary", ""),
                    "manual_adjudication",
                    100,
                    "accepted_manual_primary_evidence",
                    now,
                    now,
                ),
            )
        if adjudication:
            job_status = "COMPLETED_MANUAL_ADJUDICATION"
        elif event_passages:
            job_status = "PENDING_EVIDENCE_REVIEW"
        else:
            job_status = "PENDING_PRIMARY_EVIDENCE"
        job_id = stable_id("JOB", event_id, "historical_evidence_review")
        connection.execute(
            """
            INSERT INTO pipeline_jobs(
                job_id,event_id,job_type,status,priority,attempts,available_at,last_error,
                payload_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,0,?,NULL,?,?,?)
            ON CONFLICT(event_id,job_type) DO UPDATE SET
                status=excluded.status,
                priority=excluded.priority,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (
                job_id,
                event_id,
                "historical_evidence_review",
                job_status,
                int(float(queue["priority_score"])),
                now,
                stable_json({"queue_rank": queue["queue_rank"], "candidate_id": candidate_id}),
                now,
                now,
            ),
        )

    for market in market_rows:
        event_id = canonical_event_id(market["event_candidate_id"])
        exists = connection.execute(
            "SELECT 1 FROM canonical_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if exists is None:
            continue
        if market.get("allowed_for_discovery_rank", "false").lower() != "false":
            raise ValueError("Market outcome cannot be allowed in discovery ranking")
        if market.get("allowed_as_model_feature", "false").lower() != "false":
            raise ValueError("Post-event market outcome cannot be a current-event model feature")
        metric_id = stable_id(
            "MKT",
            event_id,
            market.get("provider", "sharadar"),
            market["metric_name"],
        )
        connection.execute(
            """
            INSERT INTO event_market_metrics(
                metric_id,event_id,provider,stable_id,ticker_at_event,event_date,event_trade_date,
                benchmark_ticker,metric_name,metric_value,metric_value_type,metric_scope,
                allowed_for_discovery_rank,allowed_as_model_feature,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0,?,?)
            ON CONFLICT(event_id,provider,metric_name) DO UPDATE SET
                metric_value=excluded.metric_value,
                metric_value_type=excluded.metric_value_type,
                metric_scope='post_event_audit_only',
                allowed_for_discovery_rank=0,
                allowed_as_model_feature=0,
                updated_at=excluded.updated_at
            """,
            (
                metric_id,
                event_id,
                market.get("provider", "sharadar"),
                market.get("stable_id"),
                market.get("ticker_at_event"),
                market["event_date"],
                market.get("event_trade_date"),
                market.get("benchmark_ticker"),
                market["metric_name"],
                market["metric_value"],
                market["metric_value_type"],
                market.get("metric_scope", "post_event_audit_only"),
                now,
                now,
            ),
        )

    connection.commit()
    counts = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in [
            "canonical_events",
            "raw_observations",
            "event_evidence",
            "event_market_metrics",
            "pipeline_jobs",
        ]
    }
    return ImportCounts(
        queue_rows=len(queue_rows),
        events=counts["canonical_events"],
        observations=counts["raw_observations"],
        evidence_rows=counts["event_evidence"],
        market_metrics=counts["event_market_metrics"],
        adjudicated_events=len(adjudication_rows),
        jobs=counts["pipeline_jobs"],
    )


def ledger_summary(connection: sqlite3.Connection) -> dict[str, Any]:
    tables = [
        "sources",
        "raw_observations",
        "source_revisions",
        "canonical_events",
        "event_chains",
        "event_chain_members",
        "event_versions",
        "event_observations",
        "event_evidence",
        "discovery_leads",
        "event_evidence_relations",
        "event_fact_workflow",
        "event_assessments",
        "entities",
        "assets",
        "event_entities",
        "event_asset_impacts",
        "market_jobs",
        "market_event_anchors",
        "market_job_anchor_links",
        "market_snapshots",
        "event_market_metrics",
        "observation_jobs",
        "pipeline_jobs",
        "alert_outbox",
        "alert_delivery_attempts",
        "alert_delivery_leases",
        "alert_delivery_cleanup",
        "runtime_leases",
        "source_cursors",
        "sec_filing_enrichments",
        "event_review_triage",
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "table_counts": {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        },
        "event_status": {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT status, COUNT(*) FROM canonical_events GROUP BY status ORDER BY status"
            )
        },
        "job_status": {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT status, COUNT(*) FROM pipeline_jobs GROUP BY status ORDER BY status"
            )
        },
        "no_trading_violations": connection.execute(
            "SELECT COUNT(*) FROM canonical_events WHERE no_trading != 1"
        ).fetchone()[0],
        "auto_verification_violations": connection.execute(
            "SELECT COUNT(*) FROM event_evidence WHERE auto_verification_allowed != 0"
        ).fetchone()[0],
        "market_metric_scope_violations": connection.execute(
            """SELECT COUNT(*) FROM event_market_metrics
               WHERE metric_scope != 'post_event_audit_only'
                  OR allowed_for_discovery_rank != 0
                  OR allowed_as_model_feature != 0"""
        ).fetchone()[0],
    }
