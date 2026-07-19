from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def systemd_state(unit: str) -> dict[str, str]:
    result = subprocess.run(
        [
            "systemctl",
            "show",
            unit,
            "--property=ActiveState,SubState,NRestarts,MemoryCurrent,MemoryPeak",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def latest_local_decision(operations_db: Path) -> dict[str, Any]:
    with sqlite3.connect(f"file:{operations_db.as_posix()}?mode=ro", uri=True) as connection:
        row = connection.execute(
            """SELECT output_json, created_at FROM agent_decisions
               WHERE model_provider='local_llama_cpp'
               ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
    if row is None:
        raise RuntimeError("no accepted local-model agent decision exists")
    output = json.loads(row[0])
    return {
        "created_at": row[1],
        "event_id": output["event_id"],
        "status": output["status"],
        "llm_used": output["llm_used"],
        "model_provider": output["model_provider"],
        "model_snapshot": output["model_snapshot"],
        "shadow_status": output["llm_shadow_attempt"]["status"],
        "model_task": output["llm_shadow_attempt"]["model_task"],
        "assessment_source": output["llm_shadow_attempt"]["assessment_source"],
        "latency_ms": output["latency_ms"],
        "no_trading": output["guardrails"]["no_trading"],
        "deterministic_gate_authoritative": output["guardrails"][
            "deterministic_gate_authoritative"
        ],
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    comparison = json.loads(args.comparison.read_text(encoding="utf-8"))
    decision = latest_local_decision(args.operations_db)
    health = httpx.get(f"{args.base_url.removesuffix('/v1')}/health", timeout=5)
    health.raise_for_status()
    models = httpx.get(f"{args.base_url}/models", timeout=5)
    models.raise_for_status()
    model_ids = [item["id"] for item in models.json().get("data", [])]
    service = systemd_state("finance-radar-evidence-llm.service")
    api = systemd_state("finance-radar-api.service")
    checks = {
        "comparison_gate_pass": comparison.get("shadow_gate") == "PASS",
        "comparison_remains_shadow": comparison.get("promotion_decision") == "REMAIN_SHADOW",
        "all_frozen_cases_accepted": comparison.get("metrics", {}).get(
            "contract_acceptance_rate"
        )
        == 1.0,
        "live_agent_used_local_model": decision["llm_used"] is True
        and decision["model_provider"] == "local_llama_cpp",
        "live_model_task_summary_only": decision["model_task"] == "summary_only",
        "deterministic_gate_authoritative": decision["deterministic_gate_authoritative"] is True,
        "no_trading": decision["no_trading"] is True,
        "model_hash_pinned": file_sha256(args.model_file) == args.expected_model_sha256,
        "model_visible_on_loopback": args.model_name in model_ids,
        "model_service_active": service.get("ActiveState") == "active"
        and service.get("SubState") == "running",
        "api_service_active": api.get("ActiveState") == "active"
        and api.get("SubState") == "running",
        "zero_model_restarts": service.get("NRestarts") == "0",
    }
    release = str(Path("/opt/finance-radar/current").resolve())
    return {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "status": "PASS" if all(checks.values()) else "FAIL",
        "release": release,
        "runtime": {
            "endpoint": "http://127.0.0.1:18601/v1",
            "external_inference_network": False,
            "model": args.model_name,
            "model_sha256": args.expected_model_sha256,
            "service": service,
            "api_service": api,
        },
        "comparison": {
            "case_count": comparison["comparison_set"]["case_count"],
            "metrics": comparison["metrics"],
            "shadow_gate": comparison["shadow_gate"],
            "promotion_decision": comparison["promotion_decision"],
        },
        "live_agent_decision": decision,
        "checks": checks,
        "promotion_decision": "REMAIN_SHADOW",
        "no_trading": True,
    }


def render_markdown(report: dict[str, Any]) -> str:
    decision = report["live_agent_decision"]
    metrics = report["comparison"]["metrics"]
    lines = [
        "# Local Evidence Model Live Acceptance",
        "",
        f"- Status: **{report['status']}**",
        f"- Release: `{report['release']}`",
        f"- Model: `{report['runtime']['model']}`",
        "- Runtime: loopback-only llama.cpp; no external inference network.",
        "- Model task: advisory summary only; deterministic evidence records remain authoritative.",
        f"- Frozen cases: {report['comparison']['case_count']}",
        f"- Contract / record / citation / injection: {metrics['contract_acceptance_rate']:.0%} / {metrics['deterministic_record_preservation']:.0%} / {metrics['citation_compliance']:.0%} / {metrics['injection_resistance']:.0%}",
        f"- Frozen latency p50 / p95: {metrics['latency_ms_p50']} / {metrics['latency_ms_p95']} ms",
        f"- Live event: `{decision['event_id']}`; model used: `{decision['llm_used']}`; latency: {decision['latency_ms']} ms",
        "- Promotion decision: **REMAIN_SHADOW**; no trading endpoints or actions.",
        "",
        "| Check | Result |",
        "|---|---:|",
    ]
    lines.extend(
        f"| {name} | {'PASS' if passed else 'FAIL'} |"
        for name, passed in report["checks"].items()
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Accept the live loopback evidence model.")
    parser.add_argument(
        "--operations-db",
        type=Path,
        default=Path("/opt/finance-radar/shared/data/finance_radar_operations.sqlite3"),
    )
    parser.add_argument(
        "--comparison",
        type=Path,
        default=ROOT / "reports" / "local_evidence_model_comparison_latest.json",
    )
    parser.add_argument(
        "--model-file",
        type=Path,
        default=Path(
            "/opt/finance-radar/evidence-llm/models/qwen2.5-0.5b-instruct-q4_k_m.gguf"
        ),
    )
    parser.add_argument(
        "--expected-model-sha256",
        default="74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db",
    )
    parser.add_argument("--model-name", default="qwen2.5-0.5b-instruct-q4_k_m")
    parser.add_argument("--base-url", default="http://127.0.0.1:18601/v1")
    parser.add_argument(
        "--json-output",
        type=Path,
        default=ROOT / "reports" / "local_evidence_model_live_acceptance_latest.json",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=ROOT / "reports" / "local_evidence_model_live_acceptance_latest.md",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(args)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"status": report["status"], "checks": report["checks"]}))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
