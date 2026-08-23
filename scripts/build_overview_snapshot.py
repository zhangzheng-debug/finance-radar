from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.overview_projection import publish_overview_snapshot
from app.config import Settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Atomically publish the Finance Radar overview data snapshot."
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    settings = Settings.from_env()
    output = args.output or settings.overview_snapshot_path
    if output is None:
        parser.error(
            "--output or FINANCE_RADAR_OVERVIEW_SNAPSHOT_PATH is required"
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
                "no_trading": True,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
