#!/usr/bin/env python3
"""Train the CPU-only Finance Radar downside-risk review router."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix
from sklearn.pipeline import FeatureUnion, Pipeline
from sklearn.linear_model import LogisticRegression


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402


LABEL_MAP = {"verified": "RISK_REVIEW", "rejected": "NON_TARGET"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_dataset(
    db_path: Path,
) -> tuple[list[str], list[str], list[str], list[dict[str, str]], dict[str, Any]]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    events = [
        dict(row)
        for row in connection.execute(
            """SELECT e.event_id,e.status,e.event_date,e.stable_id,e.company_name,
                      e.ticker_at_event,e.event_family,e.event_type,e.discovery_source,v.facts_json,
                      (SELECT chain_id FROM event_chain_members cm WHERE cm.event_id=e.event_id LIMIT 1) AS chain_id
               FROM canonical_events e
               LEFT JOIN event_versions v ON v.event_id=e.event_id AND v.version=e.current_version
               WHERE e.status IN ('verified','rejected')
               ORDER BY e.event_id"""
        )
    ]
    observation_text: dict[str, list[str]] = defaultdict(list)
    for row in connection.execute(
        """SELECT eo.event_id,o.title,o.summary
           FROM event_observations eo JOIN raw_observations o ON o.observation_id=eo.observation_id"""
    ):
        observation_text[row["event_id"]].append(f"{row['title']} {row['summary']}")
    evidence_text: dict[str, list[str]] = defaultdict(list)
    for row in connection.execute(
        "SELECT event_id,evidence_passage,form,items FROM event_evidence WHERE evidence_passage IS NOT NULL"
    ):
        evidence_text[row["event_id"]].append(
            f"{row['form'] or ''} {row['items'] or ''} {row['evidence_passage'] or ''}"
        )
    connection.close()

    ids: list[str] = []
    texts: list[str] = []
    labels: list[str] = []
    split_records: list[dict[str, str]] = []
    family_counts: Counter[str] = Counter()
    for event in events:
        try:
            facts = json.loads(event.pop("facts_json") or "{}")
        except json.JSONDecodeError:
            facts = {}
        # Deliberately exclude label_status, grades, training_role and all post-event market outcomes.
        parts = [
            event.get("company_name") or "",
            event.get("ticker_at_event") or "",
            event.get("event_family") or "",
            event.get("event_type") or "",
            event.get("discovery_source") or "",
            facts.get("evidence_summary") or "",
            *observation_text.get(event["event_id"], []),
            *evidence_text.get(event["event_id"], []),
        ]
        text = " ".join(" ".join(parts).split())[:30000]
        if len(text) < 12:
            continue
        ids.append(event["event_id"])
        texts.append(text)
        labels.append(LABEL_MAP[event["status"]])
        issuer_key = (
            event.get("stable_id")
            or event.get("ticker_at_event")
            or event.get("company_name")
            or event["event_id"]
        )
        split_records.append(
            {
                "event_date": str(event.get("event_date") or "0000-00-00"),
                "issuer_key": str(issuer_key).strip().lower(),
                "chain_id": str(event.get("chain_id") or "").strip().lower(),
            }
        )
        family_counts[event.get("event_family") or "unknown"] += 1
    metadata = {
        "rows": len(texts),
        "label_counts": dict(Counter(labels)),
        "top_event_families": dict(family_counts.most_common(20)),
    }
    return ids, texts, labels, split_records, metadata


def time_issuer_chain_split(
    labels: list[str],
    records: list[dict[str, str]],
    *,
    test_fraction: float = 0.25,
) -> tuple[list[int], list[int], dict[str, Any]]:
    """Deterministic recent-group holdout with zero issuer or event-chain overlap."""
    if len(labels) != len(records):
        raise ValueError("labels and split records must have equal length")

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

    identifiers: list[list[str]] = []
    for index, record in enumerate(records):
        keys = [f"issuer:{record['issuer_key']}"]
        if record.get("chain_id"):
            keys.append(f"chain:{record['chain_id']}")
        if not keys:
            keys = [f"event:{index}"]
        identifiers.append(keys)
        for key in keys[1:]:
            union(keys[0], key)

    groups: dict[str, list[int]] = defaultdict(list)
    for index, keys in enumerate(identifiers):
        groups[find(keys[0])].append(index)
    ordered_groups = sorted(
        groups.values(),
        key=lambda indices: max(records[index]["event_date"] for index in indices),
        reverse=True,
    )
    target_test_rows = max(1, round(len(labels) * test_fraction))
    total_counts = Counter(labels)
    test_indices: list[int] = []
    test_counts: Counter[str] = Counter()
    deferred: list[list[int]] = []
    for indices in ordered_groups:
        group_counts = Counter(labels[index] for index in indices)
        remaining = total_counts - (test_counts + group_counts)
        if any(remaining[label] < 10 for label in total_counts):
            deferred.append(indices)
            continue
        if len(test_indices) < target_test_rows or any(test_counts[label] == 0 for label in total_counts):
            test_indices.extend(indices)
            test_counts.update(group_counts)
        else:
            deferred.append(indices)

    for missing_label in [label for label in total_counts if test_counts[label] == 0]:
        candidate = next(
            (
                indices
                for indices in deferred
                if any(labels[index] == missing_label for index in indices)
                and all(
                    total_counts[label]
                    - test_counts[label]
                    - Counter(labels[index] for index in indices)[label]
                    >= 10
                    for label in total_counts
                )
            ),
            None,
        )
        if candidate is not None:
            test_indices.extend(candidate)
            test_counts.update(labels[index] for index in candidate)
            deferred.remove(candidate)

    test_set = set(test_indices)
    train_indices = [index for index in range(len(labels)) if index not in test_set]
    if len(set(labels[index] for index in train_indices)) != 2 or len(set(labels[index] for index in test_indices)) != 2:
        raise RuntimeError("grouped temporal split could not preserve both labels")
    train_issuers = {records[index]["issuer_key"] for index in train_indices}
    test_issuers = {records[index]["issuer_key"] for index in test_indices}
    train_chains = {records[index]["chain_id"] for index in train_indices if records[index]["chain_id"]}
    test_chains = {records[index]["chain_id"] for index in test_indices if records[index]["chain_id"]}
    if train_issuers & test_issuers or train_chains & test_chains:
        raise RuntimeError("issuer or event-chain leakage detected after split")
    audit = {
        "strategy": "deterministic recent connected-group holdout",
        "grouping": "connected components of issuer_key and event_chain_id",
        "target_test_fraction": test_fraction,
        "actual_test_fraction": len(test_indices) / len(labels),
        "train_rows": len(train_indices),
        "test_rows": len(test_indices),
        "train_label_counts": dict(Counter(labels[index] for index in train_indices)),
        "test_label_counts": dict(Counter(labels[index] for index in test_indices)),
        "train_date_range": [
            min(records[index]["event_date"] for index in train_indices),
            max(records[index]["event_date"] for index in train_indices),
        ],
        "test_date_range": [
            min(records[index]["event_date"] for index in test_indices),
            max(records[index]["event_date"] for index in test_indices),
        ],
        "issuer_overlap_count": len(train_issuers & test_issuers),
        "event_chain_overlap_count": len(train_chains & test_chains),
    }
    return train_indices, sorted(test_indices), audit


def build_pipeline(feature_mode: str = "combined") -> Pipeline:
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
    if feature_mode == "combined":
        features = FeatureUnion([("word_tfidf", word), ("char_tfidf", char)])
    elif feature_mode == "word_only":
        features = word
    elif feature_mode == "char_only":
        features = char
    else:
        raise ValueError(f"unsupported feature mode: {feature_mode}")
    classifier = CalibratedClassifierCV(
        estimator=LogisticRegression(
            C=2.0,
            class_weight="balanced",
            max_iter=1500,
            random_state=42,
        ),
        method="sigmoid",
        cv=3,
    )
    return Pipeline([("features", features), ("classifier", classifier)])


def train(
    db_path: Path,
    artifact_path: Path,
    model_card_path: Path,
    *,
    abstain_threshold: float = 0.62,
) -> dict[str, Any]:
    ids, texts, labels, split_records, dataset = load_dataset(db_path)
    if len(set(labels)) != 2 or min(Counter(labels).values()) < 10:
        raise RuntimeError(f"insufficient labeled data: {Counter(labels)}")
    train_indices, test_indices, split_audit = time_issuer_chain_split(labels, split_records)
    train_ids = [ids[index] for index in train_indices]
    test_ids = [ids[index] for index in test_indices]
    x_train = [texts[index] for index in train_indices]
    x_test = [texts[index] for index in test_indices]
    y_train = [labels[index] for index in train_indices]
    y_test = [labels[index] for index in test_indices]
    pipeline = build_pipeline()
    pipeline.fit(x_train, y_train)
    probabilities = pipeline.predict_proba(x_test)
    classes = [str(item) for item in pipeline.classes_]
    predictions: list[str] = []
    covered_truth: list[str] = []
    covered_predictions: list[str] = []
    confidence_values: list[float] = []
    for truth, row in zip(y_test, probabilities):
        index = int(row.argmax())
        confidence = float(row[index])
        confidence_values.append(confidence)
        prediction = classes[index] if confidence >= abstain_threshold else "ABSTAIN"
        predictions.append(prediction)
        if prediction != "ABSTAIN":
            covered_truth.append(truth)
            covered_predictions.append(prediction)

    dataset_fingerprint = hashlib.sha256(
        stable_json(
            [
                {"event_id": event_id, "label": label, "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
                for event_id, label, text in zip(ids, labels, texts)
            ]
        ).encode("utf-8")
    ).hexdigest()
    model_version = f"risk-router-v1-{dataset_fingerprint[:12]}"
    bundle = {
        "pipeline": pipeline,
        "model_version": model_version,
        "abstain_threshold": abstain_threshold,
        "trained_at": utc_now(),
        "dataset_sha256": dataset_fingerprint,
        "classes": classes,
        "no_trading": True,
        "shadow": True,
    }
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, artifact_path, compress=3)
    artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    covered_accuracy = (
        accuracy_score(covered_truth, covered_predictions) if covered_predictions else None
    )
    card = {
        "schema_version": 1,
        "model_name": "Finance Radar Downside-Risk Review Router",
        "model_version": model_version,
        "artifact_sha256": artifact_sha256,
        "trained_at": bundle["trained_at"],
        "task": "Route full-polarity financial event text to RISK_REVIEW, NON_TARGET or ABSTAIN.",
        "intended_use": "Shadow-mode prioritization of potentially material downside-risk events for human evidence review.",
        "explicit_non_uses": [
            "trading or order execution",
            "return prediction",
            "long/short recommendations",
            "automatic fact verification",
            "automatic rejection of positive news",
        ],
        "polarity_policy": {
            "ingestion": "full_polarity",
            "specialization": "downside_risk_priority",
            "positive_news": "normally NON_TARGET or ABSTAIN; retained in the ledger and never reinterpreted as bearish",
        },
        "dataset": {
            **dataset,
            "dataset_sha256": dataset_fingerprint,
            "label_definition": {
                "RISK_REVIEW": "currently verified historical downside-risk event",
                "NON_TARGET": "adjudicated rejected candidate or control",
            },
            "train_rows": len(x_train),
            "test_rows": len(x_test),
            "split": split_audit,
            "excluded_fields": [
                "event status and label status",
                "manual grade and training role",
                "post-event prices, returns and event_market_metrics",
            ],
        },
        "features": ["word TF-IDF 1-2 grams", "character TF-IDF 3-5 grams"],
        "estimator": "sigmoid-calibrated class-balanced logistic regression",
        "metrics": {
            "abstain_threshold": abstain_threshold,
            "coverage": len(covered_predictions) / len(y_test),
            "abstain_rate": predictions.count("ABSTAIN") / len(predictions),
            "covered_accuracy": covered_accuracy,
            "raw_argmax_accuracy": accuracy_score(y_test, [classes[int(row.argmax())] for row in probabilities]),
            "classification_report_covered": classification_report(
                covered_truth,
                covered_predictions,
                labels=classes,
                output_dict=True,
                zero_division=0,
            ) if covered_predictions else {},
            "confusion_matrix_covered": confusion_matrix(
                covered_truth, covered_predictions, labels=classes
            ).tolist() if covered_predictions else [],
            "mean_confidence": sum(confidence_values) / len(confidence_values),
        },
        "limitations": [
            "The dataset is intentionally rich in negative events and controls, so this is not a general financial-sentiment model.",
            "Historical labels reflect the current evidence policy and can contain adjudication noise.",
            "Issuer, source and event-family language may shift over time; drift monitoring is required.",
            "The model is only a queueing aid; evidence and finality gates remain authoritative.",
        ],
        "test_event_ids_sha256": hashlib.sha256(stable_json(sorted(test_ids)).encode("utf-8")).hexdigest(),
        "no_trading": True,
        "shadow": True,
    }
    model_card_path.parent.mkdir(parents=True, exist_ok=True)
    model_card_path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    artifact_path.with_suffix(".sha256").write_text(f"{artifact_sha256}  {artifact_path.name}\n", encoding="ascii")
    (artifact_path.parent / "risk_router_feature_schema.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "input": "UTF-8 event and exact-evidence text",
                "features": card["features"],
                "excluded": card["dataset"]["excluded_fields"],
                "output": ["RISK_REVIEW", "NON_TARGET", "ABSTAIN"],
                "post_event_market_features_allowed": False,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (artifact_path.parent / "risk_router_metrics.json").write_text(
        json.dumps(card["metrics"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (artifact_path.parent / "risk_router_data_card.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset": card["dataset"],
                "training_contract": {
                    "training_eligible": "boolean",
                    "exclusion_reason": "nullable enum",
                    "label_task": "risk_routing",
                    "label_source": "imported adjudication",
                    "label_version": "risk-routing-v1",
                },
                "limitations": card["limitations"],
                "no_trading": True,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    training_manifest = artifact_path.parent / "risk_router_training_manifest.jsonl"
    test_index_set = set(test_indices)
    training_manifest.write_text(
        "\n".join(
            stable_json(
                {
                    "event_id": event_id,
                    "training_eligible": True,
                    "exclusion_reason": None,
                    "label_task": "risk_routing",
                    "label_source": "imported_adjudication",
                    "label_version": "risk-routing-v1",
                    "label": labels[index],
                    "split": "test" if index in test_index_set else "train",
                    "event_date": split_records[index]["event_date"],
                    "issuer_group_sha256": hashlib.sha256(
                        split_records[index]["issuer_key"].encode("utf-8")
                    ).hexdigest(),
                    "event_chain_group_sha256": (
                        hashlib.sha256(split_records[index]["chain_id"].encode("utf-8")).hexdigest()
                        if split_records[index]["chain_id"]
                        else None
                    ),
                }
            )
            for index, event_id in enumerate(ids)
        )
        + "\n",
        encoding="utf-8",
    )
    (artifact_path.parent / "risk_router_model_card.md").write_text(
        "\n".join(
            [
                "# Finance Radar Risk Router Model Card",
                "",
                f"- Model version: `{model_version}`",
                f"- Artifact SHA-256: `{artifact_sha256}`",
                "- Mode: shadow only; no trading or alert permission",
                f"- Task: {card['task']}",
                f"- Split: {split_audit['strategy']}",
                f"- Issuer overlap: {split_audit['issuer_overlap_count']}",
                f"- Event-chain overlap: {split_audit['event_chain_overlap_count']}",
                f"- Coverage: {card['metrics']['coverage']:.3f}",
                f"- Covered accuracy: {card['metrics']['covered_accuracy']:.3f}",
                "",
                "## Limitations",
                "",
                *[f"- {item}" for item in card["limitations"]],
                "",
            ]
        ),
        encoding="utf-8",
    )
    (artifact_path.parent / "risk_router_data_card.md").write_text(
        "\n".join(
            [
                "# Finance Radar Risk Router Data Card",
                "",
                f"- Rows: {dataset['rows']}",
                f"- Labels: `{stable_json(dataset['label_counts'])}`",
                "- Structured manifest: `risk_router_training_manifest.jsonl`",
                "- Label task: adverse material risk routing",
                "- Label source: imported adjudication",
                "- Group split: time-prioritized connected issuer/event-chain groups",
                f"- Train/test rows: {len(train_indices)}/{len(test_indices)}",
                "- Post-event market features: prohibited",
                "- Favorable and neutral controls remain non-target examples; the corpus is not adverse-only.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return card


def main() -> int:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=settings.ledger_db)
    parser.add_argument("--artifact", type=Path, default=settings.model_artifact)
    parser.add_argument("--model-card", type=Path, default=settings.model_card)
    parser.add_argument("--abstain-threshold", type=float, default=0.62)
    args = parser.parse_args()
    card = train(args.db, args.artifact, args.model_card, abstain_threshold=args.abstain_threshold)
    print(
        json.dumps(
            {
                "model_version": card["model_version"],
                "artifact_sha256": card["artifact_sha256"],
                "dataset": card["dataset"],
                "metrics": card["metrics"],
                "artifact": str(args.artifact),
                "model_card": str(args.model_card),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
