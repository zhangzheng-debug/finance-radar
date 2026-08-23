from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from app.models.risk_label_contract import validate_annotation
from app.services.human_gold_review import (
    OFFLINE_GOLD_CONTRACT_VERSION,
    build_offline_batch,
    finalize_with_arbitration,
    merge_dual_submissions,
    stable_json,
    validate_submission,
)


def sample(index: int) -> dict:
    text = f"frozen-text-{index}"
    return {
        "sample_id": f"sample-{index}",
        "event_id": f"event-private-{index}",
        "text_sha256": __import__("hashlib").sha256(text.encode()).hexdigest(),
        "content": {
            "contract_version": "human-blind-v3.1",
            "as_of": "2026-08-01T00:00:00+00:00",
            "cutoff_policy": "source_published_or_received_at_lte_first_seen_at",
            "headline": f"Issuer disclosure {index}",
            "summary": "The frozen record contains an exact reviewable disclosure.",
            "confirmed_facts": ["A filing was available by the event-time cutoff."],
            "passages": [
                {
                    "evidence_id": f"private-evidence-{index}",
                    "authority_class": "PRIMARY_OFFICIAL",
                    "document_type": "8-K",
                    "item_section": "1.03",
                    "published_at": "2026-08-01",
                    "received_at": "2026-08-01T00:01:00+00:00",
                    "passage": (
                        "The issuer filed a voluntary petition under Chapter 11 "
                        f"for the independently frozen fixture {index}."
                    ),
                    "evidence_status": "confirmed-private-status",
                }
            ],
            "event_date": "2026-08-01",
            "source_identity_hidden": True,
            "target_label_hidden": True,
            "post_event_market_data_included": False,
            "model_output_included": False,
        },
        "source_id": f"private-source-{index}",
        "authority_tier": "P0",
        "entity_group": f"issuer:{index}",
        "event_chain_group": f"chain:{index}",
    }


@pytest.fixture()
def batch() -> dict:
    return build_offline_batch(
        [sample(1), sample(2), sample(3)],
        batch_id="HGR-TEST",
        expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        batch_secret="fixed-test-secret",
        reviewer_tokens={"A": "anonymous-a", "B": "anonymous-b"},
    )


def make_submission(
    assignment: dict,
    *,
    axes_by_token: dict[str, tuple[str, str, str]] | None = None,
) -> dict:
    rows = []
    for item in assignment["events"]:
        axes = (axes_by_token or {}).get(
            item["sample_token"],
            ("MATERIAL_ADVERSE", "ADVERSE", "PRIMARY_SUPPORTED"),
        )
        rows.append(
            {
                "sample_token": item["sample_token"],
                "materiality": axes[0],
                "polarity": axes[1],
                "evidence_state": axes[2],
                "rationale": "The exact primary passage supports this independent three-axis judgment.",
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
                "duration_seconds": 90,
            }
        )
    return {
        "schema_version": 1,
        "contract_version": OFFLINE_GOLD_CONTRACT_VERSION,
        "batch_id": assignment["batch_id"],
        "reviewer_slot": assignment["reviewer_slot"],
        "review_role": assignment["review_role"],
        "reviewer_token": assignment["reviewer_token"],
        "assignment_sha256": assignment["assignment_sha256"],
        "attestations": {
            "human_only": True,
            "independent_judgment": True,
            "no_ai_assistance": True,
            "no_model_output": True,
            "no_market_outcome": True,
            "no_old_label": True,
        },
        "peer_answers_hidden": assignment["review_role"] == "REVIEWER",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "complete": True,
        "results": rows,
        "target_label_submitted": False,
        "canonical_state_changed": False,
        "no_trading": True,
    }


def test_build_masks_private_ids_uses_slot_tokens_and_different_order(batch: dict) -> None:
    assignment_a = batch["assignments"]["A"]
    assignment_b = batch["assignments"]["B"]
    assert assignment_a["reviewer_token"] != assignment_b["reviewer_token"]
    assert [row["sample_token"] for row in assignment_a["events"]] != [
        row["sample_token"] for row in assignment_b["events"]
    ]
    assert batch["owner_manifest"]["reviewer_order_sample_ids"]["A"] != batch[
        "owner_manifest"
    ]["reviewer_order_sample_ids"]["B"]
    for assignment in (assignment_a, assignment_b):
        payload = stable_json(assignment)
        assert "event-private" not in payload
        assert "private-source" not in payload
        assert "private-evidence" not in payload
        assert "confirmed-private-status" not in payload
        assert assignment["target_label_submitted"] is False
        assert assignment["ai_assistance_allowed"] is False
        assert assignment["post_event_market_data_included"] is False
        assert assignment["old_labels_included"] is False


def test_submission_contract_rejects_ai_old_label_or_direct_target(batch: dict) -> None:
    assignment = batch["assignments"]["A"]
    valid = make_submission(assignment)
    assert validate_submission(assignment, valid)["valid"] is True

    used_ai = {**valid, "attestations": {**valid["attestations"], "no_ai_assistance": False}}
    report = validate_submission(assignment, used_ai)
    assert report["valid"] is False
    assert "attestation no_ai_assistance must be true" in report["issues"]

    with_label = {**valid, "results": [dict(row) for row in valid["results"]]}
    with_label["results"][0]["label"] = "RISK_REVIEW"
    report = validate_submission(assignment, with_label)
    assert report["valid"] is False
    assert any("unsupported fields: label" in issue for issue in report["issues"])

    old_label_lookup = {**valid, "attestations": {**valid["attestations"], "no_old_label": False}}
    assert validate_submission(assignment, old_label_lookup)["valid"] is False


def test_merge_derives_consensus_and_builds_third_human_conflict_pack(batch: dict) -> None:
    a = make_submission(batch["assignments"]["A"])
    b_assignment = batch["assignments"]["B"]
    conflicting_sample_id = "sample-2"
    conflicting_token = next(
        token
        for token, sample_id in batch["owner_manifest"]["token_maps"]["B"].items()
        if sample_id == conflicting_sample_id
    )
    b = make_submission(
        b_assignment,
        axes_by_token={
            conflicting_token: ("NOT_MATERIAL_ADVERSE", "POSITIVE", "PRIMARY_SUPPORTED")
        },
    )
    merged = merge_dual_submissions(
        batch["owner_manifest"],
        a,
        b,
        arbiter_token="anonymous-third-human",
        arbitration_secret="fixed-arbitration-secret",
    )
    assert merged["consensus_count"] == 2
    assert merged["conflict_count"] == 1
    assert merged["all_conflicts_resolved"] is False
    assert merged["axis_conflict_counts"] == {"materiality": 1, "polarity": 1}
    assert all(validate_annotation(row) == [] for row in merged["consensus_annotations"])
    assert {row["label"] for row in merged["consensus_annotations"]} == {"RISK_REVIEW"}
    arbitration = merged["arbitration_assignment"]
    assert arbitration["review_role"] == "ARBITER"
    assert arbitration["peer_answers_hidden"] is False
    assert arbitration["sample_count"] == 1
    assert len(arbitration["events"][0]["conflict_options"]) == 2
    assert "event-private" not in stable_json(arbitration)

    arbiter = make_submission(
        arbitration,
        axes_by_token={
            arbitration["events"][0]["sample_token"]: (
                "UNCLEAR",
                "UNCLEAR",
                "CONFLICTED",
            )
        },
    )
    final = finalize_with_arbitration(merged, arbiter)
    assert final["annotation_count"] == 3
    assert final["label_counts"] == {"ABSTAIN": 1, "RISK_REVIEW": 2}
    assert final["resolution_counts"] == {"ARBITRATED": 1, "CONSENSUS": 2}
    assert final["split"] == "UNASSIGNED"
    assert final["freeze_required_before_training_or_blind_evaluation"] is True
    assert all(validate_annotation(row) == [] for row in final["annotations"])


def test_conflict_cannot_be_finalized_without_distinct_third_human(batch: dict) -> None:
    a = make_submission(batch["assignments"]["A"])
    b_assignment = batch["assignments"]["B"]
    sample_id = "sample-1"
    token = next(
        token
        for token, mapped in batch["owner_manifest"]["token_maps"]["B"].items()
        if mapped == sample_id
    )
    b = make_submission(
        b_assignment,
        axes_by_token={token: ("UNCLEAR", "UNCLEAR", "INSUFFICIENT")},
    )
    merged = merge_dual_submissions(
        batch["owner_manifest"], a, b, arbiter_token="third-human"
    )
    with pytest.raises(ValueError, match="third-human arbiter"):
        finalize_with_arbitration(merged)

    duplicate = dict(merged)
    assignment = dict(duplicate["arbitration_assignment"])
    assignment["reviewer_token"] = batch["assignments"]["A"]["reviewer_token"]
    # Rebuild the assignment binding so validation reaches the independent-principal gate.
    from app.services import human_gold_review as service

    assignment["assignment_sha256"] = service._assignment_digest(assignment)
    duplicate["arbitration_assignment"] = assignment
    arbiter = make_submission(assignment)
    with pytest.raises(ValueError, match="third independent reviewer"):
        finalize_with_arbitration(duplicate, arbiter)


def test_prelabelled_or_unmasked_samples_are_rejected() -> None:
    labelled = sample(9)
    labelled["label"] = "RISK_REVIEW"
    with pytest.raises(ValueError, match="prohibited pre-label"):
        build_offline_batch(
            [labelled],
            batch_id="BAD",
            expires_at="2026-09-01T00:00:00+00:00",
        )
    market_leak = sample(10)
    market_leak["content"]["post_event_market_data_included"] = True
    with pytest.raises(ValueError, match="post_event_market_data_included"):
        build_offline_batch(
            [market_leak],
            batch_id="BAD-2",
            expires_at="2026-09-01T00:00:00+00:00",
        )


def test_cli_build_creates_self_contained_private_ab_zip(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    samples_path = tmp_path / "samples.json"
    samples_path.write_text(
        json.dumps({"samples": [sample(21), sample(22)]}, ensure_ascii=False),
        encoding="utf-8",
    )
    output = tmp_path / "human-gold-kit"
    archive = tmp_path / "human-gold-kit.zip"
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "human_gold_review_kit.py"),
            "build",
            "--samples",
            str(samples_path),
            "--output",
            str(output),
            "--zip",
            str(archive),
            "--batch-id",
            "HGR-CLI-TEST",
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert archive.is_file()
    assert (output / "成员A_私密发送" / "审核工具_成员A.html").is_file()
    assert (output / "成员B_私密发送" / "审核工具_成员B.html").is_file()
    assert (output / "负责人材料_禁止发给组员" / "owner_manifest.json").is_file()
    assert "event-private" not in (
        output / "成员A_私密发送" / "审核工具_成员A.html"
    ).read_text(encoding="utf-8")
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
    assert any(name.endswith("成员A_私密发送/审核工具_成员A.html") for name in names)
    assert any(name.endswith("负责人材料_禁止发给组员/owner_manifest.json") for name in names)
