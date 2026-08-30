#!/usr/bin/env python3
"""Train and one-shot evaluate a CPU semantic-axes router on frozen AI reviews."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import sklearn
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.multiclass import OneVsRestClassifier
from sklearn.pipeline import FeatureUnion


MATERIALITY = {"MATERIAL_ADVERSE", "NOT_MATERIAL_ADVERSE", "UNCLEAR"}
POLARITY = {"ADVERSE", "MIXED", "NEUTRAL", "POSITIVE", "UNCLEAR"}
LABEL_CLASSIFICATION = "AI_REVIEW_NOT_HUMAN_GOLD"
PROMPT_VERSION = "qwen-core-axes-prompt-v11"
PROMPT_SHA256 = "52c149dc59b8d0f196cfac06cf1927df88d011bd0e8c13ad99f10ea88d33479e"
MODEL_OUTPUT_CONTRACT = "core-axes-v1"
TARGET_CONTRACT = "core-v1"
TRAIN_MANIFEST_CONTRACT = "qwen-core-train-independent-ai-review-overlay-v1"
DEV_MANIFEST_CONTRACT = "qwen-v15-combined-frozen-dev-v1"
FEATURE_CONTRACT = {
    "word": {
        "analyzer": "word",
        "ngram_range": [1, 2],
        "min_df": 2,
        "max_features": 35000,
        "sublinear_tf": True,
        "strip_accents": "unicode",
    },
    "char": {
        "analyzer": "char_wb",
        "ngram_range": [3, 5],
        "min_df": 2,
        "max_features": 30000,
        "sublinear_tf": True,
    },
}
ESTIMATOR_CONTRACT = {
    "type": "logistic_regression_ovr",
    "solver": "liblinear",
    "multiclass_strategy": "explicit_one_vs_rest_wrapper",
    "regularization": "sklearn_1_8_default_l2_equivalent",
    "C": 2.0,
    "class_weight": "balanced",
    "max_iter": 2000,
    "random_state": 42,
}


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_text_synced(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _runtime_contract() -> dict[str, Any]:
    try:
        git_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        tracked_dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain=v1", "--untracked-files=no"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
    except (OSError, subprocess.CalledProcessError):
        git_commit = "UNAVAILABLE"
        tracked_dirty = True
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "sklearn": sklearn.__version__,
        "joblib": joblib.__version__,
        "git_commit": git_commit,
        "tracked_worktree_dirty": tracked_dirty,
        "runner_path": str(Path(__file__).resolve()),
        "runner_sha256": sha256_path(Path(__file__).resolve()),
    }


def load_manifest(
    path: Path,
    *,
    role: str,
    expected_sha256: str,
    dataset_sha256: str,
) -> tuple[dict[str, Any], str]:
    manifest_sha = sha256_path(path)
    if manifest_sha != expected_sha256:
        raise ValueError(f"{role} manifest SHA256 mismatch")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("human_gold_claimed") is not False:
        raise ValueError(f"{role} manifest claims human gold")
    if manifest.get("label_classification") != LABEL_CLASSIFICATION:
        raise ValueError(f"{role} manifest label classification mismatch")
    if role == "TRAIN":
        if manifest.get("contract_version") != TRAIN_MANIFEST_CONTRACT:
            raise ValueError("TRAIN manifest contract mismatch")
        if manifest.get("model_output_contract") != MODEL_OUTPUT_CONTRACT:
            raise ValueError("TRAIN model output contract mismatch")
        output = (manifest.get("outputs") or {}).get("unique_audit") or {}
        if output.get("sha256") != dataset_sha256:
            raise ValueError("TRAIN manifest does not bind dataset")
        isolation = manifest.get("isolation") or {}
        for key in (
            "dev_metrics_read",
            "market_results_read",
            "qwen_predictions_read",
            "sealed_benchmark_read",
        ):
            if isolation.get(key) is not False:
                raise ValueError(f"TRAIN isolation flag mismatch: {key}")
    elif role == "DEV":
        if manifest.get("contract_version") != DEV_MANIFEST_CONTRACT:
            raise ValueError("DEV manifest contract mismatch")
        if manifest.get("dataset_role") != "DEV_SELECTION_ONLY":
            raise ValueError("DEV manifest role mismatch")
        if manifest.get("membership_policy") != "UNION_OF_PRE_FROZEN_COMPONENTS_NO_LABEL_FILTERING":
            raise ValueError("DEV membership policy mismatch")
        output = manifest.get("output") or {}
        if output.get("sha256") != dataset_sha256:
            raise ValueError("DEV manifest does not bind dataset")
        overlap = manifest.get("zero_cross_component_overlap") or {}
        if not overlap or not all(value is True for value in overlap.values()):
            raise ValueError("DEV component isolation is not closed")
    else:
        raise ValueError(f"unsupported manifest role: {role}")
    return manifest, manifest_sha


def _target(row: dict[str, Any]) -> tuple[str, str]:
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("row metadata is missing")
    target = metadata.get("semantic_target")
    if not isinstance(target, dict):
        raise ValueError("row semantic target is missing")
    messages = row.get("messages")
    if not isinstance(messages, list) or len(messages) != 3:
        raise ValueError("row messages are invalid")
    if messages[2].get("role") != "assistant":
        raise ValueError("row assistant message is invalid")
    assistant_target = json.loads(messages[2]["content"])
    if not isinstance(assistant_target, dict):
        raise ValueError("row assistant target is invalid")
    materiality = target.get("materiality")
    polarity = target.get("polarity")
    if materiality not in MATERIALITY or polarity not in POLARITY:
        raise ValueError("row semantic target is invalid")
    if (
        assistant_target.get("materiality") != materiality
        or assistant_target.get("polarity") != polarity
    ):
        raise ValueError("metadata and assistant semantic targets disagree")
    return materiality, polarity


def _text(row: dict[str, Any]) -> str:
    messages = row.get("messages")
    if not isinstance(messages, list) or len(messages) != 3:
        raise ValueError("row messages are invalid")
    if messages[1].get("role") != "user":
        raise ValueError("row user message is invalid")
    content = json.loads(messages[1]["content"])
    if not isinstance(content, dict):
        raise ValueError("row source content is invalid")
    passages = content.get("passages") or []
    passage_text = []
    for passage in passages:
        if isinstance(passage, dict):
            passage_text.extend(
                str(passage.get(field) or "")
                for field in ("document_type", "item_section", "passage")
            )
    parts = [
        str(content.get("headline") or ""),
        str(content.get("summary") or ""),
        *passage_text,
    ]
    text = " ".join(" ".join(parts).split())
    if len(text) < 12:
        raise ValueError("row source text is too short")
    return text


def load_rows(path: Path, *, role: str) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        metadata = row.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError(f"{role} line {line_number} metadata is invalid")
        if metadata.get("human_gold_claimed") is not False:
            raise ValueError(f"{role} line {line_number} claims human gold")
        if metadata.get("label_classification") != LABEL_CLASSIFICATION:
            raise ValueError(f"{role} line {line_number} label classification mismatch")
        if metadata.get("split") != role:
            raise ValueError(f"{role} line {line_number} split mismatch")
        if metadata.get("prompt_version") != PROMPT_VERSION:
            raise ValueError(f"{role} line {line_number} prompt version mismatch")
        if metadata.get("prompt_sha256") != PROMPT_SHA256:
            raise ValueError(f"{role} line {line_number} prompt hash mismatch")
        if metadata.get("model_output_contract") != MODEL_OUTPUT_CONTRACT:
            raise ValueError(f"{role} line {line_number} model output contract mismatch")
        if metadata.get("target_contract") != TARGET_CONTRACT:
            raise ValueError(f"{role} line {line_number} target contract mismatch")
        if metadata.get("evidence_state_used_as_model_target") is not False:
            raise ValueError(f"{role} line {line_number} includes evidence target")
        if metadata.get("post_event_market_data_included") is not False:
            raise ValueError(f"{role} line {line_number} includes market outcomes")
        if metadata.get("qwen_prediction_included") is not False:
            raise ValueError(f"{role} line {line_number} includes Qwen predictions")
        if role == "TRAIN":
            if metadata.get("label_provenance") != "INDEPENDENT_AI_REVIEW_CONSENSUS":
                raise ValueError(f"TRAIN line {line_number} provenance mismatch")
            if metadata.get("overlay_contract_version") != TRAIN_MANIFEST_CONTRACT:
                raise ValueError(f"TRAIN line {line_number} overlay contract mismatch")
            if metadata.get("overlay_view") != "UNIQUE_AUDIT":
                raise ValueError(f"TRAIN line {line_number} overlay view mismatch")
            if metadata.get("source_payload_binding_verified") is not True:
                raise ValueError(f"TRAIN line {line_number} payload binding mismatch")
            eligibility = metadata.get("training_eligibility")
            if not isinstance(eligibility, dict):
                raise ValueError(f"TRAIN line {line_number} eligibility is missing")
            if eligibility.get("eligible") is not True:
                continue
            if metadata.get("quality_exclusion") is not None:
                raise ValueError(f"TRAIN line {line_number} eligible row is excluded")
        elif metadata.get("label_provenance") != "DEEPSEEK_ISOLATED_MULTIVIEW_ARBITRATION":
            raise ValueError(f"DEV line {line_number} provenance mismatch")
        materiality, polarity = _target(row)
        content_sha = str(
            metadata.get("content_sha256")
            or metadata.get("source_content_sha256")
            or ""
        )
        identities = {
            "sample_id": str(metadata.get("sample_id") or ""),
            "event_id": str(metadata.get("event_id") or ""),
            "entity_group": str(metadata.get("entity_group") or ""),
            "event_chain_group": str(metadata.get("event_chain_group") or ""),
            "content_sha256": content_sha,
        }
        if any(not value for value in identities.values()):
            raise ValueError(f"{role} line {line_number} identity binding is incomplete")
        rows.append(
            {
                **identities,
                "text": _text(row),
                "materiality": materiality,
                "polarity": polarity,
            }
        )
    if not rows:
        raise ValueError(f"{role} has no valid rows")
    if len({row["sample_id"] for row in rows}) != len(rows):
        raise ValueError(f"{role} sample IDs are not unique")
    return rows


def overlap_audit(train: list[dict[str, Any]], dev: list[dict[str, Any]]) -> dict[str, int]:
    fields = ("sample_id", "event_id", "entity_group", "event_chain_group", "content_sha256")
    result = {}
    for field in fields:
        left = {row[field] for row in train if row[field]}
        right = {row[field] for row in dev if row[field]}
        result[field] = len(left & right)
    if any(result.values()):
        raise ValueError(f"TRAIN/DEV overlap detected: {stable_json(result)}")
    return result


def connected_groups(rows: list[dict[str, Any]]) -> list[str]:
    parent: dict[str, str] = {}

    def find(value: str) -> str:
        parent.setdefault(value, value)
        if parent[value] != value:
            parent[value] = find(parent[value])
        return parent[value]

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    keys_by_row = []
    for row in rows:
        keys = [f"sample:{row['sample_id']}"]
        if row["entity_group"]:
            keys.append(f"entity:{row['entity_group']}")
        if row["event_chain_group"]:
            keys.append(f"chain:{row['event_chain_group']}")
        if row.get("event_id"):
            keys.append(f"event:{row['event_id']}")
        if row.get("content_sha256"):
            keys.append(f"content:{row['content_sha256']}")
        for key in keys[1:]:
            union(keys[0], key)
        keys_by_row.append(keys)
    return [find(keys[0]) for keys in keys_by_row]


def build_features() -> FeatureUnion:
    word = TfidfVectorizer(
        lowercase=True,
        ngram_range=(1, 2),
        min_df=2,
        max_features=35000,
        sublinear_tf=True,
        strip_accents="unicode",
    )
    char = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(3, 5),
        min_df=2,
        max_features=30000,
        sublinear_tf=True,
    )
    return FeatureUnion([("word_tfidf", word), ("char_tfidf", char)])


def build_classifier() -> OneVsRestClassifier:
    return OneVsRestClassifier(
        LogisticRegression(
            C=2.0,
            class_weight="balanced",
            max_iter=2000,
            random_state=42,
            solver="liblinear",
        )
    )


def metrics(
    materiality_true: list[str],
    polarity_true: list[str],
    materiality_pred: list[str],
    polarity_pred: list[str],
) -> dict[str, Any]:
    priority_true = [value == "MATERIAL_ADVERSE" for value in materiality_true]
    priority_pred = [value == "MATERIAL_ADVERSE" for value in materiality_pred]
    tp = sum(t and p for t, p in zip(priority_true, priority_pred))
    fn = sum(t and not p for t, p in zip(priority_true, priority_pred))
    fp = sum(not t and p for t, p in zip(priority_true, priority_pred))
    tn = sum(not t and not p for t, p in zip(priority_true, priority_pred))
    return {
        "rows": len(materiality_true),
        "exact_pair_accuracy": sum(
            mt == mp and pt == pp
            for mt, pt, mp, pp in zip(
                materiality_true, polarity_true, materiality_pred, polarity_pred
            )
        )
        / len(materiality_true),
        "materiality_accuracy": accuracy_score(materiality_true, materiality_pred),
        "materiality_macro_f1": f1_score(
            materiality_true,
            materiality_pred,
            labels=sorted(MATERIALITY),
            average="macro",
            zero_division=0,
        ),
        "polarity_accuracy": accuracy_score(polarity_true, polarity_pred),
        "polarity_macro_f1": f1_score(
            polarity_true,
            polarity_pred,
            labels=sorted(POLARITY),
            average="macro",
            zero_division=0,
        ),
        "priority_recall": tp / (tp + fn) if tp + fn else 0.0,
        "non_priority_false_positive_rate": fp / (fp + tn) if fp + tn else 0.0,
        "confusion": {"tp": tp, "fn": fn, "fp": fp, "tn": tn},
    }


def train_and_evaluate(
    train_path: Path,
    train_manifest_path: Path,
    dev_path: Path,
    dev_manifest_path: Path,
    output_dir: Path,
    *,
    expected_train_sha256: str,
    expected_train_manifest_sha256: str,
    expected_dev_sha256: str,
    expected_dev_manifest_sha256: str,
    evaluation_status: str,
) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    train_sha = sha256_path(train_path)
    dev_sha = sha256_path(dev_path)
    if train_sha != expected_train_sha256 or dev_sha != expected_dev_sha256:
        raise ValueError("frozen input SHA256 mismatch")
    train_manifest, train_manifest_sha = load_manifest(
        train_manifest_path,
        role="TRAIN",
        expected_sha256=expected_train_manifest_sha256,
        dataset_sha256=train_sha,
    )
    dev_manifest, dev_manifest_sha = load_manifest(
        dev_manifest_path,
        role="DEV",
        expected_sha256=expected_dev_manifest_sha256,
        dataset_sha256=dev_sha,
    )
    train_rows = load_rows(train_path, role="TRAIN")
    dev_rows = load_rows(dev_path, role="DEV")
    expected_train_rows = ((train_manifest.get("distributions") or {}).get("trainable_unique") or {}).get("row_count")
    expected_dev_rows = dev_manifest.get("row_count")
    if len(train_rows) != expected_train_rows or len(dev_rows) != expected_dev_rows:
        raise ValueError("manifest row count does not match eligible dataset rows")
    overlaps = overlap_audit(train_rows, dev_rows)
    texts = [row["text"] for row in train_rows]
    materiality = [row["materiality"] for row in train_rows]
    polarity = [row["polarity"] for row in train_rows]
    groups = connected_groups(train_rows)

    oof_materiality = [""] * len(train_rows)
    oof_polarity = [""] * len(train_rows)
    folds = []
    splitter = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=42)
    for fold, (fit_idx, test_idx) in enumerate(
        splitter.split(texts, materiality, groups), 1
    ):
        feature = build_features()
        x_fit = feature.fit_transform([texts[index] for index in fit_idx])
        x_test = feature.transform([texts[index] for index in test_idx])
        materiality_model = build_classifier().fit(
            x_fit, [materiality[index] for index in fit_idx]
        )
        polarity_model = build_classifier().fit(
            x_fit, [polarity[index] for index in fit_idx]
        )
        materiality_predictions = materiality_model.predict(x_test).tolist()
        polarity_predictions = polarity_model.predict(x_test).tolist()
        for index, prediction in zip(test_idx, materiality_predictions):
            oof_materiality[int(index)] = str(prediction)
        for index, prediction in zip(test_idx, polarity_predictions):
            oof_polarity[int(index)] = str(prediction)
        folds.append({"fold": fold, "train_rows": len(fit_idx), "test_rows": len(test_idx)})
    if any(not value for value in oof_materiality + oof_polarity):
        raise RuntimeError("OOF predictions are incomplete")

    features = build_features()
    x_train = features.fit_transform(texts)
    materiality_model = build_classifier().fit(x_train, materiality)
    polarity_model = build_classifier().fit(x_train, polarity)
    x_dev = features.transform([row["text"] for row in dev_rows])
    dev_materiality = materiality_model.predict(x_dev).tolist()
    dev_polarity = polarity_model.predict(x_dev).tolist()
    dev_materiality_probability = materiality_model.predict_proba(x_dev)
    dev_polarity_probability = polarity_model.predict_proba(x_dev)

    train_metrics = metrics(
        materiality, polarity, oof_materiality, oof_polarity
    )
    dev_metrics = metrics(
        [row["materiality"] for row in dev_rows],
        [row["polarity"] for row in dev_rows],
        [str(value) for value in dev_materiality],
        [str(value) for value in dev_polarity],
    )
    gates = {
        "rows_min_200": len(dev_rows) >= 200,
        "exact_pair_accuracy_min_0_75": dev_metrics["exact_pair_accuracy"] >= 0.75,
        "materiality_macro_f1_min_0_70": dev_metrics["materiality_macro_f1"] >= 0.70,
        "polarity_macro_f1_min_0_65": dev_metrics["polarity_macro_f1"] >= 0.65,
        "priority_recall_min_0_80": dev_metrics["priority_recall"] >= 0.80,
        "non_priority_fpr_max_0_08": dev_metrics["non_priority_false_positive_rate"] <= 0.08,
        "zero_overlap": not any(overlaps.values()),
    }
    metric_gates_pass = all(gates.values())
    selection_allowed = evaluation_status == "FRESH_DEV_SELECTION_ONLY"
    qualified = metric_gates_pass and selection_allowed
    created_at = datetime.now(timezone.utc).isoformat()
    runtime = _runtime_contract()
    input_contract = {
        "train_sha256": train_sha,
        "train_manifest_sha256": train_manifest_sha,
        "dev_sha256": dev_sha,
        "dev_manifest_sha256": dev_manifest_sha,
        "evaluation_status": evaluation_status,
    }
    bundle = {
        "contract_version": "semantic-axes-tfidf-router-v15",
        "features": features,
        "materiality_model": materiality_model,
        "polarity_model": polarity_model,
        "materiality_classes": materiality_model.classes_.tolist(),
        "polarity_classes": polarity_model.classes_.tolist(),
        "feature_contract": FEATURE_CONTRACT,
        "estimator_contract": ESTIMATOR_CONTRACT,
        "input_contract": input_contract,
        "runtime_contract": runtime,
        "selection_gates": gates,
        "label_classification": LABEL_CLASSIFICATION,
        "human_gold_claimed": False,
        "no_trading": True,
        "shadow": True,
    }
    staging = output_dir.with_name(f".{output_dir.name}.staging-{uuid.uuid4().hex}")
    staging.mkdir(parents=True)
    try:
        artifact = staging / "semantic_axes_router_v15.joblib"
        joblib.dump(bundle, artifact, compress=3)
        with artifact.open("r+b") as handle:
            os.fsync(handle.fileno())
        artifact_sha = sha256_path(artifact)
        predictions = []
        for index, row in enumerate(dev_rows):
            predictions.append(
                {
                    "sample_id": row["sample_id"],
                    "expected": {
                        "materiality": row["materiality"],
                        "polarity": row["polarity"],
                    },
                    "predicted": {
                        "materiality": str(dev_materiality[index]),
                        "polarity": str(dev_polarity[index]),
                    },
                    "confidence": {
                        "materiality": float(dev_materiality_probability[index].max()),
                        "polarity": float(dev_polarity_probability[index].max()),
                    },
                }
            )
        report = {
            "schema_version": 1,
            "contract_version": "semantic-axes-tfidf-router-v15",
            "created_at": created_at,
            "inputs": {
                "train_path": str(train_path),
                "train_sha256": train_sha,
                "train_manifest_path": str(train_manifest_path),
                "train_manifest_sha256": train_manifest_sha,
                "train_rows": len(train_rows),
                "dev_path": str(dev_path),
                "dev_sha256": dev_sha,
                "dev_manifest_path": str(dev_manifest_path),
                "dev_manifest_sha256": dev_manifest_sha,
                "dev_rows": len(dev_rows),
            },
            "evaluation_status": evaluation_status,
            "runtime_contract": runtime,
            "label_provenance": LABEL_CLASSIFICATION,
            "human_gold_claimed": False,
            "feature_contract": FEATURE_CONTRACT,
            "estimator_contract": ESTIMATOR_CONTRACT,
            "cross_validation": {"folds": folds, "metrics": train_metrics},
            "development_evaluation": {
                "metrics": dev_metrics,
                "gates": gates,
                "metric_gates_pass": metric_gates_pass,
                "selection_allowed": selection_allowed,
                "gate_pass": qualified,
            },
            "overlap_audit": overlaps,
            "artifact": {"filename": artifact.name, "sha256": artifact_sha},
            "selection_decision": (
                "QUALIFIED_SHADOW_CANDIDATE"
                if qualified
                else "REJECTED_CONSUMED_DEV_DIAGNOSTIC"
                if not selection_allowed
                else "REJECTED_ON_FRESH_DEV"
            ),
            "sealed_benchmark_read": False,
            "production_model_changed": False,
            "no_trading": True,
            "shadow": True,
        }
        _write_text_synced(
            staging / "report.json", json.dumps(report, ensure_ascii=False, indent=2)
        )
        _write_text_synced(
            staging / "dev_predictions.jsonl",
            "".join(stable_json(row) + "\n" for row in predictions),
        )
        model_card = {
            "model_name": "Finance Radar Semantic Axes TF-IDF Router v15",
            "task": "Predict materiality and polarity from captured event text.",
            "artifact_sha256": artifact_sha,
            "label_classification": LABEL_CLASSIFICATION,
            "human_gold_claimed": False,
            "evaluation": report["development_evaluation"],
            "limitations": [
                "Targets are independent AI reviews, not human gold labels.",
                "This model ranks research events; it does not verify facts or trigger trades.",
                "The sealed benchmark remains unopened.",
            ],
            "shadow": True,
        }
        _write_text_synced(
            staging / "model_card.json",
            json.dumps(model_card, ensure_ascii=False, indent=2),
        )
        output_manifest = {
            "contract_version": "semantic-axes-router-v15-output-manifest-v1",
            "created_at": created_at,
            "evaluation_status": evaluation_status,
            "selection_decision": report["selection_decision"],
            "files": {
                name: {"sha256": sha256_path(staging / name), "bytes": (staging / name).stat().st_size}
                for name in (
                    artifact.name,
                    "report.json",
                    "dev_predictions.jsonl",
                    "model_card.json",
                )
            },
        }
        _write_text_synced(
            staging / "output_manifest.json",
            json.dumps(output_manifest, ensure_ascii=False, indent=2),
        )
        os.replace(staging, output_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", type=Path, required=True)
    parser.add_argument("--train-manifest", type=Path, required=True)
    parser.add_argument("--dev", type=Path, required=True)
    parser.add_argument("--dev-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-train-sha256", required=True)
    parser.add_argument("--expected-train-manifest-sha256", required=True)
    parser.add_argument("--expected-dev-sha256", required=True)
    parser.add_argument("--expected-dev-manifest-sha256", required=True)
    parser.add_argument(
        "--evaluation-status",
        choices=("FRESH_DEV_SELECTION_ONLY", "CONSUMED_DIAGNOSTIC_REPRODUCTION"),
        required=True,
    )
    args = parser.parse_args(argv)
    report = train_and_evaluate(
        args.train,
        args.train_manifest,
        args.dev,
        args.dev_manifest,
        args.output_dir,
        expected_train_sha256=args.expected_train_sha256,
        expected_train_manifest_sha256=args.expected_train_manifest_sha256,
        expected_dev_sha256=args.expected_dev_sha256,
        expected_dev_manifest_sha256=args.expected_dev_manifest_sha256,
        evaluation_status=args.evaluation_status,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
