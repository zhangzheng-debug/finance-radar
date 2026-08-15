from __future__ import annotations

import json
from pathlib import Path

from scripts.verify_dependency_locks import verify


ROOT = Path(__file__).resolve().parents[1]


def test_dependency_inputs_and_hash_locks_match_metadata() -> None:
    report = verify(ROOT)
    assert report["status"] == "PASS", report
    assert report["verified_files"] == 4


def test_dependency_lock_verifier_detects_input_drift(tmp_path: Path) -> None:
    for name in (
        "dependency-lock.json",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements.lock",
        "requirements-dev.lock",
    ):
        (tmp_path / name).write_bytes((ROOT / name).read_bytes())
    (tmp_path / "requirements.txt").write_text("fastapi>=999\n", encoding="utf-8")
    report = verify(tmp_path)
    assert report["status"] == "FAIL"
    assert any("hash mismatch: requirements.txt" in item for item in report["failures"])


def test_dependency_lock_metadata_declares_supported_python() -> None:
    metadata = json.loads((ROOT / "dependency-lock.json").read_text(encoding="utf-8"))
    assert metadata["python_version"] == "3.12"
    assert metadata["resolver"] == "uv 0.12.1"
