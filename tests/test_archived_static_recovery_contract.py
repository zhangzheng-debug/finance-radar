from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]
CREATE_BACKUP = ROOT / "deployment" / "systemd" / "create_migration_backup.sh"
ACTIVATE_RESTORE = ROOT / "deployment" / "systemd" / "activate_prepared_restore.sh"


def test_migration_archive_preserves_only_the_public_backup_status_document() -> None:
    source = CREATE_BACKUP.read_text(encoding="utf-8")

    assert "offhost-status.json" in source
    assert "PUBLIC_STATUS_SOURCE=/var/www/finance-radar-terminal/offhost-status.json" in source
    assert "cp -a /var/www/finance-radar-terminal" not in source
    assert "static terminal is retired" in source


def test_migration_archive_materializes_data_from_a_verified_recovery_bundle() -> None:
    source = CREATE_BACKUP.read_text(encoding="utf-8")

    assert "verify_backup_receipt.py" in source
    assert "--required-kind recovery_bundle" in source
    assert "MIGRATION_RECOVERY_BUNDLE.json" in source
    assert "verified_full_recovery_bundle" in source
    assert "Replace copied live databases with transactionally consistent SQLite snapshots" not in source


def test_restore_never_reinstalls_the_retired_static_terminal() -> None:
    source = ACTIVATE_RESTORE.read_text(encoding="utf-8")

    assert "offhost-status.json" in source
    assert "rm -f -- /var/www/finance-radar-terminal/index.html" in source
    assert '"$BASE/var/www/finance-radar-terminal/index.html"' not in source
    assert "install -m 0644 -o root -g root \"$PUBLIC_STATUS_SOURCE\" \"$PUBLIC_STATUS_TARGET\"" in source


def test_archived_material_cannot_be_used_as_a_static_deployment_runbook() -> None:
    for relative in (
        "claudeUI/README.md",
        "claudeUI/RECOMMENDATIONS.md",
        "claudeUI/QA_REPORT_2026-07-21.md",
        "reports/aws_migration_20260721.md",
        "reports/aws_ui_deployment_20260721.md",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "2026-08-05" in source

    recommendations = (ROOT / "claudeUI" / "RECOMMENDATIONS.md").read_text(encoding="utf-8")
    assert "该技术路线已废止" in recommendations
    assert "并行生产路径" in recommendations


def test_archived_prototype_labels_itself_as_non_production() -> None:
    source = (ROOT / "claudeUI" / "prototype" / "index.html").read_text(encoding="utf-8")

    assert "ARCHIVED PROTOTYPE · NOT A PRODUCTION UI" in source
    assert "历史设计原型已退役 · 非生产" in source
    assert "历史冻结原型 · 非生产" in source
