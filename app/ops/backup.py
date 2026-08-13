from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
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
LOCK_STALE_AFTER_SECONDS = 6 * 60 * 60


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


def application_table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """Return an exact inventory of every application table in a SQLite file.

    The compact ``database_counts`` / ``operations_counts`` summaries below
    remain useful to existing callers, but a recovery receipt cannot treat a
    hand-picked subset as proof that the whole ledger was copied.  The
    manifest therefore records every non-internal SQLite table (including the
    schema-version table) and its count.  A verifier can reject both a missing
    historic table and an unaccounted-for future one.
    """
    names = [
        str(row[0])
        for row in connection.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name NOT LIKE 'sqlite_%'
               ORDER BY name"""
        ).fetchall()
    ]
    if not names:
        raise RuntimeError("SQLite component has no application tables")
    return _table_counts(connection, tuple(names))


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
        try:
            return _online_backup_from_open_connection(
                source_connection,
                source,
                destination,
                count_reader=count_reader,
            )
        finally:
            source_connection.rollback()


def _online_backup_from_open_connection(
    source_connection: sqlite3.Connection,
    source: Path,
    destination: Path,
    *,
    count_reader: Callable[[sqlite3.Connection], dict[str, int]],
) -> dict[str, Any]:
    """Copy one already-locked SQLite source without opening a second reader.

    ``create_and_verify`` holds both the ledger and operations writer barriers
    while it calls this helper.  Keeping the count query and online copy on the
    same source connection is what makes their values describe one snapshot.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_counts = count_reader(source_connection)
    table_counts = application_table_counts(source_connection)
    with closing(sqlite3.connect(destination, timeout=30)) as destination_connection:
        source_connection.backup(destination_connection, pages=256)
    return {
        "source_counts": source_counts,
        "table_counts": table_counts,
        "source_bytes": source.stat().st_size,
        "backup_bytes": destination.stat().st_size,
    }


def synchronized_online_backups(
    ledger_source: Path,
    ledger_destination: Path,
    operations_source: Path,
    operations_destination: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Capture ledger and operations behind one bounded cross-database barrier.

    Formal light-verification writes reserve the operations database before
    they open their ledger transaction.  This function deliberately takes the
    same order, preventing a reversed-lock deadlock and ensuring no formal
    mutation can be split between the two copied databases.
    """
    with closing(sqlite3.connect(operations_source, timeout=30)) as operations_locker:
        operations_locker.execute("PRAGMA busy_timeout=30000")
        operations_locker.execute("BEGIN IMMEDIATE")
        try:
            with closing(sqlite3.connect(ledger_source, timeout=30)) as ledger_locker:
                ledger_locker.execute("PRAGMA busy_timeout=30000")
                ledger_locker.execute("BEGIN IMMEDIATE")
                try:
                    # SQLite permits concurrent readers while the reserved
                    # writer barriers prevent any new commit.  The reader
                    # connections below therefore see one stable point across
                    # both databases without asking sqlite3_backup to operate
                    # on the write-lock-owning connection itself.
                    ledger_capture = online_backup(
                        ledger_source,
                        ledger_destination,
                        count_reader=database_counts,
                    )
                    operations_capture = online_backup(
                        operations_source,
                        operations_destination,
                        count_reader=operations_counts,
                    )
                finally:
                    ledger_locker.rollback()
        finally:
            operations_locker.rollback()
    return ledger_capture, operations_capture


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
            table_counts = application_table_counts(restored_connection)
            schema_version = _database_schema_version(restored_connection, schema_table)
        return {
            "quick_check": quick_check,
            "integrity_check": integrity_check,
            "counts": counts,
            "table_counts": table_counts,
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


def prune_backups(
    backup_dir: Path,
    retention: int,
    *,
    verified_path: Path | None = None,
) -> list[str]:
    """Retain a verified recovery point plus the requested newest daily sets.

    ``verified_path`` is the bundle that just passed the complete restore
    drill.  It is deliberately protected ahead of mtime ordering: a skewed
    timestamp on an older backup must never cause us to delete the recovery
    point we have just proved usable.
    """
    keep = max(1, int(retention))
    children = _direct_backup_children(backup_dir)
    protected: Path | None = None
    if verified_path is not None:
        _assert_direct_backup_child(backup_dir, verified_path)
        protected = verified_path.resolve()
        available = {path.resolve() for path in children}
        if protected not in available:
            raise ValueError(f"verified backup is not a complete direct child: {verified_path}")
    files = sorted(
        children,
        key=lambda path: (
            path.resolve() != protected,
            -path.stat().st_mtime,
        ),
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


def _finalize_backup_database(path: Path) -> None:
    """Checkpoint a staging SQLite copy so its main file is self-contained.

    SQLite may leave ``-wal`` / ``-shm`` siblings after a copied operations
    database is normalized.  They are not independently receipted payloads,
    so a recovery bundle must never rely on them.  This runs only against the
    staging copies, before the manifest is written.
    """
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"staging database is unavailable for checkpoint: {path}")
    with closing(sqlite3.connect(path, timeout=30)) as connection:
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if not checkpoint or int(checkpoint[0]) != 0:
            raise RuntimeError(f"unable to checkpoint staging database: {path}")
        journal_mode = str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
        if journal_mode != "delete":
            raise RuntimeError(f"unable to finalize staging database journal mode: {path}")
    leftovers = [
        companion
        for suffix in ("-wal", "-shm", "-journal")
        if (companion := Path(f"{path}{suffix}")).exists() or companion.is_symlink()
    ]
    if leftovers:
        raise RuntimeError(
            "staging database still has unreceipted SQLite sidecars: "
            + ", ".join(str(companion) for companion in leftovers)
        )


def _snapshot_tree(source: Path, bundle_dir: Path, component_name: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Capture one required evidence/report tree as a complete manifest component.

    A recovery bundle is not allowed to silently omit a configured tree.  A
    missing directory, symlink, FIFO, or other unsupported node stops the
    backup before retention runs.  An existing but empty directory is valid:
    its empty root is retained and explicitly recorded so recovery can
    distinguish it from an absent component.
    """
    target_root = bundle_dir / component_name
    if source.is_symlink():
        raise ValueError(f"snapshot component may not be a symlink: {source}")
    if not source.exists():
        raise FileNotFoundError(f"required snapshot component is missing: {source}")
    if not source.is_dir():
        raise ValueError(f"snapshot component must be a directory: {source}")

    def walk_error(error: OSError) -> None:
        raise RuntimeError(f"unable to enumerate snapshot component {source}: {error}")

    target_root.mkdir(parents=True, exist_ok=False)
    entries: list[dict[str, Any]] = []
    directories: list[str] = ["."]
    for current, dirnames, filenames in os.walk(
        source,
        topdown=True,
        followlinks=False,
        onerror=walk_error,
    ):
        current_path = Path(current)
        current_relative = current_path.relative_to(source)
        if current_relative != Path("."):
            (target_root / current_relative).mkdir(parents=True, exist_ok=False)
            directories.append(current_relative.as_posix())
        for dirname in sorted(dirnames):
            candidate = current_path / dirname
            relative = candidate.relative_to(source)
            if candidate.is_symlink():
                raise ValueError(f"snapshot component contains a symlinked directory: {relative}")
            if not candidate.is_dir():
                raise ValueError(f"snapshot component contains an unsupported directory entry: {relative}")
        # os.walk will recurse only into the real directories above.  Keep its
        # own list deterministic and avoid a source-side symlink race being
        # mistaken for an empty directory in the recovery copy.
        dirnames[:] = sorted(dirnames)
        for filename in sorted(filenames):
            original = current_path / filename
            relative = original.relative_to(source)
            if original.is_symlink():
                raise ValueError(f"snapshot component contains a symlinked file: {relative}")
            if not original.is_file():
                raise ValueError(f"snapshot component contains an unsupported file entry: {relative}")
            copied = target_root / relative
            _copy_stable_file(original, copied)
            entries.append(_file_entry(bundle_dir, copied))
    entries.sort(key=lambda entry: str(entry["path"]))
    directories.sort()
    return {
        "present": True,
        "path": component_name,
        "files": len(entries),
        "bytes": sum(int(entry["bytes"]) for entry in entries),
        # This intentionally duplicates the relevant slice of ``files``.
        # The component-local inventory makes evidence/report completeness
        # independently auditable and lets a verifier reject an unmanifested
        # object even when the SQLite copies are healthy.
        "file_inventory": [dict(entry) for entry in entries],
        # Retain empty directories as part of the recovery contract.  In
        # particular an empty-but-existing root is ``[\".\"]`` rather than a
        # misleading absent component.
        "directories": directories,
        "skipped_symlinks": [],
    }, entries


def _safe_bundle_path(bundle_dir: Path, relative: str) -> Path:
    candidate = (bundle_dir / relative).resolve()
    root = bundle_dir.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"manifest path escapes backup bundle: {relative}")
    return candidate


def _manifest_file_records(bundle_dir: Path, entries: object) -> dict[str, dict[str, Any]]:
    """Verify and normalize the global file inventory in one recovery bundle."""
    if not isinstance(entries, list) or not entries:
        raise ValueError("backup manifest does not contain a file list")
    records: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid backup manifest entry")
        relative = entry.get("path")
        if not isinstance(relative, str) or not relative or relative == "manifest.json":
            raise ValueError("backup manifest file path is invalid")
        path = _safe_bundle_path(bundle_dir, relative)
        normalized = _relative_bundle_path(bundle_dir, path)
        if normalized != relative or relative in records:
            raise ValueError(f"backup manifest file path is non-canonical or duplicated: {relative}")
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"backup manifest file is unavailable: {relative}")
        raw_bytes = entry.get("bytes")
        if isinstance(raw_bytes, bool):
            raise ValueError(f"backup manifest byte count is invalid: {relative}")
        try:
            expected_bytes = int(raw_bytes)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"backup manifest byte count is invalid: {relative}") from exc
        if expected_bytes < 0 or expected_bytes != path.stat().st_size:
            raise RuntimeError(f"backup manifest file missing or size changed: {relative}")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"backup manifest hash is invalid: {relative}")
        if _sha256_file(path) != digest:
            raise RuntimeError(f"backup manifest hash mismatch: {relative}")
        records[relative] = {"path": relative, "sha256": digest, "bytes": expected_bytes}
    return records


def _bundle_regular_file_paths(bundle_dir: Path) -> set[str]:
    """Return every payload file and reject links/special nodes outright."""
    if bundle_dir.is_symlink() or not bundle_dir.is_dir():
        raise ValueError(f"backup bundle is not a real directory: {bundle_dir}")
    files: set[str] = set()
    for path in bundle_dir.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"backup bundle contains a symlink: {_relative_bundle_path(bundle_dir, path)}")
        if path.is_file():
            relative = _relative_bundle_path(bundle_dir, path)
            if relative != "manifest.json":
                files.add(relative)
        elif not path.is_dir():
            raise RuntimeError(f"backup bundle contains an unsupported node: {_relative_bundle_path(bundle_dir, path)}")
    return files


def _tree_directories(root: Path) -> list[str]:
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError(f"backup component directory is unavailable: {root}")
    directories = ["."]
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"backup component contains a symlink: {path}")
        if path.is_dir():
            directories.append(path.relative_to(root).as_posix())
        elif not path.is_file():
            raise RuntimeError(f"backup component contains an unsupported node: {path}")
    return sorted(directories)


def _component_integer(component: dict[str, Any], field: str, *, component_name: str) -> int:
    raw = component.get(field)
    if isinstance(raw, bool):
        raise ValueError(f"backup manifest {component_name} {field} is invalid")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"backup manifest {component_name} {field} is invalid") from exc
    if value < 0:
        raise ValueError(f"backup manifest {component_name} {field} is negative")
    return value


def _verify_tree_component(
    bundle_dir: Path,
    component: object,
    *,
    component_name: str,
    records: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], set[str]]:
    """Verify one required evidence/report component and its local inventory."""
    if not isinstance(component, dict):
        raise ValueError(f"backup manifest {component_name} component is invalid")
    if component.get("present") is not True:
        raise RuntimeError(
            f"backup manifest {component_name} component is absent; complete recovery bundles require it"
        )
    if component.get("path") != component_name:
        raise ValueError(f"backup manifest {component_name} path is invalid")
    if component.get("skipped_symlinks") != []:
        raise RuntimeError(
            f"backup manifest {component_name} omitted symlinked data; recovery bundle is incomplete"
        )
    root = _safe_bundle_path(bundle_dir, component_name)
    if root != (bundle_dir / component_name).resolve():
        raise ValueError(f"backup manifest {component_name} path is non-canonical")
    actual_directories = _tree_directories(root)
    raw_directories = component.get("directories")
    if (
        not isinstance(raw_directories, list)
        or any(not isinstance(item, str) or not item for item in raw_directories)
        or len(raw_directories) != len(set(raw_directories))
        or sorted(raw_directories) != actual_directories
    ):
        raise RuntimeError(f"backup manifest {component_name} directory inventory does not match bundle")

    prefix = f"{component_name}/"
    actual_paths = {path for path in records if path.startswith(prefix)}
    raw_inventory = component.get("file_inventory")
    if not isinstance(raw_inventory, list):
        raise ValueError(f"backup manifest {component_name} file inventory is missing")
    declared: dict[str, dict[str, Any]] = {}
    for entry in raw_inventory:
        if not isinstance(entry, dict):
            raise ValueError(f"backup manifest {component_name} file inventory is invalid")
        relative = entry.get("path")
        if not isinstance(relative, str) or not relative.startswith(prefix) or relative in declared:
            raise ValueError(f"backup manifest {component_name} file inventory path is invalid")
        declared[relative] = {
            "path": relative,
            "sha256": entry.get("sha256"),
            "bytes": entry.get("bytes"),
        }
    if set(declared) != actual_paths:
        raise RuntimeError(f"backup manifest {component_name} file inventory does not cover component data")
    for relative in actual_paths:
        if declared[relative] != records[relative]:
            raise RuntimeError(f"backup manifest {component_name} file inventory differs from verified file entry")
    files = _component_integer(component, "files", component_name=component_name)
    total_bytes = _component_integer(component, "bytes", component_name=component_name)
    actual_bytes = sum(int(records[relative]["bytes"]) for relative in actual_paths)
    if files != len(actual_paths) or total_bytes != actual_bytes:
        raise RuntimeError(f"backup manifest {component_name} file/byte totals do not match bundle")
    return {
        "present": True,
        "files": files,
        "bytes": total_bytes,
        "directories": actual_directories,
    }, actual_paths


def _manifest_table_counts(component: object, *, component_name: str) -> dict[str, int]:
    """Read one exact application-table inventory from a recovery manifest."""
    if not isinstance(component, dict):
        raise ValueError(f"backup manifest {component_name} component is invalid")
    raw = component.get("table_counts")
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"backup manifest {component_name} table_counts are missing")
    normalized: dict[str, int] = {}
    for name, value in raw.items():
        if (
            not isinstance(name, str)
            or not name
            or name.startswith("sqlite_")
            or "\x00" in name
        ):
            raise ValueError(f"backup manifest {component_name} table name is invalid")
        if isinstance(value, bool):
            raise ValueError(f"backup manifest {component_name} table count is invalid: {name}")
        try:
            count = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"backup manifest {component_name} table count is invalid: {name}"
            ) from exc
        if count < 0:
            raise ValueError(f"backup manifest {component_name} table count is negative: {name}")
        normalized[name] = count
    return normalized


def _light_verification_change_reason_version(value: Any) -> int | None:
    """Return an exact, positive light-verification schema version if present."""

    prefix = "light_evidence_verification_v"
    text = str(value or "")
    if not text.startswith(prefix):
        return None
    suffix = text[len(prefix) :]
    if not suffix.isdigit():
        return None
    version = int(suffix)
    return version if suffix == str(version) and version >= 1 else None


def _formal_ledger_identity_sets(
    ledger_path: Path,
) -> tuple[set[tuple[str, int]], set[tuple[str, int]], set[tuple[str, int]]]:
    """Return all, outbox-enforced, and legacy formal-version identities.

    The formal-mutation outbox was introduced with v2.  v1 records are still
    immutable historical evidence and are retained in recovery bundles, but
    some pre-outbox rows do not contain enough structured facts to reconstruct
    a durable audit receipt.  They must therefore be disclosed rather than
    made an accidental availability gate for a daily backup.  Every v2-or-newer
    record remains strictly bound to a committed audit receipt.
    """

    with closing(sqlite3.connect(f"file:{ledger_path.as_posix()}?mode=ro", uri=True, timeout=30)) as connection:
        rows = connection.execute(
            """SELECT event_id,version,change_reason FROM event_versions
               WHERE change_reason LIKE 'light_evidence_verification_v%'"""
        ).fetchall()
    versioned_rows = [
        (str(row[0]), int(row[1]), version)
        for row in rows
        if (version := _light_verification_change_reason_version(row[2])) is not None
    ]
    all_identities = {(event_id, version) for event_id, version, _schema_version in versioned_rows}
    legacy_v1 = {
        (event_id, version)
        for event_id, version, schema_version in versioned_rows
        if schema_version == 1
    }
    return all_identities, all_identities - legacy_v1, legacy_v1


def _formal_audit_consistency(ledger_path: Path, operations_path: Path) -> dict[str, Any]:
    """Check that the outbox-era audits agree with immutable ledger versions.

    The ledger is the source of truth for a formal event version.  v2 and later
    were written through the durable outbox and must each have a committed
    audit.  v1 predates that contract; it is reconciled when possible but a
    sparse legacy row is reported as legacy rather than making recovery
    impossible.  An audit receipt for *any* version must still correspond to a
    copied ledger version.  Prepared intents are not allowed to survive in a
    verified bundle: a snapshot of an uncommitted intent is deliberately
    closed as ``ABANDONED`` before verification.
    """
    ledger_identities, enforced_ledger_identities, legacy_v1_identities = _formal_ledger_identity_sets(ledger_path)
    with closing(sqlite3.connect(f"file:{operations_path.as_posix()}?mode=ro", uri=True, timeout=30)) as connection:
        committed_rows = connection.execute(
            """SELECT event_id,after_version FROM formal_mutation_audits
               WHERE mutation_kind='LIGHT_VERIFICATION'
                 AND state IN ('LEDGER_COMMITTED','RECOVERED')"""
        ).fetchall()
        prepared = int(
            connection.execute(
                "SELECT COUNT(*) FROM formal_mutation_audits WHERE state='PREPARED'"
            ).fetchone()[0]
        )
        conflicts = int(
            connection.execute(
                "SELECT COUNT(*) FROM formal_mutation_audits WHERE state='RECOVERY_CONFLICT'"
            ).fetchone()[0]
        )
    audit_identities = {(str(row[0]), int(row[1])) for row in committed_rows}
    missing_audits = sorted(enforced_ledger_identities - audit_identities)
    orphan_audits = sorted(audit_identities - ledger_identities)
    legacy_without_audit = sorted(legacy_v1_identities - audit_identities)
    status = "PASS" if not missing_audits and not orphan_audits and not prepared and not conflicts else "FAIL"
    return {
        "status": status,
        "ledger_formal_versions": len(ledger_identities),
        "outbox_enforced_ledger_versions": len(enforced_ledger_identities),
        "legacy_v1_ledger_versions": len(legacy_v1_identities),
        "committed_audits": len(audit_identities),
        "missing_audits": [f"{event_id}@{version}" for event_id, version in missing_audits],
        "orphan_audits": [f"{event_id}@{version}" for event_id, version in orphan_audits],
        "legacy_v1_without_audit": [f"{event_id}@{version}" for event_id, version in legacy_without_audit],
        "prepared_intents": prepared,
        "recovery_conflicts": conflicts,
    }


def _normalize_bundle_formal_audits(operations_path: Path, ledger_path: Path) -> dict[str, int]:
    """Make the copied operations audit represent the captured ledger boundary.

    A prepared intent with a ledger version in the copied ledger is recovered
    deterministically.  An intent without that version had not committed before
    the shared barrier and is marked abandoned *only in the recovery copy*.
    This never changes either live database.
    """
    copied_operations = OperationsRepository(operations_path)
    # v1 predates the formal outbox.  Do not invent a retroactive audit in a
    # recovery copy: only reconcile a real prepared outbox intent that belongs
    # to the snapshot boundary.
    reconciliation = copied_operations.reconcile_light_verification_mutations(
        ledger_path,
        include_legacy=False,
    )
    with closing(copied_operations.connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """UPDATE formal_mutation_audits
               SET state='ABANDONED',updated_at=?,last_error=?
               WHERE state='PREPARED'""",
            (
                datetime.now(timezone.utc).isoformat(),
                "not committed before synchronized recovery snapshot boundary",
            ),
        )
        connection.commit()
    reconciliation["snapshot_prepared_abandoned"] = int(cursor.rowcount)
    return reconciliation


def verify_bundle_restore(bundle_path: Path) -> dict[str, Any]:
    """Verify every retained file, then restore both SQLite components in isolation."""
    bundle_dir = bundle_path.parent if bundle_path.name == "manifest.json" else bundle_path
    manifest_path = bundle_dir / "manifest.json"
    if bundle_dir.is_symlink() or not manifest_path.is_file() or manifest_path.is_symlink():
        raise FileNotFoundError(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != SNAPSHOT_FORMAT:
        raise ValueError("unsupported backup manifest format")
    records = _manifest_file_records(bundle_dir, manifest.get("files"))
    actual_payload_paths = _bundle_regular_file_paths(bundle_dir)
    if actual_payload_paths != set(records):
        raise RuntimeError(
            "backup manifest file inventory does not exactly match bundle payload: "
            f"manifest={sorted(records)} actual={sorted(actual_payload_paths)}"
        )
    components = manifest.get("components")
    if not isinstance(components, dict):
        raise ValueError("backup manifest components are invalid")
    ledger_component = components.get("ledger")
    operations_component = components.get("operations")
    if not isinstance(ledger_component, dict) or not isinstance(operations_component, dict):
        raise ValueError("backup manifest database components are invalid")
    ledger_path = _safe_bundle_path(
        bundle_dir, str(ledger_component.get("path") or "")
    )
    operations_path = _safe_bundle_path(
        bundle_dir, str(operations_component.get("path") or "")
    )
    ledger_relative = _relative_bundle_path(bundle_dir, ledger_path)
    operations_relative = _relative_bundle_path(bundle_dir, operations_path)
    if ledger_relative not in records or operations_relative not in records:
        raise RuntimeError("backup manifest database component is not covered by a verified file entry")
    evidence, evidence_paths = _verify_tree_component(
        bundle_dir,
        components.get("evidence"),
        component_name="evidence",
        records=records,
    )
    reports, report_paths = _verify_tree_component(
        bundle_dir,
        components.get("reports"),
        component_name="reports",
        records=records,
    )
    if set(records) != {ledger_relative, operations_relative, *evidence_paths, *report_paths}:
        raise RuntimeError("backup manifest contains payload outside its declared recovery components")
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
    if ledger["table_counts"] != _manifest_table_counts(
        ledger_component, component_name="ledger"
    ):
        raise RuntimeError("ledger application-table inventory does not match manifest")
    if operations["table_counts"] != _manifest_table_counts(
        operations_component, component_name="operations"
    ):
        raise RuntimeError("operations application-table inventory does not match manifest")
    audit_consistency = _formal_audit_consistency(ledger_path, operations_path)
    if audit_consistency["status"] != "PASS":
        raise RuntimeError(
            "ledger/operations formal-audit consistency failed: "
            + _stable_json(audit_consistency)
        )
    return {
        "quick_check": ledger["quick_check"],
        "integrity_check": ledger["integrity_check"],
        "counts": ledger["counts"],
        "schema_version": ledger["schema_version"],
        "operations": operations,
        "ledger": ledger,
        "evidence": evidence,
        "reports": reports,
        "formal_audit_consistency": audit_consistency,
        "manifest_verified": True,
        "manifest_sha256": _sha256_file(manifest_path),
        "files_verified": len(records),
        "isolated_restore": True,
    }


def _bundle_bytes(bundle_dir: Path) -> int:
    return sum(path.stat().st_size for path in bundle_dir.rglob("*") if path.is_file() and not path.is_symlink())


def _pid_is_alive(pid: int) -> bool:
    """Read process liveness without using Windows ``os.kill(pid, 0)``.

    On POSIX, signal 0 is a read-only probe.  On Windows Python maps signals to
    process termination semantics, so using it can kill the very backup worker
    whose stale lock is being inspected.
    """
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # The process exists but belongs to an identity this worker cannot
        # inspect.  Treat it as live: only a dead owner is safe to reclaim.
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False


@contextmanager
def _backup_process_mutex(guard_path: Path) -> Iterator[None]:
    """Hold a kernel-enforced mutex on one persistent guard-file inode.

    The JSON owner file remains useful operational metadata, but an atomic
    rename cannot safely reclaim it when two stale-lock contenders race.  This
    guard is never unlinked, so every process locks the same inode for the full
    backup/verification/retention transaction.
    """

    guard_path.parent.mkdir(parents=True, exist_ok=True)
    handle = guard_path.open("a+b")
    acquired = False
    try:
        # Windows byte-range locks require a byte to exist.  Concurrent first
        # openers may append more than one sentinel, which is harmless because
        # every contender locks byte zero.
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError(
                    f"another backup is already running: process mutex busy: {guard_path}"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError(
                    f"another backup is already running: process mutex busy: {guard_path}"
                ) from exc
        acquired = True
        yield
    finally:
        if acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                # Closing the descriptor also releases the kernel lock.  Do not
                # mask a successful backup or its original failure on cleanup.
                pass
        handle.close()


@contextmanager
def _backup_owner_lock(backup_dir: Path) -> Iterator[dict[str, Any]]:
    """Maintain human-readable ownership metadata under the process mutex."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    lock_path = backup_dir / ".finance-radar-backup.lock"
    token = uuid.uuid4().hex
    lock_state: dict[str, Any] = {"recovered_stale_lock": False}
    while True:
        try:
            with lock_path.open("x", encoding="utf-8") as handle:
                json.dump(
                    {
                        "token": token,
                        "pid": os.getpid(),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                    handle,
                    sort_keys=True,
                )
            break
        except FileExistsError as exc:
            try:
                raw = lock_path.read_text(encoding="utf-8")
                metadata = json.loads(raw) if raw.lstrip().startswith("{") else {}
            except (OSError, json.JSONDecodeError):
                metadata = {}
            age_seconds = max(0.0, time.time() - lock_path.stat().st_mtime) if lock_path.exists() else 0.0
            owner_pid = metadata.get("pid") if isinstance(metadata, dict) else None
            owner_alive = _pid_is_alive(owner_pid) if isinstance(owner_pid, int) and owner_pid > 0 else False
            if age_seconds < LOCK_STALE_AFTER_SECONDS or owner_alive:
                raise RuntimeError(
                    "another backup is already running: "
                    f"{lock_path} (age_seconds={age_seconds:.0f}, owner_pid={owner_pid!r}, owner_alive={owner_alive})"
                ) from exc
            quarantine = backup_dir / f".finance-radar-backup.lock.stale-{uuid.uuid4().hex}"
            try:
                os.replace(lock_path, quarantine)
            except FileNotFoundError:
                continue
            try:
                quarantine.unlink()
            except OSError:
                pass
            lock_state.update(
                {
                    "recovered_stale_lock": True,
                    "stale_lock_age_seconds": round(age_seconds, 3),
                    "stale_lock_owner_pid": owner_pid,
                }
            )
    try:
        yield lock_state
    finally:
        try:
            if lock_path.is_file():
                metadata = json.loads(lock_path.read_text(encoding="utf-8"))
                if metadata.get("token") == token:
                    lock_path.unlink()
        except (OSError, json.JSONDecodeError):
            # The next run will only reclaim this owner lock after its lease is
            # stale and its recorded process no longer exists.
            pass


@contextmanager
def _backup_lock(backup_dir: Path) -> Iterator[dict[str, Any]]:
    """Serialize a complete backup and safely recover stale owner metadata."""

    guard_path = backup_dir / ".finance-radar-backup.guard"
    with _backup_process_mutex(guard_path):
        with _backup_owner_lock(backup_dir) as lock_state:
            yield lock_state


def create_and_verify(
    source: Path,
    backup_dir: Path,
    operations: OperationsRepository,
    *,
    retention: int = DEFAULT_DAILY_RETENTION,
    weekly_retention: int = DEFAULT_WEEKLY_RETENTION,
    evidence_dir: Path | None = None,
    report_dir: Path | None = None,
    predeploy_bridge: bool = False,
) -> dict[str, Any]:
    """Create a self-contained, verified ledger/ops/evidence/reports recovery bundle.

    Retention is deliberately applied only after the new bundle's manifest hashes
    and isolated restores both pass.  Therefore a failed daily run cannot delete
    the last known-good recovery point.

    ``predeploy_bridge`` is deliberately narrower than a normal scheduled
    backup.  It takes the same cross-database barrier, complete bundle, and
    isolated restore drill, but it never initializes, reconciles, or records
    anything in the live operations database.  It also preserves the copied
    operations schema and formal-audit rows exactly: the recovery point must
    remain usable by the active release if the candidate is rejected.  An
    unresolved copied audit therefore fails the bridge instead of being
    normalized with candidate code.  Deployment code uses it to make a
    recovery point with candidate source before a candidate is allowed to
    perform an irreversible schema migration.
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
    backup_id: str | None = None

    with _backup_lock(backup_dir) as lock_state:
        if staging.exists():
            raise RuntimeError(f"backup staging path already exists: {staging}")
        try:
            if predeploy_bridge:
                # Do not turn an otherwise read-only pre-cutover recovery gate
                # into either a live or recovery-copy schema/data migration.
                # The copied pair is verified below exactly as captured; an
                # unresolved formal audit must fail this gate rather than be
                # repaired by code that is not yet allowed to become current.
                backup_run_reconciliation = {"status": "SKIPPED_PREDEPLOY_BRIDGE"}
                reconciliation = {"status": "SKIPPED_PREDEPLOY_BRIDGE"}
            else:
                # The backup-root lock was acquired immediately above.  It is
                # the concurrency proof for closing receipts left RUNNING by a
                # crashed earlier process; no new workflow can be active under
                # this root while the operations transaction makes those rows
                # terminal.
                backup_run_reconciliation = operations.reconcile_abandoned_backup_runs(
                    exclusive_owner=f"backup-root-lock pid={os.getpid()}"
                )
                # Recover only real prepared outbox intents before copying
                # either database.  Historical v1 rows predate the outbox
                # contract and must never acquire a retroactive formal audit
                # merely because a backup ran.  The actual copies are then
                # taken behind a shared operations -> ledger write barrier.
                reconciliation = operations.reconcile_light_verification_mutations(
                    source,
                    include_legacy=False,
                )
            started_at = datetime.now(timezone.utc).isoformat()
            staging.mkdir(parents=True, exist_ok=False)
            ledger_path = staging / "ledger.sqlite3"
            operations_path = staging / "operations.sqlite3"
            ledger_capture, operations_capture = synchronized_online_backups(
                source,
                ledger_path,
                operations.path,
                operations_path,
            )
            if predeploy_bridge:
                snapshot_audit_reconciliation = {
                    "status": "PRESERVED_PREDEPLOY_BRIDGE",
                    "reason": "copied formal audits and SQLite schema are verified without candidate mutation",
                }
            else:
                snapshot_audit_reconciliation = _normalize_bundle_formal_audits(
                    operations_path,
                    ledger_path,
                )
            # Do not publish a bundle whose apparent SQLite restore depends on
            # unlisted WAL/SHM sidecars.  The receipt covers the main database
            # files only, so each staging copy must be checkpointed into one
            # self-contained file before any hashes or tree inventories are
            # calculated.
            _finalize_backup_database(ledger_path)
            _finalize_backup_database(operations_path)
            with closing(sqlite3.connect(f"file:{operations_path.as_posix()}?mode=ro", uri=True)) as connection:
                bundled_operations_counts = operations_counts(connection)
                bundled_operations_table_counts = application_table_counts(connection)
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
                    "level": "cross_database_write_barrier",
                    "ledger": "SQLite online backup while both ledger and operations writers were reserved",
                    "operations": "SQLite online backup while both ledger and operations writers were reserved",
                    "formal_audit": (
                        "copied audit preserved and verified without candidate mutation"
                        if predeploy_bridge
                        else "copied audit reconciled against copied immutable ledger versions"
                    ),
                    "files": "stable-size-and-mtime copy with SHA-256 manifest",
                },
                "retention_policy": {
                    "daily_retention": max(1, int(retention)),
                    "weekly_retention": max(0, int(weekly_retention)),
                    "prune_after": "new_bundle_manifest_and_isolated_restore_verified",
                },
                "reconciliation": reconciliation,
                "backup_run_reconciliation": backup_run_reconciliation,
                "components": {
                    "ledger": {
                        "path": "ledger.sqlite3",
                        "source_counts": ledger_capture["source_counts"],
                        "table_counts": ledger_capture["table_counts"],
                        "source_bytes": ledger_capture["source_bytes"],
                    },
                    "operations": {
                        "path": "operations.sqlite3",
                        "source_counts": operations_capture["source_counts"],
                        "bundle_counts": bundled_operations_counts,
                        "table_counts": bundled_operations_table_counts,
                        "source_bytes": operations_capture["source_bytes"],
                    },
                    "evidence": evidence_component,
                    "reports": reports_component,
                },
                "files": entries,
                "lock": lock_state,
                "snapshot_audit_reconciliation": snapshot_audit_reconciliation,
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
            if verification["operations"]["counts"] != bundled_operations_counts:
                raise RuntimeError(
                    "restored operations row counts differ from its captured recovery snapshot: "
                    f"snapshot={bundled_operations_counts}, "
                    f"restored={verification['operations']['counts']}"
                )
            if verification["ledger"]["table_counts"] != ledger_capture["table_counts"]:
                raise RuntimeError(
                    "restored ledger table inventory differs from its online snapshot: "
                    f"source={ledger_capture['table_counts']}, "
                    f"restored={verification['ledger']['table_counts']}"
                )
            if verification["operations"]["table_counts"] != bundled_operations_table_counts:
                raise RuntimeError(
                    "restored operations table inventory differs from its normalized recovery snapshot: "
                    f"snapshot={bundled_operations_table_counts}, "
                    f"restored={verification['operations']['table_counts']}"
                )
            staging.replace(destination)
            final_manifest = destination / "manifest.json"
            backup_bytes = _bundle_bytes(destination)
            if not predeploy_bridge:
                # The run record is created only after the two-database bundle
                # has passed isolated restore.  It is intentionally not
                # embedded in its own operations snapshot as a misleading
                # RUNNING record.
                backup_id = operations.create_backup_run(
                    final_manifest,
                    ledger_capture["source_bytes"],
                    manifest_path=final_manifest,
                    snapshot_kind="recovery_bundle",
                )
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
            removed = prune_backups(backup_dir, retention, verified_path=destination)
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
                "backup_run_reconciliation": backup_run_reconciliation,
                "predeploy_bridge": predeploy_bridge,
            }
        except Exception as exc:
            # Preserve a failed-attempt diagnostic even when failure occurred
            # before a bundle could be published.  A valid pre-existing bundle
            # is never pruned by this path.
            if not predeploy_bridge and backup_id is None:
                try:
                    backup_id = operations.create_backup_run(
                        manifest_path,
                        source.stat().st_size,
                        manifest_path=manifest_path,
                        snapshot_kind="recovery_bundle",
                    )
                except Exception:
                    backup_id = None
            if backup_id is not None:
                try:
                    operations.fail_backup_run(backup_id, f"{type(exc).__name__}: {exc}")
                except Exception:
                    pass
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
    backup_parser.add_argument(
        "--predeploy-bridge",
        action="store_true",
        help="create a verified recovery bundle without mutating live operations state",
    )
    verify_parser = subparsers.add_parser("verify", help="verify a legacy backup or full recovery bundle")
    verify_parser.add_argument("backup_path", type=Path)
    subparsers.add_parser("status", help="show latest recorded backup drill")
    args = parser.parse_args()
    if args.command == "backup":
        operations = OperationsRepository(
            settings.operations_db,
            initialize=not args.predeploy_bridge,
        )
        result = create_and_verify(
            args.source,
            args.backup_dir,
            operations,
            retention=args.retention,
            weekly_retention=args.weekly_retention,
            evidence_dir=args.evidence_dir,
            report_dir=args.report_dir,
            predeploy_bridge=args.predeploy_bridge,
        )
    elif args.command == "verify":
        result = verify_restore(args.backup_path)
    else:
        operations = OperationsRepository(settings.operations_db)
        result = operations.latest_backup()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
