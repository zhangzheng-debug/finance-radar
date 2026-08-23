#!/usr/bin/env python3
"""Build, validate and merge the read-only ``ai-census-v1`` delivery kit.

There is intentionally no apply command.  The tool opens the source ledger in
read-only mode and every generated or merged result remains advisory-only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.ai_event_census import (  # noqa: E402
    BOUNDARY_VALUES,
    CONTRACT_VERSION,
    OVERLAP_RATE,
    PROMPT_SHA256,
    PROMPT_VERSION,
    SCHEMA_VERSION,
    allocate_packets,
    build_assignment_shards,
    extract_all_event_packets,
    merge_census_submissions,
    read_jsonl,
    utc_now,
    validate_submission_records,
    write_jsonl,
)


ASSET_ROOT = ROOT / "ai_census_kit"
CONTRACT_PATH = ROOT / "config" / "ai_census_v1.json"
DEFAULT_LEDGER = ROOT / "data" / "finance_radar.sqlite3"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_manifest(root: Path) -> Path:
    manifest = root / "SHA256SUMS.csv"
    rows: list[tuple[str, str, int]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path == manifest or path.suffix.lower() == ".zip":
            continue
        rows.append((path.relative_to(root).as_posix(), file_sha256(path), path.stat().st_size))
    with manifest.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["relative_path", "sha256", "bytes"])
        writer.writerows(rows)
    return manifest


def _zip_directory(source: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError(f"zip output already exists: {output}")
    temporary = output.with_suffix(output.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    with zipfile.ZipFile(
        temporary,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, arcname=f"{source.name}/{path.relative_to(source).as_posix()}")
    temporary.replace(output)


def _copy_member_docs(destination: Path, slot: str) -> None:
    shutil.copy2(ASSET_ROOT / "成员操作说明.md", destination / "01_成员操作说明.md")
    shutil.copy2(ASSET_ROOT / "字段判断手册.md", destination / "02_字段判断手册.md")
    shutil.copy2(ASSET_ROOT / "AI审核总提示词.md", destination / "03_AI审核总提示词.md")
    shutil.copy2(ASSET_ROOT / "AI审核工作台.html", destination / "AI审核工作台.html")
    shutil.copy2(CONTRACT_PATH, destination / "ai-census-v1.contract.json")
    (destination / "00_先看我.txt").write_text(
        f"你是成员{slot}。\n\n"
        "1. 先读 01_成员操作说明.md。\n"
        "2. 双击 AI审核工作台.html。\n"
        "3. 按编号选择“任务分片”中的 input.jsonl，逐条复制提示词并粘贴 AI JSON。\n"
        "4. 用工作台导出完整 result.jsonl，再私下交给负责人。\n\n"
        "不要查看另一名成员的答案；不要上传数据库、密钥或服务器文件；"
        "结果不是人工审核、正式核验或交易建议。\n",
        encoding="utf-8-sig",
    )
    result_folder = destination / "结果放这里"
    result_folder.mkdir()
    (result_folder / "README.txt").write_text(
        "把 AI 返回的完整 JSONL 保存为 *.result.jsonl。文件名应与输入分片编号一致。"
        "全部完成后，将这些 result.jsonl 私下交给负责人。\n",
        encoding="utf-8-sig",
    )


def _contract_preflight() -> None:
    contract = read_json(CONTRACT_PATH)
    if contract.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("ai census contract schema_version is out of sync")
    if contract.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("ai census contract_version is out of sync")
    if contract.get("prompt_version") != PROMPT_VERSION:
        raise ValueError("ai census prompt_version is out of sync")
    if contract.get("prompt_sha256") != PROMPT_SHA256:
        raise ValueError("ai census prompt_sha256 is out of sync")
    prompt_text = (ASSET_ROOT / "AI审核总提示词.md").read_text(
        encoding="utf-8-sig"
    ).replace("\r\n", "\n").replace("\r", "\n")
    actual_prompt_sha256 = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    if actual_prompt_sha256 != PROMPT_SHA256:
        raise ValueError("fixed AI census prompt content hash is out of sync")
    allocation = contract.get("allocation") or {}
    if float(allocation.get("deterministic_overlap_rate", -1)) != OVERLAP_RATE:
        raise ValueError("ai census overlap rate is out of sync")
    if (contract.get("mandatory_boundaries") or {}) != BOUNDARY_VALUES:
        raise ValueError("ai census boundary values are out of sync")


def build_command(args: argparse.Namespace) -> int:
    _contract_preflight()
    if args.output.exists():
        raise ValueError(f"output directory already exists: {args.output}")
    ledger = args.ledger.resolve()
    if not ledger.is_file():
        raise ValueError(f"ledger does not exist: {ledger}")
    generated_at = utc_now()
    generated = datetime.fromisoformat(generated_at)
    batch_id = args.batch_id or f"AIC-{generated.strftime('%Y%m%dT%H%M%SZ')}"

    snapshot = extract_all_event_packets(ledger)
    packets = snapshot["packets"]
    allocation = allocate_packets(packets, batch_id=batch_id)
    shards = build_assignment_shards(
        allocation,
        generated_at=generated_at,
        shard_size=args.shard_size,
    )

    args.output.mkdir(parents=True)
    shutil.copy2(CONTRACT_PATH, args.output / "ai-census-v1.contract.json")
    (args.output / "00_负责人先看.md").write_text(
        "# 全量事件 AI 普查交付包\n\n"
        "本包把冻结快照中的全部 canonical 事件分给成员A、B，并让5%的事件由两人重复处理。"
        "成员结果只能用于分流、补证、重复项和历史一致性审计，不会修改正式事件状态。\n\n"
        "请分别把“成员A”和“成员B”文件夹发给对应成员；不要互换。回传后按“负责人材料/负责人接收说明.md”校验和合并。\n",
        encoding="utf-8",
    )
    owner_folder = args.output / "负责人材料"
    owner_folder.mkdir()
    shutil.copy2(ASSET_ROOT / "负责人接收说明.md", owner_folder / "负责人接收说明.md")
    shutil.copy2(CONTRACT_PATH, owner_folder / "ai-census-v1.contract.json")

    for slot in ("A", "B"):
        member_folder = args.output / f"成员{slot}"
        member_folder.mkdir()
        _copy_member_docs(member_folder, slot)
        (member_folder / "任务分片").mkdir()

    assignment_rows: list[dict[str, Any]] = []
    assignments_by_event: dict[str, list[dict[str, str]]] = defaultdict(list)
    shard_counts: Counter[str] = Counter()
    assigned_event_counts: Counter[str] = Counter()
    for records in shards:
        header = records[0]
        slot = str(header["reviewer_slot"])
        shard_id = str(header["shard_id"])
        relative_path = Path(f"成员{slot}") / "任务分片" / f"{shard_id}.input.jsonl"
        write_jsonl(args.output / relative_path, records)
        shard_counts[slot] += 1
        assigned_event_counts[slot] += int(header["event_count"])
        assignment_rows.append(
            {
                "reviewer_slot": slot,
                "shard_id": shard_id,
                "assignment_sha256": header["assignment_sha256"],
                "event_count": header["event_count"],
                "overlap_event_count": header["overlap_event_count"],
                "relative_path": relative_path.as_posix(),
            }
        )
        for packet in records[1:]:
            assignments_by_event[str(packet["event_id"])].append(
                {
                    "reviewer_slot": slot,
                    "shard_id": shard_id,
                    "assignment_sha256": str(header["assignment_sha256"]),
                }
            )

    owner_events: list[dict[str, Any]] = []
    for row in snapshot["owner_events"]:
        event_id = str(row["event_id"])
        owner_events.append({**row, "assignments": assignments_by_event[event_id]})
    owner_index = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "batch_id": batch_id,
        "generated_at": generated_at,
        "event_count": len(packets),
        "logical_snapshot_sha256": snapshot["logical_snapshot_sha256"],
        "overlap_rate": OVERLAP_RATE,
        "overlap_event_ids": allocation["overlap_event_ids"],
        "events": owner_events,
        "assignments": assignment_rows,
        "canonical_mutation_allowed": False,
        "formal_verification": False,
        "human_reviewed": False,
        "no_trading": True,
    }
    write_json(owner_folder / "owner_index.json", owner_index)
    owner_index_sha256 = file_sha256(owner_folder / "owner_index.json")

    ledger_stat = ledger.stat()
    batch_manifest = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "prompt_version": PROMPT_VERSION,
        "batch_id": batch_id,
        "generated_at": generated_at,
        "source_ledger_name": ledger.name,
        "source_ledger_sha256": file_sha256(ledger),
        "source_ledger_bytes": ledger_stat.st_size,
        "source_ledger_modified_at": datetime.fromtimestamp(
            ledger_stat.st_mtime, timezone.utc
        ).isoformat(),
        "source_ledger_event_count": len(packets),
        "logical_snapshot_sha256": snapshot["logical_snapshot_sha256"],
        "owner_index_sha256": owner_index_sha256,
        "source_status_counts": snapshot["status_counts"],
        "source_label_status_counts": snapshot["label_status_counts"],
        "collective_full_coverage": True,
        "overlap_rate": OVERLAP_RATE,
        "overlap_event_count": allocation["overlap_count"],
        "reviewer_assigned_event_counts": dict(assigned_event_counts),
        "reviewer_shard_counts": dict(shard_counts),
        "shard_size": args.shard_size,
        "assignment_count": len(assignment_rows),
        "canonical_state_changed": False,
        "ai_assisted": True,
        "human_reviewed": False,
        "formal_verification": False,
        "canonical_mutation_allowed": False,
        "no_market_outcome": True,
        "no_trading": True,
    }
    write_json(args.output / "batch_manifest.json", batch_manifest)
    write_json(owner_folder / "batch_manifest.json", batch_manifest)
    _write_manifest(args.output)
    if args.zip:
        _zip_directory(args.output, args.zip)
    print(
        json.dumps(
            {
                "batch_id": batch_id,
                "events": len(packets),
                "overlap_events": allocation["overlap_count"],
                "assigned": dict(assigned_event_counts),
                "shards": dict(shard_counts),
                "output": str(args.output.resolve()),
                "zip": str(args.zip.resolve()) if args.zip else None,
                "canonical_state_changed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _owner_event_ids(path: Path | None) -> set[str] | None:
    if path is None:
        return None
    index = read_json(path)
    return {str(row["event_id"]) for row in index.get("events") or []}


def validate_command(args: argparse.Namespace) -> int:
    report = validate_submission_records(
        read_jsonl(args.assignment),
        read_jsonl(args.submission),
        batch_event_ids=_owner_event_ids(args.owner_index),
    )
    if args.report:
        serializable = dict(report)
        serializable.pop("results", None)
        write_json(args.report, serializable)
    printable = dict(report)
    printable.pop("results", None)
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    return 0 if report["valid"] else 2


def _find_submission_paths(directories: list[Path], explicit: list[Path]) -> list[Path]:
    paths = [path.resolve() for path in explicit]
    for directory in directories:
        paths.extend(path.resolve() for path in directory.rglob("*.result.jsonl"))
    unique: dict[str, Path] = {}
    for path in paths:
        if not path.is_file():
            raise ValueError(f"submission does not exist: {path}")
        unique[str(path).casefold()] = path
    return [unique[key] for key in sorted(unique)]


def merge_command(args: argparse.Namespace) -> int:
    package_root = args.package_root.resolve()
    owner_index = read_json(package_root / "负责人材料" / "owner_index.json")
    assignment_paths = sorted(package_root.glob("成员*/任务分片/*.input.jsonl"))
    if not assignment_paths:
        raise ValueError("no assignment JSONL files found in package")
    submission_paths = _find_submission_paths(args.submission_dir, args.submission)
    if not submission_paths:
        raise ValueError("no result JSONL files were provided")
    expected_event_ids = [str(row["event_id"]) for row in owner_index.get("events") or []]
    result = merge_census_submissions(
        [read_jsonl(path) for path in assignment_paths],
        [read_jsonl(path) for path in submission_paths],
        expected_event_ids=expected_event_ids,
        overlap_event_ids=owner_index.get("overlap_event_ids") or [],
        allow_partial=args.allow_partial,
    )
    if args.output.exists():
        raise ValueError(f"output directory already exists: {args.output}")
    args.output.mkdir(parents=True)
    write_jsonl(args.output / "merged_census.jsonl", result["records"])
    write_json(args.output / "merge_summary.json", result["summary"])
    _write_manifest(args.output)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Build the two-member full census package")
    build.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--zip", type=Path)
    build.add_argument("--batch-id")
    build.add_argument("--shard-size", type=int, default=100)
    build.set_defaults(func=build_command)

    validate = subparsers.add_parser("validate", help="Validate one returned JSONL shard")
    validate.add_argument("--assignment", type=Path, required=True)
    validate.add_argument("--submission", type=Path, required=True)
    validate.add_argument("--owner-index", type=Path)
    validate.add_argument("--report", type=Path)
    validate.set_defaults(func=validate_command)

    merge = subparsers.add_parser("merge", help="Merge a complete set of valid AI shards")
    merge.add_argument("--package-root", type=Path, required=True)
    merge.add_argument("--submission-dir", type=Path, action="append", default=[])
    merge.add_argument("--submission", type=Path, action="append", default=[])
    merge.add_argument("--output", type=Path, required=True)
    merge.add_argument("--allow-partial", action="store_true")
    merge.set_defaults(func=merge_command)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
