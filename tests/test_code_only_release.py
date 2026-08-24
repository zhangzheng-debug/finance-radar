from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


VALIDATOR = (
    Path(__file__).parents[1]
    / "deployment"
    / "systemd"
    / "verify_code_only_release.py"
)


def _release(root: Path, marker: str) -> Path:
    for directory in (
        "app/api",
        "app/web",
        "app/storage",
        "scripts",
        "deployment/systemd",
        "config",
        "replay/cases",
        "artifacts",
    ):
        (root / directory).mkdir(parents=True, exist_ok=True)
    files = {
        "app/api/main.py": f"api = {marker!r}\n",
        "app/web/Home.py": f"web = {marker!r}\n",
        "app/storage/ledger.py": "storage = 'stable'\n",
        "scripts/worker.py": "worker = 'stable'\n",
        "deployment/systemd/unit.service": "service = stable\n",
        "config/sources.json": "{}\n",
        "replay/cases/example.json": "{}\n",
        "artifacts/risk_router.joblib": "stable-model\n",
        "artifacts/risk_router.sha256": "stable-hash\n",
        "dependency-lock.json": "{}\n",
        "requirements.txt": "example==1\n",
        "requirements.lock": "example==1 --hash=sha256:abc\n",
        "requirements-dev.txt": "example==1\n",
        "requirements-dev.lock": "example==1 --hash=sha256:abc\n",
    }
    for relative, content in files.items():
        (root / relative).write_text(content, encoding="utf-8")
    return root


def _contract(previous: Path, candidate: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "contract",
            "--previous",
            str(previous),
            "--candidate",
            str(candidate),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_code_only_contract_accepts_public_web_change(tmp_path: Path) -> None:
    previous = _release(tmp_path / "previous", "same")
    candidate = _release(tmp_path / "candidate", "same")
    (candidate / "app/web/Home.py").write_text("web = 'changed'\n", encoding="utf-8")

    result = _contract(previous, candidate)

    assert result.returncode == 0, result.stderr
    assert "code_only_candidate_contract=PASS" in result.stdout


@pytest.mark.parametrize(
    ("relative", "content"),
    (
        ("tests/test_public_web.py", "def test_public_web():\n    assert True\n"),
        ("docs/PUBLIC_WEB.md", "# Public Web\n"),
        ("CHANGELOG.md", "# Changelog\n\n- Public Web update.\n"),
        ("CURRENT_STATE.md", "# Current state\n"),
        ("README.md", "# Finance Radar\n"),
    ),
)
def test_code_only_contract_accepts_non_runtime_release_evidence(
    tmp_path: Path,
    relative: str,
    content: str,
) -> None:
    previous = _release(tmp_path / "previous", "same")
    candidate = _release(tmp_path / "candidate", "same")
    target = candidate / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    result = _contract(previous, candidate)

    assert result.returncode == 0, result.stderr
    assert "code_only_candidate_contract=PASS" in result.stdout


def test_code_only_contract_accepts_api_change(tmp_path: Path) -> None:
    previous = _release(tmp_path / "previous", "same")
    candidate = _release(tmp_path / "candidate", "same")
    (candidate / "app/api/main.py").write_text("api = 'changed'\n", encoding="utf-8")

    result = _contract(previous, candidate)

    assert result.returncode == 0, result.stderr
    assert "code_only_candidate_contract=PASS" in result.stdout


def test_code_only_contract_rejects_unlisted_root_runtime_module(tmp_path: Path) -> None:
    previous = _release(tmp_path / "previous", "same")
    candidate = _release(tmp_path / "candidate", "same")
    (candidate / "sitecustomize.py").write_text(
        "raise RuntimeError('must require full deployment')\n",
        encoding="utf-8",
    )

    result = _contract(previous, candidate)

    assert result.returncode != 0
    assert "sitecustomize.py" in result.stderr


def test_code_only_contract_rejects_candidate_release_records(tmp_path: Path) -> None:
    previous = _release(tmp_path / "previous", "same")
    candidate = _release(tmp_path / "candidate", "same")
    (candidate / "release-records").mkdir()
    (candidate / "release-records/ACTIVATION.txt").write_text(
        "activation=PASS\n",
        encoding="utf-8",
    )

    result = _contract(previous, candidate)

    assert result.returncode != 0
    assert "release-records/ACTIVATION.txt" in result.stderr


def test_code_only_contract_rejects_empty_directory_shape_change(tmp_path: Path) -> None:
    previous = _release(tmp_path / "previous", "same")
    candidate = _release(tmp_path / "candidate", "same")
    (candidate / "release-records/ACTIVATION.txt").mkdir(parents=True)

    result = _contract(previous, candidate)

    assert result.returncode != 0
    assert "release-records" in result.stderr


def test_code_only_contract_accepts_query_storage_but_rejects_schema_or_deployment_changes(
    tmp_path: Path,
) -> None:
    previous = _release(tmp_path / "previous", "same")
    candidate = _release(tmp_path / "candidate", "same")
    (candidate / "app/storage/ledger.py").write_text("storage = 'changed'\n", encoding="utf-8")

    storage_result = _contract(previous, candidate)

    assert storage_result.returncode == 0, storage_result.stderr

    (candidate / "app/storage/ledger.py").write_text("storage = 'stable'\n", encoding="utf-8")
    (candidate / "deployment/systemd/new.service").write_text(
        "service = changed\n", encoding="utf-8"
    )

    deployment_result = _contract(previous, candidate)

    assert deployment_result.returncode != 0
    assert "deployment/systemd/new.service" in deployment_result.stderr


@pytest.mark.parametrize(
    "relative",
    (
        "requirements.lock",
        "config/sources.json",
        "replay/cases/example.json",
        "artifacts/risk_router.joblib",
    ),
)
def test_code_only_contract_still_rejects_executable_or_runtime_changes(
    tmp_path: Path,
    relative: str,
) -> None:
    previous = _release(tmp_path / "previous", "same")
    candidate = _release(tmp_path / "candidate", "same")
    (candidate / relative).write_text("changed\n", encoding="utf-8")

    result = _contract(previous, candidate)

    assert result.returncode != 0
    assert relative in result.stderr


def test_code_only_contract_accepts_worker_and_service_logic(tmp_path: Path) -> None:
    previous = _release(tmp_path / "previous", "same")
    candidate = _release(tmp_path / "candidate", "same")
    (candidate / "scripts/worker.py").write_text("worker = 'changed'\n", encoding="utf-8")
    (candidate / "app/services").mkdir(parents=True)
    (candidate / "app/services/example.py").write_text(
        "def classify(value):\n    return value\n",
        encoding="utf-8",
    )

    result = _contract(previous, candidate)

    assert result.returncode == 0, result.stderr


def test_code_only_contract_rejects_schema_owner_or_new_schema_sql(tmp_path: Path) -> None:
    previous = _release(tmp_path / "previous", "same")
    candidate = _release(tmp_path / "candidate", "same")
    (candidate / "scripts/event_ledger.py").write_text(
        "SCHEMA_VERSION = 99\n", encoding="utf-8"
    )

    schema_owner = _contract(previous, candidate)

    assert schema_owner.returncode != 0
    assert "scripts/event_ledger.py" in schema_owner.stderr

    (candidate / "scripts/event_ledger.py").unlink()
    (candidate / "app/api/new_schema.py").write_text(
        'SQL = "ALTER TABLE canonical_events ADD COLUMN unsafe TEXT"\n',
        encoding="utf-8",
    )

    embedded_schema = _contract(previous, candidate)

    assert embedded_schema.returncode != 0
    assert "database schema mutation SQL" in embedded_schema.stderr


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE canonical_events ADD COLUMN unsafe TEXT",
        "CREATE TABLE unsafe(event_id TEXT PRIMARY KEY)",
        "CREATE UNIQUE INDEX idx_unsafe ON canonical_events(event_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_unsafe ON canonical_events(event_id)",
        "CREATE VIRTUAL TABLE unsafe_fts USING fts5(body)",
        "CREATE TEMP TABLE unsafe_tmp(event_id TEXT)",
        "CREATE TEMPORARY TABLE unsafe_tmp(event_id TEXT)",
        "DROP INDEX idx_canonical_events_event_id",
        "PRAGMA user_version=99",
    ],
)
def test_code_only_contract_rejects_qualified_schema_mutation_sql(
    tmp_path: Path,
    statement: str,
) -> None:
    """Qualified DDL mutates the live schema exactly like the bare form.

    ``CREATE UNIQUE INDEX``/``VIRTUAL``/``TEMP`` forms must not reach the fast
    path: the installer's before/after live schema receipt is taken while the
    worker is still stopped, so a mutation on a worker-only or lazily executed
    code path would not be observed by that second control.
    """

    previous = _release(tmp_path / "previous", "same")
    candidate = _release(tmp_path / "candidate", "same")
    (candidate / "app/services").mkdir(parents=True, exist_ok=True)
    (candidate / "app/services/lazy_schema.py").write_text(
        f"def migrate(connection):\n    connection.execute({statement!r})\n",
        encoding="utf-8",
    )

    result = _contract(previous, candidate)

    assert result.returncode != 0, result.stdout
    assert "database schema mutation SQL" in result.stderr


def test_code_only_schema_receipt_changes_only_when_sqlite_schema_changes(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.sqlite3"
    operations = tmp_path / "operations.sqlite3"
    with sqlite3.connect(ledger) as connection:
        connection.execute("CREATE TABLE event_ledger_schema(version INTEGER)")
        connection.execute("INSERT INTO event_ledger_schema VALUES (14)")
        connection.execute("CREATE TABLE events(event_id TEXT PRIMARY KEY)")
    with sqlite3.connect(operations) as connection:
        connection.execute("CREATE TABLE operations_schema(version INTEGER)")
        connection.execute("INSERT INTO operations_schema VALUES (10)")
        connection.execute("CREATE TABLE worker_cycles(cycle_id TEXT PRIMARY KEY)")

    command = [
        sys.executable,
        str(VALIDATOR),
        "schema",
        "--ledger",
        str(ledger),
        "--operations",
        str(operations),
    ]
    first = subprocess.run(command, capture_output=True, text=True, check=False)
    with sqlite3.connect(ledger) as connection:
        connection.execute("INSERT INTO events VALUES ('row-only-change')")
    row_change = subprocess.run(command, capture_output=True, text=True, check=False)
    with sqlite3.connect(ledger) as connection:
        connection.execute("CREATE INDEX idx_events_id ON events(event_id)")
    schema_change = subprocess.run(command, capture_output=True, text=True, check=False)

    assert first.returncode == row_change.returncode == schema_change.returncode == 0
    assert json.loads(first.stdout)["sha256"] == json.loads(row_change.stdout)["sha256"]
    assert json.loads(first.stdout)["sha256"] != json.loads(schema_change.stdout)["sha256"]


@pytest.mark.skipif(
    os.name == "nt" or getattr(os, "geteuid", lambda: -1)() != 0,
    reason="root ownership/mode semantics require a root POSIX test process",
)
def test_code_only_backup_requires_unchanged_root_attested_bundle(tmp_path: Path) -> None:
    operations = tmp_path / "operations.sqlite3"
    backup_root = tmp_path / "operational_backups"
    bundle = backup_root / "finance_radar_20260823T000000Z"
    bundle.mkdir(parents=True)
    payload = bundle / "ledger.sqlite3"
    payload.write_bytes(b"verified recovery payload")
    empty_payload = bundle / "empty.marker"
    empty_payload.write_bytes(b"")
    payload_sha = hashlib.sha256(payload.read_bytes()).hexdigest()
    empty_payload_sha = hashlib.sha256(b"").hexdigest()
    components = {"ledger": {"sha256": payload_sha}, "operations": {"sha256": "b" * 64}}
    manifest = {
        "format": "finance-radar-recovery-bundle-v1",
        "snapshot_id": bundle.name,
        "components": components,
        "files": [
            {
                "path": payload.name,
                "bytes": payload.stat().st_size,
                "sha256": payload_sha,
            },
            {"path": empty_payload.name, "bytes": 0, "sha256": empty_payload_sha},
        ],
    }
    manifest_path = bundle / "manifest.json"
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode("utf-8")
    manifest_path.write_bytes(manifest_bytes)
    verified_at = datetime.now(timezone.utc).isoformat()
    restored_count_json = json.dumps({"canonical_events": 10}, sort_keys=True)
    components_json = json.dumps(components, sort_keys=True)
    with sqlite3.connect(operations) as connection:
        connection.execute(
            """CREATE TABLE backup_runs(
               backup_id TEXT,backup_path TEXT,source_bytes INTEGER,backup_bytes INTEGER,
               quick_check TEXT,restored_count_json TEXT,status TEXT,created_at TEXT,
               verified_at TEXT,manifest_path TEXT,components_json TEXT,snapshot_kind TEXT)"""
        )
        connection.execute(
            "INSERT INTO backup_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "backup-test",
                str(manifest_path),
                payload.stat().st_size,
                payload.stat().st_size + len(manifest_bytes),
                "ok",
                restored_count_json,
                "VERIFIED",
                verified_at,
                verified_at,
                str(manifest_path),
                components_json,
                "recovery_bundle",
            ),
        )
    payload_stat = payload.stat()
    empty_payload_stat = empty_payload.stat()
    attestation = {
        "format": "finance-radar-root-backup-attestation-v1",
        "backup_id": "backup-test",
        "snapshot_id": bundle.name,
        "manifest_path": str(manifest_path),
        "status": "VERIFIED",
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "verified_at": verified_at,
        "quick_check": "ok",
        "snapshot_kind": "recovery_bundle",
        "source_bytes": payload.stat().st_size,
        "backup_bytes": payload.stat().st_size + len(manifest_bytes),
        "restored_count_json": restored_count_json,
        "components": components,
        "payload_stats": [
            {
                "path": payload.name,
                "bytes": payload_stat.st_size,
                "device": payload_stat.st_dev,
                "inode": payload_stat.st_ino,
                "mtime_ns": payload_stat.st_mtime_ns,
                "sha256": payload_sha,
            },
            {
                "path": empty_payload.name,
                "bytes": 0,
                "device": empty_payload_stat.st_dev,
                "inode": empty_payload_stat.st_ino,
                "mtime_ns": empty_payload_stat.st_mtime_ns,
                "sha256": empty_payload_sha,
            },
        ],
    }
    attestation_dir = tmp_path / "root-attestation"
    attestation_dir.mkdir()
    attestation_dir.chmod(0o700)
    attestation_path = attestation_dir / "latest-verified-backup.json"
    attestation_path.write_text(json.dumps(attestation), encoding="utf-8")
    attestation_path.chmod(0o600)

    command = [
        sys.executable,
        str(VALIDATOR),
        "backup",
        "--operations",
        str(operations),
        "--backup-root",
        str(backup_root),
        "--attestation",
        str(attestation_path),
        "--max-age-seconds",
        "93600",
    ]
    valid = subprocess.run(command, capture_output=True, text=True, check=False)

    assert valid.returncode == 0, valid.stderr
    assert json.loads(valid.stdout)["backup_id"] == "backup-test"

    extra = bundle / "unattested.tmp"
    extra.write_bytes(b"not in manifest")
    extra_result = subprocess.run(command, capture_output=True, text=True, check=False)

    assert extra_result.returncode != 0
    assert "bundle file set changed" in extra_result.stderr
    extra.unlink()

    original_stat = payload.stat()
    payload.write_bytes(b"X" * original_stat.st_size)
    os.utime(payload, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    tampered = subprocess.run(command, capture_output=True, text=True, check=False)

    assert tampered.returncode != 0
    assert "payload changed after attestation" in tampered.stderr
