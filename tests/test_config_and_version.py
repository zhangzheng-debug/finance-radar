from __future__ import annotations

from pathlib import Path
import json
import re

from app import __version__
from app.config import Settings


ROOT = Path(__file__).resolve().parents[1]


def test_package_version_comes_from_repository_version_file() -> None:
    assert __version__ == (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"20\d{2}\.\d{2}\.\d{2}\.\d+", __version__)


def test_rate_limit_proxy_and_capacity_settings_are_environment_driven(monkeypatch) -> None:
    monkeypatch.setenv("FINANCE_RADAR_API_RATE_LIMIT_MAX_CLIENTS", "17")
    monkeypatch.setenv("FINANCE_RADAR_API_TRUSTED_PROXY_HOSTS", "127.0.0.1, ::1, 10.0.0.2")
    settings = Settings.from_env()
    assert settings.api_rate_limit_max_clients == 17
    assert settings.api_trusted_proxy_hosts == ("127.0.0.1", "::1", "10.0.0.2")


def test_rate_limit_capacity_cannot_be_disabled_by_zero(monkeypatch) -> None:
    monkeypatch.setenv("FINANCE_RADAR_API_RATE_LIMIT_MAX_CLIENTS", "0")
    assert Settings.from_env().api_rate_limit_max_clients == 1


def test_capture_llm_is_fail_closed_until_provider_key_and_budget_are_configured(
    monkeypatch,
) -> None:
    monkeypatch.delenv("FINANCE_RADAR_CAPTURE_LLM_ENABLED", raising=False)
    monkeypatch.delenv("FINANCE_RADAR_CAPTURE_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("FINANCE_RADAR_CAPTURE_LLM_MODEL", raising=False)
    monkeypatch.delenv("FINANCE_RADAR_CAPTURE_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("FINANCE_RADAR_CAPTURE_LLM_DAILY_USD_CAP", raising=False)
    monkeypatch.delenv("FINANCE_RADAR_CAPTURE_LLM_DAILY_CNY_CAP", raising=False)
    settings = Settings.from_env()
    assert settings.capture_llm_enabled is False
    assert settings.capture_llm_provider == "disabled"
    assert settings.capture_llm_model == ""
    assert settings.capture_llm_base_url == ""
    assert settings.capture_llm_timeout_seconds == 45.0
    assert settings.capture_llm_max_tokens == 700
    assert settings.capture_llm_daily_usd_cap == 0.0
    assert settings.capture_llm_daily_cny_cap == 0.0
    assert settings.capture_llm_daily_request_cap == 0

    monkeypatch.setenv("FINANCE_RADAR_CAPTURE_LLM_ENABLED", "true")
    monkeypatch.setenv("FINANCE_RADAR_CAPTURE_LLM_PROVIDER", "future-provider")
    monkeypatch.setenv("FINANCE_RADAR_CAPTURE_LLM_MODEL", "future-model")
    monkeypatch.setenv("FINANCE_RADAR_CAPTURE_LLM_BASE_URL", "https://example.test")
    monkeypatch.setenv("FINANCE_RADAR_CAPTURE_LLM_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("FINANCE_RADAR_CAPTURE_LLM_MAX_TOKENS", "9999")
    monkeypatch.setenv("FINANCE_RADAR_CAPTURE_LLM_DAILY_USD_CAP", "-5")
    monkeypatch.setenv("FINANCE_RADAR_CAPTURE_LLM_DAILY_CNY_CAP", "-2")
    monkeypatch.setenv("FINANCE_RADAR_CAPTURE_LLM_DAILY_REQUEST_CAP", "-1")
    enabled = Settings.from_env()
    assert enabled.capture_llm_enabled is True
    assert enabled.capture_llm_provider == "future-provider"
    assert enabled.capture_llm_model == "future-model"
    assert enabled.capture_llm_base_url == "https://example.test"
    assert enabled.capture_llm_timeout_seconds == 1.0
    assert enabled.capture_llm_max_tokens == 1200
    assert enabled.capture_llm_daily_usd_cap == 0.0
    assert enabled.capture_llm_daily_cny_cap == 0.0
    assert enabled.capture_llm_daily_request_cap == 0


def test_scoped_internal_tokens_are_loaded_independently(monkeypatch) -> None:
    monkeypatch.setenv("FINANCE_RADAR_REVIEWER_TOKEN", "review-secret")
    monkeypatch.setenv("FINANCE_RADAR_OPERATOR_TOKEN", "operate-secret")
    settings = Settings.from_env()
    assert settings.reviewer_token == "review-secret"
    assert settings.operator_token == "operate-secret"


def test_reviewer_principals_load_from_systemd_credential(monkeypatch, tmp_path: Path) -> None:
    credential = tmp_path / "reviewer-principals.json"
    credential.write_text(
        json.dumps(
            [
                {
                    "principal_id": "reviewer-a",
                    "role": "REVIEWER",
                    "token": "reviewer-a-token-000000000001",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("FINANCE_RADAR_REVIEWER_PRINCIPALS_JSON", raising=False)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))

    assert Settings.from_env().reviewer_principals == (
        ("reviewer-a", "REVIEWER", "reviewer-a-token-000000000001"),
    )


def test_reviewer_principals_reject_ambiguous_secret_sources(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "reviewer-principals.json").write_text("[]\n", encoding="utf-8")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("FINANCE_RADAR_REVIEWER_PRINCIPALS_JSON", "[]")

    import pytest

    with pytest.raises(ValueError, match="either environment JSON or a systemd credential"):
        Settings.from_env()


def test_internal_and_personal_credentials_must_be_distinct(monkeypatch) -> None:
    import pytest

    shared = "same-secret-must-not-cross-role-boundaries-001"
    monkeypatch.setenv("FINANCE_RADAR_ADMIN_TOKEN", shared)
    monkeypatch.setenv("FINANCE_RADAR_REVIEWER_TOKEN", shared)
    with pytest.raises(ValueError, match="distinct across scopes"):
        Settings.from_env()

    monkeypatch.setenv("FINANCE_RADAR_REVIEWER_TOKEN", "separate-shared-reviewer-secret-002")
    monkeypatch.setenv(
        "FINANCE_RADAR_REVIEWER_PRINCIPALS_JSON",
        json.dumps(
            [
                {
                    "principal_id": "reviewer-a",
                    "role": "REVIEWER",
                    "token": shared,
                }
            ]
        ),
    )
    with pytest.raises(ValueError, match="personal reviewer credentials must be distinct"):
        Settings.from_env()


def test_direct_settings_construction_rejects_duplicate_personal_credentials() -> None:
    import pytest

    duplicate = "duplicate-personal-reviewer-secret-000001"
    with pytest.raises(ValueError, match="other principal"):
        Settings(
            reviewer_principals=(
                ("reviewer-a", "REVIEWER", duplicate),
                ("reviewer-b", "REVIEWER", duplicate),
            )
        )
