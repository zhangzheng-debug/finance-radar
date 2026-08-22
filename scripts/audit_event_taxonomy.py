#!/usr/bin/env python3
"""Audit current event-family/type coverage under the shared taxonomy."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.event_taxonomy import taxonomy_coverage


def build_report(db_path: Path) -> dict:
    connection = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT event_id,event_family,event_type FROM canonical_events ORDER BY event_id"
            )
        ]
    finally:
        connection.close()
    result = taxonomy_coverage(rows)
    result["status"] = "PASS" if result["coverage_pct"] >= 95.0 else "ATTENTION"
    result["canonical_mutation_attempted"] = False
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = build_report(args.db)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8", newline="\n")
    print(text, end="")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
