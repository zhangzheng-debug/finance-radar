"""Generate distinct human-review credentials without printing the secrets."""

from __future__ import annotations

import argparse
import json
import os
import secrets
from pathlib import Path


def _principal(principal_id: str, role: str) -> dict[str, str]:
    normalized = principal_id.strip()
    if len(normalized) < 3 or len(normalized) > 80:
        raise ValueError("principal IDs must be between 3 and 80 characters")
    return {
        "principal_id": normalized,
        "role": role,
        "token": secrets.token_urlsafe(32),
    }


def generate(output: Path, reviewer_ids: list[str], arbiter_id: str) -> dict[str, object]:
    if len(reviewer_ids) < 2:
        raise ValueError("at least two distinct reviewer IDs are required")
    rows = [*(_principal(item, "REVIEWER") for item in reviewer_ids), _principal(arbiter_id, "ARBITER")]
    normalized_ids = [str(row["principal_id"]).casefold() for row in rows]
    if len(set(normalized_ids)) != len(normalized_ids):
        raise ValueError("reviewer and arbiter IDs must be unique")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(rows, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        output.unlink(missing_ok=True)
        raise
    os.chmod(output, 0o600)
    return {
        "output": str(output),
        "principals": len(rows),
        "reviewers": len(reviewer_ids),
        "arbiters": 1,
        "secrets_printed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reviewer-id", action="append", required=True)
    parser.add_argument("--arbiter-id", required=True)
    args = parser.parse_args()
    result = generate(args.output, args.reviewer_id, args.arbiter_id)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
