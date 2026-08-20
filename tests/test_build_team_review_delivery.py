from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_team_review_delivery as delivery


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_checksums(root: Path) -> None:
    manifest = root / "SHA256SUMS.csv"
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path != manifest and path.suffix.lower() != ".zip":
            rows.append((path.relative_to(root).as_posix(), _sha(path), path.stat().st_size))
    with manifest.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["relative_path", "sha256", "bytes"])
        writer.writerows(rows)


def _ai_kit(root: Path, *, count: int = 10_001, full: bool = True) -> Path:
    root.mkdir()
    _write_json(
        root / "batch_manifest.json",
        {
            "schema_version": 1,
            "contract_version": "ai-census-v1",
            "batch_id": "AIC-PRODUCTION-FIXTURE",
            "source_event_count": count,
            "collective_full_coverage": full,
        },
    )
    owner = root / "负责人材料"
    owner.mkdir()
    _write_json(owner / "owner_index.json", {"event_count": count, "events": []})
    (owner / "负责人接收说明.md").write_text("owner only\n", encoding="utf-8")
    for slot, event_range in (
        ("A", range(1, 5_002)),
        ("B", range(5_002, 10_002)),
    ):
        member = root / f"成员{slot}"
        (member / "任务分片").mkdir(parents=True)
        (member / "结果放这里").mkdir()
        (member / "01_成员操作说明.md").write_text("AI census instructions\n", encoding="utf-8")
        (member / "03_AI审核总提示词.md").write_text("fixed prompt\n", encoding="utf-8")
        events = list(event_range)
        rows = [
            json.dumps(
                {
                    "record_type": "assignment_header",
                    "reviewer_slot": slot,
                    "event_count": len(events),
                },
                separators=(",", ":"),
            )
        ]
        rows.extend(
            json.dumps(
                {"record_type": "event_packet", "event_id": f"E{index:05d}"},
                separators=(",", ":"),
            )
            for index in events
        )
        (member / "任务分片" / f"{slot}-001.input.jsonl").write_text(
            "\n".join(rows) + "\n", encoding="utf-8"
        )
    _write_checksums(root)
    return root


def _gold_kit(root: Path, *, count: int = 2) -> Path:
    root.mkdir()
    samples = [{"sample_id": f"S{index}"} for index in range(count)]
    owner = root / "负责人材料_禁止发给组员"
    owner.mkdir()
    _write_json(owner / "批次摘要.json", {"batch_id": "HGR-FIXTURE", "sample_count": count})
    _write_json(
        owner / "owner_manifest.json",
        {
            "batch_id": "HGR-FIXTURE",
            "contract_version": "human-gold-offline-v1",
            "samples": samples,
        },
    )
    _write_json(owner / "assignment_A.json", {"owner_only": True})
    _write_json(owner / "assignment_B.json", {"owner_only": True})
    for slot in ("A", "B"):
        member = root / f"成员{slot}_私密发送"
        member.mkdir()
        _write_json(
            member / "批次清单_只读.json",
            {
                "reviewer_slot": slot,
                "events": [{"sample_token": f"{slot}-{index}"} for index in range(count)],
                "ai_assistance_allowed": False,
            },
        )
        (member / f"审核工具_成员{slot}.html").write_text("<html>human only</html>", encoding="utf-8")
    _write_checksums(root)
    return root


def _zip_wrapped(root: Path, output: Path) -> Path:
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                archive.write(path, f"wrapper/{root.name}/{path.relative_to(root).as_posix()}")
    return output


def test_builds_isolated_reproducible_delivery_from_directories_and_zips(tmp_path: Path) -> None:
    ai = _ai_kit(tmp_path / "ai")
    gold = _gold_kit(tmp_path / "gold")
    output_one = tmp_path / "delivery-one"
    archive_one = tmp_path / "delivery-one.zip"
    first = delivery.build_delivery(
        ai_census=ai,
        human_gold=gold,
        output=output_one,
        archive=archive_one,
    )

    assert first["source_event_count"] == 10_001
    assert first["collective_full_coverage"] is True
    assert first["gold_sample_count"] == 2
    assert first["owner_material_in_member_packages"] is False
    for expected in (
        "START_HERE.html",
        "00_总说明与分工.md",
        "成员A_发送包",
        "成员B_发送包",
        "负责人材料",
        "源代码与测试摘要",
        "MANIFEST.json",
        "SHA256SUMS.csv",
        "成员A_发送包.zip",
        "成员B_发送包.zip",
    ):
        assert (output_one / expected).exists()
    assert (output_one / "负责人材料" / "owner_index.json").is_file()
    assert (output_one / "负责人材料" / "真人双盲金标" / "owner_manifest.json").is_file()
    for slot in ("A", "B"):
        member = output_one / f"成员{slot}_发送包"
        assert (member / "任务分片").is_dir()
        assert (member / "真人双盲金标_严禁使用AI" / f"审核工具_成员{slot}.html").is_file()
        assert not any(path.name in {"owner_index.json", "owner_manifest.json"} for path in member.rglob("*"))
        with zipfile.ZipFile(output_one / f"成员{slot}_发送包.zip") as archive:
            names = archive.namelist()
            assert not any("负责人材料" in name or "owner_manifest.json" in name for name in names)

    ai_zip = _zip_wrapped(ai, tmp_path / "ai-input.zip")
    gold_zip = _zip_wrapped(gold, tmp_path / "gold-input.zip")
    output_two = tmp_path / "delivery-two"
    archive_two = tmp_path / "delivery-two.zip"
    second = delivery.build_delivery(
        ai_census=ai_zip,
        human_gold=gold_zip,
        output=output_two,
        archive=archive_two,
    )
    assert first["package_id"] == second["package_id"]
    assert first["zip_sha256"] == second["zip_sha256"]
    assert archive_one.read_bytes() == archive_two.read_bytes()


def test_fail_closed_for_small_or_incomplete_ai_and_empty_gold(tmp_path: Path) -> None:
    ai = _ai_kit(tmp_path / "ai")
    gold = _gold_kit(tmp_path / "gold")
    manifest_path = ai / "batch_manifest.json"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_event_count"] = 10_000
    _write_json(manifest_path, manifest)
    _write_checksums(ai)
    with pytest.raises(ValueError, match="greater than 10000"):
        delivery.build_delivery(
            ai_census=ai,
            human_gold=gold,
            output=tmp_path / "small",
            archive=tmp_path / "small.zip",
        )
    assert not (tmp_path / "small").exists()

    manifest["source_event_count"] = 10_001
    manifest["collective_full_coverage"] = False
    _write_json(manifest_path, manifest)
    _write_checksums(ai)
    with pytest.raises(ValueError, match="collective_full_coverage"):
        delivery.build_delivery(
            ai_census=ai,
            human_gold=gold,
            output=tmp_path / "partial",
            archive=tmp_path / "partial.zip",
        )

    manifest["collective_full_coverage"] = True
    _write_json(manifest_path, manifest)
    _write_checksums(ai)
    _write_json(gold / "负责人材料_禁止发给组员" / "批次摘要.json", {"batch_id": "EMPTY", "sample_count": 0})
    _write_json(gold / "负责人材料_禁止发给组员" / "owner_manifest.json", {"batch_id": "EMPTY", "samples": []})
    for slot in ("A", "B"):
        _write_json(
            gold / f"成员{slot}_私密发送" / "批次清单_只读.json",
            {"reviewer_slot": slot, "events": [], "ai_assistance_allowed": False},
        )
    _write_checksums(gold)
    with pytest.raises(ValueError, match="gold sample_count"):
        delivery.build_delivery(
            ai_census=ai,
            human_gold=gold,
            output=tmp_path / "empty-gold",
            archive=tmp_path / "empty-gold.zip",
        )


def test_rejects_zip_path_traversal(tmp_path: Path) -> None:
    malicious = tmp_path / "malicious.zip"
    with zipfile.ZipFile(malicious, "w") as archive:
        archive.writestr("../outside.txt", "no")
    gold = _gold_kit(tmp_path / "gold")
    with pytest.raises(ValueError, match="unsafe zip member"):
        delivery.build_delivery(
            ai_census=malicious,
            human_gold=gold,
            output=tmp_path / "delivery",
            archive=tmp_path / "delivery.zip",
        )
    assert not (tmp_path / "outside.txt").exists()
