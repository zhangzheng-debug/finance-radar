from __future__ import annotations

import argparse
import os
import time

from app.config import Settings
from app.ops.backup import create_and_verify
from app.storage import OperationsRepository


def main() -> int:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=float(os.getenv("FINANCE_RADAR_BACKUP_INTERVAL", "86400")))
    parser.add_argument("--retention", type=int, default=int(os.getenv("FINANCE_RADAR_BACKUP_RETENTION", "1")))
    parser.add_argument(
        "--weekly-retention",
        type=int,
        default=int(os.getenv("FINANCE_RADAR_WEEKLY_BACKUP_RETENTION", "0")),
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    operations = OperationsRepository(settings.operations_db)
    backup_dir = settings.ledger_db.parent / "operational_backups"
    exit_code = 0
    while True:
        try:
            result = create_and_verify(
                settings.ledger_db,
                backup_dir,
                operations,
                retention=args.retention,
                weekly_retention=args.weekly_retention,
            )
            print(result, flush=True)
        except Exception as exc:
            exit_code = 1
            print(f"backup_failed={type(exc).__name__}: {exc}", flush=True)
        if args.once:
            return exit_code
        time.sleep(max(300, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
