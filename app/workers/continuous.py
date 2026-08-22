from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
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


def release_owned_cycle_lease(db_path: Path, token: str) -> bool:
    """Release only the lease token assigned to a timed-out child process."""

    connection = sqlite3.connect(db_path, timeout=5)
    try:
        cursor = connection.execute(
            "DELETE FROM runtime_leases WHERE lease_name='live_cycle' AND lease_token=?",
            (token,),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def process_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def execute_cycle(
    settings: Settings,
    operations: OperationsRepository,
    *,
    send: bool,
    timeout: float,
    health_only: bool,
    light_enabled: bool = False,
    light_limit: int = 25,
    light_daily_budget: int = 100,
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
            lease_token = uuid.uuid4().hex
            command = [
                sys.executable,
                str(ROOT / "scripts" / "run_live_cycle.py"),
                "--db",
                str(settings.ledger_db),
                "--report",
                str(report_path),
                "--timeout",
                str(timeout),
                "--lease-token",
                lease_token,
            ]
            if send:
                command.append("--send")
            # A failed child must never inherit the previous cycle's JSON and
            # be misclassified as a current partial success.
            report_path.unlink(missing_ok=True)
            child_timeout = max(120, int(timeout * 20))
            timed_out = False
            lease_released_after_timeout = False
            lease_release_error: str | None = None
            try:
                completed = subprocess.run(
                    command,
                    cwd=ROOT,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=child_timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                try:
                    lease_released_after_timeout = release_owned_cycle_lease(
                        Path(settings.ledger_db), lease_token
                    )
                except Exception as release_exc:
                    lease_release_error = type(release_exc).__name__
                completed = subprocess.CompletedProcess(
                    command,
                    124,
                    stdout=process_text(exc.stdout),
                    stderr=process_text(exc.stderr),
                )
            result = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
            result["process"] = {
                "returncode": completed.returncode,
                "stdout_tail": process_text(completed.stdout)[-2000:],
                "stderr_tail": process_text(completed.stderr)[-2000:],
                "telegram_send_enabled": send,
                "timed_out": timed_out,
                "timeout_seconds": child_timeout,
                "owned_lease_released": lease_released_after_timeout,
                "lease_release_error_class": lease_release_error,
            }
            if timed_out:
                status = "FAILED"
            elif completed.returncode == 0:
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
            if light_enabled and status in {"SUCCESS", "DEGRADED"}:
                # This worker is deliberately observation-only.  Formal light
                # verification is a separately invoked, expiring scoped batch;
                # never put an evergreen authorization or ``--apply`` here.
                light_report_path = ROOT / "reports" / "light_verification_latest.json"
                light_command = [
                    sys.executable,
                    str(ROOT / "scripts" / "light_verify.py"),
                    "--db",
                    str(settings.ledger_db),
                    "--operations-db",
                    str(settings.operations_db),
                    "--report",
                    str(light_report_path),
                    "--limit",
                    str(max(1, light_limit)),
                    "--max-applies",
                    str(max(1, light_limit)),
                    "--daily-budget",
                    str(max(0, light_daily_budget)),
                ]
                light_report_path.unlink(missing_ok=True)
                light_completed = subprocess.run(
                    light_command,
                    cwd=ROOT,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=max(120, int(timeout * 20)),
                    check=False,
                )
                light_result = (
                    json.loads(light_report_path.read_text(encoding="utf-8"))
                    if light_report_path.is_file()
                    else {}
                )
                light_result["process"] = {
                    "returncode": light_completed.returncode,
                    "stdout_tail": light_completed.stdout[-2000:],
                    "stderr_tail": light_completed.stderr[-2000:],
                }
                result["light_verification"] = light_result
                if light_completed.returncode != 0 and status == "SUCCESS":
                    status = "DEGRADED"
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
    parser.add_argument(
        "--light-verify-dry-run",
        action="store_true",
        help="after each successful ingestion cycle, run read-only light-verification reporting only",
    )
    parser.add_argument(
        "--no-light-verify",
        action="store_true",
        help="explicitly disable even the optional read-only light-verification report (kept for service compatibility)",
    )
    parser.add_argument("--light-limit", type=int, default=int(os.getenv("FINANCE_RADAR_LIGHT_LIMIT", "25")))
    parser.add_argument(
        "--light-daily-budget",
        type=int,
        default=int(os.getenv("FINANCE_RADAR_LIGHT_DAILY_BUDGET", "100")),
    )
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
            light_enabled=bool(args.light_verify_dry_run and not args.no_light_verify),
            light_limit=args.light_limit,
            light_daily_budget=args.light_daily_budget,
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
