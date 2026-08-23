from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.overview_projection import publish_overview_snapshot
from app.config import Settings
from app.storage import OperationsRepository


def _active_worker_cycle_is_recent(
    cycle: dict[str, object] | None,
    *,
    now: datetime,
    stale_after_seconds: float,
) -> bool:
    if not cycle or cycle.get("status") != "RUNNING":
        return False
    try:
        started = datetime.fromisoformat(
            str(cycle.get("started_at") or "").replace("Z", "+00:00")
        )
    except ValueError:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    age_seconds = max(0.0, (now - started.astimezone(timezone.utc)).total_seconds())
    return age_seconds <= max(1.0, stale_after_seconds)


def wait_for_worker_idle(
    settings: Settings,
    *,
    timeout_seconds: float,
    poll_seconds: float,
    stale_after_seconds: float = 900.0,
) -> dict[str, object]:
    """Avoid rebuilding the read projection during a live collection burst.

    The old snapshot remains readable while this lightweight gate waits.  A
    stale RUNNING row cannot block publication forever, and timeout remains a
    bounded fallback rather than a hard dependency on worker health.
    """

    timeout_seconds = max(0.0, float(timeout_seconds))
    poll_seconds = max(0.25, float(poll_seconds))
    started = time.monotonic()
    waited = False
    operations = OperationsRepository(settings.operations_db)
    while True:
        cycle = operations.latest_worker_cycle_summary()
        active = _active_worker_cycle_is_recent(
            cycle,
            now=datetime.now(timezone.utc),
            stale_after_seconds=stale_after_seconds,
        )
        elapsed = time.monotonic() - started
        if not active:
            return {
                "status": "IDLE",
                "waited": waited,
                "wait_seconds": round(elapsed, 3),
                "cycle_id": cycle.get("cycle_id") if cycle else None,
            }
        if elapsed >= timeout_seconds:
            return {
                "status": "TIMEOUT_PROCEEDING",
                "waited": True,
                "wait_seconds": round(elapsed, 3),
                "cycle_id": cycle.get("cycle_id"),
            }
        waited = True
        time.sleep(min(poll_seconds, max(0.0, timeout_seconds - elapsed)))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Atomically publish the Finance Radar overview data snapshot."
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--wait-for-worker-idle-seconds", type=float, default=0.0)
    parser.add_argument("--worker-idle-poll-seconds", type=float, default=5.0)
    args = parser.parse_args()

    settings = Settings.from_env()
    output = args.output or settings.overview_snapshot_path
    if output is None:
        parser.error(
            "--output or FINANCE_RADAR_OVERVIEW_SNAPSHOT_PATH is required"
        )
    worker_gate = wait_for_worker_idle(
        settings,
        timeout_seconds=args.wait_for_worker_idle_seconds,
        poll_seconds=args.worker_idle_poll_seconds,
    )
    envelope = publish_overview_snapshot(settings, output)
    print(
        json.dumps(
            {
                "status": "PUBLISHED",
                "schema": envelope["schema"],
                "computed_at": envelope["computed_at"],
                "build_duration_seconds": envelope["build_duration_seconds"],
                "payload_sha256": envelope["payload_sha256"],
                "worker_gate": worker_gate,
                "no_trading": True,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
