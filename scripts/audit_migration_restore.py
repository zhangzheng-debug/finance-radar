#!/usr/bin/env python3
"""Perform a full isolated restore audit of an encrypted VPS migration archive.

The audit decrypts into a temporary directory, scans every tar member without
extracting arbitrary paths, verifies the archive manifest, restores only the two
SQLite snapshots, and validates the accepted release and safety boundaries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

try:
    from scripts.backup_crypto import decrypt_file
except ModuleNotFoundError:  # Direct execution from scripts/.
    from backup_crypto import decrypt_file


ROOT = Path(__file__).resolve().parents[1]
STAMP_RE = re.compile(r"finance-radar-migration-(\d{8}T\d{6}Z)\.tgz\.aesgcm$")
CHUNK_BYTES = 1024 * 1024
MAX_MEMBERS = 100_000
MAX_UNPACKED_BYTES = 4 * 1024 * 1024 * 1024
CAPTURE_LIMIT = 4 * 1024 * 1024
LOCAL_EVIDENCE_MODEL_PATH = "evidence-llm/models/qwen2.5-0.5b-instruct-q4_k_m.gguf"
LOCAL_EVIDENCE_MODEL_SHA256 = (
    "74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db"
)
BLIND_REPORT_FILENAMES = (
    "risk_router_external_blind_v3_report.json",
    "risk_router_external_blind_v2_report.json",
    "risk_router_external_blind_v1_report.json",
)
LEDGER_TABLES = (
    "sources",
    "raw_observations",
    "canonical_events",
    "event_versions",
    "event_evidence",
    "event_market_metrics",
    "pipeline_jobs",
    "alert_outbox",
)
OPERATIONS_TABLES = (
    "replay_runs",
    "model_runs",
    "worker_cycles",
    "backup_runs",
    "agent_decisions",
    "evidence_objects",
    "human_overrides",
)
OPERATIONS_V3_TABLES = (
    "adjudication_samples",
    "adjudication_reviews",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member_name(name: str, expected_root: str) -> str:
    if not name or "\\" in name:
        raise ValueError(f"unsafe archive member name: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"unsafe archive member path: {name!r}")
    if path.parts[0] != expected_root:
        raise ValueError(f"archive member outside expected root: {name!r}")
    return PurePosixPath(*path.parts[1:]).as_posix()


def _validate_link(member: tarfile.TarInfo, expected_root: str) -> None:
    link = PurePosixPath(member.linkname)
    if "\\" in member.linkname:
        raise ValueError(f"unsafe archive link target: {member.linkname!r}")
    if link.is_absolute():
        allowed_prefix = PurePosixPath("/opt/finance-radar/shared")
        if link == allowed_prefix or allowed_prefix in link.parents:
            return
        raise ValueError(f"unsafe archive link target: {member.linkname!r}")
    if member.issym():
        combined = PurePosixPath(member.name).parent / link
    else:
        combined = link
    parts: list[str] = []
    for part in combined.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise ValueError(f"archive link escapes root: {member.name!r}")
            parts.pop()
        else:
            parts.append(part)
    if not parts or parts[0] != expected_root:
        raise ValueError(f"archive link escapes expected root: {member.name!r}")


def _stream_regular_file(
    source: BinaryIO,
    *,
    output: BinaryIO | None = None,
    capture: bool = False,
) -> tuple[str, bytes | None]:
    digest = hashlib.sha256()
    captured = bytearray() if capture else None
    while chunk := source.read(CHUNK_BYTES):
        digest.update(chunk)
        if output is not None:
            output.write(chunk)
        if captured is not None:
            if len(captured) + len(chunk) > CAPTURE_LIMIT:
                raise ValueError("captured archive member exceeds safety limit")
            captured.extend(chunk)
    return digest.hexdigest(), bytes(captured) if captured is not None else None


def _parse_manifest(raw: bytes) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        match = re.fullmatch(r"([0-9a-f]{64})  \./(.+)", line)
        if not match:
            raise ValueError(f"invalid manifest line {line_number}")
        digest, name = match.groups()
        safe_name = _safe_member_name(f"root/{name}", "root")
        if safe_name in entries:
            raise ValueError(f"duplicate manifest entry: {safe_name}")
        entries[safe_name] = digest
    if not entries:
        raise ValueError("archive manifest is empty")
    return entries


def _database_report(path: Path, *, ledger: bool) -> dict[str, Any]:
    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        integrity_check = connection.execute("PRAGMA integrity_check").fetchone()[0]
        schema_table = "event_ledger_schema" if ledger else "operations_schema"
        schema_version = int(
            connection.execute(f"SELECT MAX(version) FROM {schema_table}").fetchone()[0] or 0
        )
        tables = LEDGER_TABLES if ledger else OPERATIONS_TABLES
        if not ledger and schema_version >= 3:
            tables += OPERATIONS_V3_TABLES
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
        audit = {}
        if ledger:
            audit = {
                "trading_boundary_violations": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM canonical_events WHERE no_trading != 1"
                    ).fetchone()[0]
                ),
                "auto_verification_violations": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM event_evidence WHERE auto_verification_allowed != 0"
                    ).fetchone()[0]
                ),
                "market_feature_leakage_violations": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM event_market_metrics WHERE allowed_as_model_feature != 0"
                    ).fetchone()[0]
                ),
            }
    if quick_check != "ok" or integrity_check != "ok":
        raise ValueError(f"SQLite integrity failure: quick={quick_check} integrity={integrity_check}")
    if ledger and (schema_version != 12 or any(audit.values())):
        raise ValueError(f"ledger safety/schema failure: schema={schema_version} audit={audit}")
    if not ledger and schema_version not in {2, 3, 4}:
        raise ValueError(f"operations schema failure: schema={schema_version}; expected 2, 3 or 4")
    return {
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "quick_check": quick_check,
        "integrity_check": integrity_check,
        "schema_version": schema_version,
        "counts": counts,
        "audit": audit,
        "opened_read_only_immutable": True,
    }


def audit_archive(
    encrypted_archive: Path,
    passphrase_file: Path,
    *,
    expected_release: str,
    expected_sha256: str,
) -> dict[str, Any]:
    encrypted_archive = encrypted_archive.resolve()
    passphrase_file = passphrase_file.resolve()
    if not encrypted_archive.is_file():
        raise FileNotFoundError("encrypted archive is missing")
    match = STAMP_RE.search(encrypted_archive.name)
    if not match:
        raise ValueError("archive filename does not contain a valid migration stamp")
    stamp = match.group(1)
    expected_root = f"finance-radar-migration-{stamp}"
    if not re.fullmatch(r"\d{8}T\d{6}Z", expected_release):
        raise ValueError("expected release id is invalid")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise ValueError("expected SHA-256 is invalid")

    workdir = Path(tempfile.mkdtemp(prefix="finance-radar-isolated-restore-"))
    plain_archive = workdir / "migration.tgz"
    restored_ledger = workdir / "finance_radar.sqlite3"
    restored_operations = workdir / "finance_radar_operations.sqlite3"
    workdir_cleaned = False
    try:
        passphrase = os.getenv("FINANCE_RADAR_BACKUP_PASSPHRASE")
        if not passphrase:
            if not passphrase_file.is_file():
                raise FileNotFoundError("passphrase file is missing")
            passphrase = passphrase_file.read_text(encoding="utf-8").strip()
        crypto = decrypt_file(encrypted_archive, plain_archive, passphrase)
        archive_sha256 = sha256_file(plain_archive)
        if archive_sha256 != expected_sha256.lower():
            raise ValueError("decrypted archive SHA-256 does not match expected value")

        hashes: dict[str, str] = {}
        captures: dict[str, bytes] = {}
        seen: set[str] = set()
        special_members: list[str] = []
        member_count = 0
        regular_file_count = 0
        unpacked_bytes = 0
        nginx_config_count = 0
        release_artifacts = f"releases/{expected_release}/artifacts"
        capture_names = {
            "CURRENT_RELEASE.txt",
            "MANIFEST.sha256",
            f"releases/{expected_release}/scripts/official_event_collector.py",
            f"{release_artifacts}/risk_router_model_card.json",
            f"{release_artifacts}/risk_router.sha256",
            *(f"{release_artifacts}/{name}" for name in BLIND_REPORT_FILENAMES),
        }
        with tarfile.open(plain_archive, mode="r:gz") as archive:
            for member in archive:
                member_count += 1
                if member_count > MAX_MEMBERS:
                    raise ValueError("archive member-count safety limit exceeded")
                relative_name = _safe_member_name(member.name.rstrip("/"), expected_root)
                if relative_name in seen:
                    raise ValueError(f"duplicate archive member: {relative_name}")
                seen.add(relative_name)
                if relative_name.startswith("config/etc/nginx/") and member.isfile():
                    nginx_config_count += 1
                if member.isdir():
                    continue
                if member.issym() or member.islnk():
                    _validate_link(member, expected_root)
                    special_members.append(relative_name)
                    continue
                if not member.isfile():
                    raise ValueError(f"unsupported special archive member: {member.name!r}")
                regular_file_count += 1
                unpacked_bytes += int(member.size)
                if unpacked_bytes > MAX_UNPACKED_BYTES:
                    raise ValueError("archive unpacked-size safety limit exceeded")
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(f"could not read archive member: {member.name!r}")
                output_path = None
                if relative_name == "shared/data/finance_radar.sqlite3":
                    output_path = restored_ledger
                elif relative_name == "shared/data/finance_radar_operations.sqlite3":
                    output_path = restored_operations
                capture = relative_name in capture_names
                if output_path is not None:
                    with output_path.open("wb") as output:
                        digest, captured = _stream_regular_file(source, output=output, capture=capture)
                else:
                    digest, captured = _stream_regular_file(source, capture=capture)
                hashes[relative_name] = digest
                if captured is not None:
                    captures[relative_name] = captured

        manifest_raw = captures.get("MANIFEST.sha256")
        if manifest_raw is None:
            raise ValueError("MANIFEST.sha256 is missing")
        manifest = _parse_manifest(manifest_raw)
        missing_manifest_files = sorted(set(manifest) - set(hashes))
        mismatched_manifest_files = sorted(
            name for name, digest in manifest.items() if hashes.get(name) != digest
        )
        unexpected_regular_files = sorted(set(hashes) - set(manifest) - {"MANIFEST.sha256"})
        if missing_manifest_files or mismatched_manifest_files or unexpected_regular_files:
            raise ValueError(
                "archive manifest verification failed: "
                f"missing={len(missing_manifest_files)} "
                f"mismatch={len(mismatched_manifest_files)} "
                f"unexpected={len(unexpected_regular_files)}"
            )

        current_release_raw = captures.get("CURRENT_RELEASE.txt", b"").decode("utf-8").strip()
        expected_current = f"/opt/finance-radar/releases/{expected_release}"
        if current_release_raw != expected_current:
            raise ValueError(
                f"CURRENT_RELEASE mismatch: expected {expected_current!r}, got {current_release_raw!r}"
            )
        available_blind_paths = [
            f"{release_artifacts}/{name}"
            for name in BLIND_REPORT_FILENAMES
            if f"{release_artifacts}/{name}" in seen
        ]
        if not available_blind_paths:
            raise ValueError("no supported external-blind report is present in the accepted release")
        blind_path = available_blind_paths[0]
        required_files = [
            f"releases/{expected_release}/app/api/main.py",
            f"releases/{expected_release}/app/web/Home.py",
            f"releases/{expected_release}/scripts/official_event_collector.py",
            blind_path,
            f"{release_artifacts}/risk_router.joblib",
            f"{release_artifacts}/risk_router.sha256",
            f"{release_artifacts}/risk_router_model_card.json",
            f"releases/{expected_release}/requirements.txt",
            "shared/data/finance_radar.sqlite3",
            "shared/data/finance_radar_operations.sqlite3",
            "config/etc/finance-radar.env",
            "config/CERTIFICATE_STATUS.txt",
            "config/SERVICE_STATUS.txt",
            "config/PYTHON_VERSION.txt",
            "config/PIP_FREEZE.txt",
        ]
        missing_required = sorted(set(required_files) - seen)
        if missing_required or nginx_config_count < 1:
            raise ValueError(
                f"required migration material missing: files={missing_required} nginx={nginx_config_count}"
            )
        model_unit = (
            f"releases/{expected_release}/deployment/systemd/"
            "finance-radar-evidence-llm.service"
        )
        model_required = model_unit in hashes
        model_hash = hashes.get(LOCAL_EVIDENCE_MODEL_PATH)
        if model_required and model_hash != LOCAL_EVIDENCE_MODEL_SHA256:
            raise ValueError("local evidence model is missing or has an unexpected SHA-256")
        forbidden_names = [
            name
            for name in seen
            if "ethusdc-pivot-bot" in name
            or "/letsencrypt/" in f"/{name}/"
            or name.endswith("privkey.pem")
        ]
        if forbidden_names:
            raise ValueError(f"forbidden material found in migration archive: {forbidden_names[:3]}")

        collector_path = f"releases/{expected_release}/scripts/official_event_collector.py"
        collector_text = captures[collector_path].decode("utf-8")
        collector_sources = {
            source_id: source_id in collector_text
            for source_id in ("ecb_press", "ecb_statistical_press", "eia_press", "nvidia_official_news")
        }
        if not all(collector_sources.values()):
            raise ValueError(f"new official source registry incomplete: {collector_sources}")

        blind = json.loads(captures[blind_path])
        blind_promotion = blind.get("promotion_decision")
        blind_gate_pass = blind.get("gate_pass")
        blind_generation = next(
            version for version in ("v3", "v2", "v1") if f"_{version}_report.json" in blind_path
        )
        if blind_generation == "v3":
            if blind_promotion != "QUALIFIED_SHADOW" or blind_gate_pass is not True:
                raise ValueError("external-blind-v3 qualification is not preserved")
        elif blind_promotion != "REMAIN_SHADOW" or blind_gate_pass is not False:
            raise ValueError("legacy external-blind failure guard is not preserved")
        if blind.get("no_trading") is not True:
            raise ValueError("external-blind report does not preserve the no-trading boundary")
        if blind_generation == "v3" and blind.get("shadow") is not True:
            raise ValueError("qualified model is not explicitly constrained to SHADOW")

        model_path = f"{release_artifacts}/risk_router.joblib"
        model_card_path = f"{release_artifacts}/risk_router_model_card.json"
        model_sha_path = f"{release_artifacts}/risk_router.sha256"
        model_card = json.loads(captures[model_card_path])
        declared_sha = captures[model_sha_path].decode("utf-8").strip().split()[0].lower()
        model_sha = hashes[model_path]
        blind_sha = str(blind.get("model_artifact_sha256") or "").lower()
        card_sha = str(model_card.get("artifact_sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", declared_sha):
            raise ValueError("risk-router SHA-256 declaration is invalid")
        if len({model_sha, declared_sha, blind_sha, card_sha}) != 1:
            raise ValueError("risk-router artifact, declaration, blind report and model card hashes differ")
        model_version = str(model_card.get("model_version") or "")
        if model_version != str(blind.get("model_version") or ""):
            raise ValueError("risk-router model version differs between card and blind report")
        if model_card.get("no_trading") is not True or model_card.get("shadow") is not True:
            raise ValueError("risk-router model card does not preserve SHADOW/no-trading")

        if not restored_ledger.is_file() or not restored_operations.is_file():
            raise ValueError("isolated SQLite restore files were not produced")
        ledger = _database_report(restored_ledger, ledger=True)
        operations = _database_report(restored_operations, ledger=False)

        result = {
            "schema_version": 1,
            "verified_at": utc_now(),
            "status": "PASS",
            "encrypted_archive": str(encrypted_archive),
            "encrypted_bytes": encrypted_archive.stat().st_size,
            "decrypted_archive_sha256": archive_sha256,
            "expected_release": expected_release,
            "current_release": current_release_raw,
            "crypto": {
                "authenticated_decryption": True,
                "mode": crypto["mode"],
                "restored_bytes": crypto["restored_bytes"],
            },
            "archive": {
                "root": expected_root,
                "members": member_count,
                "regular_files": regular_file_count,
                "unpacked_bytes": unpacked_bytes,
                "safe_path_scan": True,
                "special_members": special_members,
                "manifest_entries": len(manifest),
                "manifest_all_match": True,
                "required_files_present": True,
                "nginx_config_files": nginx_config_count,
            },
            "release": {
                "required_files_present": True,
                "official_source_registry": collector_sources,
                "external_blind_report": PurePosixPath(blind_path).name,
                "external_blind_generation": blind_generation,
                "external_blind_gate_pass": blind_gate_pass,
                "external_blind_promotion": blind_promotion,
                "risk_router_model_version": model_version,
                "risk_router_artifact_sha256": model_sha,
                "risk_router_hash_chain_match": True,
                "shadow": True,
                "no_trading": True,
            },
            "local_evidence_model": {
                "required_by_release": model_required,
                "included": model_hash is not None,
                "sha256": model_hash,
                "pinned_sha256_match": model_hash == LOCAL_EVIDENCE_MODEL_SHA256,
            },
            "ledger_restore": ledger,
            "operations_restore": operations,
            "boundaries": {
                "trading_project_included": False,
                "tls_private_keys_included": False,
                "environment_present_but_not_logged": True,
                "arbitrary_archive_paths_extracted": False,
            },
            "isolated_restore": True,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=False)
        workdir_cleaned = not workdir.exists()
    result["temporary_workspace_cleaned"] = workdir_cleaned
    return result


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def render_markdown(payload: dict[str, Any]) -> str:
    if payload.get("status") != "PASS":
        return (
            "# Encrypted migration archive — full isolated restore audit\n\n"
            f"- Verified at: `{payload.get('verified_at', 'unknown')}`\n"
            "- Result: **FAIL**\n"
            f"- Accepted release: `{payload.get('expected_release', 'unknown')}`\n"
            f"- Error: `{payload.get('error_type', 'unknown')}: {payload.get('error', 'unknown')}`\n"
        )
    archive = payload["archive"]
    ledger = payload["ledger_restore"]
    operations = payload["operations_restore"]
    boundaries = payload["boundaries"]
    release = payload["release"]
    snapshot = Path(payload["encrypted_archive"]).parent.name
    lines = [
        "# Encrypted migration archive — full isolated restore audit",
        "",
        f"- Verified at: `{payload['verified_at']}`",
        "- Result: **PASS**",
        f"- Snapshot: `{snapshot}`",
        f"- Accepted release: `{payload['expected_release']}`",
        f"- Encrypted bytes: `{payload['encrypted_bytes']}`",
        f"- Authenticated decrypted archive SHA-256: `{payload['decrypted_archive_sha256']}`",
        "",
        "## Archive proof",
        "",
        f"- {archive['members']:,} archive members and {archive['regular_files']:,} regular files scanned.",
        f"- {archive['unpacked_bytes']:,} uncompressed bytes processed without arbitrary path extraction.",
        f"- All {archive['manifest_entries']:,} `MANIFEST.sha256` entries matched.",
        f"- Safe path scan: `{archive['safe_path_scan']}`; required files present: `{archive['required_files_present']}`.",
        "",
        "## Restored databases",
        "",
        f"- Ledger: Schema {ledger['schema_version']}; quick/integrity `{ledger['quick_check']}` / `{ledger['integrity_check']}`; {ledger['counts']['canonical_events']:,} events and {ledger['counts']['event_evidence']:,} evidence rows.",
        f"- Operations: Schema {operations['schema_version']}; quick/integrity `{operations['quick_check']}` / `{operations['integrity_check']}`; {operations['counts']['worker_cycles']:,} worker cycles and {operations['counts']['backup_runs']:,} backup runs.",
        "- Both databases were opened read-only/immutable during the audit.",
        "",
        "## Shadow model recovery proof",
        "",
        f"- Model: `{release['risk_router_model_version']}`.",
        f"- Blind report: `{release['external_blind_report']}`; gate `{release['external_blind_gate_pass']}`; decision `{release['external_blind_promotion']}`.",
        f"- Artifact/card/report SHA-256 chain matched: `{release['risk_router_hash_chain_match']}`.",
        f"- SHADOW / no-trading: `{release['shadow']}` / `{release['no_trading']}`.",
        "",
        "## Safety boundaries",
        "",
        f"- Trading project included: `{boundaries['trading_project_included']}`.",
        f"- TLS private keys included: `{boundaries['tls_private_keys_included']}`.",
        f"- Arbitrary archive paths extracted: `{boundaries['arbitrary_archive_paths_extracted']}`.",
        f"- Temporary plaintext workspace cleaned: `{payload['temporary_workspace_cleaned']}`.",
        "",
    ]
    return "\n".join(lines)


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(payload), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("encrypted_archive", type=Path)
    parser.add_argument("--passphrase-file", type=Path, default=ROOT / "server_migration_backup" / ".backup-passphrase")
    parser.add_argument("--expected-release", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--report", type=Path, default=ROOT / "reports" / "migration_full_restore_latest.json")
    args = parser.parse_args()
    try:
        result = audit_archive(
            args.encrypted_archive,
            args.passphrase_file,
            expected_release=args.expected_release,
            expected_sha256=args.expected_sha256,
        )
    except Exception as exc:
        failure = {
            "schema_version": 1,
            "verified_at": utc_now(),
            "status": "FAIL",
            "encrypted_archive": str(args.encrypted_archive.resolve()),
            "expected_release": args.expected_release,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        write_report(args.report, failure)
        write_markdown(args.report.with_suffix(".md"), failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        return 1
    write_report(args.report, result)
    write_markdown(args.report.with_suffix(".md"), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
