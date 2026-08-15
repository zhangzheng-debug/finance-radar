#!/usr/bin/env python3
"""Create and verify secret-safe, non-deploying Finance Radar release records."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

try:
    from scripts.release_identity import validate_release_id
except ModuleNotFoundError:  # Direct execution from scripts/.
    from release_identity import validate_release_id


SCHEMA_VERSION = 1
MANIFEST_KIND = "finance-radar-release-manifest"
ACCEPTANCE_KIND = "finance-radar-release-acceptance"
MAX_MANIFEST_BYTES = 5 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 100_000
MAX_DIRTY_SOURCE_INVENTORY_MEMBERS = 20_000
CHECK_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
CHECK_STATUSES = frozenset({"PASS", "FAIL", "SKIPPED", "NOT_RUN"})
DIRTY_SOURCE_ARCHIVE_INVENTORY_FORMAT = "finance-radar-dirty-source-archive-inventory-v1"
HIGH_CONFIDENCE_SECRET_PATTERNS: tuple[re.Pattern[bytes], ...] = (
    re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}\b"),
    re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        rb"FINANCE_RADAR_(?:ADMIN|REVIEWER|OPERATOR)_TOKEN\s*=\s*['\"]?[0-9A-Fa-f]{32,}"
    ),
    re.compile(rb"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
)

# This is a release-contract manifest, not a scan of the whole workstation.
# A full deploy archive should be passed with --artifact so its exact hash is
# bound to the release without enumerating unrelated or secret local files.
DEFAULT_CRITICAL_FILES: tuple[str, ...] = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements.lock",
    "requirements-dev.lock",
    "dependency-lock.json",
    "app/api/main.py",
    "app/ops/backup.py",
    "app/config.py",
    "app/models/risk_router.py",
    "app/models/evidence_policy.py",
    "app/services/evidence_agent.py",
    "app/services/light_verification.py",
    "app/storage/ledger.py",
    "app/storage/operations.py",
    "app/workers/continuous.py",
    "app/web/Home.py",
    "app/web/Admin.py",
    "app/web/Reviewer.py",
    "app/web/Operator.py",
    "app/web/common.py",
    "app/web/components.py",
    "app/web/design_tokens_v3.css",
    "app/web/style_v3.css",
    "app/web/pages/1_Event_Intelligence.py",
    "app/web/pages/2_Replay_Lab.py",
    "app/web/pages/3_Operations_and_Model.py",
    "app/web/pages/4_Adjudication_Studio.py",
    "app/web/pages/5_Method_and_Boundaries.py",
    "deployment/Caddyfile",
    "deployment/compose.yml",
    "deployment/Dockerfile",
    "deployment/systemd/finance-radar-api.service",
    "deployment/systemd/finance-radar-web.service",
    "deployment/systemd/finance-radar-admin.service",
    "deployment/systemd/finance-radar-reviewer.service",
    "deployment/systemd/finance-radar-operator.service",
    "deployment/systemd/finance-radar-worker.service",
    "deployment/systemd/finance-radar-worker-send.conf",
    "deployment/systemd/finance-radar-backup.service",
    "deployment/systemd/finance-radar-backup.timer",
    "deployment/systemd/finance-radar-evidence-llm.service",
    "deployment/systemd/finance-radar.slice",
    "deployment/systemd/run_backup_quiesced.sh",
    "deployment/systemd/install_remote.sh",
    "deployment/systemd/verify_backup_receipt.py",
    "deployment/systemd/activate_prepared_restore.sh",
    "deployment/systemd/create_migration_backup.sh",
    "deployment/systemd/install_direct_endpoint.sh",
    "deployment/systemd/install_local_evidence_model.sh",
    "deployment/systemd/certbot-reload-nginx.sh",
    "deployment/systemd/nginx-radar-direct.conf",
    "deployment/systemd/nginx-radar-locations.conf",
    "scripts/light_verify.py",
    "scripts/apply_authorized_rough_reviews.py",
    "scripts/official_primary_page_enricher.py",
    "scripts/run_live_cycle.py",
    "scripts/audit_migration_restore.py",
    "scripts/prepare_migration_restore.py",
    "scripts/restore_migration_to_vps.ps1",
    "scripts/release_audit.py",
    "scripts/release_identity.py",
    "scripts/verify_dependency_locks.py",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_release_bytes(data: bytes) -> tuple[bytes, str]:
    """Normalize UTF-8 text line endings without ever treating binary data as text."""

    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return data, "raw_bytes"
    return data.replace(b"\r\n", b"\n"), "utf8_lf_normalized"


def _entry_content_bytes(data: bytes, entry: dict[str, Any]) -> bytes:
    """Apply the manifest's explicit content basis to one source/archive member."""

    if entry.get("hash_basis", "workspace") == "utf8_lf_normalized":
        normalized, _basis = _canonical_release_bytes(data)
        return normalized
    return data


def assert_no_high_confidence_secret(path: Path) -> None:
    # Release-contract files are intentionally small text/code assets. Refuse
    # obvious live credentials while allowing documented placeholders and env
    # variable names such as ${FINANCE_RADAR_ADMIN_TOKEN}.
    if path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("critical release file is too large for secret audit")
    data = path.read_bytes()
    if any(pattern.search(data) for pattern in HIGH_CONFIDENCE_SECRET_PATTERNS):
        raise ValueError("high-confidence secret detected in critical release file")


def _validate_release_id(value: str) -> str:
    return validate_release_id(value)


def _safe_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} or ":" in part for part in path.parts)
    ):
        raise ValueError("unsafe relative release path")
    if _is_sensitive_path(path):
        raise ValueError("sensitive input path rejected")
    return path.as_posix()


def _is_sensitive_path(path: PurePosixPath) -> bool:
    lowered_parts = tuple(part.lower() for part in path.parts)
    if any(
        part in {".git", "server_migration_backup", "secrets", "credentials"}
        for part in lowered_parts
    ):
        return True
    base = lowered_parts[-1] if lowered_parts else ""
    if base == ".env.example":
        return False
    if base == ".env" or base.startswith(".env."):
        return True
    if base in {
        "credentials.json",
        "secrets.json",
        "secrets.toml",
        "credentials.toml",
        "id_rsa",
        "id_ed25519",
        "known_hosts",
    }:
        return True
    if any(marker in base for marker in ("passphrase", "private_key", "privkey")):
        return True
    return base.endswith((".key", ".pem", ".p12", ".pfx", ".session", ".session-journal"))


def inspect_git(root: Path) -> dict[str, Any]:
    """Read commit identity and dirty state without reading diff contents/remotes."""

    def run(*args: str) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                ["git", "-C", str(root), *args],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError:
            return None

    top = run("rev-parse", "--show-toplevel")
    if top is None or top.returncode != 0:
        return {"available": False, "commit": None, "dirty": None}
    try:
        if Path(top.stdout.strip()).resolve() != root.resolve():
            return {"available": False, "commit": None, "dirty": None}
    except OSError:
        return {"available": False, "commit": None, "dirty": None}
    commit_result = run("rev-parse", "HEAD")
    status_result = run("status", "--porcelain=v1", "--untracked-files=all")
    if (
        commit_result is None
        or status_result is None
        or commit_result.returncode != 0
        or status_result.returncode != 0
    ):
        return {"available": False, "commit": None, "dirty": None}
    commit = commit_result.stdout.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        return {"available": False, "commit": None, "dirty": None}
    # Do not retain or return status output: it may contain private filenames.
    return {"available": True, "commit": commit, "dirty": bool(status_result.stdout)}


def _audit_archive_member(name: str, *, link_target: str | None = None) -> None:
    member = PurePosixPath(name.replace("\\", "/"))
    if (
        member.is_absolute()
        or not member.parts
        or any(part in {"", ".", ".."} or ":" in part for part in member.parts)
    ):
        raise ValueError("unsafe archive member path")
    if _is_sensitive_path(member):
        raise ValueError("sensitive archive member path rejected")
    if link_target:
        target = PurePosixPath(link_target.replace("\\", "/"))
        if target.is_absolute() or any(part == ".." or ":" in part for part in target.parts):
            raise ValueError("unsafe archive link target")


def inspect_artifact(path: Path) -> dict[str, Any]:
    original = path
    if original.is_symlink():
        raise ValueError("release artifact must be a regular file")
    path = original.resolve()
    if not path.is_file():
        raise ValueError("release artifact must be a regular file")
    if _is_sensitive_path(PurePosixPath(path.name)):
        raise ValueError("sensitive artifact path rejected")

    artifact_type = "file"
    member_count: int | None = None
    if zipfile.is_zipfile(path):
        artifact_type = "zip"
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ValueError("archive contains too many members")
            for member in members:
                if member.is_dir() and member.filename.replace("\\", "/") in {".", "./"}:
                    continue
                _audit_archive_member(member.filename)
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ValueError("zip archive contains a symbolic link")
            member_count = len(members)
    elif tarfile.is_tarfile(path):
        artifact_type = "tar"
        with tarfile.open(path, "r:*") as archive:
            members = archive.getmembers()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ValueError("archive contains too many members")
            for member in members:
                if member.isdir() and member.name.replace("\\", "/") in {".", "./"}:
                    continue
                if member.isdev() or member.isfifo():
                    raise ValueError("archive contains a device or FIFO member")
                _audit_archive_member(
                    member.name,
                    link_target=member.linkname if member.issym() or member.islnk() else None,
                )
            member_count = len(members)

    result: dict[str, Any] = {
        "name": path.name,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "type": artifact_type,
        "sensitive_member_name_check": "PASS" if artifact_type != "file" else "NOT_APPLICABLE",
    }
    if member_count is not None:
        result["member_count"] = member_count
    return result


def _normalized_archive_member_path(name: str) -> str:
    """Return one safe archive-relative path, normalized across tar/zip producers."""

    _audit_archive_member(name)
    normalized = PurePosixPath(name.replace("\\", "/")).as_posix()
    return _safe_relative_path(normalized)


def _archive_inventory_members(path: Path) -> tuple[str, list[dict[str, Any]]]:
    """Read every regular archive member for a dirty-source provenance record.

    The ordinary critical-file list remains intentionally small for clean Git
    releases.  A declared-dirty release is different: it needs a complete,
    explicit package inventory so an omitted non-critical runtime module cannot
    inherit the READY label merely because the curated contract still matches.
    """

    records: list[dict[str, Any]] = []
    seen: set[str] = set()

    def append_record(
        *,
        name: str,
        kind: str,
        mode: int,
        raw: bytes | None = None,
    ) -> None:
        relative = _normalized_archive_member_path(name)
        if relative in seen:
            raise ValueError("archive contains duplicate normalized paths")
        seen.add(relative)
        if kind == "directory":
            records.append({"path": relative, "kind": kind, "mode": mode})
            return
        if raw is None:
            raise ValueError("archive file inventory is missing member content")
        content, hash_basis = _canonical_release_bytes(raw)
        records.append(
            {
                "path": relative,
                "kind": kind,
                "mode": mode,
                "bytes": len(content),
                "sha256": _sha256_bytes(content),
                "hash_basis": hash_basis,
            }
        )

    if zipfile.is_zipfile(path):
        artifact_type = "zip"
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ValueError("archive contains too many members")
            for member in members:
                raw_name = member.filename.replace("\\", "/")
                if member.is_dir() and raw_name in {".", "./"}:
                    continue
                raw_mode = int(member.external_attr >> 16)
                if stat.S_ISLNK(raw_mode):
                    raise ValueError("dirty release archive contains a symbolic link")
                mode = raw_mode & 0o7777
                if member.is_dir():
                    append_record(name=member.filename, kind="directory", mode=mode)
                else:
                    with archive.open(member, "r") as handle:
                        append_record(
                            name=member.filename,
                            kind="file",
                            mode=mode,
                            raw=handle.read(),
                        )
    elif tarfile.is_tarfile(path):
        artifact_type = "tar"
        with tarfile.open(path, "r:*") as archive:
            members = archive.getmembers()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ValueError("archive contains too many members")
            for member in members:
                raw_name = member.name.replace("\\", "/")
                if member.isdir() and raw_name in {".", "./"}:
                    continue
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise ValueError("dirty release archive contains a link or special member")
                mode = int(member.mode) & 0o7777
                if member.isdir():
                    append_record(name=member.name, kind="directory", mode=mode)
                elif member.isfile():
                    handle = archive.extractfile(member)
                    if handle is None:
                        raise ValueError("release archive member is unreadable")
                    with handle:
                        append_record(name=member.name, kind="file", mode=mode, raw=handle.read())
                else:
                    raise ValueError("dirty release archive contains an unsupported member")
    else:
        raise ValueError("dirty source inventory requires a tar or zip release archive")

    if not records:
        raise ValueError("dirty release archive contains no inventory members")
    if len(records) > MAX_DIRTY_SOURCE_INVENTORY_MEMBERS:
        raise ValueError("dirty release archive exceeds the complete-inventory member limit")
    return artifact_type, sorted(records, key=lambda entry: str(entry["path"]))


def _workspace_release_file_records(root: Path) -> tuple[str, list[dict[str, Any]]]:
    """Return the exact safe source-file set intended for a dirty release.

    In a real checkout this deliberately mirrors the normal archive producer's
    Git selection: tracked files plus unignored untracked files.  The tiny
    filesystem fallback supports isolated unit tests without weakening a real
    Git checkout: if `.git` exists but Git cannot enumerate it, release creation
    fails closed.
    """

    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=False,
        capture_output=True,
    )
    if result.returncode == 0:
        selection = "git-tracked-and-unignored"
        raw_paths = [os.fsdecode(value) for value in result.stdout.split(b"\0") if value]
    else:
        if (root / ".git").exists() or (root / ".git").is_symlink():
            raise ValueError("unable to enumerate Git release files for dirty source")
        selection = "safe-filesystem-fallback"
        raw_paths = [
            path.relative_to(root).as_posix()
            for path in sorted(root.rglob("*"))
            if ".git" not in path.relative_to(root).parts and path.is_file()
        ]

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_path in raw_paths:
        relative = _safe_relative_path(raw_path)
        if relative in seen:
            raise ValueError("workspace release file inventory contains duplicate paths")
        seen.add(relative)
        candidate = root / Path(*PurePosixPath(relative).parts)
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError(f"workspace release file is missing or not regular: {relative}")
        content, hash_basis = _canonical_release_bytes(candidate.read_bytes())
        records.append(
            {
                "path": relative,
                "kind": "file",
                "bytes": len(content),
                "sha256": _sha256_bytes(content),
                "hash_basis": hash_basis,
            }
        )
    if not records:
        raise ValueError("dirty source workspace contains no release files")
    if len(records) > MAX_DIRTY_SOURCE_INVENTORY_MEMBERS:
        raise ValueError("dirty source workspace exceeds the complete-inventory member limit")
    return selection, sorted(records, key=lambda entry: str(entry["path"]))


def _assert_dirty_archive_matches_workspace(
    workspace_files: Sequence[dict[str, Any]],
    archive_members: Sequence[dict[str, Any]],
) -> None:
    """Prove the candidate archive has every and only intended source file."""

    workspace = {str(entry["path"]): entry for entry in workspace_files}
    archive = {
        str(entry["path"]): entry
        for entry in archive_members
        if entry.get("kind") == "file"
    }
    if set(workspace) != set(archive):
        raise ValueError("dirty release archive does not contain exactly the workspace release file inventory")
    for path, source_entry in workspace.items():
        archive_entry = archive[path]
        if any(
            archive_entry.get(field) != source_entry.get(field)
            for field in ("bytes", "sha256", "hash_basis")
        ):
            raise ValueError(f"dirty release archive file does not match workspace: {path}")


def build_dirty_source_archive_inventory(root: Path, artifact_paths: Sequence[Path]) -> dict[str, Any]:
    """Build the full source/package provenance record required for dirty releases."""

    archive_paths = [path for path in artifact_paths if zipfile.is_zipfile(path) or tarfile.is_tarfile(path)]
    if len(archive_paths) != 1:
        raise ValueError("a declared-dirty release requires exactly one tar or zip deployment archive")
    archive_path = archive_paths[0]
    artifact = inspect_artifact(archive_path)
    artifact_type, archive_members = _archive_inventory_members(archive_path)
    selection, workspace_files = _workspace_release_file_records(root)
    _assert_dirty_archive_matches_workspace(workspace_files, archive_members)
    return {
        "format": DIRTY_SOURCE_ARCHIVE_INVENTORY_FORMAT,
        "source_selection": selection,
        "artifact": {
            "name": artifact["name"],
            "sha256": artifact["sha256"],
            "bytes": artifact["bytes"],
            "type": artifact_type,
        },
        "workspace_files": workspace_files,
        "archive_members": archive_members,
    }


def bind_archive_to_critical_files(
    path: Path,
    critical_files: Sequence[dict[str, Any]],
) -> str:
    """Require every critical file in a tar/zip to match its manifest content hash."""

    expected = {entry["path"]: entry for entry in critical_files}
    seen: set[str] = set()
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members: dict[str, zipfile.ZipInfo] = {}
            for member in archive.infolist():
                normalized = PurePosixPath(member.filename.replace("\\", "/")).as_posix()
                if normalized in members:
                    raise ValueError("archive contains duplicate normalized paths")
                members[normalized] = member
            for relative, entry in expected.items():
                member = members.get(relative)
                if member is None or member.is_dir():
                    raise ValueError("release artifact is missing a critical file")
                with archive.open(member, "r") as handle:
                    content = _entry_content_bytes(handle.read(), entry)
                if _sha256_bytes(content) != entry["sha256"] or len(content) != entry["bytes"]:
                    raise ValueError("release artifact critical file does not match workspace")
                seen.add(relative)
    elif tarfile.is_tarfile(path):
        with tarfile.open(path, "r:*") as archive:
            members: dict[str, tarfile.TarInfo] = {}
            for member in archive.getmembers():
                normalized = PurePosixPath(member.name.replace("\\", "/")).as_posix()
                if normalized in members:
                    raise ValueError("archive contains duplicate normalized paths")
                members[normalized] = member
            for relative, entry in expected.items():
                member = members.get(relative)
                if member is None or not member.isfile():
                    raise ValueError("release artifact is missing a critical file")
                handle = archive.extractfile(member)
                if handle is None:
                    raise ValueError("release artifact critical file is unreadable")
                with handle:
                    content = _entry_content_bytes(handle.read(), entry)
                if _sha256_bytes(content) != entry["sha256"] or len(content) != entry["bytes"]:
                    raise ValueError("release artifact critical file does not match workspace")
                seen.add(relative)
    else:
        return "NOT_APPLICABLE"
    if seen != set(expected):
        raise ValueError("release artifact critical file coverage is incomplete")
    return "PASS"


def parse_verification(value: str) -> dict[str, str]:
    if "=" not in value:
        raise ValueError("verification must use NAME=PASS|FAIL|SKIPPED|NOT_RUN")
    name, status = value.split("=", 1)
    status = status.upper()
    if not CHECK_NAME_PATTERN.fullmatch(name) or status not in CHECK_STATUSES:
        raise ValueError("invalid verification name or status")
    return {"name": name, "status": status}


def _rollback_plan() -> dict[str, list[dict[str, str]]]:
    return {
        "pre_deploy": [
            {
                "id": "CAPTURE_CURRENT_RELEASE",
                "check": "Record readlink -f /opt/finance-radar/current as PREVIOUS_RELEASE before changing it.",
            },
            {
                "id": "VERIFY_PREVIOUS_RELEASE",
                "check": "Confirm PREVIOUS_RELEASE remains a complete directory under /opt/finance-radar/releases.",
            },
            {
                "id": "BACKUP_CONFIG",
                "check": "Create timestamped copies of Nginx and systemd configuration without printing environment files.",
            },
            {
                "id": "VERIFY_DATA_BACKUP",
                "check": "Require a fresh SQLite backup whose quick_check result is ok before cutover.",
            },
            {
                "id": "VERIFY_ADMIN_INACTIVE",
                "check": "Confirm finance-radar-admin is inactive and disabled before and after public deployment.",
            },
        ],
        "rollback_triggers": [
            {"id": "SERVICE_FAILURE", "check": "Any required API, Web, Worker or Nginx health check fails."},
            {"id": "DATA_FAILURE", "check": "SQLite quick_check is not ok or event reads fail."},
            {"id": "EDGE_FAILURE", "check": "Public internal-page, admin or FastAPI deny checks do not return 404."},
            {"id": "SECRET_BOUNDARY_FAILURE", "check": "The public Web process contains an admin token or full environment."},
        ],
        "procedure": [
            {"id": "STOP_WORKER", "check": "Stop finance-radar-worker before changing the current release symlink."},
            {
                "id": "RESTORE_SYMLINK",
                "check": "Atomically repoint /opt/finance-radar/current to the recorded PREVIOUS_RELEASE; do not delete the failed release.",
            },
            {
                "id": "RESTORE_CONFIG",
                "check": "Restore timestamped systemd/Nginx copies if those files changed, then run systemctl daemon-reload.",
            },
            {"id": "VALIDATE_NGINX", "check": "Run nginx -t before reloading Nginx."},
            {
                "id": "RESTART_REQUIRED",
                "check": "Restart finance-radar-api, finance-radar-web and finance-radar-worker; leave finance-radar-admin stopped.",
            },
            {
                "id": "RECHECK",
                "check": "Repeat loopback health, database, public read-only and edge-deny acceptance checks.",
            },
        ],
    }


def _release_readiness(
    git_state: dict[str, Any],
    verifications: Sequence[dict[str, str]],
    artifacts: Sequence[dict[str, Any]],
    *,
    allow_dirty: bool,
    explicit_release_id: bool,
    dirty_source_archive_inventory: dict[str, Any] | None,
) -> str:
    statuses = {item["status"] for item in verifications}
    if "FAIL" in statuses:
        return "BLOCKED_VERIFICATION_FAILED"
    if not verifications or statuses.intersection({"SKIPPED", "NOT_RUN"}):
        return "REVIEW_REQUIRED_VERIFICATION_INCOMPLETE"
    if git_state.get("available"):
        if git_state.get("dirty"):
            if allow_dirty and any(
                item.get("critical_file_content_check") == "PASS" for item in artifacts
            ) and dirty_source_archive_inventory is not None:
                return "READY_WITH_DECLARED_DIRTY_SOURCE"
            return "REVIEW_REQUIRED_DIRTY_WORKTREE"
        return "READY"
    if explicit_release_id and any(
        item.get("critical_file_content_check") == "PASS" for item in artifacts
    ):
        return "READY_WITH_EXPLICIT_SOURCE_ID"
    return "REVIEW_REQUIRED_SOURCE_IDENTITY_INCOMPLETE"


def build_release_manifest(
    root: Path,
    *,
    release_id: str | None,
    critical_files: Iterable[str] = DEFAULT_CRITICAL_FILES,
    artifact_paths: Iterable[Path] = (),
    verifications: Iterable[dict[str, str]] = (),
    allow_dirty: bool = False,
    generated_at: datetime | None = None,
    git_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("workspace root does not exist")
    observed_at = (generated_at or utc_now()).astimezone(timezone.utc)
    git_state = dict(git_state if git_state is not None else inspect_git(root))
    git_state = {
        "available": bool(git_state.get("available")),
        "commit": git_state.get("commit"),
        "dirty": git_state.get("dirty"),
        "observed_before_output": True,
    }
    if git_state["available"]:
        if not isinstance(git_state["commit"], str) or not re.fullmatch(
            r"[0-9a-f]{40,64}", git_state["commit"]
        ):
            raise ValueError("available Git state requires a hexadecimal commit id")
        if not isinstance(git_state["dirty"], bool):
            raise ValueError("available Git state requires a boolean dirty flag")

    explicit_release_id = release_id is not None
    if release_id is None:
        commit = git_state.get("commit")
        if not git_state["available"] or not isinstance(commit, str):
            raise ValueError("explicit --release-id is required when Git identity is unavailable")
        release_id = f"{observed_at.strftime('%Y%m%dT%H%M%SZ')}-{commit[:12]}"
    release_id = _validate_release_id(release_id)

    file_records: list[dict[str, Any]] = []
    for relative in sorted({_safe_relative_path(value) for value in critical_files}):
        candidate = root / Path(*PurePosixPath(relative).parts)
        if candidate.is_symlink():
            raise ValueError(f"required release file must not be a symlink: {relative}")
        path = candidate.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("critical file escapes workspace") from exc
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(f"required release file missing: {relative}")
        assert_no_high_confidence_secret(path)
        # A release archive may honor a producer's EOL policy differently from
        # this checkout (for example, an unpinned ``text=auto`` CSS file under
        # Git for Windows). Hash UTF-8 critical text after CRLF -> LF
        # normalization, while preserving raw bytes for any non-UTF-8 file.
        # The full archive itself remains SHA-256 bound separately.
        content, hash_basis = _canonical_release_bytes(path.read_bytes())
        file_records.append(
            {
                "path": relative,
                "bytes": len(content),
                "sha256": _sha256_bytes(content),
                "hash_basis": hash_basis,
            }
        )
    if not file_records:
        raise ValueError("at least one critical release file is required")

    artifact_path_list = tuple(Path(artifact_path) for artifact_path in artifact_paths)
    artifacts: list[dict[str, Any]] = []
    for artifact_path in artifact_path_list:
        artifact = inspect_artifact(Path(artifact_path))
        artifact["critical_file_content_check"] = bind_archive_to_critical_files(
            Path(artifact_path), file_records
        )
        artifacts.append(artifact)
    names = [item["name"] for item in artifacts]
    if len(names) != len(set(names)):
        raise ValueError("artifact basenames must be unique")
    artifacts.sort(key=lambda item: item["name"])
    has_bound_archive = any(
        item.get("critical_file_content_check") == "PASS" for item in artifacts
    )
    if allow_dirty and git_state.get("dirty") and not has_bound_archive:
        raise ValueError("--allow-dirty requires a release archive bound to all critical files")

    dirty_source_archive_inventory: dict[str, Any] | None = None
    if allow_dirty and git_state.get("dirty"):
        dirty_source_archive_inventory = build_dirty_source_archive_inventory(root, artifact_path_list)

    verification_records = sorted(
        ({"name": item["name"], "status": item["status"].upper()} for item in verifications),
        key=lambda item: item["name"],
    )
    if len({item["name"] for item in verification_records}) != len(verification_records):
        raise ValueError("verification names must be unique")
    for item in verification_records:
        if not CHECK_NAME_PATTERN.fullmatch(item["name"]) or item["status"] not in CHECK_STATUSES:
            raise ValueError("invalid verification record")

    readiness = _release_readiness(
        git_state,
        verification_records,
        artifacts,
        allow_dirty=allow_dirty,
        explicit_release_id=explicit_release_id,
        dirty_source_archive_inventory=dirty_source_archive_inventory,
    )
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "generated_at_utc": observed_at.isoformat().replace("+00:00", "Z"),
        "release": {
            "id": release_id,
            "id_source": "explicit" if explicit_release_id else "derived_from_time_and_git",
            "readiness": readiness,
            "dirty_source_exception_declared": bool(allow_dirty and git_state.get("dirty")),
        },
        "source": {"git": git_state},
        "critical_files": file_records,
        "artifacts": artifacts,
        "verifications": verification_records,
        "rollback": _rollback_plan(),
        "security_boundaries": {
            "environment_values_read": False,
            "git_diff_content_read": False,
            "git_remote_read": False,
            "absolute_workspace_path_recorded": False,
            "sensitive_input_names_rejected": True,
            "critical_file_high_confidence_secret_scan": "PASS",
            "git_commit_performed": False,
            "deployment_performed": False,
            "service_or_cloud_state_changed": False,
        },
    }
    if dirty_source_archive_inventory is not None:
        manifest["dirty_source_archive_inventory"] = dirty_source_archive_inventory
    return manifest


def render_release_markdown(manifest: dict[str, Any]) -> str:
    release = manifest["release"]
    git_state = manifest["source"]["git"]
    commit = git_state.get("commit") or "not available"
    dirty = git_state.get("dirty")
    lines = [
        "# Finance Radar release manifest",
        "",
        f"- Release ID: `{release['id']}`",
        f"- Readiness: **{release['readiness']}**",
        f"- Git commit: `{commit}`",
        f"- Dirty before output: `{dirty}`",
        f"- Generated UTC: `{manifest['generated_at_utc']}`",
        "- Git commit/deploy performed by this tool: `False / False`",
        "",
        "## Verification declarations",
        "",
    ]
    if manifest["verifications"]:
        lines.extend(
            f"- `{item['name']}`: **{item['status']}**" for item in manifest["verifications"]
        )
    else:
        lines.append("- None supplied; release remains review-required.")
    lines.extend(["", "## Deployable artifacts", ""])
    if manifest["artifacts"]:
        lines.extend(
            f"- `{item['name']}` — {item['bytes']} bytes — SHA-256 `{item['sha256']}` "
            f"— critical files `{item.get('critical_file_content_check', 'NOT_APPLICABLE')}`"
            for item in manifest["artifacts"]
        )
    else:
        lines.append("- No full release artifact was supplied.")
    dirty_inventory = manifest.get("dirty_source_archive_inventory")
    if isinstance(dirty_inventory, dict):
        lines.extend(
            [
                "",
                "## Declared-dirty complete source inventory",
                "",
                f"- Source selection: `{dirty_inventory.get('source_selection')}`",
                f"- Workspace files: `{len(dirty_inventory.get('workspace_files', []))}`",
                f"- Archive members (path/type/mode/content): `{len(dirty_inventory.get('archive_members', []))}`",
                "- The archive and extracted release must both match this inventory before cutover.",
            ]
        )
    lines.extend(
        [
            "",
            "## Critical release-contract files",
            "",
            "| Path | Bytes | SHA-256 |",
            "|---|---:|---|",
        ]
    )
    lines.extend(
        f"| `{item['path']}` | {item['bytes']} | `{item['sha256']}` |"
        for item in manifest["critical_files"]
    )
    lines.extend(
        [
            "",
            "## Safety boundary",
            "",
            "This record reads no environment values, Git diff content or Git remotes. It does not commit, deploy, restart services, mutate cloud state or delete a failed release.",
            "",
            f"Use `{release['id']}.rollback-checklist.md` before cutover and keep it with this manifest.",
            "",
        ]
    )
    return "\n".join(lines)


def render_rollback_markdown(manifest: dict[str, Any]) -> str:
    release_id = manifest["release"]["id"]
    lines = [
        "# Finance Radar rollback checklist",
        "",
        f"Release ID: `{release_id}`",
        "",
        "> Fill in PREVIOUS_RELEASE before cutover. Never infer it after a failure.",
        "",
    ]
    for key, title in (
        ("pre_deploy", "Before deployment"),
        ("rollback_triggers", "Rollback triggers"),
        ("procedure", "Rollback procedure"),
    ):
        lines.extend([f"## {title}", ""])
        lines.extend(
            f"- [ ] **{item['id']}** — {item['check']}" for item in manifest["rollback"][key]
        )
        lines.append("")
    lines.extend(
        [
            "## Values to record",
            "",
            "- PREVIOUS_RELEASE: `________________`",
            "- Configuration backup directory: `________________`",
            "- Fresh database backup ID/hash: `________________`",
            "- Operator and UTC window: `________________`",
            "- Final decision: `PROCEED / ROLLBACK / HOLD`",
            "",
        ]
    )
    return "\n".join(lines)


def _write_new_files(output_dir: Path, payloads: dict[str, bytes]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    targets = [output_dir / name for name in payloads]
    existing = [path.name for path in targets if path.exists()]
    if existing:
        raise FileExistsError(f"refusing to overwrite existing release records: {', '.join(existing)}")
    written: list[Path] = []
    try:
        for name, data in payloads.items():
            target = output_dir / name
            with target.open("xb") as handle:
                handle.write(data)
            written.append(target)
    except Exception:
        for path in written:
            try:
                path.unlink()
            except OSError:
                pass
        raise


def write_release_bundle(manifest: dict[str, Any], output_dir: Path) -> dict[str, str]:
    release_id = manifest["release"]["id"]
    json_name = f"{release_id}.release-manifest.json"
    markdown_name = f"{release_id}.release-manifest.md"
    rollback_name = f"{release_id}.rollback-checklist.md"
    checksum_name = f"{release_id}.release-records.SHA256"
    payloads = {
        json_name: (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        markdown_name: render_release_markdown(manifest).encode("utf-8"),
        rollback_name: render_rollback_markdown(manifest).encode("utf-8"),
    }
    checksum_lines = [
        f"{hashlib.sha256(data).hexdigest()}  {name}" for name, data in sorted(payloads.items())
    ]
    payloads[checksum_name] = ("\n".join(checksum_lines) + "\n").encode("ascii")
    _write_new_files(output_dir, payloads)
    return {key: name for key, name in zip(("json", "markdown", "rollback", "checksums"), payloads)}


def _validate_dirty_source_file_records(value: object, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"manifest {label} is invalid")
    if len(value) > MAX_DIRTY_SOURCE_INVENTORY_MEMBERS:
        raise ValueError(f"manifest {label} exceeds the complete-inventory member limit")
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    for entry in value:
        if (
            not isinstance(entry, dict)
            or entry.get("kind") != "file"
            or not isinstance(entry.get("path"), str)
            or not isinstance(entry.get("bytes"), int)
            or isinstance(entry.get("bytes"), bool)
            or entry["bytes"] < 0
            or not isinstance(entry.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            or entry.get("hash_basis") not in {"raw_bytes", "utf8_lf_normalized"}
        ):
            raise ValueError(f"manifest {label} file entry is invalid")
        relative = _safe_relative_path(entry["path"])
        if relative in seen:
            raise ValueError(f"manifest {label} contains duplicate paths")
        seen.add(relative)
        records.append(entry)
    return records


def _validate_dirty_source_archive_inventory(
    value: object,
    *,
    artifacts: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("format") != DIRTY_SOURCE_ARCHIVE_INVENTORY_FORMAT:
        raise ValueError("manifest dirty source archive inventory is invalid")
    if value.get("source_selection") not in {"git-tracked-and-unignored", "safe-filesystem-fallback"}:
        raise ValueError("manifest dirty source selection is invalid")
    artifact = value.get("artifact")
    if (
        not isinstance(artifact, dict)
        or not isinstance(artifact.get("name"), str)
        or not isinstance(artifact.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
        or not isinstance(artifact.get("bytes"), int)
        or isinstance(artifact.get("bytes"), bool)
        or artifact["bytes"] < 0
        or artifact.get("type") not in {"tar", "zip"}
    ):
        raise ValueError("manifest dirty source archive reference is invalid")
    if not any(
        item.get("name") == artifact["name"]
        and item.get("sha256") == artifact["sha256"]
        and item.get("bytes") == artifact["bytes"]
        and item.get("type") == artifact["type"]
        for item in artifacts
    ):
        raise ValueError("manifest dirty source archive reference is not a declared artifact")

    workspace_files = _validate_dirty_source_file_records(
        value.get("workspace_files"), label="dirty source workspace file inventory"
    )
    archive_members = value.get("archive_members")
    if not isinstance(archive_members, list) or not archive_members:
        raise ValueError("manifest dirty source archive member inventory is invalid")
    if len(archive_members) > MAX_DIRTY_SOURCE_INVENTORY_MEMBERS:
        raise ValueError("manifest dirty source archive member inventory exceeds the complete-inventory member limit")
    seen: set[str] = set()
    normalized_members: list[dict[str, Any]] = []
    for entry in archive_members:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError("manifest dirty source archive member is invalid")
        relative = _safe_relative_path(entry["path"])
        if relative in seen:
            raise ValueError("manifest dirty source archive member inventory contains duplicate paths")
        seen.add(relative)
        kind = entry.get("kind")
        mode = entry.get("mode")
        if kind not in {"file", "directory"} or not isinstance(mode, int) or isinstance(mode, bool) or not 0 <= mode <= 0o7777:
            raise ValueError("manifest dirty source archive member type or mode is invalid")
        if kind == "file":
            _validate_dirty_source_file_records([entry], label="dirty source archive member inventory")
        elif any(field in entry for field in ("bytes", "sha256", "hash_basis")):
            raise ValueError("manifest dirty source directory member contains file content fields")
        normalized_members.append(entry)
    try:
        _assert_dirty_archive_matches_workspace(workspace_files, normalized_members)
    except ValueError as exc:
        raise ValueError(f"manifest dirty source inventory is inconsistent: {exc}") from exc
    return value


def _verify_dirty_source_workspace_inventory(root: Path, inventory: dict[str, Any]) -> bool:
    try:
        for entry in inventory["workspace_files"]:
            relative = _safe_relative_path(str(entry["path"]))
            candidate = root / Path(*PurePosixPath(relative).parts)
            if candidate.is_symlink() or not candidate.is_file():
                return False
            content = _entry_content_bytes(candidate.read_bytes(), entry)
            if len(content) != entry["bytes"] or _sha256_bytes(content) != entry["sha256"]:
                return False
    except (KeyError, OSError, ValueError):
        return False
    return True


def _verify_dirty_source_archive_inventory(path: Path, inventory: dict[str, Any]) -> bool:
    try:
        artifact = inspect_artifact(path)
        expected = inventory["artifact"]
        if any(
            artifact.get(field) != expected.get(field)
            for field in ("name", "sha256", "bytes", "type")
        ):
            return False
        artifact_type, members = _archive_inventory_members(path)
        return artifact_type == expected["type"] and members == inventory["archive_members"]
    except (KeyError, OSError, ValueError, tarfile.TarError, zipfile.BadZipFile):
        return False


def _load_manifest(path: Path) -> tuple[dict[str, Any], str]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("manifest must be a small regular file")
    raw = path.read_bytes()
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise ValueError("manifest root must be an object")
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("kind") != MANIFEST_KIND:
        raise ValueError("unsupported release manifest")
    release = manifest.get("release")
    if not isinstance(release, dict) or not isinstance(release.get("id"), str):
        raise ValueError("manifest release identity is missing")
    _validate_release_id(release["id"])
    if not isinstance(release.get("readiness"), str):
        raise ValueError("manifest release readiness is missing")
    dirty_exception = release.get("dirty_source_exception_declared", False)
    if not isinstance(dirty_exception, bool):
        raise ValueError("manifest dirty source exception declaration is invalid")
    critical_files = manifest.get("critical_files")
    if not isinstance(critical_files, list) or not critical_files:
        raise ValueError("manifest critical file list is invalid")
    critical_names: set[str] = set()
    for entry in critical_files:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or not isinstance(entry.get("bytes"), int)
            or entry["bytes"] < 0
            or not isinstance(entry.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            or entry.get("hash_basis", "workspace")
            not in {"workspace", "raw_bytes", "utf8_lf_normalized"}
        ):
            raise ValueError("manifest critical file entry is invalid")
        relative = _safe_relative_path(entry["path"])
        if relative in critical_names:
            raise ValueError("manifest contains duplicate critical files")
        critical_names.add(relative)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("manifest artifact list is invalid")
    artifact_names: set[str] = set()
    for entry in artifacts:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("name"), str)
            or PurePosixPath(entry["name"]).name != entry["name"]
            or ":" in entry["name"]
            or _is_sensitive_path(PurePosixPath(entry["name"]))
            or not isinstance(entry.get("bytes"), int)
            or entry["bytes"] < 0
            or not isinstance(entry.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            or entry.get("critical_file_content_check") not in {"PASS", "NOT_APPLICABLE"}
        ):
            raise ValueError("manifest artifact entry is invalid")
        if entry["name"] in artifact_names:
            raise ValueError("manifest contains duplicate artifacts")
        artifact_names.add(entry["name"])
    dirty_inventory = manifest.get("dirty_source_archive_inventory")
    if dirty_exception:
        if dirty_inventory is None:
            raise ValueError("declared-dirty manifest is missing its complete source archive inventory")
        _validate_dirty_source_archive_inventory(dirty_inventory, artifacts=artifacts)
    elif dirty_inventory is not None:
        raise ValueError("clean manifest must not contain a dirty source archive inventory")
    if release["readiness"] == "READY_WITH_DECLARED_DIRTY_SOURCE" and not dirty_exception:
        raise ValueError("declared-dirty readiness requires a dirty source exception declaration")
    return manifest, hashlib.sha256(raw).hexdigest()


def _sidecar_expected_hash(sidecar: Path, manifest_name: str) -> str | None:
    if not sidecar.is_file() or sidecar.is_symlink() or sidecar.stat().st_size > 1024 * 1024:
        return None
    for line in sidecar.read_text(encoding="ascii", errors="strict").splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].strip() == manifest_name and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            return parts[0]
    return None


def verify_release_manifest(
    manifest_path: Path,
    root: Path,
    *,
    artifact_paths: Iterable[Path] = (),
    expected_release_id: str | None = None,
    require_ready: bool = False,
    require_sidecar: bool = False,
    require_artifact: bool = False,
    skip_artifacts: bool = False,
    verified_at: datetime | None = None,
) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    manifest, manifest_hash = _load_manifest(manifest_path)
    release_id = manifest["release"]["id"]
    checks: list[dict[str, str]] = []

    if expected_release_id is not None:
        expected_release_id = _validate_release_id(expected_release_id)
        checks.append(
            {
                "name": "release_id",
                "status": "PASS" if release_id == expected_release_id else "FAIL",
            }
        )

    readiness = str(manifest["release"].get("readiness") or "")
    if require_ready:
        checks.append(
            {"name": "declared_release_readiness", "status": "PASS" if readiness.startswith("READY") else "FAIL"}
        )

    sidecar = manifest_path.with_name(f"{release_id}.release-records.SHA256")
    expected_manifest_hash = _sidecar_expected_hash(sidecar, manifest_path.name) if sidecar.exists() else None
    if expected_manifest_hash is None:
        checks.append({"name": "manifest_sidecar", "status": "FAIL" if require_sidecar else "SKIPPED"})
    else:
        checks.append(
            {"name": "manifest_sidecar", "status": "PASS" if expected_manifest_hash == manifest_hash else "FAIL"}
        )

    root = root.resolve()
    for entry in manifest.get("critical_files", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            checks.append({"name": "critical_file_schema", "status": "FAIL"})
            continue
        try:
            relative = _safe_relative_path(entry["path"])
            candidate = root / Path(*PurePosixPath(relative).parts)
            if candidate.is_symlink():
                raise ValueError("critical file is a symlink")
            target = candidate.resolve()
            target.relative_to(root)
            content = _entry_content_bytes(target.read_bytes(), entry) if target.is_file() else b""
            valid = bool(
                target.is_file()
                and len(content) == entry.get("bytes")
                and _sha256_bytes(content) == entry.get("sha256")
            )
        except (OSError, ValueError):
            valid = False
        checks.append({"name": f"file:{entry.get('path', 'invalid')}", "status": "PASS" if valid else "FAIL"})

    dirty_source_archive_inventory = manifest.get("dirty_source_archive_inventory")
    if isinstance(dirty_source_archive_inventory, dict):
        checks.append(
            {
                "name": "dirty_source_workspace_inventory",
                "status": "PASS"
                if _verify_dirty_source_workspace_inventory(root, dirty_source_archive_inventory)
                else "FAIL",
            }
        )

    declared_artifacts = manifest.get("artifacts", [])
    supplied: list[tuple[Path, dict[str, Any]]] = []
    if require_artifact and not declared_artifacts:
        checks.append({"name": "required_release_artifact", "status": "FAIL"})
    if skip_artifacts and declared_artifacts:
        checks.append({"name": "release_artifacts", "status": "SKIPPED"})
    elif declared_artifacts:
        for artifact_path in artifact_paths:
            artifact_path = Path(artifact_path)
            artifact = inspect_artifact(artifact_path)
            artifact["critical_file_content_check"] = bind_archive_to_critical_files(
                Path(artifact_path), manifest.get("critical_files", [])
            )
            supplied.append((artifact_path, artifact))
        supplied_by_hash = {item["sha256"]: item for _path, item in supplied}
        for entry in declared_artifacts:
            match = supplied_by_hash.get(entry.get("sha256")) if isinstance(entry, dict) else None
            valid = bool(
                match
                and match.get("bytes") == entry.get("bytes")
                and match.get("sensitive_member_name_check") == entry.get("sensitive_member_name_check")
                and match.get("critical_file_content_check") == entry.get("critical_file_content_check")
            )
            name = entry.get("name", "invalid") if isinstance(entry, dict) else "invalid"
            checks.append({"name": f"artifact:{name}", "status": "PASS" if valid else "FAIL"})
    else:
        checks.append({"name": "release_artifacts", "status": "PASS"})

    if isinstance(dirty_source_archive_inventory, dict):
        if skip_artifacts:
            checks.append({"name": "dirty_source_archive_inventory", "status": "SKIPPED"})
        else:
            expected = dirty_source_archive_inventory["artifact"]
            matching_paths = [
                path
                for path, artifact in supplied
                if artifact.get("sha256") == expected.get("sha256")
            ]
            valid = len(matching_paths) == 1 and _verify_dirty_source_archive_inventory(
                matching_paths[0], dirty_source_archive_inventory
            )
            checks.append(
                {
                    "name": "dirty_source_archive_inventory",
                    "status": "PASS" if valid else "FAIL",
                }
            )

    statuses = {item["status"] for item in checks}
    status = "FAIL" if "FAIL" in statuses else ("PASS_WITH_SKIPPED" if "SKIPPED" in statuses else "PASS")
    observed_at = (verified_at or utc_now()).astimezone(timezone.utc)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": ACCEPTANCE_KIND,
        "verified_at_utc": observed_at.isoformat().replace("+00:00", "Z"),
        "release_id": release_id,
        "declared_readiness": readiness,
        "manifest_sha256": manifest_hash,
        "status": status,
        "checks": checks,
        "security_boundaries": {
            "environment_values_read": False,
            "absolute_workspace_path_recorded": False,
            "deployment_performed": False,
            "git_mutation_performed": False,
        },
    }


def render_acceptance_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Finance Radar release acceptance",
        "",
        f"- Release ID: `{report['release_id']}`",
        f"- Integrity status: **{report['status']}**",
        f"- Declared readiness: `{report['declared_readiness']}`",
        f"- Manifest SHA-256: `{report['manifest_sha256']}`",
        f"- Verified UTC: `{report['verified_at_utc']}`",
        "",
        "## Checks",
        "",
    ]
    lines.extend(f"- `{item['name']}`: **{item['status']}**" for item in report["checks"])
    lines.extend(["", "No commit, deployment, service restart or cloud mutation was performed.", ""])
    return "\n".join(lines)


def write_acceptance_bundle(report: dict[str, Any], output_dir: Path) -> dict[str, str]:
    release_id = report["release_id"]
    json_name = f"{release_id}.acceptance.json"
    markdown_name = f"{release_id}.acceptance.md"
    checksum_name = f"{release_id}.acceptance.SHA256"
    payloads = {
        json_name: (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        markdown_name: render_acceptance_markdown(report).encode("utf-8"),
    }
    checksum_lines = [
        f"{hashlib.sha256(data).hexdigest()}  {name}" for name, data in sorted(payloads.items())
    ]
    payloads[checksum_name] = ("\n".join(checksum_lines) + "\n").encode("ascii")
    _write_new_files(output_dir, payloads)
    return {key: name for key, name in zip(("json", "markdown", "checksums"), payloads)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create a non-deploying release record")
    create.add_argument("--root", type=Path, default=Path.cwd())
    create.add_argument("--release-id")
    create.add_argument("--artifact", action="append", type=Path, default=[])
    create.add_argument("--critical-file", action="append", default=[])
    create.add_argument("--verification", action="append", default=[])
    create.add_argument("--allow-dirty", action="store_true")
    create.add_argument("--strict", action="store_true")
    create.add_argument("--output-dir", type=Path, required=True)

    verify = subparsers.add_parser("verify", help="verify a release record against files/artifacts")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--artifact", action="append", type=Path, default=[])
    verify.add_argument("--expected-release-id")
    verify.add_argument("--require-ready", action="store_true")
    verify.add_argument("--require-sidecar", action="store_true")
    verify.add_argument("--require-artifact", action="store_true")
    verify.add_argument("--skip-artifacts", action="store_true")
    verify.add_argument("--report-dir", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            verifications = [parse_verification(value) for value in args.verification]
            critical_files = tuple(DEFAULT_CRITICAL_FILES) + tuple(args.critical_file)
            manifest = build_release_manifest(
                args.root,
                release_id=args.release_id,
                critical_files=critical_files,
                artifact_paths=args.artifact,
                verifications=verifications,
                allow_dirty=args.allow_dirty,
            )
            outputs = write_release_bundle(manifest, args.output_dir)
            summary = {
                "release_id": manifest["release"]["id"],
                "readiness": manifest["release"]["readiness"],
                "outputs": outputs,
            }
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
            if args.strict and not manifest["release"]["readiness"].startswith("READY"):
                return 3
            return 0

        report = verify_release_manifest(
            args.manifest,
            args.root,
            artifact_paths=args.artifact,
            expected_release_id=args.expected_release_id,
            require_ready=args.require_ready,
            require_sidecar=args.require_sidecar,
            require_artifact=args.require_artifact,
            skip_artifacts=args.skip_artifacts,
        )
        outputs = write_acceptance_bundle(report, args.report_dir) if args.report_dir else {}
        print(
            json.dumps(
                {"release_id": report["release_id"], "status": report["status"], "outputs": outputs},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0 if report["status"].startswith("PASS") else 4
    except (OSError, ValueError, FileNotFoundError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
