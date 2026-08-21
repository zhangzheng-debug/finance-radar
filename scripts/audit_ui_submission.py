"""Audit a UI handover zip against the committed repository tree.

A zip carries no author, no history and no diff, so it can never be evidence by
itself. This audit binds one submitted archive to one reviewed Git state: every
zip member must exist at the same repository path, be tracked by Git, and match
byte for byte once Git's `text eol=lf` normalisation is accounted for. When a
base revision is supplied it also proves the branch changed nothing the archive
does not contain.

The audit never writes to the repository and never trusts the archive: member
names are validated before any path is joined, and archive-only file types are
refused outright.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

# Suffixes `.gitattributes` normalises to LF on commit. A Windows handover may
# therefore differ from the committed blob by line endings alone.
TEXT_SUFFIXES = frozenset(
    {
        ".cfg",
        ".conf",
        ".css",
        ".csv",
        ".html",
        ".ini",
        ".js",
        ".json",
        ".jsonl",
        ".md",
        ".py",
        ".sh",
        ".toml",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)

# File types that must never arrive through a UI handover: build output, nested
# archives, credential material and databases are not reviewable source.
REJECTED_SUFFIXES = frozenset(
    {
        ".7z",
        ".aesgcm",
        ".dll",
        ".dylib",
        ".exe",
        ".gz",
        ".joblib",
        ".key",
        ".p12",
        ".pem",
        ".pfx",
        ".pyc",
        ".pyo",
        ".rar",
        ".session",
        ".so",
        ".sqlite3",
        ".tgz",
        ".zip",
    }
)
REJECTED_NAMES = frozenset({".env", ".deploy_context.json"})

MAX_MEMBER_BYTES = 20 * 1024 * 1024
MAX_MEMBERS = 2000

_UTF8_BOM = b"\xef\xbb\xbf"

# Statuses that keep a submission acceptable.
PASSING_STATUSES = frozenset({"MATCH", "MATCH_NORMALIZED"})


def sha256_bytes(data: bytes) -> str:
    """Return the hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def is_text_path(path: str) -> bool:
    """Report whether Git normalises line endings for ``path``."""
    return PurePosixPath(path).suffix.lower() in TEXT_SUFFIXES


def normalize_text(data: bytes) -> bytes:
    """Return ``data`` with a UTF-8 BOM and CRLF endings removed."""
    if data.startswith(_UTF8_BOM):
        data = data[len(_UTF8_BOM) :]
    return data.replace(b"\r\n", b"\n")


def classify_member_name(name: str) -> str | None:
    """Return a rejection reason for an unsafe or unreviewable member name."""
    if not name or name.endswith("/"):
        return "empty member name"
    if "\\" in name:
        return "backslash in member name"
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return "absolute member path"
    parts = PurePosixPath(name).parts
    if any(part in {"..", "."} for part in parts):
        return "path traversal in member name"
    if any(part.startswith(".git") for part in parts):
        return "member writes into Git metadata"
    lowered = PurePosixPath(name).name.lower()
    if lowered in REJECTED_NAMES:
        return f"forbidden file name: {lowered}"
    suffix = PurePosixPath(name).suffix.lower()
    if suffix in REJECTED_SUFFIXES:
        return f"forbidden file type: {suffix}"
    return None


def hygiene_findings(path: str, data: bytes) -> tuple[list[str], list[str]]:
    """Return ``(failures, warnings)`` for one submitted text member."""
    failures: list[str] = []
    warnings: list[str] = []
    if not is_text_path(path):
        return failures, warnings
    if data.startswith(_UTF8_BOM):
        failures.append("UTF-8 BOM: re-save as UTF-8 without BOM")
    try:
        text = normalize_text(data).decode("utf-8")
    except UnicodeDecodeError:
        failures.append("not valid UTF-8")
        return failures, warnings
    lines = text.split("\n")
    trailing = [index + 1 for index, line in enumerate(lines[:-1]) if line != line.rstrip()]
    if trailing:
        shown = ", ".join(str(number) for number in trailing[:5])
        more = " ..." if len(trailing) > 5 else ""
        failures.append(f"trailing whitespace on line(s) {shown}{more}: CI runs git diff --check")
    if data.count(b"\r\n"):
        warnings.append("CRLF line endings; Git will normalise them to LF on commit")
    if data and not data.endswith(b"\n"):
        warnings.append("no final newline")
    return failures, warnings


def _run_git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(root), *args),
        check=True,
        capture_output=True,
    )
    return completed.stdout.decode("utf-8", errors="surrogateescape")


def git_tracked_paths(root: Path) -> set[str]:
    """Return every path tracked by Git under ``root``."""
    output = _run_git(root, "ls-files", "-z")
    return {entry for entry in output.split("\0") if entry}


def git_changed_paths(root: Path, base: str) -> list[str]:
    """Return paths this branch adds or edits relative to ``base``."""
    output = _run_git(root, "diff", "--name-only", "-z", f"{base}...HEAD")
    return sorted(entry for entry in output.split("\0") if entry)


def git_author_of(root: Path, path: str) -> dict[str, str] | None:
    """Return the commit that first added ``path``, for the traceability record."""
    try:
        output = _run_git(
            root,
            "log",
            "--diff-filter=A",
            "--format=%H%x1f%an%x1f%ae%x1f%aI",
            "-1",
            "--",
            path,
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        return None
    if not output:
        return None
    commit, name, email, authored_at = output.split("\x1f")
    return {"commit": commit, "author_name": name, "author_email": email, "authored_at": authored_at}


def read_zip_members(zip_path: Path) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Return ``(members, rejections)`` for every regular file in the archive."""
    members: list[tuple[str, bytes]] = []
    rejections: list[str] = []
    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ValueError(f"archive holds {len(infos)} entries; refuse above {MAX_MEMBERS}")
        for info in infos:
            if info.is_dir():
                continue
            mode = info.external_attr >> 16
            if mode and (mode & 0o170000) == 0o120000:
                rejections.append(f"{info.filename}: symlink members are not accepted")
                continue
            reason = classify_member_name(info.filename)
            if reason is not None:
                rejections.append(f"{info.filename}: {reason}")
                continue
            if info.file_size > MAX_MEMBER_BYTES:
                rejections.append(f"{info.filename}: {info.file_size} bytes exceeds the member limit")
                continue
            members.append((info.filename, archive.read(info)))
    members.sort(key=lambda item: item[0])
    return members, rejections


def audit_submission(
    *,
    zip_path: Path,
    root: Path,
    tracked: set[str] | None = None,
    changed_paths: list[str] | None = None,
    allow_extra: bool = False,
    with_authors: bool = False,
) -> dict[str, object]:
    """Compare one handover archive against the committed tree under ``root``."""
    zip_bytes = zip_path.read_bytes()
    members, rejections = read_zip_members(zip_path)
    if tracked is None:
        tracked = git_tracked_paths(root)

    failures: list[str] = list(rejections)
    warnings: list[str] = []
    entries: list[dict[str, object]] = []

    for name, data in members:
        entry: dict[str, object] = {
            "path": name,
            "bytes": len(data),
            "zip_sha256": sha256_bytes(data),
            "repo_sha256": None,
            "status": "MISSING",
        }
        target = root / name
        if not target.is_file():
            failures.append(f"{name}: not present in the repository tree")
        elif name not in tracked:
            entry["status"] = "UNTRACKED"
            failures.append(f"{name}: present on disk but not tracked by Git")
        else:
            repo_bytes = target.read_bytes()
            entry["repo_sha256"] = sha256_bytes(repo_bytes)
            if repo_bytes == data:
                entry["status"] = "MATCH"
            elif is_text_path(name) and normalize_text(repo_bytes) == normalize_text(data):
                entry["status"] = "MATCH_NORMALIZED"
                warnings.append(f"{name}: matches only after BOM/CRLF normalisation")
            else:
                entry["status"] = "MISMATCH"
                failures.append(f"{name}: committed content differs from the archive")

        member_failures, member_warnings = hygiene_findings(name, data)
        failures.extend(f"{name}: {item}" for item in member_failures)
        warnings.extend(f"{name}: {item}" for item in member_warnings)
        if with_authors and entry["status"] in PASSING_STATUSES:
            author = git_author_of(root, name)
            if author is not None:
                entry["introduced_by"] = author
        entries.append(entry)

    submitted = {name for name, _ in members}
    extra_changed = [path for path in (changed_paths or []) if path not in submitted]
    if extra_changed and not allow_extra:
        listed = ", ".join(extra_changed[:10])
        more = " ..." if len(extra_changed) > 10 else ""
        failures.append(f"branch changes {len(extra_changed)} path(s) absent from the archive: {listed}{more}")

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "archive": {
            "name": zip_path.name,
            "sha256": sha256_bytes(zip_bytes),
            "bytes": len(zip_bytes),
            "member_count": len(members),
        },
        "root": str(root),
        "entries": entries,
        "extra_changed_paths": extra_changed,
        "warnings": warnings,
        "failures": failures,
        "status": "FAIL" if failures else "PASS",
    }


def render_report(report: dict[str, object]) -> str:
    """Render the audit result as reviewer-readable text."""
    archive = report["archive"]
    assert isinstance(archive, dict)
    lines = [
        "UI submission audit",
        f"  archive     : {archive['name']}",
        f"  sha256      : {archive['sha256']}",
        f"  bytes       : {archive['bytes']}",
        f"  members     : {archive['member_count']}",
        f"  repository  : {report['root']}",
        "",
        "Files",
    ]
    entries = report["entries"]
    assert isinstance(entries, list)
    for entry in entries:
        lines.append(f"  [{entry['status']:>17}] {entry['path']} ({entry['bytes']} bytes)")
        lines.append(f"                      zip  {entry['zip_sha256']}")
        if entry["repo_sha256"]:
            lines.append(f"                      repo {entry['repo_sha256']}")
        introduced = entry.get("introduced_by")
        if isinstance(introduced, dict):
            lines.append(
                f"                      added by {introduced['author_name']} <{introduced['author_email']}>"
                f" in {introduced['commit'][:12]} at {introduced['authored_at']}"
            )
    warnings = report["warnings"]
    assert isinstance(warnings, list)
    if warnings:
        lines.extend(["", "Warnings"])
        lines.extend(f"  - {warning}" for warning in warnings)
    failures = report["failures"]
    assert isinstance(failures, list)
    if failures:
        lines.extend(["", "Failures"])
        lines.extend(f"  - {failure}" for failure in failures)
    lines.extend(["", f"Result: {report['status']}"])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--zip", required=True, type=Path, help="handover archive to audit")
    parser.add_argument("--root", type=Path, default=Path("."), help="repository root (default: .)")
    parser.add_argument(
        "--base",
        help="base revision; every path the branch changes must also be in the archive",
    )
    parser.add_argument(
        "--allow-extra",
        action="store_true",
        help="report but do not fail on branch changes absent from the archive",
    )
    parser.add_argument(
        "--with-authors",
        action="store_true",
        help="record the commit and author that first added each matched path",
    )
    parser.add_argument("--manifest", type=Path, help="write the JSON audit record to this path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    try:
        changed_paths = git_changed_paths(root, args.base) if args.base else None
        report = audit_submission(
            zip_path=args.zip,
            root=root,
            changed_paths=changed_paths,
            allow_extra=args.allow_extra,
            with_authors=args.with_authors,
        )
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", errors="replace").strip() if error.stderr else ""
        print(f"cannot read Git state under {root}: {detail}", file=sys.stderr)
        print("run this audit inside a clone that already contains the submitted commits", file=sys.stderr)
        return 2
    print(render_report(report))
    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nAudit record written to {args.manifest}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
