#!/usr/bin/env python3
"""Export a read-only historical event-quality recovery plan."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.event_quality_recovery import build_recovery_plan, stable_json


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _markdown(plan: dict) -> str:
    lines = [
        "# 历史事件质量恢复计划（只读）",
        "",
        f"- 合同：`{plan['contract_version']}`",
        f"- 源事件：**{plan['source_event_count']:,}**",
        f"- 逻辑快照：`{plan['logical_snapshot_sha256']}`",
        f"- 分桶完整：**{plan['partition_complete']}**",
        "- 本报告没有修改正式事件、标签或证据。",
        "",
        "## 互斥分桶",
        "",
        "| 分桶 | 数量 | 后续动作 |",
        "|---|---:|---|",
    ]
    actions = {
        record["bucket"]: record["proposed_action"] for record in plan["records"]
    }
    for bucket, count in plan["bucket_counts"].items():
        lines.append(f"| `{bucket}` | {count:,} | `{actions.get(bucket, 'NO_ACTION')}` |")
    lines.extend(
        [
            "",
            "## 执行边界",
            "",
            "任何后续写入必须逐条重新核对 event_version、evidence_fingerprint 与 "
            "facts_sha256；不一致即 STALE，禁止写入。正式状态变更还需要独立、动作级授权，"
            "并保存变更前快照与追加式审计记录。",
            "",
        ]
    )
    return "\n".join(lines)


def build(ledger: Path, output: Path) -> dict:
    plan = build_recovery_plan(ledger)
    output.mkdir(parents=True, exist_ok=True)
    manifest = {key: value for key, value in plan.items() if key != "records"}
    _write_text(output / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    _write_text(
        output / "recovery_plan.jsonl",
        "".join(stable_json(record) + "\n" for record in plan["records"]),
    )
    _write_text(output / "README.md", _markdown(plan))
    with (output / "bucket_summary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["bucket", "count"])
        writer.writerows(plan["bucket_counts"].items())
    files = sorted(path for path in output.iterdir() if path.is_file())
    _write_text(
        output / "SHA256SUMS.txt",
        "".join(f"{_sha256(path)}  {path.name}\n" for path in files),
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = build(args.ledger, args.output)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
