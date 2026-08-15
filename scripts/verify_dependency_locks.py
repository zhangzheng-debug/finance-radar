#!/usr/bin/env python3
"""Verify that dependency inputs and hash-locked outputs remain bound."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
METADATA = ROOT / "dependency-lock.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(root: Path = ROOT) -> dict[str, object]:
    metadata = json.loads((root / METADATA.name).read_text(encoding="utf-8"))
    failures: list[str] = []
    files = metadata.get("files")
    if not isinstance(files, dict) or not files:
        failures.append("dependency-lock.json has no file inventory")
        files = {}
    for relative, expected in files.items():
        path = root / str(relative)
        if not path.is_file():
            failures.append(f"missing dependency file: {relative}")
            continue
        actual = sha256(path)
        if actual != str(expected).lower():
            failures.append(f"hash mismatch: {relative}: expected={expected} actual={actual}")
    for relative in ("requirements.lock", "requirements-dev.lock"):
        path = root / relative
        if path.is_file():
            source = path.read_text(encoding="utf-8")
            if "--hash=sha256:" not in source:
                failures.append(f"lock lacks package hashes: {relative}")
            if ">=" in source or "~=" in source:
                failures.append(f"lock contains an open version constraint: {relative}")
    return {
        "status": "PASS" if not failures else "FAIL",
        "python_version": metadata.get("python_version"),
        "resolver": metadata.get("resolver"),
        "verified_files": len(files) - sum(item.startswith("missing") for item in failures),
        "failures": failures,
    }


def main() -> int:
    report = verify()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
