from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.build_external_blind_set import (
    _fed_non_enforcement,
    _nvidia_non_adverse,
    jaccard,
    parse_rss,
    shingles,
    write_freeze,
)
from scripts.evaluate_external_blind import load_and_verify
from app.models.risk_router import RiskRouter


def test_source_policy_filters_enforcement_and_adverse_company_news() -> None:
    assert not _fed_non_enforcement(
        {"title": "Board issues enforcement action", "canonical_url": "/enforcement20260716a.htm"}
    )
    assert _fed_non_enforcement(
        {"title": "Agencies issue joint statement on bank examinations", "canonical_url": "/bcreg20260716a.htm"}
    )
    assert not _nvidia_non_adverse({"title": "Company announces product recall", "summary": ""})
    assert _nvidia_non_adverse({"title": "NVIDIA launches new platform", "summary": ""})


def test_rss_repair_and_similarity_helpers() -> None:
    body = b"""<rss><channel><item><title>A & B launch platform</title><link>https://example.test/1</link><pubDate>Fri, 17 Jul 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
    rows, repaired = parse_rss(body)
    assert repaired is True
    assert rows[0]["title"] == "A & B launch platform"
    assert jaccard(shingles("alpha beta gamma delta"), shingles("alpha beta gamma omega")) > 0


def test_freeze_is_hash_bound_and_rejects_prediction_mutation(tmp_path: Path) -> None:
    dataset = tmp_path / "blind.jsonl"
    freeze_path = tmp_path / "freeze.json"
    samples = [
        {
            "sample_id": "EXT-1",
            "source_id": "official",
            "expected_label": "NON_TARGET",
            "prediction": None,
            "overlap_evidence": {
                "title_substring_overlap": False,
                "max_training_shingle_jaccard": 0.1,
            },
        }
    ]
    metadata = {
        "model_card": {
            "model_version": "model-v1",
            "trained_at": "2026-07-18T10:00:00+00:00",
            "artifact_sha256": "abc",
        },
        "training_rows": 10,
        "training_dataset_sha256": "def",
        "raw_sources": [],
        "fetched_at": "2026-07-18T12:00:00+00:00",
    }
    freeze = write_freeze(samples, metadata, dataset_path=dataset, freeze_path=freeze_path)
    assert hashlib.sha256(dataset.read_bytes()).hexdigest() == freeze["dataset_sha256"]
    rows, loaded = load_and_verify(dataset, freeze_path)
    assert rows[0]["prediction"] is None
    dataset.write_text(json.dumps({**samples[0], "prediction": "NON_TARGET"}) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="hash"):
        load_and_verify(dataset, freeze_path)


def test_router_status_loads_compact_external_blind_governance_report(tmp_path: Path) -> None:
    artifact = tmp_path / "risk_router.joblib"
    report_path = tmp_path / "risk_router_external_blind_v1_report.json"
    report_path.write_text(
        json.dumps(
            {
                "evaluation_type": "true_external_blind_label_first",
                "freeze_id": "external-blind-v1-abc",
                "rows": 40,
                "metrics": {"risk_recall": 1.0, "non_target_false_risk_rate": 0.95},
                "gates": {"covered_accuracy": False},
                "gate_pass": False,
                "promotion_decision": "REMAIN_SHADOW",
                "predictions": [{"text": "must not leak through status"}],
                "failures": [{"text": "must not leak through status"}],
                "no_trading": True,
            }
        ),
        encoding="utf-8",
    )
    status = RiskRouter(artifact).status()
    blind = status["external_blind"]
    assert blind["freeze_id"] == "external-blind-v1-abc"
    assert blind["gate_pass"] is False
    assert blind["promotion_decision"] == "REMAIN_SHADOW"
    assert "predictions" not in blind
    assert "failures" not in blind
