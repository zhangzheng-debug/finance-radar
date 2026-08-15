#!/usr/bin/env python3
"""Build a curated, secret-scanned, hash-manifested offline defense pack."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
ZIP_TIME = (2026, 7, 18, 0, 0, 0)
FORBIDDEN_NAME_PARTS = (
    ".env",
    "passphrase",
    "private_key",
    "privkey",
    "id_ed25519",
    ".aesgcm",
    ".tgz",
)
SECRET_PATTERNS = (
    re.compile(rb"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(rb"FINANCE_RADAR_(?:ADMIN|REVIEWER|OPERATOR)_TOKEN\s*=\s*[^\s]+", re.I),
    re.compile(rb"TELEGRAM_(?:BOT_TOKEN|API_HASH)\s*=\s*[^\s]+", re.I),
)
EVIDENCE_FILES = (
    "README.md",
    "ACCEPTANCE_STATUS.md",
    "financial_event_radar_project_proposal_v5_2_human.docx",
    "financial_event_radar_project_plan_v5_1_ai.md",
    "artifacts/defense_deck/finance-radar-defense-deck-v1.pptx",
    "artifacts/defense_deck/README.md",
    ".agent/deployment_runbook.md",
    ".agent/student_execution_pack.md",
    ".agent/teacher_approval_request.md",
    ".agent/v5_completion_audit.md",
    "config/course_evidence_manifest.json",
    "config/risk_label_contract_v3.json",
    "reports/product_acceptance_live_latest.json",
    "reports/product_acceptance_latest.json",
    "reports/market_capabilities_live_latest.json",
    "reports/market_capabilities_live_latest.md",
    "reports/evidence_source_snapshots_latest.json",
    "reports/evidence_source_snapshots_latest.md",
    "reports/accessibility_public_latest.json",
    "reports/accessibility_public_latest.md",
    "reports/ui_competitor_research_20260719.md",
    "reports/public_load_test_120x15_latest.json",
    "reports/defense_drills_latest.json",
    "reports/migration_full_restore_latest.json",
    "reports/migration_full_restore_latest.md",
    "reports/new_vps_encrypted_restore_audit_latest.json",
    "reports/new_vps_encrypted_restore_audit_latest.md",
    "reports/migration_service_restore_drill_latest.json",
    "reports/migration_service_restore_drill_latest.md",
    "reports/local_evidence_model_comparison_initial_fail.json",
    "reports/local_evidence_model_comparison_initial_fail.md",
    "reports/local_evidence_model_comparison_latest.json",
    "reports/local_evidence_model_comparison_latest.md",
    "reports/local_evidence_model_live_acceptance_latest.json",
    "reports/local_evidence_model_live_acceptance_latest.md",
    "reports/risk_router_input_contract_audit_v1.json",
    "reports/risk_router_input_contract_audit_v1.md",
    "reports/risk_label_contract_v3_readiness.json",
    "reports/risk_label_contract_v3_readiness.md",
    "reports/adjudication_v3_latest.json",
    "reports/adjudication_v3_latest.md",
    "reports/adjudication_v3_public_acceptance.json",
    "reports/adjudication_v3_public_acceptance.md",
    "reports/offline_demo_acceptance_latest.json",
    "reports/offline_demo_acceptance_latest.md",
    "reports/migration_backup_hardlink_failure_20260718T173750Z.json",
    "reports/migration_backup_hardlink_failure_20260718T173750Z.md",
    "reports/runtime_evidence/runtime_gate_latest.json",
    "reports/runtime_evidence/runtime_gate_latest.md",
    "reports/runtime_evidence/runtime_gate_history.jsonl",
    "reports/course_readiness_latest.json",
    "reports/course_readiness_latest.md",
    "reports/ui_qa_20260719/README.md",
    "reports/ui_qa_20260719/public_interaction_acceptance.json",
    "reports/ui_qa_20260719/public_interaction_acceptance.md",
    "reports/ui_qa_20260719/home_1920x1080.png",
    "reports/ui_qa_20260719/home_1920x1080.json",
    "reports/ui_qa_20260719/event_keyboard_after_jk_1920x1080.png",
    "reports/ui_qa_20260719/replay_completed_1920x1080.png",
    "reports/ui_qa_20260719/operations_model_1920x1080.png",
    "reports/ui_qa_20260719/operations_model_1920x1080.json",
    "reports/ui_qa_20260719/home_1366x768.png",
    "reports/ui_qa_20260719/home_1366x768.json",
    "reports/ui_qa_20260719/home_390x844.png",
    "reports/ui_qa_20260719/home_390x844.json",
    "reports/ui_qa_20260719/event_intelligence_verified_1366x768.png",
    "reports/ui_qa_20260719/event_intelligence_verified_1366x768.json",
    "reports/ui_qa_20260719/event_intelligence_verified_390x844_scrolled.png",
    "reports/ui_qa_20260719/event_intelligence_verified_390x844_scrolled.json",
    "reports/ui_qa_20260719/replay_lab_1366x768.png",
    "reports/ui_qa_20260719/replay_lab_1366x768.json",
    "reports/ui_qa_20260719/operations_model_1366x768.png",
    "reports/ui_qa_20260719/operations_model_1366x768.json",
    "reports/ui_qa_20260719/operations_model_degraded_1366x768.png",
    "reports/ui_qa_20260719/operations_model_degraded_1366x768.json",
    "reports/ui_qa_20260719/adjudication_readonly_1920x1080.png",
    "reports/ui_qa_20260719/adjudication_readonly_1920x1080.json",
    "reports/docx_qa_v5_1_external_blind/README.md",
    "reports/docx_qa_v5_2_long_running_20260719T043425Z/README.md",
    "reports/docx_qa_v5_2_long_running_20260719T043425Z/financial_event_radar_project_proposal_v5_2_human.pdf",
    "reports/docx_qa_v5_2_long_running_20260719T043425Z/a11y.json",
    "docs/UI_AESTHETIC_DIRECTION.md",
    "docs/LOCAL_EVIDENCE_MODEL.md",
    "docs/SERVER_MIGRATION_HANDOFF.md",
    "docs/STUDENT_COURSE_HANDOFF.md",
    "docs/ADJUDICATION_V3_WORKFLOW.md",
    "scripts/capture_public_ui_qa.js",
    "scripts/verify_public_ui_interactions.js",
    "scripts/audit_public_accessibility.js",
    "scripts/observe_live_event_markets.py",
    "scripts/capture_market_capabilities.py",
    "scripts/collect_product_acceptance.py",
    "scripts/audit_course_readiness.py",
    "scripts/snapshot_evidence_sources.py",
    "scripts/run_live_cycle.py",
    "scripts/pull_server_migration_backup.ps1",
    "deployment/systemd/nginx-radar-direct.conf",
    "deployment/systemd/nginx-radar-locations.conf",
    "app/web/Home.py",
    "app/web/common.py",
    "app/web/components.py",
    "app/web/pages/1_Event_Intelligence.py",
    "app/web/pages/3_Operations_and_Model.py",
    "app/storage/ledger.py",
    "app/storage/operations.py",
    "app/workers/continuous.py",
    "app/api/main.py",
    "scripts/audit_risk_label_contract_v3.py",
    "scripts/verify_public_adjudication.py",
    "app/services/adjudication.py",
    "tests/test_adjudication_workflow.py",
    "app/models/risk_label_contract.py",
    "tests/test_risk_label_contract.py",
    "tests/test_accessibility_audit_script.py",
    "tests/test_observe_live_event_markets.py",
    "tests/test_capture_market_capabilities.py",
    "tests/test_operations_model_app.py",
    "tests/test_web_components.py",
    "tests/test_audit_course_readiness.py",
    "tests/test_snapshot_evidence_sources.py",
    "tests/test_product_layer.py",
    "tests/test_nginx_streamlit_route_contract.py",
    "tests/test_systemd_install_contract.py",
    "tests/test_pull_server_migration_backup_contract.py",
    "server_migration_backup/ACCEPTED_BACKUP.json",
    "data/research/finance-radar-v2-input-20260718T1824Z.json",
    "artifacts/risk_router_model_card.json",
    "artifacts/risk_router_model_card.md",
    "artifacts/risk_router_data_card.json",
    "artifacts/risk_router_robustness.json",
    "artifacts/risk_router_external_blind_v1_freeze.json",
    "artifacts/risk_router_external_blind_v1_report.json",
    "artifacts/risk_router_external_blind_v1_report.md",
    "artifacts/risk_router_v1_shortcut_audit.json",
    "artifacts/risk_router_v1_shortcut_audit.md",
    "artifacts/risk_router_v2_candidate_model_card.json",
    "artifacts/risk_router_v2_candidate_report.json",
    "artifacts/risk_router_v2_candidate_report.md",
    "artifacts/risk_router_v2_on_legacy_blind_v1_diagnostic.json",
    "artifacts/risk_router_v2_candidate_manifest.jsonl",
    "artifacts/offline_demo/offline_demo_build_latest.json",
    "artifacts/offline_demo/finance-radar-offline-demo-latest.zip",
    "replay/cases/cases.json",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_relative_name(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"unsafe evidence path: {value!r}")
    lowered = path.as_posix().lower()
    if any(part in lowered for part in FORBIDDEN_NAME_PARTS):
        raise ValueError(f"forbidden evidence filename: {value!r}")
    return path.as_posix()


def collect_entries(root: Path, evidence_files: Iterable[str]) -> dict[str, bytes]:
    root = root.resolve()
    entries: dict[str, bytes] = {}
    total = 0
    for value in evidence_files:
        name = _safe_relative_name(value)
        path = (root / Path(*PurePosixPath(name).parts)).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"evidence file escapes workspace: {name}") from exc
        if not path.is_file():
            raise FileNotFoundError(f"required defense evidence missing: {name}")
        data = path.read_bytes()
        if not data or len(data) > MAX_FILE_BYTES:
            raise ValueError(f"evidence file is empty or too large: {name}")
        if any(pattern.search(data) for pattern in SECRET_PATTERNS):
            raise ValueError(f"secret-like value detected in evidence file: {name}")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("defense pack exceeds total-size safety limit")
        entries[name] = data
    return entries


def _zip_write(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data)


def verify_pack(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path, "r") as archive:
        bad = archive.testzip()
        if bad is not None:
            raise ValueError(f"defense pack CRC verification failed: {bad}")
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("defense pack contains duplicate entry names")
        manifest_lines = archive.read("MANIFEST.sha256").decode("utf-8").splitlines()
        manifest: dict[str, str] = {}
        for line in manifest_lines:
            match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
            if not match:
                raise ValueError("defense pack manifest contains an invalid line")
            digest, name = match.groups()
            if name in manifest:
                raise ValueError(f"duplicate defense manifest path: {name}")
            manifest[name] = digest
        expected_evidence = set(names) - {"EVIDENCE_PACK.json", "MANIFEST.sha256"}
        if set(manifest) != expected_evidence:
            raise ValueError("defense manifest inventory mismatch")
        mismatches = [
            name for name, digest in manifest.items() if sha256_bytes(archive.read(name)) != digest
        ]
        if mismatches:
            raise ValueError(f"defense manifest hash mismatch: {mismatches[:3]}")
        metadata = json.loads(archive.read("EVIDENCE_PACK.json"))
        if metadata.get("entry_count") != len(manifest):
            raise ValueError("defense metadata entry count mismatch")
    return {
        "crc_test": "PASS",
        "manifest_test": "PASS",
        "evidence_entries": len(manifest),
        "zip_entries": len(names),
    }


def build_pack(
    root: Path,
    destination: Path,
    *,
    evidence_files: Iterable[str] = EVIDENCE_FILES,
) -> dict[str, Any]:
    entries = collect_entries(root, evidence_files)
    manifest_rows = [
        {"path": name, "bytes": len(data), "sha256": sha256_bytes(data)}
        for name, data in sorted(entries.items())
    ]
    metadata = {
        "schema_version": 1,
        "created_at": utc_now(),
        "purpose": "offline reviewer and defense evidence; no runtime secrets or trading capability",
        "entry_count": len(entries),
        "source_bytes": sum(len(data) for data in entries.values()),
        "boundaries": {
            "secret_scanned": True,
            "encrypted_server_backup_included": False,
            "environment_files_included": False,
            "trading_project_included": False,
            "telegram_send_capability_included": False,
        },
        "files": manifest_rows,
    }
    metadata_bytes = (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    manifest_bytes = "".join(
        f"{row['sha256']}  {row['path']}\n" for row in manifest_rows
    ).encode("utf-8")
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with zipfile.ZipFile(temporary, "w", allowZip64=False) as archive:
        for name, data in sorted(entries.items()):
            _zip_write(archive, name, data)
        _zip_write(archive, "EVIDENCE_PACK.json", metadata_bytes)
        _zip_write(archive, "MANIFEST.sha256", manifest_bytes)
    temporary.replace(destination)
    verification = verify_pack(destination)
    with zipfile.ZipFile(destination, "r") as archive:
        names = set(archive.namelist())
    expected_names = set(entries) | {"EVIDENCE_PACK.json", "MANIFEST.sha256"}
    if names != expected_names:
        raise ValueError("defense pack entry inventory mismatch")
    return {
        "schema_version": 1,
        "created_at": metadata["created_at"],
        "status": "PASS",
        "archive": str(destination),
        "archive_bytes": destination.stat().st_size,
        "archive_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "evidence_entries": verification["evidence_entries"],
        "zip_entries": verification["zip_entries"],
        "source_bytes": metadata["source_bytes"],
        "secret_scan": "PASS",
        "crc_test": verification["crc_test"],
        "manifest_test": verification["manifest_test"],
        "boundaries": metadata["boundaries"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stamp", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts" / "defense_pack")
    args = parser.parse_args()
    if not re.fullmatch(r"\d{8}T\d{6}Z", args.stamp):
        parser.error("stamp must use YYYYMMDDTHHMMSSZ")
    destination = args.output_dir / f"finance-radar-defense-evidence-{args.stamp}.zip"
    report = build_pack(ROOT, destination)
    report_path = args.output_dir / "defense_pack_latest.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
