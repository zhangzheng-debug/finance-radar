from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from scripts.build_defense_evidence_pack import build_pack, collect_entries


def test_curated_defense_pack_has_matching_manifest(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("safe evidence", encoding="utf-8")
    (tmp_path / "report.json").write_text('{"passed":true}', encoding="utf-8")
    destination = tmp_path / "out/pack.zip"
    report = build_pack(
        tmp_path,
        destination,
        evidence_files=("README.md", "report.json"),
    )
    assert report["status"] == "PASS"
    assert report["evidence_entries"] == 2
    assert report["manifest_test"] == "PASS"
    with zipfile.ZipFile(destination) as archive:
        metadata = json.loads(archive.read("EVIDENCE_PACK.json"))
        assert metadata["entry_count"] == 2
        assert archive.testzip() is None


def test_defense_pack_rejects_secret_like_token(tmp_path: Path) -> None:
    (tmp_path / "report.md").write_text(
        "token=1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi_12345",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="secret-like value"):
        collect_entries(tmp_path, ("report.md",))
