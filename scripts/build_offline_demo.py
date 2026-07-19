#!/usr/bin/env python3
"""Build a secret-scanned, executable, loopback-only Finance Radar demo."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import AdjudicationService
from app.storage import LedgerRepository, OperationsRepository


ZIP_TIME = (2026, 7, 19, 0, 0, 0)
MIN_EVENT_COUNT = 16
MAX_EVENT_COUNT = 22
FORBIDDEN_NAME_PARTS = (
    ".env",
    "id_ed25519",
    "passphrase",
    "private_key",
    "telegram_mtproto",
    "telegram_alert",
    "collector",
    "worker",
    "binance",
    "ibkr",
    ".aesgcm",
)
SECRET_PATTERNS = (
    re.compile(rb"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(rb"(?:TELEGRAM_(?:BOT_TOKEN|API_HASH)|BINANCE_API_SECRET)\s*=\s*[^\s]+", re.I),
    re.compile(rb"-----BEGIN (?:OPENSSH|RSA|EC) PRIVATE KEY-----"),
)
SOURCE_FILES = (
    ".streamlit/config.toml",
    "replay/cases/cases.json",
    "artifacts/risk_router.joblib",
    "artifacts/risk_router.sha256",
    "artifacts/risk_router_model_card.json",
    "artifacts/risk_router_model_card.md",
    "artifacts/risk_router_data_card.json",
    "artifacts/risk_router_robustness.json",
    "artifacts/risk_router_external_blind_v1_report.json",
    "artifacts/risk_router_external_blind_v1_report.md",
    "docs/UI_AESTHETIC_DIRECTION.md",
    "docs/ADJUDICATION_V3_WORKFLOW.md",
    "deployment/offline/README_OFFLINE.md",
    "deployment/offline/requirements-offline.txt",
    "deployment/offline/start_offline_demo.ps1",
    "deployment/offline/stop_offline_demo.ps1",
    "deployment/offline/sitecustomize.py",
    "scripts/verify_offline_demo.py",
)
APP_RUNTIME_ROOTS = ("api", "models", "services", "storage", "web")
EVENT_TABLES = (
    "event_versions",
    "event_observations",
    "event_evidence",
    "event_assessments",
    "event_entities",
    "event_asset_impacts",
    "market_jobs",
    "market_snapshots",
    "event_market_metrics",
    "sec_filing_enrichments",
    "event_review_triage",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _add_unique(target: list[str], rows: Iterable[sqlite3.Row]) -> None:
    for row in rows:
        event_id = str(row["event_id"])
        if event_id not in target and len(target) < MAX_EVENT_COUNT:
            target.append(event_id)


def select_demo_events(connection: sqlite3.Connection) -> list[str]:
    """Choose a small, diverse snapshot without using post-event labels."""
    connection.row_factory = sqlite3.Row
    selected: list[str] = []

    # Preserve any live event that demonstrates explicit asset-impact mapping.
    _add_unique(
        selected,
        connection.execute(
            """SELECT DISTINCT e.event_id
               FROM canonical_events e JOIN event_asset_impacts i ON i.event_id=e.event_id
               WHERE e.event_id LIKE 'FR-LIVE-%'
               ORDER BY e.last_updated_at DESC LIMIT 3"""
        ),
    )

    # Operationally diverse verified events; one per family before repeats.
    _add_unique(
        selected,
        connection.execute(
            """WITH ranked AS (
                   SELECT e.event_id,e.event_family,e.last_updated_at,
                          ROW_NUMBER() OVER(PARTITION BY e.event_family ORDER BY e.last_updated_at DESC) rn
                   FROM canonical_events e
                   WHERE e.event_id LIKE 'FR-LIVE-%' AND e.status='verified'
                     AND EXISTS(SELECT 1 FROM event_evidence x WHERE x.event_id=e.event_id)
               )
               SELECT event_id FROM ranked WHERE rn=1
               ORDER BY last_updated_at DESC LIMIT 10"""
        ),
    )

    # Keep genuine unresolved candidates to show abstention and review queues.
    _add_unique(
        selected,
        connection.execute(
            """WITH ranked AS (
                   SELECT e.event_id,e.event_family,e.last_updated_at,
                          ROW_NUMBER() OVER(PARTITION BY e.event_family ORDER BY e.last_updated_at DESC) rn
                   FROM canonical_events e
                   WHERE e.event_id LIKE 'FR-LIVE-%' AND e.status IN ('candidate','weak')
               )
               SELECT event_id FROM ranked WHERE rn=1
               ORDER BY last_updated_at DESC LIMIT 4"""
        ),
    )

    # Historical accepted and rejected controls with exact evidence and audit metrics.
    for status in ("verified", "rejected"):
        _add_unique(
            selected,
            connection.execute(
                """WITH ranked AS (
                       SELECT e.event_id,e.event_family,e.event_date,
                              ROW_NUMBER() OVER(PARTITION BY e.event_family ORDER BY e.event_date DESC) rn
                       FROM canonical_events e
                       WHERE e.event_id LIKE 'FR-HIST-%' AND e.status=?
                         AND EXISTS(SELECT 1 FROM event_evidence x WHERE x.event_id=e.event_id)
                         AND EXISTS(SELECT 1 FROM event_market_metrics m WHERE m.event_id=e.event_id)
                   )
                   SELECT event_id FROM ranked WHERE rn=1
                   ORDER BY event_date DESC LIMIT 3""",
                (status,),
            ),
        )

    if len(selected) < MIN_EVENT_COUNT:
        _add_unique(
            selected,
            connection.execute(
                """SELECT event_id FROM canonical_events
                   WHERE status='verified'
                   ORDER BY last_updated_at DESC LIMIT 100"""
            ),
        )
    if len(selected) < MIN_EVENT_COUNT:
        raise ValueError(f"cannot build representative snapshot: only {len(selected)} events")
    return selected[:MAX_EVENT_COUNT]


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def build_demo_ledger(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    with sqlite3.connect(source) as source_connection, sqlite3.connect(destination) as target:
        source_connection.backup(target)

    connection = sqlite3.connect(destination)
    connection.row_factory = sqlite3.Row
    selected = select_demo_events(connection)
    placeholders = ",".join("?" for _ in selected)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("BEGIN")
    for table in EVENT_TABLES:
        if _table_exists(connection, table):
            connection.execute(
                f"DELETE FROM {table} WHERE event_id NOT IN ({placeholders})", selected
            )
    for table in (
        "event_chain_members",
        "event_chains",
        "pipeline_jobs",
        "observation_jobs",
        "alert_delivery_attempts",
        "alert_delivery_leases",
        "alert_delivery_cleanup",
        "alert_outbox",
        "runtime_leases",
        "telegram_source_messages",
        "telegram_source_channels",
    ):
        if _table_exists(connection, table):
            connection.execute(f"DELETE FROM {table}")
    connection.execute(
        f"DELETE FROM canonical_events WHERE event_id NOT IN ({placeholders})", selected
    )
    connection.execute(
        """DELETE FROM source_revisions
           WHERE observation_id NOT IN (
               SELECT observation_id FROM event_observations
               UNION SELECT observation_id FROM event_evidence
               UNION SELECT observation_id FROM sec_filing_enrichments
           )"""
    )
    connection.execute(
        """DELETE FROM raw_observations
           WHERE observation_id NOT IN (
               SELECT observation_id FROM event_observations
               UNION SELECT observation_id FROM event_evidence
               UNION SELECT observation_id FROM sec_filing_enrichments
           )"""
    )
    connection.execute(
        """DELETE FROM entities WHERE entity_id NOT IN
           (SELECT entity_id FROM event_entities)"""
    )
    connection.execute(
        """DELETE FROM assets WHERE asset_id NOT IN (
               SELECT asset_id FROM event_asset_impacts
               UNION SELECT asset_id FROM market_jobs
               UNION SELECT asset_id FROM market_snapshots
           )"""
    )
    connection.execute(
        """UPDATE sources SET enabled=0,read_only=1,
           updated_at=MAX(updated_at,created_at)"""
    )
    connection.execute(
        """UPDATE source_cursors SET cursor_value=NULL,etag=NULL,last_modified=NULL,
           status='FROZEN_OFFLINE',last_error=NULL"""
    )
    connection.commit()
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("VACUUM")
    quick = connection.execute("PRAGMA quick_check").fetchone()[0]
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = [dict(row) for row in connection.execute("PRAGMA foreign_key_check")]
    counts = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "sources",
            "raw_observations",
            "canonical_events",
            "event_versions",
            "event_evidence",
            "event_assessments",
            "event_market_metrics",
            "event_asset_impacts",
            "pipeline_jobs",
            "alert_outbox",
        )
    }
    status_counts = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT status,COUNT(*) FROM canonical_events GROUP BY status ORDER BY status"
        )
    }
    boundary = {
        "trading": connection.execute(
            "SELECT COUNT(*) FROM canonical_events WHERE no_trading != 1"
        ).fetchone()[0],
        "auto_verification": connection.execute(
            "SELECT COUNT(*) FROM event_evidence WHERE auto_verification_allowed != 0"
        ).fetchone()[0],
        "market_feature_leakage": connection.execute(
            "SELECT COUNT(*) FROM event_market_metrics WHERE allowed_as_model_feature != 0"
        ).fetchone()[0],
    }
    connection.close()
    if quick != "ok" or integrity != "ok" or foreign_keys or any(boundary.values()):
        raise ValueError(
            f"demo ledger validation failed: quick={quick} integrity={integrity} "
            f"foreign_keys={len(foreign_keys)} boundary={boundary}"
        )
    return {
        "selected_event_ids": selected,
        "counts": counts,
        "status_counts": status_counts,
        "quick_check": quick,
        "integrity_check": integrity,
        "foreign_key_violations": 0,
        "boundary_violations": boundary,
        "bytes": destination.stat().st_size,
        "sha256": sha256_file(destination),
    }


def build_operations(ledger_path: Path, destination: Path) -> dict[str, Any]:
    if destination.exists():
        destination.unlink()
    operations = OperationsRepository(destination)
    operations.set_demo_mode("REPLAY")
    ledger = LedgerRepository(ledger_path)
    health = ledger.health()
    cycle_id = operations.start_worker_cycle()
    operations.finish_worker_cycle(
        cycle_id,
        "SUCCESS",
        {
            "mode": "offline_snapshot_validation",
            "worker_elapsed_ms": 0,
            "ledger": health,
            "official_sources": {},
            "errors": [],
            "telegram": {"mode": "absent_from_bundle"},
            "candidate_extraction": {"new_events": 0},
            "review_triage": {
                "pending_events": health["event_status"].get("candidate", 0)
                + health["event_status"].get("weak", 0)
            },
            "snapshot_not_live_collection": True,
        },
    )
    service = AdjudicationService(ledger, operations)
    seeded = 0
    for event in ledger.list_events(status="verified", limit=30)["items"]:
        if seeded >= 10:
            break
        try:
            seeded += int(service.create_sample_from_event(event["event_id"])["created"])
        except ValueError:
            continue
    with sqlite3.connect(destination) as connection:
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        schema = connection.execute("SELECT MAX(version) FROM operations_schema").fetchone()[0]
        sample_count = connection.execute("SELECT COUNT(*) FROM adjudication_samples").fetchone()[0]
        review_count = connection.execute("SELECT COUNT(*) FROM adjudication_reviews").fetchone()[0]
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode=DELETE")
    if quick != "ok" or schema != 3 or review_count != 0:
        raise ValueError("offline operations database validation failed")
    return {
        "schema_version": schema,
        "quick_check": quick,
        "adjudication_samples": sample_count,
        "adjudication_reviews": review_count,
        "target_labels_fabricated": False,
        "bytes": destination.stat().st_size,
        "sha256": sha256_file(destination),
    }


def _copy_source_tree(stage: Path) -> None:
    app_paths = [ROOT / "app" / "__init__.py", ROOT / "app" / "config.py"]
    for directory in APP_RUNTIME_ROOTS:
        app_paths.extend(sorted((ROOT / "app" / directory).rglob("*.py")))
    for path in app_paths:
        relative = path.relative_to(ROOT)
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    for value in SOURCE_FILES:
        source = ROOT / value
        if not source.is_file():
            raise FileNotFoundError(f"offline source file missing: {value}")
        if value.startswith("deployment/offline/"):
            name = Path(value).name
            target = stage / name
        else:
            target = stage / value
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _safe_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        name = path.relative_to(root).as_posix()
        lowered = name.lower()
        if any(part in lowered for part in FORBIDDEN_NAME_PARTS):
            raise ValueError(f"forbidden capability or secret filename in offline bundle: {name}")
        data = path.read_bytes()
        if any(pattern.search(data) for pattern in SECRET_PATTERNS):
            raise ValueError(f"secret-like value detected in offline bundle: {name}")
        files.append(path)
    return files


def _write_manifest(stage: Path, metadata: dict[str, Any]) -> None:
    metadata_path = stage / "OFFLINE_BUNDLE.json"
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    files = [path for path in _safe_files(stage) if path.name != "MANIFEST.sha256"]
    manifest = "".join(
        f"{sha256_file(path)}  {path.relative_to(stage).as_posix()}\n" for path in files
    )
    (stage / "MANIFEST.sha256").write_text(manifest, encoding="utf-8")


def _zip_directory(root: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    with zipfile.ZipFile(temporary, "w", allowZip64=True) as archive:
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            name = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(name, ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    temporary.replace(destination)


def build_bundle(source_db: Path, output_dir: Path, stamp: str) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="finance-radar-offline-", ignore_cleanup_errors=True
    ) as temporary:
        stage = Path(temporary) / "finance-radar-offline-demo"
        stage.mkdir()
        _copy_source_tree(stage)
        data_dir = stage / "data"
        ledger_report = build_demo_ledger(source_db, data_dir / "finance_radar_demo.sqlite3")
        operations_report = build_operations(
            data_dir / "finance_radar_demo.sqlite3",
            data_dir / "finance_radar_demo_operations.sqlite3",
        )
        backup = data_dir / "snapshot_backup.sqlite3"
        shutil.copy2(data_dir / "finance_radar_demo.sqlite3", backup)
        with sqlite3.connect(backup) as connection:
            backup_quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        if backup_quick != "ok" or sha256_file(backup) != ledger_report["sha256"]:
            raise ValueError("offline snapshot restore copy verification failed")
        metadata = {
            "schema_version": 1,
            "created_at": utc_now(),
            "purpose": "executable offline defense snapshot",
            "mode": "FROZEN_REPLAY_AND_RECENT_CAPTURE",
            "ledger": ledger_report,
            "operations": operations_report,
            "restore_copy": {
                "path": "data/snapshot_backup.sqlite3",
                "quick_check": backup_quick,
                "sha256_matches_primary": True,
            },
            "boundaries": {
                "external_network": "blocked_by_sitecustomize",
                "external_collectors_included": False,
                "telegram_capability_included": False,
                "broker_or_exchange_client_included": False,
                "trading_capability_included": False,
                "credentials_included": False,
                "review_ui_enabled": False,
                "model_mode": "SHADOW",
            },
        }
        _write_manifest(stage, metadata)
        current = output_dir / "current"
        staged_current = output_dir / ".current.next"
        if staged_current.exists():
            shutil.rmtree(staged_current)
        shutil.copytree(stage, staged_current)
        if current.exists():
            shutil.rmtree(current)
        staged_current.replace(current)
        archive = output_dir / f"finance-radar-offline-demo-{stamp}.zip"
        _zip_directory(stage, archive)
        latest_archive = output_dir / "finance-radar-offline-demo-latest.zip"
        shutil.copy2(archive, latest_archive)

    report = {
        "schema_version": 1,
        "created_at": utc_now(),
        "status": "BUILT_PENDING_INDEPENDENT_VERIFY",
        "current_directory": str(current),
        "archive": str(archive),
        "latest_archive": str(latest_archive),
        "archive_bytes": archive.stat().st_size,
        "archive_sha256": sha256_file(archive),
        "ledger": ledger_report,
        "operations": operations_report,
        "boundaries": metadata["boundaries"],
    }
    (output_dir / "offline_demo_build_latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, default=ROOT / "data" / "finance_radar.sqlite3")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts" / "offline_demo")
    parser.add_argument("--stamp", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    args = parser.parse_args()
    if not re.fullmatch(r"\d{8}T\d{6}Z", args.stamp):
        parser.error("stamp must use YYYYMMDDTHHMMSSZ")
    report = build_bundle(args.source_db.resolve(), args.output_dir, args.stamp)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
