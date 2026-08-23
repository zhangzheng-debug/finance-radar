from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.encoders import jsonable_encoder

from app.config import Settings
from app.storage import LedgerRepository, OperationsRepository


OVERVIEW_SNAPSHOT_SCHEMA = "finance-radar-overview-snapshot-v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_overview_payload(
    settings: Settings,
    *,
    ledger: LedgerRepository | None = None,
    operations: OperationsRepository | None = None,
) -> dict[str, Any]:
    """Build the complete mutable input projection used by ``/overview``."""

    ledger = ledger or LedgerRepository(settings.ledger_db)
    operations = operations or OperationsRepository(settings.operations_db)
    return {
        "overview_base": ledger.overview(run_integrity_check=False),
        "latest_verified_backup": operations.latest_verified_backup(),
        "latest_backup_attempt": operations.latest_backup(),
        "demo_mode": operations.demo_mode(settings.demo_mode),
        "latest_worker_cycle": operations.latest_worker_cycle(),
        "latest_successful_worker_cycle": operations.latest_successful_worker_cycle(),
    }


def payload_sha256(payload: Any) -> str:
    encoded = json.dumps(
        jsonable_encoder(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def snapshot_envelope(
    payload: dict[str, Any],
    *,
    started_at: str,
    duration_seconds: float,
) -> dict[str, Any]:
    serializable = jsonable_encoder(payload)
    return {
        "schema": OVERVIEW_SNAPSHOT_SCHEMA,
        "computed_at": utc_now(),
        "build_started_at": started_at,
        "build_duration_seconds": round(max(0.0, duration_seconds), 3),
        "payload_sha256": payload_sha256(serializable),
        "payload": serializable,
    }


def publish_overview_snapshot(settings: Settings, output_path: Path) -> dict[str, Any]:
    """Compute and atomically publish one server-side overview data artifact."""

    started_at = utc_now()
    started_monotonic = time.monotonic()
    payload = build_overview_payload(settings)
    envelope = snapshot_envelope(
        payload,
        started_at=started_at,
        duration_seconds=time.monotonic() - started_monotonic,
    )
    encoded = (
        json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
        os.replace(temporary_name, output_path)
        temporary_name = None
        if os.name != "nt":
            directory_fd = os.open(output_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass
    return envelope
