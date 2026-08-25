#!/usr/bin/env python3
"""Consume the sealed Qwen human-blind split exactly once and score a candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from app.models.qwen_risk_contract import (
    QWEN_RISK_CONTRACT_VERSION,
    QWEN_RISK_PROMPT_VERSION,
    expected_semantic_payload,
    normalize_qwen_risk_content,
)
from app.services.qwen_risk_semantics import QwenRiskModelProvider


CONTRACT = "qwen-risk-human-blind-evaluation-v1"
DEFAULT_THRESHOLDS = {
    "materiality_macro_f1": 0.65,
    "polarity_macro_f1": 0.55,
    "priority_review_recall": 0.75,
}


class Predictor(Protocol):
    model_version: str

    def predict_content(self, content: dict[str, Any]) -> tuple[dict[str, str], float]: ...


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path.name}:{number} is not an object")
        rows.append(value)
    return rows


def _verify_frozen_dataset(path: Path) -> tuple[list[dict[str, Any]], str]:
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if not sidecar.is_file():
        raise ValueError("frozen dataset SHA-256 sidecar is required")
    parts = sidecar.read_text(encoding="ascii").strip().split()
    if len(parts) != 2 or parts[1] != path.name or len(parts[0]) != 64:
        raise ValueError("frozen dataset SHA-256 sidecar is invalid")
    digest = _sha256_file(path)
    if digest != parts[0].casefold():
        raise ValueError("frozen dataset SHA-256 mismatch")
    return _read_jsonl(path), digest


def _verified_blind_manifest(sft_manifest_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    sft_manifest = json.loads(sft_manifest_path.read_text(encoding="utf-8"))
    outputs = sft_manifest.get("outputs")
    if not isinstance(outputs, dict):
        raise ValueError("SFT manifest outputs missing")
    name = "qwen_risk_blind_manifest.jsonl"
    expected = str(outputs.get(name) or "").casefold()
    path = (sft_manifest_path.parent / name).resolve()
    if len(expected) != 64 or not path.is_file() or _sha256_file(path) != expected:
        raise ValueError("sealed blind manifest digest mismatch")
    return _read_jsonl(path), sft_manifest, _sha256_file(sft_manifest_path)


def _axis_metrics(expected: list[str], predicted: list[str]) -> dict[str, Any]:
    if not expected or len(expected) != len(predicted):
        return {"rows": len(expected), "accuracy": 0.0, "macro_f1": 0.0, "per_class": {}}
    labels = sorted(set(expected) | set(predicted))
    correct = sum(left == right for left, right in zip(expected, predicted))
    per_class: dict[str, dict[str, Any]] = {}
    f1_values: list[float] = []
    for label in labels:
        tp = sum(left == label and right == label for left, right in zip(expected, predicted))
        fp = sum(left != label and right == label for left, right in zip(expected, predicted))
        fn = sum(left == label and right != label for left, right in zip(expected, predicted))
        support = sum(left == label for left in expected)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        if support:
            f1_values.append(f1)
        per_class[label] = {
            "support": support,
            "predicted": sum(right == label for right in predicted),
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "f1": round(f1, 6),
        }
    return {
        "rows": len(expected),
        "accuracy": round(correct / len(expected), 6),
        "macro_f1": round(sum(f1_values) / len(f1_values), 6) if f1_values else 0.0,
        "per_class": per_class,
    }


def evaluate(
    frozen_dataset: Path,
    sft_manifest_path: Path,
    adapter_path: Path,
    output_dir: Path,
    provider: Predictor,
    *,
    thresholds: dict[str, float] | None = None,
    minimum_rows: int = 120,
    retries: int = 2,
) -> dict[str, Any]:
    """Evaluate once; creating the output directory permanently consumes the blind set."""

    frozen_dataset = frozen_dataset.resolve()
    sft_manifest_path = sft_manifest_path.resolve()
    adapter_path = adapter_path.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise ValueError("blind evaluation output already exists; the holdout may not be reused")
    rows, frozen_sha256 = _verify_frozen_dataset(frozen_dataset)
    blind_manifest, sft_manifest, sft_manifest_sha256 = _verified_blind_manifest(
        sft_manifest_path
    )
    if sft_manifest.get("frozen_dataset_sha256") != frozen_sha256:
        raise ValueError("SFT manifest is not bound to this frozen dataset")
    if sft_manifest.get("semantic_contract_version") != QWEN_RISK_CONTRACT_VERSION:
        raise ValueError("semantic contract version mismatch")
    if sft_manifest.get("prompt_version") != QWEN_RISK_PROMPT_VERSION:
        raise ValueError("prompt version mismatch")
    adapter_sha256 = _sha256_file(adapter_path)
    expected_adapter_sha256 = str(
        getattr(provider, "adapter_sha256", adapter_sha256) or ""
    ).casefold()
    if expected_adapter_sha256 != adapter_sha256:
        raise ValueError("provider adapter hash does not match candidate adapter")

    manifest_by_id = {str(row.get("sample_id") or ""): row for row in blind_manifest}
    blind_rows = [row for row in rows if row.get("split") == "HUMAN_BLIND"]
    if len(blind_rows) != len(manifest_by_id) or len(blind_rows) < int(minimum_rows):
        raise ValueError("sealed human-blind row count mismatch or below minimum")
    for row in blind_rows:
        sample_id = str(row.get("sample_id") or "")
        content = row.get("content")
        if not sample_id or sample_id not in manifest_by_id or not isinstance(content, dict):
            raise ValueError("frozen blind row is not represented by the sealed manifest")
        digest = hashlib.sha256(
            _stable_json(normalize_qwen_risk_content(content)).encode("utf-8")
        ).hexdigest()
        if digest != manifest_by_id[sample_id].get("content_sha256"):
            raise ValueError(f"blind content digest mismatch for {sample_id}")

    output_dir.mkdir(parents=True)
    evaluated_at = datetime.now(timezone.utc).isoformat()
    consumed = {
        "contract": CONTRACT,
        "status": "BLIND_CONSUMED",
        "evaluated_at": evaluated_at,
        "frozen_dataset_sha256": frozen_sha256,
        "sft_manifest_sha256": sft_manifest_sha256,
        "adapter_sha256": adapter_sha256,
        "model_version": provider.model_version,
        "rows": len(blind_rows),
        "rerun_allowed": False,
    }
    (output_dir / "BLIND_CONSUMED.json").write_text(
        json.dumps(consumed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    details: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    retry_limit = max(0, min(int(retries), 3))
    for row in blind_rows:
        sample_id = str(row["sample_id"])
        expected = expected_semantic_payload(
            str(row.get("materiality") or ""), str(row.get("polarity") or "")
        )
        prediction: dict[str, str] | None = None
        latency_ms = 0.0
        last_error: Exception | None = None
        for attempt in range(retry_limit + 1):
            try:
                prediction, latency_ms = provider.predict_content(row["content"])
                break
            except Exception as exc:  # the consumed receipt must survive every provider failure
                last_error = exc
                if attempt < retry_limit:
                    time.sleep(min(0.25 * (2**attempt), 1.0))
        if prediction is None:
            errors.append(
                {
                    "sample_id": sample_id,
                    "error": f"{type(last_error).__name__}:{str(last_error)[:240]}",
                }
            )
            continue
        details.append(
            {
                "sample_id": sample_id,
                "evidence_state": row.get("evidence_state"),
                "expected": expected,
                "predicted": prediction,
                "exact_match": prediction == expected,
                "latency_ms": round(float(latency_ms), 3),
            }
        )

    successful = len(details)
    materiality = _axis_metrics(
        [item["expected"]["materiality"] for item in details],
        [item["predicted"]["materiality"] for item in details],
    )
    polarity = _axis_metrics(
        [item["expected"]["polarity"] for item in details],
        [item["predicted"]["polarity"] for item in details],
    )
    priority = _axis_metrics(
        [item["expected"]["semantic_priority"] for item in details],
        [item["predicted"]["semantic_priority"] for item in details],
    )
    required = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        required.update({key: float(value) for key, value in thresholds.items()})
    priority_recall = float(
        priority.get("per_class", {}).get("PRIORITY_REVIEW", {}).get("recall", 0.0)
    )
    gate_checks = {
        "all_rows_scored": successful == len(blind_rows) and not errors,
        "materiality_macro_f1": materiality["macro_f1"] >= required["materiality_macro_f1"],
        "polarity_macro_f1": polarity["macro_f1"] >= required["polarity_macro_f1"],
        "priority_review_recall": priority_recall >= required["priority_review_recall"],
    }
    status = "PASS" if all(gate_checks.values()) else "FAIL"
    latencies = sorted(float(item["latency_ms"]) for item in details)
    report = {
        **consumed,
        "status": status,
        "successful_rows": successful,
        "errors": errors,
        "thresholds": required,
        "gate_checks": gate_checks,
        "metrics": {
            "materiality": materiality,
            "polarity": polarity,
            "semantic_priority": priority,
            "priority_review_recall": round(priority_recall, 6),
            "exact_match_rate": round(
                sum(bool(item["exact_match"]) for item in details) / successful, 6
            )
            if successful
            else 0.0,
            "latency_ms_p50": latencies[len(latencies) // 2] if latencies else None,
            "latency_ms_max": latencies[-1] if latencies else None,
        },
        "details": details,
        "blind_reuse_allowed": False,
        "production_eligible": status == "PASS",
        "no_trading": True,
    }
    private_path = output_dir / "qwen_risk_blind_private_report.json"
    private_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    public = {key: value for key, value in report.items() if key not in {"details", "errors"}}
    public["private_report_sha256"] = _sha256_file(private_path)
    public_path = output_dir / "qwen_risk_blind_receipt.json"
    public_path.write_text(
        json.dumps(public, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    public["receipt_sha256"] = _sha256_file(public_path)
    return public


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen-dataset", type=Path, required=True)
    parser.add_argument("--sft-manifest", type=Path, required=True)
    parser.add_argument("--adapter-model", type=Path, required=True)
    parser.add_argument("--model-url", default="http://127.0.0.1:18602")
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--minimum-rows", type=int, default=120)
    parser.add_argument("--retries", type=int, default=2)
    args = parser.parse_args()
    adapter_sha256 = _sha256_file(args.adapter_model.resolve())
    provider = QwenRiskModelProvider(
        args.model_url,
        args.model_name,
        adapter_sha256,
        timeout_seconds=60,
        max_tokens=180,
    )
    result = evaluate(
        args.frozen_dataset,
        args.sft_manifest,
        args.adapter_model,
        args.output_dir,
        provider,
        minimum_rows=args.minimum_rows,
        retries=args.retries,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
