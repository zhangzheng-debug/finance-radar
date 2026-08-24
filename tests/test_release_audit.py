from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

import scripts.release_audit as release_audit

from scripts.release_audit import (
    DEFAULT_CRITICAL_FILES,
    build_release_manifest,
    inspect_artifact,
    main,
    parse_verification,
    verify_release_manifest,
    write_acceptance_bundle,
    write_release_bundle,
)


FIXED_TIME = datetime(2026, 8, 4, 1, 2, 3, tzinfo=timezone.utc)
CLEAN_GIT = {"available": True, "commit": "a" * 40, "dirty": False}
DIRTY_GIT = {"available": True, "commit": "b" * 40, "dirty": True}


def _write(path: Path, content: str = "release contract\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _artifact(
    path: Path,
    *,
    member_name: str = "finance-radar/app.txt",
    members: dict[str, bytes] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payloads = members or {member_name: b"immutable release payload\n"}
    with tarfile.open(path, "w:gz") as archive:
        for name, data in payloads.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return path


def _manifest(root: Path, artifact: Path) -> dict[str, object]:
    return build_release_manifest(
        root,
        release_id="20260804T010203Z-test",
        critical_files=("deployment/test.conf",),
        artifact_paths=(artifact,),
        verifications=(parse_verification("pytest=PASS"), parse_verification("nginx=PASS")),
        generated_at=FIXED_TIME,
        git_state=CLEAN_GIT,
    )


def test_release_bundle_is_hash_bound_and_verifies_cross_platform(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write(root / "deployment/test.conf")
    artifact = _artifact(
        tmp_path / "finance-radar-release.tgz",
        members={"deployment/test.conf": (root / "deployment/test.conf").read_bytes()},
    )
    manifest = _manifest(root, artifact)

    assert manifest["release"]["readiness"] == "READY"
    assert manifest["source"]["git"] == {
        "available": True,
        "commit": "a" * 40,
        "dirty": False,
        "observed_before_output": True,
    }
    assert manifest["security_boundaries"]["deployment_performed"] is False
    assert "dirty_source_archive_inventory" not in manifest
    assert str(root.resolve()) not in json.dumps(manifest)

    output = tmp_path / "records"
    names = write_release_bundle(manifest, output)
    for name in names.values():
        assert (output / name).is_file()

    sidecar = (output / names["checksums"]).read_text(encoding="ascii")
    manifest_bytes = (output / names["json"]).read_bytes()
    assert f"{hashlib.sha256(manifest_bytes).hexdigest()}  {names['json']}" in sidecar

    report = verify_release_manifest(
        output / names["json"],
        root,
        artifact_paths=(artifact,),
        expected_release_id="20260804T010203Z-test",
        require_ready=True,
        require_sidecar=True,
        verified_at=FIXED_TIME,
    )
    assert report["status"] == "PASS"
    assert all(check["status"] == "PASS" for check in report["checks"])

    acceptance = write_acceptance_bundle(report, output / "acceptance")
    assert set(acceptance) == {"json", "markdown", "checksums"}
    assert all((output / "acceptance" / name).is_file() for name in acceptance.values())


def test_utf8_lf_binding_accepts_a_crlf_checkout_and_lf_git_archive(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    target = root / "deployment" / "test.conf"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"release contract\r\n")
    artifact = _artifact(
        tmp_path / "git-archive.tgz",
        members={"deployment/test.conf": b"release contract\n"},
    )
    manifest = build_release_manifest(
        root,
        release_id="crlf-git-archive",
        critical_files=("deployment/test.conf",),
        artifact_paths=(artifact,),
        verifications=(parse_verification("pytest=PASS"),),
        git_state=CLEAN_GIT,
        generated_at=FIXED_TIME,
    )

    assert manifest["critical_files"][0]["hash_basis"] == "utf8_lf_normalized"
    assert manifest["critical_files"][0]["bytes"] == len(b"release contract\n")
    records = tmp_path / "records"
    names = write_release_bundle(manifest, records)
    report = verify_release_manifest(
        records / names["json"],
        root,
        artifact_paths=(artifact,),
        require_ready=True,
        require_sidecar=True,
        require_artifact=True,
        verified_at=FIXED_TIME,
    )
    assert report["status"] == "PASS"


def test_verifier_detects_tampered_release_file(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    target = root / "deployment/test.conf"
    _write(target)
    artifact = _artifact(
        tmp_path / "release.tgz",
        members={"deployment/test.conf": target.read_bytes()},
    )
    manifest = _manifest(root, artifact)
    output = tmp_path / "records"
    names = write_release_bundle(manifest, output)

    target.write_text("tampered\n", encoding="utf-8")
    report = verify_release_manifest(
        output / names["json"],
        root,
        artifact_paths=(artifact,),
        require_sidecar=True,
    )
    assert report["status"] == "FAIL"
    failed = [check["name"] for check in report["checks"] if check["status"] == "FAIL"]
    assert failed == ["file:deployment/test.conf"]


def test_dirty_release_requires_explicit_exception_and_artifact_hash(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write(root / "deployment/test.conf")
    verifications = (parse_verification("pytest=PASS"),)

    review = build_release_manifest(
        root,
        release_id="dirty-review",
        critical_files=("deployment/test.conf",),
        verifications=verifications,
        git_state=DIRTY_GIT,
        generated_at=FIXED_TIME,
    )
    assert review["release"]["readiness"] == "REVIEW_REQUIRED_DIRTY_WORKTREE"

    with pytest.raises(ValueError, match="requires a release archive bound"):
        build_release_manifest(
            root,
            release_id="dirty-no-artifact",
            critical_files=("deployment/test.conf",),
            verifications=verifications,
            git_state=DIRTY_GIT,
            allow_dirty=True,
            generated_at=FIXED_TIME,
        )

    artifact = _artifact(
        tmp_path / "release.tgz",
        members={"deployment/test.conf": (root / "deployment/test.conf").read_bytes()},
    )
    accepted = build_release_manifest(
        root,
        release_id="dirty-declared",
        critical_files=("deployment/test.conf",),
        artifact_paths=(artifact,),
        verifications=verifications,
        git_state=DIRTY_GIT,
        allow_dirty=True,
        generated_at=FIXED_TIME,
    )
    assert accepted["release"]["readiness"] == "READY_WITH_DECLARED_DIRTY_SOURCE"
    assert accepted["release"]["dirty_source_exception_declared"] is True
    assert accepted["dirty_source_archive_inventory"]["format"] == (
        "finance-radar-dirty-source-archive-inventory-v1"
    )


def test_dirty_release_requires_and_verifies_complete_noncritical_runtime_inventory(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    api_module = root / "app" / "api.py"
    replay_module = root / "app" / "services" / "replay.py"
    _write(api_module, "from app.services.replay import ReplayService\n")
    _write(replay_module, "class ReplayService: ...\n")
    verifications = (parse_verification("pytest=PASS"),)

    omitted = _artifact(
        tmp_path / "omitted-runtime-module.tgz",
        members={"app/api.py": api_module.read_bytes()},
    )
    with pytest.raises(ValueError, match="exactly the workspace release file inventory"):
        build_release_manifest(
            root,
            release_id="dirty-omitted-runtime",
            critical_files=("app/api.py",),
            artifact_paths=(omitted,),
            verifications=verifications,
            git_state=DIRTY_GIT,
            allow_dirty=True,
            generated_at=FIXED_TIME,
        )

    artifact = _artifact(
        tmp_path / "complete-runtime-module.tgz",
        members={
            "app/api.py": api_module.read_bytes(),
            "app/services/replay.py": replay_module.read_bytes(),
        },
    )
    manifest = build_release_manifest(
        root,
        release_id="dirty-complete-runtime",
        critical_files=("app/api.py",),
        artifact_paths=(artifact,),
        verifications=verifications,
        git_state=DIRTY_GIT,
        allow_dirty=True,
        generated_at=FIXED_TIME,
    )
    records = tmp_path / "records"
    names = write_release_bundle(manifest, records)

    extracted = tmp_path / "extracted-release"
    with tarfile.open(artifact, "r:gz") as archive:
        for member in archive.getmembers():
            assert member.isfile()
            target = extracted / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            assert source is not None
            target.write_bytes(source.read())
    clean_report = verify_release_manifest(
        records / names["json"],
        extracted,
        artifact_paths=(artifact,),
        require_ready=True,
        require_sidecar=True,
        require_artifact=True,
        verified_at=FIXED_TIME,
    )
    assert clean_report["status"] == "PASS"

    (extracted / "app" / "services" / "replay.py").write_text(
        "class ReplayService: changed = True\n", encoding="utf-8"
    )
    report = verify_release_manifest(
        records / names["json"],
        extracted,
        artifact_paths=(artifact,),
        require_ready=True,
        require_sidecar=True,
        require_artifact=True,
        verified_at=FIXED_TIME,
    )
    assert report["status"] == "FAIL"
    assert {
        check["name"] for check in report["checks"] if check["status"] == "FAIL"
    } == {"dirty_source_workspace_inventory"}


def test_sensitive_files_and_unsafe_archives_are_rejected(tmp_path: Path) -> None:
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=do-not-read\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sensitive artifact path rejected"):
        inspect_artifact(secret)

    unsafe = _artifact(tmp_path / "unsafe.tgz", member_name="../.env")
    with pytest.raises(ValueError, match="unsafe archive member path"):
        inspect_artifact(unsafe)

    streamlit_secret = _artifact(
        tmp_path / "streamlit-secret.tgz",
        member_name=".streamlit/secrets.toml",
    )
    with pytest.raises(ValueError, match="sensitive archive member path rejected"):
        inspect_artifact(streamlit_secret)

    mismatch_root = tmp_path / "mismatch-workspace"
    _write(mismatch_root / "deployment/test.conf", "expected\n")
    mismatch = _artifact(
        tmp_path / "mismatch.tgz",
        members={"deployment/test.conf": b"different\n"},
    )
    with pytest.raises(ValueError, match="does not match workspace"):
        build_release_manifest(
            mismatch_root,
            release_id="mismatched-archive",
            critical_files=("deployment/test.conf",),
            artifact_paths=(mismatch,),
            git_state=CLEAN_GIT,
            generated_at=FIXED_TIME,
        )

    root = tmp_path / "workspace"
    _write(
        root / "deployment/test.conf",
        "FINANCE_RADAR_ADMIN_TOKEN=" + "f" * 64 + "\n",
    )
    with pytest.raises(ValueError, match="high-confidence secret"):
        build_release_manifest(
            root,
            release_id="secret-source",
            critical_files=("deployment/test.conf",),
            git_state=CLEAN_GIT,
            generated_at=FIXED_TIME,
        )


def test_rollback_plan_preserves_failed_release_and_keeps_admin_stopped(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write(root / "deployment/test.conf")
    artifact = _artifact(
        tmp_path / "release.tgz",
        members={"deployment/test.conf": (root / "deployment/test.conf").read_bytes()},
    )
    manifest = _manifest(root, artifact)
    rollback_text = json.dumps(manifest["rollback"], ensure_ascii=False)

    assert "PREVIOUS_RELEASE" in rollback_text
    assert "do not delete the failed release" in rollback_text
    assert "leave finance-radar-admin stopped" in rollback_text
    assert "rm -rf" not in rollback_text
    assert manifest["security_boundaries"]["environment_values_read"] is False
    assert manifest["security_boundaries"]["git_commit_performed"] is False


def test_default_release_contract_files_exist() -> None:
    root = Path(__file__).parents[1]
    missing = [relative for relative in DEFAULT_CRITICAL_FILES if not (root / relative).is_file()]
    assert missing == []


def test_release_contract_binds_dependency_lock_verification() -> None:
    required = {
        "requirements.txt",
        "requirements-dev.txt",
        "requirements.lock",
        "requirements-dev.lock",
        "dependency-lock.json",
        "scripts/verify_dependency_locks.py",
    }
    assert required.issubset(DEFAULT_CRITICAL_FILES)


def test_release_contract_binds_the_exact_qualified_shadow_router() -> None:
    required = {
        "artifacts/risk_router.joblib",
        "artifacts/risk_router.sha256",
        "artifacts/risk_router_model_card.json",
        "artifacts/risk_router_external_blind_v3_report.json",
    }
    assert required.issubset(DEFAULT_CRITICAL_FILES)


def test_default_release_contract_covers_runtime_mutation_and_edge_boundaries() -> None:
    required = {
        "app/config.py",
        "app/models/risk_router.py",
        "app/models/evidence_policy.py",
        "app/services/evidence_agent.py",
        "app/services/event_admission.py",
        "app/services/historical_primary_readmission.py",
        "app/services/light_verification.py",
        "app/storage/operations.py",
        "scripts/light_verify.py",
        "deployment/systemd/finance-radar-worker-send.conf",
        "deployment/systemd/create_migration_backup.sh",
        "deployment/systemd/install_direct_endpoint.sh",
        "deployment/systemd/install_local_evidence_model.sh",
        "deployment/systemd/certbot-reload-nginx.sh",
        "deployment/systemd/finance-radar-evidence-llm.service",
        "deployment/systemd/finance-radar.slice",
        "deployment/systemd/run_backup_quiesced.sh",
        "deployment/systemd/verify_backup_receipt.py",
        "deployment/systemd/verify_code_only_release.py",
        "deployment/windows/Open-FinanceRadar-Backend.ps1",
        "scripts/apply_historical_primary_readmission.py",
        "scripts/apply_authorized_rough_reviews.py",
        "scripts/build_historical_primary_readmission_plan.py",
        "scripts/official_primary_page_enricher.py",
        "scripts/run_live_cycle.py",
        "scripts/audit_migration_restore.py",
        "scripts/prepare_migration_restore.py",
        "scripts/restore_migration_to_vps.ps1",
        "scripts/open_internal_ui.py",
    }
    assert required.issubset(DEFAULT_CRITICAL_FILES)


def test_systemd_installer_verifies_optional_manifest_before_cutover() -> None:
    root = Path(__file__).parents[1]
    installer = (root / "deployment/systemd/install_remote.sh").read_text(encoding="utf-8")
    gate = installer.split("# Optional, backward-compatible release gate.", 1)[1].split(
        "# Mandatory recovery gates.", 1
    )[0]

    assert "RELEASE_MANIFEST=${5:-}" in installer
    sidecar_preflight = installer.split('if [ -n "$RELEASE_MANIFEST" ]; then', 1)[1].split("\nfi\n", 1)[0]
    assert 'EXPECTED_MANIFEST_NAME="$RELEASE_ID.release-manifest.json"' in sidecar_preflight
    assert '[ -f "$RELEASE_MANIFEST" ] && [ ! -L "$RELEASE_MANIFEST" ]' in sidecar_preflight
    assert 'MANIFEST_SIDECAR="$(dirname -- "$RELEASE_MANIFEST")/$RELEASE_ID.release-records.SHA256"' in sidecar_preflight
    assert '[ -f "$MANIFEST_SIDECAR" ] && [ ! -L "$MANIFEST_SIDECAR" ]' in sidecar_preflight
    assert installer.index('MANIFEST_SIDECAR="$(dirname -- "$RELEASE_MANIFEST")') < installer.index(
        'tar -xzf "$ARCHIVE"'
    )
    assert 'python3 "$RELEASE/scripts/release_audit.py" verify' in gate
    for flag in (
        "--expected-release-id",
        "--require-ready",
        "--require-sidecar",
        "--require-artifact",
        "--artifact",
    ):
        assert flag in gate
    assert installer.index("release_manifest=verified") < installer.index('ln -sfn "$RELEASE" "$BASE/current"')
    assert installer.index("release_manifest=verified") < installer.index('mv "$RELEASE/data" "$SHARED/data"')
    assert installer.index('python3 - "$ARCHIVE"') < installer.index('tar -xzf "$ARCHIVE"')
    assert "systemctl" not in gate
    assert "/etc/finance-radar.env" not in gate


def test_installer_archive_preflight_runs_before_extract_and_rejects_links(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    installer = (root / "deployment/systemd/install_remote.sh").read_text(encoding="utf-8")
    preflight = installer.split('python3 - "$ARCHIVE" <<\'PY\'\n', 1)[1].split("\nPY\n", 1)[0]
    compile(preflight, "install_remote_archive_preflight", "exec")

    safe = tmp_path / "safe.tgz"
    with tarfile.open(safe, "w:gz") as archive:
        root_member = tarfile.TarInfo("./")
        root_member.type = tarfile.DIRTYPE
        archive.addfile(root_member)
        data = b"safe\n"
        member = tarfile.TarInfo("./app.txt")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    assert inspect_artifact(safe)["sensitive_member_name_check"] == "PASS"
    safe_result = subprocess.run(
        [sys.executable, "-c", preflight, str(safe)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert safe_result.returncode == 0
    assert "archive_preflight=PASS" in safe_result.stdout

    linked = tmp_path / "linked.tgz"
    with tarfile.open(linked, "w:gz") as archive:
        member = tarfile.TarInfo("outside-link")
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc"
        archive.addfile(member)
    linked_result = subprocess.run(
        [sys.executable, "-c", preflight, str(linked)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert linked_result.returncode != 0
    assert "links and special members are forbidden" in linked_result.stderr


def test_release_audit_tool_has_no_commit_or_deployment_commands() -> None:
    root = Path(__file__).parents[1]
    source = (root / "scripts/release_audit.py").read_text(encoding="utf-8")
    for forbidden in (
        'run("commit"',
        'run("push"',
        "os.system(",
        "subprocess.Popen(",
        "shell=True",
    ):
        assert forbidden not in source
    assert source.count("subprocess.run(") == 2
    assert 'run("rev-parse", "--show-toplevel")' in source
    assert 'run("rev-parse", "HEAD")' in source
    assert 'run("status", "--porcelain=v1", "--untracked-files=all")' in source


def test_systemd_and_compose_record_release_identity_without_public_env_leak() -> None:
    root = Path(__file__).parents[1]
    installer = (root / "deployment/systemd/install_remote.sh").read_text(encoding="utf-8")
    compose = (root / "deployment/compose.yml").read_text(encoding="utf-8")
    web = compose.split("\n  web:\n", 1)[1].split("\n  admin:\n", 1)[0]

    assert "invalid release id" in installer
    assert "invalid archive sha256" in installer
    assert "FINANCE_RADAR_RELEASE_ID=$RELEASE_ID" in installer
    assert "finance-radar:${FINANCE_RADAR_RELEASE_ID:-local}" in compose
    assert "org.opencontainers.image.revision: ${FINANCE_RADAR_GIT_COMMIT:-unknown}" in compose
    assert "finance-radar.release-id: ${FINANCE_RADAR_RELEASE_ID:-local}" in compose
    assert "FINANCE_RADAR_RELEASE_ID" not in web
    assert "FINANCE_RADAR_GIT_COMMIT" not in web


def test_cli_accepts_real_repository_contract_on_windows_or_linux(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = Path(__file__).parents[1]
    artifact = tmp_path / "finance-radar-cli-smoke.tgz"
    git_state = release_audit.inspect_git(root)
    if git_state["available"] and not git_state["dirty"]:
        subprocess.run(
            ["git", "-C", str(root), "archive", "--format=tar.gz", f"--output={artifact}", "HEAD"],
            check=True,
        )
    else:
        # A deliberately dirty source is a workspace-byte release and must not
        # pretend that a clean Git archive contains the pending changes.
        with tarfile.open(artifact, "w:gz") as archive:
            _selection, workspace_files = release_audit._workspace_release_file_records(root)
            for entry in workspace_files:
                relative = str(entry["path"])
                archive.add(root / relative, arcname=relative, recursive=False)

    records = tmp_path / "records"
    create_result = main(
        [
            "create",
            "--root",
            str(root),
            "--release-id",
            "cli-smoke-release",
            "--artifact",
            str(artifact),
            "--verification",
            "pytest=PASS",
            "--allow-dirty",
            "--strict",
            "--output-dir",
            str(records),
        ]
    )
    create_output = json.loads(capsys.readouterr().out)
    assert create_result == 0
    assert create_output["readiness"].startswith("READY")

    manifest = records / create_output["outputs"]["json"]
    acceptance = tmp_path / "acceptance"
    verify_result = main(
        [
            "verify",
            "--manifest",
            str(manifest),
            "--root",
            str(root),
            "--artifact",
            str(artifact),
            "--expected-release-id",
            "cli-smoke-release",
            "--require-ready",
            "--require-sidecar",
            "--require-artifact",
            "--report-dir",
            str(acceptance),
        ]
    )
    verify_output = json.loads(capsys.readouterr().out)
    assert verify_result == 0
    assert verify_output["status"] == "PASS"
    assert (acceptance / verify_output["outputs"]["json"]).is_file()
