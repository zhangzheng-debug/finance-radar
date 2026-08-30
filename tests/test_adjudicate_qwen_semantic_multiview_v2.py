from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.adjudicate_qwen_semantic_multiview_v2 import (
    RESULTS_NAME,
    SUPERVISION_CLASS,
    _parse_completion,
    adjudicate_multiview,
)


def _env_file(path: Path) -> None:
    path.write_text("DEEPSEEK_API_KEY=unit-test-secret\n", encoding="utf-8")


def _source(path: Path, *, extra_content: dict | None = None) -> None:
    content = {
        "headline": "Issuer received a notice regarding a contractual clause",
        "summary": "The excerpt does not say that a final delisting occurred.",
        "passages": [{"passage": "If bankruptcy occurs, termination may follow."}],
    }
    content.update(extra_content or {})
    path.write_text(
        json.dumps({"sample_id": "blind-1", "content": content}) + "\n",
        encoding="utf-8",
    )


def _payload(kind: str) -> dict:
    if kind == "fact":
        return {
            "materiality": "MATERIAL_ADVERSE",
            "polarity": "ADVERSE",
            "impact_strength": "MODERATE",
            "event_realization": "FORMALLY_DECIDED_OR_COMMITTED",
            "subject_relation": "PRIMARY_SUBJECT",
            "risk_status": "ACTIVE",
            "novelty": "NEW_EVENT_OR_STATUS_CHANGE",
            "reason_codes": [
                "FORMAL_DECISION_OR_BINDING_COMMITMENT",
                "PRIMARY_SUBJECT_DIRECTLY_AFFECTED",
                "MATERIAL_DOWNSIDE_MECHANISM",
                "ADVERSE_CONDITION_ACTIVE",
                "ADVERSE_COMPONENT_PRESENT",
                "MODERATE_SOURCE_SUPPORTED_IMPACT",
                "NEW_MATERIAL_FACT_OR_STATUS_CHANGE",
            ],
            "brief_reason": "The first review treats the notice as a binding adverse action.",
        }
    return {
        "materiality": "NOT_MATERIAL_ADVERSE",
        "polarity": "NEUTRAL",
        "impact_strength": "ROUTINE_OR_NONE",
        "event_realization": "HYPOTHETICAL_OR_CONTRACT_DEFINITION",
        "subject_relation": "PRIMARY_SUBJECT",
        "risk_status": "NO_ADVERSE_CONDITION",
        "novelty": "NEW_EVENT_OR_STATUS_CHANGE",
        "reason_codes": [
            "HYPOTHETICAL_SCENARIO_OR_CONTRACT_DEFINITION",
            "PRIMARY_SUBJECT_DIRECTLY_AFFECTED",
            "NO_MATERIAL_DOWNSIDE_MECHANISM",
            "NEW_MATERIAL_FACT_OR_STATUS_CHANGE",
            "ROUTINE_OR_NO_SOURCE_SUPPORTED_IMPACT",
        ],
        "brief_reason": "The clause is hypothetical and the source does not report a realised adverse action.",
    }


def _response(value: dict) -> dict:
    return {
        "model": "deepseek-test",
        "choices": [
            {
                "message": {"content": json.dumps(value)},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def test_three_pass_workflow_keeps_first_views_isolated_and_records_final(tmp_path: Path) -> None:
    input_path = tmp_path / "inputs.jsonl"
    env_file = tmp_path / "secret.env"
    output_dir = tmp_path / "output"
    _source(input_path)
    _env_file(env_file)
    calls: list[dict] = []

    def requester(url, headers, payload, timeout):
        calls.append(payload)
        system = payload["messages"][0]["content"]
        if "FACT AND MECHANISM" in system:
            return _response(_payload("fact"))
        return _response(_payload("boundary"))

    manifest = adjudicate_multiview(
        input_path=input_path,
        env_file=env_file,
        output_dir=output_dir,
        max_workers=1,
        max_attempts=1,
        requester=requester,
        sleeper=lambda _: None,
    )

    assert len(calls) == 3
    assert "isolated_reviews" not in calls[0]["messages"][1]["content"]
    assert "isolated_reviews" not in calls[1]["messages"][1]["content"]
    assert "isolated_reviews" in calls[2]["messages"][1]["content"]
    assert calls[0]["thinking"] == {"type": "enabled"}
    assert calls[1]["thinking"] == {"type": "enabled"}
    assert calls[2]["thinking"] == {"type": "enabled"}
    assert calls[0]["reasoning_effort"] == "high"
    assert calls[1]["reasoning_effort"] == "high"
    assert calls[2]["reasoning_effort"] == "max"
    assert all(call["response_format"] == {"type": "json_object"} for call in calls)
    for payload in calls:
        wire = json.dumps(payload)
        assert "blind-1" not in wire
        assert "unit-test-secret" not in wire
        assert "qwen_prediction" not in wire
        assert "market_outcome" not in wire

    row = json.loads((output_dir / RESULTS_NAME).read_text(encoding="utf-8"))
    assert row["fact_mechanism_review"]["materiality"] == "MATERIAL_ADVERSE"
    assert row["boundary_review"]["materiality"] == "NOT_MATERIAL_ADVERSE"
    assert row["final"]["event_realization"] == "HYPOTHETICAL_OR_CONTRACT_DEFINITION"
    assert row["first_pass_pair_agreed"] is False
    assert manifest["supervision_class"] == SUPERVISION_CLASS
    assert manifest["isolation"]["first_passes_received_each_other"] is False
    assert manifest["isolation"]["qwen_predictions_read"] is False
    assert manifest["review_design"]["passes_per_sample"] == 3
    assert not (output_dir / "progress.jsonl").exists()
    all_output = "".join(path.read_text(encoding="utf-8") for path in output_dir.iterdir())
    assert "unit-test-secret" not in all_output


def test_missing_purely_derived_reason_code_is_completed_without_changing_axes(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "inputs.jsonl"
    env_file = tmp_path / "secret.env"
    output_dir = tmp_path / "output"
    _source(input_path)
    _env_file(env_file)

    def requester(url, headers, payload, timeout):
        system = payload["messages"][0]["content"]
        value = _payload("fact" if "FACT AND MECHANISM" in system else "boundary")
        value["reason_codes"] = [
            code
            for code in value["reason_codes"]
            if code != "NEW_MATERIAL_FACT_OR_STATUS_CHANGE"
        ]
        return _response(value)

    adjudicate_multiview(
        input_path=input_path,
        env_file=env_file,
        output_dir=output_dir,
        max_workers=1,
        max_attempts=1,
        requester=requester,
        sleeper=lambda _: None,
    )
    row = json.loads((output_dir / RESULTS_NAME).read_text(encoding="utf-8"))
    for field in ("fact_mechanism_review", "boundary_review", "final"):
        assert row[field]["novelty"] == "UNCLEAR"
        assert "NOVELTY_CONTEXT_MISSING" in row[field]["reason_codes"]
        assert "NEW_MATERIAL_FACT_OR_STATUS_CHANGE" not in row[field]["reason_codes"]
    assert row["fact_mechanism_review"]["materiality"] == "MATERIAL_ADVERSE"
    assert row["boundary_review"]["materiality"] == "NOT_MATERIAL_ADVERSE"


@pytest.mark.parametrize(
    "forbidden",
    [
        {"qwen_prediction": "PRIORITY_REVIEW"},
        {"materiality": "MATERIAL_ADVERSE"},
        {"reviewer_labels": {"A": "ADVERSE"}},
        {"market_outcome": {"t30m": -0.4}},
    ],
)
def test_prohibited_supervision_fields_are_rejected_before_api_call(
    tmp_path: Path, forbidden: dict
) -> None:
    input_path = tmp_path / "inputs.jsonl"
    env_file = tmp_path / "secret.env"
    output_dir = tmp_path / "output"
    _source(input_path, extra_content=forbidden)
    _env_file(env_file)
    called = False

    def requester(url, headers, payload, timeout):
        nonlocal called
        called = True
        return _response(_payload("boundary"))

    with pytest.raises(ValueError, match="prohibited supervision keys"):
        adjudicate_multiview(
            input_path=input_path,
            env_file=env_file,
            output_dir=output_dir,
            requester=requester,
        )
    assert called is False
    assert not output_dir.exists()


def test_invalid_view_contract_fails_closed_and_keeps_redacted_checkpoint(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "inputs.jsonl"
    env_file = tmp_path / "secret.env"
    output_dir = tmp_path / "output"
    _source(input_path)
    _env_file(env_file)

    def requester(url, headers, payload, timeout):
        return _response({"materiality": "MATERIAL_ADVERSE"})

    with pytest.raises(Exception, match="ATTEMPTS_EXHAUSTED"):
        adjudicate_multiview(
            input_path=input_path,
            env_file=env_file,
            output_dir=output_dir,
            max_workers=1,
            max_attempts=1,
            requester=requester,
            sleeper=lambda _: None,
        )
    stage = tmp_path / ".output.in-progress"
    assert stage.is_dir()
    checkpoint = "".join(path.read_text(encoding="utf-8") for path in stage.iterdir())
    assert "unit-test-secret" not in checkpoint
    assert "qwen_predictions_read\":false" in checkpoint


def test_legacy_resolved_alias_with_adverse_disposition_maps_to_active() -> None:
    legacy = _payload("fact")
    legacy["risk_status"] = "RESOLVED_OR_CURED"
    legacy["reason_codes"] = [
        code
        for code in legacy["reason_codes"]
        if code != "ADVERSE_CONDITION_ACTIVE"
    ] + ["ADVERSE_CONDITION_RESOLVED_OR_CURED"]
    parsed, _ = _parse_completion(_response(legacy))
    assert parsed["risk_status"] == "ACTIVE"
    assert "ADVERSE_CONDITION_ACTIVE" in parsed["reason_codes"]
    assert "ADVERSE_CONDITION_RESOLVED_OR_CURED" not in parsed["reason_codes"]


def test_resume_binds_state_skips_completed_and_retries_only_missing(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "inputs.jsonl"
    env_file = tmp_path / "secret.env"
    output_dir = tmp_path / "output"
    rows = [
        {
            "sample_id": "blind-1",
            "content": {
                "headline": "First issuer source",
                "summary": "A first anonymous source for the completed checkpoint.",
            },
        },
        {
            "sample_id": "blind-2",
            "content": {
                "headline": "Second issuer source",
                "summary": "A second anonymous source that initially fails.",
            },
        },
    ]
    original_raw = "".join(json.dumps(row) + "\n" for row in rows)
    input_path.write_text(original_raw, encoding="utf-8")
    _env_file(env_file)

    def first_requester(url, headers, payload, timeout):
        wire = payload["messages"][1]["content"]
        if "Second issuer source" in wire:
            return _response({"materiality": "MATERIAL_ADVERSE"})
        system = payload["messages"][0]["content"]
        return _response(
            _payload("fact" if "FACT AND MECHANISM" in system else "boundary")
        )

    with pytest.raises(Exception, match="ATTEMPTS_EXHAUSTED"):
        adjudicate_multiview(
            input_path=input_path,
            env_file=env_file,
            output_dir=output_dir,
            max_workers=1,
            max_attempts=1,
            requester=first_requester,
            sleeper=lambda _: None,
        )
    stage = tmp_path / ".output.in-progress"
    assert stage.is_dir()
    progress_rows = [
        json.loads(line)
        for line in (stage / "progress.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["status"] for row in progress_rows].count("completed") == 1
    assert [row["status"] for row in progress_rows].count("failed") == 1
    assert "usage" in next(row for row in progress_rows if row["status"] == "completed")

    mismatch_calls = 0

    def must_not_call(url, headers, payload, timeout):
        nonlocal mismatch_calls
        mismatch_calls += 1
        raise AssertionError("provider must not be called for a mismatched resume")

    input_path.write_text(original_raw.replace("First issuer", "Changed issuer"), encoding="utf-8")
    with pytest.raises(ValueError, match="resume state mismatch: input_sha256"):
        adjudicate_multiview(
            input_path=input_path,
            env_file=env_file,
            output_dir=output_dir,
            resume=True,
            requester=must_not_call,
        )
    input_path.write_text(original_raw, encoding="utf-8")

    state_path = stage / "run_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    original_model = state["model"]
    state["model"] = "different-model"
    state_path.write_text(json.dumps(state) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="resume state mismatch: model"):
        adjudicate_multiview(
            input_path=input_path,
            env_file=env_file,
            output_dir=output_dir,
            resume=True,
            requester=must_not_call,
        )
    state["model"] = original_model
    state_path.write_text(json.dumps(state) + "\n", encoding="utf-8")
    assert mismatch_calls == 0

    # Simulate a process dying part-way through the next progress write.
    with (stage / "progress.jsonl").open("ab") as progress:
        progress.write(b'{"sample_id":"blind-2"')

    resumed_calls: list[str] = []

    def resume_requester(url, headers, payload, timeout):
        wire = payload["messages"][1]["content"]
        resumed_calls.append(wire)
        system = payload["messages"][0]["content"]
        return _response(
            _payload("fact" if "FACT AND MECHANISM" in system else "boundary")
        )

    manifest = adjudicate_multiview(
        input_path=input_path,
        env_file=env_file,
        output_dir=output_dir,
        max_workers=1,
        max_attempts=1,
        resume=True,
        requester=resume_requester,
        sleeper=lambda _: None,
    )
    assert len(resumed_calls) == 3
    assert all("Second issuer source" in wire for wire in resumed_calls)
    assert all("First issuer source" not in wire for wire in resumed_calls)
    assert manifest["resume"] == {
        "requested": True,
        "completed_rows_loaded": 1,
        "rows_requested_this_process": 1,
        "state_binding_verified": True,
    }
    output_rows = [
        json.loads(line)
        for line in (output_dir / RESULTS_NAME).read_text(encoding="utf-8").splitlines()
    ]
    assert [row["sample_id"] for row in output_rows] == ["blind-1", "blind-2"]
    assert not stage.exists()
    all_output = "".join(path.read_text(encoding="utf-8") for path in output_dir.iterdir())
    assert "unit-test-secret" not in all_output


def test_history_context_controls_novelty_and_focal_context_is_separate() -> None:
    value = _payload("boundary")
    no_history = {
        "headline": "Anonymous issuer entered a routine agreement",
        "summary": "The agreement became effective today.",
    }
    parsed, _ = _parse_completion(_response(value), content=no_history)
    assert parsed["novelty"] == "UNCLEAR"
    assert "NOVELTY_CONTEXT_MISSING" in parsed["reason_codes"]

    with_history = {
        **no_history,
        "semantic_context": {
            "focal_subject": {"role": "ISSUER", "entity_group": "issuer-group-a"},
            "focal_asset": {
                "role": "COMMON_EQUITY",
                "entity_group": "asset-group-a",
            },
            "prior_event_context": [
                {"event_group": "prior-a", "source_fact": "A prior agreement was announced."}
            ],
            "source_excerpt_complete": True,
        },
    }
    parsed, _ = _parse_completion(_response(value), content=with_history)
    assert parsed["novelty"] == "NEW_EVENT_OR_STATUS_CHANGE"


def test_paid_merger_warrant_exit_and_spac_deadline_are_projected_to_safe_boundaries() -> None:
    paid, _ = _parse_completion(
        _response(_payload("fact")),
        content={
            "headline": "Issuer enters definitive merger at $105 per share, a 44% premium",
            "summary": "Common shares will convert into cash merger consideration.",
            "semantic_context": {
                "focal_subject": {"role": "ISSUER", "entity_group": "target-group-a"},
                "focal_asset": {
                    "role": "COMMON_EQUITY",
                    "entity_group": "target-equity-a",
                },
                "transaction_role": "TARGET",
                "prior_event_context": [],
                "source_excerpt_complete": True,
            },
        },
    )
    assert paid["materiality"] == "NOT_MATERIAL_ADVERSE"
    assert paid["polarity"] == "POSITIVE"
    assert paid["impact_strength"] == "MAJOR"
    assert "PAID_MERGER_OR_CASH_PREMIUM_EXIT" in paid["reason_codes"]

    warrant, _ = _parse_completion(
        _response(_payload("fact")),
        content={
            "headline": "Form 25 removal from listing",
            "summary": "The exact issue being removed consists only of issuer warrants.",
        },
    )
    assert warrant["materiality"] == "NOT_MATERIAL_ADVERSE"
    assert warrant["impact_strength"] == "MINOR"
    assert "NON_CORE_SECURITY_ONLY" in warrant["reason_codes"]

    spac, _ = _parse_completion(
        _response(_payload("fact")),
        content={
            "headline": "Example Acquisition Corp quarterly report",
            "summary": (
                "If the company does not complete a business combination by March 2027, "
                "it must cease operations and liquidate."
            ),
        },
    )
    assert spac["materiality"] == "NOT_MATERIAL_ADVERSE"
    assert spac["event_realization"] == "PROPOSED_OR_CONDITIONAL"
    assert spac["impact_strength"] == "ROUTINE_OR_NONE"
    assert "SPAC_STRUCTURAL_LIFECYCLE_NOT_TRIGGERED" in spac["reason_codes"]


def test_paid_merger_projection_does_not_treat_acquirer_or_unknown_role_as_target() -> None:
    acquirer_input = _payload("fact")
    acquirer, _ = _parse_completion(
        _response(acquirer_input),
        content={
            "headline": "Acquirer signs definitive merger agreement",
            "summary": "Acquirer will pay $105 per target share, a 44% premium.",
            "semantic_context": {
                "focal_subject": {"role": "ISSUER", "entity_group": "acquirer-group-a"},
                "focal_asset": {
                    "role": "COMMON_EQUITY",
                    "entity_group": "acquirer-equity-a",
                },
                "transaction_role": "ACQUIRER",
                "prior_event_context": [],
                "source_excerpt_complete": True,
            },
        },
    )
    assert acquirer["materiality"] == "MATERIAL_ADVERSE"
    assert acquirer["polarity"] == "ADVERSE"
    assert "PAID_MERGER_OR_CASH_PREMIUM_EXIT" not in acquirer["reason_codes"]

    unknown_input = _payload("boundary")
    unknown, _ = _parse_completion(
        _response(unknown_input),
        content={
            "headline": "Issuer enters a merger at $105 per share, a 44% premium",
            "summary": "The announcement does not identify the focal transaction role.",
        },
    )
    assert unknown["polarity"] == "NEUTRAL"
    assert unknown["impact_strength"] == "ROUTINE_OR_NONE"
    assert "PAID_MERGER_OR_CASH_PREMIUM_EXIT" not in unknown["reason_codes"]


def test_spac_deadline_projection_preserves_independent_current_hard_downside() -> None:
    parsed, _ = _parse_completion(
        _response(_payload("fact")),
        content={
            "headline": "Example Acquisition Corp quarterly report",
            "summary": (
                "If the company does not complete a business combination by March 2027, "
                "it must cease operations and liquidate. Management states there is "
                "substantial doubt about the company's ability to continue as a going concern."
            ),
        },
    )
    assert parsed["materiality"] == "MATERIAL_ADVERSE"
    assert parsed["risk_status"] == "ACTIVE"
    assert "SPAC_STRUCTURAL_LIFECYCLE_NOT_TRIGGERED" not in parsed["reason_codes"]


def test_ads_cross_listing_migration_does_not_imply_issuer_wide_adverse_exit() -> None:
    parsed, _ = _parse_completion(
        _response(_payload("fact")),
        content={
            "headline": "Issuer will delist its ADSs from Nasdaq",
            "summary": "The ordinary shares remain listed and trading on Euronext Paris.",
            "semantic_context": {
                "focal_subject": {"role": "ISSUER", "entity_group": "issuer-group-a"},
                "focal_asset": {"role": "ADS", "entity_group": "ads-group-a"},
                "prior_event_context": [],
                "source_excerpt_complete": True,
            },
        },
    )
    assert parsed["materiality"] == "NOT_MATERIAL_ADVERSE"
    assert parsed["polarity"] == "NEUTRAL"
    assert "ADS_OR_CROSS_LISTING_MIGRATION" in parsed["reason_codes"]


def test_incomplete_decisive_clause_forces_unresolved_axes_to_unclear() -> None:
    parsed, _ = _parse_completion(
        _response(_payload("fact")),
        content={
            "headline": "Exchange notice",
            "summary": "The company received a notice from staff advising the Company",
        },
    )
    assert parsed["materiality"] == "UNCLEAR"
    assert parsed["polarity"] == "UNCLEAR"
    assert parsed["impact_strength"] == "UNCLEAR"
    assert parsed["event_realization"] == "UNCLEAR"
    assert parsed["risk_status"] == "UNCLEAR"
    assert "SOURCE_TEXT_TRUNCATED_OR_INCOMPLETE" in parsed["reason_codes"]


def test_arbitration_cannot_escalate_without_enumerated_source_trigger(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "inputs.jsonl"
    env_file = tmp_path / "secret.env"
    output_dir = tmp_path / "output"
    _source(input_path)
    _env_file(env_file)
    call_number = 0

    def requester(url, headers, payload, timeout):
        nonlocal call_number
        call_number += 1
        return _response(_payload("fact") if call_number == 3 else _payload("boundary"))

    with pytest.raises(Exception, match="UNSUPPORTED_ARBITRATION_ESCALATION"):
        adjudicate_multiview(
            input_path=input_path,
            env_file=env_file,
            output_dir=output_dir,
            max_workers=1,
            max_attempts=1,
            requester=requester,
            sleeper=lambda _: None,
        )


def test_arbitration_escalation_is_allowed_with_common_equity_suspension_trigger(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "inputs.jsonl"
    env_file = tmp_path / "secret.env"
    output_dir = tmp_path / "output"
    _source(
        input_path,
        extra_content={
            "headline": "Exchange suspended trading in issuer common stock",
            "summary": "Common stock trading has been suspended and will be delisted.",
            "semantic_context": {
                "focal_subject": {"role": "ISSUER", "entity_group": "issuer-group-a"},
                "focal_asset": {
                    "role": "COMMON_EQUITY",
                    "entity_group": "asset-group-a",
                },
                "prior_event_context": [],
                "source_excerpt_complete": True,
            },
        },
    )
    _env_file(env_file)
    call_number = 0

    def requester(url, headers, payload, timeout):
        nonlocal call_number
        call_number += 1
        if call_number < 3:
            return _response(_payload("boundary"))
        value = _payload("fact")
        value["reason_codes"] = [
            *value["reason_codes"],
            "FORMAL_DELISTING_SUSPENSION_OR_TERMINATION",
            "ISSUER_COMMON_EQUITY_DIRECTLY_AFFECTED",
        ]
        return _response(value)

    adjudicate_multiview(
        input_path=input_path,
        env_file=env_file,
        output_dir=output_dir,
        max_workers=1,
        max_attempts=1,
        requester=requester,
        sleeper=lambda _: None,
    )
    row = json.loads((output_dir / RESULTS_NAME).read_text(encoding="utf-8"))
    assert row["final"]["materiality"] == "MATERIAL_ADVERSE"


def test_literal_post_event_return_leakage_is_rejected_before_provider(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "inputs.jsonl"
    env_file = tmp_path / "secret.env"
    output_dir = tmp_path / "output"
    _source(
        input_path,
        extra_content={"summary": "one_day_crash candidate ret_1d <= -20%; value=-0.8"},
    )
    _env_file(env_file)
    called = False

    def requester(url, headers, payload, timeout):
        nonlocal called
        called = True
        return _response(_payload("boundary"))

    with pytest.raises(ValueError, match="prohibited post-event supervision text"):
        adjudicate_multiview(
            input_path=input_path,
            env_file=env_file,
            output_dir=output_dir,
            requester=requester,
        )
    assert called is False


def test_volume_crash_outcome_leakage_is_rejected_before_provider(tmp_path: Path) -> None:
    input_path = tmp_path / "inputs.jsonl"
    env_file = tmp_path / "secret.env"
    output_dir = tmp_path / "output"
    _source(
        input_path,
        extra_content={
            "headline": "WAYS volume_crash candidate",
            "summary": "ret_1d <= -15%; value=ret_1d=-0.99;volume_ratio=60.0",
        },
    )
    _env_file(env_file)
    called = False

    def requester(url, headers, payload, timeout):
        nonlocal called
        called = True
        return _response(_payload("boundary"))

    with pytest.raises(ValueError, match="prohibited post-event supervision text"):
        adjudicate_multiview(
            input_path=input_path,
            env_file=env_file,
            output_dir=output_dir,
            requester=requester,
        )
    assert called is False
