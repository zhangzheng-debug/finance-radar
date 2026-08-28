#!/usr/bin/env python3
"""Audit the configured observation universe against provider symbol catalogs.

The audit is read-only.  It does not create market jobs, change event mappings,
or call any trading/account endpoint.  Active mapping assets are release gates;
dormant registry entries are reported as capacity warnings until a rule uses them.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "event_asset_mapping_v1.json"
DEFAULT_ENV = ROOT / ".env"
BINANCE_EXCHANGE_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"
TWELVE_DATA_ETF_CATALOG_URL = "https://api.twelvedata.com/etf"


def _load_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("asset mapping config must be a JSON object")
    return value


def _catalog_symbols(payload: Any) -> set[str]:
    if isinstance(payload, dict):
        rows = payload.get("data")
    else:
        rows = payload
    if not isinstance(rows, list):
        raise ValueError("provider catalog response has no data list")
    return {
        str(row.get("symbol") or "").strip().upper()
        for row in rows
        if isinstance(row, dict) and str(row.get("symbol") or "").strip()
    }


def fetch_binance_symbols(timeout: float) -> set[str]:
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        response = client.get(
            BINANCE_EXCHANGE_INFO_URL,
            headers={"Accept": "application/json", "User-Agent": "FinanceRadar-CoverageAudit/1.0"},
        )
        response.raise_for_status()
        payload = response.json()
    rows = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Binance exchangeInfo has no symbols list")
    return {
        str(row.get("symbol") or "").strip().upper()
        for row in rows
        if isinstance(row, dict)
        and row.get("status") == "TRADING"
        and str(row.get("symbol") or "").strip()
    }


def fetch_twelve_data_etfs(api_key: str, timeout: float) -> set[str]:
    if not api_key:
        raise ValueError("TWELVE_DATA_API_KEY is required for the ETF catalog audit")
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        response = client.get(
            TWELVE_DATA_ETF_CATALOG_URL,
            headers={
                "Accept": "application/json",
                "Authorization": f"apikey {api_key}",
                "User-Agent": "FinanceRadar-CoverageAudit/1.0",
            },
        )
        response.raise_for_status()
        payload = response.json()
    if isinstance(payload, dict) and payload.get("status") == "error":
        raise RuntimeError(str(payload.get("message") or "Twelve Data catalog error"))
    return _catalog_symbols(payload)


def assess_coverage(
    config: dict[str, Any],
    *,
    binance_symbols: set[str] | None,
    twelve_data_symbols: set[str] | None,
    provider_errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    registry = config.get("asset_registry")
    rules = config.get("rules")
    if not isinstance(registry, dict) or not isinstance(rules, list):
        raise ValueError("asset mapping config is missing registry or rules")
    active_symbols = {
        str(symbol).upper()
        for rule in rules
        if isinstance(rule, dict)
        for symbol in (rule.get("assets") or [])
    }
    errors = dict(provider_errors or {})
    rows: list[dict[str, Any]] = []
    for registry_symbol, raw_asset in sorted(registry.items()):
        if not isinstance(raw_asset, dict):
            continue
        symbol = str(raw_asset.get("symbol") or registry_symbol).upper()
        provider_symbol = str(raw_asset.get("provider_symbol") or symbol).upper()
        provider = "binance_public" if str(raw_asset.get("asset_type") or "").lower() == "crypto" else "twelve_data"
        catalog = binance_symbols if provider == "binance_public" else twelve_data_symbols
        if catalog is None:
            status = "UNKNOWN_PROVIDER_ERROR"
        elif provider_symbol in catalog:
            status = "SUPPORTED"
        else:
            status = "NOT_IN_PROVIDER_CATALOG"
        rows.append(
            {
                "symbol": symbol,
                "provider_symbol": provider_symbol,
                "provider": provider,
                "active": symbol in active_symbols,
                "status": status,
                "role": raw_asset.get("role"),
                "label": raw_asset.get("proxy_label"),
            }
        )

    active_rows = [row for row in rows if row["active"]]
    active_failures = [row for row in active_rows if row["status"] != "SUPPORTED"]
    counts = Counter(row["status"] for row in rows)
    return {
        "contract_version": "asset-provider-coverage-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy_version": config.get("policy_version"),
        "registry_assets": len(rows),
        "active_assets": len(active_rows),
        "dormant_assets": len(rows) - len(active_rows),
        "passed": not active_failures and not errors,
        "status_counts": dict(sorted(counts.items())),
        "provider_errors": errors,
        "active_failures": active_failures,
        "dormant_failures": [
            row for row in rows if not row["active"] and row["status"] != "SUPPORTED"
        ],
        "assets": rows,
        "boundary": {
            "read_only": True,
            "no_trading": True,
            "no_event_or_mapping_mutation": True,
            "active_assets_are_release_gate": True,
            "dormant_assets_are_warning_only": True,
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Asset provider coverage audit",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Policy: `{report.get('policy_version')}`",
        f"- Status: `{'PASS' if report['passed'] else 'FAIL'}`",
        f"- Registry / active / dormant: `{report['registry_assets']}` / `{report['active_assets']}` / `{report['dormant_assets']}`",
        "- Boundary: read-only provider catalog check; no event, mapping, account, order or trading mutation.",
        "",
        "## Provider errors",
        "",
    ]
    if report["provider_errors"]:
        for provider, error in sorted(report["provider_errors"].items()):
            lines.append(f"- `{provider}`: {error}")
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "## Active failures",
            "",
            "| Symbol | Provider symbol | Provider | Status |",
            "|---|---|---|---|",
        ]
    )
    for row in report["active_failures"]:
        lines.append(
            f"| `{row['symbol']}` | `{row['provider_symbol']}` | `{row['provider']}` | `{row['status']}` |"
        )
    if not report["active_failures"]:
        lines.append("| - | - | - | All active assets supported |")
    lines.extend(["", "## Status counts", ""])
    for status, count in report["status_counts"].items():
        lines.append(f"- `{status}`: `{count}`")
    lines.append("")
    return "\n".join(lines)


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def run(
    config_path: Path,
    *,
    api_key: str,
    timeout: float,
    binance_fetcher: Callable[[float], set[str]] = fetch_binance_symbols,
    twelve_data_fetcher: Callable[[str, float], set[str]] = fetch_twelve_data_etfs,
) -> dict[str, Any]:
    errors: dict[str, str] = {}
    binance: set[str] | None = None
    twelve: set[str] | None = None
    try:
        binance = binance_fetcher(timeout)
    except Exception as exc:  # provider failure belongs in the report
        errors["binance_public"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    try:
        twelve = twelve_data_fetcher(api_key, timeout)
    except Exception as exc:  # provider failure belongs in the report
        errors["twelve_data"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    return assess_coverage(
        _json_object(config_path),
        binance_symbols=binance,
        twelve_data_symbols=twelve,
        provider_errors=errors,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--json-output", type=Path, default=ROOT / "reports" / "asset_provider_coverage_latest.json"
    )
    parser.add_argument(
        "--markdown-output", type=Path, default=ROOT / "reports" / "asset_provider_coverage_latest.md"
    )
    args = parser.parse_args()
    _load_env(args.env_file)
    report = run(
        args.config,
        api_key=os.environ.get("TWELVE_DATA_API_KEY", "").strip(),
        timeout=max(1.0, args.timeout),
    )
    _write_atomic(args.json_output, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    _write_atomic(args.markdown_output, render_markdown(report))
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "registry_assets": report["registry_assets"],
                "active_assets": report["active_assets"],
                "active_failures": len(report["active_failures"]),
                "provider_errors": report["provider_errors"],
                "json": str(args.json_output),
                "markdown": str(args.markdown_output),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
