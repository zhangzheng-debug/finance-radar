#!/usr/bin/env python3
"""Render the durable historical adjudication ledger as readable UTF-8 Markdown."""

from __future__ import annotations

import csv
from collections import Counter
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "reports" / "active_event_adjudications.csv"
REPORT_PATH = ROOT / "reports" / "active_event_adjudications.md"


def read_rows() -> list[dict[str, str]]:
    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    rows = read_rows()
    status = Counter(row["label_status"] for row in rows)
    grades = Counter(row["manual_grade"] for row in rows if row["label_status"] == "verified")
    lines = [
        "# 主动历史事件证据裁决",
        "",
        f"日期：`{date.today().isoformat()}`",
        "",
        f"- 已裁决：`{len(rows)}`",
        f"- Verified：`{status['verified']}`；Rejected controls：`{status['rejected']}`",
        f"- 等级：S `{grades['S']}`、A++ `{grades['A++']}`、A `{grades['A']}`、B `{grades['B']}`、C `{grades['C']}`",
        "- 边界：不直接修改 `D:\\short`，不自动启用训练，不使用事后收益定级，不产生交易动作。",
        "",
        "## 最近裁决",
        "",
        "以下内容来自显式人工复核配置；自动抓取和候选排序本身无权改变标签。",
        "",
    ]
    for row in rows[-10:]:
        lines.extend(
            [
                f"### {row['ticker_at_event']} / {row['manual_grade']} / {row['canonical_event_type']}",
                "",
                row["evidence_summary"],
                "",
                f"- 裁决：{row['adjudication_note']}",
                f"- 一手证据：[{row['evidence_form'] or 'primary source'}]({row['evidence_url']})",
                f"- 训练角色：`{row['training_role']}`；状态：`{row['label_status']}`",
                "",
            ]
        )
    lines.extend(
        [
            "## 完整记录",
            "",
            "逐行证据、R/L/E/C/P/X、分数和裁决理由见 `active_event_adjudications.csv`。",
            "",
        ]
    )
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"rows={len(rows)} verified={status['verified']} rejected={status['rejected']}")
    print(REPORT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
