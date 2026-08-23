#!/usr/bin/env python3
"""Build, validate, merge and explicitly apply offline event fact reviews."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import shutil
import sqlite3
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.event_fact_review import (  # noqa: E402
    CONTRACT_VERSION,
    apply_consensus,
    build_assignment,
    build_authorization_template,
    merge_submissions,
    select_reviewable_events,
    stable_json,
    utc_now,
    validate_submission,
)


ASSET_ROOT = ROOT / "review_kit"
DEFAULT_LEDGER = ROOT / "data" / "finance_radar.sqlite3"


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _render_app(template: str, assignment: dict[str, Any]) -> str:
    encoded = base64.b64encode(stable_json(assignment).encode("utf-8")).decode("ascii")
    return template.replace("__ASSIGNMENT_BASE64__", encoded).replace(
        "__REVIEWER_SLOT__", str(assignment["reviewer_slot"])
    )


def _copy_reviewer_docs(destination: Path) -> None:
    for name in (
        "01_一分钟上手.html",
        "02_审核判断树.html",
        "03_正反例与易错点.html",
        "04_交回结果说明.html",
    ):
        shutil.copy2(ASSET_ROOT / name, destination / name)


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
    temporary = output.with_suffix(output.suffix + ".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, arcname=f"{source.name}/{path.relative_to(source).as_posix()}")
    temporary.replace(output)


def _ledger_snapshot_summary(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    try:
        total_events, last_event_updated_at = connection.execute(
            "SELECT COUNT(*),MAX(last_updated_at) FROM canonical_events"
        ).fetchone()
    finally:
        connection.close()
    stat = resolved.stat()
    return {
        "source_ledger_name": resolved.name,
        "source_ledger_sha256": file_sha256(resolved),
        "source_ledger_bytes": stat.st_size,
        "source_ledger_modified_at": datetime.fromtimestamp(
            stat.st_mtime, timezone.utc
        ).isoformat(),
        "source_ledger_total_events": int(total_events or 0),
        "source_ledger_last_event_updated_at": last_event_updated_at,
    }


def build_command(args: argparse.Namespace) -> int:
    if args.output.exists():
        raise ValueError(f"output directory already exists: {args.output}")
    events = select_reviewable_events(
        args.ledger,
        limit=args.limit,
        families=args.family,
    )
    generated = datetime.now(timezone.utc)
    batch_id = args.batch_id or f"EFR-{generated.strftime('%Y%m%dT%H%M%SZ')}"
    expires_at = (generated + timedelta(days=args.valid_days)).isoformat()
    template = (ASSET_ROOT / "reviewer_app.html").read_text(encoding="utf-8")
    args.output.mkdir(parents=True)
    assignments: dict[str, dict[str, Any]] = {}
    for slot in ("A", "B"):
        assignment = build_assignment(
            events,
            batch_id=batch_id,
            reviewer_slot=slot,
            expires_at=expires_at,
        )
        assignments[slot] = assignment
        folder = args.output / f"成员{slot}"
        folder.mkdir()
        write_json(folder / "批次清单_只读.json", assignment)
        (folder / "00_先看我.txt").write_text(
            f"成员{slot}：\n\n"
            f"1. 双击“01_一分钟上手.html”。\n"
            f"2. 再双击“审核工具_成员{slot}.html”。\n"
            "3. 全部完成后点击页面右上角“导出最终结果”。\n"
            "4. 只把导出的 .review.json 文件私下交给负责人。\n\n"
            "不要交换答案，不要把结果先传到公开 GitHub，不需要安装项目或 Python。\n",
            encoding="utf-8-sig",
        )
        # Keep the filename short and obvious after extraction.
        (folder / f"审核工具_成员{slot}.html").write_text(
            _render_app(template, assignment), encoding="utf-8"
        )
        _copy_reviewer_docs(folder)

    write_json(args.output / "负责人材料" / "assignment_A.json", assignments["A"])
    write_json(args.output / "负责人材料" / "assignment_B.json", assignments["B"])
    shutil.copy2(ASSET_ROOT / "负责人_接收与导入说明.md", args.output / "负责人材料")
    shutil.copy2(ASSET_ROOT / "为什么有这么多待审.md", args.output / "负责人材料")
    shutil.copy2(ASSET_ROOT / "submission.schema.json", args.output / "负责人材料")
    snapshot_summary = _ledger_snapshot_summary(args.ledger)
    write_json(
        args.output / "负责人材料" / "批次摘要.json",
        {
            "schema_version": 1,
            "contract_version": CONTRACT_VERSION,
            "batch_id": batch_id,
            "generated_at": utc_now(),
            **snapshot_summary,
            "event_count": len(events),
            "families": sorted({str(row.get("event_family") or "") for row in events}),
            "reviewer_mode": "two_independent_reviewers_same_frozen_batch",
            "canonical_state_changed": False,
            "no_trading": True,
        },
    )
    _write_manifest(args.output)
    if args.zip:
        _zip_directory(args.output, args.zip)
    print(
        json.dumps(
            {
                "batch_id": batch_id,
                "events": len(events),
                "output": str(args.output.resolve()),
                "zip": str(args.zip.resolve()) if args.zip else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def validate_command(args: argparse.Namespace) -> int:
    result = validate_submission(read_json(args.assignment), read_json(args.submission))
    if args.output:
        write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 2


def merge_command(args: argparse.Namespace) -> int:
    merged = merge_submissions(
        read_json(args.assignment_a),
        read_json(args.submission_a),
        read_json(args.assignment_b),
        read_json(args.submission_b),
    )
    write_json(args.output, merged)
    authorization = build_authorization_template(merged)
    write_json(args.authorization_template, authorization)
    print(
        json.dumps(
            {
                "batch_id": merged["batch_id"],
                "consensus": merged["consensus_count"],
                "conflicts": merged["conflict_count"],
                "output": str(args.output.resolve()),
                "authorization_template": str(args.authorization_template.resolve()),
                "canonical_state_changed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _backup_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError(f"backup output already exists: {destination}")
    source_connection = sqlite3.connect(source)
    target_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(target_connection)
        integrity = target_connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"backup integrity_check failed: {integrity}")
    finally:
        target_connection.close()
        source_connection.close()


def apply_command(args: argparse.Namespace) -> int:
    if not args.apply:
        raise ValueError("formal application requires the exact --apply flag")
    consensus = read_json(args.consensus)
    authorization = read_json(args.authorization)
    _backup_sqlite(args.ledger, args.backup_output)
    result = apply_consensus(args.ledger, consensus, authorization)
    result["preapply_backup"] = str(args.backup_output.resolve())
    result["preapply_backup_sha256"] = file_sha256(args.backup_output)
    write_json(args.report, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Build two self-contained reviewer packages")
    build.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--zip", type=Path)
    build.add_argument("--limit", type=int, default=24)
    build.add_argument("--batch-id")
    build.add_argument("--valid-days", type=int, default=7)
    build.add_argument("--family", action="append", default=[])
    build.set_defaults(func=build_command)

    validate = subparsers.add_parser("validate", help="Validate one returned review file")
    validate.add_argument("--assignment", type=Path, required=True)
    validate.add_argument("--submission", type=Path, required=True)
    validate.add_argument("--output", type=Path)
    validate.set_defaults(func=validate_command)

    merge = subparsers.add_parser("merge", help="Merge two valid independent submissions")
    merge.add_argument("--assignment-a", type=Path, required=True)
    merge.add_argument("--submission-a", type=Path, required=True)
    merge.add_argument("--assignment-b", type=Path, required=True)
    merge.add_argument("--submission-b", type=Path, required=True)
    merge.add_argument("--output", type=Path, required=True)
    merge.add_argument("--authorization-template", type=Path, required=True)
    merge.set_defaults(func=merge_command)

    apply_parser = subparsers.add_parser(
        "apply", help="Apply an exact owner-authorized consensus after creating a backup"
    )
    apply_parser.add_argument("--ledger", type=Path, required=True)
    apply_parser.add_argument("--consensus", type=Path, required=True)
    apply_parser.add_argument("--authorization", type=Path, required=True)
    apply_parser.add_argument("--backup-output", type=Path, required=True)
    apply_parser.add_argument("--report", type=Path, required=True)
    apply_parser.add_argument("--apply", action="store_true")
    apply_parser.set_defaults(func=apply_command)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
