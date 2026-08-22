from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.subjectless_event_cleanup import (
    plan_subjectless_cleanup,
    purge_subjectless_events,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def online_backup(source: sqlite3.Connection, target: Path) -> dict[str, object]:
    target = target.resolve()
    if target.exists():
        raise FileExistsError(f"backup path already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = sqlite3.connect(target)
    try:
        source.backup(backup)
        quick = str(backup.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(backup.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        backup.close()
    if quick != "ok" or integrity != "ok":
        raise RuntimeError(f"backup verification failed: quick={quick} integrity={integrity}")
    return {
        "path": str(target),
        "bytes": target.stat().st_size,
        "sha256": sha256_file(target),
        "quick_check": quick,
        "integrity_check": integrity,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Plan or purge canonical events whose company and ticker are both empty."
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply and args.backup is None:
        parser.error("--apply requires a new --backup path")

    connection = sqlite3.connect(args.db)
    connection.row_factory = sqlite3.Row
    try:
        plan = plan_subjectless_cleanup(connection)
        receipt: dict[str, object] = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "APPLY" if args.apply else "DRY_RUN",
            "database": str(args.db.resolve()),
            "plan": plan.as_dict(),
            "canonical_mutation": bool(args.apply),
            "raw_source_deletion": False,
        }
        if args.apply:
            receipt["backup"] = online_backup(connection, args.backup)
            receipt["result"] = purge_subjectless_events(connection).as_dict()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
