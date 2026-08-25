#!/usr/bin/env python3
"""Build and reconcile the offline human-only risk-router gold-label kit."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import shutil
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.human_gold_review import (  # noqa: E402
    build_offline_batch,
    finalize_with_arbitration,
    merge_dual_submissions,
    summarize_partial_progress,
    validate_submission,
)


ASSET_ROOT = ROOT / "human_gold_review_kit"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def _load_samples(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    else:
        payload = _read_json(path)
        if isinstance(payload, dict):
            rows = payload.get("samples") or payload.get("items")
        else:
            rows = payload
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("sample input must be a JSON array/JSONL or an object with a samples array")
    return rows


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest(root: Path) -> None:
    manifest = root / "SHA256SUMS.csv"
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path != manifest and path.suffix.lower() != ".zip":
            rows.append((path.relative_to(root).as_posix(), _file_sha256(path), path.stat().st_size))
    with manifest.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["relative_path", "sha256", "bytes"])
        writer.writerows(rows)


def _zip_directory(source: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, arcname=f"{source.name}/{path.relative_to(source).as_posix()}")
    temporary.replace(output)


def _render_app(template: str, assignment: dict[str, Any]) -> str:
    encoded = base64.b64encode(
        json.dumps(assignment, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return template.replace("__ASSIGNMENT_BASE64__", encoded).replace(
        "__REVIEWER_SLOT__", str(assignment["reviewer_slot"])
    )


def _copy_member_docs(folder: Path) -> None:
    for name in ("01_三轴判断标准.html", "02_交回与保密说明.html"):
        shutil.copy2(ASSET_ROOT / name, folder / name)


def build_command(args: argparse.Namespace) -> int:
    if args.output.exists():
        raise ValueError(f"output directory already exists: {args.output}")
    generated = datetime.now(timezone.utc)
    batch_id = args.batch_id or f"HGR-{generated.strftime('%Y%m%dT%H%M%SZ')}"
    expires_at = (generated + timedelta(days=args.valid_days)).isoformat()
    built = build_offline_batch(
        _load_samples(args.samples), batch_id=batch_id, expires_at=expires_at
    )
    args.output.mkdir(parents=True)
    template = (ASSET_ROOT / "reviewer_app.html").read_text(encoding="utf-8")
    for slot in ("A", "B"):
        assignment = built["assignments"][slot]
        folder = args.output / f"成员{slot}_私密发送"
        folder.mkdir()
        (folder / "00_先看我.txt").write_text(
            f"你是成员{slot}。这是一份真人独立双盲金标任务。\n\n"
            f"1. 先双击“01_三轴判断标准.html”。\n"
            f"2. 再双击“审核工具_成员{slot}.html”。\n"
            "3. 全部完成后导出最终 .gold-review.json，私下交给负责人。\n\n"
            "硬性禁止：使用任何AI、查看股价/事后涨跌、寻找旧标签、询问另一名成员答案。\n"
            "你只填写重大性、极性、证据状态和理由；不要选择最终模型标签。\n",
            encoding="utf-8-sig",
        )
        _write_json(folder / "批次清单_只读.json", assignment)
        (folder / f"审核工具_成员{slot}.html").write_text(
            _render_app(template, assignment), encoding="utf-8"
        )
        _copy_member_docs(folder)

    owner = args.output / "负责人材料_禁止发给组员"
    owner.mkdir()
    _write_json(owner / "owner_manifest.json", built["owner_manifest"])
    _write_json(owner / "assignment_A.json", built["assignments"]["A"])
    _write_json(owner / "assignment_B.json", built["assignments"]["B"])
    shutil.copy2(ASSET_ROOT / "负责人_接收合并仲裁说明.md", owner)
    _write_json(
        owner / "批次摘要.json",
        {
            "schema_version": 1,
            "batch_id": batch_id,
            "generated_at": generated.isoformat(),
            "expires_at": expires_at,
            "sample_count": len(built["owner_manifest"]["samples"]),
            "reviewers": 2,
            "same_samples": True,
            "independent_random_order": True,
            "anonymous_sample_tokens": True,
            "human_only": True,
            "target_labels_preassigned": False,
            "canonical_state_changed": False,
            "model_changed": False,
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
                "samples": len(built["owner_manifest"]["samples"]),
                "output": str(args.output.resolve()),
                "zip": str(args.zip.resolve()) if args.zip else None,
                "canonical_state_changed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def validate_command(args: argparse.Namespace) -> int:
    report = validate_submission(_read_json(args.assignment), _read_json(args.submission))
    if args.output:
        _write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["valid"] else 2


def merge_command(args: argparse.Namespace) -> int:
    if args.output.exists():
        raise ValueError(f"output directory already exists: {args.output}")
    merged = merge_dual_submissions(
        _read_json(args.owner_manifest),
        _read_json(args.submission_a),
        _read_json(args.submission_b),
    )
    args.output.mkdir(parents=True)
    _write_json(args.output / "merge_manifest_private.json", merged)
    _write_jsonl(args.output / "已达成共识_未冻结.jsonl", merged["consensus_annotations"])
    _write_json(args.output / "冲突摘要.json", {"conflicts": merged["conflicts"]})
    _write_json(
        args.output / "合并报告.json",
        {
            key: merged[key]
            for key in (
                "batch_id",
                "consensus_count",
                "conflict_count",
                "axis_conflict_counts",
                "all_conflicts_resolved",
                "target_labels_were_submitted",
                "split",
                "freeze_required_before_blind_use",
                "canonical_state_changed",
                "model_changed",
                "no_trading",
            )
        },
    )
    if merged["arbitration_assignment"]:
        folder = args.output / "第三人仲裁包_私密发送"
        folder.mkdir()
        assignment = merged["arbitration_assignment"]
        _write_json(folder / "仲裁批次清单_只读.json", assignment)
        template = (ASSET_ROOT / "reviewer_app.html").read_text(encoding="utf-8")
        (folder / "仲裁工具.html").write_text(_render_app(template, assignment), encoding="utf-8")
        shutil.copy2(ASSET_ROOT / "01_三轴判断标准.html", folder)
        shutil.copy2(ASSET_ROOT / "02_交回与保密说明.html", folder)
        (folder / "00_先看我.txt").write_text(
            "你是第三名独立仲裁人。逐条阅读冻结证据和甲乙两个冲突意见，独立填写最终三轴。\n"
            "禁止使用任何AI、股价/事后结果、旧标签，也不得由原来的A或B冒充仲裁人。\n",
            encoding="utf-8-sig",
        )
    _write_manifest(args.output)
    if args.zip:
        _zip_directory(args.output, args.zip)
    print(
        json.dumps(
            {
                "batch_id": merged["batch_id"],
                "consensus": merged["consensus_count"],
                "conflicts": merged["conflict_count"],
                "output": str(args.output.resolve()),
                "zip": str(args.zip.resolve()) if args.zip else None,
                "canonical_state_changed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def finalize_command(args: argparse.Namespace) -> int:
    merged = _read_json(args.merge_manifest)
    arbiter = _read_json(args.arbiter_submission) if args.arbiter_submission else None
    result = finalize_with_arbitration(merged, arbiter)
    _write_jsonl(args.output, result["annotations"])
    _write_json(args.report, {key: value for key, value in result.items() if key != "annotations"})
    print(
        json.dumps(
            {
                "batch_id": result["batch_id"],
                "annotations": result["annotation_count"],
                "label_counts": result["label_counts"],
                "output": str(args.output.resolve()),
                "report": str(args.report.resolve()),
                "split": result["split"],
                "freeze_required": True,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def progress_command(args: argparse.Namespace) -> int:
    """Stage one or more progress snapshots without deriving gold labels."""

    owner_manifest = _read_json(args.owner_manifest)
    report = summarize_partial_progress(
        owner_manifest,
        {
            "A": [_read_json(path) for path in args.submission_a],
            "B": [_read_json(path) for path in args.submission_b],
        },
    )
    _write_json(args.output, report)
    print(json.dumps(report["progress"], ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="create private A/B offline reviewer folders")
    build.add_argument("--samples", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--zip", type=Path)
    build.add_argument("--batch-id")
    build.add_argument("--valid-days", type=int, default=21)
    build.set_defaults(handler=build_command)

    validate = commands.add_parser("validate", help="validate one returned export")
    validate.add_argument("--assignment", type=Path, required=True)
    validate.add_argument("--submission", type=Path, required=True)
    validate.add_argument("--output", type=Path)
    validate.set_defaults(handler=validate_command)

    merge = commands.add_parser("merge", help="merge A/B and create a third-human conflict kit")
    merge.add_argument("--owner-manifest", type=Path, required=True)
    merge.add_argument("--submission-a", type=Path, required=True)
    merge.add_argument("--submission-b", type=Path, required=True)
    merge.add_argument("--output", type=Path, required=True)
    merge.add_argument("--zip", type=Path)
    merge.set_defaults(handler=merge_command)

    finalize = commands.add_parser("finalize", help="finalize consensus plus arbitration")
    finalize.add_argument("--merge-manifest", type=Path, required=True)
    finalize.add_argument("--arbiter-submission", type=Path)
    finalize.add_argument("--output", type=Path, required=True)
    finalize.add_argument("--report", type=Path, required=True)
    finalize.set_defaults(handler=finalize_command)

    progress = commands.add_parser(
        "progress",
        help="stage partial A/B snapshots and report coverage/conflicts without making gold",
    )
    progress.add_argument("--owner-manifest", type=Path, required=True)
    progress.add_argument("--submission-a", type=Path, action="append", default=[])
    progress.add_argument("--submission-b", type=Path, action="append", default=[])
    progress.add_argument("--output", type=Path, required=True)
    progress.set_defaults(handler=progress_command)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "valid_days", 1) < 1:
        parser.error("--valid-days must be at least 1")
    try:
        raise SystemExit(args.handler(args))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
