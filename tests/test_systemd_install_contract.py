from __future__ import annotations

from pathlib import Path


INSTALLER = Path(__file__).parents[1] / "deployment" / "systemd" / "install_remote.sh"
BACKUP_UNIT = Path(__file__).parents[1] / "deployment" / "systemd" / "finance-radar-backup.service"
WORKER_UNIT = Path(__file__).parents[1] / "deployment" / "systemd" / "finance-radar-worker.service"
WORKER_SEND_OVERRIDE = Path(__file__).parents[1] / "deployment" / "systemd" / "finance-radar-worker-send.conf"
WEB_UNIT = Path(__file__).parents[1] / "deployment" / "systemd" / "finance-radar-web.service"
ADMIN_UNIT = Path(__file__).parents[1] / "deployment" / "systemd" / "finance-radar-admin.service"
ACTIVATOR = Path(__file__).parents[1] / "deployment" / "systemd" / "activate_prepared_restore.sh"


def test_remote_installer_keeps_venv_readable_by_service_account() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    assert 'chown -R finance-radar:finance-radar "$BASE/venv"' in source
    assert "runuser -u finance-radar" in source
    assert "import sklearn, sklearn.pipeline" in source
    assert 'sklearn.__version__ == "1.8.0"' in source


def test_remote_installer_uses_explicit_current_public_url() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    assert "PUBLIC_WEB_URL=${6:-https://radar.18-208-34-152.sslip.io:8443/radar}" in source
    assert '"FINANCE_RADAR_WEB_URL=$PUBLIC_WEB_URL"' in source
    assert "radar.167-172-69-16.sslip.io" not in source


def test_long_running_units_restart_worker_disable_formal_auto_verify_and_keep_one_bundle() -> None:
    worker = WORKER_UNIT.read_text(encoding="utf-8")
    worker_send_override = WORKER_SEND_OVERRIDE.read_text(encoding="utf-8")
    backup = BACKUP_UNIT.read_text(encoding="utf-8")
    assert "--interval 300" in worker
    assert "--no-light-verify" in worker
    assert "--send" in worker_send_override
    assert "--no-light-verify" in worker_send_override
    assert "Restart=on-failure" in worker
    assert "RestartSec=20" in worker
    assert "--retention 1 --weekly-retention 0" in backup


def test_installer_and_restore_refresh_existing_telegram_override_without_reenabling_verify() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    activator = ACTIVATOR.read_text(encoding="utf-8")
    for source in (installer, activator):
        assert "finance-radar-worker.service.d/telegram-send.conf" in source
        assert "finance-radar-worker-send.conf" in source
        assert "--no-light-verify" in WORKER_SEND_OVERRIDE.read_text(encoding="utf-8")


def test_public_web_uses_a_fixed_minimal_environment_without_admin_token() -> None:
    web = WEB_UNIT.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    assert "EnvironmentFile=/etc/finance-radar-public.env" in web
    assert "EnvironmentFile=/etc/finance-radar.env" not in web
    assert "Environment=FINANCE_RADAR_UI_ROLE=public" in web
    assert "Environment=FINANCE_RADAR_SHOW_DEBUG=0" in web
    assert "UnsetEnvironment=FINANCE_RADAR_ADMIN_TOKEN" in web
    assert "ReadWritePaths=" not in web
    assert "InaccessiblePaths=" in web
    for protected_path in (
        "/etc/finance-radar.env",
        "/opt/finance-radar/current/.env",
        "/opt/finance-radar/shared/data",
        "/opt/finance-radar/shared/reports",
    ):
        assert protected_path in web
    assert "install -m 0640 -o root -g finance-radar /dev/null /etc/finance-radar-public.env" in installer
    for literal in (
        "FINANCE_RADAR_API_URL=http://127.0.0.1:18000",
        "FINANCE_RADAR_UI_ROLE=public",
        "FINANCE_RADAR_SHOW_DEBUG=0",
    ):
        assert literal in installer
    public_env_block = installer.split("/etc/finance-radar-public.env", 1)[1].split(
        'ln -sfn "$RELEASE"', 1
    )[0]
    assert "FINANCE_RADAR_ADMIN_TOKEN" not in public_env_block
    assert "cp " not in public_env_block
    assert "grep " not in public_env_block


def test_admin_ui_is_manual_loopback_only_and_installed_without_enablement() -> None:
    admin = ADMIN_UNIT.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    assert "EnvironmentFile=/etc/finance-radar.env" in admin
    assert "Environment=FINANCE_RADAR_UI_ROLE=admin" in admin
    assert "--server.address 127.0.0.1" in admin
    assert "--server.port 18502" in admin
    assert "--server.baseUrlPath radar-admin" in admin
    assert "MemoryMax=256M" in admin
    assert "\n[Install]\n" not in admin
    assert "finance-radar-admin.service" in installer
    assert "systemctl enable" not in installer


def test_restore_recreates_public_environment_and_never_enables_admin() -> None:
    source = ACTIVATOR.read_text(encoding="utf-8")
    assert "install -m 0640 -o root -g finance-radar /dev/null /etc/finance-radar-public.env" in source
    public_env_block = source.split("/etc/finance-radar-public.env", 1)[1].split(
        "python3 -m venv", 1
    )[0]
    assert "FINANCE_RADAR_API_URL=http://127.0.0.1:18000" in public_env_block
    assert "FINANCE_RADAR_UI_ROLE=public" in public_env_block
    assert "FINANCE_RADAR_SHOW_DEBUG=0" in public_env_block
    assert "FINANCE_RADAR_ADMIN_TOKEN" not in public_env_block
    enable_line = next(
        line for line in source.splitlines() if line.startswith("systemctl enable --now finance-radar-api")
    )
    assert "finance-radar-admin" not in enable_line
