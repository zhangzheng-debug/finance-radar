from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.models.qwen_risk_contract import (
    derive_adverse_strength,
    expected_semantic_payload,
    validate_semantic_payload,
)
from scripts.prepare_qwen_risk_sft import prepare


def _content(text: str) -> dict:
    return {
        "as_of": "2026-08-01T00:00:00+00:00",
        "event_date": "2026-08-01",
        "headline": text,
        "summary": "Frozen source summary",
        "passages": [
            {
                "document_type": "8-K",
                "item_section": "1.03",
                "published_at": "2026-08-01",
                "passage": text + " exact source passage",
            }
        ],
        "target_label_hidden": True,
        "post_event_market_data_included": False,
        "model_output_included": False,
    }


def _row(index: int, split: str, materiality: str, polarity: str) -> dict:
    evidence_state = "PRIMARY_SUPPORTED"
    label = (
        "RISK_REVIEW"
        if materiality == "MATERIAL_ADVERSE" and polarity in {"ADVERSE", "MIXED"}
        else "NON_TARGET"
    )
    return {
        "sample_id": f"sample-{index}",
        "event_id": f"event-{index}",
        "text_sha256": hashlib.sha256(f"text-{index}".encode()).hexdigest(),
        "content": _content(f"Issuer disclosure {index}"),
        "source_id": f"source-{index}",
        "authority_tier": "P0",
        "entity_group": f"issuer:{index}",
        "event_chain_group": f"chain:{index}",
        "label": label,
        "materiality": materiality,
        "polarity": polarity,
        "evidence_state": evidence_state,
        "split": split,
    }


def _write_frozen(path: Path, rows: list[dict]) -> None:
    raw = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode()
    path.write_bytes(raw)
    path.with_suffix(path.suffix + ".sha256").write_text(
        f"{hashlib.sha256(raw).hexdigest()}  {path.name}\n", encoding="ascii"
    )


def test_strength_contract_does_not_invent_unlabelled_granularity() -> None:
    assert derive_adverse_strength("MATERIAL_ADVERSE", "ADVERSE") == "HIGH"
    assert derive_adverse_strength("NOT_MATERIAL_ADVERSE", "ADVERSE") == "LOW"
    assert derive_adverse_strength("NOT_MATERIAL_ADVERSE", "POSITIVE") == "NONE"
    assert derive_adverse_strength("UNCLEAR", "UNCLEAR") == "UNCLEAR"
    payload = expected_semantic_payload("MATERIAL_ADVERSE", "MIXED")
    assert payload["adverse_strength"] == "HIGH"
    assert payload["semantic_priority"] == "PRIORITY_REVIEW"
    assert validate_semantic_payload(payload) == []


def test_prepare_exports_messages_but_keeps_blind_content_and_labels_sealed(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "frozen.jsonl"
    _write_frozen(
        dataset,
        [
            _row(1, "TRAIN", "MATERIAL_ADVERSE", "ADVERSE"),
            _row(2, "VALIDATION", "NOT_MATERIAL_ADVERSE", "NEUTRAL"),
            _row(3, "HUMAN_BLIND", "MATERIAL_ADVERSE", "MIXED"),
        ],
    )
    output = tmp_path / "qwen"
    manifest = prepare(dataset, output)
    assert manifest["train_rows"] == 1
    assert manifest["validation_rows"] == 1
    assert manifest["human_blind_rows"] == 1
    assert manifest["human_blind_labels_exported"] is False
    assert manifest["human_blind_content_exported"] is False
    assert manifest["evidence_state_used_as_model_target"] is False

    train = json.loads((output / "qwen_risk_sft_train.jsonl").read_text(encoding="utf-8"))
    answer = json.loads(train["messages"][-1]["content"])
    assert answer == expected_semantic_payload("MATERIAL_ADVERSE", "ADVERSE")
    assert "evidence_state" not in answer
    blind_text = (output / "qwen_risk_blind_manifest.jsonl").read_text(encoding="utf-8")
    assert "Issuer disclosure 3" not in blind_text
    assert "MATERIAL_ADVERSE" not in blind_text


def test_prepare_keeps_source_only_semantics_but_never_exposes_evidence_state(
    tmp_path: Path,
) -> None:
    row = _row(4, "TRAIN", "MATERIAL_ADVERSE", "ADVERSE")
    row["evidence_state"] = "DISCOVERY_ONLY"
    row["label"] = "ABSTAIN"
    dataset = tmp_path / "source-only.jsonl"
    _write_frozen(dataset, [row])

    output = tmp_path / "qwen"
    manifest = prepare(dataset, output)

    assert manifest["train_rows"] == 1
    train_text = (output / "qwen_risk_sft_train.jsonl").read_text(encoding="utf-8")
    assert "DISCOVERY_ONLY" not in train_text
    prepared = json.loads(train_text)
    assert all("evidence_state" not in message["content"] for message in prepared["messages"])
    audit = json.loads(
        (output / "qwen_risk_evidence_posture_audit.jsonl").read_text(
            encoding="utf-8"
        )
    )
    assert audit["evidence_state"] == "DISCOVERY_ONLY"
    assert audit["qwen_training_included"] is True
    assert audit["evidence_state_exposed_to_model"] is False


def test_prepare_rejects_market_or_model_output_leakage(tmp_path: Path) -> None:
    row = _row(1, "TRAIN", "MATERIAL_ADVERSE", "ADVERSE")
    row["content"]["post_event_market_data_included"] = True
    dataset = tmp_path / "bad.jsonl"
    _write_frozen(dataset, [row])
    with pytest.raises(ValueError, match="review boundary violation"):
        prepare(dataset, tmp_path / "out")
