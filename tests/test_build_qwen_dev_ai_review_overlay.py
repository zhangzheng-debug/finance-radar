from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.models.qwen_risk_contract import expected_semantic_payload
from app.models.qwen_weak_supervision_contract import (
    QWEN_WEAK_PROMPT_VERSION,
    QWEN_WEAK_SUPERVISION_VERSION,
    QWEN_WEAK_SYSTEM_PROMPT,
)
from scripts import build_qwen_dev_ai_review_overlay as overlay


PROMPT = QWEN_WEAK_SYSTEM_PROMPT
PROMPT_VERSION = QWEN_WEAK_PROMPT_VERSION


def _review_values(kind: str) -> dict:
    if kind == "adverse":
        return {
            "materiality": "MATERIAL_ADVERSE",
            "polarity": "ADVERSE",
            "reason": (
                "The source reports a binding adverse action with a moderate "
                "downside impact for the primary subject."
            ),
        }
    if kind == "positive":
        return {
            "materiality": "NOT_MATERIAL_ADVERSE",
            "polarity": "POSITIVE",
            "reason": (
                "The source reports a realized positive outcome with moderate "
                "impact and no material downside mechanism."
            ),
        }
    return {
        "materiality": "NOT_MATERIAL_ADVERSE",
        "polarity": "NEUTRAL",
        "reason": (
            "The source describes a hypothetical clause and no realized adverse "
            "condition for the primary subject."
        ),
    }


def _review_row(sample_id: str, kind: str, slot: str) -> dict:
    review_class = (
        "INDEPENDENT_AI_ARBITRATION_NOT_HUMAN_GOLD"
        if slot == "C"
        else "INDEPENDENT_AI_REVIEW_NOT_HUMAN_GOLD"
    )
    return {
        "sample_id": sample_id,
        **_review_values(kind),
        "review_class": review_class,
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _fixture(
    tmp_path: Path,
    *,
    count: int = overlay.EXPECTED_ROW_COUNT,
    disagreements: set[int] | None = None,
) -> tuple[dict[str, Path], list[str]]:
    disagreements = {0} if disagreements is None else disagreements
    paths = {
        "dev_sft": tmp_path / "dev.jsonl",
        "source_only": tmp_path / "source-only.jsonl",
        "review_a": tmp_path / "review-a.jsonl",
        "review_b": tmp_path / "review-b.jsonl",
        "review_c": tmp_path / "review-c.jsonl",
        "output_dir": tmp_path / "overlay",
    }
    prompt_sha = hashlib.sha256(PROMPT.encode("utf-8")).hexdigest()
    ids = [f"sample-{index:03d}" for index in range(count)]
    dev_rows: list[dict] = []
    source_rows: list[dict] = []
    review_a_rows: list[dict] = []
    review_b_rows: list[dict] = []
    review_c_rows: list[dict] = []
    for index, sample_id in enumerate(ids):
        content = {
            "as_of": "2026-08-30T00:00:00+00:00",
            "event_date": "2026-08-30",
            "headline": f"Source headline {index}",
            "summary": "The supplied text is reviewed without external outcomes.",
            "passages": [
                {
                    "document_type": "8-K",
                    "item_section": "1.01",
                    "published_at": (
                        "2026-08-30T01:30:00+00:00"
                        if index == 0
                        else "2026-08-30"
                    ),
                    "passage": f"Contemporaneous passage {index}.",
                }
            ],
        }
        content_sha = hashlib.sha256(
            overlay.stable_json(content).encode("utf-8")
        ).hexdigest()
        dev_rows.append(
            {
                "messages": [
                    {"role": "system", "content": PROMPT},
                    {"role": "user", "content": overlay.stable_json(content)},
                    {
                        "role": "assistant",
                        "content": "ORIGINAL WEAK TARGET MUST NOT BE PARSED",
                    },
                ],
                "metadata": {
                    "sample_id": sample_id,
                    "event_id": f"event-{index:03d}",
                    "entity_group": f"issuer-{index:03d}",
                    "event_chain_group": f"chain-{index:03d}",
                    "content_sha256": content_sha,
                    "split": "DEV",
                    "target_contract": "core-v1",
                    "model_output_contract": "core-axes-v1",
                    "weak_supervision_version": QWEN_WEAK_SUPERVISION_VERSION,
                    "semantic_target": {
                        "poisoned_original_weak_truth": "MUST_NOT_BE_USED"
                    },
                    "prompt_version": PROMPT_VERSION,
                    "prompt_sha256": prompt_sha,
                    "weak_rule": "MUST_NOT_BE_COPIED",
                    "label_provenance": "ORIGINAL_WEAK_SOURCE",
                    "label_classification": "WEAK_SUPERVISION_NOT_HUMAN_GOLD",
                    "human_gold_claimed": False,
                    "qwen_prediction_included": False,
                    "post_event_market_data_included": False,
                    "evidence_state_used_as_model_target": False,
                },
            }
        )
        source_rows.append({"sample_id": sample_id, "content": content})
        a_kind = "adverse" if index in disagreements else "routine"
        review_a_rows.append(_review_row(sample_id, a_kind, "A"))
        review_b_rows.append(_review_row(sample_id, "routine", "B"))
        if index in disagreements:
            review_c_rows.append(_review_row(sample_id, "positive", "C"))

    _write_jsonl(paths["dev_sft"], dev_rows)
    _write_jsonl(paths["source_only"], source_rows)
    _write_jsonl(paths["review_a"], review_a_rows)
    _write_jsonl(paths["review_b"], review_b_rows)
    _write_jsonl(paths["review_c"], review_c_rows)
    return paths, ids


def _build(paths: dict[str, Path]) -> dict:
    return overlay.build_overlay(
        dev_sft=paths["dev_sft"],
        source_only=paths["source_only"],
        review_a=paths["review_a"],
        review_b=paths["review_b"],
        review_c=paths["review_c"],
        output_dir=paths["output_dir"],
    )


def test_builds_138_row_overlay_from_ab_consensus_and_c_arbitration(
    tmp_path: Path,
) -> None:
    paths, ids = _fixture(tmp_path)

    manifest = _build(paths)
    output_path = paths["output_dir"] / overlay.OUTPUT_NAME
    rows = _read_jsonl(output_path)

    assert len(rows) == overlay.EXPECTED_ROW_COUNT == 138
    assert [row["metadata"]["sample_id"] for row in rows] == ids
    first_target = json.loads(rows[0]["messages"][-1]["content"])
    assert first_target == {
        "materiality": "NOT_MATERIAL_ADVERSE",
        "polarity": "POSITIVE",
    }
    assert rows[0]["metadata"]["semantic_target"] == expected_semantic_payload(
        "NOT_MATERIAL_ADVERSE", "POSITIVE"
    )
    assert rows[0]["metadata"]["review_resolution"]["decision_source"] == (
        "C_ARBITRATION"
    )
    review_a_by_id = {
        row["sample_id"]: row for row in _read_jsonl(paths["review_a"])
    }
    review_b_by_id = {
        row["sample_id"]: row for row in _read_jsonl(paths["review_b"])
    }
    review_c_by_id = {
        row["sample_id"]: row for row in _read_jsonl(paths["review_c"])
    }
    first_review_hashes = rows[0]["metadata"]["review_resolution"][
        "original_review_row_sha256"
    ]
    assert first_review_hashes == {
        "A": hashlib.sha256(
            overlay.stable_json(review_a_by_id[ids[0]]).encode("utf-8")
        ).hexdigest(),
        "B": hashlib.sha256(
            overlay.stable_json(review_b_by_id[ids[0]]).encode("utf-8")
        ).hexdigest(),
        "C": hashlib.sha256(
            overlay.stable_json(review_c_by_id[ids[0]]).encode("utf-8")
        ).hexdigest(),
    }
    assert rows[0]["metadata"]["review_resolution"][
        "original_review_file_sha256"
    ] == {
        "A": hashlib.sha256(paths["review_a"].read_bytes()).hexdigest(),
        "B": hashlib.sha256(paths["review_b"].read_bytes()).hexdigest(),
        "C": hashlib.sha256(paths["review_c"].read_bytes()).hexdigest(),
    }
    assert rows[1]["metadata"]["review_resolution"]["decision_source"] == (
        "A_B_CONSENSUS"
    )
    assert json.loads(rows[1]["messages"][-1]["content"]) == {
        "materiality": "NOT_MATERIAL_ADVERSE",
        "polarity": "NEUTRAL",
    }
    for row in rows:
        metadata = row["metadata"]
        assert metadata["label_provenance"] == (
            "INDEPENDENT_AI_REVIEW_CONSENSUS"
        )
        assert metadata["label_classification"] == "AI_REVIEW_NOT_HUMAN_GOLD"
        assert metadata["target_contract"] == "core-v1"
        assert metadata["model_output_contract"] == "core-axes-v1"
        assert metadata["human_gold_claimed"] is False
        assert metadata["original_weak_truth_used"] is False
        assert set(metadata["overlay_input_sha256"]) == {
            "dev_sft",
            "source_only",
            "review_a",
            "review_b",
            "review_c",
        }
        assert "weak_rule" not in metadata
        assert metadata["semantic_target"].get(
            "poisoned_original_weak_truth"
        ) is None

    output_raw = output_path.read_bytes()
    assert hashlib.sha256(output_raw).hexdigest() == manifest["output"]["sha256"]
    assert manifest["row_count"] == 138
    assert manifest["resolution_counts"] == {
        "A_B_CONSENSUS": 137,
        "C_ARBITRATION": 1,
    }
    assert manifest["coverage"]["review_c_equals_a_b_disagreement_set"] is True
    assert manifest["source_content_equivalence"]["raw_match_count"] == 138
    assert (
        manifest["source_content_equivalence"][
            "timezone_normalized_match_count"
        ]
        == 0
    )
    assert rows[0]["metadata"]["source_content_equivalence"]["match_method"] == (
        "RAW_EXACT"
    )
    assert rows[0]["metadata"]["source_content_equivalence"][
        "contract_version"
    ] == overlay.SOURCE_CONTENT_EQUIVALENCE_CONTRACT_VERSION
    assert manifest["review_input_schema"] == {
        "fields": [
            "materiality",
            "polarity",
            "reason",
            "review_class",
            "sample_id",
        ],
        "review_class_by_slot": {
            "A": "INDEPENDENT_AI_REVIEW_NOT_HUMAN_GOLD",
            "B": "INDEPENDENT_AI_REVIEW_NOT_HUMAN_GOLD",
            "C": "INDEPENDENT_AI_ARBITRATION_NOT_HUMAN_GOLD",
        },
        "semantic_v2_fields_required_or_derived": False,
    }
    assert manifest["inputs"]["review_a"]["sha256"] == hashlib.sha256(
        paths["review_a"].read_bytes()
    ).hexdigest()
    assert manifest["isolation"]["original_weak_truth_used"] is False
    assert manifest["isolation"]["frozen_review_rows_rewritten"] is False
    assert manifest["human_gold_claimed"] is False
    assert (
        paths["output_dir"] / (overlay.OUTPUT_NAME + ".sha256")
    ).read_text(encoding="ascii") == (
        f"{manifest['output']['sha256']}  {overlay.OUTPUT_NAME}\n"
    )
    manifest_raw = (paths["output_dir"] / overlay.MANIFEST_NAME).read_bytes()
    manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    assert (
        paths["output_dir"] / (overlay.MANIFEST_NAME + ".sha256")
    ).read_text(encoding="ascii") == f"{manifest_sha}  {overlay.MANIFEST_NAME}\n"


@pytest.mark.parametrize("case", ["missing", "extra"])
def test_review_c_must_equal_exact_ab_disagreement_set(
    tmp_path: Path, case: str
) -> None:
    paths, ids = _fixture(tmp_path)
    c_rows = _read_jsonl(paths["review_c"])
    if case == "missing":
        c_rows.clear()
    else:
        c_rows.append(_review_row(ids[1], "routine", "C"))
    _write_jsonl(paths["review_c"], c_rows)

    with pytest.raises(ValueError, match="review C arbitration sample_id coverage"):
        _build(paths)
    assert not paths["output_dir"].exists()


def test_empty_c_is_valid_only_when_ab_have_no_pair_disagreements(
    tmp_path: Path,
) -> None:
    paths, _ = _fixture(tmp_path, disagreements=set())

    manifest = _build(paths)

    assert manifest["inputs"]["review_c"]["row_count"] == 0
    assert manifest["coverage"]["a_b_disagreement_count"] == 0
    assert manifest["resolution_counts"] == {"A_B_CONSENSUS": 138}


@pytest.mark.parametrize("slot", ["source_only", "review_a", "review_b"])
def test_full_inputs_require_exact_unique_dev_coverage(
    tmp_path: Path, slot: str
) -> None:
    paths, _ = _fixture(tmp_path)
    rows = _read_jsonl(paths[slot])
    rows.pop()
    _write_jsonl(paths[slot], rows)

    with pytest.raises(ValueError, match="sample_id coverage mismatch"):
        _build(paths)
    assert not paths["output_dir"].exists()


def test_duplicate_review_id_and_invalid_review_fail_closed(tmp_path: Path) -> None:
    duplicate_root = tmp_path / "duplicate"
    duplicate_root.mkdir()
    duplicate_paths, _ = _fixture(duplicate_root)
    rows = _read_jsonl(duplicate_paths["review_a"])
    rows.append(rows[0])
    _write_jsonl(duplicate_paths["review_a"], rows)
    with pytest.raises(ValueError, match="review A duplicate sample_id"):
        _build(duplicate_paths)

    invalid_root = tmp_path / "invalid"
    invalid_root.mkdir()
    invalid_paths, _ = _fixture(invalid_root)
    rows = _read_jsonl(invalid_paths["review_b"])
    rows[0]["polarity"] = "NEGATIVE"
    _write_jsonl(invalid_paths["review_b"], rows)
    with pytest.raises(ValueError, match="invalid polarity"):
        _build(invalid_paths)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("extra_v2_field", "invalid flat review fields"),
        ("empty_reason", "empty reason"),
        ("prediction_reason", "prohibited prediction or market text"),
        ("wrong_a_class", "review A review_class mismatch"),
        ("wrong_c_class", "review C review_class mismatch"),
        ("normalized_enum", "invalid materiality"),
    ],
)
def test_frozen_flat_review_schema_is_strict(
    tmp_path: Path, case: str, message: str
) -> None:
    paths, _ = _fixture(tmp_path)
    slot = "review_c" if case == "wrong_c_class" else "review_a"
    rows = _read_jsonl(paths[slot])
    if case == "extra_v2_field":
        rows[0]["impact_strength"] = "MODERATE"
    elif case == "empty_reason":
        rows[0]["reason"] = "   "
    elif case == "prediction_reason":
        rows[0]["reason"] = "The Qwen model prediction supplied this label."
    elif case == "wrong_a_class":
        rows[0]["review_class"] = (
            "INDEPENDENT_AI_ARBITRATION_NOT_HUMAN_GOLD"
        )
    elif case == "wrong_c_class":
        rows[0]["review_class"] = "INDEPENDENT_AI_REVIEW_NOT_HUMAN_GOLD"
    else:
        rows[0]["materiality"] = "material_adverse"
    _write_jsonl(paths[slot], rows)

    with pytest.raises(ValueError, match=message):
        _build(paths)
    assert not paths["output_dir"].exists()


def test_source_payload_and_prompt_hash_are_bound(tmp_path: Path) -> None:
    mismatch_root = tmp_path / "source-mismatch"
    mismatch_root.mkdir()
    mismatch_paths, _ = _fixture(mismatch_root)
    rows = _read_jsonl(mismatch_paths["source_only"])
    rows[0]["content"]["headline"] = "Changed source"
    _write_jsonl(mismatch_paths["source_only"], rows)
    with pytest.raises(ValueError, match="does not match clean DEV user payload"):
        _build(mismatch_paths)

    prompt_root = tmp_path / "prompt-mismatch"
    prompt_root.mkdir()
    prompt_paths, _ = _fixture(prompt_root)
    dev_rows = _read_jsonl(prompt_paths["dev_sft"])
    dev_rows[0]["messages"][0]["content"] += " changed"
    _write_jsonl(prompt_paths["dev_sft"], dev_rows)
    with pytest.raises(ValueError, match="system prompt text mismatch"):
        _build(prompt_paths)


def test_timezone_only_source_differences_match_the_same_instant(
    tmp_path: Path,
) -> None:
    paths, ids = _fixture(tmp_path)
    source_rows = _read_jsonl(paths["source_only"])
    for row in source_rows:
        row["content"]["as_of"] = "2026-08-30T08:00:00+08:00"
    source_rows[0]["content"]["passages"][0]["published_at"] = (
        "2026-08-30T09:30:00+08:00"
    )
    _write_jsonl(paths["source_only"], source_rows)

    manifest = _build(paths)
    output_rows = _read_jsonl(paths["output_dir"] / overlay.OUTPUT_NAME)

    equivalence = manifest["source_content_equivalence"]
    assert equivalence["contract_version"] == (
        overlay.SOURCE_CONTENT_EQUIVALENCE_CONTRACT_VERSION
    )
    assert equivalence["raw_match_count"] == 0
    assert equivalence["timezone_normalized_match_count"] == 138
    assert equivalence["normalized_time_keys"] == ["as_of", "published_at"]
    assert equivalence["published_at_date_only_preserved"] is True
    assert equivalence["naive_or_unparseable_datetime_allowed"] is False

    first_metadata = output_rows[0]["metadata"]
    row_equivalence = first_metadata["source_content_equivalence"]
    assert row_equivalence["match_method"] == "TIMEZONE_NORMALIZED"
    assert row_equivalence["raw_match_count"] == 0
    assert row_equivalence["timezone_normalized_match_count"] == 138
    assert row_equivalence["raw_source_content_sha256"] != (
        row_equivalence["raw_dev_content_sha256"]
    )
    assert first_metadata["source_payload_sha256"] == row_equivalence[
        "raw_source_content_sha256"
    ]

    dev_by_id = {
        row["metadata"]["sample_id"]: json.loads(row["messages"][1]["content"])
        for row in _read_jsonl(paths["dev_sft"])
    }
    normalized_first = overlay._normalize_content_timestamps(dev_by_id[ids[0]])
    assert normalized_first["as_of"] == "2026-08-30T00:00:00.000000+00:00"
    assert normalized_first["passages"][0]["published_at"] == (
        "2026-08-30T01:30:00.000000+00:00"
    )
    normalized_second = overlay._normalize_content_timestamps(dev_by_id[ids[1]])
    assert normalized_second["passages"][0]["published_at"] == "2026-08-30"
    expected_normalized_sha = hashlib.sha256(
        overlay.stable_json(normalized_first).encode("utf-8")
    ).hexdigest()
    assert row_equivalence["normalized_content_sha256"] == (
        expected_normalized_sha
    )
    normalized_index = [
        {
            "sample_id": row["metadata"]["sample_id"],
            "normalized_content_sha256": row["metadata"][
                "source_content_equivalence"
            ]["normalized_content_sha256"],
        }
        for row in output_rows
    ]
    assert equivalence["normalized_content_sha256_index_sha256"] == (
        hashlib.sha256(
            overlay.stable_json(normalized_index).encode("utf-8")
        ).hexdigest()
    )


@pytest.mark.parametrize(
    ("source_as_of", "message"),
    [
        (
            "2026-08-30T08:00:01+08:00",
            "does not match clean DEV user payload",
        ),
        (
            "2026-08-30T08:00:00",
            "strict timezone-aware ISO-8601 datetime",
        ),
        (
            "not-an-iso-datetime",
            "strict timezone-aware ISO-8601 datetime",
        ),
    ],
)
def test_source_time_difference_or_naive_datetime_fails_closed(
    tmp_path: Path, source_as_of: str, message: str
) -> None:
    paths, _ = _fixture(tmp_path)
    source_rows = _read_jsonl(paths["source_only"])
    source_rows[0]["content"]["as_of"] = source_as_of
    _write_jsonl(paths["source_only"], source_rows)

    with pytest.raises(ValueError, match=message):
        _build(paths)
    assert not paths["output_dir"].exists()


def test_source_only_rejects_nested_supervision_before_output(tmp_path: Path) -> None:
    paths, _ = _fixture(tmp_path)
    rows = _read_jsonl(paths["source_only"])
    rows[0]["content"]["nested"] = {"qwen_prediction": "ADVERSE"}
    _write_jsonl(paths["source_only"], rows)

    with pytest.raises(ValueError, match="prohibited supervision keys"):
        _build(paths)
    assert not paths["output_dir"].exists()


def test_source_only_rejects_nested_numeric_market_audit(tmp_path: Path) -> None:
    paths, _ = _fixture(tmp_path)
    rows = _read_jsonl(paths["source_only"])
    rows[0]["content"]["nested"] = {
        "price_reaction_audit": {"ret_30m": -0.12}
    }
    _write_jsonl(paths["source_only"], rows)

    with pytest.raises(ValueError, match="prohibited post-event supervision"):
        _build(paths)
    assert not paths["output_dir"].exists()


@pytest.mark.parametrize(
    "reason",
    (
        "The share price fell 18% after the disclosure, so the label is adverse.",
        "披露后股价下跌18%，所以应标记为负面。",
    ),
)
def test_review_reason_rejects_english_and_chinese_market_outcomes(
    tmp_path: Path, reason: str
) -> None:
    paths, _ = _fixture(tmp_path)
    rows = _read_jsonl(paths["review_a"])
    rows[0]["reason"] = reason
    _write_jsonl(paths["review_a"], rows)

    with pytest.raises(ValueError, match="prohibited prediction or market text"):
        _build(paths)
    assert not paths["output_dir"].exists()


def test_requires_exactly_138_dev_rows(tmp_path: Path) -> None:
    paths, _ = _fixture(tmp_path, count=137)
    with pytest.raises(ValueError, match="must contain exactly 138 rows"):
        _build(paths)


def test_existing_output_is_rejected_before_missing_inputs_are_read(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "existing"
    output_dir.mkdir()
    missing = tmp_path / "missing.jsonl"
    with pytest.raises(FileExistsError, match="output directory already exists"):
        overlay.build_overlay(
            dev_sft=missing,
            source_only=missing,
            review_a=missing,
            review_b=missing,
            review_c=missing,
            output_dir=output_dir,
        )


def test_atomic_publish_failure_leaves_no_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, _ = _fixture(tmp_path)
    original = overlay._write_new_file
    calls = 0

    def fail_second(path: Path, raw: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected write failure")
        original(path, raw)

    monkeypatch.setattr(overlay, "_write_new_file", fail_second)
    with pytest.raises(OSError, match="injected write failure"):
        _build(paths)

    assert not paths["output_dir"].exists()
    assert not list(tmp_path.glob(".overlay.*.tmp"))
