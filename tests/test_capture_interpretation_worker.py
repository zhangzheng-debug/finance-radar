from __future__ import annotations

import threading
import time

import scripts.run_capture_interpretation_worker as worker
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
