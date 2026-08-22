#!/usr/bin/env python3
"""Assemble the production AI census and human-gold kits into one delivery.

The assembler is deliberately fail-closed.  It accepts an unpacked kit or the
zip produced by each upstream builder, verifies the upstream checksum manifest,
checks that the AI census really covers more than 10,000 source events, and
keeps owner-only human-gold material out of both member archives.

No database, provider credential, API key, or environment file is copied.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import shutil
import stat
import sys
import tempfile
import zipfile
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SCHEMA_VERSION = 1
PACKAGE_CONTRACT_VERSION = "finance-radar-team-review-delivery-v1"
ARCHIVE_ROOT_NAME = "FinanceRadar_团队审核交付包"

AI_REQUIRED_DIRS = ("成员A", "成员B", "负责人材料")
GOLD_REQUIRED_DIRS = (
    "成员A_私密发送",
    "成员B_私密发送",
    "负责人材料_禁止发给组员",
)

SOURCE_FILES = (
    "scripts/ai_event_census_kit.py",
    "scripts/build_human_blind_candidates.py",
    "scripts/human_gold_review_kit.py",
    "scripts/build_team_review_delivery.py",
    "app/services/ai_event_census.py",
    "app/services/human_blind_candidate_sampler.py",
    "app/services/human_gold_review.py",
    "app/models/risk_label_contract.py",
    "config/ai_census_v1.json",
    "config/risk_label_contract_v3.json",
    "tests/test_ai_event_census.py",
    "tests/test_human_blind_candidate_sampler.py",
    "tests/test_human_gold_review.py",
    "tests/test_build_team_review_delivery.py",
)

PROHIBITED_SUFFIXES = {
    ".db",
    ".env",
    ".key",
    ".p12",
    ".pfx",
    ".pem",
    ".sqlite",
    ".sqlite3",
}
PROHIBITED_NAMES = {
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ed25519",
    "id_rsa",
    "known_hosts",
}
OWNER_ONLY_NAMES = {
    "assignment_a.json",
    "assignment_b.json",
    "owner_index.json",
    "owner_manifest.json",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8-sig")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_stable_json(payload), encoding="utf-8")


def _strict_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _reject_sensitive_path(path: Path) -> None:
    name = path.name.casefold()
    if name in PROHIBITED_NAMES or path.suffix.casefold() in PROHIBITED_SUFFIXES:
        raise ValueError(f"credential/database-like file is prohibited: {path.name}")


def _copy_tree(source: Path, destination: Path) -> None:
    """Copy a curated tree without following links or accepting secret files."""

    if not source.is_dir():
        raise ValueError(f"required directory does not exist: {source}")
    destination.mkdir(parents=True, exist_ok=False)
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symbolic links are not allowed in delivery inputs: {path}")
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not path.is_file():
            raise ValueError(f"unsupported filesystem entry: {path}")
        _reject_sensitive_path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _copy_files_into(source: Path, destination: Path) -> None:
    """Copy the immediate contents of *source* into an existing destination."""

    if not source.is_dir():
        raise ValueError(f"required directory does not exist: {source}")
    for path in sorted(source.iterdir()):
        if path.is_symlink():
            raise ValueError(f"symbolic links are not allowed in delivery inputs: {path}")
        target = destination / path.name
        if path.is_dir():
            _copy_tree(path, target)
        elif path.is_file():
            _reject_sensitive_path(Path(path.name))
            shutil.copy2(path, target)
        else:
            raise ValueError(f"unsupported filesystem entry: {path}")


def _safe_extract_zip(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            raw = info.filename.replace("\\", "/")
            relative = PurePosixPath(raw)
            if (
                not raw
                or raw.startswith("/")
                or relative.is_absolute()
                or any(part in {"", ".", ".."} for part in relative.parts)
                or any(":" in part for part in relative.parts)
            ):
                raise ValueError(f"unsafe zip member: {info.filename!r}")
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode and stat.S_ISLNK(mode):
                raise ValueError(f"zip symbolic link is prohibited: {info.filename!r}")
            target = destination.joinpath(*relative.parts)
            if not _inside(target, destination):
                raise ValueError(f"unsafe zip member: {info.filename!r}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


@contextmanager
def _materialize_input(path: Path, temporary_root: Path, label: str) -> Iterator[Path]:
    resolved = path.resolve()
    if resolved.is_dir():
        yield resolved
        return
    if not resolved.is_file() or resolved.suffix.casefold() != ".zip":
        raise ValueError(f"{label} input must be an existing directory or .zip: {path}")
    destination = temporary_root / label
    _safe_extract_zip(resolved, destination)
    yield destination


def _find_ai_root(search_root: Path) -> Path:
    candidates: list[Path] = []
    for manifest in sorted(search_root.rglob("batch_manifest.json")):
        root = manifest.parent
        if all((root / name).is_dir() for name in AI_REQUIRED_DIRS):
            candidates.append(root)
    if len(candidates) != 1:
        raise ValueError(
            "AI census input must contain exactly one package root with "
            "batch_manifest.json, 成员A, 成员B and 负责人材料"
        )
    return candidates[0]


def _find_gold_root(search_root: Path) -> Path:
    candidates: list[Path] = []
    for summary in sorted(search_root.rglob("批次摘要.json")):
        if summary.parent.name != "负责人材料_禁止发给组员":
            continue
        root = summary.parent.parent
        if all((root / name).is_dir() for name in GOLD_REQUIRED_DIRS):
            candidates.append(root)
    unique = {str(path.resolve()).casefold(): path for path in candidates}
    if len(unique) != 1:
        raise ValueError(
            "human-gold input must contain exactly one package root with both "
            "private member folders and 负责人材料_禁止发给组员"
        )
    return next(iter(unique.values()))


def _verify_checksum_manifest(root: Path) -> dict[str, Any]:
    manifest = root / "SHA256SUMS.csv"
    if not manifest.is_file():
        raise ValueError(f"upstream checksum manifest is missing: {manifest}")
    checked = 0
    with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["relative_path", "sha256", "bytes"]:
            raise ValueError(f"invalid checksum manifest header: {manifest}")
        seen: set[str] = set()
        for row in reader:
            raw = str(row.get("relative_path") or "").replace("\\", "/")
            relative = PurePosixPath(raw)
            if (
                not raw
                or relative.is_absolute()
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise ValueError(f"unsafe path in checksum manifest: {raw!r}")
            target = root.joinpath(*relative.parts)
            if not _inside(target, root) or not target.is_file():
                raise ValueError(f"checksummed file is missing or unsafe: {raw}")
            if raw.casefold() in seen:
                raise ValueError(f"duplicate checksum row: {raw}")
            seen.add(raw.casefold())
            expected_size = _strict_int(int(str(row["bytes"])), f"bytes for {raw}")
            if target.stat().st_size != expected_size:
                raise ValueError(f"checksum size mismatch: {raw}")
            expected_hash = str(row.get("sha256") or "").casefold()
            if len(expected_hash) != 64 or _sha256(target) != expected_hash:
                raise ValueError(f"checksum mismatch: {raw}")
            checked += 1
    if checked == 0:
        raise ValueError(f"upstream checksum manifest is empty: {manifest}")
    return {"verified_file_count": checked, "manifest_sha256": _sha256(manifest)}


def _ai_assignment_coverage(root: Path) -> dict[str, Any]:
    union: set[str] = set()
    counts: dict[str, int] = {}
    shard_counts: dict[str, int] = {}
    for slot in ("A", "B"):
        paths = sorted((root / f"成员{slot}" / "任务分片").glob("*.input.jsonl"))
        if not paths:
            raise ValueError(f"AI member {slot} has no input shards")
        slot_ids: list[str] = []
        for path in paths:
            lines = [line for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
            if len(lines) < 2:
                raise ValueError(f"AI shard has no event records: {path.name}")
            header = json.loads(lines[0])
            if not isinstance(header, dict) or header.get("record_type") != "assignment_header":
                raise ValueError(f"AI shard header is invalid: {path.name}")
            event_ids: list[str] = []
            for line in lines[1:]:
                row = json.loads(line)
                if not isinstance(row, dict) or not row.get("event_id"):
                    raise ValueError(f"AI shard event record is invalid: {path.name}")
                event_ids.append(str(row["event_id"]))
            if header.get("event_count") != len(event_ids):
                raise ValueError(f"AI shard event_count mismatch: {path.name}")
            slot_ids.extend(event_ids)
        if len(slot_ids) != len(set(slot_ids)):
            raise ValueError(f"AI member {slot} contains duplicate event assignments")
        counts[slot] = len(slot_ids)
        shard_counts[slot] = len(paths)
        union.update(slot_ids)
    return {
        "collective_unique_event_count": len(union),
        "reviewer_assigned_event_counts": counts,
        "reviewer_shard_counts": shard_counts,
    }


def _validate_ai(root: Path) -> dict[str, Any]:
    checksum = _verify_checksum_manifest(root)
    manifest_path = root / "batch_manifest.json"
    manifest = _read_json(manifest_path)
    exact = manifest.get("source_event_count")
    legacy = manifest.get("source_ledger_event_count")
    if exact is None and legacy is None:
        raise ValueError("AI batch manifest is missing source_event_count")
    if exact is not None and legacy is not None and exact != legacy:
        raise ValueError("AI source_event_count fields disagree")
    source_event_count = _strict_int(exact if exact is not None else legacy, "source_event_count")
    if source_event_count <= 10_000:
        raise ValueError("AI source_event_count must be greater than 10000")
    if manifest.get("collective_full_coverage") is not True:
        raise ValueError("AI collective_full_coverage must be true")
    coverage = _ai_assignment_coverage(root)
    if coverage["collective_unique_event_count"] != source_event_count:
        raise ValueError(
            "AI assignment union does not match source_event_count: "
            f"{coverage['collective_unique_event_count']} != {source_event_count}"
        )
    return {
        "batch_id": str(manifest.get("batch_id") or ""),
        "contract_version": str(manifest.get("contract_version") or ""),
        "source_event_count": source_event_count,
        "collective_full_coverage": True,
        "manifest_sha256": _sha256(manifest_path),
        **coverage,
        "checksum": checksum,
    }


def _validate_gold(root: Path) -> dict[str, Any]:
    checksum = _verify_checksum_manifest(root)
    owner = root / "负责人材料_禁止发给组员"
    summary_path = owner / "批次摘要.json"
    owner_manifest_path = owner / "owner_manifest.json"
    summary = _read_json(summary_path)
    owner_manifest = _read_json(owner_manifest_path)
    sample_count = _strict_int(summary.get("sample_count"), "gold sample_count")
    if sample_count <= 0:
        raise ValueError("gold sample_count must be greater than 0")
    samples = owner_manifest.get("samples")
    if not isinstance(samples, list) or len(samples) != sample_count:
        raise ValueError("gold owner_manifest sample count does not match batch summary")
    for slot in ("A", "B"):
        assignment_path = root / f"成员{slot}_私密发送" / "批次清单_只读.json"
        assignment = _read_json(assignment_path)
        events = assignment.get("events")
        if not isinstance(events, list) or len(events) != sample_count:
            raise ValueError(f"gold member {slot} assignment sample count mismatch")
        if assignment.get("ai_assistance_allowed") is not False:
            raise ValueError(f"gold member {slot} assignment must prohibit AI assistance")
    return {
        "batch_id": str(summary.get("batch_id") or owner_manifest.get("batch_id") or ""),
        "contract_version": str(owner_manifest.get("contract_version") or ""),
        "sample_count": sample_count,
        "reviewers": 2,
        "human_only": True,
        "manifest_sha256": _sha256(summary_path),
        "owner_manifest_sha256": _sha256(owner_manifest_path),
        "checksum": checksum,
    }


def _assert_member_isolation(folder: Path) -> None:
    for path in folder.rglob("*"):
        relative = path.relative_to(folder)
        folded_parts = [part.casefold() for part in relative.parts]
        if any("负责人材料" in part or "禁止发给组员" in part for part in folded_parts):
            raise ValueError(f"owner-only path leaked into member package: {relative}")
        if path.is_file() and path.name.casefold() in OWNER_ONLY_NAMES:
            raise ValueError(f"owner-only file leaked into member package: {relative}")
        if path.is_file():
            _reject_sensitive_path(relative)


def _deterministic_zip(source: Path, output: Path, archive_root: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError(f"zip output already exists: {output}")
    temporary = output.with_suffix(output.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
                if not path.is_file():
                    continue
                relative = path.relative_to(source).as_posix()
                info = zipfile.ZipInfo(f"{archive_root}/{relative}", date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o100644 & 0xFFFF) << 16
                info.create_system = 3
                with path.open("rb") as handle:
                    archive.writestr(info, handle.read(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        with zipfile.ZipFile(temporary) as archive:
            bad = archive.testzip()
            if bad is not None:
                raise ValueError(f"zip integrity check failed: {bad}")
        temporary.replace(output)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _write_checksum_manifest(root: Path) -> dict[str, Any]:
    path = root / "SHA256SUMS.csv"
    rows: list[tuple[str, str, int]] = []
    for item in sorted(root.rglob("*")):
        if item.is_file() and item != path:
            rows.append((item.relative_to(root).as_posix(), _sha256(item), item.stat().st_size))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["relative_path", "sha256", "bytes"])
        writer.writerows(rows)
    return {"file_count": len(rows), "manifest_sha256": _sha256(path)}


def _member_start_html(slot: str, ai_count: int, gold_count: int) -> str:
    return f"""<!doctype html>
<html lang=\"zh-CN\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\"><title>成员{slot}任务入口</title>
<style>body{{font-family:system-ui,'Microsoft YaHei',sans-serif;max-width:900px;margin:36px auto;padding:0 24px;line-height:1.7;color:#172033}}section{{border:1px solid #ccd6e5;border-radius:14px;padding:18px 22px;margin:18px 0}}.ai{{border-left:8px solid #1769e0}}.human{{border-left:8px solid #d33939}}code{{background:#eef2f7;padding:2px 6px}}strong{{color:#a40000}}</style>
<body><h1>成员{slot}：两条任务线</h1>
<p>两条任务使用相反规则，必须分开完成、分开回传。不要查看成员{'B' if slot == 'A' else 'A'}的答案。</p>
<section class=\"ai\"><h2>1. AI全量事件普查</h2><p>总快照 {ai_count:,} 条；你只处理本文件夹 <code>任务分片</code> 中分给成员{slot}的部分。<strong>这一部分允许并要求使用AI</strong>。先读 <code>01_成员操作说明.md</code> 和 <code>03_AI审核总提示词.md</code>，把结果放进 <code>结果放这里</code>。</p></section>
<section class=\"human\"><h2>2. 真人独立双盲金标</h2><p>共 {gold_count} 条，两名成员都要独立审核同一批。进入 <code>真人双盲金标_严禁使用AI</code>。<strong>这一部分绝对禁止任何AI、旧标签、股价和同伴答案</strong>。完成后导出 <code>.gold-review.json</code> 私下交给负责人。</p></section>
<p>不要上传数据库、环境文件、API密钥或服务器文件。成员包不包含负责人材料。</p></body></html>
"""


def _start_html(ai: dict[str, Any], gold: dict[str, Any]) -> str:
    ai_batch = html.escape(ai["batch_id"])
    gold_batch = html.escape(gold["batch_id"])
    return f"""<!doctype html>
<html lang=\"zh-CN\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\"><title>Finance Radar 团队审核交付包</title>
<style>body{{font-family:system-ui,'Microsoft YaHei',sans-serif;max-width:980px;margin:34px auto;padding:0 24px;line-height:1.7;color:#162238}}.card{{border:1px solid #cad4e2;border-radius:16px;padding:20px;margin:18px 0}}.warn{{background:#fff5f3;border-color:#e6a29a}}code{{background:#eef2f7;padding:2px 6px}}</style>
<body><h1>Finance Radar 团队审核交付包</h1>
<p>AI普查批次 <code>{ai_batch}</code> 覆盖 {ai['source_event_count']:,} 条；真人双盲批次 <code>{gold_batch}</code> 含 {gold['sample_count']} 条。</p>
<div class=\"card\"><h2>负责人现在只做三件事</h2><ol><li>只把 <code>成员A_发送包.zip</code> 发给A。</li><li>只把 <code>成员B_发送包.zip</code> 发给B。</li><li>两人回传后，把文件放进负责人接收目录，再按 <code>负责人材料/00_负责人先看.md</code> 合并。</li></ol></div>
<div class=\"card warn\"><h2>不可混淆</h2><p>AI普查允许AI，但只形成建议性分流；真人金标严禁AI，才可形成真人训练/评估依据。任何一条普查输出都不会自动修改canonical状态，也不构成交易信号。</p></div>
<p>本包未嵌入数据库、API密钥、云凭据或环境文件。完整文件哈希见 <code>SHA256SUMS.csv</code>。</p></body></html>
"""


def _build_contents(staging: Path, ai_root: Path, gold_root: Path, ai: dict[str, Any], gold: dict[str, Any]) -> None:
    _write_text(staging / "START_HERE.html", _start_html(ai, gold))
    _write_text(
        staging / "00_总说明与分工.md",
        "# 总说明与分工\n\n"
        f"- 生产AI普查：{ai['source_event_count']:,} 条，A/B合计全覆盖；仅作建议性分流。\n"
        f"- 真人双盲金标：{gold['sample_count']} 条，A/B审核同一批；严禁AI，冲突交第三名真人仲裁。\n"
        "- 成员A只收到成员A压缩包，成员B只收到成员B压缩包。\n"
        "- 负责人材料不得发给任何审核成员。\n"
        "- 不提交数据库、API密钥、云凭据、环境文件或交易能力。\n",
    )

    for slot in ("A", "B"):
        member = staging / f"成员{slot}_发送包"
        member.mkdir()
        _copy_files_into(ai_root / f"成员{slot}", member)
        _copy_tree(
            gold_root / f"成员{slot}_私密发送",
            member / "真人双盲金标_严禁使用AI",
        )
        _write_text(
            member / "00_双轨任务先看.html",
            _member_start_html(slot, ai["source_event_count"], gold["sample_count"]),
        )
        _assert_member_isolation(member)

    owner = staging / "负责人材料"
    owner.mkdir()
    _copy_files_into(ai_root / "负责人材料", owner)
    shutil.copy2(ai_root / "batch_manifest.json", owner / "batch_manifest.json")
    shutil.copy2(ai_root / "SHA256SUMS.csv", owner / "AI输入_SHA256SUMS.csv")
    _copy_tree(
        gold_root / "负责人材料_禁止发给组员",
        owner / "真人双盲金标",
    )
    shutil.copy2(gold_root / "SHA256SUMS.csv", owner / "真人金标输入_SHA256SUMS.csv")
    _write_text(
        owner / "00_负责人先看.md",
        "# 负责人接收顺序\n\n"
        "1. 不要把本目录发送给A或B。\n"
        "2. AI普查：收齐每个分片的 `*.result.jsonl`，先逐片validate，再完整merge；AI结果仍为advisory。\n"
        "3. 真人金标：分别收取A/B的 `.gold-review.json`，先validate，再merge；有冲突必须交第三名真人仲裁。\n"
        "4. 密封盲测冻结前不得看模型预测或事后市场结果。\n"
        "5. 所有API密钥只存在执行者本机环境，不得放入回传文件或Git。\n",
    )

    source_root = staging / "源代码与测试摘要"
    source_code = source_root / "源代码"
    source_code.mkdir(parents=True)
    source_hashes: list[dict[str, Any]] = []
    for relative_text in SOURCE_FILES:
        relative = Path(relative_text)
        source = ROOT / relative
        if not source.is_file():
            raise ValueError(f"required reproducibility source is missing: {relative_text}")
        destination = source_code / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        source_hashes.append(
            {"relative_path": relative_text, "sha256": _sha256(source), "bytes": source.stat().st_size}
        )
    _write_json(source_root / "源代码清单.json", source_hashes)
    _write_text(
        source_root / "TEST_SUMMARY.md",
        "# 测试与复现摘要\n\n"
        "组装时已执行并通过：上游SHA-256逐文件校验、AI实际分片并集全覆盖校验、"
        "真人A/B样本数一致性校验、成员包负责人材料隔离检查、子包与总包ZIP CRC检查。\n\n"
        "仓库定向复跑命令：\n\n"
        "```powershell\n"
        "python -m pytest -q tests/test_ai_event_census.py tests/test_human_gold_review.py tests/test_build_team_review_delivery.py\n"
        "python -m compileall -q app scripts tests\n"
        "```\n\n"
        "此摘要不宣称运行了pytest；pytest结果应由交付负责人在生成最终包前另行记录。\n",
    )


def build_delivery(
    *,
    ai_census: Path,
    human_gold: Path,
    output: Path,
    archive: Path,
) -> dict[str, Any]:
    output = output.resolve()
    archive = archive.resolve()
    if output.exists():
        raise ValueError(f"output directory already exists: {output}")
    if archive.exists():
        raise ValueError(f"zip output already exists: {archive}")
    if archive.suffix.casefold() != ".zip":
        raise ValueError("zip output must end in .zip")
    if _inside(archive, output):
        raise ValueError("zip output must not be inside the output directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    archive.parent.mkdir(parents=True, exist_ok=True)

    staging_path: Path | None = None
    zip_temporary: Path | None = None
    try:
        with tempfile.TemporaryDirectory(prefix=".team-review-inputs-", dir=output.parent) as temporary:
            temporary_root = Path(temporary)
            with ExitStack() as stack:
                ai_search = stack.enter_context(
                    _materialize_input(ai_census, temporary_root, "ai-census")
                )
                gold_search = stack.enter_context(
                    _materialize_input(human_gold, temporary_root, "human-gold")
                )
                ai_root = _find_ai_root(ai_search)
                gold_root = _find_gold_root(gold_search)
                ai = _validate_ai(ai_root)
                gold = _validate_gold(gold_root)

                staging_path = Path(
                    tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
                )
                _build_contents(staging_path, ai_root, gold_root, ai, gold)

                for slot in ("A", "B"):
                    member = staging_path / f"成员{slot}_发送包"
                    _deterministic_zip(
                        member,
                        staging_path / f"成员{slot}_发送包.zip",
                        f"成员{slot}_发送包",
                    )

                source_hash = hashlib.sha256(
                    f"{ai['manifest_sha256']}:{gold['manifest_sha256']}:{PACKAGE_CONTRACT_VERSION}".encode(
                        "utf-8"
                    )
                ).hexdigest()
                package_manifest = {
                    "schema_version": PACKAGE_SCHEMA_VERSION,
                    "contract_version": PACKAGE_CONTRACT_VERSION,
                    "package_id": f"FR-TEAM-{source_hash[:16].upper()}",
                    "production_ai_census": ai,
                    "human_gold": gold,
                    "member_packages": {
                        slot: {
                            "directory": f"成员{slot}_发送包",
                            "zip": f"成员{slot}_发送包.zip",
                            "zip_sha256": _sha256(staging_path / f"成员{slot}_发送包.zip"),
                            "owner_material_included": False,
                        }
                        for slot in ("A", "B")
                    },
                    "boundaries": {
                        "ai_census_is_advisory_only": True,
                        "human_gold_ai_assistance_allowed": False,
                        "canonical_mutation_allowed": False,
                        "market_outcomes_included": False,
                        "trading_capability_included": False,
                        "database_included": False,
                        "api_or_cloud_credentials_included": False,
                    },
                    "reproducible_archive": True,
                }
                _write_json(staging_path / "MANIFEST.json", package_manifest)
                checksum = _write_checksum_manifest(staging_path)
                package_manifest["delivery_checksum_file_count"] = checksum["file_count"]
                # Rewrite MANIFEST with the final count, then regenerate checksums once.
                _write_json(staging_path / "MANIFEST.json", package_manifest)
                checksum = _write_checksum_manifest(staging_path)

                zip_temporary = archive.with_suffix(archive.suffix + ".building")
                _deterministic_zip(staging_path, zip_temporary, ARCHIVE_ROOT_NAME)
                os.replace(staging_path, output)
                staging_path = None
                os.replace(zip_temporary, archive)
                zip_temporary = None
                return {
                    "package_id": package_manifest["package_id"],
                    "source_event_count": ai["source_event_count"],
                    "collective_full_coverage": True,
                    "gold_sample_count": gold["sample_count"],
                    "output": str(output),
                    "zip": str(archive),
                    "zip_sha256": _sha256(archive),
                    "delivery_file_count": checksum["file_count"],
                    "owner_material_in_member_packages": False,
                    "credentials_included": False,
                }
    finally:
        if staging_path is not None and staging_path.exists():
            shutil.rmtree(staging_path)
        if zip_temporary is not None and zip_temporary.exists():
            zip_temporary.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ai-census", type=Path, required=True, help="production AI census directory or zip")
    parser.add_argument("--human-gold", type=Path, required=True, help="human-gold directory or zip")
    parser.add_argument("--output", type=Path, required=True, help="new output directory (D: recommended)")
    parser.add_argument("--zip", dest="archive", type=Path, required=True, help="new final .zip path")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = build_delivery(
            ai_census=args.ai_census,
            human_gold=args.human_gold,
            output=args.output,
            archive=args.archive,
        )
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
