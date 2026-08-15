from __future__ import annotations

import tempfile
from pathlib import Path

from app.services.shadow_runner import run_shadow_batch
from app.storage import OperationsRepository


class FakeLedger:
    def list_events(self, *, limit: int):
        return {"items": [{"event_id": "evt-1"}], "total": 1, "limit": limit, "offset": 0}

    def event_detail(self, event_id: str):
        assert event_id == "evt-1"
        return {
            "event": {
                "event_id": event_id,
                "current_version": 2,
                "status": "candidate",
                "company_name": "Example Corp",
            },
            "current_version": {"facts": {"source_summary": "An official filing."}},
            "preferred_source": {"title": "8-K", "summary": "An official filing."},
        }

    def event_evidence(self, event_id: str):
        assert event_id == "evt-1"
        return [
            {
                "evidence_id": "evidence-1",
                "evidence_status": "confirmed_primary",
                "authority_tier": "P0",
                "source_id": "sec_current_filings",
                "evidence_passage": "The company filed a voluntary Chapter 11 petition.",
            }
        ]


class FakeRouter:
    def predict(self, text: str, evidence_context):
        assert "Chapter 11" in text
        assert evidence_context["state"] == "PRIMARY_SUPPORTED_REVIEWED"
        return {
            "label": "RISK_REVIEW",
            "confidence": 0.91,
            "probabilities": {"RISK_REVIEW": 0.91, "NON_TARGET": 0.09},
            "model_version": "test-router-v1",
            "runtime": "trained_semantic_artifact",
            "decision_source": "TRAINED_SEMANTIC_MODEL",
            "semantic_model_invoked": True,
            "confidence_applicable": True,
            "shadow": True,
            "no_trading": True,
            "input_sha256": "a" * 64,
            "latency_ms": 1.2,
        }


def test_shadow_batch_persists_once_per_event_version_and_input() -> None:
    with tempfile.TemporaryDirectory() as directory:
        operations = OperationsRepository(Path(directory) / "operations.sqlite3")
        first = run_shadow_batch(FakeLedger(), operations, FakeRouter())
        second = run_shadow_batch(FakeLedger(), operations, FakeRouter())
        rows = operations.model_runs("evt-1")
    assert first["recorded"] == 1
    assert first["by_execution_status"] == {"MODEL_EXECUTED": 1}
    assert second["recorded"] == 0
    assert second["already_current"] == 1
    assert len(rows) == 1
    assert rows[0]["output"]["event_version"] == 2
    assert rows[0]["output"]["execution_status"] == "MODEL_EXECUTED"
