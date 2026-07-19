from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

from app.config import ROOT, Settings


def run_once(settings: Settings, *, send: bool) -> int:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "telegram_alert_outbox.py"),
        "--db",
        str(settings.ledger_db),
        "--enqueue",
    ]
    command.append("--send" if send else "--dry-run")
    completed = subprocess.run(command, cwd=ROOT, env=os.environ.copy(), check=False)
    return completed.returncode


def main() -> int:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--send", action="store_true", help="required to perform external Telegram writes")
    args = parser.parse_args()
    while True:
        code = run_once(settings, send=args.send)
        if args.once:
            return code
        time.sleep(max(10, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
