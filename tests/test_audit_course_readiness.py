from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.audit_course_readiness import audit, safe_evidence_file


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _product_reports(
    root: Path,
    runtime_pass: bool = True,
    *,
    acceptance_count: int = 15,
    acceptance_failure: bool = False,
) -> None:
    _write_json(root / ".deploy_context.json", {"release": "test-release"})
    acceptance_checks = {f"check_{index}": True for index in range(acceptance_count)}
    if acceptance_failure and acceptance_checks:
        acceptance_checks["check_0"] = False
    _write_json(
        root / "reports/product_acceptance_live_latest.json",
        {"passed": not acceptance_failure, "checks": acceptance_checks},
    )
    _write_json(root / "reports/migration_full_restore_latest.json", {"status": "PASS"})
    _write_json(
        root / "reports/runtime_evidence/runtime_gate_latest.json",
        {"status": "PASS" if runtime_pass else "WAITING"},
    )
    _write_json(
        root / "artifacts/risk_router_external_blind_v1_report.json",
        {"gate_pass": False, "promotion_decision": "REMAIN_SHADOW"},
    )
    _write_json(
        root / "reports/risk_label_contract_v3_readiness.json",
        {
            "status": "NOT_READY_FOR_BLIND_V2",
            "production_changed": False,
            "no_blind_v2_claim": True,
        },
    )
    _write_json(
        root / "reports/adjudication_v3_latest.json",
        {
            "status": "NOT_READY_FOR_FREEZE",
            "samples": 24,
            "invalid_annotations": [],
            "reviewer_inputs_target_label": False,
            "peer_answers_hidden_during_independent_review": True,
            "model_and_market_outcomes_hidden": True,
            "source_used_as_label": False,
            "public_review_ui_default_closed": True,
            "split": "UNASSIGNED",
            "production_changed": False,
            "blind_v2_frozen": False,
        },
    )
    _write_json(
        root / "reports/adjudication_v3_public_acceptance.json",
        {
            "status": "PASS",
            "passed": 11,
            "total": 11,
            "checks": {f"check_{index}": True for index in range(11)},
            "boundaries": {
                "admin_token_used": False,
                "review_submitted": False,
                "trading_system_touched": False,
            },
        },
    )
    (root / "financial_event_radar_project_proposal_v5_1_human.docx").write_bytes(b"docx")
    (root / "financial_event_radar_project_plan_v5_1_ai.md").write_text("plan", encoding="utf-8")
    deck_dir = root / "artifacts/defense_deck"
    deck_dir.mkdir(parents=True, exist_ok=True)
    (deck_dir / "finance-radar-defense-deck-v1.pptx").write_bytes(b"pptx")
    (deck_dir / "README.md").write_text("rendered and reviewed", encoding="utf-8")
    ui = root / "reports/ui_qa"
    ui.mkdir(parents=True, exist_ok=True)
    for name in (
        "home_1366x768.png",
        "event_intelligence_1366x768.png",
        "replay_lab_1366x768.png",
        "operations_model_external_blind_1366x768.png",
        "operations_sources_new_feeds_1366x900.png",
        "event_intelligence_mobile_390x844.png",
    ):
        (ui / name).write_bytes(b"png")
    current_ui = root / "reports/ui_qa_20260719"
    current_ui.mkdir(parents=True, exist_ok=True)
    for name in (
        "home_1920x1080.png",
        "event_keyboard_after_jk_1920x1080.png",
        "replay_completed_1920x1080.png",
        "operations_model_1920x1080.png",
    ):
        (current_ui / name).write_bytes(b"png")
    _write_json(
        current_ui / "public_interaction_acceptance.json",
        {
            "release": "test-release",
            "result": "PASS",
            "checks": [{"name": f"check_{index}", "passed": True} for index in range(6)],
            "console_errors": [],
            "page_errors": [],
            "http_errors": [],
        },
    )


def test_browser_QA_must_match_current_release(tmp_path: Path) -> None:
    _product_reports(tmp_path)
    interaction = tmp_path / "reports/ui_qa_20260719/public_interaction_acceptance.json"
    payload = json.loads(interaction.read_text(encoding="utf-8"))
    payload["release"] = "older-release"
    _write_json(interaction, payload)
    manifest = tmp_path / "config/course_evidence_manifest.json"
    _write_json(manifest, {"teacher_approval": {}, "members": [], "forbidden_zones": []})

    report = audit(tmp_path, manifest, commit_checker=lambda *_: False, commit_counter=lambda _: 0)

    assert report["product_checks"]["public_1920_interaction_QA_baseline_pass"] is True
    assert report["product_checks"]["current_release_browser_QA_pass"] is False
    assert "current_release_browser_QA_pass" in report["missing_product_evidence"]


def test_safe_evidence_file_rejects_workspace_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-evidence.txt"
    outside.write_text("evidence", encoding="utf-8")
    assert safe_evidence_file(tmp_path, "../outside-evidence.txt") is False


def test_current_empty_course_manifest_stays_not_ready(tmp_path: Path) -> None:
    _product_reports(tmp_path, runtime_pass=False)
    manifest = tmp_path / "config/course_evidence_manifest.json"
    _write_json(manifest, {"teacher_approval": {}, "members": [], "forbidden_zones": []})
    report = audit(
        tmp_path,
        manifest,
        commit_checker=lambda *_: False,
        commit_counter=lambda _: 0,
    )
    assert report["status"] == "NOT_READY"
    assert report["product_status"] == "WAITING"
    assert report["course_process_status"] == "WAITING_EXTERNAL"
    assert report["no_fabrication"] is True


def test_expanded_public_acceptance_report_remains_forward_compatible(tmp_path: Path) -> None:
    _product_reports(tmp_path, acceptance_count=18)
    manifest = tmp_path / "config/course_evidence_manifest.json"
    _write_json(manifest, {"teacher_approval": {}, "members": [], "forbidden_zones": []})
    report = audit(
        tmp_path,
        manifest,
        commit_checker=lambda *_: False,
        commit_counter=lambda _: 0,
    )
    assert report["product_checks"]["public_acceptance_current_report_all_checks_pass"] is True


def test_public_acceptance_requires_minimum_count_and_every_check(tmp_path: Path) -> None:
    manifest = tmp_path / "config/course_evidence_manifest.json"
    _write_json(manifest, {"teacher_approval": {}, "members": [], "forbidden_zones": []})

    _product_reports(tmp_path, acceptance_count=14)
    short_report = audit(tmp_path, manifest, commit_checker=lambda *_: False, commit_counter=lambda _: 0)
    assert short_report["product_checks"]["public_acceptance_current_report_all_checks_pass"] is False

    _product_reports(tmp_path, acceptance_count=18, acceptance_failure=True)
    failed_report = audit(tmp_path, manifest, commit_checker=lambda *_: False, commit_counter=lambda _: 0)
    assert failed_report["product_checks"]["public_acceptance_current_report_all_checks_pass"] is False


def test_complete_real_evidence_manifest_can_pass(tmp_path: Path) -> None:
    _product_reports(tmp_path, runtime_pass=True)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    paths = {}
    for name in ("teacher", "roles", "fz1-design", "fz1-code", "fz1-tests", "fz2-design", "fz2-code", "fz2-tests", "fz3-design", "fz3-code", "fz3-tests", "drill1", "drill2", "drill3", "improv"):
        path = evidence / f"{name}.txt"
        path.write_text(name, encoding="utf-8")
        paths[name] = path.relative_to(tmp_path).as_posix()
    teacher_sha = hashlib.sha256((tmp_path / paths["teacher"]).read_bytes()).hexdigest()
    manifest_payload = {
        "teacher_approval": {
            "evidence_path": paths["teacher"],
            "sha256": teacher_sha,
            "approved_high_difficulty": True,
            "approved_fz3": True,
        },
        "role_matrix_path": paths["roles"],
        "members": [
            {"name": "Student A", "role": "Lead", "answer_scope": "Evidence gate"},
            {"name": "Student B", "role": "Reviewer", "answer_scope": "Finality gate"},
        ],
        "forbidden_zones": [
            {
                "id": f"FZ{index}",
                "owner": "Student A",
                "reviewer": "Student B",
                "design_path": paths[f"fz{index}-design"],
                "implementation_path": paths[f"fz{index}-code"],
                "tests_path": paths[f"fz{index}-tests"],
                "first_student_commit": "abcdef1",
                "final_student_commit": "abcdef2",
            }
            for index in range(1, 4)
        ],
        "timed_drills": [
            {
                "member": member,
                "report_path": paths[f"drill{index}"],
                "failing_test_commit": "abcdef3",
                "fix_commit": "abcdef4",
                "started_at": "2026-07-20T00:00:00Z",
                "finished_at": "2026-07-20T00:20:00Z",
            }
            for member in ("Student A", "Student B")
            for index in range(1, 4)
        ],
        "improvised_changes": [
            {"member": member, "report_path": paths["improv"], "commit": "abcdef5"}
            for member in ("Student A", "Student B")
        ],
    }
    manifest = tmp_path / "config/course_evidence_manifest.json"
    _write_json(manifest, manifest_payload)

    report = audit(
        tmp_path,
        manifest,
        commit_checker=lambda *_: True,
        commit_counter=lambda _: 10,
    )
    assert report["status"] == "READY"
    assert report["course_process_status"] == "PASS"
