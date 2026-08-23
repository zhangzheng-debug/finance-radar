#!/usr/bin/env python3
"""Fail-closed eligibility checks for a Finance Radar code-only release.

The fast path is deliberately limited to public Web code and non-runtime
release evidence. Everything that owns persistence, collection, dependencies,
services, recovery or the runtime model must be byte-identical to the active
release. A recent root-owned attestation binds the fast cutover to a full
restore drill that already completed successfully.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import sys
from typing import Any


MAX_BACKUP_AGE_SECONDS = 93_600
IGNORED_SUFFIXES = {".pyc", ".pyo"}
GENERATED_RUNTIME_ROOTS = {"data", "reports", "release-records"}
GENERATED_RUNTIME_FILES = {".env"}
# The fast path may carry public-Web code plus release evidence that is never
# imported or executed by a production unit.  Keep this list explicit: adding a
# new top-level tree must remain a reviewed change, and every API, persistence,
# collector, deployment, dependency, model and replay path remains protected by
# the byte-for-byte comparison below.
ALLOWED_CHANGE_PREFIXES = ("app/web/", ".streamlit/", "docs/", "tests/")
ALLOWED_CHANGE_DIRECTORIES = {"docs", "tests"}
ALLOWED_CHANGE_FILES = {
    "CHANGELOG.md",
    "CURRENT_STATE.md",
    "README.md",
    "VERSION",
}


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"code-only precondition failed: {message}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path, label: str) -> os.stat_result:
    try:
        result = path.lstat()
    except OSError as exc:
        fail(f"{label} is unavailable: {exc}")
    if not stat.S_ISREG(result.st_mode) or path.is_symlink():
        fail(f"{label} is not a non-symlink regular file")
    return result


def _private_root_path(path: Path, *, directory: bool) -> os.stat_result:
    try:
        result = path.lstat()
    except OSError as exc:
        fail(f"root attestation path is unavailable: {exc}")
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(result.st_mode) or path.is_symlink() or result.st_uid != 0:
        fail("root attestation path is not a root-owned non-symlink object")
    if result.st_mode & 0o077:
        fail("root attestation path is accessible outside root")
    return result


def _generated_runtime_path(relative: str, *, active_release: bool) -> bool:
    first_part = relative.split("/", 1)[0]
    if active_release:
        return relative in GENERATED_RUNTIME_FILES or first_part in GENERATED_RUNTIME_ROOTS
    # A source archive normally contains tracked reports, which the installer
    # always discards in favour of the shared reports link. Candidate data,
    # environment or release-record paths are never legitimate fast-path input:
    # retaining or merely staging them could forge persistence/audit state.
    return first_part == "reports"


def _allowed_change(relative: str) -> bool:
    return (
        relative in ALLOWED_CHANGE_FILES
        or relative in ALLOWED_CHANGE_DIRECTORIES
        or relative.startswith(ALLOWED_CHANGE_PREFIXES)
    )


def _inventory_release(
    root: Path,
    *,
    active_release: bool,
) -> dict[str, tuple[str, int, str]]:
    if not root.is_dir() or root.is_symlink():
        fail(f"release root is unavailable or unsafe: {root}")
    inventory: dict[str, tuple[str, int, str]] = {}
    for candidate in root.rglob("*"):
        relative = candidate.relative_to(root).as_posix()
        if _generated_runtime_path(relative, active_release=active_release):
            continue
        if "__pycache__" in candidate.parts or candidate.suffix in IGNORED_SUFFIXES:
            # The active Python services naturally create bytecode.  A release
            # archive must never ship it: ignoring candidate bytecode would let
            # executable payloads bypass the all-tree comparison.
            if not active_release:
                fail(f"candidate contains generated Python bytecode: {relative}")
            continue
        if candidate.is_symlink():
            fail(f"release path is a symlink: {relative}")
        if candidate.is_file():
            result = _regular_file(candidate, relative)
            inventory[relative] = ("file", result.st_size, _sha256(candidate))
        elif candidate.is_dir():
            inventory[relative] = ("directory", 0, "")
        else:
            fail(f"release path is not regular: {relative}")
    return inventory


def check_contract(previous: Path, candidate: Path) -> None:
    previous_inventory = _inventory_release(previous, active_release=True)
    candidate_inventory = _inventory_release(candidate, active_release=False)
    all_paths = sorted(previous_inventory.keys() | candidate_inventory.keys())
    changed_paths = [
        relative
        for relative in all_paths
        if previous_inventory.get(relative) != candidate_inventory.get(relative)
    ]
    forbidden = [relative for relative in changed_paths if not _allowed_change(relative)]
    if forbidden:
        fail(
            "release content outside the public-Web whitelist changed; "
            f"use full deployment: {forbidden[:3]}"
        )
    print(
        "code_only_candidate_contract=PASS "
        f"compared_files={len(all_paths)} allowed_changes={len(changed_paths)}"
    )


def _utc(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        fail("verified backup timestamp is invalid")
    if parsed.tzinfo is None:
        fail("verified backup timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _safe_payload(bundle: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        fail(f"unsafe backup payload path: {relative!r}")
    candidate = bundle.joinpath(*pure.parts)
    _regular_file(candidate, f"backup payload {relative}")
    try:
        if bundle.resolve() not in candidate.resolve().parents:
            fail(f"backup payload escaped its bundle: {relative}")
    except OSError as exc:
        fail(f"backup payload cannot be resolved: {exc}")
    return candidate


def check_backup(
    operations_path: Path,
    backup_root: Path,
    attestation_path: Path,
    max_age_seconds: int,
) -> None:
    if not 3_600 <= max_age_seconds <= MAX_BACKUP_AGE_SECONDS:
        fail(f"backup age policy must be between 3600 and {MAX_BACKUP_AGE_SECONDS} seconds")
    _regular_file(operations_path, "operations database")
    if not backup_root.is_dir() or backup_root.is_symlink():
        fail("operational backup root is unavailable or unsafe")
    _private_root_path(attestation_path.parent, directory=True)
    _private_root_path(attestation_path, directory=False)
    try:
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"root backup attestation is invalid: {exc}")
    if attestation.get("format") != "finance-radar-root-backup-attestation-v1":
        fail("root backup attestation format is invalid")

    uri = f"file:{operations_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute(
            """SELECT backup_id,backup_path,source_bytes,backup_bytes,quick_check,
                      restored_count_json,status,created_at,verified_at,manifest_path,
                      components_json,snapshot_kind
               FROM backup_runs WHERE status='VERIFIED'
               ORDER BY verified_at DESC,created_at DESC LIMIT 1"""
        ).fetchone()
    if row is None:
        fail("no verified backup record exists")
    record = dict(row)
    if record.get("quick_check") != "ok" or record.get("snapshot_kind") != "recovery_bundle":
        fail("latest backup record is not a verified full recovery bundle")
    age = (datetime.now(timezone.utc) - _utc(record.get("verified_at"))).total_seconds()
    if age < -300 or age > max_age_seconds:
        fail(f"latest verified backup is outside policy: age={age:.0f}s")

    manifest_path = Path(str(record.get("manifest_path") or ""))
    if Path(str(record.get("backup_path") or "")) != manifest_path:
        fail("backup record paths disagree")
    _regular_file(manifest_path, "verified backup manifest")
    bundle = manifest_path.parent
    if bundle.is_symlink() or not bundle.is_dir():
        fail("verified backup bundle is unavailable or unsafe")
    try:
        if bundle.parent.resolve() != backup_root.resolve():
            fail("verified backup is not a direct child of the backup root")
    except OSError as exc:
        fail(f"verified backup path cannot be resolved: {exc}")

    manifest_bytes = manifest_path.read_bytes()
    if len(manifest_bytes) > 8 * 1024 * 1024:
        fail("verified backup manifest is unexpectedly large")
    try:
        manifest = json.loads(manifest_bytes)
        recorded_components = json.loads(record.get("components_json") or "")
        restored_counts = json.loads(record.get("restored_count_json") or "")
    except json.JSONDecodeError as exc:
        fail(f"verified backup metadata JSON is invalid: {exc}")
    if manifest.get("format") != "finance-radar-recovery-bundle-v1":
        fail("verified backup manifest format is invalid")
    if manifest.get("snapshot_id") != bundle.name:
        fail("verified backup snapshot id does not match its directory")
    if manifest.get("components") != recorded_components:
        fail("verified backup component receipts disagree")
    if not isinstance(restored_counts, dict) or "canonical_events" not in restored_counts:
        fail("verified restore counts are incomplete")

    exact_fields = {
        "backup_id": record["backup_id"],
        "snapshot_id": bundle.name,
        "manifest_path": str(manifest_path),
        "status": record["status"],
        "verified_at": record["verified_at"],
        "quick_check": record["quick_check"],
        "snapshot_kind": record["snapshot_kind"],
        "source_bytes": record["source_bytes"],
        "backup_bytes": record["backup_bytes"],
        "restored_count_json": record["restored_count_json"],
        "components": recorded_components,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    for key, expected in exact_fields.items():
        if attestation.get(key) != expected:
            fail(f"root attestation no longer matches verified backup field: {key}")

    files = manifest.get("files")
    recorded_stats = attestation.get("payload_stats")
    if not isinstance(files, list) or not files or not isinstance(recorded_stats, list):
        fail("verified backup file inventory is incomplete")
    stats_by_path = {
        item.get("path"): item
        for item in recorded_stats
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    if len(stats_by_path) != len(recorded_stats) or len(files) != len(recorded_stats):
        fail("root attestation payload inventory has duplicates or omissions")
    manifest_paths: list[str] = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            fail("verified backup manifest file inventory is invalid")
        relative = item["path"]
        expected_sha = item.get("sha256")
        if (
            not isinstance(expected_sha, str)
            or len(expected_sha) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha)
        ):
            fail(f"verified backup payload hash is invalid: {relative}")
        expected_bytes = item.get("bytes")
        if (
            not isinstance(expected_bytes, int)
            or isinstance(expected_bytes, bool)
            or expected_bytes < 0
        ):
            fail(f"verified backup payload byte count is invalid: {relative}")
        manifest_paths.append(relative)
        candidate = _safe_payload(bundle, relative)
        result = candidate.lstat()
        expected_stat = stats_by_path.get(relative)
        current_sha = _sha256(candidate)
        current = {
            "path": relative,
            "bytes": result.st_size,
            "device": result.st_dev,
            "inode": result.st_ino,
            "mtime_ns": result.st_mtime_ns,
            "sha256": current_sha,
        }
        if (
            expected_stat != current
            or current_sha != expected_sha
            or result.st_size != expected_bytes
        ):
            fail(f"verified backup payload changed after attestation: {relative}")

    if len(set(manifest_paths)) != len(manifest_paths):
        fail("verified backup manifest contains duplicate payload paths")
    actual_bundle_files: set[str] = set()
    for candidate in bundle.rglob("*"):
        relative = candidate.relative_to(bundle).as_posix()
        if candidate.is_symlink():
            fail(f"verified backup bundle contains a symlink: {relative}")
        if candidate.is_file():
            actual_bundle_files.add(relative)
        elif not candidate.is_dir():
            fail(f"verified backup bundle contains a special object: {relative}")
    expected_bundle_files = set(manifest_paths) | {"manifest.json"}
    if actual_bundle_files != expected_bundle_files:
        added = sorted(actual_bundle_files - expected_bundle_files)
        missing = sorted(expected_bundle_files - actual_bundle_files)
        fail(
            "verified backup bundle file set changed after attestation; "
            f"added={added[:3]} missing={missing[:3]}"
        )

    payload = {
        "backup_id": str(record["backup_id"]),
        "snapshot_id": bundle.name,
        "manifest_sha256": exact_fields["manifest_sha256"],
        "age_seconds": int(age),
    }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    commands = result.add_subparsers(dest="command", required=True)
    contract = commands.add_parser("contract")
    contract.add_argument("--previous", type=Path, required=True)
    contract.add_argument("--candidate", type=Path, required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--operations", type=Path, required=True)
    backup.add_argument("--backup-root", type=Path, required=True)
    backup.add_argument("--attestation", type=Path, required=True)
    backup.add_argument("--max-age-seconds", type=int, default=MAX_BACKUP_AGE_SECONDS)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.command == "contract":
        check_contract(args.previous, args.candidate)
    else:
        check_backup(args.operations, args.backup_root, args.attestation, args.max_age_seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
