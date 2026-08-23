from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.train_risk_router_human_gold import train


def development(path: Path, *, include_blind: bool = False) -> Path:
    rows = []
    for index in range(16):
        label = "RISK_REVIEW" if index % 2 == 0 else "NON_TARGET"
        text = (
            f"Issuer {index} filed for bankruptcy and old common equity may be cancelled."
            if label == "RISK_REVIEW"
            else f"Issuer {index} announced a routine annual meeting and unchanged policy."
        )
        rows.append(
            {
                "sample_id": f"sample-{index}",
                "event_id": f"event-{index}",
                "text": text,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "entity_group": f"issuer-{index}",
                "event_chain_group": f"chain-{index}",
                "split": "HUMAN_BLIND" if include_blind and index == 0 else ("TRAIN" if index < 12 else "VALIDATION"),
                "label": label,
                "axes": {},
                "label_provenance": "INDEPENDENT_DUAL_HUMAN_OR_ARBITRATED",
                "post_event_market_data_included": False,
                "model_output_included_in_review": False,
            }
        )
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def test_human_gold_trainer_builds_shadow_candidate_without_blind_labels(tmp_path: Path) -> None:
    report = train(
        development(tmp_path / "dev.jsonl"),
        tmp_path / "candidate.joblib",
        tmp_path / "report.json",
        tmp_path / "card.json",
        minimum_rows=16,
    )
    assert report["label_provenance"] == "INDEPENDENT_DUAL_HUMAN_OR_ARBITRATED"
    assert report["human_blind_labels_read"] is False
    assert report["production_model_changed"] is False
    assert (tmp_path / "candidate.joblib").is_file()


def test_human_gold_trainer_rejects_blind_split(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="HUMAN_BLIND"):
        train(
            development(tmp_path / "dev.jsonl", include_blind=True),
            tmp_path / "candidate.joblib",
            tmp_path / "report.json",
            tmp_path / "card.json",
            minimum_rows=16,
        )
