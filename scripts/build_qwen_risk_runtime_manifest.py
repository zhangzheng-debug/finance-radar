#!/usr/bin/env python3
"""Bind one passing blind receipt to the exact merged Qwen GGUF runtime file."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


RUNTIME_CONTRACT = "finance-radar-qwen-risk-runtime-v1"
BLIND_CONTRACT = "qwen-risk-human-blind-evaluation-v1"
BASE_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(
    gguf_path: Path,
    adapter_path: Path,
    blind_receipt_path: Path,
    sft_manifest_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    gguf_path = gguf_path.resolve()
    adapter_path = adapter_path.resolve()
    blind_receipt_path = blind_receipt_path.resolve()
    sft_manifest_path = sft_manifest_path.resolve()
    output_path = output_path.resolve()
    if output_path.exists():
        raise ValueError("runtime manifest already exists")
    if gguf_path.name != "finance-radar-qwen-risk-v1.gguf":
        raise ValueError("runtime GGUF must use the deployment's fixed filename")
    receipt = json.loads(blind_receipt_path.read_text(encoding="utf-8"))
    sft_manifest = json.loads(sft_manifest_path.read_text(encoding="utf-8"))
    adapter_sha256 = _sha256_file(adapter_path)
    sft_manifest_sha256 = _sha256_file(sft_manifest_path)
    if receipt.get("contract") != BLIND_CONTRACT or receipt.get("status") != "PASS":
        raise ValueError("blind evaluation receipt has not passed")
    if receipt.get("production_eligible") is not True or receipt.get("no_trading") is not True:
        raise ValueError("blind receipt lacks production/no-trading boundary")
    if int(receipt.get("rows") or 0) < 120:
        raise ValueError("blind receipt does not contain at least 120 rows")
    if receipt.get("adapter_sha256") != adapter_sha256:
        raise ValueError("blind receipt is not bound to this adapter")
    if receipt.get("sft_manifest_sha256") != sft_manifest_sha256:
        raise ValueError("blind receipt is not bound to this SFT manifest")
    if sft_manifest.get("base_model") != BASE_MODEL:
        raise ValueError("unexpected Qwen base model")
    frozen_sha256 = str(sft_manifest.get("frozen_dataset_sha256") or "").casefold()
    if SHA256_RE.fullmatch(frozen_sha256) is None:
        raise ValueError("SFT manifest frozen dataset hash is invalid")
    manifest = {
        "schema_version": 1,
        "contract": RUNTIME_CONTRACT,
        "base_model": BASE_MODEL,
        "model_file": gguf_path.name,
        "model_sha256": _sha256_file(gguf_path),
        "adapter_sha256": adapter_sha256,
        "sft_manifest_sha256": sft_manifest_sha256,
        "frozen_dataset_sha256": frozen_sha256,
        "human_blind_evaluation": {
            "contract": BLIND_CONTRACT,
            "status": "PASS",
            "rows": int(receipt["rows"]),
            "receipt_sha256": _sha256_file(blind_receipt_path),
            "gate_checks": receipt.get("gate_checks"),
            "metrics": receipt.get("metrics"),
            "blind_reuse_allowed": False,
        },
        "production_eligible": True,
        "no_trading": True,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gguf", type=Path, required=True)
    parser.add_argument("--adapter-model", type=Path, required=True)
    parser.add_argument("--blind-receipt", type=Path, required=True)
    parser.add_argument("--sft-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = build(
        args.gguf,
        args.adapter_model,
        args.blind_receipt,
        args.sft_manifest,
        args.output,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
