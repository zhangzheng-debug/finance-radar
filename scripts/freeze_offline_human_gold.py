#!/usr/bin/env python3
"""Assess and freeze finalized offline human-gold annotations."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.human_gold_freeze import assess_freeze_readiness, stable_json


def _load_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--dataset", type=Path)
    args = parser.parse_args()

    result = assess_freeze_readiness(_load_jsonl(args.annotations))
    report = {key: value for key, value in result.items() if key != "rows"}
    _write(args.report, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if args.dataset:
        if result["status"] != "READY_TO_FREEZE":
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 2
        dataset_text = "".join(stable_json(row) + "\n" for row in result["rows"])
        _write(args.dataset, dataset_text)
        digest = hashlib.sha256(dataset_text.encode("utf-8")).hexdigest()
        _write(args.dataset.with_suffix(args.dataset.suffix + ".sha256"), f"{digest}  {args.dataset.name}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "READY_TO_FREEZE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
