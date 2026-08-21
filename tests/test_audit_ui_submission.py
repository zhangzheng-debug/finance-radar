from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts.audit_ui_submission import (
    audit_submission,
    classify_member_name,
    hygiene_findings,
    main,
    normalize_text,
    render_report,
    sha256_bytes,
)


def _write_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def _materialize(root: Path, members: dict[str, bytes]) -> None:
    for name, data in members.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


SUBMISSION = {
    "ui_preview/index.html": b"<!doctype html>\n<title>preview</title>\n",
    "scripts/serve_preview.py": b"VALUE = 1\n",
    "tests/test_serve_preview.py": b"def test_value() -> None:\n    assert True\n",
}


def test_classify_member_name_rejects_unsafe_and_unreviewable_members() -> None:
    assert classify_member_name("ui_preview/index.html") is None
    assert "traversal" in (classify_member_name("../outside.py") or "")
    assert "absolute" in (classify_member_name("/etc/passwd") or "")
    assert "absolute" in (classify_member_name("C:/keys.txt") or "")
    assert "backslash" in (classify_member_name("ui_preview\\index.html") or "")
    assert "Git metadata" in (classify_member_name(".git/config") or "")
    assert "forbidden file name" in (classify_member_name("ui_preview/.env") or "")
    assert "forbidden file type" in (classify_member_name("ui_preview/nested.zip") or "")


def test_normalize_text_drops_bom_and_crlf() -> None:
    assert normalize_text(b"\xef\xbb\xbfa\r\nb\r\n") == b"a\nb\n"


def test_hygiene_findings_separates_blocking_defects_from_warnings() -> None:
    failures, warnings = hygiene_findings("scripts/a.py", b"VALUE = 1   \nOTHER = 2\n")
    assert any("trailing whitespace" in item for item in failures)
    assert warnings == []

    failures, warnings = hygiene_findings("scripts/a.py", b"VALUE = 1\r\nOTHER = 2\r\n")
    assert failures == []
    assert any("CRLF" in item for item in warnings)

    failures, _ = hygiene_findings("scripts/a.py", b"\xef\xbb\xbfVALUE = 1\n")
    assert any("BOM" in item for item in failures)

    # Binary members are never line-linted.
    assert hygiene_findings("ui_preview/logo.png", b"\x89PNG\r\n   ") == ([], [])


def test_audit_passes_when_every_member_is_committed_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _materialize(root, SUBMISSION)
    archive = _write_zip(tmp_path / "ui.zip", SUBMISSION)

    report = audit_submission(zip_path=archive, root=root, tracked=set(SUBMISSION))

    assert report["status"] == "PASS"
    assert report["failures"] == []
    assert {entry["status"] for entry in report["entries"]} == {"MATCH"}
    assert report["archive"]["member_count"] == 3
    assert report["entries"][0]["zip_sha256"] == report["entries"][0]["repo_sha256"]


def test_audit_reports_mismatch_missing_and_untracked_paths(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    committed = dict(SUBMISSION)
    committed["scripts/serve_preview.py"] = b"VALUE = 999\n"
    del committed["tests/test_serve_preview.py"]
    _materialize(root, committed)
    (root / "ui_preview" / "index.html").write_bytes(SUBMISSION["ui_preview/index.html"])
    archive = _write_zip(tmp_path / "ui.zip", SUBMISSION)

    report = audit_submission(
        zip_path=archive,
        root=root,
        tracked={"scripts/serve_preview.py"},
    )

    statuses = {entry["path"]: entry["status"] for entry in report["entries"]}
    assert statuses["scripts/serve_preview.py"] == "MISMATCH"
    assert statuses["tests/test_serve_preview.py"] == "MISSING"
    assert statuses["ui_preview/index.html"] == "UNTRACKED"
    assert report["status"] == "FAIL"
    assert any("differs from the archive" in item for item in report["failures"])
    assert any("not tracked by Git" in item for item in report["failures"])


def test_audit_accepts_line_ending_normalisation_as_a_warning(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _materialize(root, {"scripts/serve_preview.py": b"VALUE = 1\nOTHER = 2\n"})
    archive = _write_zip(tmp_path / "ui.zip", {"scripts/serve_preview.py": b"VALUE = 1\r\nOTHER = 2\r\n"})

    report = audit_submission(zip_path=archive, root=root, tracked={"scripts/serve_preview.py"})

    assert report["status"] == "PASS"
    assert report["entries"][0]["status"] == "MATCH_NORMALIZED"
    assert any("normalisation" in item for item in report["warnings"])


def test_audit_fails_on_branch_changes_the_archive_does_not_contain(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _materialize(root, SUBMISSION)
    archive = _write_zip(tmp_path / "ui.zip", SUBMISSION)
    changed = sorted(SUBMISSION) + ["app/api/main.py"]

    report = audit_submission(
        zip_path=archive,
        root=root,
        tracked=set(SUBMISSION),
        changed_paths=changed,
    )
    assert report["status"] == "FAIL"
    assert report["extra_changed_paths"] == ["app/api/main.py"]

    allowed = audit_submission(
        zip_path=archive,
        root=root,
        tracked=set(SUBMISSION),
        changed_paths=changed,
        allow_extra=True,
    )
    assert allowed["status"] == "PASS"
    assert allowed["extra_changed_paths"] == ["app/api/main.py"]


def test_audit_rejects_unsafe_members_before_touching_the_tree(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    archive = _write_zip(
        tmp_path / "ui.zip",
        {"../escape.py": b"VALUE = 1\n", "ui_preview/bundle.zip": b"PK\x03\x04"},
    )

    report = audit_submission(zip_path=archive, root=root, tracked=set())

    assert report["status"] == "FAIL"
    assert report["entries"] == []
    assert any("traversal" in item for item in report["failures"])
    assert any("forbidden file type" in item for item in report["failures"])
    assert not (tmp_path / "escape.py").exists()


def test_render_report_shows_digests_and_verdict(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _materialize(root, SUBMISSION)
    archive = _write_zip(tmp_path / "ui.zip", SUBMISSION)

    text = render_report(audit_submission(zip_path=archive, root=root, tracked=set(SUBMISSION)))

    assert "Result: PASS" in text
    assert sha256_bytes(SUBMISSION["scripts/serve_preview.py"]) in text
    assert "ui_preview/index.html" in text


def _init_repo(root: Path, members: dict[str, bytes]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _materialize(root, members)
    for command in (
        ("git", "init", "-q", "-b", "main"),
        ("git", "config", "user.name", "Submission Author"),
        ("git", "config", "user.email", "author@example.invalid"),
        ("git", "add", "-A"),
        ("git", "commit", "-q", "-m", "Add UI preview handover"),
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True)


def test_main_records_git_authorship_for_a_clean_submission(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "repo"
    _init_repo(root, SUBMISSION)
    archive = _write_zip(tmp_path / "ui.zip", SUBMISSION)
    manifest = tmp_path / "record" / "audit.json"

    exit_code = main(
        ["--zip", str(archive), "--root", str(root), "--manifest", str(manifest), "--with-authors"]
    )

    assert exit_code == 0
    record = json.loads(manifest.read_text(encoding="utf-8"))
    assert record["status"] == "PASS"
    assert record["archive"]["sha256"] == sha256_bytes(archive.read_bytes())
    authors = {entry["introduced_by"]["author_email"] for entry in record["entries"]}
    assert authors == {"author@example.invalid"}
    assert "Result: PASS" in capsys.readouterr().out


def test_main_fails_when_the_tree_no_longer_matches_the_archive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "repo"
    _init_repo(root, SUBMISSION)
    archive = _write_zip(tmp_path / "ui.zip", SUBMISSION)
    manifest = tmp_path / "record" / "audit.json"
    (root / "ui_preview" / "index.html").write_bytes(b"tampered\n")

    exit_code = main(["--zip", str(archive), "--root", str(root), "--manifest", str(manifest)])

    assert exit_code == 1
    record = json.loads(manifest.read_text(encoding="utf-8"))
    assert record["status"] == "FAIL"
    assert "Result: FAIL" in capsys.readouterr().out


def test_main_refuses_to_audit_outside_a_git_clone(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    _materialize(root, SUBMISSION)
    archive = _write_zip(tmp_path / "ui.zip", SUBMISSION)

    assert main(["--zip", str(archive), "--root", str(root)]) == 2
    assert "cannot read Git state" in capsys.readouterr().err
