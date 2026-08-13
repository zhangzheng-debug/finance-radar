#!/usr/bin/env python3
"""Independently validate a fresh Finance Radar recovery point.

This deliberately uses only the Python standard library.  It runs before an
upgrade changes the current release, so it must not depend on the candidate
application being importable.  The validator accepts either the modern,
two-database recovery bundle or the one-time legacy SQLite bridge, but both
forms are restored in isolation and tied to a fresh systemd backup run.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from typing import Any, Iterable


SNAPSHOT_FORMAT = "finance-radar-recovery-bundle-v1"
LEDGER_COUNT_TABLES = (
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
# A recovery receipt must cover the full current application schema, not merely
# the compact operational summaries above.  Keep this versioned with the
# schema: a future migration which adds a table must update this contract and
# its fixture, otherwise the candidate release is safely rejected.
LEDGER_REQUIRED_TABLES = frozenset(
    {
        "alert_delivery_attempts",
        "alert_delivery_cleanup",
        "alert_delivery_leases",
        "alert_outbox",
        "assets",
        "canonical_events",
        "entities",
        "event_assessments",
        "event_asset_impacts",
        "event_chain_members",
        "event_chains",
        "event_entities",
        "event_evidence",
        "event_ledger_schema",
        "event_market_metrics",
        "event_observations",
        "event_review_triage",
        "event_versions",
        "market_jobs",
        "market_snapshots",
        "observation_jobs",
        "pipeline_jobs",
        "raw_observations",
        "runtime_leases",
        "sec_filing_enrichments",
        "source_cursors",
        "source_revisions",
        "sources",
        "telegram_source_channels",
        "telegram_source_messages",
    }
)
OPERATIONS_REQUIRED_TABLES = frozenset(
    {
        "adjudication_reviews",
        "adjudication_samples",
        "agent_decisions",
        "backup_runs",
        "evidence_object_links",
        "evidence_objects",
        "formal_mutation_audits",
        "human_overrides",
        "light_verification_runs",
        "model_runs",
        "operations_schema",
        "replay_runs",
        "runtime_state",
        "worker_cycles",
    }
)
IDENTITY_RE = re.compile(r"^finance_radar_[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
LEGACY_RE = re.compile(r"^finance_radar_[A-Za-z0-9][A-Za-z0-9_-]{0,127}[.]sqlite3$")


class ReceiptError(RuntimeError):
    """A backup is not a complete, fresh, independently verifiable receipt."""


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ReceiptError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ReceiptError(f"timestamp is missing a timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _direct_child(root: Path, candidate: Path) -> None:
    if candidate.is_symlink() or candidate.parent.resolve() != root.resolve():
        raise ReceiptError(f"backup is not a direct non-symlink child: {candidate}")


def _safe_component(bundle: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ReceiptError("backup manifest contains an invalid component path")
    root = bundle.resolve()
    candidate = (root / relative).resolve()
    if candidate == root or root not in candidate.parents:
        raise ReceiptError(f"backup manifest path escapes its bundle: {relative!r}")
    return candidate


def _manifest_file_records(bundle: Path, entries: object) -> dict[str, dict[str, object]]:
    """Return the verified, canonical global payload-file inventory."""
    if not isinstance(entries, list) or not entries:
        raise ReceiptError("backup manifest does not contain files")
    records: dict[str, dict[str, object]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ReceiptError("backup manifest contains an invalid file entry")
        relative = entry.get("path")
        if not isinstance(relative, str) or not relative or relative == "manifest.json":
            raise ReceiptError("backup manifest file path is invalid")
        path = _safe_component(bundle, relative)
        normalized = path.relative_to(bundle.resolve()).as_posix()
        if normalized != relative or relative in records:
            raise ReceiptError(f"backup manifest repeats or normalizes a file path: {relative!r}")
        if path.is_symlink() or not path.is_file():
            raise ReceiptError(f"backup manifest file is unavailable: {relative!r}")
        raw_bytes = entry.get("bytes")
        if isinstance(raw_bytes, bool):
            raise ReceiptError(f"backup manifest size is invalid: {relative!r}")
        try:
            expected_bytes = int(raw_bytes)
        except (TypeError, ValueError) as exc:
            raise ReceiptError(f"backup manifest size is invalid: {relative!r}") from exc
        if expected_bytes < 0 or expected_bytes != path.stat().st_size:
            raise ReceiptError(f"backup manifest size mismatch: {relative!r}")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ReceiptError(f"backup manifest hash is invalid: {relative!r}")
        if _sha256(path) != digest:
            raise ReceiptError(f"backup manifest hash mismatch: {relative!r}")
        records[relative] = {"path": relative, "sha256": digest, "bytes": expected_bytes}
    return records


def _bundle_payload_paths(bundle: Path) -> set[str]:
    """Return every on-disk payload file and reject links/special nodes."""
    if bundle.is_symlink() or not bundle.is_dir():
        raise ReceiptError(f"backup bundle is not a real directory: {bundle}")
    paths: set[str] = set()
    for path in bundle.rglob("*"):
        relative = path.relative_to(bundle).as_posix()
        if path.is_symlink():
            raise ReceiptError(f"backup bundle contains a symlink: {relative}")
        if path.is_file():
            if relative != "manifest.json":
                paths.add(relative)
        elif not path.is_dir():
            raise ReceiptError(f"backup bundle contains an unsupported node: {relative}")
    return paths


def _tree_directories(root: Path) -> list[str]:
    if root.is_symlink() or not root.is_dir():
        raise ReceiptError(f"backup component directory is unavailable: {root}")
    directories = ["."]
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ReceiptError(f"backup component contains a symlink: {path}")
        if path.is_dir():
            directories.append(path.relative_to(root).as_posix())
        elif not path.is_file():
            raise ReceiptError(f"backup component contains an unsupported node: {path}")
    return sorted(directories)


def _component_nonnegative(component: dict[str, object], field: str, name: str) -> int:
    raw = component.get(field)
    if isinstance(raw, bool):
        raise ReceiptError(f"backup manifest {name} {field} is invalid")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ReceiptError(f"backup manifest {name} {field} is invalid") from exc
    if value < 0:
        raise ReceiptError(f"backup manifest {name} {field} is negative")
    return value


def _verify_tree_component(
    bundle: Path,
    component: object,
    *,
    name: str,
    records: dict[str, dict[str, object]],
) -> tuple[dict[str, object], set[str]]:
    """Verify a required evidence/reports tree and its component-local receipt."""
    if not isinstance(component, dict):
        raise ReceiptError(f"backup manifest {name} component is invalid")
    if component.get("present") is not True:
        raise ReceiptError(f"backup manifest {name} component is absent")
    if component.get("path") != name:
        raise ReceiptError(f"backup manifest {name} path is invalid")
    if component.get("skipped_symlinks") != []:
        raise ReceiptError(f"backup manifest {name} omits symlinked data")
    root = _safe_component(bundle, name)
    if root != (bundle / name).resolve():
        raise ReceiptError(f"backup manifest {name} path is non-canonical")
    actual_directories = _tree_directories(root)
    raw_directories = component.get("directories")
    if (
        not isinstance(raw_directories, list)
        or any(not isinstance(item, str) or not item for item in raw_directories)
        or len(raw_directories) != len(set(raw_directories))
        or sorted(raw_directories) != actual_directories
    ):
        raise ReceiptError(f"backup manifest {name} directory inventory does not match bundle")
    prefix = f"{name}/"
    actual_paths = {relative for relative in records if relative.startswith(prefix)}
    raw_inventory = component.get("file_inventory")
    if not isinstance(raw_inventory, list):
        raise ReceiptError(f"backup manifest {name} file inventory is missing")
    declared: dict[str, dict[str, object]] = {}
    for entry in raw_inventory:
        if not isinstance(entry, dict):
            raise ReceiptError(f"backup manifest {name} file inventory is invalid")
        relative = entry.get("path")
        if not isinstance(relative, str) or not relative.startswith(prefix) or relative in declared:
            raise ReceiptError(f"backup manifest {name} file inventory path is invalid")
        declared[relative] = {
            "path": relative,
            "sha256": entry.get("sha256"),
            "bytes": entry.get("bytes"),
        }
    if set(declared) != actual_paths:
        raise ReceiptError(f"backup manifest {name} file inventory does not cover component data")
    for relative in actual_paths:
        if declared[relative] != records[relative]:
            raise ReceiptError(f"backup manifest {name} file inventory differs from verified file entry")
    files = _component_nonnegative(component, "files", name)
    total_bytes = _component_nonnegative(component, "bytes", name)
    actual_bytes = sum(int(records[relative]["bytes"]) for relative in actual_paths)
    if files != len(actual_paths) or total_bytes != actual_bytes:
        raise ReceiptError(f"backup manifest {name} file/byte totals do not match bundle")
    return {
        "present": True,
        "files": files,
        "bytes": total_bytes,
        "directories": actual_directories,
    }, actual_paths


def _count_map(connection: sqlite3.Connection, tables: Iterable[str]) -> dict[str, int]:
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in tables
    }


def _application_table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    names = [
        str(row[0])
        for row in connection.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name NOT LIKE 'sqlite_%'
               ORDER BY name"""
        ).fetchall()
    ]
    if not names:
        raise ReceiptError("backup restore has no application tables")
    counts: dict[str, int] = {}
    for name in names:
        quoted = '"' + name.replace('"', '""') + '"'
        counts[name] = int(connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
    return counts


def _restore_and_inspect(
    path: Path,
    *,
    required_tables: set[str],
    count_tables: Iterable[str],
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise ReceiptError(f"backup database is not a nonempty regular file: {path}")
    with tempfile.TemporaryDirectory(prefix="finance-radar-receipt-restore-") as temp_dir:
        restored = Path(temp_dir) / "restored.sqlite3"
        source = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
        destination = sqlite3.connect(restored, timeout=30)
        try:
            source.execute("PRAGMA query_only=ON")
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        connection = sqlite3.connect(restored, timeout=30)
        try:
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            integrity_check = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            missing = sorted(required_tables - tables)
            if missing:
                raise ReceiptError(
                    "backup restore is missing required tables for "
                    f"{path.name}: {','.join(missing)}"
                )
            counts = _count_map(connection, count_tables)
            table_counts = _application_table_counts(connection)
        finally:
            connection.close()
    if quick_check != "ok" or integrity_check != "ok":
        raise ReceiptError(f"backup restore integrity check failed for {path.name}")
    return {
        "quick_check": quick_check,
        "integrity_check": integrity_check,
        "counts": counts,
        "table_counts": table_counts,
    }


def _expected_counts(
    component: object,
    field: str,
    actual: dict[str, int],
    *,
    exact_table_set: bool = False,
) -> None:
    if not isinstance(component, dict):
        raise ReceiptError("backup manifest component is invalid")
    raw = component.get(field)
    if not isinstance(raw, dict):
        raise ReceiptError(f"backup manifest component is missing {field}")
    if exact_table_set and set(raw) != set(actual):
        raise ReceiptError(
            f"backup manifest {field} table set does not match isolated restore: "
            f"expected={_stable_json(sorted(str(name) for name in raw))} "
            f"actual={_stable_json(sorted(actual))}"
        )
    expected: dict[str, int] = {}
    for table in actual:
        value = raw.get(table)
        if isinstance(value, bool):
            raise ReceiptError(f"backup manifest count is not an integer: {field}.{table}")
        try:
            converted = int(value)
        except (TypeError, ValueError) as exc:
            raise ReceiptError(
                f"backup manifest count is not an integer: {field}.{table}"
            ) from exc
        if converted < 0:
            raise ReceiptError(f"backup manifest count is negative: {field}.{table}")
        expected[table] = converted
    if expected != actual:
        raise ReceiptError(
            f"backup manifest {field} does not match isolated restored counts: "
            f"expected={_stable_json(expected)} actual={_stable_json(actual)}"
        )


def _verification_version(value: object) -> int | None:
    prefix = "light_evidence_verification_v"
    text = str(value or "")
    if not text.startswith(prefix):
        return None
    suffix = text[len(prefix) :]
    if not suffix.isdigit():
        return None
    version = int(suffix)
    return version if version >= 1 and suffix == str(version) else None


def _formal_audit_consistency(ledger: Path, operations: Path) -> None:
    with sqlite3.connect(f"file:{ledger.as_posix()}?mode=ro", uri=True, timeout=30) as connection:
        rows = connection.execute(
            """SELECT event_id,version,change_reason FROM event_versions
               WHERE change_reason LIKE 'light_evidence_verification_v%'"""
        ).fetchall()
    identities: set[tuple[str, int, int]] = set()
    for event_id, version, reason in rows:
        schema = _verification_version(reason)
        if schema is None:
            continue
        try:
            identities.add((str(event_id), int(version), schema))
        except (TypeError, ValueError) as exc:
            raise ReceiptError("ledger light-verification identity is invalid") from exc
    required = {(event_id, version) for event_id, version, schema in identities if schema >= 2}
    all_identities = {(event_id, version) for event_id, version, _ in identities}
    with sqlite3.connect(f"file:{operations.as_posix()}?mode=ro", uri=True, timeout=30) as connection:
        committed = {
            (str(event_id), int(version))
            for event_id, version in connection.execute(
                """SELECT event_id,after_version FROM formal_mutation_audits
                   WHERE mutation_kind='LIGHT_VERIFICATION'
                     AND state IN ('LEDGER_COMMITTED','RECOVERED')"""
            ).fetchall()
        }
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
    if required - committed or committed - all_identities or prepared or conflicts:
        raise ReceiptError("backup bundle formal-audit consistency failed")


def verify_full_bundle(bundle: Path, *, started_at: datetime) -> dict[str, Any]:
    _direct_child(bundle.parent, bundle)
    manifest_path = bundle / "manifest.json"
    if not IDENTITY_RE.fullmatch(bundle.name) or manifest_path.is_symlink() or not manifest_path.is_file():
        raise ReceiptError(f"backup bundle identity or manifest is invalid: {bundle.name}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"unable to parse backup manifest: {bundle.name}") from exc
    if not isinstance(manifest, dict):
        raise ReceiptError("backup manifest is not an object")
    if manifest.get("format") != SNAPSHOT_FORMAT or manifest.get("snapshot_id") != bundle.name:
        raise ReceiptError("backup manifest has an unexpected format or snapshot id")
    if _utc(str(manifest.get("created_at") or "")) < started_at:
        raise ReceiptError("backup manifest predates the requested backup run")
    records = _manifest_file_records(bundle, manifest.get("files"))
    actual_payload_paths = _bundle_payload_paths(bundle)
    if actual_payload_paths != set(records):
        raise ReceiptError(
            "backup manifest file inventory does not exactly match bundle payload: "
            f"manifest={_stable_json(sorted(records))} actual={_stable_json(sorted(actual_payload_paths))}"
        )
    components = manifest.get("components")
    if not isinstance(components, dict):
        raise ReceiptError("backup manifest does not contain components")
    ledger_component = components.get("ledger")
    operations_component = components.get("operations")
    ledger = _safe_component(bundle, ledger_component.get("path") if isinstance(ledger_component, dict) else None)
    operations = _safe_component(
        bundle, operations_component.get("path") if isinstance(operations_component, dict) else None
    )
    relative_ledger = str(ledger.relative_to(bundle.resolve()).as_posix())
    relative_operations = str(operations.relative_to(bundle.resolve()).as_posix())
    if relative_ledger not in records or relative_operations not in records:
        raise ReceiptError("backup manifest component is not covered by a verified file entry")
    evidence, evidence_paths = _verify_tree_component(
        bundle,
        components.get("evidence"),
        name="evidence",
        records=records,
    )
    reports, report_paths = _verify_tree_component(
        bundle,
        components.get("reports"),
        name="reports",
        records=records,
    )
    if set(records) != {relative_ledger, relative_operations, *evidence_paths, *report_paths}:
        raise ReceiptError("backup manifest contains payload outside declared recovery components")
    ledger_verification = _restore_and_inspect(
        ledger,
        required_tables=set(LEDGER_REQUIRED_TABLES),
        count_tables=LEDGER_COUNT_TABLES,
    )
    operations_verification = _restore_and_inspect(
        operations,
        required_tables=set(OPERATIONS_REQUIRED_TABLES),
        count_tables=OPERATIONS_COUNT_TABLES,
    )
    _expected_counts(ledger_component, "source_counts", ledger_verification["counts"])
    _expected_counts(operations_component, "bundle_counts", operations_verification["counts"])
    _expected_counts(
        ledger_component,
        "table_counts",
        ledger_verification["table_counts"],
        exact_table_set=True,
    )
    _expected_counts(
        operations_component,
        "table_counts",
        operations_verification["table_counts"],
        exact_table_set=True,
    )
    _formal_audit_consistency(ledger, operations)
    return {
        "kind": "recovery_bundle",
        "snapshot_id": bundle.name,
        "relative_path": bundle.name,
        "receipt_sha256": _sha256(manifest_path),
        "ledger_counts": ledger_verification["counts"],
        "ledger_table_counts": ledger_verification["table_counts"],
        "operations_counts": operations_verification["counts"],
        "operations_table_counts": operations_verification["table_counts"],
        "evidence": evidence,
        "reports": reports,
    }


def _normalise_recorded_path(value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        raw = Path(value)
        if not raw.is_absolute():
            return None
        return raw.resolve(strict=False)
    except OSError:
        return None


def _legacy_backup_run(
    operations_db: Path,
    candidate: Path,
    *,
    started_at: datetime,
    restored_counts: dict[str, int],
    recorded_candidate: Path | None = None,
) -> dict[str, Any]:
    if operations_db.is_symlink() or not operations_db.is_file():
        raise ReceiptError(f"operations database is unavailable for legacy receipt: {operations_db}")
    connection = sqlite3.connect(f"file:{operations_db.as_posix()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(backup_runs)").fetchall()
        }
        required_columns = {
            "backup_id",
            "backup_path",
            "backup_bytes",
            "quick_check",
            "restored_count_json",
            "status",
            "verified_at",
        }
        missing = sorted(required_columns - columns)
        if missing:
            raise ReceiptError(
                "legacy operations receipt is missing required backup_runs columns: "
                + ",".join(missing)
            )
        rows = connection.execute(
            """SELECT backup_id,backup_path,backup_bytes,quick_check,restored_count_json,status,verified_at
               FROM backup_runs WHERE status='VERIFIED'"""
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise ReceiptError("unable to read legacy backup_runs receipt") from exc
    finally:
        connection.close()
    # A protected pre-cutover hold is a hard-linked copy, so its path is
    # intentionally different from the path recorded by the original backup
    # run.  It still needs the original receipt identity for the database
    # lookup, while all byte/table validation below remains against
    # ``candidate`` (the held file).
    resolved = (recorded_candidate or candidate).resolve()
    matches = [row for row in rows if _normalise_recorded_path(row["backup_path"]) == resolved]
    if len(matches) != 1:
        raise ReceiptError(
            f"expected exactly one verified legacy backup_runs receipt for {candidate.name}, found {len(matches)}"
        )
    row = matches[0]
    if not isinstance(row["backup_id"], str) or not row["backup_id"]:
        raise ReceiptError("legacy backup_runs receipt has an invalid backup id")
    if row["quick_check"] != "ok":
        raise ReceiptError("legacy backup_runs receipt did not report quick_check=ok")
    verified_at = _utc(str(row["verified_at"] or ""))
    if verified_at < started_at:
        raise ReceiptError("legacy backup_runs receipt predates the requested backup run")
    if isinstance(row["backup_bytes"], bool) or int(row["backup_bytes"] or -1) != candidate.stat().st_size:
        raise ReceiptError("legacy backup_runs receipt does not match snapshot bytes")
    try:
        raw_counts = json.loads(str(row["restored_count_json"] or ""))
    except json.JSONDecodeError as exc:
        raise ReceiptError("legacy backup_runs restored counts are not JSON") from exc
    if not isinstance(raw_counts, dict):
        raise ReceiptError("legacy backup_runs restored counts are not an object")
    receipt_counts: dict[str, int] = {}
    for table in LEDGER_COUNT_TABLES:
        value = raw_counts.get(table)
        if isinstance(value, bool):
            raise ReceiptError(f"legacy backup_runs count is invalid: {table}")
        try:
            converted = int(value)
        except (TypeError, ValueError) as exc:
            raise ReceiptError(f"legacy backup_runs count is invalid: {table}") from exc
        if converted < 0:
            raise ReceiptError(f"legacy backup_runs count is negative: {table}")
        receipt_counts[table] = converted
    if receipt_counts != restored_counts:
        raise ReceiptError(
            "legacy backup_runs restored counts do not match isolated restore: "
            f"record={_stable_json(receipt_counts)} actual={_stable_json(restored_counts)}"
        )
    if sum(restored_counts.values()) <= 0:
        raise ReceiptError("legacy backup receipt is structurally valid but contains no data")
    return {
        "backup_id": str(row["backup_id"]),
        "verified_at": verified_at.isoformat(),
        "backup_bytes": candidate.stat().st_size,
        "restored_counts": receipt_counts,
    }


def _live_ledger_table_counts(path: Path) -> dict[str, int]:
    if path.is_symlink() or not path.is_file():
        raise ReceiptError(f"live ledger is unavailable for legacy receipt: {path}")
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30) as connection:
            connection.execute("PRAGMA query_only=ON")
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            missing = sorted(LEDGER_REQUIRED_TABLES - tables)
            if missing:
                raise ReceiptError(
                    "live ledger is missing required tables for legacy receipt: "
                    + ",".join(missing)
                )
            return _application_table_counts(connection)
    except sqlite3.DatabaseError as exc:
        raise ReceiptError("unable to read live ledger for legacy receipt") from exc


def verify_legacy_snapshot(
    candidate: Path,
    *,
    operations_db: Path | None,
    ledger_source: Path | None,
    started_at: datetime,
    recorded_candidate: Path | None = None,
) -> dict[str, Any]:
    _direct_child(candidate.parent, candidate)
    if not LEGACY_RE.fullmatch(candidate.name):
        raise ReceiptError(f"legacy backup identity is invalid: {candidate.name}")
    if operations_db is None:
        raise ReceiptError("legacy backup receipt requires an operations database")
    if ledger_source is None:
        raise ReceiptError("legacy backup receipt requires its live ledger source")
    verification = _restore_and_inspect(
        candidate,
        required_tables=set(LEDGER_REQUIRED_TABLES),
        count_tables=LEDGER_COUNT_TABLES,
    )
    run = _legacy_backup_run(
        operations_db,
        candidate,
        started_at=started_at,
        restored_counts=verification["counts"],
        recorded_candidate=recorded_candidate,
    )
    source_table_counts = _live_ledger_table_counts(ledger_source)
    if verification["table_counts"] != source_table_counts:
        raise ReceiptError(
            "legacy backup full application-table inventory does not match its live ledger source: "
            f"source={_stable_json(source_table_counts)} "
            f"actual={_stable_json(verification['table_counts'])}"
        )
    receipt_payload = {
        "kind": "legacy_sqlite",
        "snapshot_id": candidate.stem,
        "candidate_sha256": _sha256(candidate),
        "backup_run": run,
    }
    return {
        "kind": "legacy_sqlite",
        "snapshot_id": candidate.stem,
        "relative_path": candidate.name,
        "receipt_sha256": hashlib.sha256(_stable_json(receipt_payload).encode("utf-8")).hexdigest(),
        "ledger_counts": verification["counts"],
    }


def _kind_for(candidate: Path) -> str | None:
    if candidate.is_symlink():
        return None
    if candidate.is_dir() and IDENTITY_RE.fullmatch(candidate.name):
        return "recovery_bundle"
    if candidate.is_file() and LEGACY_RE.fullmatch(candidate.name):
        return "legacy_sqlite"
    return None


def _signature(candidate: Path, kind: str) -> tuple[str, str, int, int, int, int]:
    stat = candidate.stat()
    return (candidate.name, kind, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def write_inventory(backup_root: Path, output: Path) -> None:
    records: list[dict[str, object]] = []
    if backup_root.is_dir():
        for candidate in sorted(backup_root.iterdir(), key=lambda path: path.name):
            kind = _kind_for(candidate)
            if kind is None or candidate.parent.resolve() != backup_root.resolve():
                continue
            signature = _signature(candidate, kind)
            records.append(
                {
                    "name": signature[0],
                    "kind": signature[1],
                    "device": signature[2],
                    "inode": signature[3],
                    "size": signature[4],
                    "mtime_ns": signature[5],
                }
            )
    output.write_text(_stable_json(records) + "\n", encoding="utf-8")


def _inventory_signatures(path: Path) -> set[tuple[str, str, int, int, int, int]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"unable to read pre-backup inventory: {exc}") from exc
    if not isinstance(raw, list):
        raise ReceiptError("pre-backup inventory is not a list")
    signatures: set[tuple[str, str, int, int, int, int]] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ReceiptError("pre-backup inventory contains an invalid record")
        try:
            signature = (
                str(item["name"]),
                str(item["kind"]),
                int(item["device"]),
                int(item["inode"]),
                int(item["size"]),
                int(item["mtime_ns"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReceiptError("pre-backup inventory contains an invalid signature") from exc
        signatures.add(signature)
    return signatures


def capture_receipt(
    backup_root: Path,
    *,
    inventory_path: Path,
    required_kind: str,
    started_at: datetime,
    operations_db: Path | None,
    ledger_source: Path | None,
) -> dict[str, Any]:
    if required_kind not in {"any", "recovery_bundle"}:
        raise ReceiptError("required backup kind is invalid")
    if not backup_root.is_dir():
        raise ReceiptError(f"backup root is missing: {backup_root}")
    before = _inventory_signatures(inventory_path)
    receipts: list[dict[str, Any]] = []
    for candidate in sorted(backup_root.iterdir(), key=lambda path: path.name):
        kind = _kind_for(candidate)
        if kind is None or candidate.parent.resolve() != backup_root.resolve():
            continue
        if _signature(candidate, kind) in before:
            continue
        if kind == "recovery_bundle":
            receipts.append(verify_full_bundle(candidate, started_at=started_at))
        elif required_kind == "any":
            receipts.append(
                verify_legacy_snapshot(
                    candidate,
                    operations_db=operations_db,
                    ledger_source=ledger_source,
                    started_at=started_at,
                )
            )
    if required_kind == "recovery_bundle":
        receipts = [receipt for receipt in receipts if receipt["kind"] == "recovery_bundle"]
    if len(receipts) != 1:
        raise ReceiptError(
            f"expected exactly one fresh verified {required_kind} backup candidate, found {len(receipts)}"
        )
    return receipts[0]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inventory = subparsers.add_parser("inventory", help="record direct backup children before a run")
    inventory.add_argument("--backup-root", type=Path, required=True)
    inventory.add_argument("--output", type=Path, required=True)
    receipt = subparsers.add_parser("receipt", help="validate exactly one fresh backup candidate")
    receipt.add_argument("--backup-root", type=Path, required=True)
    receipt.add_argument("--inventory", type=Path, required=True)
    receipt.add_argument("--required-kind", choices=("any", "recovery_bundle"), required=True)
    receipt.add_argument("--started-at", required=True)
    receipt.add_argument("--operations-db", type=Path)
    receipt.add_argument("--ledger-source", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inventory":
            write_inventory(args.backup_root, args.output)
            return 0
        receipt = capture_receipt(
            args.backup_root,
            inventory_path=args.inventory,
            required_kind=args.required_kind,
            started_at=_utc(args.started_at),
            operations_db=args.operations_db,
            ledger_source=args.ledger_source,
        )
    except ReceiptError as exc:
        print(f"backup receipt validation failed: {exc}", file=sys.stderr)
        return 4
    print(
        "\t".join(
            (
                str(receipt["snapshot_id"]),
                str(receipt["kind"]),
                str(receipt["receipt_sha256"]),
                str(receipt["relative_path"]),
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
