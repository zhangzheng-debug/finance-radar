#!/usr/bin/env python3
"""Capture a public, credential-free proof of the read-only market-data boundary."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = os.environ.get("FINANCE_RADAR_AUDIT_API_URL")
REQUIRED_PROVIDERS = {
    "binance_public": ("PERSISTED_EVENT_OBSERVATION", "SERVER_DIRECT"),
    "twelve_data": ("PERSISTED_EVENT_OBSERVATION", "SERVER_DIRECT"),
    "ibkr_tws_readonly": ("CAPABILITY_PROBE_ONLY", "OPERATOR_DESKTOP"),
}


def fetch_payload(base_url: str, timeout: float) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/v1/market/capabilities"
    with httpx.Client(
        timeout=httpx.Timeout(timeout, connect=timeout),
        trust_env=False,
        headers={"Accept": "application/json", "User-Agent": "FinanceRadar-MarketAudit/1.0"},
    ) as client:
        for attempt in range(3):
            try:
                response = client.get(url)
                response.raise_for_status()
                envelope = response.json()
                data = envelope.get("data")
                if not isinstance(data, dict):
                    raise ValueError("API envelope has no data object")
                return data
            except httpx.TransportError:
                if attempt == 2:
                    raise
                time.sleep(0.5 * (attempt + 1))
    raise AssertionError("unreachable")


def assess(data: dict[str, Any], endpoint: str) -> dict[str, Any]:
    boundary = data.get("boundary") if isinstance(data.get("boundary"), dict) else {}
    horizon_policy = (
        data.get("horizon_policy") if isinstance(data.get("horizon_policy"), dict) else {}
    )
    provider_list = data.get("providers") if isinstance(data.get("providers"), list) else []
    providers = {
        str(item.get("provider_id")): item
        for item in provider_list
        if isinstance(item, dict) and item.get("provider_id")
    }

    checks: dict[str, bool] = {
        "boundary_read_only": boundary.get("read_only") is True,
        "boundary_no_trading": boundary.get("no_trading") is True,
        "boundary_no_account_data": boundary.get("account_data_used") is False,
        "boundary_post_event_only": boundary.get("post_event_audit_only") is True,
        "boundary_not_model_feature": boundary.get("allowed_as_model_feature") is False,
        "required_providers_present": set(REQUIRED_PROVIDERS).issubset(providers),
        "all_providers_read_only": bool(providers)
        and all(item.get("read_only") is True for item in providers.values()),
        "no_order_endpoints": bool(providers)
        and all(item.get("order_endpoints_present") is False for item in providers.values()),
        "binance_server_observed": (
            providers.get("binance_public", {}).get("status") == "OBSERVED"
            and int(providers.get("binance_public", {}).get("snapshots") or 0) >= 1
        ),
        "twelve_server_observed": (
            providers.get("twelve_data", {}).get("status") == "OBSERVED"
            and int(providers.get("twelve_data", {}).get("snapshots") or 0) >= 1
        ),
        "ibkr_local_probe_only": (
            providers.get("ibkr_tws_readonly", {}).get("status") == "LOCAL_PROBE_ONLY"
            and providers.get("ibkr_tws_readonly", {}).get("deployment") == "OPERATOR_DESKTOP"
        ),
        "observer_relative_horizons_declared": horizon_policy.get("windows")
        == ["t_plus_5m", "t_plus_30m", "t_plus_1d"],
        "missed_windows_never_backfilled": horizon_policy.get("missed_window_behavior")
        == "record_MISSED_WINDOW_without_latest_quote_substitution",
        "horizon_metrics_post_event_only": horizon_policy.get("return_metric_scope")
        == "post_event_audit_only",
    }
    for provider_id, (role, deployment) in REQUIRED_PROVIDERS.items():
        item = providers.get(provider_id, {})
        checks[f"{provider_id}_role"] = (
            item.get("role") == role and item.get("deployment") == deployment
        )

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": endpoint,
        "passed": all(checks.values()),
        "checks": checks,
        "provider_policy": data.get("provider_policy", {}),
        "horizon_policy": horizon_policy,
        "boundary": boundary,
        "providers": [providers[key] for key in REQUIRED_PROVIDERS if key in providers],
        "interpretation": {
            "market_data_role": "post_event_audit_context_only",
            "truth_or_causality_evidence": False,
            "model_feature_allowed": False,
            "trading_or_account_access": False,
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Live read-only market capability audit",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Endpoint: `{report['endpoint']}`",
        f"- Status: `{'PASS' if report['passed'] else 'FAIL'}`",
        "- Meaning: quotes are post-event context only; they are not truth, causality, model features, or trading signals.",
        "",
        "## Providers",
        "",
        "| Provider | Role | Deployment | Status | Jobs completed | Snapshots | Last snapshot | Last error |",
        "|---|---|---|---|---:|---:|---|---|",
    ]
    for item in report["providers"]:
        lines.append(
            "| {provider_id} | {role} | {deployment} | {status} | {completed_jobs} | "
            "{snapshots} | {last_snapshot_at} | {last_error} |".format(
                provider_id=item.get("provider_id", "-"),
                role=item.get("role", "-"),
                deployment=item.get("deployment", "-"),
                status=item.get("status", "-"),
                completed_jobs=item.get("completed_jobs", 0),
                snapshots=item.get("snapshots", 0),
                last_snapshot_at=item.get("last_snapshot_at") or "-",
                last_error=(item.get("last_error") or "-").replace("|", "/"),
            )
        )
    lines.extend(["", "## Machine checks", ""])
    for name, passed in report["checks"].items():
        lines.append(f"- `{'PASS' if passed else 'FAIL'}` {name}")
    lines.extend(
        [
            "",
            "## Boundary",
            "",
            "- All providers are read-only and expose no order endpoint.",
            "- Binance and Twelve Data are persisted server observations.",
            "- IBKR TWS remains an operator-desktop capability probe; it is not a server dependency.",
            "- T+5m/T+30m/T+1d are measured from the first real observer snapshot; missed windows are recorded, never backfilled.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE, required=DEFAULT_BASE is None)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--json-output",
        type=Path,
        default=ROOT / "reports" / "market_capabilities_live_latest.json",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=ROOT / "reports" / "market_capabilities_live_latest.md",
    )
    args = parser.parse_args()
    endpoint = f"{args.base_url.rstrip('/')}/api/v1/market/capabilities"
    try:
        data = fetch_payload(args.base_url, args.timeout)
        report = assess(data, endpoint)
    except Exception as exc:
        print(json.dumps({"passed": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 2
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "checks": report["checks"],
                "json": str(args.json_output),
                "markdown": str(args.markdown_output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
