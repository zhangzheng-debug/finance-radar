#!/usr/bin/env python3
"""Verify that dependency inputs and hash-locked outputs remain bound."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
METADATA = ROOT / "dependency-lock.json"


SCHEMA_VERSION = 2
DIGEST_ALGORITHM = "sha256-canonical-text-v1"
TEXT_NORMALIZATION = "crlf-and-cr-to-lf"


def canonical_text_bytes(path: Path) -> bytes:
    """Return platform-independent bytes without changing semantic content."""

    raw = path.read_bytes()
    return raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def sha256_canonical_text(path: Path) -> str:
    return hashlib.sha256(canonical_text_bytes(path)).hexdigest()


def verify(root: Path = ROOT) -> dict[str, object]:
    metadata = json.loads((root / METADATA.name).read_text(encoding="utf-8"))
    failures: list[str] = []
    if metadata.get("schema_version") != SCHEMA_VERSION:
        failures.append("unsupported dependency lock metadata schema")
    if metadata.get("digest_algorithm") != DIGEST_ALGORITHM:
        failures.append("unsupported dependency lock digest algorithm")
    if metadata.get("text_normalization") != TEXT_NORMALIZATION:
        failures.append("unsupported dependency lock text normalization")
    files = metadata.get("files")
    if not isinstance(files, dict) or not files:
        failures.append("dependency-lock.json has no file inventory")
        files = {}
    for relative, expected in files.items():
        path = root / str(relative)
        if not path.is_file():
            failures.append(f"missing dependency file: {relative}")
            continue
        actual = sha256_canonical_text(path)
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
