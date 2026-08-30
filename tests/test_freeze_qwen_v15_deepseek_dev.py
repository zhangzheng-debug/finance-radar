from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.freeze_qwen_v15_deepseek_dev import freeze_dev, stable_json


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(stable_json(row) + "\n" for row in rows), encoding="utf-8")


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    provider = tmp_path / "provider.jsonl"
    index = tmp_path / "index.jsonl"
    progress = tmp_path / "progress.jsonl"
    exposure = tmp_path / "exposure.jsonl"
    providers, indexes, results = [], [], []
    for number in range(4):
        sample = f"sample-{number}"
        content = {
            "as_of": "2026-08-29T00:00:00Z",
            "event_date": "2026-08-29",
            "headline": f"Event {number}",
            "summary": "A contemporaneous source statement.",
            "passages": [],
        }
        digest = hashlib.sha256(stable_json(content).encode()).hexdigest()
        providers.append({"sample_id": sample, "content": content})
        indexes.append({
            "sample_id": sample,
            "source_event_id": f"event-{number}",
            "entity_group": f"issuer-{number}",
            "entity_group_quality": "TICKER",
            "event_chain_group": f"chain-{number}",
            "provider_text_sha256": digest,
        })
        results.append({
            "sample_id": sample,
            "status": "completed",
            "result": {
                "sample_id": sample,
                "input_sha256": digest,
                "model": "deepseek-test",
                "first_pass_pair_agreed": number % 2 == 0,
                "final": {
                    "materiality": "MATERIAL_ADVERSE" if number == 3 else "NOT_MATERIAL_ADVERSE",
                    "polarity": "ADVERSE" if number == 3 else "NEUTRAL",
                },
            },
        })
    _write(provider, providers)
    _write(index, indexes)
    _write(progress, results)
    _write(exposure, [{
        "sample_id": "old",
        "entity_group": "issuer-0",
        "event_chain_group": "old-chain",
        "content_sha256": "f" * 64,
    }])
    return provider, index, progress, exposure


def test_freezes_only_fresh_members_before_copying_targets(tmp_path: Path) -> None:
    provider, index, progress, exposure = _fixture(tmp_path)
    out = tmp_path / "out"
    manifest = freeze_dev(
        provider_input=provider,
        source_index=index,
        progress=progress,
        exposure_indexes=[exposure],
        output_dir=out,
        minimum_rows=3,
    )
    rows = [json.loads(line) for line in (out / "qwen_core_v15_fresh_deepseek_dev.jsonl").read_text(encoding="utf-8").splitlines()]
    assert manifest["row_count"] == len(rows) == 3
    assert {row["metadata"]["sample_id"] for row in rows} == {"sample-1", "sample-2", "sample-3"}
    assert manifest["exclusions"] == {"PRIOR_ENTITY_GROUP_OVERLAP": 1}
    assert manifest["isolation"]["strict_test_provider_payload_read"] is False
    assert all(row["metadata"]["label_classification"] == "AI_REVIEW_NOT_HUMAN_GOLD" for row in rows)
    assert json.loads(rows[-1]["messages"][-1]["content"])["materiality"] == "MATERIAL_ADVERSE"


def test_rejects_progress_content_binding_mismatch(tmp_path: Path) -> None:
    provider, index, progress, exposure = _fixture(tmp_path)
    rows = [json.loads(line) for line in progress.read_text(encoding="utf-8").splitlines()]
    rows[0]["result"]["input_sha256"] = "0" * 64
    _write(progress, rows)
    try:
        freeze_dev(
            provider_input=provider,
            source_index=index,
            progress=progress,
            exposure_indexes=[exposure],
            output_dir=tmp_path / "out",
            minimum_rows=3,
        )
    except ValueError as exc:
        assert "progress binding mismatch" in str(exc)
    else:
        raise AssertionError("expected binding failure")
