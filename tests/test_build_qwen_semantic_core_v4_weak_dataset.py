from __future__ import annotations

import json
from pathlib import Path

from app.models.qwen_risk_contract import expected_semantic_payload
from scripts.build_qwen_semantic_core_v4_weak_dataset import build_dataset, stable_json


def _row(sample: str, event: str, entity: str, chain: str, materiality: str, polarity: str) -> dict:
    content = {
        "as_of": "2026-01-01T00:00:00Z",
        "event_date": "2026-01-01",
        "headline": f"headline {sample}",
        "summary": f"summary {sample}",
        "passages": [],
    }
    target = expected_semantic_payload(materiality, polarity)
    return {
        "messages": [
            {"role": "system", "content": "old"},
            {"role": "user", "content": stable_json(content)},
            {"role": "assistant", "content": stable_json(target)},
        ],
        "metadata": {
            "sample_id": sample, "event_id": event, "entity_group": entity,
            "event_chain_group": chain,
        },
    }


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(stable_json(row) + "\n" for row in rows), encoding="utf-8")


def test_builder_excludes_strict_entities_and_conflicting_duplicates(tmp_path: Path) -> None:
    dual = tmp_path / "dual.jsonl"
    ai = tmp_path / "ai.jsonl"
    weak = tmp_path / "weak.jsonl"
    strict = tmp_path / "strict.jsonl"
    _write(dual, [
        _row("s1", "e1", "issuer:clean", "chain:1", "NOT_MATERIAL_ADVERSE", "NEUTRAL"),
        _row("s2", "e2", "issuer:sealed", "chain:2", "MATERIAL_ADVERSE", "ADVERSE"),
        _row("s3", "e3", "issuer:conflict", "chain:3", "MATERIAL_ADVERSE", "ADVERSE"),
    ])
    _write(ai, [_row("s3", "e3", "issuer:conflict", "chain:3", "NOT_MATERIAL_ADVERSE", "NEUTRAL")])
    _write(weak, [_row("s4", "e4", "issuer:clean2", "chain:4", "NOT_MATERIAL_ADVERSE", "POSITIVE")])
    _write(strict, [{"sample_id": "other", "source_event_id": "other", "entity_group": "issuer:sealed", "event_chain_group": "sealed-chain"}])
    output = tmp_path / "output"
    manifest = build_dataset(
        dual_consensus=[dual], ai_assisted=[ai], deterministic_weak=[weak],
        strict_indices=[strict], output_dir=output,
    )
    assert manifest["candidate_rows"] == 5
    assert manifest["leakage_excluded_rows"] == 1
    assert manifest["conflict_excluded_rows"] == 2
    assert manifest["unique_rows"] == 2
    all_rows = (output / "qwen_core_v4_train_unique.jsonl").read_text() + (output / "qwen_core_v4_dev.jsonl").read_text()
    assert "issuer:sealed" not in all_rows
    assert "issuer:conflict" not in all_rows
    assert "WEAK_SUPERVISION_NOT_HUMAN_GOLD" in all_rows
    prepared = [
        json.loads(line)
        for path in (
            output / "qwen_core_v4_train_unique.jsonl",
            output / "qwen_core_v4_dev.jsonl",
        )
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert all(row["metadata"]["target_contract"] == "core-v1" for row in prepared)
    assert manifest["target_contract"] == "core-v1"


def test_component_split_keeps_same_entity_together(tmp_path: Path) -> None:
    dual = tmp_path / "dual.jsonl"
    strict = tmp_path / "strict.jsonl"
    _write(dual, [
        _row("s1", "e1", "issuer:same", "chain:1", "NOT_MATERIAL_ADVERSE", "NEUTRAL"),
        _row("s2", "e2", "issuer:same", "chain:2", "NOT_MATERIAL_ADVERSE", "POSITIVE"),
        _row("s3", "e3", "issuer:other", "chain:3", "MATERIAL_ADVERSE", "ADVERSE"),
    ])
    _write(strict, [])
    output = tmp_path / "output"
    build_dataset(
        dual_consensus=[dual], ai_assisted=[], deterministic_weak=[],
        strict_indices=[strict], output_dir=output,
    )
    locations: dict[str, str] = {}
    for split, name in (("TRAIN", "qwen_core_v4_train_unique.jsonl"), ("DEV", "qwen_core_v4_dev.jsonl")):
        for line in (output / name).read_text().splitlines():
            row = json.loads(line)
            if row["metadata"]["entity_group"] == "issuer:same":
                locations[row["metadata"]["sample_id"]] = split
    assert locations["s1"] == locations["s2"]
    assert (output / "manifest.json.sha256").exists()


def test_explicit_canonical_mapping_exclusion_is_audited(tmp_path: Path) -> None:
    dual = tmp_path / "dual.jsonl"
    strict = tmp_path / "strict.jsonl"
    exclusion = tmp_path / "exclusion.json"
    _write(dual, [
        _row("weak-1", "event-1", "legacy-name", "legacy-chain", "MATERIAL_ADVERSE", "ADVERSE"),
        _row("keep", "event-2", "issuer:other", "chain:2", "NOT_MATERIAL_ADVERSE", "NEUTRAL"),
    ])
    _write(strict, [])
    exclusion.write_text(stable_json([{"hardcase_sample_id": "weak-1", "event_id": "event-1"}]), encoding="utf-8")
    output = tmp_path / "output"
    manifest = build_dataset(
        dual_consensus=[dual], ai_assisted=[], deterministic_weak=[], strict_indices=[strict],
        output_dir=output, explicit_exclusions=[exclusion],
    )
    assert manifest["leakage_excluded_rows"] == 1
    assert manifest["explicit_exclusion_counts"] == {"sample_id": 1, "event_id": 1}
    audit = (output / "leakage_exclusions.jsonl").read_text()
    assert "explicit_sample_id" in audit and "explicit_event_id" in audit


def test_quality_exclusion_is_separate_from_leakage(tmp_path: Path) -> None:
    dual = tmp_path / "dual.jsonl"
    strict = tmp_path / "strict.jsonl"
    quality = tmp_path / "quality.json"
    _write(dual, [
        _row("bad-label", "event-1", "issuer:1", "chain:1", "MATERIAL_ADVERSE", "ADVERSE"),
        _row("keep", "event-2", "issuer:2", "chain:2", "NOT_MATERIAL_ADVERSE", "NEUTRAL"),
    ])
    _write(strict, [])
    quality.write_text(stable_json([{"sample_id": "bad-label"}]), encoding="utf-8")
    output = tmp_path / "output"
    manifest = build_dataset(
        dual_consensus=[dual], ai_assisted=[], deterministic_weak=[], strict_indices=[strict],
        output_dir=output, quality_exclusions=[quality],
    )
    assert manifest["leakage_excluded_rows"] == 0
    assert manifest["quality_excluded_rows"] == 1
    assert "rationale_source_sanity_sample_id" in (output / "quality_exclusions.jsonl").read_text()


def test_legacy_hardcase_is_rejoined_to_canonical_entity_before_split(tmp_path: Path) -> None:
    weak = tmp_path / "weak.jsonl"
    strict = tmp_path / "strict.jsonl"
    source_map = tmp_path / "source-map.jsonl"
    pool = tmp_path / "pool.jsonl"
    _write(weak, [_row("weak-1", "event-1", "LEGACY NAME", "legacy", "MATERIAL_ADVERSE", "ADVERSE")])
    _write(strict, [])
    _write(source_map, [{"event_id": "event-1", "sample_id": "canonical-1"}])
    _write(pool, [{"sample_id": "canonical-1", "entity_group": "issuer:hash", "event_chain_group": "chain:hash"}])
    output = tmp_path / "output"
    manifest = build_dataset(
        dual_consensus=[], ai_assisted=[], deterministic_weak=[weak], strict_indices=[strict],
        output_dir=output, canonical_source_map=source_map, canonical_pool=pool,
    )
    assert manifest["canonical_rejoined_rows"] == 1
    all_rows = (output / "qwen_core_v4_train_unique.jsonl").read_text() + (output / "qwen_core_v4_dev.jsonl").read_text()
    assert "issuer:hash" in all_rows
    assert "LEGACY NAME" not in all_rows


def test_provisional_canonical_issuer_is_excluded_from_training(tmp_path: Path) -> None:
    dual = tmp_path / "dual.jsonl"
    strict = tmp_path / "strict.jsonl"
    issuer_map = tmp_path / "issuer-map.jsonl"
    _write(dual, [
        _row("provisional", "event-p", "issuer:p", "chain:p", "NOT_MATERIAL_ADVERSE", "NEUTRAL"),
        _row("strong", "event-s", "issuer:s", "chain:s", "MATERIAL_ADVERSE", "ADVERSE"),
    ])
    _write(strict, [])
    _write(issuer_map, [
        {
            "sample_id": "provisional", "event_id": "event-p",
            "canonical_issuer_key": "issuer:v1:raw_ticker:TEST",
            "resolution_quality": "PROVISIONAL_RAW_TICKER",
        },
        {
            "sample_id": "strong", "event_id": "event-s",
            "canonical_issuer_key": "issuer:v1:sec_cik:0000000001",
            "resolution_quality": "STRONG_CIK",
        },
    ])
    output = tmp_path / "output"
    manifest = build_dataset(
        dual_consensus=[dual], ai_assisted=[], deterministic_weak=[],
        strict_indices=[strict], canonical_issuer_map=issuer_map, output_dir=output,
    )
    assert manifest["canonical_provisional_excluded_rows"] == 1
    audit = (output / "leakage_exclusions.jsonl").read_text(encoding="utf-8")
    assert "canonical_issuer_not_strong" in audit
    outputs = (
        (output / "qwen_core_v4_train_unique.jsonl").read_text(encoding="utf-8")
        + (output / "qwen_core_v4_dev.jsonl").read_text(encoding="utf-8")
    )
    assert '"sample_id":"provisional"' not in outputs
    assert '"sample_id":"strong"' in outputs
