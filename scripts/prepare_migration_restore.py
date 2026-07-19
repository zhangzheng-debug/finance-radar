#!/usr/bin/env python3
"""Validate and fully prepare a plaintext Finance Radar migration archive.

This is the second restore gate after ``audit_migration_restore.py`` has
authenticated and decrypted the encrypted off-host backup. It extracts every
manifested regular file into a new staging directory, skips archive symlinks,
and writes an explicit Linux symlink plan for the activation script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from scripts.audit_migration_restore import (
        MAX_MEMBERS,
        MAX_UNPACKED_BYTES,
        _parse_manifest,
        _safe_member_name,
        _stream_regular_file,
        _validate_link,
        sha256_file,
    )
except ModuleNotFoundError:  # Direct execution from scripts/.
    from audit_migration_restore import (
        MAX_MEMBERS,
        MAX_UNPACKED_BYTES,
        _parse_manifest,
        _safe_member_name,
        _stream_regular_file,
        _validate_link,
        sha256_file,
    )


ARCHIVE_RE = re.compile(r"finance-radar-migration-(\d{8}T\d{6}Z)\.tgz$")
RELEASE_RE = re.compile(r"\d{8}T\d{6}Z")
FORBIDDEN_PARTS = {"ethusdc-pivot-bot", "server_migration_backup", ".ssh", ".ssh1"}
CAPTURE_NAMES = {"CURRENT_RELEASE.txt", "MANIFEST.sha256"}
LOCAL_EVIDENCE_MODEL_PATH = "evidence-llm/models/qwen2.5-0.5b-instruct-q4_k_m.gguf"
LOCAL_EVIDENCE_MODEL_SHA256 = (
    "74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_target_base(value: str) -> str:
    target = PurePosixPath(value)
    if not target.is_absolute() or ".." in target.parts or len(target.parts) < 3:
        raise ValueError("target base must be an absolute dedicated Linux path")
    if target.parts[:2] != ("/", "opt"):
        raise ValueError("target base must be below /opt")
    if any(part.lower() in FORBIDDEN_PARTS for part in target.parts):
        raise ValueError("target base intersects a forbidden project or credential path")
    return target.as_posix()


def inspect_plain_archive(
    archive_path: Path,
    *,
    expected_release: str,
    expected_sha256: str,
) -> dict[str, Any]:
    archive_path = archive_path.resolve()
    if not archive_path.is_file():
        raise FileNotFoundError(f"migration archive not found: {archive_path}")
    if not RELEASE_RE.fullmatch(expected_release):
        raise ValueError("expected release must use YYYYMMDDTHHMMSSZ")
    match = ARCHIVE_RE.fullmatch(archive_path.name)
    if not match:
        raise ValueError("plaintext archive name is not a Finance Radar migration snapshot")
    expected_root = archive_path.name.removesuffix(".tgz")
    actual_sha256 = sha256_file(archive_path)
    if actual_sha256 != expected_sha256.lower():
        raise ValueError(f"archive SHA-256 mismatch: expected={expected_sha256} actual={actual_sha256}")

    hashes: dict[str, str] = {}
    captures: dict[str, bytes] = {}
    links: list[dict[str, str]] = []
    member_count = 0
    regular_files = 0
    unpacked_bytes = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            member_count += 1
            if member_count > MAX_MEMBERS:
                raise ValueError("archive member limit exceeded")
            relative = _safe_member_name(member.name, expected_root)
            if any(part.lower() in FORBIDDEN_PARTS for part in PurePosixPath(relative).parts):
                raise ValueError(f"forbidden path present in migration archive: {relative}")
            if member.isdir():
                continue
            if member.issym() or member.islnk():
                _validate_link(member, expected_root)
                links.append({"path": relative, "target": member.linkname})
                continue
            if not member.isfile():
                raise ValueError(f"unsupported special archive member: {member.name}")
            regular_files += 1
            unpacked_bytes += member.size
            if unpacked_bytes > MAX_UNPACKED_BYTES:
                raise ValueError("archive unpacked byte limit exceeded")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"could not read archive member: {member.name}")
            digest, captured = _stream_regular_file(source, capture=relative in CAPTURE_NAMES)
            hashes[relative] = digest
            if captured is not None:
                captures[relative] = captured

    manifest_raw = captures.get("MANIFEST.sha256")
    if manifest_raw is None:
        raise ValueError("MANIFEST.sha256 is missing")
    manifest = _parse_manifest(manifest_raw)
    missing = sorted(set(manifest) - set(hashes))
    unexpected = sorted(set(hashes) - set(manifest) - {"MANIFEST.sha256"})
    mismatched = sorted(
        name for name, expected in manifest.items() if hashes.get(name) != expected
    )
    if missing or unexpected or mismatched:
        raise ValueError(
            f"manifest verification failed: missing={missing[:5]} "
            f"unexpected={unexpected[:5]} mismatched={mismatched[:5]}"
        )

    current = captures.get("CURRENT_RELEASE.txt", b"").decode("utf-8").strip()
    expected_current = f"/opt/finance-radar/releases/{expected_release}"
    if current != expected_current:
        raise ValueError(f"CURRENT_RELEASE mismatch: expected={expected_current!r} got={current!r}")
    required = {
        f"releases/{expected_release}/app/api/main.py",
        f"releases/{expected_release}/app/web/Home.py",
        f"releases/{expected_release}/requirements.txt",
        "shared/data/finance_radar.sqlite3",
        "shared/data/finance_radar_operations.sqlite3",
        "config/etc/finance-radar.env",
    }
    missing_required = sorted(required - set(hashes))
    if missing_required:
        raise ValueError(f"required restore files missing: {missing_required}")
    model_unit = (
        f"releases/{expected_release}/deployment/systemd/"
        "finance-radar-evidence-llm.service"
    )
    model_required = model_unit in hashes
    model_hash = hashes.get(LOCAL_EVIDENCE_MODEL_PATH)
    if model_required and model_hash != LOCAL_EVIDENCE_MODEL_SHA256:
        raise ValueError("local evidence model is missing or has an unexpected SHA-256")
    return {
        "archive_sha256": actual_sha256,
        "archive_root": expected_root,
        "snapshot": match.group(1),
        "expected_release": expected_release,
        "members": member_count,
        "regular_files": regular_files,
        "unpacked_bytes": unpacked_bytes,
        "manifest": manifest,
        "links": links,
        "local_evidence_model": {
            "required_by_release": model_required,
            "included": model_hash is not None,
            "sha256": model_hash,
            "pinned_sha256_match": model_hash == LOCAL_EVIDENCE_MODEL_SHA256,
        },
    }


def prepare_restore(
    archive_path: Path,
    destination: Path,
    *,
    expected_release: str,
    expected_sha256: str,
    target_base: str = "/opt/finance-radar",
) -> dict[str, Any]:
    target_base = _validate_target_base(target_base)
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"restore destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    inspection = inspect_plain_archive(
        archive_path,
        expected_release=expected_release,
        expected_sha256=expected_sha256,
    )
    partial = destination.with_name(f"{destination.name}.partial-{uuid.uuid4().hex}")
    partial.mkdir(mode=0o700)
    extracted_files = 0
    try:
        with tarfile.open(archive_path.resolve(), "r:gz") as archive:
            for member in archive:
                relative = _safe_member_name(member.name, inspection["archive_root"])
                if not relative or member.issym() or member.islnk():
                    continue
                target = partial.joinpath(*PurePosixPath(relative).parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    os.chmod(target, member.mode & 0o777)
                    continue
                if not member.isfile():
                    raise ValueError(f"unsupported special archive member: {member.name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(f"could not read archive member: {member.name}")
                with target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                os.chmod(target, member.mode & 0o777)
                extracted_files += 1

        manifest: dict[str, str] = inspection["manifest"]
        mismatched = [
            name
            for name, digest in manifest.items()
            if not (partial / Path(*PurePosixPath(name).parts)).is_file()
            or sha256_file(partial / Path(*PurePosixPath(name).parts)) != digest
        ]
        if mismatched:
            raise ValueError(f"post-extraction manifest verification failed: {mismatched[:5]}")

        link_plan: list[dict[str, str]] = []
        # Recreate only links that existed in the source archive. Some early
        # historical releases legitimately contain materialized data/report
        # directories; converting every release would destroy that history.
        for archived_link in inspection["links"]:
            relative = PurePosixPath(archived_link["path"])
            if (
                len(relative.parts) != 3
                or relative.parts[0] != "releases"
                or relative.name not in {"data", "reports"}
            ):
                raise ValueError(f"unsupported migration symlink: {relative}")
            link_path = relative.as_posix()
            target = f"{target_base}/shared/{relative.name}"
            if partial.joinpath(*relative.parts).exists():
                raise ValueError(f"restore link path unexpectedly materialized: {link_path}")
            link_plan.append({"path": link_path, "target": target})
        link_plan.sort(key=lambda item: item["path"])
        (partial / "SYMLINK_PLAN.json").write_text(
            json.dumps(link_plan, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        report = {
            "schema_version": 1,
            "prepared_at": utc_now(),
            "status": "PREPARED_NOT_ACTIVATED",
            "source_archive": str(archive_path.resolve()),
            "archive_sha256": inspection["archive_sha256"],
            "snapshot": inspection["snapshot"],
            "expected_release": expected_release,
            "target_base": target_base,
            "destination": str(destination),
            "members_scanned": inspection["members"],
            "manifest_entries_verified": len(manifest),
            "regular_files_extracted": extracted_files,
            "unpacked_bytes_scanned": inspection["unpacked_bytes"],
            "symlinks_skipped": len(inspection["links"]),
            "symlinks_planned": len(link_plan),
            "activation_required": True,
            "local_evidence_model": inspection["local_evidence_model"],
            "boundaries": {
                "existing_destination_overwritten": False,
                "trading_project_included": False,
                "credentials_or_ssh_paths_included": False,
                "archive_symlinks_followed": False,
            },
        }
        (partial / "PREPARED_RESTORE.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(partial, destination)
        return report
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise


def render_markdown(report: dict[str, Any]) -> str:
    boundaries = report.get("boundaries") or {}
    return "\n".join(
        [
            "# Finance Radar full service-restore preparation drill",
            "",
            f"- Result: **{report.get('status')}**",
            f"- Snapshot: `{report.get('snapshot')}`",
            f"- Expected release: `{report.get('expected_release')}`",
            f"- Archive SHA-256: `{report.get('archive_sha256')}`",
            f"- Members scanned: `{report.get('members_scanned')}`",
            f"- Manifest entries verified: `{report.get('manifest_entries_verified')}`",
            f"- Regular files extracted: `{report.get('regular_files_extracted')}`",
            f"- Unpacked bytes scanned: `{report.get('unpacked_bytes_scanned')}`",
            f"- Archive symlinks skipped/planned: `{report.get('symlinks_skipped')}` / `{report.get('symlinks_planned')}`",
            f"- Existing destination overwritten: `{boundaries.get('existing_destination_overwritten')}`",
            f"- Trading project included: `{boundaries.get('trading_project_included')}`",
            f"- Archive symlinks followed: `{boundaries.get('archive_symlinks_followed')}`",
            "",
            "`PREPARED_NOT_ACTIVATED` proves that the complete authenticated archive can be safely materialized without overwriting a target or starting services. Actual replacement-VPS activation still requires the explicit `--activate` gate.",
            "",
        ]
    )


def write_report(path: Path, report: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.with_suffix(".md").write_text(render_markdown(report), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--expected-release", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--target-base", default="/opt/finance-radar")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = prepare_restore(
        args.archive,
        args.destination,
        expected_release=args.expected_release,
        expected_sha256=args.expected_sha256,
        target_base=args.target_base,
    )
    if args.report:
        write_report(args.report, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
