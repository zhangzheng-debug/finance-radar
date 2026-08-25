from __future__ import annotations

import hashlib
from pathlib import Path

from app.models.qwen_risk_contract import (
    expected_semantic_payload,
    normalize_qwen_risk_content,
)
from app.services.qwen_risk_semantics import QwenRiskModelProvider
from app.services.qwen_risk_worker import run_qwen_risk_batch
from app.storage import OperationsRepository


def _item(index: int) -> dict:
    event_id = f"event-{index}"
    return {
        "detail": {
            "event": {
                "event_id": event_id,
                "current_version": 1,
                "status": "candidate",
                "event_date": "2026-08-01",
                "last_updated_at": "2026-08-01T01:00:00+00:00",
            },
            "current_version": {"facts": {}},
            "preferred_source": {"title": event_id, "summary": "issuer may default"},
        },
        "evidence": [],
    }


class Ledger:
    def __init__(self) -> None:
        self.items = [_item(index) for index in range(6)]

    def shadow_batch(self, *, limit: int, offset: int = 0, order: str = "latest"):
        values = list(reversed(self.items)) if order == "latest" else self.items
        return values[offset : offset + limit]


class Provider:
    model_version = "qwen-risk-" + "a" * 16

    def input_contract(self, detail, evidence):
        event_id = detail["event"]["event_id"]
        return {
            "input_sha256": hashlib.sha256(event_id.encode()).hexdigest(),
            "model_version": self.model_version,
        }

    def assess(self, detail, evidence):
        contract = self.input_contract(detail, evidence)
        return {
            **expected_semantic_payload("MATERIAL_ADVERSE", "ADVERSE"),
            **contract,
            "model_task": "QWEN_RISK_SEMANTICS",
            "contract_version": "qwen-risk-semantics-v1",
            "prompt_version": "qwen-risk-human-gold-sft-v1",
            "assessment_scope": "SOURCE_CONDITIONAL",
            "event_version": 1,
            "event_status": "candidate",
            "label": "PRIORITY_REVIEW",
            "confidence": 0.0,
            "confidence_applicable": False,
            "decision_source": "HUMAN_GOLD_TRAINED_QWEN",
            "call_kind": "QWEN_RISK_SEMANTICS",
            "latency_ms": 1.0,
            "shadow": True,
            "no_trading": True,
        }


def test_provider_contract_is_conditional_without_current_primary_evidence() -> None:
    provider = QwenRiskModelProvider(
        "http://127.0.0.1:18602",
        "qwen-risk-test",
        "a" * 64,
        request_fn=lambda *args, **kwargs: None,
    )
    contract = provider.input_contract(_item(1)["detail"], [])
    assert contract["assessment_scope"] == "SOURCE_CONDITIONAL"
    assert contract["model_version"].startswith("qwen-risk-")
    assert len(contract["input_sha256"]) == 64


def test_training_and_runtime_share_one_canonical_content_shape() -> None:
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": expected_semantic_payload(
                                "MATERIAL_ADVERSE", "ADVERSE"
                            )
                        }
                    }
                ]
            }

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return Response()

    provider = QwenRiskModelProvider(
        "http://127.0.0.1:18602",
        "qwen-risk-test",
        "a" * 64,
        request_fn=request,
    )
    content = {
        "headline": "  Issuer   may default  ",
        "summary": "  source   summary ",
        "passages": [{"passage": "  exact   source text  "}],
    }
    prediction, _latency = provider.predict_content(content)
    assert prediction == expected_semantic_payload("MATERIAL_ADVERSE", "ADVERSE")
    request_content = calls[0][1]["json"]["messages"][1]["content"]
    import json

    assert json.loads(request_content) == normalize_qwen_risk_content(content)


def test_qwen_worker_persists_without_reprocessing_current_input(tmp_path: Path) -> None:
    operations = OperationsRepository(tmp_path / "operations.sqlite3")
    first = run_qwen_risk_batch(Ledger(), operations, Provider(), scan_limit=4, run_limit=2)
    second = run_qwen_risk_batch(Ledger(), operations, Provider(), scan_limit=4, run_limit=2)
    selected = operations.latest_qwen_risk_runs_for_versions(
        {f"event-{index}": 1 for index in range(6)}
    )
    assert first["recorded"] == 2
    assert second["already_current"] >= 1
    assert all(row["output"]["model_task"] == "QWEN_RISK_SEMANTICS" for row in selected.values())
    assert first["independent_from_collection"] is True
