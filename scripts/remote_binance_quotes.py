#!/usr/bin/env python3
"""Fetch Binance public prices through a remote SSH relay.

Safety boundary: this program only runs a fixed ``curl`` command against
public Binance market-data endpoints. It does not read remote project files,
does not load Binance account credentials, and has no account/order methods.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULT_SYMBOLS = {
    "spot": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"],
    "usdm": ["BTCUSDC", "ETHUSDC", "XRPUSDC", "BTCUSDT", "ETHUSDT"],
}
PUBLIC_ENDPOINTS = {
    "spot": "https://api.binance.com/api/v3/ticker/price",
    "usdm": "https://fapi.binance.com/fapi/v1/ticker/price",
}
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{5,24}$")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing configuration: {name}")
    return value


def remote_json(url: str, timeout: float) -> Any:
    host = required_env("BINANCE_REMOTE_SSH_HOST")
    user = required_env("BINANCE_REMOTE_SSH_USER")
    key_path = Path(required_env("BINANCE_REMOTE_SSH_KEY")).expanduser()
    port = os.environ.get("BINANCE_REMOTE_SSH_PORT", "22").strip() or "22"
    if not key_path.is_file():
        raise ValueError(f"SSH key does not exist: {key_path}")
    if not port.isdigit():
        raise ValueError("BINANCE_REMOTE_SSH_PORT must be numeric")

    remote_command = f"curl -fsS --max-time {max(1, int(timeout))} {shlex.quote(url)}"
    command = [
        "ssh",
        "-i",
        str(key_path),
        "-p",
        port,
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={max(1, int(timeout))}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{user}@{host}",
        remote_command,
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout + 10,
        check=False,
    )
    if completed.returncode != 0:
        detail = " ".join(completed.stderr.split())[:240]
        raise RuntimeError(f"remote market-data command failed ({completed.returncode}): {detail}")
    return json.loads(completed.stdout)


def fetch_market(market: str, symbols: list[str], timeout: float) -> dict[str, Any]:
    invalid = [symbol for symbol in symbols if not SYMBOL_PATTERN.fullmatch(symbol)]
    if invalid:
        raise ValueError(f"Invalid symbols: {', '.join(invalid)}")

    payload = remote_json(PUBLIC_ENDPOINTS[market], timeout)
    if not isinstance(payload, list):
        raise ValueError(f"Unexpected Binance {market} response")
    by_symbol = {
        str(item.get("symbol")): str(item.get("price"))
        for item in payload
        if isinstance(item, dict) and item.get("symbol") and item.get("price") is not None
    }
    missing = [symbol for symbol in symbols if symbol not in by_symbol]
    if missing:
        raise ValueError(f"Binance {market} response missing: {', '.join(missing)}")
    return {
        "market": market,
        "endpoint": PUBLIC_ENDPOINTS[market],
        "quotes": [{"symbol": symbol, "price": by_symbol[symbol]} for symbol in symbols],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--market", choices=("spot", "usdm", "both"), default="both")
    parser.add_argument("--symbols", nargs="*", help="Override symbols for a single market")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()

    load_dotenv(Path(args.env_file))
    markets = ["spot", "usdm"] if args.market == "both" else [args.market]
    if args.symbols and len(markets) != 1:
        parser.error("--symbols requires --market spot or --market usdm")

    try:
        results = []
        for market in markets:
            symbols = [symbol.upper() for symbol in (args.symbols or DEFAULT_SYMBOLS[market])]
            results.append(fetch_market(market, symbols, args.timeout))
        document = {
            "source": "binance_public_market_data_via_ssh",
            "relay_host": required_env("BINANCE_REMOTE_SSH_HOST"),
            "retrieved_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "safety": {
                "account_credentials_used": False,
                "account_endpoint_called": False,
                "order_endpoint_called": False,
                "remote_project_files_read": False,
            },
            "markets": results,
        }
        print(json.dumps(document, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
