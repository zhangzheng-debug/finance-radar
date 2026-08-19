from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.generate_reviewer_principals import generate


def test_generate_distinct_reviewer_and_arbiter_credentials(tmp_path: Path) -> None:
    output = tmp_path / "reviewer-principals.json"
    result = generate(output, ["reviewer-a", "reviewer-b"], "arbiter-c")
    rows = json.loads(output.read_text(encoding="utf-8"))

    assert result == {
        "output": str(output.resolve()),
        "principals": 3,
        "reviewers": 2,
        "arbiters": 1,
        "secrets_printed": False,
    }
    assert [row["role"] for row in rows] == ["REVIEWER", "REVIEWER", "ARBITER"]
    assert len({row["token"] for row in rows}) == 3
    assert all(len(row["token"]) >= 24 for row in rows)


def test_generator_refuses_overwrite_and_shared_identity(tmp_path: Path) -> None:
    output = tmp_path / "reviewer-principals.json"
    generate(output, ["reviewer-a", "reviewer-b"], "arbiter-c")
    with pytest.raises(FileExistsError):
        generate(output, ["reviewer-x", "reviewer-y"], "arbiter-z")
    with pytest.raises(ValueError, match="must be unique"):
        generate(tmp_path / "other.json", ["same", "reviewer-b"], "SAME")
