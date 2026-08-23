from __future__ import annotations

import hashlib
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


class FairLedger:
    def __init__(self, count: int = 8) -> None:
        self.items = [self._item(index) for index in range(count)]

    @staticmethod
    def _item(index: int):
        event_id = f"evt-{index:02d}"
        return {
            "detail": {
                "event": {
                    "event_id": event_id,
                    "current_version": 1,
                    "status": "candidate",
                    "company_name": f"Company {index}",
                },
                "current_version": {"facts": {"source_summary": event_id}},
                "preferred_source": {"title": event_id, "summary": event_id},
            },
            "evidence": [],
        }

    def shadow_batch(self, *, limit: int, offset: int = 0, order: str = "latest"):
        ordered = list(reversed(self.items)) if order == "latest" else list(self.items)
        return ordered[offset : offset + limit]


class FairRouter:
    def predict(self, text: str, evidence_context):
        return {
            "label": "ABSTAIN",
            "confidence": 0.0,
            "probabilities": {"RISK_REVIEW": 0.0, "NON_TARGET": 0.0},
            "model_version": "fair-router-v1",
            "runtime": "structured_evidence_gate",
            "decision_source": "DETERMINISTIC_EVIDENCE_GATE",
            "semantic_model_invoked": False,
            "confidence_applicable": False,
            "shadow": True,
            "no_trading": True,
            "input_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "latency_ms": 0.1,
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


def test_shadow_batch_round_robin_lane_eventually_reaches_old_history() -> None:
    with tempfile.TemporaryDirectory() as directory:
        operations = OperationsRepository(Path(directory) / "operations.sqlite3")
        ledger = FairLedger(count=8)
        results = [
            run_shadow_batch(
                ledger,
                operations,
                FairRouter(),
                scan_limit=4,
                run_limit=2,
            )
            for _ in range(8)
        ]
        rows = operations.model_runs(limit=200)
        cursor = operations.get_state("shadow_router_fair_cursor_v1")

    assert {row["event_id"] for row in rows} == {
        f"evt-{index:02d}" for index in range(8)
    }
    assert results[0]["selection"]["recent_loaded"] == 2
    assert results[0]["selection"]["fair_loaded"] == 2
    assert results[0]["selection"]["fair_examined"] == 1
    assert results[0]["input_loader"] == "fair_recent_bulk_v3"
    assert isinstance(cursor["next_offset"], int)
