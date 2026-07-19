#!/usr/bin/env python3
"""Read-only Gate 0 probes for the Finance Radar external dependencies.

The script intentionally uses only the Python standard library. It never prints
or writes credential values, and the Telegram probes call only getMe/getChat.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import dataclasses
import datetime as dt
import hashlib
import json
import os
import platform
import secrets
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable


DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_RESPONSE_BYTES = 2_000_000
GENERIC_USER_AGENT = "finance-radar-gate0/0.1"
HTTP_TRANSPORT_ATTEMPTS = 3


@dataclasses.dataclass
class ProbeResult:
    order: int
    probe_id: str
    name: str
    group: str
    status: str
    endpoint: str
    started_at_utc: str
    latency_ms: int | None = None
    http_status: int | None = None
    summary: str = ""
    evidence: dict[str, Any] = dataclasses.field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE entries without overriding the process env."""
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


def configured(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def blocked(
    order: int,
    probe_id: str,
    name: str,
    endpoint: str,
    env_names: list[str],
    reason: str | None = None,
) -> ProbeResult:
    missing = [name for name in env_names if not configured(name)]
    return ProbeResult(
        order=order,
        probe_id=probe_id,
        name=name,
        group="credentialed",
        status="BLOCKED",
        endpoint=endpoint,
        started_at_utc=utc_now(),
        summary=reason or f"Missing configuration: {', '.join(missing)}",
        evidence={"required_env": env_names, "configured": False},
    )


def safe_text(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value).split())
    return text[:limit]


Validator = Callable[[bytes, dict[str, str]], tuple[str, dict[str, Any]]]


def http_probe(
    *,
    order: int,
    probe_id: str,
    name: str,
    group: str,
    url: str,
    endpoint_label: str,
    timeout: float,
    validator: Validator,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    data: bytes | None = None,
    failure_status: str = "FAIL",
) -> ProbeResult:
    started_at = utc_now()
    started = time.perf_counter()
    request_headers = {"User-Agent": GENERIC_USER_AGENT, "Accept": "*/*"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method=method,
    )
    for attempt in range(1, HTTP_TRANSPORT_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError(f"Response exceeded {MAX_RESPONSE_BYTES} bytes")
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                summary, evidence = validator(body, response_headers)
                evidence["transport_attempts"] = attempt
                return ProbeResult(
                    order=order,
                    probe_id=probe_id,
                    name=name,
                    group=group,
                    status="PASS",
                    endpoint=endpoint_label,
                    started_at_utc=started_at,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    http_status=response.status,
                    summary=summary,
                    evidence=evidence,
                )
        except urllib.error.HTTPError as exc:
            preview = ""
            try:
                preview = safe_text(exc.read(800).decode("utf-8", errors="replace"))
            except Exception:
                preview = ""
            return ProbeResult(
                order=order,
                probe_id=probe_id,
                name=name,
                group=group,
                status=failure_status,
                endpoint=endpoint_label,
                started_at_utc=started_at,
                latency_ms=round((time.perf_counter() - started) * 1000),
                http_status=exc.code,
                summary=f"HTTP {exc.code}: {safe_text(exc.reason)}",
                evidence={"response_preview": preview} if preview else {},
            )
        except (urllib.error.URLError, ssl.SSLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            if attempt < HTTP_TRANSPORT_ATTEMPTS:
                time.sleep(0.4 * attempt)
                continue
            return ProbeResult(
                order=order,
                probe_id=probe_id,
                name=name,
                group=group,
                status=failure_status,
                endpoint=endpoint_label,
                started_at_utc=started_at,
                latency_ms=round((time.perf_counter() - started) * 1000),
                summary=f"{type(exc).__name__}: {safe_text(exc)}",
                evidence={"transport_attempts": attempt},
            )
        except Exception as exc:
            return ProbeResult(
                order=order,
                probe_id=probe_id,
                name=name,
                group=group,
                status=failure_status,
                endpoint=endpoint_label,
                started_at_utc=started_at,
                latency_ms=round((time.perf_counter() - started) * 1000),
                summary=f"{type(exc).__name__}: {safe_text(exc)}",
                evidence={"transport_attempts": attempt},
            )


def json_body(body: bytes) -> Any:
    return json.loads(body.decode("utf-8"))


def validate_rss(body: bytes, _: dict[str, str]) -> tuple[str, dict[str, Any]]:
    root = ET.fromstring(body)
    items = root.findall(".//item")
    entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    count = len(items) + len(entries)
    if count == 0:
        raise ValueError("Feed parsed but contained no items/entries")
    first = items[0] if items else entries[0]
    title = first.findtext("title") or first.findtext("{http://www.w3.org/2005/Atom}title") or ""
    return "Feed parsed and contains entries", {"entry_count": count, "first_title": safe_text(title)}


def validate_bls(body: bytes, _: dict[str, str]) -> tuple[str, dict[str, Any]]:
    payload = json_body(body)
    if payload.get("status") != "REQUEST_SUCCEEDED":
        raise ValueError(f"BLS status was {payload.get('status')!r}")
    series = payload.get("Results", {}).get("series", [])
    points = series[0].get("data", []) if series else []
    if not series:
        raise ValueError("BLS returned no series")
    return "BLS public API returned CPI observations", {
        "series_count": len(series),
        "observation_count": len(points),
        "used_registration_key": configured("BLS_API_KEY"),
    }


def validate_gdelt(body: bytes, _: dict[str, str]) -> tuple[str, dict[str, Any]]:
    payload = json_body(body)
    articles = payload.get("articles", []) if isinstance(payload, dict) else []
    if not isinstance(articles, list):
        raise ValueError("GDELT response did not contain an articles list")
    return "GDELT DOC API returned valid JSON", {"article_count": len(articles)}


def validate_server_time(body: bytes, _: dict[str, str]) -> tuple[str, dict[str, Any]]:
    payload = json_body(body)
    server_time = payload.get("serverTime")
    if not isinstance(server_time, int):
        raise ValueError("serverTime missing")
    return "Exchange REST endpoint returned server time", {"server_time_ms": server_time}


def validate_sec(body: bytes, _: dict[str, str]) -> tuple[str, dict[str, Any]]:
    payload = json_body(body)
    recent = payload.get("filings", {}).get("recent", {})
    accessions = recent.get("accessionNumber", [])
    if not payload.get("cik") or not isinstance(accessions, list):
        raise ValueError("Unexpected SEC submissions schema")
    return "SEC submissions JSON returned a valid filing list", {
        "cik": str(payload.get("cik")),
        "entity_name": safe_text(payload.get("name", "")),
        "recent_filing_count": len(accessions),
    }


def validate_bea(body: bytes, _: dict[str, str]) -> tuple[str, dict[str, Any]]:
    payload = json_body(body)
    api = payload.get("BEAAPI", {})
    if "Error" in api:
        raise ValueError(safe_text(api["Error"]))
    datasets = api.get("Results", {}).get("Dataset", [])
    if not isinstance(datasets, list) or not datasets:
        raise ValueError("BEA returned no dataset list")
    return "BEA API key is accepted and datasets are available", {"dataset_count": len(datasets)}


def validate_marketaux(body: bytes, _: dict[str, str]) -> tuple[str, dict[str, Any]]:
    payload = json_body(body)
    articles = payload.get("data", [])
    if not isinstance(articles, list):
        raise ValueError("Marketaux response did not contain data list")
    meta = payload.get("meta", {})
    return "Marketaux token is accepted and news endpoint responded", {
        "article_count": len(articles),
        "found": meta.get("found"),
        "returned": meta.get("returned"),
    }


def validate_alpaca_snapshot(body: bytes, _: dict[str, str]) -> tuple[str, dict[str, Any]]:
    payload = json_body(body)
    keys = sorted(payload.keys()) if isinstance(payload, dict) else []
    if not any(key in payload for key in ("latestTrade", "minuteBar", "dailyBar")):
        raise ValueError("Alpaca snapshot did not contain market-data fields")
    return "Alpaca credentials can read the IEX snapshot endpoint", {"response_fields": keys}


def validate_alpaca_news(body: bytes, _: dict[str, str]) -> tuple[str, dict[str, Any]]:
    payload = json_body(body)
    news = payload.get("news", []) if isinstance(payload, dict) else []
    if not isinstance(news, list):
        raise ValueError("Alpaca response did not contain news list")
    return "Alpaca credentials can read historical news", {"news_count": len(news)}


def validate_telegram(body: bytes, _: dict[str, str]) -> tuple[str, dict[str, Any]]:
    payload = json_body(body)
    if payload.get("ok") is not True:
        raise ValueError(f"Telegram returned ok={payload.get('ok')!r}")
    result = payload.get("result", {})
    evidence: dict[str, Any] = {}
    for key in ("id", "username", "type", "title"):
        if key in result:
            evidence[key] = result[key]
    return "Telegram read-only method succeeded", evidence


def validate_fred(body: bytes, _: dict[str, str]) -> tuple[str, dict[str, Any]]:
    payload = json_body(body)
    observations = payload.get("observations", [])
    if not isinstance(observations, list):
        raise ValueError("FRED response did not contain observations")
    return "FRED key is accepted and CPI series is readable", {"observation_count": len(observations)}


def validate_twelve_data_multiasset(body: bytes, _: dict[str, str]) -> tuple[str, dict[str, Any]]:
    payload = json_body(body)
    expected = ["AAPL", "SPY", "EUR/USD", "BTC/USD"]
    if not isinstance(payload, dict):
        raise ValueError("Twelve Data returned a non-object response")
    if payload.get("status") == "error":
        raise ValueError(safe_text(payload.get("message", "Twelve Data error")))
    missing = [
        symbol
        for symbol in expected
        if not isinstance(payload.get(symbol), dict) or payload[symbol].get("price") is None
    ]
    if missing:
        raise ValueError(f"Twelve Data response missing prices: {', '.join(missing)}")
    return "Twelve Data returned stock, ETF, FX, and crypto prices", {
        "symbols": expected,
        "prices": {symbol: str(payload[symbol]["price"]) for symbol in expected},
    }


def websocket_probe(
    *,
    order: int,
    probe_id: str,
    name: str,
    host: str,
    path: str,
    endpoint_label: str,
    timeout: float,
) -> ProbeResult:
    started_at = utc_now()
    started = time.perf_counter()
    raw_sock = None
    tls_sock = None
    try:
        raw_sock = socket.create_connection((host, 443), timeout=timeout)
        tls_sock = ssl.create_default_context().wrap_socket(raw_sock, server_hostname=host)
        tls_sock.settimeout(timeout)
        ws_key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"User-Agent: {GENERIC_USER_AGENT}\r\n\r\n"
        ).encode("ascii")
        tls_sock.sendall(request)
        buffer = b""
        while b"\r\n\r\n" not in buffer and len(buffer) < 65536:
            chunk = tls_sock.recv(4096)
            if not chunk:
                break
            buffer += chunk
        headers, _, remainder = buffer.partition(b"\r\n\r\n")
        status_line = headers.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        if " 101 " not in f" {status_line} ":
            raise ValueError(f"WebSocket upgrade failed: {status_line}")
        accept_expected = base64.b64encode(
            hashlib.sha1((ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        header_text = headers.decode("iso-8859-1", errors="replace").lower()
        if f"sec-websocket-accept: {accept_expected.lower()}" not in header_text:
            raise ValueError("WebSocket accept key validation failed")
        frame_bytes = len(remainder)
        if frame_bytes == 0:
            frame_bytes = len(tls_sock.recv(16))
        if frame_bytes == 0:
            raise ValueError("Upgrade succeeded but no stream frame was received")
        return ProbeResult(
            order=order,
            probe_id=probe_id,
            name=name,
            group="public",
            status="PASS",
            endpoint=endpoint_label,
            started_at_utc=started_at,
            latency_ms=round((time.perf_counter() - started) * 1000),
            http_status=101,
            summary="WebSocket upgraded and delivered stream bytes",
            evidence={"received_frame_bytes": frame_bytes},
        )
    except Exception as exc:
        return ProbeResult(
            order=order,
            probe_id=probe_id,
            name=name,
            group="public",
            status="FAIL",
            endpoint=endpoint_label,
            started_at_utc=started_at,
            latency_ms=round((time.perf_counter() - started) * 1000),
            summary=f"{type(exc).__name__}: {safe_text(exc)}",
        )
    finally:
        if tls_sock is not None:
            try:
                tls_sock.close()
            except Exception:
                pass
        elif raw_sock is not None:
            try:
                raw_sock.close()
            except Exception:
                pass


def remote_binance_ssh_probe(*, order: int, timeout: float) -> ProbeResult:
    started_at = utc_now()
    started = time.perf_counter()
    endpoint_label = "SSH relay -> Binance public spot/USD-M ticker endpoints"
    try:
        helper = Path(__file__).with_name("remote_binance_quotes.py")
        completed = subprocess.run(
            [sys.executable, str(helper), "--market", "both", "--timeout", str(timeout)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=(timeout * 2) + 20,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(safe_text(completed.stderr or completed.stdout, 260))
        payload = json.loads(completed.stdout)
        safety = payload.get("safety", {})
        forbidden_true = [
            key
            for key in (
                "account_credentials_used",
                "account_endpoint_called",
                "order_endpoint_called",
                "remote_project_files_read",
            )
            if safety.get(key) is not False
        ]
        if forbidden_true:
            raise ValueError(f"Remote quote safety assertion failed: {', '.join(forbidden_true)}")
        markets = payload.get("markets", [])
        if not isinstance(markets, list) or len(markets) != 2:
            raise ValueError("Remote quote helper did not return both markets")
        symbols: dict[str, list[str]] = {}
        quote_count = 0
        for market in markets:
            market_name = str(market.get("market", ""))
            quotes = market.get("quotes", [])
            if market_name not in {"spot", "usdm"} or not isinstance(quotes, list) or not quotes:
                raise ValueError("Remote quote helper returned an invalid market payload")
            symbols[market_name] = [str(quote.get("symbol")) for quote in quotes]
            quote_count += len(quotes)
        return ProbeResult(
            order=order,
            probe_id="binance_remote_public_quotes",
            name="Binance Public Quotes via Singapore SSH Relay",
            group="credentialed",
            status="PASS",
            endpoint=endpoint_label,
            started_at_utc=started_at,
            latency_ms=round((time.perf_counter() - started) * 1000),
            summary="Remote relay returned spot and USD-M public prices without account/order access",
            evidence={"quote_count": quote_count, "symbols": symbols, "safety": safety},
        )
    except Exception as exc:
        return ProbeResult(
            order=order,
            probe_id="binance_remote_public_quotes",
            name="Binance Public Quotes via Singapore SSH Relay",
            group="credentialed",
            status="FAIL",
            endpoint=endpoint_label,
            started_at_utc=started_at,
            latency_ms=round((time.perf_counter() - started) * 1000),
            summary=f"{type(exc).__name__}: {safe_text(exc, 260)}",
        )


def ibkr_tws_readonly_probe(*, order: int, timeout: float) -> ProbeResult:
    started_at = utc_now()
    started = time.perf_counter()
    host = os.environ.get("IBKR_TWS_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = os.environ.get("IBKR_TWS_PORT", "7497").strip() or "7497"
    client_id = os.environ.get("IBKR_CLIENT_ID", "71").strip() or "71"
    endpoint_label = f"TWS socket {host}:{port} (market-data snapshots only)"
    try:
        helper = Path(__file__).with_name("ibkr_readonly_probe.py")
        completed = subprocess.run(
            [
                sys.executable,
                str(helper),
                "--host",
                host,
                "--port",
                port,
                "--client-id",
                client_id,
                "--timeout",
                str(timeout),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout + 12,
            check=False,
        )
        payload = json.loads(completed.stdout)
        if payload.get("read_only_scope") != "market-data snapshots only; no account/order methods":
            raise ValueError("IBKR helper did not return the read-only safety assertion")
        status = str(payload.get("status", "FAIL"))
        if status not in {"PASS", "WARN", "FAIL"}:
            raise ValueError(f"Unexpected IBKR helper status: {status}")
        instruments = payload.get("instruments", [])
        evidence = {
            "quoted": [item.get("label") for item in instruments if item.get("has_price")],
            "unavailable": [item.get("label") for item in instruments if not item.get("has_price")],
            "market_data_types": {
                str(item.get("label")): item.get("market_data_type") for item in instruments
            },
            "error_codes": [item.get("code") for item in payload.get("errors", [])],
            "safety": payload.get("read_only_scope"),
        }
        return ProbeResult(
            order=order,
            probe_id="ibkr_tws_readonly_market_data",
            name="IBKR TWS Read-Only Multi-Asset Market Data",
            group="local-app",
            status=status,
            endpoint=endpoint_label,
            started_at_utc=started_at,
            latency_ms=round((time.perf_counter() - started) * 1000),
            summary=str(payload.get("summary", "IBKR helper returned no summary")),
            evidence=evidence,
        )
    except Exception as exc:
        return ProbeResult(
            order=order,
            probe_id="ibkr_tws_readonly_market_data",
            name="IBKR TWS Read-Only Multi-Asset Market Data",
            group="local-app",
            status="FAIL",
            endpoint=endpoint_label,
            started_at_utc=started_at,
            latency_ms=round((time.perf_counter() - started) * 1000),
            summary=f"{type(exc).__name__}: {safe_text(exc, 260)}",
        )


def build_probe_jobs(timeout: float) -> tuple[list[Callable[[], ProbeResult]], list[ProbeResult]]:
    jobs: list[Callable[[], ProbeResult]] = []
    immediate: list[ProbeResult] = []

    def add_http(**kwargs: Any) -> None:
        jobs.append(lambda kwargs=kwargs: http_probe(timeout=timeout, **kwargs))

    add_http(
        order=10,
        probe_id="fed_rss",
        name="Federal Reserve RSS",
        group="public",
        url="https://www.federalreserve.gov/feeds/press_all.xml",
        endpoint_label="federalreserve.gov/feeds/press_all.xml",
        validator=validate_rss,
    )
    add_http(
        order=20,
        probe_id="bls_rss",
        name="BLS RSS",
        group="public",
        url="https://www.bls.gov/feed/bls_latest.rss",
        endpoint_label="bls.gov/feed/bls_latest.rss",
        validator=validate_rss,
        failure_status="WARN",
    )

    bls_payload: dict[str, Any] = {
        "seriesid": ["CUUR0000SA0"],
        "startyear": str(dt.datetime.now().year - 1),
        "endyear": str(dt.datetime.now().year),
    }
    if configured("BLS_API_KEY"):
        bls_payload["registrationkey"] = os.environ["BLS_API_KEY"].strip()
    add_http(
        order=30,
        probe_id="bls_api",
        name="BLS Public Data API",
        group="public",
        url="https://api.bls.gov/publicAPI/v2/timeseries/data/",
        endpoint_label="api.bls.gov/publicAPI/v2/timeseries/data",
        validator=validate_bls,
        method="POST",
        data=json.dumps(bls_payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    gdelt_query = urllib.parse.urlencode(
        {
            "query": "(economy OR markets)",
            "mode": "artlist",
            "maxrecords": "1",
            "format": "json",
            "timespan": "1d",
        }
    )
    add_http(
        order=40,
        probe_id="gdelt_doc_api",
        name="GDELT DOC API",
        group="public",
        url=f"https://api.gdeltproject.org/api/v2/doc/doc?{gdelt_query}",
        endpoint_label="api.gdeltproject.org/api/v2/doc/doc",
        validator=validate_gdelt,
        headers={"Accept": "application/json"},
        failure_status="WARN",
    )
    add_http(
        order=50,
        probe_id="binance_spot_rest",
        name="Binance Spot REST",
        group="public",
        url="https://api.binance.com/api/v3/time",
        endpoint_label="api.binance.com/api/v3/time",
        validator=validate_server_time,
        headers={"Accept": "application/json"},
        failure_status="WARN",
    )
    add_http(
        order=55,
        probe_id="binance_spot_market_data_rest",
        name="Binance Spot Market-Data-Only REST",
        group="public",
        url="https://data-api.binance.vision/api/v3/time",
        endpoint_label="data-api.binance.vision/api/v3/time",
        validator=validate_server_time,
        headers={"Accept": "application/json"},
    )
    add_http(
        order=60,
        probe_id="binance_futures_rest",
        name="Binance USD-M Futures REST",
        group="public",
        url="https://fapi.binance.com/fapi/v1/time",
        endpoint_label="fapi.binance.com/fapi/v1/time",
        validator=validate_server_time,
        headers={"Accept": "application/json"},
        failure_status="WARN",
    )
    jobs.append(
        lambda: websocket_probe(
            order=70,
            probe_id="binance_futures_agg_trade_ws",
            name="Binance USD-M Futures Aggregate-Trade WebSocket",
            host="fstream.binance.com",
            path="/market/ws/btcusdt@aggTrade",
            endpoint_label="fstream.binance.com/market/ws/btcusdt@aggTrade",
            timeout=timeout,
        )
    )

    remote_env = ["BINANCE_REMOTE_SSH_HOST", "BINANCE_REMOTE_SSH_USER", "BINANCE_REMOTE_SSH_KEY"]
    if all(configured(name) for name in remote_env):
        jobs.append(lambda: remote_binance_ssh_probe(order=80, timeout=timeout))
    else:
        immediate.append(
            blocked(
                80,
                "binance_remote_public_quotes",
                "Binance Public Quotes via Singapore SSH Relay",
                "SSH relay -> Binance public spot/USD-M ticker endpoints",
                remote_env,
            )
        )
    jobs.append(
        lambda: websocket_probe(
            order=75,
            probe_id="binance_futures_mark_price_ws",
            name="Binance USD-M Futures Mark-Price WebSocket",
            host="fstream.binance.com",
            path="/market/ws/btcusdt@markPrice@1s",
            endpoint_label="fstream.binance.com/market/ws/btcusdt@markPrice@1s",
            timeout=timeout,
        )
    )
    jobs.append(lambda: ibkr_tws_readonly_probe(order=90, timeout=timeout))

    sec_user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not sec_user_agent or "@" not in sec_user_agent:
        immediate.append(
            blocked(
                100,
                "sec_submissions",
                "SEC Submissions API",
                "data.sec.gov/submissions/CIK0000320193.json",
                ["SEC_USER_AGENT"],
                "SEC_USER_AGENT is missing or does not include a contact email",
            )
        )
    else:
        add_http(
            order=100,
            probe_id="sec_submissions",
            name="SEC Submissions API",
            group="credentialed",
            url="https://data.sec.gov/submissions/CIK0000320193.json",
            endpoint_label="data.sec.gov/submissions/CIK0000320193.json",
            validator=validate_sec,
            headers={"User-Agent": sec_user_agent, "Accept": "application/json"},
        )

    if configured("BEA_API_KEY"):
        params = urllib.parse.urlencode(
            {
                "UserID": os.environ["BEA_API_KEY"].strip(),
                "method": "GETDATASETLIST",
                "ResultFormat": "JSON",
            }
        )
        add_http(
            order=110,
            probe_id="bea_api",
            name="BEA Data API",
            group="credentialed",
            url=f"https://apps.bea.gov/api/data?{params}",
            endpoint_label="apps.bea.gov/api/data",
            validator=validate_bea,
            headers={"Accept": "application/json"},
        )
    else:
        immediate.append(blocked(110, "bea_api", "BEA Data API", "apps.bea.gov/api/data", ["BEA_API_KEY"]))

    if configured("MARKETAUX_API_TOKEN"):
        params = urllib.parse.urlencode(
            {
                "symbols": "AAPL",
                "filter_entities": "true",
                "limit": "1",
                "api_token": os.environ["MARKETAUX_API_TOKEN"].strip(),
            }
        )
        add_http(
            order=120,
            probe_id="marketaux_news",
            name="Marketaux News API",
            group="credentialed",
            url=f"https://api.marketaux.com/v1/news/all?{params}",
            endpoint_label="api.marketaux.com/v1/news/all",
            validator=validate_marketaux,
            headers={"Accept": "application/json"},
        )
    else:
        immediate.append(
            blocked(
                120,
                "marketaux_news",
                "Marketaux News API",
                "api.marketaux.com/v1/news/all",
                ["MARKETAUX_API_TOKEN"],
            )
        )

    if configured("TWELVE_DATA_API_KEY"):
        params = urllib.parse.urlencode(
            {
                "symbol": "AAPL,SPY,EUR/USD,BTC/USD",
                "apikey": os.environ["TWELVE_DATA_API_KEY"].strip(),
            }
        )
        add_http(
            order=125,
            probe_id="twelve_data_multiasset",
            name="Twelve Data Multi-Asset Prices",
            group="credentialed",
            url=f"https://api.twelvedata.com/price?{params}",
            endpoint_label="api.twelvedata.com/price?symbol=<stock,ETF,FX,crypto>&apikey=<redacted>",
            validator=validate_twelve_data_multiasset,
            headers={"Accept": "application/json"},
        )
    else:
        immediate.append(
            blocked(
                125,
                "twelve_data_multiasset",
                "Twelve Data Multi-Asset Prices",
                "api.twelvedata.com/price",
                ["TWELVE_DATA_API_KEY"],
            )
        )

    alpaca_env = ["APCA_API_KEY_ID", "APCA_API_SECRET_KEY"]
    if all(configured(name) for name in alpaca_env):
        alpaca_headers = {
            "APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"].strip(),
            "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"].strip(),
            "Accept": "application/json",
        }
        add_http(
            order=130,
            probe_id="alpaca_iex_snapshot",
            name="Alpaca IEX Snapshot",
            group="credentialed",
            url="https://data.alpaca.markets/v2/stocks/AAPL/snapshot?feed=iex",
            endpoint_label="data.alpaca.markets/v2/stocks/AAPL/snapshot?feed=iex",
            validator=validate_alpaca_snapshot,
            headers=alpaca_headers,
        )
        add_http(
            order=140,
            probe_id="alpaca_news",
            name="Alpaca Historical News",
            group="credentialed",
            url="https://data.alpaca.markets/v1beta1/news?symbols=AAPL&limit=1",
            endpoint_label="data.alpaca.markets/v1beta1/news",
            validator=validate_alpaca_news,
            headers=alpaca_headers,
        )
    else:
        immediate.extend(
            [
                blocked(
                    130,
                    "alpaca_iex_snapshot",
                    "Alpaca IEX Snapshot",
                    "data.alpaca.markets/v2/stocks/AAPL/snapshot?feed=iex",
                    alpaca_env,
                ),
                blocked(
                    140,
                    "alpaca_news",
                    "Alpaca Historical News",
                    "data.alpaca.markets/v1beta1/news",
                    alpaca_env,
                ),
            ]
        )

    if configured("TELEGRAM_BOT_TOKEN"):
        token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
        add_http(
            order=150,
            probe_id="telegram_get_me",
            name="Telegram Bot getMe",
            group="credentialed",
            url=f"https://api.telegram.org/bot{token}/getMe",
            endpoint_label="api.telegram.org/bot<redacted>/getMe",
            validator=validate_telegram,
            headers={"Accept": "application/json"},
        )
        if configured("TELEGRAM_CHAT_ID"):
            chat_id = os.environ["TELEGRAM_CHAT_ID"].strip()
            data = urllib.parse.urlencode({"chat_id": chat_id}).encode("utf-8")
            add_http(
                order=160,
                probe_id="telegram_get_chat",
                name="Telegram Bot getChat",
                group="credentialed",
                url=f"https://api.telegram.org/bot{token}/getChat",
                endpoint_label="api.telegram.org/bot<redacted>/getChat",
                validator=validate_telegram,
                method="POST",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            )
        else:
            immediate.append(
                blocked(
                    160,
                    "telegram_get_chat",
                    "Telegram Bot getChat",
                    "api.telegram.org/bot<redacted>/getChat",
                    ["TELEGRAM_CHAT_ID"],
                )
            )
    else:
        immediate.extend(
            [
                blocked(
                    150,
                    "telegram_get_me",
                    "Telegram Bot getMe",
                    "api.telegram.org/bot<redacted>/getMe",
                    ["TELEGRAM_BOT_TOKEN"],
                ),
                blocked(
                    160,
                    "telegram_get_chat",
                    "Telegram Bot getChat",
                    "api.telegram.org/bot<redacted>/getChat",
                    ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"],
                ),
            ]
        )

    if configured("FRED_API_KEY"):
        params = urllib.parse.urlencode(
            {
                "series_id": "CPIAUCSL",
                "api_key": os.environ["FRED_API_KEY"].strip(),
                "file_type": "json",
                "limit": "1",
                "sort_order": "desc",
            }
        )
        add_http(
            order=170,
            probe_id="fred_api",
            name="FRED API",
            group="credentialed",
            url=f"https://api.stlouisfed.org/fred/series/observations?{params}",
            endpoint_label="api.stlouisfed.org/fred/series/observations",
            validator=validate_fred,
            headers={"Accept": "application/json"},
        )
    else:
        immediate.append(
            blocked(
                170,
                "fred_api",
                "FRED API",
                "api.stlouisfed.org/fred/series/observations",
                ["FRED_API_KEY"],
            )
        )

    return jobs, immediate


def markdown_report(payload: dict[str, Any]) -> str:
    def escape(value: Any) -> str:
        return safe_text(value).replace("|", "\\|")

    lines = [
        "# Gate 0 External Dependency Preflight",
        "",
        f"- Run (UTC): `{payload['run']['finished_at_utc']}`",
        f"- Python: `{payload['run']['python_version']}`",
        f"- PASS: **{payload['summary']['PASS']}**",
        f"- WARN: **{payload['summary']['WARN']}**",
        f"- FAIL: **{payload['summary']['FAIL']}**",
        f"- BLOCKED: **{payload['summary']['BLOCKED']}**",
        "",
        "`BLOCKED` means the endpoint was intentionally not called because a required identity, API key, or destination was absent. It is not an API failure.",
        "",
        "| Status | Probe | Group | Latency | HTTP | Evidence |",
        "|---|---|---|---:|---:|---|",
    ]
    for result in payload["results"]:
        latency = f"{result['latency_ms']} ms" if result["latency_ms"] is not None else "-"
        http_status = result["http_status"] if result["http_status"] is not None else "-"
        lines.append(
            f"| {result['status']} | {escape(result['name'])} | {result['group']} | {latency} | {http_status} | {escape(result['summary'])} |"
        )
    lines.extend(
        [
            "",
            "## Configuration state",
            "",
            "Only presence/absence is reported; secret values are never persisted.",
            "",
        ]
    )
    for name, present in payload["configuration"].items():
        lines.append(f"- `{name}`: {'configured' if present else 'missing'}")
    lines.append("")
    return "\n".join(lines)


def write_reports(output_dir: Path, payload: dict[str, Any]) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    md_text = markdown_report(payload)
    json_path = output_dir / f"gate0_{stamp}.json"
    md_path = output_dir / f"gate0_{stamp}.md"
    json_path.write_text(json_text, encoding="utf-8")
    md_path.write_text(md_text, encoding="utf-8")
    (output_dir / "latest.json").write_text(json_text, encoding="utf-8")
    (output_dir / "latest.md").write_text(md_text, encoding="utf-8")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env", help="Optional dotenv file (default: .env)")
    parser.add_argument("--output-dir", default="reports/gate0", help="Report directory")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--strict-blocked",
        action="store_true",
        help="Return non-zero when any credentialed probe is BLOCKED",
    )
    args = parser.parse_args()

    load_dotenv(Path(args.env_file))
    started_at = utc_now()
    jobs, results = build_probe_jobs(args.timeout)
    max_workers = min(8, max(1, len(jobs)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(job) for job in jobs]
        for future in concurrent.futures.as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append(
                    ProbeResult(
                        order=9999,
                        probe_id="internal_probe_error",
                        name="Internal probe runner",
                        group="internal",
                        status="FAIL",
                        endpoint="local",
                        started_at_utc=utc_now(),
                        summary=f"{type(exc).__name__}: {safe_text(exc)}",
                    )
                )
    results.sort(key=lambda result: result.order)

    config_names = [
        "SEC_USER_AGENT",
        "BLS_API_KEY",
        "BEA_API_KEY",
        "MARKETAUX_API_TOKEN",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "FRED_API_KEY",
        "TWELVE_DATA_API_KEY",
        "BINANCE_REMOTE_SSH_HOST",
        "BINANCE_REMOTE_SSH_PORT",
        "BINANCE_REMOTE_SSH_USER",
        "BINANCE_REMOTE_SSH_KEY",
    ]
    summary = {
        status: sum(result.status == status for result in results)
        for status in ("PASS", "WARN", "FAIL", "BLOCKED")
    }
    payload = {
        "run": {
            "started_at_utc": started_at,
            "finished_at_utc": utc_now(),
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "timeout_seconds": args.timeout,
        },
        "summary": summary,
        "configuration": {name: configured(name) for name in config_names},
        "results": [result.as_dict() for result in results],
    }
    json_path, md_path = write_reports(Path(args.output_dir), payload)

    print(
        f"PASS={summary['PASS']} WARN={summary['WARN']} "
        f"FAIL={summary['FAIL']} BLOCKED={summary['BLOCKED']}"
    )
    for result in results:
        latency = f" {result.latency_ms}ms" if result.latency_ms is not None else ""
        print(f"[{result.status:<7}] {result.probe_id}{latency} - {result.summary}")
    print(f"JSON={json_path.resolve()}")
    print(f"MARKDOWN={md_path.resolve()}")

    if summary["FAIL"]:
        return 1
    if args.strict_blocked and summary["BLOCKED"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
