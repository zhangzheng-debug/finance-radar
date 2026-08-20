#!/usr/bin/env python3
"""Create 720 leakage-safe human-blind candidates from an AI census package."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.human_blind_candidate_sampler import (  # noqa: E402
    DEFAULT_SEED,
    DEFAULT_TARGET_COUNT,
    build_candidate_set,
    load_packets_from_census_package,
    write_candidate_set,
)


def build_command(args: argparse.Namespace) -> int:
    if args.output.exists():
        raise ValueError(f"output already exists: {args.output}")
    packets, source = load_packets_from_census_package(args.census_package)
    candidate_set = build_candidate_set(
        packets,
        target_count=args.target_count,
        seed=args.seed,
    )
    candidate_set["source_batch_id"] = source["batch_id"]
    candidate_set["source_owner_event_count"] = source["owner_event_count"]
    candidate_set["source_assignment_file_count"] = source["assignment_file_count"]
    write_candidate_set(args.output, candidate_set)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "row_count": candidate_set["row_count"],
                "sample_set_sha256": candidate_set["sample_set_sha256"],
                "source_owner_event_count": source["owner_event_count"],
                "canonical_state_changed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--census-package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-count", type=int, default=DEFAULT_TARGET_COUNT)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    return parser.parse_args()


def main() -> int:
    return build_command(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())

