from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.storage import OperationsRepository


COUNT_TABLES = (
    "sources",
    "raw_observations",
    "canonical_events",
    "event_versions",
    "event_evidence",
    "event_market_metrics",
)


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def database_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in COUNT_TABLES}


def online_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True, timeout=10)) as source_connection:
        with closing(sqlite3.connect(destination, timeout=10)) as destination_connection:
            source_connection.backup(destination_connection, pages=256)


def verify_restore(backup_path: Path) -> dict[str, Any]:
    """Restore into an isolated temporary database and compare durable row counts."""
    if not backup_path.is_file():
        raise FileNotFoundError(backup_path)
    with tempfile.TemporaryDirectory(prefix="finance-radar-restore-") as temp_dir:
        restored_path = Path(temp_dir) / "restored.sqlite3"
        with closing(sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)) as backup_connection:
            with closing(sqlite3.connect(restored_path)) as restored_connection:
                backup_connection.backup(restored_connection)
        with closing(sqlite3.connect(restored_path)) as restored_connection:
            quick_check = restored_connection.execute("PRAGMA quick_check").fetchone()[0]
            counts = database_counts(restored_connection)
            schema_version = restored_connection.execute(
                "SELECT MAX(version) FROM event_ledger_schema"
            ).fetchone()[0]
        return {
            "quick_check": quick_check,
            "counts": counts,
            "schema_version": schema_version,
            "isolated_restore": True,
        }


def prune_backups(backup_dir: Path, retention: int) -> list[str]:
    files = sorted(backup_dir.glob("finance_radar_*.sqlite3"), key=lambda path: path.stat().st_mtime, reverse=True)
    removed: list[str] = []
    for path in files[max(1, retention):]:
        for suffix in ("", "-wal", "-shm"):
            companion = Path(f"{path}{suffix}")
            if companion.exists():
                companion.unlink()
                removed.append(str(companion))
    return removed


def create_weekly_snapshot(
    verified_backup: Path,
    weekly_dir: Path,
    *,
    retention: int = 8,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Keep the first verified daily backup for each ISO week and retain eight weeks."""
    current = at or datetime.now(timezone.utc)
    iso_year, iso_week, _ = current.isocalendar()
    weekly_path = weekly_dir / f"finance_radar_week_{iso_year}-W{iso_week:02d}.sqlite3"
    created = False
    if not weekly_path.exists():
        weekly_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = weekly_path.with_suffix(".sqlite3.tmp")
        shutil.copy2(verified_backup, temporary)
        temporary.replace(weekly_path)
        created = True
    verification = verify_restore(weekly_path)
    if verification["quick_check"] != "ok":
        raise RuntimeError(f"weekly snapshot quick_check={verification['quick_check']}")
    files = sorted(
        weekly_dir.glob("finance_radar_week_*.sqlite3"),
        key=lambda path: path.name,
        reverse=True,
    )
    removed: list[str] = []
    for path in files[max(1, retention):]:
        path.unlink()
        removed.append(str(path))
    return {
        "status": "CREATED" if created else "RETAINED",
        "backup_path": str(weekly_path),
        "backup_bytes": weekly_path.stat().st_size,
        "verification": verification,
        "pruned": removed,
    }


def create_and_verify(
    source: Path,
    backup_dir: Path,
    operations: OperationsRepository,
    *,
    retention: int = 30,
    weekly_retention: int = 12,
) -> dict[str, Any]:
    if not source.is_file():
        raise FileNotFoundError(source)
    destination = backup_dir / f"finance_radar_{timestamp()}.sqlite3"
    backup_id = operations.create_backup_run(destination, source.stat().st_size)
    try:
        online_backup(source, destination)
        verification = verify_restore(destination)
        if verification["quick_check"] != "ok":
            raise RuntimeError(f"restored database quick_check={verification['quick_check']}")
        with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as source_connection:
            source_counts = database_counts(source_connection)
        if source_counts != verification["counts"]:
            raise RuntimeError(f"restored row counts differ: source={source_counts}, restored={verification['counts']}")
        operations.finish_backup_run(
            backup_id,
            backup_bytes=destination.stat().st_size,
            quick_check=verification["quick_check"],
            counts=verification["counts"],
        )
        removed = prune_backups(backup_dir, retention)
        weekly = create_weekly_snapshot(
            destination,
            backup_dir / "weekly",
            retention=weekly_retention,
        )
        return {
            "backup_id": backup_id,
            "status": "VERIFIED",
            "backup_path": str(destination),
            "backup_bytes": destination.stat().st_size,
            "verification": verification,
            "pruned": removed,
            "weekly_snapshot": weekly,
        }
    except Exception as exc:
        operations.fail_backup_run(backup_id, f"{type(exc).__name__}: {exc}")
        raise


def main() -> int:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("backup", help="create an online backup and isolated restore drill")
    backup_parser.add_argument("--source", type=Path, default=settings.ledger_db)
    backup_parser.add_argument("--backup-dir", type=Path, default=settings.ledger_db.parent / "operational_backups")
    backup_parser.add_argument("--retention", type=int, default=30)
    backup_parser.add_argument("--weekly-retention", type=int, default=12)
    verify_parser = subparsers.add_parser("verify", help="verify an existing backup through an isolated restore")
    verify_parser.add_argument("backup_path", type=Path)
    subparsers.add_parser("status", help="show latest recorded backup drill")
    args = parser.parse_args()
    operations = OperationsRepository(settings.operations_db)
    if args.command == "backup":
        result = create_and_verify(
            args.source,
            args.backup_dir,
            operations,
            retention=args.retention,
            weekly_retention=args.weekly_retention,
        )
    elif args.command == "verify":
        result = verify_restore(args.backup_path)
    else:
        result = operations.latest_backup()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
