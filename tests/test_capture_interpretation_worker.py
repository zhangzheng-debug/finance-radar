from __future__ import annotations

from scripts.run_capture_interpretation_deepseek import RUN_CACHED, RUN_COMPLETED
from scripts.run_capture_interpretation_worker import candidates, classify_run_code


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
