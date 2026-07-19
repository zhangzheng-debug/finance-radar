from __future__ import annotations

from pathlib import Path


INSTALLER = Path(__file__).parents[1] / "deployment" / "systemd" / "install_remote.sh"
BACKUP_UNIT = Path(__file__).parents[1] / "deployment" / "systemd" / "finance-radar-backup.service"
WORKER_UNIT = Path(__file__).parents[1] / "deployment" / "systemd" / "finance-radar-worker.service"


def test_remote_installer_keeps_venv_readable_by_service_account() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    assert 'chown -R finance-radar:finance-radar "$BASE/venv"' in source
    assert "runuser -u finance-radar" in source
    assert "import sklearn, sklearn.pipeline" in source
    assert 'sklearn.__version__ == "1.8.0"' in source


def test_long_running_units_restart_worker_and_keep_extended_backups() -> None:
    worker = WORKER_UNIT.read_text(encoding="utf-8")
    backup = BACKUP_UNIT.read_text(encoding="utf-8")
    assert "--interval 300" in worker
    assert "Restart=on-failure" in worker
    assert "RestartSec=20" in worker
    assert "--retention 30 --weekly-retention 12" in backup
