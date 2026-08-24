#!/usr/bin/env python3
"""Export a read-only, authorization-ready historical primary re-admission plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.historical_primary_readmission import (
    build_readmission_authorization_template,
    build_readmission_plan,
)
from app.services.event_quality_recovery import stable_json


def _write(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8", newline="\n")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(ledger: Path, output: Path) -> dict:
    plan = build_readmission_plan(ledger)
    output.mkdir(parents=True, exist_ok=False)
    manifest = {key: value for key, value in plan.items() if key != "records"}
    _write(output / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    _write(
        output / "readmission_plan.jsonl",
        "".join(stable_json(record) + "\n" for record in plan["records"]),
    )
    _write(
        output / "authorization_template.json",
        json.dumps(
            build_readmission_authorization_template(plan),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    _write(
        output / "README.md",
        "\n".join(
            (
                "# 历史一手证据重接纳计划",
                "",
                f"- 扫描有 P0/P1 原文的历史候选：**{plan['enrichable_primary_scanned']:,}**",
                f"- 当前规则可无猜测重放：**{plan['candidate_count']:,}**",
                "- 只创建新的 candidate/weak 版本；不改为已核验，不改标签，不触发交易。",
                "- 执行前必须冻结写入、制作同一逻辑快照备份、填写授权并显式 --apply。",
                "",
            )
        ),
    )
    files = sorted(path for path in output.iterdir() if path.is_file())
    _write(
        output / "SHA256SUMS.txt",
        "".join(f"{_sha(path)}  {path.name}\n" for path in files),
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
