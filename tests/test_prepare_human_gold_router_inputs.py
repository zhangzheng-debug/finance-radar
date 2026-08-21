from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.prepare_human_gold_router_inputs import prepare


def row(index: int, split: str, label: str) -> dict:
    axes = {
        "RISK_REVIEW": ("MATERIAL_ADVERSE", "ADVERSE", "PRIMARY_SUPPORTED"),
        "NON_TARGET": ("NOT_MATERIAL_ADVERSE", "NEUTRAL", "PRIMARY_SUPPORTED"),
        "ABSTAIN": ("UNCLEAR", "UNCLEAR", "INSUFFICIENT"),
    }[label]
    return {
        "sample_id": f"sample-{index}",
        "event_id": f"event-{index}",
        "text_sha256": hashlib.sha256(f"text-{index}".encode()).hexdigest(),
        "content": {
            "headline": f"Headline {index}",
            "summary": f"Summary {index}",
            "passages": [{"passage": f"Passage {index}"}],
            "post_event_market_data_included": False,
            "model_output_included": False,
            "target_label_hidden": True,
        },
        "entity_group": f"issuer-{index}",
        "event_chain_group": f"chain-{index}",
        "split": split,
        "label": label,
        "materiality": axes[0],
        "polarity": axes[1],
        "evidence_state": axes[2],
    }


def test_prepare_never_exports_blind_labels_or_content(tmp_path: Path) -> None:
    frozen = tmp_path / "frozen.jsonl"
    rows = [
        row(1, "TRAIN", "RISK_REVIEW"),
        row(2, "VALIDATION", "NON_TARGET"),
        row(3, "TRAIN", "ABSTAIN"),
        row(4, "HUMAN_BLIND", "RISK_REVIEW"),
    ]
    frozen.write_text("".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8")
    digest = hashlib.sha256(frozen.read_bytes()).hexdigest()
    frozen.with_suffix(".jsonl.sha256").write_text(f"{digest}  {frozen.name}\n", encoding="ascii")

    manifest = prepare(frozen, tmp_path / "output")
    assert manifest["development_rows"] == 2
    assert manifest["abstain_gate_rows"] == 1
    assert manifest["human_blind_labels_exported"] is False
    blind_text = (tmp_path / "output" / "human_gold_blind_manifest.jsonl").read_text()
    assert "RISK_REVIEW" not in blind_text
    assert "Headline 4" not in blind_text
    assert "content_sha256" in blind_text
