#!/usr/bin/env python3
"""Approve one verified Qwen runtime manifest for public research projection."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from app.models.qwen_risk_contract import (
    QWEN_RISK_CONTRACT_VERSION,
    QWEN_RISK_PROMPT_VERSION,
)
from app.storage import OperationsRepository
from app.storage.operations import QWEN_RISK_PUBLICATION_STATE_KEY


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def publish(manifest_path: Path, operations_db: Path) -> dict[str, object]:
    manifest_path = manifest_path.resolve()
    if manifest_path.is_symlink():
        raise ValueError("runtime manifest must not be a symlink")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("contract") != "finance-radar-qwen-risk-runtime-v2":
        raise ValueError("only the authorized Qwen v2 runtime may be published")
    if manifest.get("production_eligible") is not True or manifest.get("no_trading") is not True:
        raise ValueError("runtime manifest lacks production/no-trading authorization")
    if manifest.get("prompt_version") != QWEN_RISK_PROMPT_VERSION:
        raise ValueError("runtime prompt version does not match application code")
    if manifest.get("semantic_contract_version") != QWEN_RISK_CONTRACT_VERSION:
        raise ValueError("runtime semantic contract does not match application code")
    if (manifest.get("evaluation") or {}).get("status") != "PASS":
        raise ValueError("runtime evaluation has not passed")
    authorization = manifest.get("production_authorization") or {}
    if authorization.get("authority") != "PROJECT_OWNER_EXPLICIT":
        raise ValueError("project-owner authorization missing")
    adapter_sha256 = str(manifest.get("adapter_sha256") or "").casefold()
    if len(adapter_sha256) != 64:
        raise ValueError("runtime adapter hash is invalid")
    state = {
        "state": "PUBLIC_APPROVED",
        "public_approved": True,
        "model_version": "qwen-risk-" + adapter_sha256[:16],
        "adapter_sha256": adapter_sha256,
        "contract_version": QWEN_RISK_CONTRACT_VERSION,
        "prompt_version": QWEN_RISK_PROMPT_VERSION,
        "approval_receipt_sha256": _sha256_file(manifest_path),
        "approved_at": datetime.now(timezone.utc).isoformat(),
    }
    OperationsRepository(operations_db.resolve()).set_state(
        QWEN_RISK_PUBLICATION_STATE_KEY, state
    )
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--operations-db", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(publish(args.manifest, args.operations_db), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
