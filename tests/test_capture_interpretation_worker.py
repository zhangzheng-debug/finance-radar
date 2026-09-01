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
from app.storage.operations import OperationsRepository
from scripts.run_capture_interpretation_deepseek import RUN_CACHED, RUN_COMPLETED
from scripts.run_capture_interpretation_worker import (
    candidates,
    classify_run_code,
    current_persisted_pending_requests,
    is_current_terminal,
    prepare_persisted_pending_requests,
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


def test_persisted_pending_reader_selects_only_ready_current_generation() -> None:
    class FakeOperations:
        def capture_interpretation_pending_runs(self, **kwargs):
            assert kwargs == {
                "provider": "deepseek",
                "contract_version": CAPTURE_INTERPRETATION_CONTRACT,
                "prompt_version": CAPTURE_INTERPRETATION_PROMPT_VERSION,
                "prompt_sha256": CAPTURE_INTERPRETATION_PROMPT_SHA256,
                "model_snapshot": DEEPSEEK_CHEAP_TEXT_MODEL,
                "available_before": "2026-09-01T00:01:00+00:00",
                "max_attempts": worker.MAX_ATTEMPTS,
                "limit": 10,
            }
            return [{"interpretation_id": "ready"}]

    selected = current_persisted_pending_requests(
        FakeOperations(),
        limit=10,
        now=worker.datetime.fromisoformat("2026-09-01T00:01:00+00:00"),
    )

    assert [item["interpretation_id"] for item in selected] == ["ready"]


def test_pending_sql_is_not_starved_by_500_terminal_failures(
    tmp_path,
) -> None:
    operations = OperationsRepository(tmp_path / "operations.sqlite3")
    normalized = {
        "capture_receipt_sha256": "a" * 64,
        "semantic_content_sha256": "b" * 64,
        "input_sha256": "c" * 64,
    }
    run_id, inserted = operations.enqueue_capture_interpretation(
        "event-on-demand",
        "observation-on-demand",
        normalized,
        contract_version=CAPTURE_INTERPRETATION_CONTRACT,
        prompt_version=CAPTURE_INTERPRETATION_PROMPT_VERSION,
        prompt_sha256=CAPTURE_INTERPRETATION_PROMPT_SHA256,
        provider="deepseek",
        model_snapshot=DEEPSEEK_CHEAP_TEXT_MODEL,
        external_call=True,
    )
    duplicate_id, duplicate_inserted = operations.enqueue_capture_interpretation(
        "event-on-demand",
        "observation-on-demand",
        normalized,
        contract_version=CAPTURE_INTERPRETATION_CONTRACT,
        prompt_version=CAPTURE_INTERPRETATION_PROMPT_VERSION,
        prompt_sha256=CAPTURE_INTERPRETATION_PROMPT_SHA256,
        provider="deepseek",
        model_snapshot=DEEPSEEK_CHEAP_TEXT_MODEL,
        external_call=True,
    )

    assert inserted is True
    assert duplicate_inserted is False
    assert duplicate_id == run_id

    connection = operations.connect()
    connection.executemany(
        """INSERT INTO capture_interpretation_runs(
               interpretation_id,event_id,observation_id,capture_receipt_sha256,
               semantic_content_sha256,input_sha256,contract_version,prompt_version,
               prompt_sha256,provider,model_snapshot,status,output_json,guardrails_json,
               usage_json,external_call,canonical_mutation_allowed,no_trading,
               idempotency_key,created_at,updated_at,error
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'FAILED','{}','{}','{}',1,0,1,?,?,?,?)""",
        [
            (
                f"terminal-{index}",
                f"event-terminal-{index}",
                f"observation-terminal-{index}",
                "d" * 64,
                "e" * 64,
                f"{index + 1:064x}",
                CAPTURE_INTERPRETATION_CONTRACT,
                CAPTURE_INTERPRETATION_PROMPT_VERSION,
                CAPTURE_INTERPRETATION_PROMPT_SHA256,
                "deepseek",
                DEEPSEEK_CHEAP_TEXT_MODEL,
                f"terminal-idempotency-{index}",
                "9999-01-01T00:00:00+00:00",
                "9999-01-01T00:00:00+00:00",
                "terminal failure",
            )
            for index in range(505)
        ],
    )
    connection.commit()
    connection.close()

    assert [
        row["interpretation_id"]
        for row in current_persisted_pending_requests(operations, limit=10)
    ] == [run_id]
    assert operations.capture_interpretation_active_keys(
        provider="deepseek",
        contract_version=CAPTURE_INTERPRETATION_CONTRACT,
        prompt_version=CAPTURE_INTERPRETATION_PROMPT_VERSION,
        prompt_sha256=CAPTURE_INTERPRETATION_PROMPT_SHA256,
        model_snapshot=DEEPSEEK_CHEAP_TEXT_MODEL,
    ) == {("event-on-demand", "a" * 64)}

    operations.fail_capture_interpretation(run_id, "terminal contract failure")
    assert current_persisted_pending_requests(operations, limit=10) == []


def test_persisted_pending_is_bound_to_current_capture_and_stale_rows_fail() -> None:
    event = {
        "event_id": "event-current",
        "current_version": 2,
        "event_family": "macro_policy",
        "event_type": "sanctions",
    }
    capture = {
        "observation_id": "obs-current",
        "source_name": "OpenNews",
        "source_type": "aggregated_discovery",
        "authority_tier": "P2_experimental",
        "title": "A retained source title for interpretation.",
        "summary": "A retained source summary for interpretation.",
        "semantic_content_sha256": "a" * 64,
        "capture_receipt_sha256": "b" * 64,
        "latest_revision_no": 1,
    }
    normalized = worker.normalized_capture_input(event, capture)
    rows = [
        {
            "interpretation_id": "current-run",
            "event_id": "event-current",
            "observation_id": "obs-current",
            "capture_receipt_sha256": normalized["capture_receipt_sha256"],
            "input_sha256": normalized["input_sha256"],
        },
        {
            "interpretation_id": "stale-run",
            "event_id": "event-stale",
            "observation_id": "obs-stale",
            "capture_receipt_sha256": "c" * 64,
            "input_sha256": "d" * 64,
        },
    ]

    class FakeLedger:
        def capture_interpretation_eligibility(self, event_id, *, observation_id=None):
            if event_id == "event-current":
                return {"eligible": True, "reason_code": "NO_EVENT_EVIDENCE", "bucket": "P2_CAPTURE_ONLY"}
            return {"eligible": False, "reason_code": "EVIDENCE_PRESENT"}

        def capture_interpretation_context(self, event_id, observation_id):
            assert (event_id, observation_id) == ("event-current", "obs-current")
            return {"event": event, "capture": capture}

    class FakeOperations:
        def __init__(self):
            self.failures = []

        def fail_capture_interpretation(self, interpretation_id, error):
            self.failures.append((interpretation_id, error))

    operations = FakeOperations()
    prepared, rejected = prepare_persisted_pending_requests(
        FakeLedger(),
        operations,
        rows,
        limit=2,
    )

    assert [item["interpretation_id"] for item in prepared] == ["current-run"]
    assert rejected == 1
    assert operations.failures == [
        ("stale-run", "CAPTURE_INTERPRETATION_NOT_ELIGIBLE:EVIDENCE_PRESENT")
    ]


def test_worker_executes_persisted_request_before_inventory_scan_work(
    monkeypatch,
    tmp_path,
) -> None:
    inventory_item = {
        "event_id": "inventory-event",
        "event_version": 1,
        "observation_id": "inventory-observation",
        "capture_receipt_sha256": "e" * 64,
        "bucket": "P2_CAPTURE_ONLY",
        "bucket_priority": 1,
    }
    priority_item = {
        "interpretation_id": "priority-run",
        "event_id": "priority-event",
        "observation_id": "priority-observation",
        "capture_receipt_sha256": "f" * 64,
        "scheduler_lane": "persisted_pending",
    }

    class FakeLedger:
        def capture_source_generation(self):
            return {"observation_count": 2}

        def capture_interpretation_candidate_count(self):
            return 1

        def capture_interpretation_candidates(self, **kwargs):
            return [dict(inventory_item)]

    class FakeOperations:
        def __init__(self):
            self.state = {}

        def get_state(self, key, default=None):
            return self.state.get(key, default)

        def set_state(self, key, value):
            self.state[key] = value

        def capture_interpretation_terminal_keys(self, **kwargs):
            return {}

        def capture_interpretation_queue_health(self, *args, **kwargs):
            return {"by_status": {}, "daily": {}}

        def capture_interpretation_active_keys(self, **kwargs):
            return set()

        def capture_interpretation_priority_runs(self, **kwargs):
            return []

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
    priority_reads = 0

    def read_priority(repository, *, limit):
        nonlocal priority_reads
        priority_reads += 1
        return [{"interpretation_id": "priority-run"}] if priority_reads == 1 else []

    monkeypatch.setattr(worker, "current_public_priority_requests", read_priority)
    monkeypatch.setattr(
        worker,
        "current_persisted_pending_requests",
        lambda repository, *, limit: [
            {"interpretation_id": f"old-pending-{index}"} for index in range(24)
        ],
    )
    monkeypatch.setattr(
        worker,
        "prepare_persisted_pending_requests",
        lambda ledger_repository, operations_repository, rows, *, limit: (
            [priority_item] if rows else [],
            0,
        ),
    )
    batches = []

    def complete(items, env_file, *, workers):
        batches.append([str(item.get("scheduler_lane") or "") for item in items])
        return ["COMPLETED"] * len(items)

    monkeypatch.setattr(worker, "process_pending_items", complete)
    args = SimpleNamespace(
        env_file=Path(tmp_path / "capture.env"),
        limit=2,
        scan_limit=2,
        workers=1,
    )

    assert worker.run(args) == 0
    assert batches == [["persisted_pending"], ["recent"]]


def test_ten_second_wakeup_only_consumes_public_priority_before_scan_gate(
    monkeypatch,
    tmp_path,
) -> None:
    priority_item = {
        "interpretation_id": "priority-now",
        "event_id": "event-now",
        "observation_id": "obs-now",
        "capture_receipt_sha256": "a" * 64,
        "scheduler_lane": "persisted_pending",
    }

    class FakeLedger:
        inventory_calls = 0

        def capture_source_generation(self):
            return {"observation_count": 1}

        def capture_interpretation_candidate_count(self):
            return 1

        def capture_interpretation_candidates(self, **kwargs):
            self.inventory_calls += 1
            return []

    class FakeOperations:
        def __init__(self):
            self.state = {
                worker.INVENTORY_STATE_KEY: {
                    "last_inventory_scan_at": worker.datetime.now(
                        worker.timezone.utc
                    ).isoformat(),
                    "fair_cursor": None,
                }
            }

        def get_state(self, key, default=None):
            return self.state.get(key, default)

        def set_state(self, key, value):
            self.state[key] = value

        def capture_interpretation_active_keys(self, **kwargs):
            return set()

        def capture_interpretation_terminal_keys(self, **kwargs):
            return {}

        def capture_interpretation_queue_health(self, *args, **kwargs):
            return {"by_status": {}, "daily": {}}

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
    reads = 0

    def priority(repository, *, limit):
        nonlocal reads
        reads += 1
        return [{"interpretation_id": "priority-now"}] if reads == 1 else []

    monkeypatch.setattr(worker, "current_public_priority_requests", priority)
    monkeypatch.setattr(
        worker,
        "prepare_persisted_pending_requests",
        lambda *args, **kwargs: ([priority_item], 0),
    )
    monkeypatch.setattr(
        worker,
        "process_pending_items",
        lambda items, env_file, *, workers: ["COMPLETED"],
    )
    args = SimpleNamespace(
        env_file=Path(tmp_path / "capture.env"),
        limit=20,
        scan_limit=500,
        workers=3,
    )

    assert worker.run(args) == 0
    assert ledger.inventory_calls == 0
    assert operations.state[worker.INVENTORY_STATE_KEY]["inventory_scan_due"] is False
    assert operations.state[worker.INVENTORY_STATE_KEY]["public_priority_examined"] == 1


def test_priority_arriving_during_background_wave_gets_next_slot(
    monkeypatch,
    tmp_path,
) -> None:
    background = [
        {
            "interpretation_id": f"old-{index}",
            "event_id": f"old-event-{index}",
            "observation_id": f"old-obs-{index}",
            "capture_receipt_sha256": str(index + 1) * 64,
            "scheduler_lane": "persisted_pending",
        }
        for index in range(3)
    ]
    public = {
        "interpretation_id": "public-new",
        "event_id": "public-event",
        "observation_id": "public-obs",
        "capture_receipt_sha256": "f" * 64,
        "scheduler_lane": "persisted_pending",
    }

    class FakeLedger:
        def capture_source_generation(self):
            return {"observation_count": 4}

        def capture_interpretation_candidate_count(self):
            return 0

        def capture_interpretation_candidates(self, **kwargs):
            return []

    class FakeOperations:
        def __init__(self):
            self.state = {}

        def get_state(self, key, default=None):
            return self.state.get(key, default)

        def set_state(self, key, value):
            self.state[key] = value

        def capture_interpretation_active_keys(self, **kwargs):
            return set()

        def capture_interpretation_terminal_keys(self, **kwargs):
            return {}

        def capture_interpretation_queue_health(self, *args, **kwargs):
            return {"by_status": {}, "daily": {}}

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
    public_available = False

    def priority(repository, *, limit):
        return [{"interpretation_id": "public-new"}] if public_available else []

    monkeypatch.setattr(worker, "current_public_priority_requests", priority)
    monkeypatch.setattr(
        worker,
        "current_persisted_pending_requests",
        lambda repository, *, limit: [dict(item) for item in background],
    )

    def prepare(ledger_repository, operations_repository, rows, *, limit):
        if rows and rows[0].get("interpretation_id") == "public-new":
            return ([dict(public)], 0)
        return ([dict(item) for item in background[:limit]], 0)

    monkeypatch.setattr(worker, "prepare_persisted_pending_requests", prepare)
    order = []

    def complete(items, env_file, *, workers):
        nonlocal public_available
        order.extend(str(item["interpretation_id"]) for item in items)
        if items and str(items[0]["interpretation_id"]).startswith("old-"):
            public_available = True
        else:
            public_available = False
        return ["COMPLETED"] * len(items)

    monkeypatch.setattr(worker, "process_pending_items", complete)
    args = SimpleNamespace(
        env_file=Path(tmp_path / "capture.env"),
        limit=4,
        scan_limit=4,
        workers=1,
    )

    assert worker.run(args) == 0
    assert order[:3] == ["old-0", "public-new", "old-1"]


def test_worker_combines_recent_and_durable_keyset_lanes_without_generation_reset(
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
        calls: list[tuple[str, tuple[int, str, str] | None]] = []

        def capture_source_generation(self):
            return {"observation_count": 3, "revision_count": 0}

        def capture_interpretation_candidate_count(self):
            return len(items)

        def capture_interpretation_candidates(
            self,
            *,
            limit: int,
            offset: int = 0,
            order: str = "fair",
            after=None,
        ):
            self.calls.append((order, after))
            values = list(reversed(items)) if order == "recent" else list(items)
            if after is not None:
                values = [
                    item
                    for item in values
                    if (
                        int(item.get("bucket_priority") or 1),
                        item["event_id"],
                        item["observation_id"],
                    )
                    > after
                ]
            return values[offset : offset + limit]

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

        def capture_interpretation_pending_runs(self, **kwargs):
            return []

        def capture_interpretation_active_keys(self, **kwargs):
            return set()

        def capture_interpretation_priority_runs(self, **kwargs):
            return []

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

    for item in items:
        item["bucket_priority"] = 1

    assert worker.run(args) == 0
    first_state = operations.state[worker.INVENTORY_STATE_KEY]
    assert first_state["inventory_loader"] == (
        "persisted_pending_then_recent_plus_durable_keyset_v4"
    )
    assert first_state["fair_cursor"]["event_id"] == "event-1"

    # Even though source_generation is unchanged, the durable fair cursor moves
    # forward instead of returning early or resetting to the head.
    operations.state[worker.INVENTORY_STATE_KEY]["last_inventory_scan_at"] = (
        "2000-01-01T00:00:00+00:00"
    )
    assert worker.run(args) == 0
    second_state = operations.state[worker.INVENTORY_STATE_KEY]
    assert second_state["fair_cursor"]["event_id"] == "event-2"
    assert ("fair", (1, "event-1", "obs-1")) in ledger.calls

    assert worker.run(args) == 0
    assert len(operations.terminal) == 3
    assert worker.RUNTIME_STATE_KEY in operations.state


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
