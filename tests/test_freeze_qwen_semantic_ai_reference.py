from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models.qwen_risk_contract import normalize_qwen_risk_content
from app.services.human_gold_review import build_offline_batch, stable_json
from scripts.freeze_qwen_semantic_ai_reference import (
    DATASET_NAME,
    REFERENCE_STATUS,
    freeze,
)


def _sample(index: int) -> dict:
    return {
        "sample_id": f"sample-{index:04d}",
        "event_id": f"event-{index:04d}",
        "text_sha256": hashlib.sha256(f"text-{index}".encode()).hexdigest(),
        "content": {
            "contract_version": "human-blind-v3.1",
            "as_of": "2026-08-01T00:00:00+00:00",
            "event_date": "2026-08-01",
            "headline": f"Issuer disclosure {index}",
            "summary": f"Frozen semantic source text {index}.",
            "confirmed_facts": [],
            "passages": [
                {
                    "authority_class": "PRIMARY_OFFICIAL",
                    "document_type": "8-K",
                    "item_section": "8.01",
                    "published_at": "2026-08-01",
                    "passage": f"The issuer disclosed independently reviewable event {index}.",
                }
            ],
            "source_identity_hidden": True,
            "target_label_hidden": True,
            "post_event_market_data_included": False,
            "model_output_included": False,
        },
        "source_id": f"source-{index}",
        "authority_tier": "P0",
        "entity_group": f"issuer:{index}",
        "event_chain_group": f"chain:{index}",
    }


def _pair_a(sample_id: str) -> tuple[str, str]:
    index = int(sample_id.rsplit("-", 1)[1])
    return (
        ("MATERIAL_ADVERSE", "ADVERSE")
        if index % 3 == 0
        else ("NOT_MATERIAL_ADVERSE", "NEUTRAL")
    )


def _pair_b(sample_id: str, consensus_ids: set[str]) -> tuple[str, str]:
    if sample_id in consensus_ids:
        return _pair_a(sample_id)
    return "NOT_MATERIAL_ADVERSE", "POSITIVE"


def _submission(
    assignment: dict,
    token_map: dict[str, str],
    *,
    consensus_ids: set[str],
) -> dict:
    rows = []
    for event in assignment["events"]:
        sample_id = token_map[event["sample_token"]]
        pair = (
            _pair_a(sample_id)
            if assignment["reviewer_slot"] == "A"
            else _pair_b(sample_id, consensus_ids)
        )
        rows.append(
            {
                "sample_token": event["sample_token"],
                "materiality": pair[0],
                "polarity": pair[1],
                "evidence_state": "PRIMARY_SUPPORTED",
                "rationale": "Independent human review of the exact frozen source passage supports this label.",
                "reviewed_at": "2026-08-29T00:00:00+00:00",
                "duration_seconds": 30,
            }
        )
    return {
        "schema_version": 1,
        "contract_version": assignment["contract_version"],
        "batch_id": assignment["batch_id"],
        "reviewer_slot": assignment["reviewer_slot"],
        "review_role": assignment["review_role"],
        "reviewer_token": assignment["reviewer_token"],
        "assignment_sha256": assignment["assignment_sha256"],
        "attestations": {
            "human_only": True,
            "independent_judgment": True,
            "no_ai_assistance": True,
            "no_model_output": True,
            "no_market_outcome": True,
            "no_old_label": True,
        },
        "peer_answers_hidden": True,
        "exported_at": "2026-08-29T00:01:00+00:00",
        "complete": True,
        "results": rows,
        "target_label_submitted": False,
        "canonical_state_changed": False,
        "no_trading": True,
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _write_split(path: Path, sample_ids: list[str], split: str) -> None:
    path.write_text(
        "".join(
            json.dumps({"metadata": {"sample_id": sample_id, "split": split}}) + "\n"
            for sample_id in sample_ids
        ),
        encoding="utf-8",
    )


def _corpus(tmp_path: Path, count: int = 33) -> tuple[dict, dict[str, dict]]:
    consensus_ids = {f"sample-{index:04d}" for index in range(3, 7)}
    samples = [_sample(index) for index in range(count)]
    built = build_offline_batch(
        samples,
        batch_id="AI-REFERENCE-TEST",
        expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        batch_secret="fixed-batch-secret",
        reviewer_tokens={"A": "reviewer-a-secret", "B": "reviewer-b-secret"},
    )
    owner = built["owner_manifest"]
    owner_zip = tmp_path / "owner.zip"
    with zipfile.ZipFile(owner_zip, "w") as archive:
        archive.writestr("owner/owner_manifest.json", json.dumps(owner))
        archive.writestr("owner/assignment_A.json", json.dumps(built["assignments"]["A"]))
        archive.writestr("owner/assignment_B.json", json.dumps(built["assignments"]["B"]))
    review_a, review_b = tmp_path / "review-a.json", tmp_path / "review-b.json"
    _write_json(
        review_a,
        _submission(
            built["assignments"]["A"], owner["token_maps"]["A"], consensus_ids=consensus_ids
        ),
    )
    _write_json(
        review_b,
        _submission(
            built["assignments"]["B"], owner["token_maps"]["B"], consensus_ids=consensus_ids
        ),
    )
    train, validation, holdout = (
        tmp_path / "train.jsonl",
        tmp_path / "validation.jsonl",
        tmp_path / "holdout.jsonl",
    )
    _write_split(train, ["sample-0000"], "TRAIN")
    _write_split(validation, ["sample-0001"], "VALIDATION")
    _write_split(holdout, ["sample-0002"], "OWNER_HOLDOUT")
    remaining = {sample["sample_id"]: sample for sample in samples[3:]}
    third = tmp_path / "third.jsonl"
    third_rows = []
    for offset, (sample_id, sample) in enumerate(sorted(remaining.items())):
        if offset < 20:
            pair = _pair_a(sample_id)
        elif offset < 25:
            pair = _pair_b(sample_id, consensus_ids)
        else:
            pair = "UNCLEAR", "UNCLEAR"
        content = normalize_qwen_risk_content(sample["content"])
        third_rows.append(
            {
                "sample_id": sample_id,
                "materiality": pair[0],
                "polarity": pair[1],
                "rationale": "Independent third adjudication of the frozen anonymized source content.",
                "model": "independent-test-judge",
                "input_sha256": hashlib.sha256(stable_json(content).encode()).hexdigest(),
            }
        )
    third.write_text(
        "".join(stable_json(row) + "\n" for row in third_rows), encoding="utf-8"
    )
    inputs = {
        "owner_package": owner_zip,
        "review_a": review_a,
        "review_b": review_b,
        "v3_train": train,
        "v3_validation": validation,
        "v3_owner_holdout": holdout,
        "third_adjudication": third,
        "candidate_commit": "a" * 40,
    }
    return inputs, remaining


def test_freezes_ai_assisted_reference_without_human_gold_claim(tmp_path: Path) -> None:
    inputs, _ = _corpus(tmp_path)
    output = tmp_path / "reference"
    manifest = freeze(**inputs, output_dir=output)

    assert manifest["reference_status"] == REFERENCE_STATUS
    assert manifest["remaining_blind_pool_count"] == 30
    assert manifest["accepted_reference_count"] == 25
    assert manifest["excluded_third_matches_neither_human_count"] == 5
    assert manifest["human_consensus_audit"]["count"] == 4
    assert manifest["human_gold_claimed"] is False
    assert manifest["qwen_predictions_read"] is False
    assert manifest["priority_support"] == 7
    assert manifest["evaluation_eligibility"]["passed"] is False

    rows = [
        json.loads(line)
        for line in (output / DATASET_NAME).read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 25
    assert rows[0]["expected"] == json.loads(rows[0]["messages"][-1]["content"])
    assert rows[0]["metadata"]["reference_status"] == REFERENCE_STATUS
    assert rows[0]["metadata"]["human_gold_claimed"] is False
    assert "rationale" not in stable_json(rows)
    assert all("qwen_prediction" not in row for row in rows)
    assert all(row["metadata"]["qwen_prediction_included"] is False for row in rows)

    for filename in (DATASET_NAME, "manifest.json"):
        raw = (output / filename).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        assert (output / f"{filename}.sha256").read_text(encoding="ascii") == (
            f"{digest}  {filename}\n"
        )


def test_input_hash_mismatch_writes_nothing(tmp_path: Path) -> None:
    inputs, _ = _corpus(tmp_path, 10)
    rows = [
        json.loads(line)
        for line in inputs["third_adjudication"].read_text(encoding="utf-8").splitlines()
    ]
    rows[0]["input_sha256"] = "0" * 64
    inputs["third_adjudication"].write_text(
        "".join(stable_json(row) + "\n" for row in rows), encoding="utf-8"
    )
    output = tmp_path / "reference"
    with pytest.raises(ValueError, match="input_sha256 mismatch"):
        freeze(**inputs, output_dir=output)
    assert not output.exists()


def test_qwen_prediction_field_is_rejected(tmp_path: Path) -> None:
    inputs, _ = _corpus(tmp_path, 10)
    rows = [
        json.loads(line)
        for line in inputs["third_adjudication"].read_text(encoding="utf-8").splitlines()
    ]
    rows[0]["qwen_prediction"] = {"materiality": "MATERIAL_ADVERSE"}
    inputs["third_adjudication"].write_text(
        "".join(stable_json(row) + "\n" for row in rows), encoding="utf-8"
    )
    output = tmp_path / "reference"
    with pytest.raises(ValueError, match="prohibited fields"):
        freeze(**inputs, output_dir=output)
    assert not output.exists()


def test_existing_output_directory_is_refused_first(tmp_path: Path) -> None:
    output = tmp_path / "reference"
    output.mkdir()
    with pytest.raises(FileExistsError, match="already exists"):
        freeze(
            owner_package=tmp_path / "missing.zip",
            review_a=tmp_path / "missing-a.json",
            review_b=tmp_path / "missing-b.json",
            v3_train=tmp_path / "missing-train.jsonl",
            v3_validation=tmp_path / "missing-validation.jsonl",
            v3_owner_holdout=tmp_path / "missing-holdout.jsonl",
            third_adjudication=tmp_path / "missing-third.jsonl",
            output_dir=output,
            candidate_commit="a" * 40,
        )
