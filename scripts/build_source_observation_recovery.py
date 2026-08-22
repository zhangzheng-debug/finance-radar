#!/usr/bin/env python3
"""Export the read-only capture-to-evidence recovery inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.source_observation_recovery import (
    build_source_observation_recovery_plan,
    stable_json,
)


def _write(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8", newline="\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(ledger: Path, output: Path) -> dict:
    plan = build_source_observation_recovery_plan(ledger)
    output.mkdir(parents=True, exist_ok=True)
    manifest = {key: value for key, value in plan.items() if key != "records"}
    _write(output / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    _write(
        output / "recovery_records.jsonl",
        "".join(stable_json(record) + "\n" for record in plan["records"]),
    )
    lines = [
        "# 采集来源恢复计划",
        "",
        f"- 零证据事件：**{plan['zero_evidence_event_count']:,}**",
        f"- 无事件投影的保留采集：**{plan['orphan_capture_count']:,}**",
        f"- 分桶完整：**{plan['partition_complete']}**",
        f"- 逻辑快照：`{plan['logical_snapshot_sha256']}`",
        "- 本计划未联网、未创建证据、未改变事件状态。",
        "",
        "## 分桶",
        "",
        "| 分桶 | 数量 |",
        "|---|---:|",
        *[
            f"| `{bucket}` | {count:,} |"
            for bucket, count in plan["bucket_counts"].items()
        ],
        "",
        "采集记录只回答系统当时收到了什么。只有重新取得可定位的 P0/P1 原文段落，"
        "并通过主体、动作、日期和当前版本关系门，才能进入 event_evidence。",
        "",
    ]
    _write(output / "README.md", "\n".join(lines))
    files = sorted(
        path
        for path in output.iterdir()
        if path.is_file() and path.name != "SHA256SUMS.txt"
    )
    _write(
        output / "SHA256SUMS.txt",
        "".join(f"{_sha256(path)}  {path.name}\n" for path in files),
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.ledger, args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
