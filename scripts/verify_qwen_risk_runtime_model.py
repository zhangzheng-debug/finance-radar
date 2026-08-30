#!/usr/bin/env python3
"""Fail-closed verification for an accepted Qwen risk GGUF runtime bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


CONTRACT = "finance-radar-qwen-risk-runtime-v1"
AUTHORIZED_CONTRACT = "finance-radar-qwen-risk-runtime-v2"
BASE_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
PROMPT_VERSION = "qwen-risk-dual-review-consensus-v2"
SEMANTIC_CONTRACT = "qwen-risk-semantics-v1"
HYBRID_POLICY_VERSION = "qwen-v3-narrow-anchors-v1"
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
    lora_path: Path | None = None,
    expected_adapter_sha256: str | None = None,
) -> dict:
    manifest_path = manifest_path.resolve()
    model_path = model_path.resolve()
    if manifest_path.is_symlink() or model_path.is_symlink() or (
        lora_path is not None and lora_path.resolve().is_symlink()
    ):
        raise ValueError("runtime manifest and model must not be symlinks")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    contract = manifest.get("contract")
    schema_version = manifest.get("schema_version")
    if (contract, schema_version) not in {(CONTRACT, 1), (AUTHORIZED_CONTRACT, 2)}:
        raise ValueError("invalid Qwen runtime manifest contract")
    if manifest.get("base_model") != BASE_MODEL:
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
    if contract == AUTHORIZED_CONTRACT:
        if lora_path is None:
            raise ValueError("runtime LoRA is required by the authorized bundle")
        lora_path = lora_path.resolve()
        if manifest.get("lora_file") != lora_path.name:
            raise ValueError("runtime LoRA filename mismatch")
        lora_sha256 = str(manifest.get("lora_sha256") or "").casefold()
        if SHA256_RE.fullmatch(lora_sha256) is None:
            raise ValueError("runtime LoRA hash is invalid")
        if _sha256_file(lora_path) != lora_sha256:
            raise ValueError("runtime LoRA SHA-256 mismatch")
        if manifest.get("prompt_version") != PROMPT_VERSION:
            raise ValueError("runtime prompt version mismatch")
        if manifest.get("semantic_contract_version") != SEMANTIC_CONTRACT:
            raise ValueError("runtime semantic contract mismatch")
        if manifest.get("hybrid_policy_version") != HYBRID_POLICY_VERSION:
            raise ValueError("runtime hybrid policy mismatch")
        if manifest.get("training_basis") != "DUAL_REVIEW_AI_CONSENSUS":
            raise ValueError("runtime training basis mismatch")
        evaluation = manifest.get("evaluation")
        if not isinstance(evaluation, dict) or evaluation.get("status") != "PASS":
            raise ValueError("authorized evaluation receipt missing")
        metrics = evaluation.get("metrics")
        if not isinstance(metrics, dict):
            raise ValueError("authorized evaluation metrics missing")
        checks = {
            "rows": int(metrics.get("rows") or 0) >= 57,
            "exact_payload_accuracy": float(metrics.get("exact_payload_accuracy") or 0) >= 0.85,
            "materiality_macro_f1": float(metrics.get("materiality_macro_f1") or 0) >= 0.90,
            "polarity_macro_f1": float(metrics.get("polarity_macro_f1") or 0) >= 0.80,
            "priority_review_recall": float(metrics.get("priority_review_recall") or 0) >= 0.95,
            "false_priority_rate": float(metrics.get("false_priority_rate") or 1) <= 0.05,
        }
        if not all(checks.values()):
            raise ValueError("authorized evaluation thresholds have not passed")
        authorization = manifest.get("production_authorization")
        if not isinstance(authorization, dict):
            raise ValueError("production authorization missing")
        if authorization.get("authority") != "PROJECT_OWNER_EXPLICIT":
            raise ValueError("production authorization authority mismatch")
        if authorization.get("scope") != "PUBLIC_RESEARCH_SEMANTICS":
            raise ValueError("production authorization scope mismatch")
        if authorization.get("no_trading") is not True:
            raise ValueError("production authorization no_trading boundary missing")
        for field in (
            "sft_manifest_sha256",
            "model_report_sha256",
            "hybrid_report_sha256",
            "publication_policy_sha256",
        ):
            if SHA256_RE.fullmatch(str(manifest.get(field) or "").casefold()) is None:
                raise ValueError(f"runtime provenance hash missing: {field}")
        if manifest.get("production_eligible") is not True:
            raise ValueError("model is not production eligible")
        if manifest.get("no_trading") is not True:
            raise ValueError("no_trading boundary missing")
        return {
            "status": "PASS",
            "contract": AUTHORIZED_CONTRACT,
            "model_sha256": model_sha256,
            "lora_sha256": lora_sha256,
            "adapter_sha256": adapter_sha256,
            "evaluation_rows": int(metrics["rows"]),
            "training_basis": manifest["training_basis"],
            "no_trading": True,
        }
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
    parser.add_argument("--lora", type=Path)
    parser.add_argument("--expected-adapter-sha256")
    args = parser.parse_args()
    print(
        json.dumps(
            verify(
                args.manifest,
                args.model,
                lora_path=args.lora,
                expected_adapter_sha256=args.expected_adapter_sha256,
            ),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
