from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.api.main import create_app
from app.config import Settings
from app.models import RiskRouter
from app.ops.backup import verify_restore
from app.services import ReplayService
from app.storage import OperationsRepository
from official_event_collector import parse_rss, parse_xml_root


def run_drill(name: str, action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        evidence = action()
        return {"name": name, "passed": True, "evidence": evidence}
    except Exception as exc:
        return {"name": name, "passed": False, "error": f"{type(exc).__name__}: {exc}"}


def malformed_xml_drill() -> dict[str, Any]:
    body = b"<?xml version='1.0'?><rss><channel><item><title>SEC & markets</title><link>https://sec.example/item</link></item></channel></rss>"
    _root, repaired = parse_xml_root(body)
    entries = parse_rss(body)
    if not repaired or entries[0]["title"] != "SEC & markets":
        raise AssertionError("malformed XML repair did not preserve the title")
    return {"injected_fault": "bare_ampersand", "xml_repaired": repaired, "items": len(entries)}


def corrupt_model_drill(temp_root: Path) -> dict[str, Any]:
    artifact = temp_root / "corrupt.joblib"
    artifact.write_bytes(b"not-a-joblib-model")
    router = RiskRouter(artifact)
    status = router.status()
    prediction = router.predict("The issuer filed a voluntary Chapter 11 bankruptcy petition.")
    if not status["load_error"] or prediction["runtime"] != "fallback" or not prediction["no_trading"]:
        raise AssertionError("corrupt model did not degrade visibly and safely")
    return {
        "load_error_visible": True,
        "runtime": prediction["runtime"],
        "label": prediction["label"],
        "no_trading": prediction["no_trading"],
    }


def corrupt_backup_drill(temp_root: Path) -> dict[str, Any]:
    backup = temp_root / "corrupt.sqlite3"
    backup.write_bytes(b"this is not sqlite")
    try:
        verify_restore(backup)
    except sqlite3.DatabaseError as exc:
        return {"corruption_detected": True, "error_type": type(exc).__name__}
    raise AssertionError("corrupt backup unexpectedly passed restore verification")


def replay_gate_drill(temp_root: Path) -> dict[str, Any]:
    operations = OperationsRepository(temp_root / "ops.sqlite3")
    router = RiskRouter(ROOT / "artifacts" / "risk_router.joblib")
    replay = ReplayService(ROOT / "replay" / "cases", router, operations)
    result = replay.run("sec_bankruptcy_verified")
    first, second = result["steps"]
    if first["shadow_decision"]["label"] != "ABSTAIN" or first["shadow_decision"]["alert_eligible"]:
        raise AssertionError("discovery-only step escaped the evidence gate")
    if second["shadow_decision"]["label"] != "RISK_REVIEW" or not second["shadow_decision"]["alert_eligible"]:
        raise AssertionError("P0-supported step did not become alert-eligible")
    return {
        "step_1": {"label": "ABSTAIN", "alert_eligible": False},
        "step_2": {"label": "RISK_REVIEW", "alert_eligible": True},
        "persisted_runs": len(operations.replay_runs()),
        "external_network_used": result["external_network_used"],
    }


def revision_withdrawal_drill(temp_root: Path) -> dict[str, Any]:
    operations = OperationsRepository(temp_root / "revision-ops.sqlite3")
    router = RiskRouter(ROOT / "artifacts" / "risk_router.joblib")
    replay = ReplayService(ROOT / "replay" / "cases", router, operations)
    result = replay.run("sec_filing_corrected_abstain")
    first, correction = result["steps"]
    if not first["shadow_decision"]["alert_eligible"]:
        raise AssertionError("initial P0 filing did not become alert-eligible")
    if correction["observation"].get("revision_kind") != "CORRECTION":
        raise AssertionError("superseding observation is not marked as a correction")
    if correction["evidence_state"]["status"] != "CONFLICT_REVIEW":
        raise AssertionError("official correction did not create a conflict review state")
    if correction["shadow_decision"]["label"] != "ABSTAIN" or correction["shadow_decision"]["alert_eligible"]:
        raise AssertionError("official correction did not withdraw alert eligibility")
    return {
        "step_1": {"label": first["shadow_decision"]["label"], "alert_eligible": True},
        "step_2": {
            "revision_kind": "CORRECTION",
            "supersedes_step": correction["observation"].get("supersedes_step"),
            "evidence_status": "CONFLICT_REVIEW",
            "label": "ABSTAIN",
            "alert_eligible": False,
        },
        "persisted_runs": len(operations.replay_runs()),
        "external_network_used": result["external_network_used"],
    }


def forbidden_route_drill(temp_root: Path) -> dict[str, Any]:
    settings = Settings(
        ledger_db=temp_root / "unused-ledger.sqlite3",
        operations_db=temp_root / "route-ops.sqlite3",
        artifact_dir=temp_root,
        replay_dir=ROOT / "replay" / "cases",
    )
    paths = sorted(route.path for route in create_app(settings).routes)
    forbidden_terms = ("orders", "positions", "balances", "brokerage", "trade_execution")
    violations = [path for path in paths if any(term in path.lower() for term in forbidden_terms)]
    if violations:
        raise AssertionError(f"forbidden routes found: {violations}")
    return {"route_count": len(paths), "forbidden_routes": violations}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run safe, deterministic defense fault-injection drills.")
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "defense_drills_latest.json")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="finance-radar-drills-") as temp_dir:
        temp_root = Path(temp_dir)
        drills = [
            run_drill("malformed_official_xml", malformed_xml_drill),
            run_drill("corrupt_model_artifact", lambda: corrupt_model_drill(temp_root)),
            run_drill("corrupt_backup", lambda: corrupt_backup_drill(temp_root)),
            run_drill("primary_evidence_gate", lambda: replay_gate_drill(temp_root)),
            run_drill("official_revision_withdrawal", lambda: revision_withdrawal_drill(temp_root)),
            run_drill("forbidden_route_injection_guard", lambda: forbidden_route_drill(temp_root)),
        ]
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "passed": all(drill["passed"] for drill in drills),
        "network_used": False,
        "trading_system_touched": False,
        "drills": drills,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
