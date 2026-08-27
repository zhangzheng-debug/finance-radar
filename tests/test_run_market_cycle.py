from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_market_cycle as market_cycle


def test_market_cycle_maps_schedules_and_drains_bounded_jobs(monkeypatch) -> None:
    connection = Mock()
    mapping = Mock(return_value={"mode": "APPLY", "mapped": 2})
    schedule = Mock(return_value=4)
    followups = Mock(side_effect=[1, 3])
    pending = Mock(return_value={"completed": 5, "errors": 0})
    monkeypatch.setattr(market_cycle, "map_event_assets", mapping)
    monkeypatch.setattr(market_cycle, "schedule_jobs", schedule)
    monkeypatch.setattr(market_cycle, "schedule_followup_jobs", followups)
    monkeypatch.setattr(market_cycle, "run_pending", pending)

    result = market_cycle.run_market_cycle(
        connection,
        mapping_mode="apply",
        api_key="unit-key",
        timeout=12,
        request_limit=7,
        today=date(2026, 8, 27),
    )

    mapping.assert_called_once_with(
        connection,
        freshness_days=0,
        today=date(2026, 8, 27),
        apply=True,
    )
    schedule.assert_called_once_with(
        connection,
        freshness_days=0,
        today=date(2026, 8, 27),
    )
    pending.assert_called_once_with(
        connection,
        api_key="unit-key",
        timeout=12.0,
        max_exact_requests_per_provider=7,
    )
    assert result["market"]["scheduled"] == 4
    assert result["market"]["followups_scheduled"] == 4
    assert result["configuration"] == {
        "mapping_mode": "apply",
        "freshness_days": 0,
        "request_limit": 7,
        "issuer_directory_loaded": False,
        "issuer_directory_error": None,
    }
    assert result["no_trading"] is True


def test_market_cycle_can_disable_mapping_without_disabling_crypto_quotes(monkeypatch) -> None:
    mapping = Mock()
    pending = Mock(return_value={"completed": 1, "errors": 0})
    monkeypatch.setattr(market_cycle, "map_event_assets", mapping)
    monkeypatch.setattr(market_cycle, "schedule_jobs", Mock(return_value=0))
    monkeypatch.setattr(
        market_cycle, "schedule_followup_jobs", Mock(side_effect=[0, 0])
    )
    monkeypatch.setattr(market_cycle, "run_pending", pending)

    result = market_cycle.run_market_cycle(
        Mock(),
        mapping_mode="disabled",
        api_key="",
        timeout=10,
        request_limit=6,
        today=date(2026, 8, 27),
    )

    mapping.assert_not_called()
    assert result["mapping"]["mode"] == "DISABLED"
    assert pending.call_args.kwargs["api_key"] == ""


def test_market_cycle_report_is_valid_json(tmp_path: Path) -> None:
    target = tmp_path / "market" / "latest.json"
    market_cycle.write_report(target, {"market": {"completed": 2}})

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "market": {"completed": 2}
    }
    assert list(target.parent.glob("*.tmp")) == []


def test_market_cycle_loads_runtime_issuer_directory(tmp_path: Path, monkeypatch) -> None:
    index = tmp_path / "company_tickers_exchange.json"
    index.write_text(
        json.dumps(
            {
                "fields": ["cik", "name", "ticker", "exchange"],
                "data": [[1045810, "NVIDIA CORP", "NVDA", "Nasdaq"]],
            }
        ),
        encoding="utf-8",
    )
    mapping = Mock(return_value={"mode": "APPLY", "issuer_directory_records": 1})
    monkeypatch.setattr(market_cycle, "map_event_assets", mapping)
    monkeypatch.setattr(market_cycle, "schedule_jobs", Mock(return_value=0))
    monkeypatch.setattr(
        market_cycle, "schedule_followup_jobs", Mock(side_effect=[0, 0])
    )
    monkeypatch.setattr(
        market_cycle, "run_pending", Mock(return_value={"completed": 0, "errors": 0})
    )

    result = market_cycle.run_market_cycle(
        Mock(),
        mapping_mode="apply",
        api_key="unit-key",
        timeout=10,
        request_limit=6,
        today=date(2026, 8, 27),
        issuer_index_path=index,
    )

    assert mapping.call_args.kwargs["issuer_directory"].record_count == 1
    assert result["configuration"]["issuer_directory_loaded"] is True


def test_malformed_issuer_directory_does_not_stop_macro_market_cycle(
    tmp_path: Path, monkeypatch
) -> None:
    index = tmp_path / "company_tickers_exchange.json"
    index.write_text("{not-json", encoding="utf-8")
    mapping = Mock(return_value={"mode": "APPLY", "issuer_directory_records": 0})
    monkeypatch.setattr(market_cycle, "map_event_assets", mapping)
    monkeypatch.setattr(market_cycle, "schedule_jobs", Mock(return_value=0))
    monkeypatch.setattr(
        market_cycle, "schedule_followup_jobs", Mock(side_effect=[0, 0])
    )
    monkeypatch.setattr(
        market_cycle, "run_pending", Mock(return_value={"completed": 0, "errors": 0})
    )

    result = market_cycle.run_market_cycle(
        Mock(),
        mapping_mode="apply",
        api_key="unit-key",
        timeout=10,
        request_limit=6,
        today=date(2026, 8, 27),
        issuer_index_path=index,
    )

    mapping.assert_called_once()
    assert "issuer_directory" not in mapping.call_args.kwargs
    assert result["configuration"]["issuer_directory_loaded"] is False
    assert result["configuration"]["issuer_directory_error"].startswith(
        "JSONDecodeError:"
    )
