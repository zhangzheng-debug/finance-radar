from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.models.qwen_risk_contract import (
    QWEN_RISK_CONTRACT_VERSION,
    QWEN_RISK_PROMPT_VERSION,
    expected_semantic_payload,
    normalize_qwen_risk_content,
)
from app.services.qwen_risk_semantics import QwenRiskModelProvider
from app.services.qwen_risk_semantics import (
    QWEN_RISK_RUNTIME_INPUT_VERSION,
    build_qwen_risk_runtime_input,
)
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
        self.semantic_calls = 0

    def shadow_batch(
        self,
        *,
        limit: int,
        offset: int = 0,
        order: str = "latest",
        event_ids: list[str] | None = None,
        after_event_id: str | None = None,
        semantic_events_only: bool = False,
    ):
        assert semantic_events_only is True
        self.semantic_calls += 1
        values = list(reversed(self.items)) if order == "latest" else self.items
        if event_ids:
            requested = set(event_ids)
            values = [
                item
                for item in values
                if item["detail"]["event"]["event_id"] in requested
            ]
        if after_event_id is not None:
            values = [
                item
                for item in values
                if item["detail"]["event"]["event_id"] > after_event_id
            ]
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
            "prompt_version": "qwen-risk-dual-review-consensus-v2",
            "assessment_scope": "SOURCE_CONDITIONAL",
            "event_version": 1,
            "event_status": "candidate",
            "label": "PRIORITY_REVIEW",
            "confidence": 0.0,
            "confidence_applicable": False,
            "decision_source": "QWEN_ADAPTER",
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
    assert contract["input_sufficient"] is True
    assert len(contract["source_identity_sha256"]) == 64


def test_provider_contract_fails_closed_when_all_semantic_input_is_empty() -> None:
    provider = QwenRiskModelProvider(
        "http://127.0.0.1:18602",
        "qwen-risk-test",
        "a" * 64,
        request_fn=lambda *args, **kwargs: None,
    )
    empty = _item(1)
    empty["detail"]["preferred_source"] = {}
    contract = provider.input_contract(empty["detail"], [])
    assert contract["input_sufficient"] is False


def test_runtime_wire_is_bounded_while_canonical_content_stays_complete() -> None:
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
        "headline": "  Issuer   may default  " + ("headline " * 100),
        "summary": "  source   summary " + ("summary " * 400),
        "passages": [
            {"passage": "START " + ("exact source text " * 250) + " END"},
            {"passage": "SECOND " + ("context " * 300) + " TAIL"},
            {"passage": "third passage must not reach the runtime wire"},
        ],
    }
    prediction, _latency = provider.predict_content(content)
    assert prediction == expected_semantic_payload("MATERIAL_ADVERSE", "ADVERSE")
    response_schema = calls[0][1]["json"]["response_format"]["json_schema"]["schema"]
    assert response_schema["required"] == ["materiality", "polarity"]
    request_content = calls[0][1]["json"]["messages"][1]["content"]
    import json

    canonical = normalize_qwen_risk_content(content)
    runtime = json.loads(request_content)
    assert runtime == build_qwen_risk_runtime_input(content)
    assert runtime != canonical
    assert len(canonical["passages"]) == 3
    assert len(runtime["passages"]) == 2
    assert runtime["passages"][0]["passage"].startswith("START")
    assert runtime["passages"][0]["passage"].endswith("END")
    assert len(request_content) < 2_500


def test_provider_derives_strength_and_priority_instead_of_trusting_model() -> None:
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": {
                                "materiality": "NOT_MATERIAL_ADVERSE",
                                "polarity": "POSITIVE",
                                "adverse_strength": "HIGH",
                                "semantic_priority": "PRIORITY_REVIEW",
                            }
                        }
                    }
                ]
            }

    provider = QwenRiskModelProvider(
        "http://127.0.0.1:18602",
        "qwen-risk-test",
        "a" * 64,
        request_fn=lambda *args, **kwargs: Response(),
    )

    prediction, _latency = provider.predict_content(
        {"headline": "Revenue guidance was raised", "summary": "", "passages": []}
    )

    assert prediction == expected_semantic_payload("NOT_MATERIAL_ADVERSE", "POSITIVE")


def test_provider_applies_narrow_hybrid_anchor_after_model_inference() -> None:
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": expected_semantic_payload(
                                "NOT_MATERIAL_ADVERSE", "NEUTRAL"
                            )
                        }
                    }
                ]
            }

    item = _item(1)
    item["detail"]["preferred_source"]["summary"] = (
        "The issuer filed a voluntary petition under Chapter 11 in bankruptcy court."
    )
    provider = QwenRiskModelProvider(
        "http://127.0.0.1:18602",
        "qwen-risk-test",
        "a" * 64,
        request_fn=lambda *args, **kwargs: Response(),
    )

    result = provider.assess(item["detail"], [])

    assert result["materiality"] == "MATERIAL_ADVERSE"
    assert result["polarity"] == "ADVERSE"
    assert result["decision_source"] == "DETERMINISTIC_HARDCASE_ANCHOR"
    assert result["hybrid_rule"] == "bankruptcy_restructuring_or_equity_cancellation"
    assert result["training_basis"] == "DUAL_REVIEW_AI_CONSENSUS"
    assert result["runtime_input_version"] == QWEN_RISK_RUNTIME_INPUT_VERSION
    assert len(result["runtime_input_sha256"]) == 64


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
    assert first["selection"]["fair_after_event_id"] is None
    assert first["selection"]["next_after_event_id"] == "event-0"


def test_latest_qwen_run_can_be_pinned_to_exact_approved_generation(
    tmp_path: Path,
) -> None:
    operations = OperationsRepository(tmp_path / "operations.sqlite3")
    provider = Provider()
    approved = provider.assess(_item(1)["detail"], [])
    approved["input_sha256"] = hashlib.sha256(b"approved").hexdigest()
    operations.record_model_run_once("event-1", approved)
    time.sleep(0.001)

    wrong_contract = {
        **approved,
        "input_sha256": hashlib.sha256(b"wrong-contract").hexdigest(),
        "contract_version": "qwen-risk-semantics-v999",
    }
    operations.record_model_run_once("event-1", wrong_contract)
    time.sleep(0.001)
    wrong_prompt = {
        **approved,
        "input_sha256": hashlib.sha256(b"wrong-prompt").hexdigest(),
        "prompt_version": "qwen-risk-prompt-v999",
    }
    operations.record_model_run_once("event-1", wrong_prompt)
    time.sleep(0.001)
    wrong_model = {
        **approved,
        "input_sha256": hashlib.sha256(b"wrong-model").hexdigest(),
        "model_version": "qwen-risk-" + "b" * 16,
    }
    operations.record_model_run_once("event-1", wrong_model)

    unpinned = operations.latest_qwen_risk_runs_for_versions({"event-1": 1})
    pinned = operations.latest_qwen_risk_runs_for_versions(
        {"event-1": 1},
        model_version=provider.model_version,
        contract_version=QWEN_RISK_CONTRACT_VERSION,
        prompt_version=QWEN_RISK_PROMPT_VERSION,
    )

    assert unpinned["event-1"]["output"]["model_version"] == wrong_model["model_version"]
    assert pinned["event-1"]["output"]["input_sha256"] == approved["input_sha256"]
    assert operations.latest_qwen_risk_runs_for_versions(
        {"event-1": 1}, model_version=""
    ) == {}


def test_qwen_worker_keyset_queue_covers_every_semantic_event(tmp_path: Path) -> None:
    operations = OperationsRepository(tmp_path / "operations.sqlite3")
    ledger = Ledger()

    for _index in range(8):
        run_qwen_risk_batch(
            ledger,
            operations,
            Provider(),
            scan_limit=4,
            run_limit=2,
        )

    selected = operations.latest_qwen_risk_runs_for_versions(
        {f"event-{index}": 1 for index in range(6)}
    )
    assert set(selected) == {f"event-{index}" for index in range(6)}
    assert ledger.semantic_calls > 0


def test_qwen_worker_can_use_two_bounded_model_slots(tmp_path: Path) -> None:
    class ConcurrentProvider(Provider):
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.active = 0
            self.maximum = 0
            self.calls = 0
            self.parallel_pair = threading.Barrier(2)

        def assess(self, detail, evidence):
            with self.lock:
                self.calls += 1
                call_number = self.calls
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            try:
                if call_number in {2, 3}:
                    self.parallel_pair.wait(timeout=5)
                time.sleep(0.01)
                return super().assess(detail, evidence)
            finally:
                with self.lock:
                    self.active -= 1

    provider = ConcurrentProvider()
    result = run_qwen_risk_batch(
        Ledger(),
        OperationsRepository(tmp_path / "operations.sqlite3"),
        provider,
        scan_limit=6,
        run_limit=4,
        concurrency=2,
    )

    assert result["recorded"] == 4
    assert result["concurrency"] == 2
    assert provider.maximum == 2


def test_qwen_priority_queue_is_exact_idempotent_and_bounded(tmp_path: Path) -> None:
    operations = OperationsRepository(tmp_path / "operations.sqlite3")
    model_version = Provider.model_version
    first_hash = hashlib.sha256(b"event-1").hexdigest()
    second_hash = hashlib.sha256(b"event-2").hexdigest()

    first = operations.enqueue_qwen_risk_priority(
        "event-1", 1, first_hash, model_version, max_items=1
    )
    duplicate = operations.enqueue_qwen_risk_priority(
        "event-1", 1, first_hash, model_version, max_items=1
    )
    operations.set_state("qwen_risk_activity_v1", {"items": []})
    recovered_duplicate = operations.enqueue_qwen_risk_priority(
        "event-1", 1, first_hash, model_version, max_items=1
    )
    full = operations.enqueue_qwen_risk_priority(
        "event-2", 1, second_hash, model_version, max_items=1
    )

    assert first["state"] == "QUEUED" and first["enqueued"] is True
    assert duplicate["state"] == "QUEUED" and duplicate["enqueued"] is False
    assert recovered_duplicate["state"] == "QUEUED"
    assert recovered_duplicate["enqueued"] is False
    assert full["state"] == "FAILED" and full["error_code"] == "PRIORITY_QUEUE_FULL"
    assert operations.qwen_risk_activity(
        "event-2", 1, second_hash, model_version
    ) is None

    claimed = operations.claim_qwen_risk_priority(model_version)
    assert claimed is not None
    assert claimed["event_id"] == "event-1"
    assert claimed["state"] == "RUNNING"
    assert operations.claim_qwen_risk_priority(model_version) is None
    assert operations.qwen_risk_activity(
        "event-1", 1, "f" * 64, model_version
    ) is None


def test_qwen_stale_running_expires_and_can_be_requeued(tmp_path: Path) -> None:
    operations = OperationsRepository(tmp_path / "operations.sqlite3")
    model_version = Provider.model_version
    input_sha256 = hashlib.sha256(b"event-1").hexdigest()
    started = datetime(2026, 9, 1, tzinfo=timezone.utc)
    operations.enqueue_qwen_risk_priority(
        "event-1", 1, input_sha256, model_version, now=started
    )
    operations.claim_qwen_risk_priority(model_version, now=started)

    stale = operations.qwen_risk_activity(
        "event-1",
        1,
        input_sha256,
        model_version,
        now=started + timedelta(seconds=181),
    )
    assert stale is not None
    assert stale["state"] == "FAILED"
    assert stale["error_code"] == "STALE_HEARTBEAT"

    requeued = operations.enqueue_qwen_risk_priority(
        "event-1",
        1,
        input_sha256,
        model_version,
        now=started + timedelta(seconds=181),
    )
    assert requeued["state"] == "QUEUED"
    assert requeued["enqueued"] is True
    assert requeued["queued_at"] == (started + timedelta(seconds=181)).isoformat()


def test_qwen_expired_queued_input_can_be_requeued_with_a_fresh_timestamp(
    tmp_path: Path,
) -> None:
    operations = OperationsRepository(tmp_path / "operations.sqlite3")
    model_version = Provider.model_version
    input_sha256 = hashlib.sha256(b"event-4").hexdigest()
    started = datetime(2026, 9, 1, tzinfo=timezone.utc)
    operations.enqueue_qwen_risk_priority(
        "event-4", 1, input_sha256, model_version, now=started
    )

    refreshed_at = started + timedelta(minutes=16)
    refreshed = operations.enqueue_qwen_risk_priority(
        "event-4", 1, input_sha256, model_version, now=refreshed_at
    )

    assert refreshed["state"] == "QUEUED"
    assert refreshed["enqueued"] is True
    assert refreshed["queued_at"] == refreshed_at.isoformat()


def test_qwen_failed_activity_obeys_retry_window(tmp_path: Path) -> None:
    operations = OperationsRepository(tmp_path / "operations.sqlite3")
    model_version = Provider.model_version
    input_sha256 = hashlib.sha256(b"event-3").hexdigest()
    failed_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    failed = operations.set_qwen_risk_activity(
        "event-3",
        1,
        input_sha256,
        model_version,
        "FAILED",
        error_code="MODEL_TIMEOUT",
        now=failed_at,
    )
    assert failed["retry_after"] == (failed_at + timedelta(seconds=15)).isoformat()

    deferred = operations.enqueue_qwen_risk_priority(
        "event-3",
        1,
        input_sha256,
        model_version,
        now=failed_at + timedelta(seconds=1),
    )
    assert deferred["state"] == "FAILED"
    assert deferred["enqueued"] is False
    accepted = operations.enqueue_qwen_risk_priority(
        "event-3",
        1,
        input_sha256,
        model_version,
        now=failed_at + timedelta(seconds=16),
    )
    assert accepted["state"] == "QUEUED"
    assert accepted["enqueued"] is True


def test_qwen_worker_processes_one_priority_before_recent_and_marks_ready(
    tmp_path: Path,
) -> None:
    class RecordingProvider(Provider):
        def __init__(self) -> None:
            self.calls: list[str] = []

        def assess(self, detail, evidence):
            self.calls.append(detail["event"]["event_id"])
            return super().assess(detail, evidence)

    operations = OperationsRepository(tmp_path / "operations.sqlite3")
    provider = RecordingProvider()
    input_sha256 = hashlib.sha256(b"event-1").hexdigest()
    operations.enqueue_qwen_risk_priority(
        "event-1", 1, input_sha256, provider.model_version
    )

    result = run_qwen_risk_batch(
        Ledger(), operations, provider, scan_limit=4, run_limit=2, concurrency=2
    )

    assert result["priority_claimed"] == 1
    assert result["recorded"] == 2
    assert provider.calls[0] == "event-1"
    assert provider.calls[1] == "event-0"
    activity = operations.qwen_risk_activity(
        "event-1", 1, input_sha256, provider.model_version
    )
    assert activity is not None
    assert activity["state"] == "READY"


def test_qwen_worker_failure_does_not_block_other_inputs_and_defers_retry(
    tmp_path: Path,
) -> None:
    class FailingProvider(Provider):
        def assess(self, detail, evidence):
            if detail["event"]["event_id"] == "event-5":
                raise TimeoutError("provider timeout details are not public")
            return super().assess(detail, evidence)

    operations = OperationsRepository(tmp_path / "operations.sqlite3")
    provider = FailingProvider()
    first = run_qwen_risk_batch(
        Ledger(), operations, provider, scan_limit=4, run_limit=2
    )

    failed_hash = hashlib.sha256(b"event-5").hexdigest()
    failed = operations.qwen_risk_activity(
        "event-5", 1, failed_hash, provider.model_version
    )
    ready_hash = hashlib.sha256(b"event-0").hexdigest()
    ready = operations.qwen_risk_activity(
        "event-0", 1, ready_hash, provider.model_version
    )
    assert first["recorded"] == 1
    assert first["errors"] == ["event-5:TimeoutError"]
    assert failed is not None and failed["state"] == "FAILED"
    assert ready is not None and ready["state"] == "READY"
    queue = operations.get_state("qwen_risk_priority_queue_v1", {"items": []})
    assert any(
        item.get("event_id") == "event-5" and item.get("available_at")
        for item in queue["items"]
    )

    second = run_qwen_risk_batch(
        Ledger(), operations, provider, scan_limit=4, run_limit=2
    )
    assert second["retry_deferred"] >= 1
    assert second["recorded"] >= 1


def test_qwen_worker_rechecks_public_priority_after_each_completed_item(
    tmp_path: Path,
) -> None:
    operations = OperationsRepository(tmp_path / "operations.sqlite3")

    class EnqueuingProvider(Provider):
        def __init__(self) -> None:
            self.calls: list[str] = []

        def assess(self, detail, evidence):
            event_id = detail["event"]["event_id"]
            self.calls.append(event_id)
            if len(self.calls) == 1:
                operations.enqueue_qwen_risk_priority(
                    "event-1",
                    1,
                    hashlib.sha256(b"event-1").hexdigest(),
                    self.model_version,
                )
            return super().assess(detail, evidence)

    provider = EnqueuingProvider()
    result = run_qwen_risk_batch(
        Ledger(), operations, provider, scan_limit=4, run_limit=3, concurrency=1
    )

    assert result["recorded"] == 3
    assert result["priority_claimed"] == 1
    assert provider.calls[:2] == ["event-5", "event-1"]


def test_qwen_failed_work_is_requeued_without_another_public_request(
    tmp_path: Path,
) -> None:
    operations = OperationsRepository(tmp_path / "operations.sqlite3")
    model_version = Provider.model_version
    input_sha256 = hashlib.sha256(b"event-retry").hexdigest()
    identity = ("event-retry", 1, input_sha256, model_version)
    observed_at = datetime(2026, 9, 1, tzinfo=timezone.utc)

    operations.set_qwen_risk_activity(*identity, "RUNNING", now=observed_at)
    failed = operations.schedule_qwen_risk_retry(
        *identity,
        error_code="MODEL_TIMEOUT",
        now=observed_at,
    )
    assert failed["state"] == "FAILED"
    assert failed["requeued"] is True
    assert failed["retry_after"] == (
        observed_at + timedelta(seconds=15)
    ).isoformat()
    assert operations.qwen_risk_activity(
        *identity, now=observed_at + timedelta(seconds=1)
    )["state"] == "FAILED"
    assert operations.qwen_risk_activity(
        *identity, now=observed_at + timedelta(seconds=16)
    )["state"] == "QUEUED"

    claimed = operations.claim_qwen_risk_priority(
        model_version, now=observed_at + timedelta(seconds=16)
    )
    assert claimed is not None
    assert claimed["event_id"] == "event-retry"
    assert claimed["state"] == "RUNNING"


def test_qwen_automatic_retries_are_bounded(tmp_path: Path) -> None:
    operations = OperationsRepository(tmp_path / "operations.sqlite3")
    model_version = Provider.model_version
    input_sha256 = hashlib.sha256(b"event-bounded-retry").hexdigest()
    identity = ("event-bounded-retry", 1, input_sha256, model_version)
    observed_at = datetime(2026, 9, 1, tzinfo=timezone.utc)

    terminal = None
    for attempt in range(4):
        operations.set_qwen_risk_activity(*identity, "RUNNING", now=observed_at)
        terminal = operations.schedule_qwen_risk_retry(
            *identity,
            error_code="MODEL_TIMEOUT",
            now=observed_at,
        )
        if attempt < 3:
            assert terminal["requeued"] is True
            retry_after = datetime.fromisoformat(terminal["retry_after"])
            claimed = operations.claim_qwen_risk_priority(
                model_version, now=retry_after
            )
            assert claimed is not None
            observed_at = retry_after

    assert terminal is not None
    assert terminal["attempts"] == 4
    assert terminal["requeued"] is False
    assert "retry_after" not in terminal
    assert operations.claim_qwen_risk_priority(
        model_version, now=observed_at + timedelta(minutes=10)
    ) is None


def test_qwen_running_state_exists_only_while_provider_is_actually_invoked(
    tmp_path: Path,
) -> None:
    class BlockingProvider(Provider):
        def __init__(self) -> None:
            self.entered = threading.Event()
            self.release = threading.Event()

        def assess(self, detail, evidence):
            self.entered.set()
            assert self.release.wait(timeout=5)
            return super().assess(detail, evidence)

    operations = OperationsRepository(tmp_path / "operations.sqlite3")
    provider = BlockingProvider()
    result: dict = {}

    def run_worker() -> None:
        result.update(
            run_qwen_risk_batch(
                Ledger(), operations, provider, scan_limit=2, run_limit=1
            )
        )

    thread = threading.Thread(target=run_worker, daemon=True)
    thread.start()
    assert provider.entered.wait(timeout=5)
    input_sha256 = hashlib.sha256(b"event-5").hexdigest()
    running = operations.qwen_risk_activity(
        "event-5", 1, input_sha256, provider.model_version
    )
    assert running is not None
    assert running["state"] == "RUNNING"
    assert running["heartbeat_at"]

    provider.release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert result["recorded"] == 1
    ready = operations.qwen_risk_activity(
        "event-5", 1, input_sha256, provider.model_version
    )
    assert ready is not None and ready["state"] == "READY"
