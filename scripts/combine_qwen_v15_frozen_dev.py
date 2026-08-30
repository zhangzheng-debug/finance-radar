#!/usr/bin/env python3
"""Combine already-frozen v15 DEV components without changing membership."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.freeze_qwen_v15_deepseek_dev import (  # noqa: E402
    CONTRACT_VERSION as COMPONENT_CONTRACT,
    LABEL_CLASSIFICATION,
    MANIFEST_NAME,
    OUTPUT_NAME,
    sha256_bytes,
    sha256_file,
    stable_json,
    write_with_sidecar,
)


CONTRACT_VERSION = "qwen-v15-combined-frozen-dev-v1"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _read_rows(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"non-empty object JSONL required: {path}")
    return rows


def combine(*, component_dirs: list[Path], output_dir: Path, minimum_rows: int) -> dict[str, Any]:
    if len(component_dirs) < 2:
        raise ValueError("at least two frozen DEV components are required")
    if output_dir.exists():
        raise FileExistsError(output_dir)
    rows: list[dict[str, Any]] = []
    inputs: list[dict[str, Any]] = []
    seen = {key: set() for key in ("sample_id", "entity_group", "event_chain_group", "source_content_sha256")}
    for directory in component_dirs:
        manifest_path = directory / MANIFEST_NAME
        dataset_path = directory / OUTPUT_NAME
        manifest = _read_json(manifest_path)
        if manifest.get("contract_version") != COMPONENT_CONTRACT:
            raise ValueError(f"unexpected component contract: {directory}")
        if manifest.get("label_classification") != LABEL_CLASSIFICATION:
            raise ValueError(f"unexpected component label class: {directory}")
        if manifest.get("output", {}).get("sha256") != sha256_file(dataset_path):
            raise ValueError(f"component dataset hash mismatch: {directory}")
        component_rows = _read_rows(dataset_path)
        if len(component_rows) != manifest.get("row_count"):
            raise ValueError(f"component row count mismatch: {directory}")
        for row in component_rows:
            metadata = row.get("metadata")
            if not isinstance(metadata, dict) or metadata.get("split") != "DEV":
                raise ValueError(f"component contains non-DEV row: {directory}")
            for key in seen:
                value = str(metadata.get(key) or "").strip()
                if not value or value in seen[key]:
                    raise ValueError(f"blank or cross-component duplicate {key}: {value}")
                seen[key].add(value)
            rows.append(row)
        inputs.append({
            "path": str(directory),
            "manifest_sha256": sha256_file(manifest_path),
            "dataset_sha256": sha256_file(dataset_path),
            "rows": len(component_rows),
        })
    rows.sort(key=lambda row: row["metadata"]["sample_id"])
    if len(rows) < minimum_rows:
        raise ValueError(f"combined DEV has {len(rows)} rows; minimum is {minimum_rows}")
    materiality: Counter[str] = Counter()
    polarity: Counter[str] = Counter()
    for row in rows:
        target = json.loads(row["messages"][-1]["content"])
        materiality[str(target["materiality"])] += 1
        polarity[str(target["polarity"])] += 1

    stage = output_dir.parent / f".{output_dir.name}.staging"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    try:
        raw = "".join(stable_json(row) + "\n" for row in rows).encode("utf-8")
        output = write_with_sidecar(stage / OUTPUT_NAME, raw)
        manifest = {
            "schema_version": 1,
            "contract_version": CONTRACT_VERSION,
            "dataset_role": "DEV_SELECTION_ONLY",
            "label_classification": LABEL_CLASSIFICATION,
            "human_gold_claimed": False,
            "membership_policy": "UNION_OF_PRE_FROZEN_COMPONENTS_NO_LABEL_FILTERING",
            "minimum_rows": minimum_rows,
            "row_count": len(rows),
            "sample_ids_sha256": sha256_bytes(stable_json(sorted(seen["sample_id"])).encode()),
            "zero_cross_component_overlap": {
                "sample_id": True,
                "entity_group": True,
                "event_chain_group": True,
                "source_content_sha256": True,
            },
            "distributions": {
                "materiality": dict(sorted(materiality.items())),
                "polarity": dict(sorted(polarity.items())),
            },
            "inputs": inputs,
            "output": output,
        }
        manifest_raw = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        write_with_sidecar(stage / MANIFEST_NAME, manifest_raw)
        os.replace(stage, output_dir)
    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        raise
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component-dir", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--minimum-rows", type=int, default=200)
    args = parser.parse_args(argv)
    manifest = combine(
        component_dirs=[path.resolve() for path in args.component_dir],
        output_dir=args.output_dir.resolve(),
        minimum_rows=args.minimum_rows,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
