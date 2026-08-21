#!/usr/bin/env python3
"""Dry-run or explicitly apply the machine-safe historical relation subset."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.event_quality_recovery import (
    apply_machine_relation_backfill,
    sha256_file,
    sha256_json,
    stable_json,
    validate_recovery_plan,
)


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def load_plan(plan_dir: Path) -> dict[str, Any]:
    manifest_path = plan_dir / "manifest.json"
    records_path = plan_dir / "recovery_plan.jsonl"
    sums_path = plan_dir / "SHA256SUMS.txt"
    for path in (manifest_path, records_path, sums_path):
        if not path.is_file():
            raise ValueError(f"recovery plan artifact is missing: {path.name}")
    expected: dict[str, str] = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or len(digest) != 64:
            raise ValueError("invalid SHA256SUMS entry")
        expected[name] = digest.lower()
    for path in (manifest_path, records_path):
        if expected.get(path.name) != sha256_file(path):
            raise ValueError(f"SHA256SUMS mismatch: {path.name}")
    manifest = _load_json(manifest_path)
    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(records_path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid recovery_plan.jsonl line {line_no}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"recovery_plan.jsonl line {line_no} is not an object")
        records.append(record)
    plan = {**manifest, "records": records}
    validate_recovery_plan(plan)
    return plan


def _write_exclusive(path: Path, payload: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _seal_audit_output(audit_output: Path) -> None:
    """Seal every file that exists, including terminal failure receipts."""

    sums_path = audit_output / "SHA256SUMS.txt"
    audit_files = sorted(
        path
        for path in audit_output.iterdir()
        if path.is_file() and path != sums_path
    )
    with sums_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(
            "".join(f"{sha256_file(path)}  {path.name}\n" for path in audit_files)
        )


def run(
    ledger: Path,
    plan_dir: Path,
    *,
    execute: bool = False,
    authorization_path: Path | None = None,
    audit_output: Path | None = None,
) -> dict[str, Any]:
    plan = load_plan(plan_dir)
    if not execute:
        return apply_machine_relation_backfill(ledger, plan, execute=False)
    if authorization_path is None:
        raise ValueError("--authorization is required with --apply")
    if audit_output is None:
        raise ValueError("--audit-output is required with --apply")
    authorization = _load_json(authorization_path)
    audit_output.mkdir(parents=True, exist_ok=False)
    intent = {
        "audit_contract_version": "event-quality-recovery-audit-v2",
        "state": "PREPARED",
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "target_ledger": str(ledger.resolve()),
        "plan_sha256": plan["plan_sha256"],
        "authorization_sha256": sha256_json(authorization),
        "authorization_id": authorization.get("authorization_id"),
        "scope_sha256": authorization.get("scope_sha256"),
        "target_ledger_identity_sha256": plan.get(
            "target_ledger_identity_sha256"
        ),
        "no_status_or_version_mutation": True,
        "no_trading": True,
    }
    intent["intent_sha256"] = sha256_json(intent)
    _write_exclusive(audit_output / "apply_intent.json", intent)
    result: dict[str, Any] | None = None
    try:
        result = apply_machine_relation_backfill(
            ledger,
            plan,
            authorization,
            execute=True,
        )
        _write_exclusive(audit_output / "apply_result.json", result)
    except Exception as exc:
        error_receipt = {
            "audit_contract_version": "event-quality-recovery-audit-v2",
            "state": (
                "DATABASE_COMMITTED_FILE_RECEIPT_FAILED"
                if result is not None
                else "ABORTED_OR_FAILED"
            ),
            "intent_sha256": intent["intent_sha256"],
            "error_type": type(exc).__name__,
            "error": str(exc),
            "durable_audit_id": (
                result.get("durable_audit_id") if result is not None else None
            ),
            "durable_result_sha256": (
                result.get("result_sha256") if result is not None else None
            ),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_exclusive(audit_output / "apply_error.json", error_receipt)
        _seal_audit_output(audit_output)
        raise
    _seal_audit_output(audit_output)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--plan-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--audit-output", type=Path)
    args = parser.parse_args()
    result = run(
        args.ledger,
        args.plan_dir,
        execute=args.apply,
        authorization_path=args.authorization,
        audit_output=args.audit_output,
    )
    print(stable_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
