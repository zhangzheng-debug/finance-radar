from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.config import Settings
from app.workers import backup_scheduler, continuous, notifier


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        ledger_db=tmp_path / "ledger.sqlite3",
        operations_db=tmp_path / "operations.sqlite3",
        artifact_dir=tmp_path / "artifacts",
        evidence_object_dir=tmp_path / "evidence",
        replay_dir=tmp_path / "replay",
    )


def test_notifier_run_once_preserves_dry_run_and_send_boundary(tmp_path, monkeypatch) -> None:
    completed = SimpleNamespace(returncode=7)
    run = Mock(return_value=completed)
    monkeypatch.setattr(notifier.subprocess, "run", run)
    settings = _settings(tmp_path)

    assert notifier.run_once(settings, send=False) == 7
    command = run.call_args.args[0]
    assert "--dry-run" in command
    assert "--send" not in command

    notifier.run_once(settings, send=True)
    assert "--send" in run.call_args.args[0]


def test_backup_scheduler_run_once_uses_single_generation_policy(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    operations = object()
    repository = Mock(return_value=operations)
    create = Mock(return_value={"status": "VERIFIED"})
    monkeypatch.setattr(backup_scheduler, "OperationsRepository", repository)
    monkeypatch.setattr(backup_scheduler, "create_and_verify", create)

    result = backup_scheduler.run_once(settings, retention=1, weekly_retention=0)

    assert result == {"status": "VERIFIED"}
    repository.assert_called_once_with(settings.operations_db)
    create.assert_called_once_with(
        settings.ledger_db,
        settings.ledger_db.parent / "operational_backups",
        operations,
        retention=1,
        weekly_retention=0,
    )


def test_continuous_once_returns_failure_when_cycle_fails(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(continuous.Settings, "from_env", Mock(return_value=settings))
    monkeypatch.setattr(continuous, "OperationsRepository", Mock(return_value=object()))
    monkeypatch.setattr(
        continuous,
        "execute_cycle",
        Mock(return_value=("FAILED", {"error": "fixture"})),
    )
    monkeypatch.setattr(continuous.signal, "signal", Mock())
    monkeypatch.setattr(continuous.sys, "argv", ["continuous", "--once", "--health-only"])
    monkeypatch.setattr(continuous, "STOP_REQUESTED", False)

    assert continuous.main() == 1
