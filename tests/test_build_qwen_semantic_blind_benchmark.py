from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.human_gold_review import build_offline_batch
from scripts.build_qwen_semantic_blind_benchmark import build


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


def _submission(
    assignment: dict,
    token_map: dict[str, str],
    *,
    consensus_ids: set[str],
) -> dict:
    rows = []
    for event in assignment["events"]:
        sample_id = token_map[event["sample_token"]]
        if assignment["reviewer_slot"] == "A" or sample_id in consensus_ids:
            materiality, polarity = "MATERIAL_ADVERSE", "ADVERSE"
        else:
            materiality, polarity = "NOT_MATERIAL_ADVERSE", "POSITIVE"
        rows.append(
            {
                "sample_token": event["sample_token"],
                "materiality": materiality,
                "polarity": polarity,
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


def _corpus(tmp_path: Path, count: int, consensus_ids: set[str]) -> dict:
    built = build_offline_batch(
        [_sample(index) for index in range(count)],
        batch_id="SEMANTIC-BLIND-TEST",
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
    review_a = tmp_path / "review-a.json"
    review_b = tmp_path / "review-b.json"
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
    adapter = tmp_path / "adapter_model.safetensors"
    adapter.write_bytes(b"frozen-adapter")
    return {
        "owner_package": owner_zip,
        "review_a": review_a,
        "review_b": review_b,
        "v3_train": train,
        "v3_validation": validation,
        "v3_owner_holdout": holdout,
        "adapter": adapter,
        "candidate_commit": "a" * 40,
    }


def test_freezes_real_protocol_shape_without_exposing_labels(tmp_path: Path) -> None:
    consensus = {f"sample-{index:04d}" for index in range(3, 7)}
    inputs = _corpus(tmp_path, 438, consensus)
    output = tmp_path / "freeze"
    manifest = build(**inputs, output_dir=output)

    assert manifest["prefreeze_pool_count"] == 435
    assert manifest["arbitration_input_count"] == 435
    assert manifest["consensus_audit"]["row_count"] == 4
    assert manifest["disagreements"]["row_count"] == 431
    assert manifest["benchmark_ready"] is False
    assert manifest["human_gold_claimed"] is False
    assert manifest["full_arbitration_gold"] is False
    assert manifest["model_predictions_read"] is False
    assert "sample_ids" not in manifest["disagreements"]

    arbitration_text = (output / "arbitration_inputs.jsonl").read_text(encoding="utf-8")
    arbitration_rows = [json.loads(line) for line in arbitration_text.splitlines()]
    assert len(arbitration_rows) == 435
    assert set(arbitration_rows[0]) == {"sample_id", "content"}
    assert "reviewer_labels" not in arbitration_text
    assert "MATERIAL_ADVERSE" not in arbitration_text
    assert "model_prediction" not in arbitration_text
    assert "assistant" not in arbitration_text

    sealed_text = (output / "sealed_reviewer_labels.jsonl").read_text(encoding="utf-8")
    sealed_rows = [json.loads(line) for line in sealed_text.splitlines()]
    assert len(sealed_rows) == 435
    assert set(sealed_rows[0]) == {"sample_id", "reviewer_labels"}
    assert "content" not in sealed_text
    assert "rationale" not in sealed_text
    assert "reviewer-a-secret" not in sealed_text
    assert "MATERIAL_ADVERSE" in sealed_text

    for filename in ("arbitration_inputs.jsonl", "sealed_reviewer_labels.jsonl"):
        raw = (output / filename).read_bytes()
        expected = hashlib.sha256(raw).hexdigest()
        assert (output / f"{filename}.sha256").read_text(encoding="ascii") == (
            f"{expected}  {filename}\n"
        )


def test_same_inputs_produce_identical_freeze_bytes(tmp_path: Path) -> None:
    inputs = _corpus(tmp_path, 8, {"sample-0003", "sample-0004"})
    first = build(**inputs, output_dir=tmp_path / "first")
    second = build(**inputs, output_dir=tmp_path / "second")
    assert first == second
    for filename in (
        "arbitration_inputs.jsonl",
        "arbitration_inputs.jsonl.sha256",
        "sealed_reviewer_labels.jsonl",
        "sealed_reviewer_labels.jsonl.sha256",
        "manifest.json",
    ):
        assert (tmp_path / "first" / filename).read_bytes() == (
            tmp_path / "second" / filename
        ).read_bytes()


def test_strict_submission_failure_writes_nothing(tmp_path: Path) -> None:
    inputs = _corpus(tmp_path, 8, {"sample-0003"})
    submission = json.loads(inputs["review_b"].read_text(encoding="utf-8"))
    submission["attestations"]["no_model_output"] = False
    _write_json(inputs["review_b"], submission)
    output = tmp_path / "freeze"
    with pytest.raises(ValueError, match="strict A/B submission validation failed"):
        build(**inputs, output_dir=output)
    assert not output.exists()


def test_existing_output_directory_is_refused_before_input_reads(tmp_path: Path) -> None:
    output = tmp_path / "freeze"
    output.mkdir()
    with pytest.raises(FileExistsError, match="already exists"):
        build(
            owner_package=tmp_path / "missing-owner.zip",
            review_a=tmp_path / "missing-a.json",
            review_b=tmp_path / "missing-b.json",
            v3_train=tmp_path / "missing-train.jsonl",
            v3_validation=tmp_path / "missing-validation.jsonl",
            v3_owner_holdout=tmp_path / "missing-holdout.jsonl",
            adapter=tmp_path / "missing-adapter",
            output_dir=output,
            candidate_commit="a" * 40,
        )


def test_overlapping_v3_splits_are_rejected_without_artifacts(tmp_path: Path) -> None:
    inputs = _corpus(tmp_path, 8, {"sample-0003"})
    _write_split(inputs["v3_validation"], ["sample-0000"], "VALIDATION")
    output = tmp_path / "freeze"
    with pytest.raises(ValueError, match="v3 splits overlap"):
        build(**inputs, output_dir=output)
    assert not output.exists()
