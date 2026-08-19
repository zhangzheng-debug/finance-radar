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
