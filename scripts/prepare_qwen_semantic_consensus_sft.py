#!/usr/bin/env python3
"""Prepare a Qwen SFT experiment from the clean A/B semantic-consensus subset.

This is not a human-gold freeze.  It preserves only rows where both reviewers
independently chose the same materiality and polarity, removes rows contradicted
by a high-precision semantic rule, and makes deterministic train/validation/
owner-holdout splits.  Evidence posture, reviewer rationale, prices, and prior
model output never enter the messages or target.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.qwen_risk_contract import (  # noqa: E402
    QWEN_RISK_CONTRACT_VERSION,
    expected_semantic_payload,
    normalize_qwen_risk_content,
)
from app.services.human_gold_review import validate_submission  # noqa: E402
from scripts.train_risk_router_ai_adjudicated import (  # noqa: E402
    _risk_first_policy,
    _zip_json,
    sha256_bytes,
    stable_json,
)


CONTRACT_VERSION = "qwen-semantic-dual-review-consensus-experiment-v3"
SPLIT_SALT = "finance-radar-qwen-semantic-consensus-v1"
EXPERIMENT_PROMPT_VERSION = "qwen-risk-dual-review-consensus-v2"
EXPERIMENT_SYSTEM_PROMPT = (
    "你是金融雷达的语义风险分类器。只判断所给文本表达的极性与做空风险重大性，"
    "不判断证据真假，不补充外部事实，不给投资建议。"
    "已发生或正式披露的破产重组、Form 25或确定退市、现金不足或无法融资将缩减业务、"
    "已发生违约、正式监管处罚、重大内控审计失败、关键临床失败，通常属于"
    "MATERIAL_ADVERSE与ADVERSE；单纯风险因素、合同定义、假设性清算、已解决问题或"
    "有偿并购退市不得仅凭关键词判为重大负面。明确业务改善或成功结果可判POSITIVE，"
    "普通信息披露判NEUTRAL。仅输出指定 JSON。"
)
TRAIN_TARGET_MULTIPLIERS = {
    ("MATERIAL_ADVERSE", "ADVERSE"): 3,
    ("NOT_MATERIAL_ADVERSE", "POSITIVE"): 4,
    ("NOT_MATERIAL_ADVERSE", "ADVERSE"): 4,
}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _read_git_json(repository: Path, specification: str) -> dict[str, Any]:
    raw = subprocess.check_output(["git", "show", specification], cwd=repository)
    return json.loads(raw.decode("utf-8-sig"))


def _rank(sample_id: str) -> str:
    return hashlib.sha256(f"{SPLIT_SALT}:{sample_id}".encode()).hexdigest()


def _semantic_route(materiality: str, polarity: str) -> str:
    payload = expected_semantic_payload(materiality, polarity)
    return str(payload["semantic_priority"])


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> str:
    path.write_text("".join(stable_json(row) + "\n" for row in rows), encoding="utf-8")
    return sha256_bytes(path.read_bytes())


def _prepared_row(sample: dict[str, Any], review: dict[str, Any], split: str) -> dict[str, Any]:
    review_input = normalize_qwen_risk_content(sample["content"])
    target = expected_semantic_payload(review["materiality"], review["polarity"])
    return {
        "messages": [
            {"role": "system", "content": EXPERIMENT_SYSTEM_PROMPT},
            {"role": "user", "content": stable_json(review_input)},
            {"role": "assistant", "content": stable_json(target)},
        ],
        "metadata": {
            "sample_id": sample["sample_id"],
            "event_id": sample.get("event_id"),
            "entity_group": sample.get("entity_group"),
            "event_chain_group": sample.get("event_chain_group"),
            "content_sha256": sha256_bytes(stable_json(review_input).encode()),
            "split": split,
            "label_provenance": "DUAL_REVIEW_SEMANTIC_PAIR_CONSENSUS",
            "human_gold_claimed": False,
            "evidence_state_used_as_model_target": False,
            "post_event_market_data_included": False,
            "model_output_included_in_review": False,
        },
    }


def _balanced_training_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Repeat clean minority classes without importing validation examples.

    Version 1 only targeted a 25% priority share.  Validation showed that this
    still collapsed positive and low-adverse examples into neutral, and missed
    one third of priority events.  These fixed multipliers operate exclusively
    on the training split and keep every copy linked to its origin.
    """

    balanced: list[dict[str, Any]] = []
    for row in rows:
        target = json.loads(row["messages"][-1]["content"])
        pair = (target["materiality"], target["polarity"])
        multiplier = TRAIN_TARGET_MULTIPLIERS.get(pair, 1)
        for repeat_index in range(multiplier):
            copy = json.loads(json.dumps(row))
            if repeat_index:
                copy["metadata"]["training_repeat"] = repeat_index
                copy["metadata"]["origin_sample_id"] = row["metadata"]["sample_id"]
            balanced.append(copy)
    return balanced


def prepare(
    *,
    owner_package: Path,
    review_a: Path,
    review_b: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    with zipfile.ZipFile(owner_package) as archive:
        owner = _zip_json(archive, "owner_manifest.json")
        assignment_a = _zip_json(archive, "assignment_A.json")
        assignment_b = _zip_json(archive, "assignment_B.json")
    submission_a = _read_json(review_a)
    validation_a = validate_submission(assignment_a, submission_a)
    validation_b = validate_submission(assignment_b, review_b)
    if not validation_a["valid"] or not validation_b["valid"]:
        raise ValueError("A/B strict validation failed")

    reviews: dict[str, dict[str, dict[str, Any]]] = {}
    for slot, submission in (("A", submission_a), ("B", review_b)):
        token_map = owner["token_maps"][slot]
        reviews[slot] = {
            token_map[row["sample_token"]]: row for row in submission["results"]
        }

    accepted: list[dict[str, Any]] = []
    screened: list[dict[str, Any]] = []
    for sample in owner["samples"]:
        sample_id = sample["sample_id"]
        first, second = reviews["A"][sample_id], reviews["B"][sample_id]
        pair_a = (first["materiality"], first["polarity"])
        pair_b = (second["materiality"], second["polarity"])
        if pair_a != pair_b:
            continue
        priority = _semantic_route(*pair_a)
        text = "\n".join(
            [
                str(sample["content"].get("headline") or ""),
                str(sample["content"].get("summary") or ""),
                *[
                    str(item.get("passage") or "")
                    for item in sample["content"].get("passages") or []
                    if isinstance(item, dict)
                ],
            ]
        )
        policy_label, policy_reason = _risk_first_policy(text)
        policy_priority = {
            "RISK_REVIEW": "PRIORITY_REVIEW",
            "NON_TARGET": "ROUTINE",
        }.get(str(policy_label or ""))
        if policy_priority and priority in {"PRIORITY_REVIEW", "ROUTINE"} and policy_priority != priority:
            screened.append(
                {
                    "sample_id": sample_id,
                    "semantic_pair": list(pair_a),
                    "semantic_priority": priority,
                    "policy_label": policy_label,
                    "policy_reason": policy_reason,
                }
            )
            continue
        accepted.append({"sample": sample, "review": first, "target": expected_semantic_payload(*pair_a)})

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in accepted:
        key = stable_json(row["target"])
        grouped.setdefault(key, []).append(row)
    splits: dict[str, list[dict[str, Any]]] = {"TRAIN": [], "VALIDATION": [], "OWNER_HOLDOUT": []}
    for rows in grouped.values():
        rows = sorted(rows, key=lambda row: _rank(row["sample"]["sample_id"]))
        holdout_count = max(1, round(len(rows) * 0.20))
        validation_count = max(1, round(len(rows) * 0.20))
        if len(rows) - holdout_count - validation_count < 2:
            raise ValueError("semantic class is too small for three-way split")
        splits["OWNER_HOLDOUT"].extend(rows[:holdout_count])
        splits["VALIDATION"].extend(rows[holdout_count : holdout_count + validation_count])
        splits["TRAIN"].extend(rows[holdout_count + validation_count :])

    prepared = {
        split: [_prepared_row(row["sample"], row["review"], split) for row in rows]
        for split, rows in splits.items()
    }
    balanced = _balanced_training_rows(prepared["TRAIN"])

    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "train": output_dir / "qwen_risk_sft_train.jsonl",
        "train_balanced": output_dir / "qwen_risk_sft_train_balanced.jsonl",
        "validation": output_dir / "qwen_risk_sft_validation.jsonl",
        "owner_holdout": output_dir / "qwen_risk_owner_holdout.jsonl",
        "screened": output_dir / "semantic_consensus_policy_screen.jsonl",
    }
    digests = {
        "train": _write_jsonl(paths["train"], prepared["TRAIN"]),
        "train_balanced": _write_jsonl(paths["train_balanced"], balanced),
        "validation": _write_jsonl(paths["validation"], prepared["VALIDATION"]),
        "owner_holdout": _write_jsonl(paths["owner_holdout"], prepared["OWNER_HOLDOUT"]),
        "screened": _write_jsonl(paths["screened"], screened),
    }
    def counts(rows: list[dict[str, Any]]) -> dict[str, int]:
        return dict(Counter(json.loads(row["messages"][-1]["content"])["adverse_strength"] for row in rows))

    def pair_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
        return dict(
            Counter(
                f"{target['materiality']}|{target['polarity']}"
                for row in rows
                for target in [json.loads(row["messages"][-1]["content"])]
            )
        )

    manifest = {
        "schema_version": 1,
        "contract_version": CONTRACT_VERSION,
        "semantic_contract_version": QWEN_RISK_CONTRACT_VERSION,
        "prompt_version": EXPERIMENT_PROMPT_VERSION,
        "base_model": "Qwen/Qwen2.5-1.5B-Instruct",
        "owner_manifest_sha256": owner["manifest_sha256"],
        "input_rows": len(owner["samples"]),
        "exact_semantic_pair_consensus_rows": len(accepted) + len(screened),
        "policy_screened_rows": len(screened),
        "train_rows": len(prepared["TRAIN"]),
        "train_balanced_rows": len(balanced),
        "validation_rows": len(prepared["VALIDATION"]),
        "owner_holdout_rows": len(prepared["OWNER_HOLDOUT"]),
        "adverse_strength_counts": {split: counts(rows) for split, rows in prepared.items()},
        "training_pair_counts_unique": pair_counts(prepared["TRAIN"]),
        "training_pair_counts_effective": pair_counts(balanced),
        "training_target_multipliers": {
            "|".join(pair): multiplier
            for pair, multiplier in TRAIN_TARGET_MULTIPLIERS.items()
        },
        "output_sha256": digests,
        "label_provenance": "DUAL_REVIEW_SEMANTIC_PAIR_CONSENSUS",
        "human_gold_claimed": False,
        "ai_conflict_labels_used": False,
        "evidence_state_used_as_model_target": False,
        "post_event_market_data_included": False,
        "production_model_changed": False,
        "no_trading": True,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-package", type=Path, required=True)
    parser.add_argument("--review-a", type=Path, required=True)
    parser.add_argument("--review-b-git-spec", required=True)
    parser.add_argument("--review-b-repository", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    review_b = _read_git_json(args.review_b_repository.resolve(), args.review_b_git_spec)
    manifest = prepare(
        owner_package=args.owner_package.resolve(),
        review_a=args.review_a.resolve(),
        review_b=review_b,
        output_dir=args.output_dir.resolve(),
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
