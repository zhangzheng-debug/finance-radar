from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.services.deepseek_capture_interpretation import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_CHEAP_TEXT_MODEL,
)
from scripts.adjudicate_qwen_semantic_blind_deepseek import (
    MANIFEST_NAME,
    REFERENCE_CLASS,
    RESULTS_NAME,
    BlindAdjudicationError,
    adjudicate,
)


def _write_inputs(path: Path, count: int = 2) -> None:
    path.write_text(
        "".join(
            json.dumps(
                {
                    "sample_id": f"blind-{index}",
                    "content": {
                        "headline": f"Anonymous issuer event {index}",
                        "summary": f"Frozen source passage {index}",
                        "passages": [
                            {
                                "document_type": "8-K",
                                "passage": f"Issuer disclosed event {index}.",
                            }
                        ],
                    },
                }
            )
            + "\n"
            for index in range(count)
        ),
        encoding="utf-8",
    )


def _env_file(path: Path) -> None:
    path.write_text(
        "UNRELATED=ignored\nDEEPSEEK_API_KEY='unit-test-secret'\n",
        encoding="utf-8",
    )


def _response(materiality: str, polarity: str, reason: str) -> dict:
    return {
        "id": "safe-response-id",
        "model": DEEPSEEK_CHEAP_TEXT_MODEL,
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "materiality": materiality,
                            "polarity": polarity,
                            "brief_reason": reason,
                        }
                    )
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
        },
    }


def test_isolated_reference_uses_only_content_and_writes_hashed_artifacts(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "arbitration_inputs.jsonl"
    env_file = tmp_path / "secret.env"
    output_dir = tmp_path / "reference"
    _write_inputs(input_path)
    _env_file(env_file)
    captured: list[dict] = []

    def requester(url, headers, payload, timeout):
        captured.append(
            {"url": url, "headers": headers, "payload": payload, "timeout": timeout}
        )
        return _response(
            "MATERIAL_ADVERSE" if len(captured) == 1 else "NOT_MATERIAL_ADVERSE",
            "ADVERSE" if len(captured) == 1 else "NEUTRAL",
            "The frozen text directly expresses the classified event.",
        )

    manifest = adjudicate(
        input_path=input_path,
        env_file=env_file,
        output_dir=output_dir,
        max_workers=1,
        requester=requester,
        sleeper=lambda _: None,
    )

    assert len(captured) == 2
    for request in captured:
        assert request["url"] == DEEPSEEK_BASE_URL + "/chat/completions"
        assert request["headers"]["Authorization"] == "Bearer unit-test-secret"
        assert request["payload"]["model"] == DEEPSEEK_CHEAP_TEXT_MODEL
        assert request["payload"]["response_format"] == {"type": "json_object"}
        assert request["payload"]["thinking"] == {"type": "disabled"}
        assert request["payload"]["temperature"] == 0
        assert request["payload"]["stream"] is False
        serialized = json.dumps(request["payload"], ensure_ascii=False)
        assert "blind-0" not in serialized
        assert "blind-1" not in serialized
        assert "reviewer_labels" not in serialized
        assert "qwen_prediction" not in serialized

    rows = [
        json.loads(line)
        for line in (output_dir / RESULTS_NAME).read_text(encoding="utf-8").splitlines()
    ]
    assert [row["sample_id"] for row in rows] == ["blind-0", "blind-1"]
    assert all(
        set(row)
        == {
            "sample_id",
            "materiality",
            "polarity",
            "rationale",
            "model",
            "input_sha256",
        }
        for row in rows
    )
    assert all(len(row["input_sha256"]) == 64 for row in rows)
    assert manifest["reference_class"] == REFERENCE_CLASS
    assert manifest["human_gold_claimed"] is False
    assert manifest["isolation"] == {
        "provider_received_sample_id": False,
        "reviewer_labels_read": False,
        "qwen_predictions_read": False,
        "market_outcomes_read": False,
    }
    assert manifest["results"]["row_count"] == 2
    assert manifest["failed_rows"] == 0

    for filename in (RESULTS_NAME, MANIFEST_NAME):
        raw = (output_dir / filename).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        assert (output_dir / f"{filename}.sha256").read_text(encoding="ascii") == (
            f"{digest}  {filename}\n"
        )
    all_output = "".join(
        path.read_text(encoding="utf-8")
        for path in output_dir.iterdir()
        if path.is_file()
    )
    assert "unit-test-secret" not in all_output
    assert str(env_file) not in all_output
    assert not (tmp_path / ".reference.in-progress").exists()


def test_retries_contract_failure_without_relaxing_closed_labels(tmp_path: Path) -> None:
    input_path = tmp_path / "arbitration_inputs.jsonl"
    env_file = tmp_path / "secret.env"
    output_dir = tmp_path / "reference"
    _write_inputs(input_path, count=1)
    _env_file(env_file)
    calls = 0

    def requester(url, headers, payload, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _response("VERY_BAD", "ADVERSE", "Invalid open label on first call.")
        return _response(
            "MATERIAL_ADVERSE",
            "ADVERSE",
            "The text expresses a materially adverse event.",
        )

    manifest = adjudicate(
        input_path=input_path,
        env_file=env_file,
        output_dir=output_dir,
        max_workers=1,
        max_attempts=2,
        requester=requester,
        sleeper=lambda _: None,
    )

    assert calls == 2
    assert manifest["usage"]["request_attempts"] == 2
    row = json.loads((output_dir / RESULTS_NAME).read_text(encoding="utf-8"))
    assert row["materiality"] == "MATERIAL_ADVERSE"


def test_failed_run_preserves_incremental_progress_but_not_final_output(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "arbitration_inputs.jsonl"
    env_file = tmp_path / "secret.env"
    output_dir = tmp_path / "reference"
    _write_inputs(input_path, count=2)
    _env_file(env_file)
    calls = 0

    def requester(url, headers, payload, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _response(
                "NOT_MATERIAL_ADVERSE", "NEUTRAL", "The first row is routine context."
            )
        return {"choices": []}

    with pytest.raises(BlindAdjudicationError, match="ATTEMPTS_EXHAUSTED"):
        adjudicate(
            input_path=input_path,
            env_file=env_file,
            output_dir=output_dir,
            max_workers=1,
            max_attempts=1,
            requester=requester,
            sleeper=lambda _: None,
        )

    assert not output_dir.exists()
    stage = tmp_path / ".reference.in-progress"
    assert stage.is_dir()
    progress = [
        json.loads(line)
        for line in (stage / "progress.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    completed = [row for row in progress if row["status"] == "completed"]
    failed = [row for row in progress if row["status"] == "failed"]
    assert len(completed) == 1
    assert completed[0]["sample_id"] == "blind-0"
    assert failed == [
        {
            "sample_id": "blind-1",
            "status": "failed",
            "error_code": "DEEPSEEK_INVALID_COMPLETION_ATTEMPTS_EXHAUSTED",
        }
    ]
    assert "unit-test-secret" not in (stage / "run_state.json").read_text(
        encoding="utf-8"
    )


def test_input_with_reviewer_or_model_labels_is_rejected_before_any_call(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "arbitration_inputs.jsonl"
    input_path.write_text(
        json.dumps(
            {
                "sample_id": "blind-0",
                "content": {
                    "headline": "Anonymous input",
                    "reviewer_labels": {"A": "MATERIAL_ADVERSE"},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    env_file = tmp_path / "secret.env"
    _env_file(env_file)
    output_dir = tmp_path / "reference"
    called = False

    def requester(url, headers, payload, timeout):
        nonlocal called
        called = True
        return _response("UNCLEAR", "UNCLEAR", "This should never be called.")

    with pytest.raises(ValueError, match="prohibited label/output keys"):
        adjudicate(
            input_path=input_path,
            env_file=env_file,
            output_dir=output_dir,
            requester=requester,
        )
    assert called is False
    assert not output_dir.exists()


def test_existing_output_directory_is_refused_before_input_or_env_reads(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "reference"
    output_dir.mkdir()
    with pytest.raises(FileExistsError, match="already exists"):
        adjudicate(
            input_path=tmp_path / "missing-input.jsonl",
            env_file=tmp_path / "missing.env",
            output_dir=output_dir,
        )
