#!/usr/bin/env python3
"""Fail-closed verification for an accepted Qwen risk GGUF runtime model."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


CONTRACT = "finance-radar-qwen-risk-runtime-v1"
SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(
    manifest_path: Path,
    model_path: Path,
    *,
    expected_adapter_sha256: str | None = None,
) -> dict:
    manifest_path = manifest_path.resolve()
    model_path = model_path.resolve()
    if manifest_path.is_symlink() or model_path.is_symlink():
        raise ValueError("runtime manifest and model must not be symlinks")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("contract") != CONTRACT or manifest.get("schema_version") != 1:
        raise ValueError("invalid Qwen runtime manifest contract")
    if manifest.get("base_model") != "Qwen/Qwen2.5-1.5B-Instruct":
        raise ValueError("unexpected Qwen base model")
    if manifest.get("model_file") != model_path.name:
        raise ValueError("runtime model filename mismatch")
    model_sha256 = str(manifest.get("model_sha256") or "").casefold()
    adapter_sha256 = str(manifest.get("adapter_sha256") or "").casefold()
    if SHA256_RE.fullmatch(model_sha256) is None or SHA256_RE.fullmatch(adapter_sha256) is None:
        raise ValueError("runtime model hashes are invalid")
    if expected_adapter_sha256 and adapter_sha256 != expected_adapter_sha256.casefold():
        raise ValueError("runtime adapter hash does not match service configuration")
    # A quantized 1.5B model is large enough that read_bytes() can temporarily
    # consume most of the memory reserved for the model service.  Stream the
    # integrity check before llama-server starts instead.
    actual_model_sha256 = _sha256_file(model_path)
    if actual_model_sha256 != model_sha256:
        raise ValueError("runtime GGUF SHA-256 mismatch")
    blind = manifest.get("human_blind_evaluation")
    if not isinstance(blind, dict):
        raise ValueError("human blind evaluation receipt missing")
    if blind.get("status") != "PASS" or int(blind.get("rows") or 0) < 120:
        raise ValueError("human blind evaluation has not passed")
    for field in (
        "sft_manifest_sha256",
        "frozen_dataset_sha256",
    ):
        if SHA256_RE.fullmatch(str(manifest.get(field) or "").casefold()) is None:
            raise ValueError(f"runtime provenance hash missing: {field}")
    if blind.get("contract") != "qwen-risk-human-blind-evaluation-v1":
        raise ValueError("human blind evaluation contract mismatch")
    if SHA256_RE.fullmatch(str(blind.get("receipt_sha256") or "").casefold()) is None:
        raise ValueError("human blind receipt hash missing")
    if blind.get("blind_reuse_allowed") is not False:
        raise ValueError("human blind reuse boundary missing")
    if manifest.get("production_eligible") is not True:
        raise ValueError("model is not production eligible")
    if manifest.get("no_trading") is not True:
        raise ValueError("no_trading boundary missing")
    return {
        "status": "PASS",
        "contract": CONTRACT,
        "model_sha256": model_sha256,
        "adapter_sha256": adapter_sha256,
        "human_blind_rows": int(blind["rows"]),
        "no_trading": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--expected-adapter-sha256")
    args = parser.parse_args()
    print(
        json.dumps(
            verify(
                args.manifest,
                args.model,
                expected_adapter_sha256=args.expected_adapter_sha256,
            ),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
