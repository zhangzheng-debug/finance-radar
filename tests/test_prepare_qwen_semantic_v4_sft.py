from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.models.qwen_risk_contract import validate_semantic_payload
from scripts.prepare_qwen_semantic_v4_sft import (
    MANIFEST_NAME,
    OUTPUT_NAMES,
    _balanced_training_rows,
    prepare,
    stable_json,
)


PAIRS = (
    ("MATERIAL_ADVERSE", "ADVERSE"),
    ("NOT_MATERIAL_ADVERSE", "POSITIVE"),
    ("NOT_MATERIAL_ADVERSE", "MIXED"),
    ("NOT_MATERIAL_ADVERSE", "NEUTRAL"),
    ("NOT_MATERIAL_ADVERSE", "ADVERSE"),
)


def _target(materiality: str, polarity: str) -> dict:
    codes = [
        "ACTUAL_EVENT_COMPLETED_OR_EFFECTIVE",
        "PRIMARY_SUBJECT_DIRECTLY_AFFECTED",
        "NEW_MATERIAL_FACT_OR_STATUS_CHANGE",
    ]
    if materiality == "MATERIAL_ADVERSE":
        codes.append("MATERIAL_DOWNSIDE_MECHANISM")
    else:
        codes.append("NO_MATERIAL_DOWNSIDE_MECHANISM")
    risk_status = "ACTIVE" if polarity in {"ADVERSE", "MIXED"} else "NO_ADVERSE_CONDITION"
    if polarity == "ADVERSE":
        codes.extend(["ADVERSE_CONDITION_ACTIVE", "ADVERSE_COMPONENT_PRESENT"])
    elif polarity == "POSITIVE":
        codes.append("POSITIVE_COMPONENT_PRESENT")
    elif polarity == "MIXED":
        codes.extend(["ADVERSE_CONDITION_ACTIVE", "POSITIVE_AND_ADVERSE_COMPONENTS"])
    if materiality == "MATERIAL_ADVERSE":
        impact_strength = "MODERATE"
        codes.append("MODERATE_SOURCE_SUPPORTED_IMPACT")
    elif polarity == "NEUTRAL":
        impact_strength = "ROUTINE_OR_NONE"
        codes.append("ROUTINE_OR_NO_SOURCE_SUPPORTED_IMPACT")
    else:
        impact_strength = "MINOR"
        codes.append("MINOR_SOURCE_SUPPORTED_IMPACT")
    return {
        "materiality": materiality,
        "polarity": polarity,
        "impact_strength": impact_strength,
        "event_realization": "REALIZED_OR_EFFECTIVE",
        "subject_relation": "PRIMARY_SUBJECT",
        "risk_status": risk_status,
        "novelty": "NEW_EVENT_OR_STATUS_CHANGE",
        "reason_codes": codes,
        "brief_reason": "The exact frozen source supports this independent semantic classification.",
    }


def _row(index: int, *, entity: str | None = None, chain: str | None = None) -> dict:
    materiality, polarity = PAIRS[index % len(PAIRS)]
    result = {
        "sample_id": f"sample-{index:04d}",
        "event_id": f"event-{index:04d}",
        "content": {
            "as_of": "2026-08-29T00:00:00Z",
            "event_date": "2026-08-29",
            "headline": f"Independent adjudicated event {index}",
            "summary": f"Exact frozen source text for event {index}.",
            "passages": [{"document_type": "8-K", "passage": f"Passage {index}."}],
        },
        "entity_group": entity or f"entity-{index}",
        "event_chain_group": chain or f"chain-{index}",
        "adjudication_model": "independent-judge",
    }
    result.update(_target(materiality, polarity))
    return result


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(stable_json(row) + "\n" for row in rows), encoding="utf-8")


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_prepare_is_reproducible_and_group_leakage_free(tmp_path: Path) -> None:
    rows = [_row(index) for index in range(45)]
    # These three rows form one transitive component: 0--entity--1--chain--2.
    rows[0]["entity_group"] = "shared-entity"
    rows[1]["entity_group"] = "shared-entity"
    rows[1]["event_chain_group"] = "shared-chain"
    rows[2]["event_chain_group"] = "shared-chain"
    source = tmp_path / "adjudications.jsonl"
    _write(source, rows)

    first = tmp_path / "first"
    second = tmp_path / "second"
    first_manifest = prepare(adjudications=source, output_dir=first)
    second_manifest = prepare(adjudications=source, output_dir=second)

    assert first_manifest == second_manifest
    assert first_manifest["split"]["leakage_audit"]["passed"] is True
    assert first_manifest["qwen_predictions_read"] is False
    assert first_manifest["human_gold_claimed"] is False
    for name in (*OUTPUT_NAMES.values(), MANIFEST_NAME):
        assert (first / name).read_bytes() == (second / name).read_bytes()
        digest = hashlib.sha256((first / name).read_bytes()).hexdigest()
        assert (first / f"{name}.sha256").read_text(encoding="ascii") == f"{digest}  {name}\n"

    split_by_sample: dict[str, str] = {}
    for key, split in (("train", "TRAIN"), ("dev", "DEV"), ("test", "TEST")):
        for row in _read(first / OUTPUT_NAMES[key]):
            split_by_sample[row["metadata"]["sample_id"]] = split
    assert len(split_by_sample) == len(rows)
    assert len({split_by_sample[f"sample-{index:04d}"] for index in range(3)}) == 1


def test_only_train_is_resampled_with_max_not_product_policy(tmp_path: Path) -> None:
    def semantic_row(sample_id: str, materiality: str, polarity: str) -> dict:
        target = _target(materiality, polarity)
        return {
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "{}"},
                {"role": "assistant", "content": stable_json(target)},
            ],
            "metadata": {"sample_id": sample_id, "split": "TRAIN"},
        }

    rows = [
        semantic_row("priority", "MATERIAL_ADVERSE", "ADVERSE"),
        semantic_row("positive", "NOT_MATERIAL_ADVERSE", "POSITIVE"),
        semantic_row("mixed-priority", "MATERIAL_ADVERSE", "MIXED"),
        semantic_row("neutral", "NOT_MATERIAL_ADVERSE", "NEUTRAL"),
    ]
    balanced = _balanced_training_rows(
        rows, {"material_adverse": 3, "positive": 4, "mixed": 4}
    )
    counts = {}
    for row in balanced:
        counts[row["metadata"]["origin_sample_id"]] = (
            counts.get(row["metadata"]["origin_sample_id"], 0) + 1
        )
    assert counts == {"priority": 3, "positive": 4, "mixed-priority": 4, "neutral": 1}
    assert len({row["metadata"]["training_instance_id"] for row in balanced}) == len(balanced)

    source = tmp_path / "adjudications.jsonl"
    _write(source, [_row(index) for index in range(45)])
    output = tmp_path / "out"
    manifest = prepare(adjudications=source, output_dir=output)
    assert manifest["split"]["dev_resampled"] is False
    assert manifest["split"]["test_resampled"] is False
    assert manifest["split"]["test_used_for_model_selection"] is False
    assert all("oversampled" not in row["metadata"] for row in _read(output / OUTPUT_NAMES["dev"]))
    assert all("oversampled" not in row["metadata"] for row in _read(output / OUTPUT_NAMES["test"]))
    assert OUTPUT_NAMES["test"] not in manifest["ms_swift"]["training_recipe"]


def test_rejects_prior_qwen_prediction_before_writing(tmp_path: Path) -> None:
    row = _row(1)
    row["qwen_prediction"] = {"polarity": "ADVERSE"}
    source = tmp_path / "adjudications.jsonl"
    _write(source, [row])
    output = tmp_path / "out"
    with pytest.raises(ValueError, match="prohibited prior-model fields"):
        prepare(adjudications=source, output_dir=output)
    assert not output.exists()


def test_rejects_existing_output_and_missing_group_identity(tmp_path: Path) -> None:
    source = tmp_path / "adjudications.jsonl"
    row = _row(1)
    row["entity_group"] = ""
    _write(source, [row])
    output = tmp_path / "out"
    with pytest.raises(ValueError, match="missing group identity"):
        prepare(adjudications=source, output_dir=output)
    assert not output.exists()

    output.mkdir()
    with pytest.raises(FileExistsError, match="already exists"):
        prepare(adjudications=source, output_dir=output)


def test_nested_multiview_final_joins_source_index_and_verifies_hashes(tmp_path: Path) -> None:
    source_rows = []
    adjudications = []
    for index in range(30):
        flat = _row(index)
        content_hash = hashlib.sha256(stable_json(flat["content"]).encode("utf-8")).hexdigest()
        source_rows.append(
            {
                "sample_id": flat["sample_id"],
                "event_id": flat["event_id"],
                "content": flat["content"],
                "text_sha256": content_hash,
                "entity_group": flat["entity_group"],
                "event_chain_group": flat["event_chain_group"],
            }
        )
        adjudications.append(
            {
                "sample_id": flat["sample_id"],
                "input_sha256": content_hash,
                "model": "deepseek-v4-flash",
                "fact_mechanism_review": _target("NOT_MATERIAL_ADVERSE", "NEUTRAL"),
                "boundary_review": _target("NOT_MATERIAL_ADVERSE", "NEUTRAL"),
                "final": {key: flat[key] for key in _target(flat["materiality"], flat["polarity"])},
                "first_pass_pair_agreed": True,
            }
        )
    source_path = tmp_path / "source.jsonl"
    adjudication_path = tmp_path / "nested.jsonl"
    _write(source_path, source_rows)
    _write(adjudication_path, adjudications)
    output = tmp_path / "out"
    manifest = prepare(
        adjudications=adjudication_path,
        source_index=source_path,
        output_dir=output,
    )
    assert manifest["input"]["nested_multiview_final"] is True
    assert manifest["source_index"]["joined_adjudication_count"] == 30
    assert manifest["source_index"]["text_sha256_verified"] is True
    target = json.loads(_read(output / OUTPUT_NAMES["train"])[0]["messages"][-1]["content"])
    assert set(target) == {
        "materiality",
        "polarity",
        "impact_strength",
        "event_realization",
        "subject_relation",
        "risk_status",
        "novelty",
        "reason_codes",
        "brief_reason",
    }

    broken = list(adjudications)
    broken[0] = dict(broken[0], input_sha256="0" * 64)
    broken_path = tmp_path / "broken.jsonl"
    _write(broken_path, broken)
    with pytest.raises(ValueError, match="adjudication input_sha256 mismatch"):
        prepare(
            adjudications=broken_path,
            source_index=source_path,
            output_dir=tmp_path / "broken-out",
        )


def test_core_v1_target_keeps_v2_mechanisms_as_audit_metadata(tmp_path: Path) -> None:
    source = tmp_path / "adjudications.jsonl"
    _write(source, [_row(index) for index in range(45)])
    output = tmp_path / "core-v1"

    manifest = prepare(
        adjudications=source,
        output_dir=output,
        target_contract="core-v1",
    )

    row = _read(output / OUTPUT_NAMES["train"])[0]
    target = json.loads(row["messages"][-1]["content"])
    assert validate_semantic_payload(target) == []
    assert set(target) == {
        "materiality",
        "polarity",
        "adverse_strength",
        "semantic_priority",
    }
    audit = row["metadata"]["adjudication_v2_audit"]
    assert set(audit) == {
        "impact_strength",
        "event_realization",
        "subject_relation",
        "risk_status",
        "novelty",
        "reason_codes",
        "brief_reason",
    }
    assert manifest["target_contract"] == "core-v1"
    assert manifest["semantic_contract_version"] == "qwen-risk-semantics-v1"
    assert manifest["adjudication_contract_version"] == "qwen-risk-semantics-v2"
    assert manifest["mechanism_axes_exposed_to_model_target"] is False
    assert manifest["full_v2_adjudication_preserved_in_metadata"] is True


def test_fixed_test_split_writes_external_unique_test_only(tmp_path: Path) -> None:
    source_rows = []
    adjudications = []
    for index in range(12):
        flat = _row(index)
        content_hash = hashlib.sha256(stable_json(flat["content"]).encode("utf-8")).hexdigest()
        source_rows.append(
            {
                "sample_id": flat["sample_id"],
                "event_id": flat["event_id"],
                "content": flat["content"],
                "text_sha256": content_hash,
                "entity_group": flat["entity_group"],
                "event_chain_group": flat["event_chain_group"],
            }
        )
        adjudications.append(
            {
                "sample_id": flat["sample_id"],
                "input_sha256": content_hash,
                "model": "independent-strict-judge",
                "fact_mechanism_review": _target(
                    flat["materiality"], flat["polarity"]
                ),
                "boundary_review": _target(flat["materiality"], flat["polarity"]),
                "final": _target(flat["materiality"], flat["polarity"]),
                "first_pass_pair_agreed": True,
            }
        )
    source_path = tmp_path / "strict-source-index.jsonl"
    adjudication_path = tmp_path / "strict-adjudications.jsonl"
    _write(source_path, source_rows)
    _write(adjudication_path, adjudications)
    output = tmp_path / "external-test"

    manifest = prepare(
        adjudications=adjudication_path,
        source_index=source_path,
        output_dir=output,
        target_contract="core-v1",
        fixed_split="TEST",
        agreement_policy="none",
    )

    test_rows = _read(output / OUTPUT_NAMES["test"])
    assert len(test_rows) == len(adjudications)
    assert len({row["metadata"]["sample_id"] for row in test_rows}) == len(test_rows)
    assert all(row["metadata"]["split"] == "TEST" for row in test_rows)
    assert all("oversampled" not in row["metadata"] for row in test_rows)
    assert not (output / OUTPUT_NAMES["train"]).exists()
    assert not (output / OUTPUT_NAMES["train_balanced"]).exists()
    assert not (output / OUTPUT_NAMES["dev"]).exists()
    assert manifest["dataset_role"] == "EXTERNAL_FIXED_TEST_ONLY"
    assert manifest["split"]["fixed_split"] == "TEST"
    assert manifest["split"]["unique_rows"] == {"TRAIN": 0, "DEV": 0, "TEST": 12}
    assert manifest["train_resampling"]["policy"] == "NONE_EXTERNAL_FIXED_TEST_ONLY"
    assert manifest["ms_swift"]["training_allowed"] is False
    assert manifest["ms_swift"]["training_recipe"] is None
    assert set(manifest["outputs"]) == {
        OUTPUT_NAMES["test"],
        OUTPUT_NAMES["split_audit"],
    }

    with pytest.raises(ValueError, match="prevent selection bias"):
        prepare(
            adjudications=adjudication_path,
            source_index=source_path,
            output_dir=tmp_path / "filtered-external-test",
            fixed_split="TEST",
        )

    with pytest.raises(ValueError, match="mutually exclusive"):
        prepare(
            adjudications=adjudication_path,
            source_index=source_path,
            output_dir=tmp_path / "invalid-fixed-test",
            fixed_split="TEST",
            ratios={"TRAIN": 0.7, "DEV": 0.15, "TEST": 0.15},
        )


def test_real_three_party_join_and_agreement_policies(tmp_path: Path) -> None:
    provider_rows = []
    owner_rows = []
    adjudications = []
    for index in range(30):
        flat = _row(index)
        content_hash = hashlib.sha256(stable_json(flat["content"]).encode("utf-8")).hexdigest()
        provider_rows.append({"sample_id": flat["sample_id"], "content": flat["content"]})
        owner_rows.append(
            {
                "schema_version": 1,
                "sample_id": flat["sample_id"],
                "source_event_id": flat["event_id"],
                "provider_text_sha256": content_hash,
                "source_text_sha256": content_hash,
                "entity_group": flat["entity_group"],
                "event_chain_group": flat["event_chain_group"],
            }
        )
        first = _target(flat["materiality"], flat["polarity"])
        second = json.loads(json.dumps(first))
        if index == 3:
            # Core pair agrees, but the independent impact axis does not.
            second["impact_strength"] = "MINOR"
            second["reason_codes"].remove("ROUTINE_OR_NO_SOURCE_SUPPORTED_IMPACT")
            second["reason_codes"].append("MINOR_SOURCE_SUPPORTED_IMPACT")
        if index == 1:
            first = _target("NOT_MATERIAL_ADVERSE", "POSITIVE")
            second = _target("NOT_MATERIAL_ADVERSE", "ADVERSE")
        adjudications.append(
            {
                "sample_id": flat["sample_id"],
                "input_sha256": content_hash,
                "model": "deepseek-v4-pro",
                "fact_mechanism_review": first,
                "boundary_review": second,
                "final": _target(flat["materiality"], flat["polarity"]),
                "first_pass_pair_agreed": index != 1,
            }
        )
    provider_path = tmp_path / "development_provider_input.jsonl"
    owner_path = tmp_path / "development_source_index.owner-only.jsonl"
    adjudication_path = tmp_path / "deepseek_multiview_semantic_v2.jsonl"
    _write(provider_path, provider_rows)
    _write(owner_path, owner_rows)
    _write(adjudication_path, adjudications)

    all_manifest = prepare(
        adjudications=adjudication_path,
        provider_input=provider_path,
        source_index=owner_path,
        output_dir=tmp_path / "all",
    )
    core_manifest = prepare(
        adjudications=adjudication_path,
        provider_input=provider_path,
        source_index=owner_path,
        agreement_policy="core",
        output_dir=tmp_path / "core",
    )
    none_manifest = prepare(
        adjudications=adjudication_path,
        provider_input=provider_path,
        source_index=owner_path,
        agreement_policy="none",
        output_dir=tmp_path / "none",
    )

    assert all_manifest["agreement_filter"]["kept_rows"] == 28
    assert all_manifest["agreement_filter"]["filtered_rows"] == 2
    assert core_manifest["agreement_filter"]["kept_rows"] == 29
    assert core_manifest["agreement_filter"]["filtered_rows"] == 1
    assert none_manifest["agreement_filter"]["kept_rows"] == 30
    assert none_manifest["provider_input"]["joined_adjudication_count"] == 30
    assert none_manifest["source_index"]["content_stored_in_index"] is False
    assert none_manifest["source_index"]["provider_text_sha256_verified"] is True
    assert none_manifest["source_index"]["source_text_sha256_verified"] is True
    all_rows = sum(
        (_read(tmp_path / "none" / OUTPUT_NAMES[name]) for name in ("train", "dev", "test")),
        [],
    )
    assert {row["metadata"]["event_id"] for row in all_rows} == {
        flat["event_id"] for flat in (_row(index) for index in range(30))
    }
    assert all(row["metadata"]["provider_text_sha256"] for row in all_rows)

    broken_owner = list(owner_rows)
    broken_owner[0] = dict(broken_owner[0], provider_text_sha256="0" * 64)
    broken_owner_path = tmp_path / "broken-owner.jsonl"
    _write(broken_owner_path, broken_owner)
    with pytest.raises(ValueError, match="source provider_text_sha256 mismatch"):
        prepare(
            adjudications=adjudication_path,
            provider_input=provider_path,
            source_index=broken_owner_path,
            agreement_policy="none",
            output_dir=tmp_path / "broken-owner-out",
        )

    broken_source = list(owner_rows)
    broken_source[0] = dict(broken_source[0], source_text_sha256="f" * 64)
    broken_source_path = tmp_path / "broken-source.jsonl"
    _write(broken_source_path, broken_source)
    with pytest.raises(ValueError, match="source text_sha256 mismatch"):
        prepare(
            adjudications=adjudication_path,
            provider_input=provider_path,
            source_index=broken_source_path,
            agreement_policy="none",
            output_dir=tmp_path / "broken-source-out",
        )

    broken_adjudications = list(adjudications)
    broken_adjudications[0] = dict(
        broken_adjudications[0], input_sha256="a" * 64
    )
    broken_adjudication_path = tmp_path / "broken-adjudications.jsonl"
    _write(broken_adjudication_path, broken_adjudications)
    with pytest.raises(ValueError, match="adjudication input_sha256 mismatch"):
        prepare(
            adjudications=broken_adjudication_path,
            provider_input=provider_path,
            source_index=owner_path,
            agreement_policy="none",
            output_dir=tmp_path / "broken-adjudication-out",
        )
