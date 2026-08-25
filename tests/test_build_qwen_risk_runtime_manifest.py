from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.build_qwen_risk_runtime_manifest import build
from scripts.verify_qwen_risk_runtime_model import verify


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inputs(tmp_path: Path):
    gguf = tmp_path / "finance-radar-qwen-risk-v1.gguf"
    gguf.write_bytes(b"gguf")
    adapter = tmp_path / "adapter_model.safetensors"
    adapter.write_bytes(b"adapter")
    sft = tmp_path / "qwen_risk_sft_manifest.json"
    sft.write_text(
        json.dumps(
            {
                "base_model": "Qwen/Qwen2.5-1.5B-Instruct",
                "frozen_dataset_sha256": "f" * 64,
            }
        ),
        encoding="utf-8",
    )
    receipt = tmp_path / "qwen_risk_blind_receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "contract": "qwen-risk-human-blind-evaluation-v1",
                "status": "PASS",
                "rows": 180,
                "adapter_sha256": _sha(adapter),
                "sft_manifest_sha256": _sha(sft),
                "production_eligible": True,
                "no_trading": True,
                "gate_checks": {"all_rows_scored": True},
                "metrics": {},
            }
        ),
        encoding="utf-8",
    )
    return gguf, adapter, sft, receipt


def test_runtime_manifest_binds_model_adapter_dataset_and_blind_receipt(
    tmp_path: Path,
) -> None:
    gguf, adapter, sft, receipt = _inputs(tmp_path)
    output = tmp_path / "model-manifest.json"
    manifest = build(gguf, adapter, receipt, sft, output)
    assert manifest["adapter_sha256"] == _sha(adapter)
    assert manifest["human_blind_evaluation"]["rows"] == 180
    assert verify(output, gguf, expected_adapter_sha256=_sha(adapter))["status"] == "PASS"


def test_runtime_manifest_rejects_unbound_adapter(tmp_path: Path) -> None:
    gguf, adapter, sft, receipt = _inputs(tmp_path)
    adapter.write_bytes(b"different")
    with pytest.raises(ValueError, match="not bound"):
        build(gguf, adapter, receipt, sft, tmp_path / "model-manifest.json")
