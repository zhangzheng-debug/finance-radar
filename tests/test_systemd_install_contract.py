from __future__ import annotations

import io
from pathlib import Path
import subprocess
import sys
import tarfile


INSTALLER = Path(__file__).parents[1] / "deployment" / "systemd" / "install_remote.sh"
BACKUP_UNIT = Path(__file__).parents[1] / "deployment" / "systemd" / "finance-radar-backup.service"
BACKUP_QUIESCE_WRAPPER = Path(__file__).parents[1] / "deployment" / "systemd" / "run_backup_quiesced.sh"
WORKER_UNIT = Path(__file__).parents[1] / "deployment" / "systemd" / "finance-radar-worker.service"
WORKER_SEND_OVERRIDE = Path(__file__).parents[1] / "deployment" / "systemd" / "finance-radar-worker-send.conf"
WEB_UNIT = Path(__file__).parents[1] / "deployment" / "systemd" / "finance-radar-web.service"
ADMIN_UNIT = Path(__file__).parents[1] / "deployment" / "systemd" / "finance-radar-admin.service"
REVIEWER_UNIT = Path(__file__).parents[1] / "deployment" / "systemd" / "finance-radar-reviewer.service"
OPERATOR_UNIT = Path(__file__).parents[1] / "deployment" / "systemd" / "finance-radar-operator.service"
ACTIVATOR = Path(__file__).parents[1] / "deployment" / "systemd" / "activate_prepared_restore.sh"
SLICE_UNIT = Path(__file__).parents[1] / "deployment" / "systemd" / "finance-radar.slice"
LLM_UNIT = Path(__file__).parents[1] / "deployment" / "systemd" / "finance-radar-evidence-llm.service"
CAPTURE_INTERPRETATION_UNIT = (
    Path(__file__).parents[1]
    / "deployment"
    / "systemd"
    / "finance-radar-capture-interpretation.service"
)
OVERVIEW_SNAPSHOT_UNIT = (
    Path(__file__).parents[1]
    / "deployment"
    / "systemd"
    / "finance-radar-overview-snapshot.service"
)
OVERVIEW_SNAPSHOT_TIMER = (
    Path(__file__).parents[1]
    / "deployment"
    / "systemd"
    / "finance-radar-overview-snapshot.timer"
)
RECEIPT_VALIDATOR = Path(__file__).parents[1] / "deployment" / "systemd" / "verify_backup_receipt.py"
CODE_ONLY_VALIDATOR = (
    Path(__file__).parents[1]
    / "deployment"
    / "systemd"
    / "verify_code_only_release.py"
)
LOCAL_LLM_INSTALLER = Path(__file__).parents[1] / "deployment" / "systemd" / "install_local_evidence_model.sh"
MIGRATION_BACKUP = Path(__file__).parents[1] / "deployment" / "systemd" / "create_migration_backup.sh"


def _remote_archive_preflight_source() -> str:
    source = INSTALLER.read_text(encoding="utf-8")
    return source.split('python3 - "$ARCHIVE" <<\'PY\'\n', 1)[1].split(
        "\nPY\n\nensure_public_web_principal()", 1
    )[0]


def test_remote_archive_preflight_rejects_streamlit_secrets_before_unpacking(tmp_path: Path) -> None:
    archive = tmp_path / "candidate.tgz"
    payload = b"must-not-be-packaged\n"
    with tarfile.open(archive, "w:gz") as handle:
        member = tarfile.TarInfo(".streamlit/secrets.toml")
        member.size = len(payload)
        handle.addfile(member, io.BytesIO(payload))
    preflight = tmp_path / "remote_archive_preflight.py"
    preflight.write_text(_remote_archive_preflight_source(), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(preflight), str(archive)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "sensitive member path" in result.stderr


def test_remote_installer_keeps_venv_readable_by_service_account() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    assert 'chown -R finance-radar:finance-radar "$BASE/venv"' in source
    assert "runuser -u finance-radar" in source
    assert "import sklearn, sklearn.pipeline" in source
    assert 'sklearn.__version__ == "1.8.0"' in source


def test_code_only_mode_skips_expensive_recovery_and_dependency_work_fail_closed() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    backup_unit = BACKUP_UNIT.read_text(encoding="utf-8")
    backup_wrapper = BACKUP_QUIESCE_WRAPPER.read_text(encoding="utf-8")
    validator = CODE_ONLY_VALIDATOR.read_text(encoding="utf-8")

    assert 'DEPLOY_MODE=${FINANCE_RADAR_DEPLOY_MODE:-full}' in source
    assert 'full|code-only' in source
    assert "code-only deployment must be launched with the active release installer" in source
    assert 'if [ "$DEPLOY_MODE" = full ]; then' in source
    assert "verify_code_only_candidate_before_candidate_execution" in source
    assert "require_recent_verified_backup_record" in source
    assert 'trusted_validator="$PREVIOUS_RELEASE/deployment/systemd/verify_code_only_release.py"' in source
    assert 'python3 "$trusted_validator" contract' in source
    assert 'python3 "$trusted_validator" backup' in source
    assert "FINANCE_RADAR_CODE_ONLY_BACKUP_MAX_AGE_SECONDS:-93600" in source
    assert "<= 93600" in source
    assert "reused_verified_daily" in source
    assert "immutable=1" not in validator
    assert "MAX_BACKUP_AGE_SECONDS = 93_600" in validator
    assert 'ALLOWED_CHANGE_PREFIXES = ("app/web/", ".streamlit/")' in validator
    assert 'ALLOWED_CHANGE_FILES = {"VERSION"}' in validator
    assert "candidate contains generated Python bytecode" in validator
    assert "release content outside the public-Web whitelist changed" in validator
    assert 'inventory[relative] = ("directory", 0, "")' in validator
    assert "current_sha = _sha256(candidate)" in validator
    assert 'expected_bundle_files = set(manifest_paths) | {"manifest.json"}' in validator
    assert "actual_sha = digest.hexdigest()" in backup_wrapper
    assert 'expected_bundle_files = set(manifest_paths) | {"manifest.json"}' in backup_wrapper
    assert "latest-verified-backup.json" in source
    assert "/var/lib/finance-radar" in backup_unit
    assert 'PREDEPLOY_BACKUP_RUN_ID=""' in source
    assert "predeploy_backup_run_id=%s" in source
    assert 'ACTIVATION_PENDING="$RELEASE_RECORDS/.ACTIVATION.pending.$$"' in source
    assert "candidate archive must not contain release-records" in source
    assert "activation record target already exists or is unsafe" in source
    assert 'mv -f -- "$ACTIVATION_PENDING" "$RELEASE_RECORDS/ACTIVATION.txt"' in source
    assert "committed activation record failed validation" in source
    assert source.index('mv -f -- "$ACTIVATION_PENDING"') < source.index("trap - ERR", source.index('mv -f -- "$ACTIVATION_PENDING"'))
    assert "activation_warning=predeploy_hold_cleanup_failed" in source
    assert 'operations_db="$(operations_database_path)"' in source
    assert "require_code_only_shared_state" in source
    assert "code-only deployment requires the active release shared-data link" in source
    assert "code-only deployment requires the existing operations database" in source
    assert "code-only candidate must not contain a data path" in source
    assert 'rm -rf -- "$RELEASE/reports"' in source
    assert source.index("require_code_only_shared_state ||") < source.index(
        'if [ ! -f "$SHARED/data/finance_radar.sqlite3" ]'
    )
    assert "daily backup is already active; retry deployment after it finishes" in source
    assert 'if [ "$BACKUP_SERVICE_OWNED" -eq 1 ]; then' in source
    assert "rollback_preserved_unowned_backup_service=1" in source
    service_units = source.split("ROLLBACK_SERVICE_UNITS=(", 1)[1].split("\n)", 1)[0]
    assert "finance-radar-backup.service" not in service_units
    assert "finance-radar-backup.timer" in service_units
    assert "inhibit_scheduled_backup_start" in source
    assert "assert_backup_service_quiescent" in source
    assert "systemctl list-jobs --no-legend --plain" in source
    assert "scheduled backup start is inhibited during deployment stabilization" in backup_wrapper
    assert source.index("systemctl stop finance-radar-backup.timer") < source.index(
        "require_recent_verified_backup_record ||",
        source.index("SERVICES_TOUCHED=1"),
    )
    postcutover_marker = source.index("require_postcutover_verified_backup ||")
    assert postcutover_marker < source.index(
        "systemctl start finance-radar-backup.timer", postcutover_marker
    )
    assert 'OPERATIONS_DB="${FINANCE_RADAR_OPS_DB:-$BASE/shared/data/finance_radar_operations.sqlite3}"' in backup_wrapper
    assert 'python3 - "$OPERATIONS_DB"' in backup_wrapper


def test_prepared_restore_creates_root_backup_attestation_directory() -> None:
    source = (
        Path(__file__).parents[1]
        / "deployment"
        / "systemd"
        / "activate_prepared_restore.sh"
    ).read_text(encoding="utf-8")

    create_marker = "install -d -m 0700 -o root -g root /var/lib/finance-radar"
    start_marker = "systemctl enable --now finance-radar-api finance-radar-web finance-radar-worker finance-radar-backup.timer"
    assert create_marker in source
    assert source.index(create_marker) < source.index(start_marker)


def test_capture_interpretation_unit_does_not_treat_dev_null_as_an_env_file() -> None:
    source = CAPTURE_INTERPRETATION_UNIT.read_text(encoding="utf-8")

    assert (
        "scripts/run_capture_interpretation_worker.py --limit 20 --scan-limit 100000 --workers 3"
        in source
    )
    assert "--env-file /dev/null" not in source


def test_overview_is_published_by_a_bounded_external_process() -> None:
    unit = OVERVIEW_SNAPSHOT_UNIT.read_text(encoding="utf-8")
    timer = OVERVIEW_SNAPSHOT_TIMER.read_text(encoding="utf-8")
    api = (INSTALLER.parent / "finance-radar-api.service").read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    activator = ACTIVATOR.read_text(encoding="utf-8")

    assert "Type=oneshot" in unit
    assert "scripts/build_overview_snapshot.py" in unit
    assert "--wait-for-worker-idle-seconds 600" in unit
    assert "--worker-idle-poll-seconds 5" in unit
    assert "Slice=finance-radar.slice" in unit
    assert "MemoryMax=360M" in unit
    assert "TimeoutStartSec=15min" in unit
    assert "OnUnitInactiveSec=5min" in timer
    assert "FINANCE_RADAR_OVERVIEW_SNAPSHOT_PATH=" in api
    assert "ExecStartPre=/usr/bin/test -s" in api
    assert "systemctl start finance-radar-overview-snapshot.service" in installer
    assert "systemctl start finance-radar-overview-snapshot.service" in activator
    assert "api/v1/overview" in installer
    assert "api/v1/overview" in activator


def test_remote_installer_uses_explicit_current_public_url() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    candidate = (INSTALLER.parent / "nginx-radar-direct.conf").read_text(encoding="utf-8")
    assert "PUBLIC_WEB_URL=${6:-${FINANCE_RADAR_PUBLIC_WEB_URL:-}}" in source
    assert "public Web URL is required as argument 6 or FINANCE_RADAR_PUBLIC_WEB_URL" in source
    assert '"FINANCE_RADAR_WEB_URL=$PUBLIC_WEB_URL"' in source
    assert 's/__FINANCE_RADAR_DOMAIN__/$PUBLIC_EDGE_HOST/g' in source
    assert 's/__FINANCE_RADAR_PORT__/$PUBLIC_EDGE_PORT/g' in source
    assert "server_name __FINANCE_RADAR_DOMAIN__;" in candidate
    assert "listen __FINANCE_RADAR_PORT__ ssl;" in candidate
    assert "/etc/letsencrypt/live/__FINANCE_RADAR_DOMAIN__/" in candidate
    assert "18.208.34.152" not in source + candidate


def test_remote_installer_verifies_candidate_dependency_binding_before_mutation() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    dependency_gate = source.index('python3 "$RELEASE/scripts/verify_dependency_locks.py"')
    recovery_gate = source.index("# Mandatory recovery gates.")
    package_install = source.index('pip install --require-hashes -r "$RELEASE/requirements.lock"')

    assert dependency_gate < recovery_gate < package_install
    assert "candidate dependency lock verification failed" in source


def test_long_running_units_restart_worker_disable_formal_auto_verify_and_keep_one_bundle() -> None:
    api = Path(__file__).parents[1] / "deployment" / "systemd" / "finance-radar-api.service"
    worker = WORKER_UNIT.read_text(encoding="utf-8")
    worker_send_override = WORKER_SEND_OVERRIDE.read_text(encoding="utf-8")
    backup = BACKUP_UNIT.read_text(encoding="utf-8")
    backup_wrapper = BACKUP_QUIESCE_WRAPPER.read_text(encoding="utf-8")
    api_source = api.read_text(encoding="utf-8")
    assert "--interval 300" in worker
    assert "--no-light-verify" in worker
    assert "--send" in worker_send_override
    assert "--no-light-verify" in worker_send_override
    assert "Restart=on-failure" in worker
    assert "RestartSec=20" in worker
    assert "MemoryHigh=380M" in worker
    assert "MemoryMax=520M" in worker
    assert "MemorySwapMax=256M" in worker
    assert "TasksMax=128" in worker
    assert "UMask=0077" in worker
    assert "MemoryHigh=320M" in api_source
    assert "MemoryMax=430M" in api_source
    assert "MemorySwapMax=128M" in api_source
    assert "TasksMax=128" in api_source
    assert "MemoryHigh=340M" in backup
    assert "MemoryMax=460M" in backup
    assert "MemorySwapMax=128M" in backup
    assert "TasksMax=128" in backup
    assert "TimeoutStartSec=90min" in backup
    assert "TimeoutStopSec=2min" in backup
    assert "UMask=0077" in backup
    assert "User=root" in backup
    assert "Group=root" in backup
    assert "ExecStart=/usr/local/libexec/finance-radar/run_backup_quiesced.sh" in backup
    assert "--retention 1 --weekly-retention 0" in backup_wrapper
    assert "runuser -u finance-radar" in backup_wrapper
    assert "systemctl show \"$WORKER_UNIT\" --property=ActiveState --value" in backup_wrapper
    assert "worker-resume.inhibit" in backup_wrapper


def test_all_radar_workloads_share_an_aggregate_memory_budget_and_prioritize_recovery() -> None:
    slice_source = SLICE_UNIT.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    assert "MemoryHigh=600M" in slice_source
    assert "MemoryMax=700M" in slice_source
    assert "MemorySwapMax=384M" in slice_source
    assert "TasksMax=256" in slice_source
    for unit in (
        WORKER_UNIT,
        WEB_UNIT,
        ADMIN_UNIT,
        BACKUP_UNIT,
        LLM_UNIT,
        OVERVIEW_SNAPSHOT_UNIT,
    ):
        source = unit.read_text(encoding="utf-8")
        assert "Slice=finance-radar.slice" in source
        assert "MemoryAccounting=true" in source
        assert "OOMPolicy=stop" in source
    assert "OOMScoreAdjust=500" in WORKER_UNIT.read_text(encoding="utf-8")
    assert "OOMScoreAdjust=700" in BACKUP_UNIT.read_text(encoding="utf-8")
    llm = LLM_UNIT.read_text(encoding="utf-8")
    assert "OOMScoreAdjust=800" in llm
    assert "Conflicts=finance-radar-worker.service finance-radar-backup.service" in llm
    assert '[[ "$group" == /finance.slice/finance-radar.slice/* ]]' in installer
    assert "/system.slice/finance-radar.slice/" not in installer


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
    assert "User=finance-radar-web" in web
    assert "Group=finance-radar-web" in web
    assert "Environment=FINANCE_RADAR_UI_ROLE=public" in web
    assert "Environment=FINANCE_RADAR_SHOW_DEBUG=0" in web
    assert "UnsetEnvironment=FINANCE_RADAR_ADMIN_TOKEN" in web
    assert "ProtectProc=invisible" in web
    assert "ProcSubset=pid" in web
    assert "ReadWritePaths=" not in web
    assert "InaccessiblePaths=" in web
    for protected_path in (
        "/etc/finance-radar.env",
        "/opt/finance-radar/current/.env",
        "/opt/finance-radar/shared/data",
        "/opt/finance-radar/shared/reports",
    ):
        assert protected_path in web
    assert "install -m 0600 -o finance-radar-web -g finance-radar-web /dev/null /etc/finance-radar-public.env" in installer
    assert "ensure_public_web_principal" in installer
    assert "grant_public_web_runtime_access" in installer
    assert "assert_private_runtime_import_boundary" in installer
    assert "assert_public_runtime_import_boundary" in installer
    assert 'chmod 0711 "$BASE"' in installer
    assert 'chmod 0751 "$BASE/releases"' in installer
    assert 'chmod 0755 "$RELEASE"' in installer
    assert 'chmod 0644 "$RELEASE/VERSION" "$RELEASE/requirements.txt" "$RELEASE/requirements.lock"' in installer
    assert 'test -r "$RELEASE/VERSION"' in installer
    assert 'chmod 0751 "$BASE/releases" "$RELEASE"' not in installer
    assert 'chmod 0711 "$BASE" "$BASE/releases" "$RELEASE"' not in installer
    assert 'unset PYTHONPATH' in installer
    assert 'exec "$2" -B -c "import app; assert app.__file__"' in installer
    private_import_gate = installer.index("assert_private_runtime_import_boundary ||")
    public_import_gate = installer.index("assert_public_runtime_import_boundary ||")
    assert installer.index("grant_public_web_runtime_access ||") < private_import_gate
    assert private_import_gate < public_import_gate
    assert public_import_gate < installer.index("# The only point at which the running release changes.")
    assert "assert_public_web_identity_and_boundary" in installer
    assert 'streamlit_dir="$RELEASE/.streamlit"' in installer
    assert '"secrets.toml"' in installer
    assert 'refusing a release that contains Streamlit secrets' in installer
    assert 'find "$streamlit_dir" -mindepth 1 -maxdepth 1 ! -name config.toml -print -quit' in installer
    assert 'chmod 0711 "$streamlit_dir"' in installer
    assert 'chmod 0644 "$streamlit_dir/config.toml"' in installer
    assert 'test -r "$RELEASE/.streamlit/config.toml"' in installer
    for literal in (
        "FINANCE_RADAR_API_URL=http://127.0.0.1:18000",
        "FINANCE_RADAR_UI_ROLE=public",
        "FINANCE_RADAR_SHOW_DEBUG=0",
    ):
        assert literal in installer
    public_env_block = installer.split(
        "# The public Streamlit process receives a deliberately minimal environment.", 1
    )[1].split('install -m 0644 "$RELEASE/deployment/systemd/finance-radar-api.service"', 1)[0]
    assert "FINANCE_RADAR_ADMIN_TOKEN" not in public_env_block
    assert "cp " not in public_env_block
    assert "grep " not in public_env_block


def test_candidate_reports_are_replaced_by_a_verified_shared_link() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    reports_migration = source.split(
        'if [ ! -e "$SHARED/reports" ]; then', 1
    )[1].split(
        '[ -s "$SHARED/data/finance_radar.sqlite3" ]', 1
    )[0]

    assert '[ -d "$RELEASE/reports" ] && [ ! -L "$RELEASE/reports" ]' in reports_migration
    assert 'rm -rf -- "$RELEASE/reports"' in reports_migration
    assert 'ln -s -- "$SHARED/reports" "$RELEASE/reports"' in reports_migration
    assert '[ -L "$RELEASE/reports" ]' in reports_migration
    assert 'readlink -f -- "$RELEASE/reports"' in reports_migration
    assert reports_migration.index('rm -rf -- "$RELEASE/reports"') < reports_migration.index(
        'ln -s -- "$SHARED/reports" "$RELEASE/reports"'
    )


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
    enable_line = next(
        line for line in installer.splitlines() if line.startswith("systemctl enable finance-radar-api")
    )
    assert "finance-radar-admin" not in enable_line


def test_scoped_internal_uis_are_manual_loopback_only_and_mutually_exclusive() -> None:
    reviewer = REVIEWER_UNIT.read_text(encoding="utf-8")
    operator = OPERATOR_UNIT.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    assert "Environment=FINANCE_RADAR_UI_ROLE=reviewer" in reviewer
    assert "UnsetEnvironment=FINANCE_RADAR_ADMIN_TOKEN FINANCE_RADAR_OPERATOR_TOKEN" in reviewer
    assert "app/web/Reviewer.py" in reviewer
    assert "--server.port 18503" in reviewer
    assert "--server.baseUrlPath radar-review" in reviewer
    assert "Environment=FINANCE_RADAR_UI_ROLE=operator" in operator
    assert "UnsetEnvironment=FINANCE_RADAR_ADMIN_TOKEN FINANCE_RADAR_REVIEWER_TOKEN" in operator
    assert "app/web/Operator.py" in operator
    assert "--server.port 18504" in operator
    assert "--server.baseUrlPath radar-ops" in operator
    for source in (reviewer, operator):
        assert "\n[Install]\n" not in source
        assert "MemoryMax=256M" in source
        assert "Conflicts=" in source
    assert "FINANCE_RADAR_REVIEWER_TOKEN" in installer
    assert "FINANCE_RADAR_OPERATOR_TOKEN" in installer
    assert "finance-radar-reviewer.service" in installer
    assert "finance-radar-operator.service" in installer
    api = (INSTALLER.parent / "finance-radar-api.service").read_text(encoding="utf-8")
    assert "LoadCredential=reviewer-principals.json:/etc/finance-radar-reviewer-principals.json" in api
    assert "/etc/finance-radar-reviewer-principals.json" in installer
    assert "chmod 0600 /etc/finance-radar-reviewer-principals.json" in installer


def test_restore_recreates_public_environment_and_never_enables_admin() -> None:
    source = ACTIVATOR.read_text(encoding="utf-8")
    assert "install -m 0600 -o finance-radar-web -g finance-radar-web /dev/null /etc/finance-radar-public.env" in source
    assert "ensure_public_web_principal" in source
    assert "grant_public_web_runtime_access" in source
    assert "assert_private_runtime_import_boundary" in source
    assert "assert_public_runtime_import_boundary" in source
    assert 'chmod 0711 "$BASE"' in source
    assert 'chmod 0751 "$BASE/releases"' in source
    assert 'chmod 0755 "$RELEASE"' in source
    assert 'chmod 0644 "$RELEASE/VERSION" "$RELEASE/requirements.txt" "$RELEASE/requirements.lock"' in source
    assert 'test -r "$RELEASE/VERSION"' in source
    assert 'chmod 0751 "$BASE/releases" "$RELEASE"' not in source
    assert 'chmod 0711 "$BASE" "$BASE/releases" "$RELEASE"' not in source
    assert 'unset PYTHONPATH' in source
    assert 'exec "$2" -B -c "import app; assert app.__file__"' in source
    private_import_gate = source.index("assert_private_runtime_import_boundary ||")
    public_import_gate = source.index("assert_public_runtime_import_boundary ||")
    assert source.index("grant_public_web_runtime_access ||") < private_import_gate
    assert private_import_gate < public_import_gate
    assert public_import_gate < source.index("systemctl daemon-reload", public_import_gate)
    assert "assert_public_web_identity_and_boundary" in source
    assert 'streamlit_dir="$RELEASE/.streamlit"' in source
    assert 'refusing a prepared restore that contains Streamlit secrets' in source
    assert 'find "$streamlit_dir" -mindepth 1 -maxdepth 1 ! -name config.toml -print -quit' in source
    assert 'chmod 0711 "$streamlit_dir"' in source
    assert 'chmod 0644 "$streamlit_dir/config.toml"' in source
    assert 'test -r "$RELEASE/.streamlit/config.toml"' in source
    public_env_block = source.split("# Recreate rather than copy/filter", 1)[1].split(
        "python3 -m venv", 1
    )[0]
    assert "FINANCE_RADAR_API_URL=http://127.0.0.1:18000" in public_env_block
    assert "FINANCE_RADAR_UI_ROLE=public" in public_env_block
    assert "FINANCE_RADAR_SHOW_DEBUG=0" in public_env_block
    assert "FINANCE_RADAR_ADMIN_TOKEN" not in public_env_block
    assert "FINANCE_RADAR_REVIEWER_TOKEN" not in public_env_block
    assert "FINANCE_RADAR_OPERATOR_TOKEN" not in public_env_block
    assert "FINANCE_RADAR_REVIEWER_PRINCIPALS_JSON" not in public_env_block
    assert "reviewer-principals.json" in source
    assert "human-label submission fail-closed" in source
    enable_line = next(
        line for line in source.splitlines() if line.startswith("systemctl enable --now finance-radar-api")
    )
    assert "finance-radar-admin" not in enable_line


def test_restore_uses_current_systemd_units_and_never_auto_starts_the_local_llm() -> None:
    source = ACTIVATOR.read_text(encoding="utf-8")
    assert "install_versioned_unit()" in source
    assert "finance-radar.slice" in source
    assert '"$BASE/current/deployment/systemd/$unit"' in source
    assert "systemctl disable --now finance-radar-evidence-llm.service || true" in source
    assert "systemctl enable --now finance-radar-evidence-llm.service" not in source
    assert "local_evidence_model=disabled_after_restore" in source
    assert "run_backup_quiesced.sh" in source
    assert "/usr/local/libexec/finance-radar/run_backup_quiesced.sh" in source


def test_restore_accepts_historic_archives_without_a_slice_or_optional_llm_unit() -> None:
    source = ACTIVATOR.read_text(encoding="utf-8")

    assert 'elif [ "$unit" = "finance-radar.slice" ]; then' in source
    assert "write_legacy_slice_fallback()" in source
    assert "MemoryHigh=600M" in source
    assert "MemoryMax=700M" in source
    assert "MemorySwapMax=384M" in source
    assert "TasksMax=256" in source
    assert "optional evidence LLM unit is absent from this prepared archive" in source


def test_restore_rollback_removes_all_new_units_and_configuration_after_a_failed_gate() -> None:
    source = ACTIVATOR.read_text(encoding="utf-8")

    for literal in (
        "MANAGED_UNIT_PATHS=(",
        "finance-radar-backup.timer",
        "finance-radar-evidence-llm.service",
        "/etc/finance-radar.env",
        "/etc/finance-radar-public.env",
        'systemctl stop "${MANAGED_RUNTIME_UNITS[@]}"',
        'systemctl disable "${MANAGED_ENABLEMENT_UNITS[@]}"',
        'rm -f -- "${MANAGED_UNIT_PATHS[@]}" "${MANAGED_CONFIG_PATHS[@]}"',
        "systemctl daemon-reload || true",
        "BASE_MOVED=1",
    ):
        assert literal in source


def test_migration_backup_declares_optional_local_model_separately_from_its_unit() -> None:
    source = MIGRATION_BACKUP.read_text(encoding="utf-8")

    assert "LOCAL_EVIDENCE_MODEL_CAPABILITY.json" in source
    assert '"kind": "local_evidence_model"' in source
    assert '"restore_policy": "DISABLED_AFTER_RESTORE"' in source
    assert "MODEL_INSTALLED=false" in source
    assert "MODEL_ARCHIVED=false" in source


def test_manual_local_llm_install_never_preserves_an_old_boot_enablement_or_competes_with_backup() -> None:
    source = LOCAL_LLM_INSTALLER.read_text(encoding="utf-8")
    assert "service_disabled=true" in source
    assert "systemctl disable --now finance-radar-evidence-llm.service" in source
    assert "refusing evidence LLM activation while worker or backup is active" in source
    assert source.index("refusing evidence LLM activation") < source.index(
        "systemctl enable --now finance-radar-evidence-llm.service"
    )


def test_in_place_installer_rolls_back_services_and_edge_on_any_cutover_failure() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    assert 'PREVIOUS_RELEASE="$(readlink -f -- "$BASE/current")"' in source
    assert 'ROLLBACK_DIR="/var/tmp/finance-radar-install-${RELEASE_ID}-' in source
    for path in (
        "/etc/finance-radar-public.env",
        "/etc/systemd/system/finance-radar-worker.service",
        "/etc/nginx/conf.d/finance-radar-direct.conf",
        "/etc/nginx/conf.d/finance-radar-aws.conf",
        "/etc/letsencrypt/renewal-hooks/deploy/finance-radar-reload-nginx.sh",
        "/etc/systemd/system.control/finance-radar-worker.service.d/50-MemoryMax.conf",
    ):
        assert path in source
    assert "rollback()" in source
    assert 'ln -sfn "$PREVIOUS_RELEASE" "$BASE/current"' in source
    cutover = source.split("# The only point at which the running release changes.", 1)[1]
    assert source.index("systemctl stop finance-radar-worker ||") < source.index(
        "# The only point at which the running release changes."
    )
    assert "worker unexpectedly restarted during protected cutover" in cutover
    assert "local attempts=${2:-90}" in cutover
    assert "local attempts=${2:-30}" not in cutover
    restore_source = ACTIVATOR.read_text(encoding="utf-8")
    assert "for _ in $(seq 1 90)" in restore_source
    assert "for _ in $(seq 1 30)" not in restore_source
    assert "ROLLBACK_ENABLED_UNITS" in source
    assert "ROLLBACK_ACTIVE_UNITS" in source
    assert "restore_service_runtime()" in source
    assert "systemctl is-enabled --quiet" in source
    assert "systemctl is-active --quiet" in source
    assert "restore_service_runtime" in source
    assert "remove_legacy_managed_property_dropins" in source
    cutover_reload = source.index(
        "systemctl daemon-reload", source.index("remove_legacy_managed_property_dropins ||")
    )
    assert source.index("remove_legacy_managed_property_dropins ||") < cutover_reload
    assert "systemctl is-active --quiet finance-radar-api finance-radar-web finance-radar-worker ||" in source
    assert "systemctl is-active --quiet finance-radar-backup.timer ||" in source
    assert 'bash "$DIRECT_ENDPOINT_INSTALLER" "$DIRECT_ENDPOINT_CANDIDATE" "$DIRECT_ENDPOINT_HOOK"' in source
    assert "retire_known_predecessor_vhost" in source
    edge_touch = source.index("EDGE_TOUCHED=1", source.index("# Treat the public edge"))
    assert edge_touch < source.index("retire_known_predecessor_vhost ||", edge_touch)
    assert "assert_public_release_marker" in source
    marker_check = source.split("assert_public_release_marker() {", 1)[1].split(
        "remove_legacy_managed_property_dropins() {", 1
    )[0]
    assert "for attempt in $(seq 1 15)" in marker_check
    assert "except json.JSONDecodeError:" in marker_check
    assert "public_release_marker=PASS" in marker_check
    assert "public release marker did not converge" in marker_check
    assert "--noproxy '*'" in marker_check
    assert "/radar/release.json" in (Path(__file__).parents[1] / "deployment" / "systemd" / "nginx-radar-direct.conf").read_text(encoding="utf-8")
    assert "nginx -t" in source
    assert "--resolve \"$PUBLIC_EDGE_HOST:$PUBLIC_EDGE_PORT:127.0.0.1\"" in source
    for denied_path in (
        "/finance-radar-api/",
        "/radar-admin/",
        "/radar-review/",
        "/radar-ops/",
        "/radar/Event_Intelligence",
        "Operations_and_Model",
    ):
        assert denied_path in source


def test_in_place_installer_requires_a_fresh_verified_backup_before_cutover() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    cutover_marker = '# The only point at which the running release changes.'
    backup_marker = 'require_predeploy_verified_backup ||'

    assert 'systemctl is-active --quiet finance-radar-admin' in source
    assert 'systemctl show finance-radar-admin --property=UnitFileState --value' in source
    assert "finance-radar-reviewer finance-radar-operator" in source
    assert 'disabled|static|masked|masked-runtime|not-found|""' in source
    assert 'enabled|enabled-runtime|linked|linked-runtime|alias|indirect|generated' in source
    assert 'systemctl is-active --quiet finance-radar-backup.service' in source
    assert 'systemctl start finance-radar-backup.service' in source
    assert 'systemctl show finance-radar-backup.service --property=Result --value' in source
    assert RECEIPT_VALIDATOR.is_file()
    assert 'BACKUP_RECEIPT_VERIFIER="$RELEASE/deployment/systemd/verify_backup_receipt.py"' in source
    assert 'write_backup_inventory' in source
    assert 'capture_fresh_verified_backup_receipt' in source
    assert 'systemd-run --quiet --wait --collect --pipe' in source
    assert '--slice=finance-radar.slice' in source
    # The receipt drill restores the same SQLite snapshot that the transient
    # candidate bridge just created. Keep its cgroup envelope aligned with the
    # normal bounded backup job so the deployment guard cannot be throttled
    # below the working set it must independently verify.
    receipt_runner = source[
        source.index('capture_fresh_verified_backup_receipt()') : source.index(
            'assert_bounded_backup_unit()'
        )
    ]
    assert '--property=MemoryHigh=340M' in receipt_runner
    assert '--property=MemoryMax=460M' in receipt_runner
    assert '--property=MemorySwapMax=128M' in receipt_runner
    candidate_bridge = source[
        source.index('run_predeploy_candidate_backup()') : source.index(
            'require_predeploy_memory_headroom()'
        )
    ]
    assert '--property=TimeoutStartSec=90min' in candidate_bridge
    assert '--setenv=TMPDIR="$receipt_tmpdir"' in receipt_runner
    assert 'MemoryHigh=160M' not in receipt_runner
    assert 'MemoryMax=220M' not in receipt_runner
    assert 'MemorySwapMax=96M' not in receipt_runner
    assert 'require_predeploy_memory_headroom' in source
    assert 'FINANCE_RADAR_BACKUP_RECEIPT_TMPDIR:-/var/tmp/finance-radar-receipt' in source
    assert 'run_predeploy_candidate_backup()' in source
    assert 'run_and_capture_fresh_backup recovery_bundle candidate_bridge' in source
    assert 'predeploy backup did not produce a complete recovery bundle' in source
    assert 'install_bounded_bridge_backup_runtime' not in source
    assert 'assert_bounded_backup_unit' in source
    bridge = source[source.index('run_predeploy_candidate_backup()') : source.index('require_predeploy_memory_headroom()')]
    for literal in (
        '--unit="$transient_unit"',
        '--slice=finance-radar.slice',
        '--property="WorkingDirectory=$RELEASE"',
        '--property=EnvironmentFile=/etc/finance-radar.env',
        '--property=MemoryHigh=340M',
        '--property=MemoryMax=460M',
        '--property=MemorySwapMax=128M',
        '--property=NoNewPrivileges=true',
        '--property=ProtectSystem=strict',
        '--property="ReadWritePaths=$SHARED/data"',
        '--setenv="TMPDIR=$BACKUP_RESTORE_TMPDIR"',
        '--setenv="FINANCE_RADAR_BACKUP_SOURCE_ROOT=$RELEASE"',
        '--setenv=FINANCE_RADAR_PREDEPLOY_BRIDGE=1',
        'bash "$BACKUP_QUIESCE_WRAPPER_SOURCE"',
        'runtime_log="$RELEASE_RECORDS/PREDEPLOY_BACKUP_RUNTIME.log"',
        '>"$runtime_log" 2>&1',
        'chmod 0640 "$runtime_log"',
    ):
        assert literal in bridge
    assert 'bash "$BACKUP_QUIESCE_WRAPPER_SOURCE" >&2' not in bridge
    assert 'legacy_sqlite' not in source
    assert 'PREDEPLOY_BACKUP_RECEIPT_SHA256' in source
    assert 'require_postcutover_verified_backup' in source
    assert 'POSTDEPLOY_FULL_BUNDLE_STATUS=VERIFIED' in source
    assert 'POSTDEPLOY_FULL_BUNDLE_STATUS=REUSED_VERIFIED_DAILY' in source
    assert 'predeploy_backup_snapshot_id=%s' in source
    assert 'BACKUP_QUIESCE_WRAPPER_SOURCE="$RELEASE/deployment/systemd/run_backup_quiesced.sh"' in source
    assert "install_backup_quiesce_wrapper" in source
    assert 'BACKUP_QUIESCE_WRAPPER_TARGET=/usr/local/libexec/finance-radar/run_backup_quiesced.sh' in source
    assert "inhibit_worker_resume()" in source
    assert "clear_worker_resume_inhibit()" in source

    assert source.index(backup_marker) < source.index(cutover_marker)
    assert source.index('systemctl stop finance-radar-worker ||') < source.index(backup_marker)
    assert source.index('run_and_capture_fresh_backup recovery_bundle candidate_bridge') < source.index(cutover_marker)
    assert source.index('install_backup_quiesce_wrapper ||') > source.index(backup_marker)
    assert source.index('install_backup_quiesce_wrapper ||') < source.index(cutover_marker)
    assert source.index('require_postcutover_verified_backup ||') > source.index(cutover_marker)
    assert source.index('inhibit_worker_resume ||') < source.index('systemctl stop finance-radar-worker ||')
    assert source.index('clear_worker_resume_inhibit ||', source.index('systemctl start finance-radar-worker')) > source.index(
        'systemctl start finance-radar-worker'
    )


def test_backup_restore_drills_use_validated_root_volume_scratch() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    backup_unit = BACKUP_UNIT.read_text(encoding="utf-8")
    activator = ACTIVATOR.read_text(encoding="utf-8")
    scratch = "/opt/finance-radar/shared/data/.backup-restore-tmp"

    assert 'BACKUP_RESTORE_TMPDIR="$SHARED/data/.backup-restore-tmp"' in installer
    assert "prepare_backup_restore_tmpdir()" in installer
    bridge = installer[
        installer.index("run_predeploy_candidate_backup()") : installer.index(
            "require_predeploy_memory_headroom()"
        )
    ]
    assert bridge.index("prepare_backup_restore_tmpdir || return 1") < bridge.index(
        "systemd-run --quiet --wait --collect --pipe"
    )
    assert '--setenv="TMPDIR=$BACKUP_RESTORE_TMPDIR"' in bridge

    assert f"Environment=TMPDIR={scratch}" in backup_unit
    assert f"ExecStartPre=/usr/bin/test ! -L {scratch}" in backup_unit
    assert (
        "ExecStartPre=/usr/bin/install -d -m 0700 -o finance-radar "
        f"-g finance-radar {scratch}"
    ) in backup_unit
    assert backup_unit.index(f"Environment=TMPDIR={scratch}") < backup_unit.index("ExecStart=")

    assert 'BACKUP_RESTORE_TMPDIR="$BASE/shared/data/.backup-restore-tmp"' in activator
    assert "prepare_backup_restore_tmpdir()" in activator
    scratch_prepare = activator.index("prepare_backup_restore_tmpdir || {")
    assert scratch_prepare < activator.index("systemctl daemon-reload", scratch_prepare)


def test_installer_recognizes_only_audited_static_or_direct_predecessor_vhosts_before_retirement() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    # The actual July AWS vhost used an alias to index.html, not a root
    # directive.  The signer must recognize this bounded historic shape before
    # moving the config out of nginx's include tree.
    assert 'location[[:space:]]*=[[:space:]]*/radar/index[.]html' in source
    assert 'alias[[:space:]]+$PUBLIC_STATUS_DIR/index[.]html;' in source
    assert 'rewrite[[:space:]]+' in source
    assert '/radar/index[.]html[[:space:]]+last;' in source
    assert 'location[[:space:]]+/finance-radar-api/' in source
    assert "proxy_pass http://127.0.0.1:18000/" in source
    assert "RETIRABLE_VHOST_KIND=static" in source

    # Production has since moved to the guarded Streamlit predecessor while
    # retaining the historical file name. It must be recognized only when its
    # redirect, internal-page and loopback API guards are all present.
    assert "RETIRABLE_VHOST_KIND=direct-streamlit" in source
    assert "proxy_pass http://127.0.0.1:18501;" in source
    assert r"Event_Intelligence\\|Operations_and_Model\\|Adjudication_Studio" in source
    assert r"location[[:space:]]+\\^~[[:space:]]+/finance-radar-api/" in source
    assert r"location[[:space:]]+\\^~[[:space:]]+/radar-admin/" in source
    assert "assert_candidate_vhost_owns_public_edge" in source
    assert "expected exactly one active Nginx vhost" in source
    assert "refusing to retire an unrecognized Finance Radar Nginx vhost" in source


def test_installer_atomically_transfers_a_root_only_predeploy_hold_until_postcutover_backup() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    hold_transfer = (
        Path(__file__).parents[1]
        / "deployment"
        / "systemd"
        / "transfer_verified_backup_hold.py"
    ).read_text(encoding="utf-8")
    cutover_marker = '# The only point at which the running release changes.'
    assert "create_predeploy_backup_hold()" in source
    assert 'FINANCE_RADAR_DEPLOY_HOLD_MODE:-atomic_custody' in source
    assert 'BACKUP_HOLD_TRANSFER="$RELEASE/deployment/systemd/transfer_verified_backup_hold.py"' in source
    assert 'mode=atomic_custody' in source
    assert "preserve_failed_predeploy_backup_hold" in source
    assert "rollback_recovery_hold=PRESERVED" in source
    assert "failed-precutover" in source
    assert "two retained failed recovery holds require explicit operator review" in source
    assert "clear_predeploy_backup_hold" in source
    assert "os.rename(source, destination)" in hold_transfer
    assert "verify_bundle(verifier, source, receipt_sha256)" in hold_transfer
    assert "verify_bundle(verifier, destination, receipt_sha256)" in hold_transfer
    assert "validated_superseded_bundles" in hold_transfer
    assert "shutil.rmtree(candidate)" in hold_transfer
    assert "projected_postcutover_bundle" in hold_transfer
    assert "projected_sqlite_receipt_scratch" in hold_transfer
    assert "atomic predeploy custody storage headroom insufficient" in hold_transfer
    assert "os.rename(destination, source)" in hold_transfer
    assert "root-owned atomic custody transfer" in hold_transfer
    assert source.index("create_predeploy_backup_hold ||") < source.index(cutover_marker)
    post_gate = source.index("require_postcutover_verified_backup ||")
    cleanup = source.index("if ! clear_predeploy_backup_hold; then", post_gate)
    commit = source.index('mv -f -- "$ACTIVATION_PENDING"', post_gate)
    assert post_gate < commit < cleanup
    assert source.index("trap - ERR", commit) < cleanup
    assert cleanup > source.index("systemctl start finance-radar-worker", post_gate)
    assert "$SHARED/recovery_holds" not in source
    assert "RECOVERY_HOLD_PARENT=/var/lib/finance-radar" in source
    assert "RECOVERY_HOLD_ROOT=\"$RECOVERY_HOLD_PARENT/recovery-holds\"" in source


def test_candidate_code_is_root_owned_and_runtime_read_only_before_root_bridge_execution() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    assert 'install -d -m 0751 -o root -g root "$BASE"' in source
    assert 'install -d -m 0751 -o root -g finance-radar "$BASE/releases"' in source
    assert 'install -d -m 0750 -o root -g finance-radar "$RELEASE"' in source
    assert 'chown -R root:finance-radar "$RELEASE"' in source
    assert 'chown -R finance-radar:finance-radar "$RELEASE"' not in source
    assert 'find "$RELEASE/app" "$RELEASE/deployment" -xdev -perm /022 -print -quit' in source
    assert source.index('chown -R root:finance-radar "$RELEASE"') < source.index('run_predeploy_candidate_backup()')


def test_installer_stops_and_disables_the_advisory_llm_before_starting_the_worker() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    cutover = source.split("# The only point at which the running release changes.", 1)[1]
    assert 'install -m 0644 "$RELEASE/deployment/systemd/finance-radar.slice" /etc/systemd/system/' in source
    assert "finance-radar-evidence-llm.service" in source
    assert "systemctl disable finance-radar-evidence-llm.service" in cutover
    assert cutover.index("systemctl disable finance-radar-evidence-llm.service") < cutover.index(
        "systemctl start finance-radar-worker"
    )
    assert "advisory evidence LLM must remain stopped and disabled after deployment" in source
