#!/usr/bin/env python3
"""Prove read-only multi-asset market-data access through a local IBKR TWS.

This probe contains no account, position, execution, or order requests. It asks
only for delayed-fallback market-data snapshots for one stock, one FX pair, and
one crude-oil future, then disconnects.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.ticktype import TickTypeEnum
from ibapi.wrapper import EWrapper


INFO_ERROR_CODES = {2104, 2106, 2107, 2108, 2119, 2158}
NON_TERMINAL_MARKET_DATA_ERRORS = {10089, 10090}
KNOWN_ERROR_MESSAGES = {
    10089: "Requested data needs an additional API market-data subscription.",
    10285: "Installed ibapi is too old for fractional-size data; API version 163+ is required.",
}


def tick_name(tick_type: int) -> str:
    """Support both the older PyPI API and newer official API enum helpers."""
    converter = getattr(TickTypeEnum, "toStr", None) or getattr(TickTypeEnum, "to_str")
    return str(converter(tick_type))


@dataclass(frozen=True)
class Instrument:
    request_id: int
    label: str
    contract: Contract


def stock(symbol: str) -> Contract:
    contract = Contract()
    contract.symbol = symbol
    contract.secType = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"
    return contract


def fx(base: str, quote: str) -> Contract:
    contract = Contract()
    contract.symbol = base
    contract.secType = "CASH"
    contract.exchange = "IDEALPRO"
    contract.currency = quote
    return contract


def future(symbol: str, contract_month: str, exchange: str) -> Contract:
    contract = Contract()
    contract.symbol = symbol
    contract.secType = "FUT"
    contract.exchange = exchange
    contract.currency = "USD"
    contract.lastTradeDateOrContractMonth = contract_month
    return contract


class ReadOnlyProbe(EWrapper, EClient):
    def __init__(self, instruments: list[Instrument]) -> None:
        EClient.__init__(self, self)
        self.instruments = {item.request_id: item for item in instruments}
        self.ready = threading.Event()
        self.finished = threading.Event()
        self.snapshot_ends: set[int] = set()
        self.terminal_requests: set[int] = set()
        self.market_data_types: dict[int, int] = {}
        self.ticks: dict[int, dict[str, float]] = defaultdict(dict)
        self.errors: list[dict[str, Any]] = []

    def nextValidId(self, orderId: int) -> None:  # noqa: N802 - IBKR callback name
        # TWS sends this handshake value on every API connection. The probe
        # deliberately never uses it to place an order.
        self.ready.set()

    def error(self, reqId: int, *args: Any) -> None:  # noqa: N802 - IBKR callback name
        # API 10.33+ inserts errorTime before errorCode. Keep compatibility
        # with older clients so the probe can explain an upgrade requirement.
        if len(args) >= 3 and isinstance(args[1], int):
            error_time, errorCode, errorString = args[:3]
        elif len(args) >= 2:
            error_time, errorCode, errorString = None, args[0], args[1]
        else:
            return
        informational = errorCode in INFO_ERROR_CODES
        self.errors.append(
            {
                "request_id": reqId,
                "error_time": error_time,
                "code": errorCode,
                "message": KNOWN_ERROR_MESSAGES.get(
                    errorCode, " ".join(errorString.split())[:300]
                ),
                "informational": informational,
            }
        )
        if (
            reqId in self.instruments
            and not informational
            and errorCode not in NON_TERMINAL_MARKET_DATA_ERRORS
        ):
            self.terminal_requests.add(reqId)
            self._mark_finished_if_complete()

    def marketDataType(self, reqId: int, marketDataType: int) -> None:  # noqa: N802
        self.market_data_types[reqId] = marketDataType

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib: Any) -> None:  # noqa: N802
        if price >= 0:
            self.ticks[reqId][tick_name(tickType)] = price

    def tickSize(self, reqId: int, tickType: int, size: Any) -> None:  # noqa: N802
        numeric = float(size)
        if numeric >= 0:
            self.ticks[reqId][tick_name(tickType)] = numeric

    def tickSnapshotEnd(self, reqId: int) -> None:  # noqa: N802
        self.snapshot_ends.add(reqId)
        self._mark_finished_if_complete()

    def _mark_finished_if_complete(self) -> None:
        if self.snapshot_ends | self.terminal_requests == set(self.instruments):
            self.finished.set()


def normalized_ticks(raw: dict[str, float]) -> dict[str, float]:
    result: dict[str, float] = {}
    for name, value in raw.items():
        clean = name.removeprefix("DELAYED_").lower()
        result[clean] = value
    return dict(sorted(result.items()))


def run_probe(host: str, port: int, client_id: int, timeout: float) -> dict[str, Any]:
    instruments = [
        Instrument(7101, "AAPL stock", stock("AAPL")),
        Instrument(7102, "EUR.USD spot FX", fx("EUR", "USD")),
        Instrument(7103, "CL Aug-2026 future", future("CL", "202608", "NYMEX")),
    ]
    app = ReadOnlyProbe(instruments)
    started = time.perf_counter()

    try:
        # Older official ibapi releases return None even after a successful
        # connect, so use isConnected() as the authoritative state check.
        app.connect(host, port, clientId=client_id)
        if not app.isConnected():
            raise ConnectionError("TWS socket connection was rejected")
        network_thread = threading.Thread(target=app.run, name="ibkr-api-reader", daemon=True)
        network_thread.start()

        if not app.ready.wait(timeout=min(timeout, 8.0)):
            raise TimeoutError("TWS connected but did not complete the API handshake")

        # Type 3 requests delayed data as a fallback. TWS automatically returns
        # live data instead when the account has the relevant live entitlement.
        app.reqMarketDataType(3)
        for item in instruments:
            app.reqMktData(
                item.request_id,
                item.contract,
                genericTickList="",
                snapshot=True,
                regulatorySnapshot=False,
                mktDataOptions=[],
            )

        remaining = max(1.0, timeout - (time.perf_counter() - started))
        app.finished.wait(timeout=remaining)
    except Exception as exc:
        return {
            "status": "FAIL",
            "endpoint": f"{host}:{port}",
            "summary": f"{type(exc).__name__}: {exc}",
            "instruments": [],
            "errors": app.errors,
        }
    finally:
        if app.isConnected():
            app.disconnect()

    type_names = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}
    results = []
    for item in instruments:
        ticks = normalized_ticks(app.ticks.get(item.request_id, {}))
        results.append(
            {
                "label": item.label,
                "request_id": item.request_id,
                "market_data_type": type_names.get(
                    app.market_data_types.get(item.request_id), "not-reported"
                ),
                "snapshot_complete": item.request_id in app.snapshot_ends,
                "ticks": ticks,
                "has_price": any(
                    field in ticks for field in ("bid", "ask", "last", "close", "open")
                ),
            }
        )

    quoted_count = sum(bool(item["has_price"]) for item in results)
    fatal_errors = [item for item in app.errors if not item["informational"]]
    return {
        "status": "PASS" if quoted_count else "WARN",
        "endpoint": f"{host}:{port}",
        "summary": f"Received prices for {quoted_count}/{len(results)} asset classes",
        "read_only_scope": "market-data snapshots only; no account/order methods",
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "instruments": results,
        "errors": fatal_errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("IBKR_TWS_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("IBKR_TWS_PORT", "7497"))
    )
    parser.add_argument(
        "--client-id", type=int, default=int(os.environ.get("IBKR_CLIENT_ID", "71"))
    )
    parser.add_argument("--timeout", type=float, default=18.0)
    args = parser.parse_args()

    result = run_probe(args.host, args.port, args.client_id, args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
