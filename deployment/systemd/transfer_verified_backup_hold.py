#!/usr/bin/env python3
"""Move one verified recovery bundle into root-only deployment custody.

The deployment host intentionally retains only one daily recovery bundle.  A
physical second copy of a multi-gigabyte evidence tree made a protected
cutover impossible on the supported 40-GB volume.  This helper revalidates the
fresh bundle, atomically removes it from normal retention, prunes only older
verified-bundle directories, and proves that enough space remains for the
post-cutover bundle and its isolated SQLite restore.

It is invoked only by the root-owned installer while the worker and backup
timer are stopped.  On any failure after the custody move, the fresh bundle is
moved back to the operational backup root before the helper exits non-zero.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys
from typing import Any


SAFETY_BYTES = 512 * 1024 * 1024


def fail(message: str) -> None:
    raise RuntimeError(message)


def lstat_directory(
    path: Path,
    label: str,
    *,
    private_root_only: bool = False,
) -> os.stat_result:
    try:
        result = os.lstat(path)
    except OSError as exc:
        fail(f"{label} is unavailable: {exc}")
    if stat.S_ISLNK(result.st_mode) or not stat.S_ISDIR(result.st_mode):
        fail(f"{label} is not a real directory")
    if private_root_only and (
        result.st_uid != 0
        or result.st_gid != 0
        or stat.S_IMODE(result.st_mode) & 0o077
    ):
        fail(f"{label} must be a root-owned private directory")
    return result


def load_verifier(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        fail("backup receipt verifier is unavailable")
    spec = importlib.util.spec_from_file_location(
        "finance_radar_backup_hold_receipt",
        path,
    )
    if spec is None or spec.loader is None:
        fail("unable to load backup receipt verifier")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def verify_bundle(module: Any, path: Path, receipt_sha256: str) -> dict[str, Any]:
    baseline = datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        receipt = module.verify_full_bundle(path, started_at=baseline)
    except module.ReceiptError as exc:
        fail(f"predeploy hold receipt validation failed: {exc}")
    actual = str(receipt.get("receipt_sha256") or "")
    if actual != receipt_sha256:
        fail("predeploy hold manifest hash does not match the verified receipt")
    return receipt


def inspect_bundle_size(source: Path) -> tuple[int, int]:
    logical_bytes = 0
    largest_sqlite_bytes = 0
    for directory, names, filenames in os.walk(source, followlinks=False):
        root = Path(directory)
        for name in names:
            child = root / name
            child_stat = os.lstat(child)
            if stat.S_ISLNK(child_stat.st_mode) or not stat.S_ISDIR(child_stat.st_mode):
                fail(f"bundle contains an unsafe directory: {child.relative_to(source)}")
        for name in filenames:
            child = root / name
            child_stat = os.lstat(child)
            if stat.S_ISLNK(child_stat.st_mode) or not stat.S_ISREG(child_stat.st_mode):
                fail(f"bundle contains an unsafe file: {child.relative_to(source)}")
            logical_bytes += child_stat.st_size
            if name.endswith(".sqlite3"):
                largest_sqlite_bytes = max(largest_sqlite_bytes, child_stat.st_size)
    if logical_bytes <= 0 or largest_sqlite_bytes <= 0:
        fail("predeploy recovery bundle has no measurable SQLite recovery payload")
    return logical_bytes, largest_sqlite_bytes


def validated_superseded_bundles(backup_root: Path) -> list[Path]:
    candidates: list[Path] = []
    for child in sorted(backup_root.iterdir(), key=lambda item: item.name):
        if not child.name.startswith("finance_radar_"):
            continue
        child_stat = os.lstat(child)
        if stat.S_ISLNK(child_stat.st_mode) or not stat.S_ISDIR(child_stat.st_mode):
            # Legacy single-file snapshots are not touched by this recovery-
            # bundle transaction; a link or special path must fail closed.
            if stat.S_ISREG(child_stat.st_mode):
                continue
            fail(f"unsafe superseded backup path: {child}")
        manifest = child / "manifest.json"
        manifest_stat = os.lstat(manifest)
        if stat.S_ISLNK(manifest_stat.st_mode) or not stat.S_ISREG(manifest_stat.st_mode):
            fail(f"superseded recovery bundle lacks a regular manifest: {child}")
        candidates.append(child)
    return candidates


def required_headroom(
    backup_root: Path,
    receipt_tmpdir: Path,
    *,
    bundle_bytes: int,
    largest_sqlite_bytes: int,
) -> tuple[int, int]:
    planned_by_device: dict[int, dict[str, int]] = {}

    def reserve(path: Path, label: str, amount: int) -> None:
        device = os.stat(path).st_dev
        filesystem = os.statvfs(path)
        available = filesystem.f_bavail * filesystem.f_frsize
        plan = planned_by_device.setdefault(device, {"available": available})
        plan["available"] = min(plan["available"], available)
        plan[label] = plan.get(label, 0) + amount

    reserve(backup_root, "projected_postcutover_bundle", bundle_bytes)
    reserve(receipt_tmpdir, "projected_sqlite_receipt_scratch", largest_sqlite_bytes)
    minimum_available = 0
    maximum_required = 0
    for device, plan in planned_by_device.items():
        planned = sum(value for key, value in plan.items() if key != "available")
        required = planned + SAFETY_BYTES
        available = plan["available"]
        minimum_available = available if minimum_available == 0 else min(minimum_available, available)
        maximum_required = max(maximum_required, required)
        if available < required:
            fail(
                "atomic predeploy custody storage headroom insufficient: "
                f"device={device} available_bytes={available} required_bytes={required} "
                f"projected_postcutover_bundle_bytes={plan.get('projected_postcutover_bundle', 0)} "
                f"projected_sqlite_receipt_scratch_bytes={plan.get('projected_sqlite_receipt_scratch', 0)} "
                f"safety_bytes={SAFETY_BYTES}"
            )
    return minimum_available, maximum_required


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def transfer(args: argparse.Namespace) -> dict[str, Any]:
    source: Path = args.source
    backup_root: Path = args.backup_root
    hold_root: Path = args.hold_root
    receipt_tmpdir: Path = args.receipt_tmpdir
    receipt_sha256: str = args.receipt_sha256.lower()

    if any(not path.is_absolute() for path in (source, backup_root, hold_root, receipt_tmpdir)):
        fail("predeploy custody paths must be absolute")
    if source.parent != backup_root or source.name in {"", ".", ".."}:
        fail("predeploy source is not a direct operational-backups child")
    if len(receipt_sha256) != 64 or any(character not in "0123456789abcdef" for character in receipt_sha256):
        fail("invalid predeploy receipt hash")
    if os.path.lexists(hold_root):
        fail("predeploy hold already exists")

    backup_stat = lstat_directory(backup_root, "operational backup root")
    lstat_directory(hold_root.parent, "predeploy hold parent", private_root_only=True)
    lstat_directory(hold_root.parent.parent, "predeploy hold grandparent", private_root_only=True)
    lstat_directory(receipt_tmpdir, "receipt verifier temporary directory", private_root_only=True)
    source_stat = lstat_directory(source, "fresh verified recovery bundle")
    if source_stat.st_dev != backup_stat.st_dev or source_stat.st_dev != os.stat(hold_root.parent).st_dev:
        fail("predeploy custody transfer must remain on one filesystem")

    verifier = load_verifier(args.verifier)
    verify_bundle(verifier, source, receipt_sha256)
    logical_bytes, largest_sqlite_bytes = inspect_bundle_size(source)
    superseded = validated_superseded_bundles(backup_root)
    if source in superseded:
        superseded.remove(source)

    destination = hold_root / source.name
    moved = False
    committed = False
    pruned: list[str] = []
    try:
        hold_root.mkdir(mode=0o700)
        os.rename(source, destination)
        moved = True
        destination_stat = os.lstat(destination)
        if (
            not stat.S_ISDIR(destination_stat.st_mode)
            or destination_stat.st_dev != source_stat.st_dev
            or destination_stat.st_ino != source_stat.st_ino
            or os.path.lexists(source)
        ):
            fail("predeploy custody move did not preserve the verified bundle identity")
        fsync_directory(backup_root)
        fsync_directory(hold_root)
        held_receipt = verify_bundle(verifier, destination, receipt_sha256)

        for candidate in superseded:
            shutil.rmtree(candidate)
            pruned.append(candidate.name)
        fsync_directory(backup_root)

        available_bytes, required_bytes = required_headroom(
            backup_root,
            receipt_tmpdir,
            bundle_bytes=logical_bytes,
            largest_sqlite_bytes=largest_sqlite_bytes,
        )
        metadata = {
            "kind": "recovery_bundle",
            "original_path": str(source),
            "hold_path": str(destination),
            "receipt_sha256": receipt_sha256,
            "held_receipt_sha256": str(held_receipt.get("receipt_sha256") or ""),
            "protection": "root-owned atomic custody transfer; source pathname removed from normal retention",
            "pruned_superseded_bundles": pruned,
            "available_bytes_after_prune": available_bytes,
            "required_bytes_for_postcutover_backup": required_bytes,
        }
        metadata_path = hold_root / "HOLD_RECEIPT.json"
        with metadata_path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(metadata_path, 0o400)
        fsync_directory(hold_root)
        committed = True
        return metadata
    finally:
        if moved and not committed and destination.exists() and not os.path.lexists(source):
            os.rename(destination, source)
            fsync_directory(backup_root)
        if not committed and hold_root.exists():
            try:
                hold_root.rmdir()
            except OSError:
                pass


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--source", type=Path, required=True)
    result.add_argument("--backup-root", type=Path, required=True)
    result.add_argument("--hold-root", type=Path, required=True)
    result.add_argument("--receipt-sha256", required=True)
    result.add_argument("--verifier", type=Path, required=True)
    result.add_argument("--receipt-tmpdir", type=Path, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        metadata = transfer(parser().parse_args(argv))
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 4
    print(json.dumps(metadata, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
