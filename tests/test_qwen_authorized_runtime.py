import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.build_qwen_authorized_runtime_manifest import build
from scripts.publish_qwen_risk_runtime import publish
from scripts.verify_qwen_risk_runtime_model import verify


ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_authorized_runtime_build_verify_and_publish(tmp_path: Path) -> None:
    model = tmp_path / "qwen2.5-1.5b-instruct-q4_k_m.gguf"
    lora = tmp_path / "finance-radar-qwen-risk-v3-lora-f16.gguf"
    adapter = tmp_path / "adapter_model.safetensors"
    model.write_bytes(b"model")
    lora.write_bytes(b"lora")
    adapter.write_bytes(b"adapter")
    validation_sha = "a" * 64
    predictions_sha = "b" * 64
    sft = tmp_path / "sft.json"
    model_report = tmp_path / "model-report.json"
    hybrid_report = tmp_path / "hybrid-report.json"
    _write_json(
        sft,
        {
            "base_model": "Qwen/Qwen2.5-1.5B-Instruct",
            "prompt_version": "qwen-risk-dual-review-consensus-v2",
            "semantic_contract_version": "qwen-risk-semantics-v1",
            "human_gold_claimed": False,
            "output_sha256": {"validation": validation_sha},
        },
    )
    _write_json(
        model_report,
        {"dataset_sha256": validation_sha, "predictions_sha256": predictions_sha},
    )
    _write_json(
        hybrid_report,
        {
            "dataset_sha256": validation_sha,
            "model_predictions_sha256": predictions_sha,
            "hybrid_metrics": {
                "rows": 57,
                "exact_payload_accuracy": 0.90,
                "materiality": {"macro_f1_truth_supported_classes": 0.94},
                "polarity": {"macro_f1_truth_supported_classes": 0.82},
                "priority_review": {"recall": 1.0, "false_priority_rate": 0.04},
            },
            "hybrid_gate": {
                "passed": True,
                "decision": "QUALIFIED_SHADOW_SEMANTIC_CANDIDATE",
            },
        },
    )
    policy = tmp_path / "policy.json"
    _write_json(
        policy,
        {
            "accepted_gate": "QUALIFIED_SHADOW_SEMANTIC_CANDIDATE",
            "minimum_metrics": {
                "rows": 57,
                "exact_payload_accuracy": 0.85,
                "materiality_macro_f1": 0.90,
                "polarity_macro_f1": 0.80,
                "priority_review_recall": 0.95,
                "false_priority_rate": 0.05,
            },
            "frozen_hashes": {
                "model_sha256": _sha(model),
                "lora_sha256": _sha(lora),
                "adapter_sha256": _sha(adapter),
                "sft_manifest_sha256": _sha(sft),
                "model_report_sha256": _sha(model_report),
                "hybrid_report_sha256": _sha(hybrid_report),
            },
            "production_authorization": {
                "authority": "PROJECT_OWNER_EXPLICIT",
                "authorized_at": "2026-08-30",
                "scope": "PUBLIC_RESEARCH_SEMANTICS",
                "no_trading": True,
            },
        },
    )
    manifest_path = tmp_path / "runtime.json"
    manifest = build(
        model,
        lora,
        adapter,
        sft,
        model_report,
        hybrid_report,
        policy,
        manifest_path,
    )
    receipt = verify(
        manifest_path,
        model,
        lora_path=lora,
        expected_adapter_sha256=_sha(adapter),
    )
    assert manifest["training_basis"] == "DUAL_REVIEW_AI_CONSENSUS"
    assert receipt["status"] == "PASS"
    operations_db = tmp_path / "operations.sqlite3"
    publication = publish(manifest_path, operations_db)
    assert publication["state"] == "PUBLIC_APPROVED"
    assert publication["model_version"] == "qwen-risk-" + _sha(adapter)[:16]


def test_qwen_runtime_scripts_resolve_project_imports_outside_repo(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["FINANCE_RADAR_QWEN_RISK_ENABLED"] = "0"
    worker = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_qwen_risk_worker.py"),
            "--limit",
            "1",
            "--scan-limit",
            "1",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert worker.returncode == 0, worker.stderr
    assert json.loads(worker.stdout)["status"] == "DISABLED"

    publisher = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "publish_qwen_risk_runtime.py"),
            "--help",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert publisher.returncode == 0, publisher.stderr
    assert "--manifest" in publisher.stdout
