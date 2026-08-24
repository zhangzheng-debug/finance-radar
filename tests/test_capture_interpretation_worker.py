from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.run_capture_interpretation_worker as worker
import scripts.run_capture_interpretation_deepseek as single_job
from app.services.capture_interpretation import (
    CAPTURE_INTERPRETATION_CONTRACT,
    CAPTURE_INTERPRETATION_PROMPT_SHA256,
    CAPTURE_INTERPRETATION_PROMPT_VERSION,
)
from app.services.deepseek_capture_interpretation import DEEPSEEK_CHEAP_TEXT_MODEL
from scripts.run_capture_interpretation_deepseek import RUN_CACHED, RUN_COMPLETED
from scripts.run_capture_interpretation_worker import (
    candidates,
    classify_run_code,
    is_current_terminal,
    process_pending_items,
)


def test_worker_only_selects_nonempty_live_zero_evidence_capture_buckets() -> None:
    plan = {
        "records": [
            {
                "event": {"event_id": "p2"},
                "bucket": "P2_CAPTURE_ONLY",
                "captures": [{"observation_id": "obs-p2", "capture_receipt_sha256": "a" * 64, "title": "retained title", "observation_status": "captured"}],
            },
            {
                "event": {"event_id": "raw"},
                "bucket": "NO_URL_RAW_ONLY",
                "captures": [{"observation_id": "obs-raw", "capture_receipt_sha256": "b" * 64, "summary": "retained summary", "observation_status": "captured"}],
            },
            {
                "event": {"event_id": "official"},
                "bucket": "OFFICIAL_REFETCH_READY",
                "captures": [{"observation_id": "obs-official", "capture_receipt_sha256": "c" * 64, "title": "official", "observation_status": "captured"}],
            },
            {
                "event": {"event_id": "deleted"},
                "bucket": "NO_URL_RAW_ONLY",
                "captures": [{"observation_id": "obs-deleted", "capture_receipt_sha256": "d" * 64, "title": "deleted", "observation_status": "deleted"}],
            },
        ]
    }

    selected = candidates(plan)
    assert [item["event_id"] for item in selected] == ["raw", "p2"]


def test_cached_single_job_does_not_consume_batch_completion_limit() -> None:
    assert classify_run_code(RUN_COMPLETED) == "COMPLETED"
    assert classify_run_code(RUN_CACHED) == "CACHED"
    assert classify_run_code(99) == "FAILED"


def test_worker_only_skips_terminal_result_for_current_generation() -> None:
    current = {
        "status": "COMPLETED",
        "contract_version": CAPTURE_INTERPRETATION_CONTRACT,
        "prompt_version": CAPTURE_INTERPRETATION_PROMPT_VERSION,
        "prompt_sha256": CAPTURE_INTERPRETATION_PROMPT_SHA256,
        "provider": "deepseek",
        "model_snapshot": DEEPSEEK_CHEAP_TEXT_MODEL,
    }
    assert is_current_terminal(current) is True
    assert is_current_terminal({**current, "status": "FAILED"}) is True
    assert is_current_terminal({**current, "status": "PENDING"}) is False
    assert is_current_terminal({**current, "prompt_version": "stale-prompt"}) is False
    assert is_current_terminal({**current, "prompt_sha256": "0" * 64}) is False


def test_worker_overlaps_only_a_bounded_number_of_independent_receipts(
    monkeypatch,
    tmp_path,
) -> None:
    lock = threading.Lock()
    active = 0
    peak = 0

    def fake_process(item, env_file):
        nonlocal active, peak
        assert env_file == tmp_path / "capture.env"
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return "COMPLETED"

    monkeypatch.setattr(worker, "process_pending_item", fake_process)
    items = [
        {"event_id": f"event-{index}", "observation_id": f"obs-{index}"}
        for index in range(6)
    ]

    outcomes = process_pending_items(
        items,
        tmp_path / "capture.env",
        workers=3,
    )

    assert outcomes == ["COMPLETED"] * 6
    assert 2 <= peak <= 3


def test_worker_advances_a_bounded_inventory_and_then_uses_generation_shortcut(
    monkeypatch,
    tmp_path,
) -> None:
    items = [
        {
            "event_id": f"event-{index}",
            "event_version": 1,
            "observation_id": f"obs-{index}",
            "capture_receipt_sha256": str(index) * 64,
            "bucket": "P2_CAPTURE_ONLY",
        }
        for index in range(1, 4)
    ]

    class FakeLedger:
        calls: list[tuple[int, int]] = []

        def capture_source_generation(self):
            return {"observation_count": 3, "revision_count": 0}

        def capture_interpretation_candidate_count(self):
            return len(items)

        def capture_interpretation_candidates(self, *, limit: int, offset: int):
            self.calls.append((limit, offset))
            return items[offset : offset + limit]

    class FakeOperations:
        def __init__(self) -> None:
            self.state = {}
            self.terminal: set[tuple[str, str, int]] = set()

        def get_state(self, key, default=None):
            return self.state.get(key, default)

        def set_state(self, key, value):
            self.state[key] = value

        def capture_interpretation_queue_health(self, *args, **kwargs):
            return {"by_status": {"COMPLETED": len(self.terminal)}, "daily": {}}

        def capture_interpretation_terminal_keys(self, **kwargs):
            return {key: "COMPLETED" for key in self.terminal}

    ledger = FakeLedger()
    operations = FakeOperations()

    monkeypatch.setattr(worker, "load_local_env", lambda path: None)
    monkeypatch.setattr(
        worker.Settings,
        "from_env",
        classmethod(
            lambda cls: SimpleNamespace(
                ledger_db=tmp_path / "ledger.sqlite3",
                operations_db=tmp_path / "operations.sqlite3",
            )
        ),
    )
    monkeypatch.setattr(worker, "LedgerRepository", lambda path: ledger)
    monkeypatch.setattr(worker, "OperationsRepository", lambda path: operations)

    def complete(selected, env_file, *, workers):
        operations.terminal.update(
            (
                item["event_id"],
                item["capture_receipt_sha256"],
                int(item["event_version"]),
            )
            for item in selected
        )
        return ["COMPLETED"] * len(selected)

    monkeypatch.setattr(worker, "process_pending_items", complete)
    args = SimpleNamespace(
        env_file=Path(tmp_path / "capture.env"),
        limit=2,
        scan_limit=2,
        workers=2,
    )

    assert worker.run(args) == 0
    first_state = operations.state[worker.INVENTORY_STATE_KEY]
    assert first_state["next_offset"] == 2
    assert first_state["backlog_complete"] is False

    assert worker.run(args) == 0
    second_state = operations.state[worker.INVENTORY_STATE_KEY]
    assert second_state["next_offset"] == 0
    assert second_state["backlog_complete"] is True
    calls_before_idle = list(ledger.calls)

    assert worker.run(args) == 0
    assert ledger.calls == calls_before_idle
    assert len(operations.terminal) == 3


def test_single_job_rejects_evidence_event_before_provider_or_enqueue(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(single_job, "load_local_env", lambda path: None)
    monkeypatch.setattr(single_job, "_credential", lambda: "test-only")
    monkeypatch.setattr(
        single_job.Settings,
        "from_env",
        classmethod(
            lambda cls: SimpleNamespace(
                capture_llm_enabled=True,
                capture_llm_provider="deepseek",
                capture_llm_model=DEEPSEEK_CHEAP_TEXT_MODEL,
                capture_llm_base_url="https://api.deepseek.com",
                ledger_db=tmp_path / "ledger.sqlite3",
                operations_db=tmp_path / "operations.sqlite3",
            )
        ),
    )

    class EvidenceLedger:
        def capture_interpretation_eligibility(self, event_id, *, observation_id=None):
            return {"eligible": False, "reason_code": "EVIDENCE_PRESENT"}

    monkeypatch.setattr(single_job, "LedgerRepository", lambda path: EvidenceLedger())
    monkeypatch.setattr(single_job, "OperationsRepository", lambda path: object())
    monkeypatch.setattr(
        single_job,
        "DeepSeekCaptureInterpretationProvider",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("provider must not start")),
    )

    with pytest.raises(RuntimeError, match="EVIDENCE_PRESENT"):
        single_job.run(
            SimpleNamespace(
                env_file=Path(tmp_path / "capture.env"),
                event_id="event-with-evidence",
                observation_id="obs-1",
            )
        )
