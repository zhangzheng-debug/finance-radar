from __future__ import annotations

import json

import pytest

from app.services.capture_interpretation import normalized_capture_input
from app.services.deepseek_capture_interpretation import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_CHEAP_TEXT_MODEL,
    DeepSeekCaptureInterpretationError,
    DeepSeekCaptureInterpretationProvider,
    estimate_flash_peak_cny,
)


def _normalized() -> dict[str, object]:
    return normalized_capture_input(
        {
            "event_id": "event-gold",
            "current_version": 2,
            "public_state": "excluded",
            "event_family": "macro_policy",
            "event_type": "monetary_policy",
        },
        {
            "source_name": "OpenNews",
            "source_type": "aggregated_discovery",
            "authority_tier": "P2_experimental",
            "source_title": "Markets awaited the Fed meeting minutes while gold rose.",
            "source_excerpt": "Markets awaited the Fed meeting minutes while gold rose.",
            "source_published_at": "2026-08-19T00:23:30+00:00",
            "local_received_at": "2026-08-19T08:09:23+00:00",
            "latest_revision_no": 1,
            "semantic_content_sha256": "a" * 64,
            "capture_receipt_sha256": "b" * 64,
        },
    )


def _model_output() -> dict[str, object]:
    return {
        "one_line_zh": "来源称市场正在等待美联储会议纪要，同时黄金上涨。",
        "what_source_says": [
            {
                "text_zh": "市场正在等待美联储会议纪要。",
                "quote": "Markets awaited the Fed meeting minutes",
            }
        ],
        "what_source_does_not_prove_zh": ["没有证明会议纪要已经发布。"],
        "actors": [{"text": "Fed", "role": "CONTEXT", "quote": "Fed"}],
        "affected_assets": ["GOLD"],
        "modality": "COMMENTARY",
        "missing_to_change_state_zh": ["需要美联储官方网站原文。"],
        "prompt_injection_suspected": False,
    }


def test_deepseek_flash_request_is_official_json_nonthinking_and_bounded() -> None:
    captured: dict[str, object] = {}

    def requester(url, headers, payload, timeout):
        captured.update(url=url, headers=headers, payload=payload, timeout=timeout)
        return {
            "id": "response-1",
            "model": DEEPSEEK_CHEAP_TEXT_MODEL,
            "choices": [
                {
                    "message": {"content": json.dumps(_model_output(), ensure_ascii=False)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 1000,
                "prompt_cache_hit_tokens": 250,
                "prompt_cache_miss_tokens": 750,
                "completion_tokens": 200,
                "total_tokens": 1200,
            },
        }

    provider = DeepSeekCaptureInterpretationProvider(
        api_key="unit-test-secret",
        requester=requester,
    )
    output, usage = provider.interpret(_normalized())

    assert captured["url"] == DEEPSEEK_BASE_URL + "/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer unit-test-secret"
    assert captured["payload"]["model"] == DEEPSEEK_CHEAP_TEXT_MODEL
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert captured["payload"]["thinking"] == {"type": "disabled"}
    assert captured["payload"]["stream"] is False
    assert captured["payload"]["max_tokens"] == 700
    assert "raw_json" not in json.dumps(captured["payload"], ensure_ascii=False)
    assert output == _model_output()
    assert usage["thinking_disabled"] is True
    assert usage["estimated_cny"] == 0.004075


def test_deepseek_cost_uses_peak_price_and_charges_unclassified_input_as_miss() -> None:
    usage = estimate_flash_peak_cny(
        {
            "prompt_tokens": 100,
            "prompt_cache_hit_tokens": 25,
            "completion_tokens": 50,
        }
    )
    assert usage["prompt_cache_miss_tokens"] == 75
    assert usage["estimated_cny"] == 0.0006775
    assert usage["billing_currency"] == "CNY"


def test_deepseek_provider_rejects_unapproved_model_and_hallucinated_quote() -> None:
    with pytest.raises(ValueError, match="approved cheapest model"):
        DeepSeekCaptureInterpretationProvider(
            api_key="unit-test-secret",
            model="deepseek-v4-pro",
        )

    invalid = _model_output()
    invalid["what_source_says"] = [
        {"text_zh": "虚构事实。", "quote": "The Fed released the minutes"}
    ]

    def requester(url, headers, payload, timeout):
        return {
            "choices": [
                {"message": {"content": json.dumps(invalid)}, "finish_reason": "stop"}
            ],
            "usage": {},
        }

    provider = DeepSeekCaptureInterpretationProvider(
        api_key="unit-test-secret",
        requester=requester,
    )
    with pytest.raises(DeepSeekCaptureInterpretationError, match="UNSUPPORTED_SOURCE_QUOTE") as error:
        provider.interpret(_normalized())
    assert error.value.error_class == "CONTRACT_REJECTED"
    assert error.value.retryable is True


def test_deepseek_contract_failure_carries_billable_usage() -> None:
    invalid = _model_output()
    invalid["what_source_says"] = [
        {"text_zh": "虚构事实。", "quote": "The Fed released the minutes"}
    ]

    def requester(url, headers, payload, timeout):
        return {
            "id": "billed-invalid-response",
            "choices": [
                {"message": {"content": json.dumps(invalid)}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }

    provider = DeepSeekCaptureInterpretationProvider(
        api_key="unit-test-secret",
        requester=requester,
    )
    with pytest.raises(DeepSeekCaptureInterpretationError) as error:
        provider.interpret(_normalized())
    assert error.value.usage["estimated_cny"] > 0
    assert error.value.usage["response_id"] == "billed-invalid-response"


def test_deepseek_provider_restores_case_only_quotes_to_captured_text() -> None:
    normalized = _normalized()
    normalized["title"] = "MARKETS AWAITED THE FED MEETING MINUTES WHILE GOLD ROSE."
    normalized["summary_or_content"] = normalized["title"]
    output = _model_output()
    output["what_source_says"][0]["quote"] = "Markets awaited the Fed meeting minutes"
    output["actors"] = [{"text": "Fed", "role": "CONTEXT", "quote": "Fed"}]

    def requester(url, headers, payload, timeout):
        return {
            "choices": [
                {"message": {"content": json.dumps(output)}, "finish_reason": "stop"}
            ],
            "usage": {},
        }

    provider = DeepSeekCaptureInterpretationProvider(
        api_key="unit-test-secret",
        requester=requester,
    )
    validated, _ = provider.interpret(normalized)

    assert validated["what_source_says"][0]["quote"] == (
        "MARKETS AWAITED THE FED MEETING MINUTES"
    )
    assert validated["actors"][0]["quote"] == "FED"


def test_deepseek_provider_narrows_extra_fields_asset_objects_and_ungrounded_numbers() -> None:
    output = _model_output()
    output["affected_assets"] = [{"symbol": "BTC"}]
    output["one_line_zh"] = "来源称价格上涨了 20%，涉及 BTC。"
    output["what_source_says"][0]["text_zh"] = "市场已经等待了 20 天。"
    output["unexpected"] = "must be removed"

    def requester(url, headers, payload, timeout):
        return {
            "choices": [
                {"message": {"content": json.dumps(output)}, "finish_reason": "stop"}
            ],
            "usage": {},
        }

    provider = DeepSeekCaptureInterpretationProvider(
        api_key="unit-test-secret",
        requester=requester,
    )
    validated, _ = provider.interpret(_normalized())

    assert set(validated) == set(_model_output())
    assert validated["affected_assets"] == ["GOLD"]
    assert "20" not in validated["one_line_zh"]
    assert validated["what_source_says"][0]["text_zh"] == "来源表达了所引用的内容。"
