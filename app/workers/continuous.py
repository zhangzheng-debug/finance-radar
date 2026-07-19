from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import ROOT, Settings
from app.storage import LedgerRepository, OperationsRepository


STOP_REQUESTED = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_stop(signum: int, frame: Any) -> None:
    del signum, frame
    global STOP_REQUESTED
    STOP_REQUESTED = True


def execute_cycle(
    settings: Settings,
    operations: OperationsRepository,
    *,
    send: bool,
    timeout: float,
    health_only: bool,
) -> tuple[str, dict[str, Any]]:
    cycle_id = operations.start_worker_cycle()
    started = time.perf_counter()
    try:
        if health_only:
            result = {
                "mode": "health_only",
                "ledger": LedgerRepository(settings.ledger_db).health(),
                "started_at": utc_now(),
                "finished_at": utc_now(),
            }
            status = "SUCCESS"
        else:
            report_path = ROOT / "reports" / "live_cycle_latest.json"
            command = [
                sys.executable,
                str(ROOT / "scripts" / "run_live_cycle.py"),
                "--db",
                str(settings.ledger_db),
                "--report",
                str(report_path),
                "--timeout",
                str(timeout),
            ]
            if send:
                command.append("--send")
            # A failed child must never inherit the previous cycle's JSON and
            # be misclassified as a current partial success.
            report_path.unlink(missing_ok=True)
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(120, int(timeout * 20)),
                check=False,
            )
            result = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
            result["process"] = {
                "returncode": completed.returncode,
                "stdout_tail": completed.stdout[-2000:],
                "stderr_tail": completed.stderr[-2000:],
                "telegram_send_enabled": send,
            }
            if completed.returncode == 0:
                status = "SUCCESS"
            elif completed.returncode == 3:
                status = "SKIPPED"
            elif result.get("finished_at") and (
                result.get("official_sources") is not None or result.get("candidate_extraction") is not None
            ):
                # A source may fail while the remaining sources, evidence routing,
                # and durable writes complete. Surface that as degraded service,
                # not as a total pipeline failure.
                status = "DEGRADED"
            else:
                status = "FAILED"
        result["worker_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        operations.finish_worker_cycle(cycle_id, status, result)
        operations.set_state(
            "worker_heartbeat",
            {"cycle_id": cycle_id, "status": status, "at": utc_now(), "send": send},
        )
        return status, result
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        result = {"error": error, "worker_elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}
        operations.finish_worker_cycle(cycle_id, "FAILED", result, error)
        operations.set_state("worker_heartbeat", {"cycle_id": cycle_id, "status": "FAILED", "at": utc_now(), "error": error})
        return "FAILED", result


def main() -> int:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=float(os.getenv("FINANCE_RADAR_WORKER_INTERVAL", "300")))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-cycles", type=int)
    parser.add_argument("--health-only", action="store_true", help="record a local health cycle without external network calls")
    parser.add_argument("--send", action="store_true", help="explicitly enable Telegram delivery")
    args = parser.parse_args()
    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    operations = OperationsRepository(settings.operations_db)
    cycles = 0
    exit_code = 0
    while not STOP_REQUESTED:
        status, result = execute_cycle(
            settings,
            operations,
            send=args.send,
            timeout=args.timeout,
            health_only=args.health_only,
        )
        print(json.dumps({"status": status, "result": result}, ensure_ascii=False), flush=True)
        cycles += 1
        if status == "FAILED":
            exit_code = 1
        if args.once or (args.max_cycles and cycles >= args.max_cycles):
            break
        deadline = time.monotonic() + max(5, args.interval)
        while not STOP_REQUESTED and time.monotonic() < deadline:
            time.sleep(min(1, deadline - time.monotonic()))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
