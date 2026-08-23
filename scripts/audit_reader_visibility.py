#!/usr/bin/env python3
"""Explain why canonical events are or are not visible to the public reader.

The public event list forces ``reader_ready=True``.  That flag is the AND of
several independent conditions, so an empty public page has many possible
causes and the aggregate count alone does not say which one is responsible.

This audit decomposes the flag, reports how many events fail on each condition,
and separates the backlog into the part a deterministic enricher can recover on
its own and the part that needs a human.  It opens the database read-only and
never writes to it, so it is safe to run against production.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_REPORT = ROOT / "reports" / "reader_visibility_audit_latest.md"

VALID_CLAIM_STAGES = (
    "PROPOSED",
    "FILED",
    "DISCLOSED",
    "EFFECTIVE",
    "ONGOING",
    "COMPLETED",
)
# Mirrors READER_ALLOWED_EVIDENCE_STATUSES in app/services/event_admission.py.
READER_ALLOWED_EVIDENCE_STATUSES = (
    "machine_extracted_unreviewed",
    "candidate_passage",
    "confirmed_primary",
    "accepted_manual_primary_evidence",
    "accepted_light_primary_evidence",
)
# Sources whose filings a deterministic enricher can parse without a human.
MACHINE_RECOVERABLE_SOURCES = ("sec_current_filings", "sec_litigation_releases")

CONDITION_LABELS = {
    "subject": "有主体（company_name 或 ticker）",
    "evidence": "有可引用证据（近似上界）",
    "summary": "事实摘要 >= 20 字",
    "claim_subject": "claim_subject >= 2 字",
    "claim_action": "claim_action >= 3 字",
    "claim_stage": "claim_stage 在合法取值内",
    "known_at": "known_at 为完整时间戳",
}


def connect_readonly(path: Path) -> sqlite3.Connection:
    """Open the ledger read-only so an audit can never mutate production."""

    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _json_text(column: str, key: str) -> str:
    return f"TRIM(COALESCE(json_extract({column},'$.{key}'),''))"


def _condition_sql() -> str:
    facts = "ev.facts_json"
    stages = ",".join(f"'{stage}'" for stage in VALID_CLAIM_STAGES)
    allowed = ",".join(f"'{status}'" for status in READER_ALLOWED_EVIDENCE_STATUSES)
    summary = (
        "COALESCE("
        f"NULLIF({_json_text(facts, 'public_fact_summary')},''),"
        f"NULLIF({_json_text(facts, 'fact_summary')},''),"
        f"NULLIF({_json_text(facts, 'evidence_summary')},''),"
        "'')"
    )
    return f"""
        SELECT ce.event_id AS event_id,
               COALESCE(NULLIF(TRIM(ce.discovery_source),''),'unknown') AS discovery_source,
               CASE WHEN COALESCE(
                      NULLIF(TRIM(ce.company_name),''),
                      NULLIF(TRIM(ce.ticker_at_event),''),'')!=''
                    THEN 1 ELSE 0 END AS subject,
               CASE WHEN EXISTS (
                      SELECT 1 FROM event_evidence x
                      WHERE x.event_id=ce.event_id
                        AND TRIM(COALESCE(x.evidence_url,''))!=''
                        AND LENGTH(TRIM(COALESCE(x.evidence_passage,'')))>=40
                        AND COALESCE(x.evidence_status,'') IN ({allowed})
                    ) THEN 1 ELSE 0 END AS evidence,
               CASE WHEN json_valid({facts}) AND LENGTH({summary})>=20
                    THEN 1 ELSE 0 END AS summary,
               CASE WHEN json_valid({facts})
                     AND LENGTH({_json_text(facts, 'claim_subject')})>=2
                    THEN 1 ELSE 0 END AS claim_subject,
               CASE WHEN json_valid({facts})
                     AND LENGTH({_json_text(facts, 'claim_action')})>=3
                    THEN 1 ELSE 0 END AS claim_action,
               CASE WHEN json_valid({facts})
                     AND UPPER({_json_text(facts, 'claim_stage')}) IN ({stages})
                    THEN 1 ELSE 0 END AS claim_stage,
               CASE WHEN json_valid({facts})
                     AND LENGTH({_json_text(facts, 'known_at')})>=20
                    THEN 1 ELSE 0 END AS known_at
        FROM canonical_events ce
        LEFT JOIN event_versions ev
          ON ev.event_id=ce.event_id AND ev.version=ce.current_version
    """


def audit(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = [dict(row) for row in connection.execute(_condition_sql())]
    conditions = tuple(CONDITION_LABELS)

    passing = {name: 0 for name in conditions}
    missing_counts: Counter[str] = Counter()
    blocked_by_claim_contract = 0
    machine_recoverable = 0
    needs_human = 0
    visible = 0

    for row in rows:
        missing = tuple(name for name in conditions if not row[name])
        for name in conditions:
            passing[name] += 1 if row[name] else 0
        if not missing:
            visible += 1
            continue
        missing_counts[" + ".join(missing)] += 1
        claim_fields = {"claim_subject", "claim_action", "claim_stage", "known_at"}
        # The interesting backlog is an event that already carries a subject, a
        # citable passage and a summary, and is held back only by the newer
        # claim contract.  Everything else needs evidence work first.
        if set(missing) <= claim_fields and row["subject"] and row["evidence"] and row["summary"]:
            blocked_by_claim_contract += 1
            if row["discovery_source"] in MACHINE_RECOVERABLE_SOURCES:
                machine_recoverable += 1
            else:
                needs_human += 1

    total = len(rows)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "total_events": total,
        "reader_visible": visible,
        "reader_hidden": total - visible,
        "condition_pass_counts": {
            name: {"label": CONDITION_LABELS[name], "passed": passing[name], "failed": total - passing[name]}
            for name in conditions
        },
        "top_blocking_combinations": [
            {"missing": key, "events": count} for key, count in missing_counts.most_common(10)
        ],
        "claim_contract_backlog": {
            "total": blocked_by_claim_contract,
            "machine_recoverable": machine_recoverable,
            "needs_human": needs_human,
            "note": (
                "Events that already have a subject, a citable passage and a summary, "
                "and are held back only by claim_subject/claim_action/claim_stage/known_at."
            ),
        },
        "caveats": [
            "The evidence condition is an upper bound: it checks event_evidence "
            "directly and does not replay the version-bound relation and source "
            "joins that the production reader_ready CTE performs.",
            "reader_visible is therefore an optimistic ceiling, not a promise.",
        ],
    }


def cross_check(result: dict[str, Any], db_path: Path) -> dict[str, Any]:
    """Compare against the repository's own reader_ready when it is available."""

    try:
        import sys

        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from app.storage import LedgerRepository  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - depends on the checkout
        return {"available": False, "reason": f"{type(exc).__name__}"}
    try:
        ledger = LedgerRepository(db_path)
        authoritative = int(ledger.list_events(reader_ready=True, limit=1).get("total") or 0)
    except TypeError:
        return {"available": False, "reason": "this checkout has no reader_ready filter"}
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}"}
    estimate = int(result["reader_visible"])
    return {
        "available": True,
        "authoritative_reader_ready": authoritative,
        "audit_estimate": estimate,
        "estimate_is_upper_bound": estimate >= authoritative,
        "difference": estimate - authoritative,
    }


def render_markdown(result: dict[str, Any]) -> str:
    total = result["total_events"]
    lines = [
        "# 公开可见性诊断",
        "",
        f"- 生成时间：`{result['generated_at']}`",
        f"- 事件总数：**{total:,}**",
        f"- 公开可见（估计上界）：**{result['reader_visible']:,}**",
        f"- 公开不可见：**{result['reader_hidden']:,}**",
        "",
        "## 各条件通过情况",
        "",
        "| 条件 | 通过 | 未通过 |",
        "|---|--:|--:|",
    ]
    for name, entry in result["condition_pass_counts"].items():
        lines.append(f"| {entry['label']} | {entry['passed']:,} | {entry['failed']:,} |")
    backlog = result["claim_contract_backlog"]
    lines += [
        "",
        "## 仅被 claim 契约挡住的积压",
        "",
        f"- 合计：**{backlog['total']:,}**",
        f"- 确定性 enricher 可自动恢复：**{backlog['machine_recoverable']:,}**",
        f"- 需要人工：**{backlog['needs_human']:,}**",
        "",
        "## 最常见的缺失组合",
        "",
        "| 缺失条件 | 事件数 |",
        "|---|--:|",
    ]
    for entry in result["top_blocking_combinations"]:
        lines.append(f"| {entry['missing']} | {entry['events']:,} |")
    check = result.get("cross_check") or {}
    if check.get("available"):
        lines += [
            "",
            "## 与仓库自身 reader_ready 的交叉校验",
            "",
            f"- 权威值：**{check['authoritative_reader_ready']:,}**",
            f"- 本次估计：**{check['audit_estimate']:,}**",
            f"- 估计是否为上界：**{check['estimate_is_upper_bound']}**",
        ]
    lines += ["", "## 说明", ""] + [f"- {item}" for item in result["caveats"]] + [""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--no-cross-check", action="store_true")
    args = parser.parse_args()

    connection = connect_readonly(args.db)
    try:
        result = audit(connection)
    finally:
        connection.close()
    if not args.no_cross_check:
        result["cross_check"] = cross_check(result, args.db)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_markdown(result), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
