#!/usr/bin/env python3
"""Dry-run or explicitly apply an immutable historical re-admission plan."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.event_quality_recovery import sha256_file, stable_json
from app.services.historical_primary_readmission import (
    apply_readmission_plan,
    validate_readmission_plan,
)


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def load_plan(plan_dir: Path) -> dict[str, Any]:
    manifest_path = plan_dir / "manifest.json"
    records_path = plan_dir / "readmission_plan.jsonl"
    sums_path = plan_dir / "SHA256SUMS.txt"
    for path in (manifest_path, records_path, sums_path):
        if not path.is_file():
            raise ValueError(f"readmission plan artifact missing: {path.name}")
    expected: dict[str, str] = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or len(digest) != 64:
            raise ValueError("invalid SHA256SUMS entry")
        expected[name] = digest.lower()
    for path in (manifest_path, records_path):
        if expected.get(path.name) != sha256_file(path):
            raise ValueError(f"SHA256SUMS mismatch: {path.name}")
    records = [
        json.loads(line)
        for line in records_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    plan = {**_json(manifest_path), "records": records}
    validate_readmission_plan(plan)
    return plan


def run(
    ledger: Path,
    plan_dir: Path,
    *,
    execute: bool = False,
    authorization_path: Path | None = None,
) -> dict[str, Any]:
    plan = load_plan(plan_dir)
    authorization = _json(authorization_path) if authorization_path else None
    if execute and authorization is None:
        raise ValueError("--authorization is required with --apply")
    return apply_readmission_plan(
        ledger,
        plan,
        authorization,
        execute=execute,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--plan-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--authorization", type=Path)
    args = parser.parse_args()
    print(
        stable_json(
            run(
                args.ledger,
                args.plan_dir,
                execute=args.apply,
                authorization_path=args.authorization,
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
