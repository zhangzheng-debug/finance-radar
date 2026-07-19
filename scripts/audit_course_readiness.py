#!/usr/bin/env python3
"""Audit engineering and authentic course-process evidence without fabrication."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FORBIDDEN_ZONES = {"FZ1", "FZ2", "FZ3"}
MIN_PUBLIC_ACCEPTANCE_CHECKS = 15

PRODUCT_NEXT_ACTIONS = {
    "runtime_24h_hash_chain_pass": "等待服务端窗口自然达到24小时后重新运行 capture_runtime_evidence.py。",
    "current_release_browser_QA_pass": "用真实浏览器对当前release重跑大屏/桌面/移动、键盘、Replay和可访问性矩阵，并在报告写入release字段。",
}

COURSE_NEXT_ACTIONS = {
    "teacher_approval_evidence": "保存教师签字扫描件或原始回复，计算SHA-256后写入manifest。",
    "teacher_approved_high_difficulty": "教师明确确认自主高难度选题后才设为true。",
    "teacher_approved_FZ3": "教师明确批准finality_gate或指定替代项后才设为true。",
    "role_matrix_evidence": "由学生填写角色/代码/测试/评审/答辩责任矩阵并保存真实路径。",
    "member_records_complete": "在manifest登记每位真实成员、角色和答辩责任范围。",
    "three_forbidden_zone_file_sets": "每个禁飞区补齐学生设计、实现与测试三个真实文件。",
    "forbidden_zone_ownership_and_review": "每个禁飞区指定不同的负责人和复核人。",
    "forbidden_zone_student_commits": "填写每个禁飞区真实首提交和最终提交，禁止倒签AI历史。",
    "three_timed_drills_per_member": "每人完成三次计时Bug练习，保存失败提交、修复提交、起止时间和报告。",
    "one_improvised_change_per_member": "每人完成一次即兴修改并保存报告与真实提交。",
    "repository_has_real_commits": "学生审阅边界后开始真实、小步、可解释的Git提交。",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def safe_evidence_file(root: Path, value: Any, expected_sha256: str | None = None) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return False
    if not candidate.is_file() or candidate.stat().st_size == 0:
        return False
    if expected_sha256:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
            return False
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual != expected_sha256.lower():
            return False
    return True


def git_commit_exists(root: Path, commit_id: Any) -> bool:
    if not isinstance(commit_id, str) or not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit_id):
        return False
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit_id}^{{commit}}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def git_commit_count(root: Path) -> int:
    result = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return int(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip().isdigit() else 0


def product_checks(root: Path) -> dict[str, bool]:
    acceptance = load_json(root / "reports/product_acceptance_live_latest.json")
    migration = load_json(root / "reports/migration_full_restore_latest.json")
    runtime = load_json(root / "reports/runtime_evidence/runtime_gate_latest.json")
    blind = load_json(root / "artifacts/risk_router_external_blind_v1_report.json")
    label_v3 = load_json(root / "reports/risk_label_contract_v3_readiness.json")
    adjudication_v3 = load_json(root / "reports/adjudication_v3_latest.json")
    adjudication_public = load_json(root / "reports/adjudication_v3_public_acceptance.json")
    interactions = load_json(root / "reports/ui_qa_20260719/public_interaction_acceptance.json")
    deploy_context = load_json(root / ".deploy_context.json")
    current_release = str(deploy_context.get("release") or "").strip()
    acceptance_checks = acceptance.get("checks") or {}
    interaction_checks = interactions.get("checks") or []
    return {
        "public_acceptance_current_report_all_checks_pass": (
            acceptance.get("passed") is True
            and len(acceptance_checks) >= MIN_PUBLIC_ACCEPTANCE_CHECKS
            and all(value is True for value in acceptance_checks.values())
        ),
        "full_encrypted_migration_restore": migration.get("status") == "PASS",
        "runtime_24h_hash_chain_pass": runtime.get("status") == "PASS",
        "external_blind_failure_disclosed_and_shadow_blocked": (
            blind.get("gate_pass") is False
            and blind.get("promotion_decision") == "REMAIN_SHADOW"
        ),
        "risk_label_v3_invalid_data_blocked": (
            label_v3.get("status") == "NOT_READY_FOR_BLIND_V2"
            and label_v3.get("production_changed") is False
            and label_v3.get("no_blind_v2_claim") is True
        ),
        "v3_dual_review_workflow_operational": (
            adjudication_v3.get("status") in {
                "NOT_READY_FOR_FREEZE",
                "READY_FOR_OVERLAP_AUDIT",
            }
            and int(adjudication_v3.get("samples") or 0) >= 24
            and adjudication_v3.get("invalid_annotations") == []
            and adjudication_v3.get("reviewer_inputs_target_label") is False
            and adjudication_v3.get("peer_answers_hidden_during_independent_review") is True
            and adjudication_v3.get("model_and_market_outcomes_hidden") is True
            and adjudication_v3.get("source_used_as_label") is False
            and adjudication_v3.get("public_review_ui_default_closed") is True
            and adjudication_v3.get("split") == "UNASSIGNED"
            and adjudication_v3.get("production_changed") is False
            and adjudication_v3.get("blind_v2_frozen") is False
        ),
        "v3_public_readonly_boundary_pass": (
            adjudication_public.get("status") == "PASS"
            and adjudication_public.get("passed") == 11
            and adjudication_public.get("total") == 11
            and all((adjudication_public.get("checks") or {}).values())
            and (adjudication_public.get("boundaries") or {}).get("admin_token_used") is False
            and (adjudication_public.get("boundaries") or {}).get("review_submitted") is False
            and (adjudication_public.get("boundaries") or {}).get("trading_system_touched") is False
        ),
        "human_taskbook_present": (root / "financial_event_radar_project_proposal_v5_1_human.docx").is_file(),
        "ai_spec_present": (root / "financial_event_radar_project_plan_v5_1_ai.md").is_file(),
        "rendered_defense_deck_present": (
            (root / "artifacts/defense_deck/finance-radar-defense-deck-v1.pptx").is_file()
            and (root / "artifacts/defense_deck/finance-radar-defense-deck-v1.pptx").stat().st_size > 0
            and (root / "artifacts/defense_deck/README.md").is_file()
        ),
        "browser_QA_baseline_present": all(
            (root / "reports/ui_qa" / name).is_file()
            for name in (
                "home_1366x768.png",
                "event_intelligence_1366x768.png",
                "replay_lab_1366x768.png",
                "operations_model_external_blind_1366x768.png",
                "operations_sources_new_feeds_1366x900.png",
                "event_intelligence_mobile_390x844.png",
            )
        ),
        "public_1920_interaction_QA_baseline_pass": (
            interactions.get("result") == "PASS"
            and len(interaction_checks) == 6
            and all(item.get("passed") is True for item in interaction_checks)
            and interactions.get("page_errors") == []
            and all(
                (root / "reports/ui_qa_20260719" / name).is_file()
                for name in (
                    "home_1920x1080.png",
                    "event_keyboard_after_jk_1920x1080.png",
                    "replay_completed_1920x1080.png",
                    "operations_model_1920x1080.png",
                )
            )
        ),
        "current_release_browser_QA_pass": (
            bool(current_release)
            and interactions.get("release") == current_release
            and interactions.get("result") == "PASS"
            and len(interaction_checks) == 6
            and all(item.get("passed") is True for item in interaction_checks)
            and interactions.get("page_errors") == []
            and interactions.get("console_errors") == []
            and interactions.get("http_errors") == []
        ),
    }


def course_checks(
    root: Path,
    manifest: dict[str, Any],
    *,
    commit_checker: Callable[[Path, Any], bool] = git_commit_exists,
    commit_counter: Callable[[Path], int] = git_commit_count,
) -> tuple[dict[str, bool], list[str]]:
    reasons: list[str] = []
    teacher = manifest.get("teacher_approval") or {}
    teacher_file = safe_evidence_file(root, teacher.get("evidence_path"), teacher.get("sha256"))
    members = manifest.get("members") or []
    member_names = {
        str(member.get("name") or "").strip()
        for member in members
        if isinstance(member, dict) and str(member.get("name") or "").strip()
    }
    role_matrix = safe_evidence_file(root, manifest.get("role_matrix_path"))
    member_records_complete = bool(member_names) and len(member_names) == len(members) and all(
        isinstance(member, dict)
        and str(member.get("role") or "").strip()
        and str(member.get("answer_scope") or "").strip()
        for member in members
    )

    zones = manifest.get("forbidden_zones") or []
    zone_by_id = {
        zone.get("id"): zone for zone in zones if isinstance(zone, dict) and zone.get("id")
    }
    zone_files_complete = set(zone_by_id) == REQUIRED_FORBIDDEN_ZONES
    zone_commits_complete = zone_files_complete
    zone_ownership_complete = zone_files_complete
    for zone_id in REQUIRED_FORBIDDEN_ZONES:
        zone = zone_by_id.get(zone_id) or {}
        zone_files_complete = zone_files_complete and all(
            safe_evidence_file(root, zone.get(field))
            for field in ("design_path", "implementation_path", "tests_path")
        )
        zone_commits_complete = zone_commits_complete and all(
            commit_checker(root, zone.get(field))
            for field in ("first_student_commit", "final_student_commit")
        )
        zone_ownership_complete = zone_ownership_complete and (
            zone.get("owner") in member_names
            and zone.get("reviewer") in member_names
            and zone.get("owner") != zone.get("reviewer")
        )

    drills = [item for item in (manifest.get("timed_drills") or []) if isinstance(item, dict)]
    drill_counts = Counter(item.get("member") for item in drills if item.get("member") in member_names)
    drills_complete = bool(member_names) and all(drill_counts[name] >= 3 for name in member_names) and all(
        safe_evidence_file(root, item.get("report_path"))
        and commit_checker(root, item.get("failing_test_commit"))
        and commit_checker(root, item.get("fix_commit"))
        and item.get("started_at")
        and item.get("finished_at")
        for item in drills
    )
    improvised = [item for item in (manifest.get("improvised_changes") or []) if isinstance(item, dict)]
    improvised_counts = Counter(
        item.get("member") for item in improvised if item.get("member") in member_names
    )
    improvised_complete = bool(member_names) and all(
        improvised_counts[name] >= 1 for name in member_names
    ) and all(
        safe_evidence_file(root, item.get("report_path"))
        and commit_checker(root, item.get("commit"))
        for item in improvised
    )

    checks = {
        "teacher_approval_evidence": teacher_file,
        "teacher_approved_high_difficulty": teacher.get("approved_high_difficulty") is True,
        "teacher_approved_FZ3": teacher.get("approved_fz3") is True,
        "role_matrix_evidence": role_matrix,
        "member_records_complete": member_records_complete,
        "three_forbidden_zone_file_sets": zone_files_complete,
        "forbidden_zone_ownership_and_review": zone_ownership_complete,
        "forbidden_zone_student_commits": zone_commits_complete,
        "three_timed_drills_per_member": drills_complete,
        "one_improvised_change_per_member": improvised_complete,
        "repository_has_real_commits": commit_counter(root) > 0,
    }
    for name, passed in checks.items():
        if not passed:
            reasons.append(name)
    return checks, reasons


def audit(
    root: Path,
    manifest_path: Path,
    *,
    commit_checker: Callable[[Path, Any], bool] = git_commit_exists,
    commit_counter: Callable[[Path], int] = git_commit_count,
) -> dict[str, Any]:
    root = root.resolve()
    manifest = load_json(manifest_path)
    products = product_checks(root)
    course, missing = course_checks(
        root,
        manifest,
        commit_checker=commit_checker,
        commit_counter=commit_counter,
    )
    product_ready = all(products.values())
    course_ready = all(course.values())
    missing_product = [name for name, passed in products.items() if not passed]
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "status": "READY" if product_ready and course_ready else "NOT_READY",
        "product_status": "PASS" if product_ready else "WAITING",
        "course_process_status": "PASS" if course_ready else "WAITING_EXTERNAL",
        "product_checks": products,
        "course_checks": course,
        "missing_product_evidence": missing_product,
        "missing_course_evidence": missing,
        "no_fabrication": True,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Finance Radar course readiness audit",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Overall: **{report['status']}**",
        f"- Engineering product: **{report['product_status']}**",
        f"- Authentic course process: **{report['course_process_status']}**",
        "",
        "## Engineering evidence",
        "",
    ]
    lines.extend(["| 状态 | 工程门禁 | 下一步 |", "|---|---|---|"])
    lines.extend(
        f"| {'PASS' if passed else 'WAITING'} | `{name}` | "
        f"{'' if passed else PRODUCT_NEXT_ACTIONS.get(name, '检查对应报告并重新生成可信证据。')} |"
        for name, passed in report["product_checks"].items()
    )
    lines.extend(["", "## Authentic student / teacher evidence", ""])
    lines.extend(["| 状态 | 课程门禁 | 下一步 |", "|---|---|---|"])
    lines.extend(
        f"| {'PASS' if passed else 'WAITING'} | `{name}` | "
        f"{'' if passed else COURSE_NEXT_ACTIONS.get(name, '补充真实学生或教师证据。')} |"
        for name, passed in report["course_checks"].items()
    )
    lines.extend(
        [
            "",
            "This audit deliberately refuses to infer teacher approval, student authorship, Git history or timed performance from AI-generated files. Fill `config/course_evidence_manifest.json` only with real evidence paths and commit IDs, then re-run the audit.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "config/course_evidence_manifest.json")
    parser.add_argument("--json", type=Path, default=ROOT / "reports/course_readiness_latest.json")
    parser.add_argument("--markdown", type=Path, default=ROOT / "reports/course_readiness_latest.md")
    parser.add_argument(
        "--require-product-ready",
        action="store_true",
        help="return a non-zero exit code unless every engineering product gate passes",
    )
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="return a non-zero exit code unless engineering and authentic course gates all pass",
    )
    args = parser.parse_args()
    report = audit(ROOT, args.manifest.resolve())
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.require_ready and report["status"] != "READY":
        return 2
    if args.require_product_ready and report["product_status"] != "PASS":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
