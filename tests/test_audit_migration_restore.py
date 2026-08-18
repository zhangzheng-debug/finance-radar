from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from scripts.audit_migration_restore import (
    CAPTURE_LIMIT,
    MAX_UNPACKED_BYTES,
    _stream_regular_file,
    _validate_link,
    audit_archive,
    render_markdown,
)
from scripts.backup_crypto import encrypt_file


STAMP = "20260718T010203Z"
# Match release_audit.py's default timestamp-plus-commit release identity.
RELEASE = "20260718T000000Z-deadbeefcafe"
PASSPHRASE = "correct horse battery staple for backup"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _ledger(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE event_ledger_schema(version INTEGER);
            INSERT INTO event_ledger_schema VALUES (12);
            CREATE TABLE sources(id INTEGER);
            INSERT INTO sources VALUES (1);
            CREATE TABLE raw_observations(id INTEGER);
            INSERT INTO raw_observations VALUES (1);
            CREATE TABLE canonical_events(id INTEGER, no_trading INTEGER);
            INSERT INTO canonical_events VALUES (1, 1);
            CREATE TABLE event_versions(id INTEGER);
            INSERT INTO event_versions VALUES (1);
            CREATE TABLE event_evidence(id INTEGER, auto_verification_allowed INTEGER);
            INSERT INTO event_evidence VALUES (1, 0);
            CREATE TABLE event_market_metrics(id INTEGER, allowed_as_model_feature INTEGER);
            INSERT INTO event_market_metrics VALUES (1, 0);
            CREATE TABLE pipeline_jobs(id INTEGER);
            INSERT INTO pipeline_jobs VALUES (1);
            CREATE TABLE alert_outbox(id INTEGER);
            """
        )


def _operations(path: Path, *, schema_version: int = 4) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE operations_schema(version INTEGER)")
        connection.execute("INSERT INTO operations_schema VALUES (?)", (schema_version,))
        for table in (
            "replay_runs",
            "model_runs",
            "worker_cycles",
            "backup_runs",
            "agent_decisions",
            "evidence_objects",
            "human_overrides",
        ):
            connection.execute(f"CREATE TABLE {table}(id INTEGER)")
        connection.execute("CREATE TABLE adjudication_samples(id INTEGER)")
        connection.execute("CREATE TABLE adjudication_reviews(id INTEGER)")
        if schema_version >= 6:
            connection.execute("CREATE TABLE light_verification_runs(id INTEGER)")
            connection.execute("CREATE TABLE formal_mutation_audits(id INTEGER)")
        connection.execute("INSERT INTO worker_cycles VALUES (1)")
        connection.commit()


def _fixture(
    tmp_path: Path,
    *,
    tamper_after_manifest: bool = False,
    wrong_model_hash: bool = False,
    operations_schema_version: int = 4,
    declared_absent_local_model: bool = False,
    legacy_model_unit: bool = False,
    bound_recovery_bundle: bool = False,
    tamper_recovery_bundle_mapping: bool = False,
    extra_unbound_recovery_payload: bool = False,
) -> tuple[Path, Path, str]:
    root = tmp_path / f"finance-radar-migration-{STAMP}"
    release = root / "releases" / RELEASE
    _write(root / "CURRENT_RELEASE.txt", f"/opt/finance-radar/releases/{RELEASE}\n")
    _write(release / "app/api/main.py", "app = 'api'\n")
    _write(release / "app/web/Home.py", "page = 'home'\n")
    _write(release / "app/web/Reviewer.py", "page = 'reviewer'\n")
    _write(release / "app/web/Operator.py", "page = 'operator'\n")
    _write(
        release / "scripts/official_event_collector.py",
        "ecb_press ecb_statistical_press eia_press nvidia_official_news\n",
    )
    _write(
        release / "artifacts/risk_router_external_blind_v3_report.json",
        json.dumps(
            {
                "gate_pass": True,
                "promotion_decision": "QUALIFIED_SHADOW",
                "model_version": "risk-router-v4-test",
                "model_artifact_sha256": hashlib.sha256(
                    b"wrong-model" if wrong_model_hash else b"model"
                ).hexdigest(),
                "no_trading": True,
                "shadow": True,
            }
        ),
    )
    (release / "artifacts/risk_router.joblib").write_bytes(b"model")
    model_sha = hashlib.sha256(b"model").hexdigest()
    _write(release / "artifacts/risk_router.sha256", f"{model_sha}  risk_router.joblib\n")
    _write(
        release / "artifacts/risk_router_model_card.json",
        json.dumps(
            {
                "model_version": "risk-router-v4-test",
                "artifact_sha256": model_sha,
                "shadow": True,
                "no_trading": True,
            }
        ),
    )
    _write(release / "requirements.txt", "fastapi\n")
    _write(release / "requirements.lock", "fastapi==1.0 --hash=sha256:fixture\n")
    if legacy_model_unit or declared_absent_local_model:
        _write(
            release / "deployment/systemd/finance-radar-evidence-llm.service",
            "[Unit]\nDescription=optional local model\n",
        )
    if declared_absent_local_model:
        _write(
            root / "config/LOCAL_EVIDENCE_MODEL_CAPABILITY.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "local_evidence_model",
                    "installed": False,
                    "archive_includes_model": False,
                    "restore_policy": "DISABLED_AFTER_RESTORE",
                }
            ),
        )
    _write(root / "config/etc/finance-radar.env", "SECRET=not-logged\n")
    _write(root / "config/etc/nginx/sites-enabled/finance-radar.conf", "server {}\n")
    for name in ("CERTIFICATE_STATUS.txt", "SERVICE_STATUS.txt", "PYTHON_VERSION.txt", "PIP_FREEZE.txt"):
        _write(root / "config" / name, f"{name}\n")
    data = root / "shared/data"
    data.mkdir(parents=True)
    _ledger(data / "finance_radar.sqlite3")
    _operations(data / "finance_radar_operations.sqlite3", schema_version=operations_schema_version)
    if bound_recovery_bundle:
        snapshot_id = "finance_radar_20260805T000000Z_abcdef12"
        source_entries = []
        mapping = []
        for source_path, target_path in (
            ("ledger.sqlite3", "shared/data/finance_radar.sqlite3"),
            ("operations.sqlite3", "shared/data/finance_radar_operations.sqlite3"),
        ):
            payload = root / target_path
            source_entries.append(
                {
                    "path": source_path,
                    "bytes": payload.stat().st_size,
                    "sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
                }
            )
            mapping.append(
                {
                    "source_path": source_path,
                    "target_path": target_path,
                    "bytes": payload.stat().st_size,
                    "sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
                }
            )
        if tamper_recovery_bundle_mapping:
            mapping[0]["target_path"] = "shared/reports/not-the-ledger.sqlite3"
        source_manifest = {
            "format": "finance-radar-recovery-bundle-v1",
            "snapshot_id": snapshot_id,
            "files": source_entries,
        }
        source_manifest_raw = json.dumps(source_manifest, sort_keys=True).encode("utf-8")
        source_manifest_path = root / "config/MIGRATION_RECOVERY_BUNDLE.manifest.json"
        _write(source_manifest_path, source_manifest_raw.decode("utf-8") + "\n")
        _write(
            root / "config/MIGRATION_RECOVERY_BUNDLE.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "snapshot_id": snapshot_id,
                    "source_manifest_sha256": hashlib.sha256(source_manifest_path.read_bytes()).hexdigest(),
                    "source_manifest_path": "config/MIGRATION_RECOVERY_BUNDLE.manifest.json",
                    "mapping": mapping,
                    "consistency": "verified_full_recovery_bundle",
                },
                sort_keys=True,
            )
            + "\n",
        )
        if extra_unbound_recovery_payload:
            _write(root / "shared/reports/unbound-live-copy.txt", "must not be restored\n")

    manifest_lines = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest_lines.append(f"{digest}  ./{path.relative_to(root).as_posix()}")
    _write(root / "MANIFEST.sha256", "\n".join(manifest_lines) + "\n")
    if tamper_after_manifest:
        _write(release / "app/api/main.py", "app = 'tampered'\n")

    plain = tmp_path / f"finance-radar-migration-{STAMP}.tgz"
    with tarfile.open(plain, "w:gz") as archive:
        archive.add(root, arcname=root.name)
    expected_sha256 = hashlib.sha256(plain.read_bytes()).hexdigest()
    encrypted = tmp_path / f"finance-radar-migration-{STAMP}.tgz.aesgcm"
    passphrase = tmp_path / "passphrase"
    passphrase.write_text(PASSPHRASE + "\n", encoding="utf-8")
    encrypt_file(plain, encrypted, PASSPHRASE)
    plain.unlink()
    return encrypted, passphrase, expected_sha256


def test_full_encrypted_migration_restore_audit(tmp_path: Path) -> None:
    encrypted, passphrase, expected_sha256 = _fixture(tmp_path)
    result = audit_archive(
        encrypted,
        passphrase,
        expected_release=RELEASE,
        expected_sha256=expected_sha256,
    )
    assert result["status"] == "PASS"
    assert result["archive"]["manifest_all_match"] is True
    assert result["ledger_restore"]["schema_version"] == 12
    assert result["ledger_restore"]["audit"] == {
        "trading_boundary_violations": 0,
        "auto_verification_violations": 0,
        "market_feature_leakage_violations": 0,
    }
    assert result["operations_restore"]["schema_version"] == 4
    assert result["release"]["external_blind_generation"] == "v3"
    assert result["release"]["external_blind_gate_pass"] is True
    assert result["release"]["external_blind_promotion"] == "QUALIFIED_SHADOW"
    assert result["release"]["risk_router_hash_chain_match"] is True
    assert result["temporary_workspace_cleaned"] is True
    markdown = render_markdown(result)
    assert "Result: **PASS**" in markdown
    assert f"Accepted release: `{RELEASE}`" in markdown
    assert "Trading project included: `False`" in markdown
    assert "risk-router-v4-test" in markdown
    assert "Artifact/card/report SHA-256 chain matched: `True`" in markdown


def test_restore_audit_uses_and_cleans_an_explicit_workspace_root(tmp_path: Path) -> None:
    encrypted, passphrase, expected_sha256 = _fixture(tmp_path)
    workspace_root = tmp_path / "D-drive-audit-workspace"

    result = audit_archive(
        encrypted,
        passphrase,
        expected_release=RELEASE,
        expected_sha256=expected_sha256,
        workspace_root=workspace_root,
    )

    assert result["temporary_workspace_parent"] == str(workspace_root.resolve())
    assert result["temporary_workspace_cleaned"] is True
    assert list(workspace_root.iterdir()) == []


def test_restore_audit_rejects_manifest_mismatch(tmp_path: Path) -> None:
    encrypted, passphrase, expected_sha256 = _fixture(tmp_path, tamper_after_manifest=True)
    with pytest.raises(ValueError, match="manifest verification failed"):
        audit_archive(
            encrypted,
            passphrase,
            expected_release=RELEASE,
            expected_sha256=expected_sha256,
        )


def test_restore_audit_rejects_model_governance_hash_mismatch(tmp_path: Path) -> None:
    encrypted, passphrase, expected_sha256 = _fixture(tmp_path, wrong_model_hash=True)
    with pytest.raises(ValueError, match="artifact, declaration, blind report and model card hashes differ"):
        audit_archive(
            encrypted,
            passphrase,
            expected_release=RELEASE,
            expected_sha256=expected_sha256,
        )


def test_restore_audit_accepts_current_operations_schema_six(tmp_path: Path) -> None:
    encrypted, passphrase, expected_sha256 = _fixture(tmp_path, operations_schema_version=6)

    result = audit_archive(
        encrypted,
        passphrase,
        expected_release=RELEASE,
        expected_sha256=expected_sha256,
    )

    assert result["operations_restore"]["schema_version"] == 6
    assert result["operations_restore"]["counts"]["light_verification_runs"] == 0
    assert result["operations_restore"]["counts"]["formal_mutation_audits"] == 0


def test_restore_audit_binds_new_archive_to_verified_recovery_bundle(tmp_path: Path) -> None:
    encrypted, passphrase, expected_sha256 = _fixture(tmp_path, bound_recovery_bundle=True)

    result = audit_archive(
        encrypted,
        passphrase,
        expected_release=RELEASE,
        expected_sha256=expected_sha256,
    )

    bundle = result["migration_recovery_bundle"]
    assert bundle["bound_to_verified_recovery_bundle"] is True
    assert bundle["legacy_archive_contract"] is False
    assert bundle["consistency"] == "verified_full_recovery_bundle"
    assert bundle["mapped_files"] == 2


def test_restore_audit_rejects_recovery_bundle_mapping_mismatch(tmp_path: Path) -> None:
    encrypted, passphrase, expected_sha256 = _fixture(
        tmp_path,
        bound_recovery_bundle=True,
        tamper_recovery_bundle_mapping=True,
    )

    with pytest.raises(ValueError, match="migration recovery-bundle staged payload"):
        audit_archive(
            encrypted,
            passphrase,
            expected_release=RELEASE,
            expected_sha256=expected_sha256,
        )


def test_restore_audit_rejects_unbound_staged_payload_in_bound_bundle(tmp_path: Path) -> None:
    encrypted, passphrase, expected_sha256 = _fixture(
        tmp_path,
        bound_recovery_bundle=True,
        extra_unbound_recovery_payload=True,
    )

    with pytest.raises(ValueError, match="staged payload set is not exact"):
        audit_archive(
            encrypted,
            passphrase,
            expected_release=RELEASE,
            expected_sha256=expected_sha256,
        )


def test_restore_audit_accepts_declared_absent_optional_local_model(tmp_path: Path) -> None:
    encrypted, passphrase, expected_sha256 = _fixture(tmp_path, declared_absent_local_model=True)

    result = audit_archive(
        encrypted,
        passphrase,
        expected_release=RELEASE,
        expected_sha256=expected_sha256,
    )

    local_model = result["local_evidence_model"]
    assert local_model["capability_declared"] is True
    assert local_model["required_by_release"] is False
    assert local_model["included"] is False
    assert local_model["restore_policy"] == "DISABLED_AFTER_RESTORE"


def test_restore_audit_keeps_legacy_model_unit_contract(tmp_path: Path) -> None:
    encrypted, passphrase, expected_sha256 = _fixture(tmp_path, legacy_model_unit=True)

    with pytest.raises(ValueError, match="local evidence model is missing"):
        audit_archive(
            encrypted,
            passphrase,
            expected_release=RELEASE,
            expected_sha256=expected_sha256,
        )


def test_restore_audit_accepts_environment_passphrase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    encrypted, _, expected_sha256 = _fixture(tmp_path)
    monkeypatch.setenv("FINANCE_RADAR_BACKUP_PASSPHRASE", PASSPHRASE)
    result = audit_archive(
        encrypted,
        tmp_path / "missing-passphrase-file",
        expected_release=RELEASE,
        expected_sha256=expected_sha256,
    )
    assert result["status"] == "PASS"


def test_restore_audit_rejects_path_traversal(tmp_path: Path) -> None:
    plain = tmp_path / f"finance-radar-migration-{STAMP}.tgz"
    with tarfile.open(plain, "w:gz") as archive:
        payload = b"escape"
        member = tarfile.TarInfo(f"finance-radar-migration-{STAMP}/../escape.txt")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    expected_sha256 = hashlib.sha256(plain.read_bytes()).hexdigest()
    encrypted = tmp_path / f"finance-radar-migration-{STAMP}.tgz.aesgcm"
    passphrase = tmp_path / "passphrase"
    passphrase.write_text(PASSPHRASE + "\n", encoding="utf-8")
    encrypt_file(plain, encrypted, PASSPHRASE)
    plain.unlink()
    with pytest.raises(ValueError, match="unsafe archive member path"):
        audit_archive(
            encrypted,
            passphrase,
            expected_release=RELEASE,
            expected_sha256=expected_sha256,
        )


def test_restore_audit_rejects_unsafe_release_id_before_building_paths(tmp_path: Path) -> None:
    encrypted, passphrase, expected_sha256 = _fixture(tmp_path)

    with pytest.raises(ValueError, match="expected release id is invalid"):
        audit_archive(
            encrypted,
            passphrase,
            expected_release="20260718T000000Z-deadbeefcafe/../../escape",
            expected_sha256=expected_sha256,
        )


def test_restore_audit_allows_only_expected_absolute_shared_links() -> None:
    member = tarfile.TarInfo(f"finance-radar-migration-{STAMP}/releases/{RELEASE}/data")
    member.type = tarfile.SYMTYPE
    member.linkname = "/opt/finance-radar/shared/data"
    _validate_link(member, f"finance-radar-migration-{STAMP}")
    member.linkname = "/etc/passwd"
    with pytest.raises(ValueError, match="unsafe archive link target"):
        _validate_link(member, f"finance-radar-migration-{STAMP}")


def test_restore_audit_limits_match_the_bounded_full_recovery_contract() -> None:
    assert MAX_UNPACKED_BYTES == 12 * 1024 * 1024 * 1024
    assert CAPTURE_LIMIT == 16 * 1024 * 1024
    with pytest.raises(ValueError, match="captured archive member exceeds safety limit"):
        _stream_regular_file(io.BytesIO(b"x" * (CAPTURE_LIMIT + 1)), capture=True)
