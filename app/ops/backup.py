from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

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
OPERATIONS_COUNT_TABLES = (
    "replay_runs",
    "model_runs",
    "worker_cycles",
    "backup_runs",
    "agent_decisions",
    "light_verification_runs",
    "formal_mutation_audits",
    "evidence_objects",
    "human_overrides",
    "adjudication_samples",
    "adjudication_reviews",
)
SNAPSHOT_FORMAT = "finance-radar-recovery-bundle-v1"
DEFAULT_DAILY_RETENTION = 1
DEFAULT_WEEKLY_RETENTION = 0


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_counts(connection: sqlite3.Connection, tables: tuple[str, ...]) -> dict[str, int]:
    return {table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}


def database_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """Durable ledger row counts retained for backward-compatible callers."""
    return _table_counts(connection, COUNT_TABLES)


def operations_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return _table_counts(connection, OPERATIONS_COUNT_TABLES)


def _database_schema_version(connection: sqlite3.Connection, table: str) -> int | None:
    try:
        value = connection.execute(f"SELECT MAX(version) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return None
    return int(value) if value is not None else None


def online_backup(
    source: Path,
    destination: Path,
    *,
    count_reader: Callable[[sqlite3.Connection], dict[str, int]] = database_counts,
) -> dict[str, Any]:
    """Create one SQLite-consistent component copy and capture its source counts.

    SQLite's online backup API is used while a read transaction is open, so the
    copied file and the captured counts describe the same database snapshot.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as source_connection:
        source_connection.execute("PRAGMA query_only=ON")
        source_connection.execute("BEGIN")
        source_counts = count_reader(source_connection)
        with closing(sqlite3.connect(destination, timeout=30)) as destination_connection:
            source_connection.backup(destination_connection, pages=256)
        source_connection.rollback()
    return {
        "source_counts": source_counts,
        "source_bytes": source.stat().st_size,
        "backup_bytes": destination.stat().st_size,
    }


def _verify_database_restore(
    backup_path: Path,
    *,
    count_reader: Callable[[sqlite3.Connection], dict[str, int]],
    schema_table: str,
) -> dict[str, Any]:
    if not backup_path.is_file():
        raise FileNotFoundError(backup_path)
    with tempfile.TemporaryDirectory(prefix="finance-radar-restore-") as temp_dir:
        restored_path = Path(temp_dir) / "restored.sqlite3"
        with closing(sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)) as backup_connection:
            with closing(sqlite3.connect(restored_path)) as restored_connection:
                backup_connection.backup(restored_connection)
        with closing(sqlite3.connect(restored_path)) as restored_connection:
            quick_check = restored_connection.execute("PRAGMA quick_check").fetchone()[0]
            integrity_check = restored_connection.execute("PRAGMA integrity_check").fetchone()[0]
            counts = count_reader(restored_connection)
            schema_version = _database_schema_version(restored_connection, schema_table)
        return {
            "quick_check": quick_check,
            "integrity_check": integrity_check,
            "counts": counts,
            "schema_version": schema_version,
            "isolated_restore": True,
        }


def verify_restore(backup_path: Path) -> dict[str, Any]:
    """Verify a legacy ledger-only backup or a full recovery bundle."""
    if backup_path.is_dir() or backup_path.name == "manifest.json":
        return verify_bundle_restore(backup_path)
    return _verify_database_restore(
        backup_path,
        count_reader=database_counts,
        schema_table="event_ledger_schema",
    )


def _direct_backup_children(backup_dir: Path) -> list[Path]:
    if not backup_dir.exists():
        return []
    children: list[Path] = []
    for path in backup_dir.iterdir():
        if not path.name.startswith("finance_radar_"):
            continue
        if path.is_file() and path.suffix == ".sqlite3":
            children.append(path)
        elif path.is_dir() and (path / "manifest.json").is_file():
            children.append(path)
    return children


def _assert_direct_backup_child(backup_dir: Path, path: Path) -> None:
    if path.parent.resolve() != backup_dir.resolve() or not path.name.startswith("finance_radar_"):
        raise ValueError(f"refusing to remove non-backup path: {path}")


def _remove_backup_path(backup_dir: Path, path: Path) -> list[str]:
    _assert_direct_backup_child(backup_dir, path)
    removed: list[str] = []
    if path.is_symlink() or path.is_file():
        for suffix in ("", "-wal", "-shm", "-journal"):
            companion = Path(f"{path}{suffix}")
            if companion.exists() or companion.is_symlink():
                companion.unlink()
                removed.append(str(companion))
        return removed
    if path.is_dir():
        # The target is a validated direct child of the explicitly supplied
        # backup directory.  Only fully verified new bundles reach retention.
        shutil.rmtree(path)
        return [str(path)]
    return removed


def _remove_staging_path(backup_dir: Path, path: Path) -> None:
    if (
        path.parent.resolve() != backup_dir.resolve()
        or not path.name.startswith(".finance_radar_")
        or not path.name.endswith(".partial")
    ):
        raise ValueError(f"refusing to remove non-staging backup path: {path}")
    if path.exists():
        shutil.rmtree(path)


def prune_backups(backup_dir: Path, retention: int) -> list[str]:
    """Retain exactly the newest complete daily backup sets after a verified run."""
    keep = max(1, int(retention))
    files = sorted(
        _direct_backup_children(backup_dir),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    removed: list[str] = []
    for path in files[keep:]:
        removed.extend(_remove_backup_path(backup_dir, path))
    return removed


def create_weekly_snapshot(
    verified_backup: Path,
    weekly_dir: Path,
    *,
    retention: int = 8,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Optional legacy weekly ledger copies.

    ``create_and_verify`` deliberately passes ``weekly_retention=0`` by default
    for the requested one-latest-daily policy.  This helper keeps its historical
    standalone default for explicit maintenance or migration callers.
    """
    if retention <= 0:
        removed: list[str] = []
        if weekly_dir.exists():
            for path in sorted(weekly_dir.glob("finance_radar_week_*.sqlite3")):
                for suffix in ("", "-wal", "-shm", "-journal"):
                    companion = Path(f"{path}{suffix}")
                    if companion.exists() or companion.is_symlink():
                        companion.unlink()
                        removed.append(str(companion))
        return {
            "status": "DISABLED",
            "backup_path": None,
            "backup_bytes": 0,
            "verification": None,
            "pruned": removed,
        }

    source = verified_backup / "ledger.sqlite3" if verified_backup.is_dir() else verified_backup
    current = at or datetime.now(timezone.utc)
    iso_year, iso_week, _ = current.isocalendar()
    weekly_path = weekly_dir / f"finance_radar_week_{iso_year}-W{iso_week:02d}.sqlite3"
    created = False
    if not weekly_path.exists():
        weekly_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = weekly_path.with_suffix(".sqlite3.tmp")
        shutil.copy2(source, temporary)
        temporary.replace(weekly_path)
        created = True
    verification = _verify_database_restore(
        weekly_path,
        count_reader=database_counts,
        schema_table="event_ledger_schema",
    )
    if verification["quick_check"] != "ok" or verification["integrity_check"] != "ok":
        raise RuntimeError("weekly snapshot restore verification failed")
    files = sorted(weekly_dir.glob("finance_radar_week_*.sqlite3"), key=lambda path: path.name, reverse=True)
    removed: list[str] = []
    for path in files[max(1, retention):]:
        for suffix in ("", "-wal", "-shm", "-journal"):
            companion = Path(f"{path}{suffix}")
            if companion.exists() or companion.is_symlink():
                companion.unlink()
                removed.append(str(companion))
    return {
        "status": "CREATED" if created else "RETAINED",
        "backup_path": str(weekly_path),
        "backup_bytes": weekly_path.stat().st_size,
        "verification": verification,
        "pruned": removed,
    }


def _relative_bundle_path(bundle_dir: Path, path: Path) -> str:
    return path.relative_to(bundle_dir).as_posix()


def _file_entry(bundle_dir: Path, path: Path) -> dict[str, Any]:
    return {
        "path": _relative_bundle_path(bundle_dir, path),
        "sha256": _sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _copy_stable_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(3):
        before = source.stat()
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.partial")
        try:
            shutil.copy2(source, temporary)
            after = source.stat()
            if before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns:
                temporary.replace(destination)
                return
        finally:
            if temporary.exists():
                temporary.unlink()
    raise RuntimeError(f"source changed repeatedly while snapshotting: {source}")


def _snapshot_tree(source: Path, bundle_dir: Path, component_name: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Copy regular files and retain a content-addressed manifest for restoration."""
    target_root = bundle_dir / component_name
    if not source.exists():
        return {
            "present": False,
            "path": component_name,
            "files": 0,
            "bytes": 0,
            "skipped_symlinks": [],
        }, []
    if not source.is_dir():
        raise ValueError(f"snapshot component must be a directory: {source}")
    entries: list[dict[str, Any]] = []
    skipped_symlinks: list[str] = []
    for current, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
        current_path = Path(current)
        safe_dirs: list[str] = []
        for dirname in sorted(dirnames):
            candidate = current_path / dirname
            if candidate.is_symlink():
                skipped_symlinks.append(candidate.relative_to(source).as_posix())
            else:
                safe_dirs.append(dirname)
        dirnames[:] = safe_dirs
        for filename in sorted(filenames):
            original = current_path / filename
            relative = original.relative_to(source)
            if original.is_symlink():
                skipped_symlinks.append(relative.as_posix())
                continue
            if not original.is_file():
                continue
            copied = target_root / relative
            _copy_stable_file(original, copied)
            entries.append(_file_entry(bundle_dir, copied))
    return {
        "present": True,
        "path": component_name,
        "files": len(entries),
        "bytes": sum(int(entry["bytes"]) for entry in entries),
        "skipped_symlinks": skipped_symlinks,
    }, entries


def _safe_bundle_path(bundle_dir: Path, relative: str) -> Path:
    candidate = (bundle_dir / relative).resolve()
    root = bundle_dir.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"manifest path escapes backup bundle: {relative}")
    return candidate


def verify_bundle_restore(bundle_path: Path) -> dict[str, Any]:
    """Verify every retained file, then restore both SQLite components in isolation."""
    bundle_dir = bundle_path.parent if bundle_path.name == "manifest.json" else bundle_path
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != SNAPSHOT_FORMAT:
        raise ValueError("unsupported backup manifest format")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise ValueError("backup manifest does not contain a file list")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid backup manifest entry")
        path = _safe_bundle_path(bundle_dir, str(entry.get("path") or ""))
        if not path.is_file() or int(entry.get("bytes", -1)) != path.stat().st_size:
            raise RuntimeError(f"backup manifest file missing or size changed: {entry.get('path')}")
        if _sha256_file(path) != entry.get("sha256"):
            raise RuntimeError(f"backup manifest hash mismatch: {entry.get('path')}")
    components = manifest.get("components") or {}
    ledger_path = _safe_bundle_path(bundle_dir, str((components.get("ledger") or {}).get("path") or ""))
    operations_path = _safe_bundle_path(bundle_dir, str((components.get("operations") or {}).get("path") or ""))
    ledger = _verify_database_restore(
        ledger_path,
        count_reader=database_counts,
        schema_table="event_ledger_schema",
    )
    operations = _verify_database_restore(
        operations_path,
        count_reader=operations_counts,
        schema_table="operations_schema",
    )
    if ledger["quick_check"] != "ok" or ledger["integrity_check"] != "ok":
        raise RuntimeError("ledger restore verification failed")
    if operations["quick_check"] != "ok" or operations["integrity_check"] != "ok":
        raise RuntimeError("operations restore verification failed")
    return {
        "quick_check": ledger["quick_check"],
        "integrity_check": ledger["integrity_check"],
        "counts": ledger["counts"],
        "schema_version": ledger["schema_version"],
        "operations": operations,
        "ledger": ledger,
        "manifest_verified": True,
        "manifest_sha256": _sha256_file(manifest_path),
        "files_verified": len(entries),
        "isolated_restore": True,
    }


def _bundle_bytes(bundle_dir: Path) -> int:
    return sum(path.stat().st_size for path in bundle_dir.rglob("*") if path.is_file() and not path.is_symlink())


@contextmanager
def _backup_lock(backup_dir: Path) -> Iterator[None]:
    """Reject concurrent daily backup jobs instead of interleaving their retention passes."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    lock_path = backup_dir / ".finance-radar-backup.lock"
    token = uuid.uuid4().hex
    try:
        with lock_path.open("x", encoding="utf-8") as handle:
            handle.write(token)
    except FileExistsError as exc:
        raise RuntimeError(f"another backup is already running: {lock_path}") from exc
    try:
        yield
    finally:
        try:
            if lock_path.is_file() and lock_path.read_text(encoding="utf-8") == token:
                lock_path.unlink()
        except OSError:
            # A stale lock is intentionally visible and fails closed rather than
            # allowing two retain/prune operations to overlap.
            pass


def create_and_verify(
    source: Path,
    backup_dir: Path,
    operations: OperationsRepository,
    *,
    retention: int = DEFAULT_DAILY_RETENTION,
    weekly_retention: int = DEFAULT_WEEKLY_RETENTION,
    evidence_dir: Path | None = None,
    report_dir: Path | None = None,
) -> dict[str, Any]:
    """Create a self-contained, verified ledger/ops/evidence/reports recovery bundle.

    Retention is deliberately applied only after the new bundle's manifest hashes
    and isolated restores both pass.  Therefore a failed daily run cannot delete
    the last known-good recovery point.
    """
    if not source.is_file():
        raise FileNotFoundError(source)
    if not operations.path.is_file():
        raise FileNotFoundError(operations.path)
    evidence_dir = evidence_dir or source.parent / "evidence_objects"
    report_dir = report_dir or source.parent.parent / "reports"
    snapshot_name = f"finance_radar_{timestamp()}_{uuid.uuid4().hex[:8]}"
    destination = backup_dir / snapshot_name
    manifest_path = destination / "manifest.json"
    staging = backup_dir / f".{snapshot_name}.partial"

    with _backup_lock(backup_dir):
        if staging.exists():
            raise RuntimeError(f"backup staging path already exists: {staging}")
        # Existing committed ledger mutations are reconciled before copying the
        # operations database, so recovery bundles cannot perpetuate a known
        # ledger/audit split.
        reconciliation = operations.reconcile_light_verification_mutations(source)
        backup_id = operations.create_backup_run(
            manifest_path,
            source.stat().st_size,
            manifest_path=manifest_path,
            snapshot_kind="recovery_bundle",
        )
        try:
            started_at = datetime.now(timezone.utc).isoformat()
            staging.mkdir(parents=True, exist_ok=False)
            ledger_path = staging / "ledger.sqlite3"
            operations_path = staging / "operations.sqlite3"
            ledger_capture = online_backup(source, ledger_path, count_reader=database_counts)
            operations_capture = online_backup(
                operations.path,
                operations_path,
                count_reader=operations_counts,
            )
            evidence_component, evidence_entries = _snapshot_tree(evidence_dir, staging, "evidence")
            reports_component, report_entries = _snapshot_tree(report_dir, staging, "reports")
            entries = [
                _file_entry(staging, ledger_path),
                _file_entry(staging, operations_path),
                *evidence_entries,
                *report_entries,
            ]
            manifest = {
                "format": SNAPSHOT_FORMAT,
                "snapshot_id": snapshot_name,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "capture_started_at": started_at,
                "capture_completed_at": datetime.now(timezone.utc).isoformat(),
                "consistency": {
                    "level": "component_consistent_capture_window",
                    "ledger": "SQLite online backup from one read transaction",
                    "operations": "SQLite online backup from one read transaction",
                    "files": "stable-size-and-mtime copy with SHA-256 manifest",
                },
                "retention_policy": {
                    "daily_retention": max(1, int(retention)),
                    "weekly_retention": max(0, int(weekly_retention)),
                    "prune_after": "new_bundle_manifest_and_isolated_restore_verified",
                },
                "reconciliation": reconciliation,
                "components": {
                    "ledger": {
                        "path": "ledger.sqlite3",
                        "source_counts": ledger_capture["source_counts"],
                        "source_bytes": ledger_capture["source_bytes"],
                    },
                    "operations": {
                        "path": "operations.sqlite3",
                        "source_counts": operations_capture["source_counts"],
                        "source_bytes": operations_capture["source_bytes"],
                    },
                    "evidence": evidence_component,
                    "reports": reports_component,
                },
                "files": entries,
            }
            (staging / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            verification = verify_bundle_restore(staging)
            if verification["counts"] != ledger_capture["source_counts"]:
                raise RuntimeError(
                    "restored ledger row counts differ from its online snapshot: "
                    f"source={ledger_capture['source_counts']}, restored={verification['counts']}"
                )
            if verification["operations"]["counts"] != operations_capture["source_counts"]:
                raise RuntimeError(
                    "restored operations row counts differ from its online snapshot: "
                    f"source={operations_capture['source_counts']}, "
                    f"restored={verification['operations']['counts']}"
                )
            staging.replace(destination)
            final_manifest = destination / "manifest.json"
            backup_bytes = _bundle_bytes(destination)
            operations.finish_backup_run(
                backup_id,
                backup_bytes=backup_bytes,
                quick_check=verification["quick_check"],
                counts=verification["counts"],
                manifest_path=final_manifest,
                components=manifest["components"],
                snapshot_kind="recovery_bundle",
            )
            # Only now may the previous daily bundle and all optional weekly
            # copies be removed.  This is the user's single-latest-backup policy.
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
                "manifest_path": str(final_manifest),
                "backup_bytes": backup_bytes,
                "verification": verification,
                "pruned": removed,
                "weekly_snapshot": weekly,
                "reconciliation": reconciliation,
            }
        except Exception as exc:
            operations.fail_backup_run(backup_id, f"{type(exc).__name__}: {exc}")
            if staging.exists():
                _remove_staging_path(backup_dir, staging)
            raise


def main() -> int:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("backup", help="create one verified complete recovery bundle")
    backup_parser.add_argument("--source", type=Path, default=settings.ledger_db)
    backup_parser.add_argument("--backup-dir", type=Path, default=settings.ledger_db.parent / "operational_backups")
    backup_parser.add_argument("--evidence-dir", type=Path, default=settings.evidence_object_dir)
    backup_parser.add_argument("--report-dir", type=Path, default=settings.ledger_db.parent.parent / "reports")
    backup_parser.add_argument("--retention", type=int, default=DEFAULT_DAILY_RETENTION)
    backup_parser.add_argument("--weekly-retention", type=int, default=DEFAULT_WEEKLY_RETENTION)
    verify_parser = subparsers.add_parser("verify", help="verify a legacy backup or full recovery bundle")
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
            evidence_dir=args.evidence_dir,
            report_dir=args.report_dir,
        )
    elif args.command == "verify":
        result = verify_restore(args.backup_path)
    else:
        result = operations.latest_backup()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
