from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.services import (  # noqa: E402
    CAPTURE_INTERPRETATION_CONTRACT,
    CAPTURE_INTERPRETATION_PROMPT_SHA256,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_CHEAP_TEXT_MODEL,
    CaptureInterpretationContractError,
    DeepSeekCaptureInterpretationError,
    DeepSeekCaptureInterpretationProvider,
    llm_assisted_interpretation,
    normalized_capture_input,
)
from app.services.capture_interpretation import (  # noqa: E402
    CAPTURE_INTERPRETATION_PROMPT_VERSION,
)
from app.storage import LedgerRepository, OperationsRepository  # noqa: E402


ALLOWED_LOCAL_ENV_NAMES = {
    "DEEPSEEK_API_KEY",
    "FINANCE_RADAR_DB",
    "FINANCE_RADAR_OPS_DB",
    "FINANCE_RADAR_CAPTURE_LLM_ENABLED",
    "FINANCE_RADAR_CAPTURE_LLM_PROVIDER",
    "FINANCE_RADAR_CAPTURE_LLM_MODEL",
    "FINANCE_RADAR_CAPTURE_LLM_BASE_URL",
    "FINANCE_RADAR_CAPTURE_LLM_TIMEOUT_SECONDS",
    "FINANCE_RADAR_CAPTURE_LLM_MAX_TOKENS",
    "FINANCE_RADAR_CAPTURE_LLM_DAILY_CNY_CAP",
    "FINANCE_RADAR_CAPTURE_LLM_DAILY_REQUEST_CAP",
}


def load_local_env(path: Path) -> None:
    """Load only the capture-provider allowlist without printing values."""

    if not path.exists():
        return
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("capture env file must be a regular non-symlink file")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name not in ALLOWED_LOCAL_ENV_NAMES or name in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[name] = value


RESERVED_CNY_PER_REQUEST = 0.02
MAX_ATTEMPTS = 4


def _credential() -> str:
    direct = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if direct:
        return direct
    credentials_dir = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    if not credentials_dir:
        return ""
    path = Path(credentials_dir) / "deepseek_api_key"
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 512:
        return ""
    return path.read_text(encoding="utf-8").strip()


def _safe_result(**values: Any) -> None:
    print(json.dumps(values, ensure_ascii=False, sort_keys=True))


def run(args: argparse.Namespace) -> int:
    load_local_env(args.env_file.resolve())
    settings = Settings.from_env()
    if not settings.capture_llm_enabled:
        raise RuntimeError("CAPTURE_LLM_DISABLED")
    if settings.capture_llm_provider != "deepseek":
        raise RuntimeError("CAPTURE_LLM_PROVIDER_NOT_DEEPSEEK")
    if settings.capture_llm_model != DEEPSEEK_CHEAP_TEXT_MODEL:
        raise RuntimeError("CAPTURE_LLM_MODEL_NOT_APPROVED_CHEAPEST")
    if settings.capture_llm_base_url.rstrip("/") != DEEPSEEK_BASE_URL:
        raise RuntimeError("CAPTURE_LLM_BASE_URL_NOT_OFFICIAL")
    api_key = _credential()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY_MISSING")
    ledger = LedgerRepository(settings.ledger_db)
    operations = OperationsRepository(settings.operations_db)

    event_data = ledger.event_detail(args.event_id)
    if event_data is None:
        raise RuntimeError("EVENT_NOT_FOUND")
    event = dict(event_data.get("event") or {})
    capture = next(
        (
            dict(item)
            for item in ledger.captured_sources(args.event_id)
            if str(item.get("observation_id") or "") == args.observation_id
        ),
        None,
    )
    if capture is None:
        raise RuntimeError("CAPTURE_NOT_FOUND")
    normalized = normalized_capture_input(event, capture)
    provider = DeepSeekCaptureInterpretationProvider(
        api_key=api_key,
        model=settings.capture_llm_model,
        base_url=settings.capture_llm_base_url,
        timeout_seconds=settings.capture_llm_timeout_seconds,
        max_tokens=settings.capture_llm_max_tokens,
    )
    run_id, inserted = operations.enqueue_capture_interpretation(
        args.event_id,
        args.observation_id,
        normalized,
        contract_version=CAPTURE_INTERPRETATION_CONTRACT,
        prompt_version=CAPTURE_INTERPRETATION_PROMPT_VERSION,
        prompt_sha256=CAPTURE_INTERPRETATION_PROMPT_SHA256,
        provider=provider.provider_name,
        model_snapshot=provider.model_snapshot,
        external_call=True,
    )
    matching = next(
        (
            row
            for row in operations.capture_interpretation_runs(args.event_id, limit=100)
            if row.get("interpretation_id") == run_id
        ),
        None,
    )
    if not inserted and matching and matching.get("status") == "COMPLETED":
        _safe_result(
            status="CACHED",
            interpretation_id=run_id,
            event_id=args.event_id,
            observation_id=args.observation_id,
            provider=provider.provider_name,
            model=provider.model_snapshot,
            usage=matching.get("usage") or {},
            canonical_state_unchanged=True,
            no_trading=True,
        )
        return 0

    claim = operations.claim_capture_interpretation(
        provider=provider.provider_name,
        daily_request_cap=settings.capture_llm_daily_request_cap,
        daily_cny_cap=settings.capture_llm_daily_cny_cap,
        reserve_cny=RESERVED_CNY_PER_REQUEST,
        lease_seconds=max(60, int(settings.capture_llm_timeout_seconds) + 30),
        max_attempts=MAX_ATTEMPTS,
        interpretation_id=run_id,
    )
    if not claim.get("claimed"):
        reason = str(claim.get("reason") or "CLAIM_REJECTED")
        if reason.startswith("DAILY_"):
            raise RuntimeError("CAPTURE_LLM_" + reason)
        raise RuntimeError("CAPTURE_LLM_JOB_" + reason)

    usage: dict[str, Any] = {}
    try:
        started_at = time.perf_counter()
        model_output, usage = provider.interpret(normalized)
        latency_ms = (time.perf_counter() - started_at) * 1000.0
        output = llm_assisted_interpretation(event, capture, model_output)
        operations.complete_claimed_capture_interpretation(
            run_id,
            str(claim["attempt_id"]),
            str(claim["lease_token"]),
            output,
            guardrails={
                "source_text_untrusted": True,
                "strict_json_contract": True,
                "quote_substrings_validated": True,
                "prompt_injection_failed_closed": True,
                "thinking_disabled": True,
                "tools_allowed": False,
                "canonical_mutation": False,
                "used_as_model_feature": False,
            },
            usage=usage,
            latency_ms=latency_ms,
        )
    except Exception as exc:
        failure_usage = dict(getattr(exc, "usage", None) or usage)
        retryable = bool(getattr(exc, "retryable", False))
        error_class = str(getattr(exc, "error_class", type(exc).__name__))
        if isinstance(exc, CaptureInterpretationContractError):
            retryable = False
            error_class = "SERVER_CONTRACT_REJECTED"
        operations.fail_claimed_capture_interpretation(
            run_id,
            str(claim["attempt_id"]),
            str(claim["lease_token"]),
            error=str(exc),
            error_class=error_class,
            usage=failure_usage,
            retryable=retryable,
            max_attempts=MAX_ATTEMPTS,
            backoff_seconds=60,
        )
        raise

    _safe_result(
        status="COMPLETED",
        interpretation_id=run_id,
        event_id=args.event_id,
        observation_id=args.observation_id,
        provider=provider.provider_name,
        model=provider.model_snapshot,
        usage=usage,
        canonical_state_unchanged=True,
        no_trading=True,
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Generate one receipt-bound DeepSeek capture interpretation."
    )
    result.add_argument("--event-id", required=True)
    result.add_argument("--observation-id", required=True)
    result.add_argument("--env-file", type=Path, default=ROOT / ".env.local")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        return run(args)
    except Exception as exc:
        _safe_result(status="FAILED", error=str(exc)[:240], no_trading=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
