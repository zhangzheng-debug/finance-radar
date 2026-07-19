from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.capture_runtime_evidence import (
    append_snapshot,
    build_snapshot,
    capture_once,
    validate_history,
)


def _health(*, complete: bool = False) -> dict:
    return {
        "status": "ok",
        "ledger": {
            "quick_check": "ok",
            "counts": {"sources": 22, "raw_observations": 100, "canonical_events": 10},
            "audit": {
                "trading_boundary_violations": 0,
                "auto_verification_violations": 0,
                "market_feature_leakage_violations": 0,
            },
        },
        "operations": {
            "quick_check": "ok",
            "counts": {"worker_cycles": 300},
            "latest_worker_cycle": {
                "cycle_id": "cycle-1",
                "status": "SUCCESS",
                "started_at": "2026-07-18T12:00:00+00:00",
                "finished_at": "2026-07-18T12:00:03+00:00",
                "error": None,
            },
            "latest_backup": {"status": "VERIFIED"},
            "worker_window_24h": {
                "complete": complete,
                "status": "PASS" if complete else "PARTIAL",
                "observed_hours": 24.1 if complete else 5.0,
                "cycles": 300,
                "success_cycles": 300 if complete else 295,
                "degraded_cycles": 0 if complete else 2,
                "failed_cycles": 0 if complete else 3,
                "success_rate": 1.0 if complete else 0.983333,
            },
        },
        "model": {"shadow": True, "no_trading": True},
    }


def test_snapshot_waits_for_complete_runtime_window() -> None:
    snapshot = build_snapshot(
        _health(complete=False),
        api_base="https://example.test/api",
        captured_at=datetime(2026, 7, 18, 13, tzinfo=timezone.utc),
        known_last_non_success=datetime(2026, 7, 18, 12, tzinfo=timezone.utc),
    )
    assert snapshot["status"] == "WAITING"
    assert snapshot["checks"]["runtime_window_complete"] is False
    assert snapshot["known_earliest_possible_pass"] == "2026-07-19T12:00:00+00:00"


def test_hash_chain_detects_history_tampering(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    first = append_snapshot(history, {"status": "WAITING", "captured_at": "one"})
    second = append_snapshot(history, {"status": "PASS", "captured_at": "two"})
    assert second["previous_record_sha256"] == first["record_sha256"]
    assert validate_history(history)[0] == 2
    rows = history.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(rows[0])
    tampered["status"] = "PASS"
    rows[0] = json.dumps(tampered)
    history.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_history(history)


def test_capture_writes_latest_history_and_markdown(tmp_path: Path) -> None:
    record = capture_once(
        api_base="https://example.test/api",
        output_dir=tmp_path,
        known_last_non_success=None,
        fetcher=lambda _: _health(complete=True),
    )
    assert record["status"] == "PASS"
    assert (tmp_path / "runtime_gate_latest.json").is_file()
    assert (tmp_path / "runtime_gate_latest.md").is_file()
    assert "Gate: **PASS**" in (tmp_path / "runtime_gate_latest.md").read_text(encoding="utf-8")
