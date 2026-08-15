from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = os.environ.get("FINANCE_RADAR_AUDIT_API_URL")
ROUTES = (
    "/api/v1/health",
    "/api/v1/overview",
    "/api/v1/events?status=verified&limit=10",
)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def request_once(client: httpx.Client, base: str, route: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = client.get(f"{base}{route}")
        payload = response.json()
        status = response.status_code
        valid_envelope = "schema_version" in payload and "trace_id" in payload and "generated_at" in payload
        error = None if status == 200 and valid_envelope else "invalid_response_envelope"
    except Exception as exc:
        status = 0
        valid_envelope = False
        error = f"{type(exc).__name__}: {exc}"
    return {
        "route": route.split("?", 1)[0],
        "status": status,
        "valid_envelope": valid_envelope,
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a bounded read-only smoke load against the loopback API.")
    parser.add_argument("--base-url", default=DEFAULT_BASE, required=DEFAULT_BASE is None)
    parser.add_argument("--requests", type=int, default=60)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "smoke_load_latest.json")
    args = parser.parse_args()
    total = max(1, min(args.requests, 500))
    concurrency = max(1, min(args.concurrency, 20, total))
    base = args.base_url.rstrip("/")

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    timeout = httpx.Timeout(args.timeout, connect=args.timeout)
    with httpx.Client(
        http2=False,
        timeout=timeout,
        limits=limits,
        trust_env=False,
        headers={"Accept": "application/json", "User-Agent": "FinanceRadar-SmokeLoad/1.0"},
    ) as client:
        for route in ROUTES:
            warmup = request_once(client, base, route)
            if warmup["error"]:
                print(json.dumps({"passed": False, "phase": "warmup", "result": warmup}, ensure_ascii=False, indent=2))
                return 2

        started = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(request_once, client, base, ROUTES[index % len(ROUTES)]) for index in range(total)]
            results = [future.result() for future in concurrent.futures.as_completed(futures)]
        elapsed = time.perf_counter() - started
    latencies = [result["latency_ms"] for result in results]
    errors = [result for result in results if result["error"]]
    status_counts = Counter(str(result["status"]) for result in results)
    route_summaries = {}
    for route in {result["route"] for result in results}:
        route_values = [result["latency_ms"] for result in results if result["route"] == route]
        route_summaries[route] = {
            "requests": len(route_values),
            "p50_ms": round(percentile(route_values, 0.50), 3),
            "p95_ms": round(percentile(route_values, 0.95), 3),
            "max_ms": round(max(route_values), 3),
        }
    success_rate = (total - len(errors)) / total
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": base,
        "read_only": True,
        "passed": success_rate >= 0.99 and percentile(latencies, 0.95) < 5000,
        "requests": total,
        "concurrency": concurrency,
        "elapsed_seconds": round(elapsed, 3),
        "throughput_requests_per_second": round(total / elapsed, 3),
        "success_rate": success_rate,
        "status_counts": dict(status_counts),
        "latency_ms": {
            "mean": round(statistics.fmean(latencies), 3),
            "p50": round(percentile(latencies, 0.50), 3),
            "p95": round(percentile(latencies, 0.95), 3),
            "p99": round(percentile(latencies, 0.99), 3),
            "max": round(max(latencies), 3),
        },
        "routes": route_summaries,
        "errors": errors[:10],
        "pass_policy": {"minimum_success_rate": 0.99, "maximum_p95_ms": 5000},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
