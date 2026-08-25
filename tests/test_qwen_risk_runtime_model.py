from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.verify_qwen_risk_runtime_model import verify


def _bundle(tmp_path: Path, *, status: str = "PASS") -> tuple[Path, Path, str]:
    model = tmp_path / "finance-radar-qwen-risk-v1.gguf"
    model.write_bytes(b"test-gguf")
    adapter = "a" * 64
    manifest = tmp_path / "model-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "contract": "finance-radar-qwen-risk-runtime-v1",
                "base_model": "Qwen/Qwen2.5-1.5B-Instruct",
                "model_file": model.name,
                "model_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
                "adapter_sha256": adapter,
                "sft_manifest_sha256": "b" * 64,
                "frozen_dataset_sha256": "c" * 64,
                "human_blind_evaluation": {
                    "contract": "qwen-risk-human-blind-evaluation-v1",
                    "status": status,
                    "rows": 180,
                    "receipt_sha256": "d" * 64,
                    "blind_reuse_allowed": False,
                },
                "production_eligible": True,
                "no_trading": True,
            }
        ),
        encoding="utf-8",
    )
    return manifest, model, adapter


def test_runtime_model_requires_hash_and_passing_blind_receipt(tmp_path: Path) -> None:
    manifest, model, adapter = _bundle(tmp_path)
    result = verify(manifest, model, expected_adapter_sha256=adapter)
    assert result["status"] == "PASS"
    model.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="GGUF SHA-256 mismatch"):
        verify(manifest, model, expected_adapter_sha256=adapter)


def test_runtime_model_blocks_failed_blind_evaluation(tmp_path: Path) -> None:
    manifest, model, adapter = _bundle(tmp_path, status="FAIL")
    with pytest.raises(ValueError, match="has not passed"):
        verify(manifest, model, expected_adapter_sha256=adapter)
