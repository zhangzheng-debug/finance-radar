#!/usr/bin/env python3
"""Capture public runtime evidence into an append-only SHA-256 chain."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_API = os.environ.get("FINANCE_RADAR_AUDIT_API_URL")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stable_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def fetch_health(api_base: str, timeout: float = 30.0) -> dict[str, Any]:
    url = f"{api_base.rstrip('/')}/api/v1/health"
    last_error: Exception | None = None
    with httpx.Client(
        timeout=httpx.Timeout(timeout, connect=timeout),
        trust_env=False,
        headers={"Accept": "application/json", "User-Agent": "FinanceRadar-RuntimeEvidence/1.0"},
    ) as client:
        for attempt in range(3):
            try:
                response = client.get(url)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
                    raise ValueError("health endpoint returned an invalid envelope")
                return payload["data"]
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"public health request failed after three attempts: {last_error}")


def build_snapshot(
    health: dict[str, Any],
    *,
    api_base: str,
    captured_at: datetime,
    known_last_non_success: datetime | None = None,
) -> dict[str, Any]:
    ledger = health.get("ledger") or {}
    operations = health.get("operations") or {}
    window = operations.get("worker_window_24h") or {}
    latest_worker = operations.get("latest_worker_cycle") or {}
    latest_backup = operations.get("latest_backup") or {}
    model = health.get("model") or {}
    audits = ledger.get("audit") or {}
    known_eligible_after = (
        known_last_non_success + timedelta(hours=24) if known_last_non_success else None
    )
    checks = {
        "api_status_ok": health.get("status") == "ok",
        "ledger_quick_check_ok": ledger.get("quick_check") == "ok",
        "operations_quick_check_ok": operations.get("quick_check") == "ok",
        "latest_worker_success": latest_worker.get("status") == "SUCCESS",
        "latest_backup_verified": latest_backup.get("status") == "VERIFIED",
        "model_shadow_no_trading": model.get("shadow") is True and model.get("no_trading") is True,
        "safety_audits_zero": bool(audits) and sum(int(value) for value in audits.values()) == 0,
        "runtime_window_complete": window.get("complete") is True,
    }
    return {
        "schema_version": 1,
        "captured_at": captured_at.astimezone(timezone.utc).isoformat(),
        "target_api": api_base.rstrip("/"),
        "status": "PASS" if all(checks.values()) else "WAITING",
        "checks": checks,
        "worker_window_24h": window,
        "latest_worker": {
            "cycle_id": latest_worker.get("cycle_id"),
            "status": latest_worker.get("status"),
            "started_at": latest_worker.get("started_at"),
            "finished_at": latest_worker.get("finished_at"),
            "error": latest_worker.get("error"),
        },
        "ledger_counts": ledger.get("counts") or {},
        "operations_counts": operations.get("counts") or {},
        "safety_audit": audits,
        "known_last_non_success": (
            known_last_non_success.astimezone(timezone.utc).isoformat()
            if known_last_non_success
            else None
        ),
        "known_earliest_possible_pass": (
            known_eligible_after.astimezone(timezone.utc).isoformat()
            if known_eligible_after
            else None
        ),
        "eligibility_note": (
            "Earliest known time only; any new non-success cycle moves the gate later."
            if known_eligible_after
            else None
        ),
    }


def validate_history(path: Path) -> tuple[int, str | None]:
    if not path.exists():
        return 0, None
    previous: str | None = None
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"runtime evidence history line {line_number} is invalid JSON") from exc
            claimed = record.pop("record_sha256", None)
            if record.get("previous_record_sha256") != previous:
                raise ValueError(f"runtime evidence history chain breaks at line {line_number}")
            actual = hashlib.sha256(stable_json(record).encode("utf-8")).hexdigest()
            if claimed != actual:
                raise ValueError(f"runtime evidence history hash mismatch at line {line_number}")
            previous = claimed
            count += 1
    return count, previous


def append_snapshot(history_path: Path, snapshot: dict[str, Any]) -> dict[str, Any]:
    history_path = history_path.resolve()
    history_path.parent.mkdir(parents=True, exist_ok=True)
    count, previous = validate_history(history_path)
    record = dict(snapshot)
    record["sequence"] = count + 1
    record["previous_record_sha256"] = previous
    record["record_sha256"] = hashlib.sha256(stable_json(record).encode("utf-8")).hexdigest()
    with history_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(stable_json(record) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    validate_history(history_path)
    return record


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def render_markdown(record: dict[str, Any]) -> str:
    window = record.get("worker_window_24h") or {}
    checks = record.get("checks") or {}
    counts = record.get("ledger_counts") or {}
    lines = [
        "# Finance Radar 24-hour runtime evidence",
        "",
        f"- Captured: `{record.get('captured_at')}`",
        f"- Gate: **{record.get('status')}**",
        f"- Chain sequence: `{record.get('sequence')}`",
        f"- Record SHA-256: `{record.get('record_sha256')}`",
        f"- Observed window: `{window.get('observed_hours', 0)}` / `24` hours",
        f"- Cycles: `{window.get('cycles', 0)}`; success rate: `{window.get('success_rate', 0)}`",
        f"- SUCCESS / DEGRADED / FAILED: `{window.get('success_cycles', 0)}` / `{window.get('degraded_cycles', 0)}` / `{window.get('failed_cycles', 0)}`",
        f"- Latest Worker: `{(record.get('latest_worker') or {}).get('status')}`",
        f"- Earliest known possible pass: `{record.get('known_earliest_possible_pass') or 'unknown'}`",
        "",
        "## Gate checks",
        "",
    ]
    lines.extend(f"- [{'x' if passed else ' '}] `{name}`" for name, passed in checks.items())
    lines.extend(
        [
            "",
            "## Ledger snapshot",
            "",
            f"- Sources: `{counts.get('sources', 0)}`",
            f"- Raw observations: `{counts.get('raw_observations', 0)}`",
            f"- Canonical events: `{counts.get('canonical_events', 0)}`",
            f"- Event versions: `{counts.get('event_versions', 0)}`",
            f"- Evidence rows: `{counts.get('event_evidence', 0)}`",
            "",
            "`PASS` is emitted only when every safety/health check and the persisted 24-hour Worker gate are true. A known eligibility time is a lower bound, not a promise.",
            "",
        ]
    )
    return "\n".join(lines)


def capture_once(
    *,
    api_base: str,
    output_dir: Path,
    known_last_non_success: datetime | None,
    fetcher: Callable[[str], dict[str, Any]] = fetch_health,
) -> dict[str, Any]:
    health = fetcher(api_base)
    snapshot = build_snapshot(
        health,
        api_base=api_base,
        captured_at=utc_now(),
        known_last_non_success=known_last_non_success,
    )
    output_dir = output_dir.resolve()
    history = output_dir / "runtime_gate_history.jsonl"
    record = append_snapshot(history, snapshot)
    atomic_json(output_dir / "runtime_gate_latest.json", record)
    markdown = output_dir / "runtime_gate_latest.md"
    temporary = markdown.with_suffix(".md.tmp")
    temporary.write_text(render_markdown(record), encoding="utf-8")
    os.replace(temporary, markdown)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default=DEFAULT_API, required=DEFAULT_API is None)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports" / "runtime_evidence")
    parser.add_argument("--known-last-non-success")
    args = parser.parse_args()
    record = capture_once(
        api_base=args.api_base,
        output_dir=args.output_dir,
        known_last_non_success=parse_time(args.known_last_non_success),
    )
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
