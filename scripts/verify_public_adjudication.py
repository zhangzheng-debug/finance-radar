#!/usr/bin/env python3
"""Verify the public, read-only boundary of the v3 adjudication workflow."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]


def verify(
    api_base: str,
    web_url: str,
    diagnostics_path: Path,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    owns_client = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=True)
    try:
        health_response = client.get(f"{api_base.rstrip('/')}/api/v1/health")
        status_response = client.get(f"{api_base.rstrip('/')}/api/v1/adjudication/status")
        queue_response = client.get(
            f"{api_base.rstrip('/')}/api/v1/adjudication/queue",
            params={"reviewer_id": "unauthenticated-boundary-probe"},
        )
        web_response = client.get(web_url)
    finally:
        if owns_client:
            client.close()
    health = health_response.json() if health_response.is_success else {}
    status = status_response.json() if status_response.is_success else {}
    queue = queue_response.json()
    data = status.get("data") or {}
    health_data = health.get("data") or {}
    diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
    checks = {
        "public_health_ok": health_response.status_code == 200 and health_data.get("status") == "ok",
        "api_schema_1_1": health.get("schema_version") == "1.1",
        "operations_schema_3": (health_data.get("operations") or {}).get("schema_version") == 3,
        "dual_review_capability_advertised": "dual_blind_adjudication" in (health_data.get("capabilities") or []),
        "aggregate_status_public": status_response.status_code == 200 and int(data.get("samples") or 0) >= 24,
        "aggregate_status_hides_annotations": "annotations" not in data,
        "public_write_controls_default_closed": data.get("public_review_ui_default_closed") is True,
        "no_unauthenticated_queue_read": (
            queue_response.status_code == 403
            and (queue.get("error") or {}).get("code") == "ADMIN_TOKEN_REQUIRED"
        ),
        "production_and_blind_freeze_unchanged": (
            data.get("production_changed") is False and data.get("blind_v2_frozen") is False
        ),
        "public_web_route_rendered": web_response.status_code == 200,
        "browser_QA_no_runtime_error": (
            diagnostics.get("skeleton_count") == 0
            and diagnostics.get("page_errors") == []
            and diagnostics.get("title") == "Adjudication Studio · Finance Radar"
            and int(diagnostics.get("body_text_length") or 0) >= 800
        ),
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if all(checks.values()) else "FAIL",
        "passed": sum(checks.values()),
        "total": len(checks),
        "checks": checks,
        "observed": {
            "samples": data.get("samples"),
            "workflow_status": data.get("status"),
            "status_counts": data.get("status_counts"),
            "valid_annotations": data.get("valid_annotations"),
            "api_schema": health.get("schema_version"),
            "operations_schema": (health_data.get("operations") or {}).get("schema_version"),
            "unauthenticated_queue_status": queue_response.status_code,
            "web_final_url": str(web_response.url),
            "browser_final_url": diagnostics.get("final_url"),
            "browser_http_errors": diagnostics.get("http_errors"),
        },
        "boundaries": {
            "admin_token_used": False,
            "review_submitted": False,
            "target_label_assigned": False,
            "trading_system_touched": False,
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Public adjudication workflow acceptance",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Result: **{report['status']} ({report['passed']}/{report['total']})**",
        f"- Samples: **{report['observed']['samples']}**",
        f"- Workflow: **{report['observed']['workflow_status']}**",
        f"- Unauthenticated queue request: **HTTP {report['observed']['unauthenticated_queue_status']}**",
        "",
        "## Checks",
        "",
    ]
    lines.extend(
        f"- [{'x' if passed else ' '}] `{name}`" for name, passed in report["checks"].items()
    )
    lines.extend(
        [
            "",
            "The aggregate progress page is public and read-only. Raw review tasks remain behind the API administrator gate, while the Streamlit write controls are disabled by default. This probe used no administrator token and submitted no review.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-base",
        default="https://radar.167-172-69-16.sslip.io:8443/finance-radar-api",
    )
    parser.add_argument(
        "--web-url",
        default="https://radar.167-172-69-16.sslip.io:8443/radar/Adjudication_Studio",
    )
    parser.add_argument(
        "--diagnostics",
        type=Path,
        default=ROOT / "reports/ui_qa_20260719/adjudication_readonly_1920x1080.json",
    )
    parser.add_argument(
        "--json", type=Path, default=ROOT / "reports/adjudication_v3_public_acceptance.json"
    )
    parser.add_argument(
        "--markdown", type=Path, default=ROOT / "reports/adjudication_v3_public_acceptance.md"
    )
    args = parser.parse_args()
    report = verify(args.api_base, args.web_url, args.diagnostics.resolve())
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
