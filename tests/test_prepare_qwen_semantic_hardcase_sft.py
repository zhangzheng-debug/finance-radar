import json
import zipfile

from app.models.qwen_risk_contract import expected_semantic_payload
from scripts.prepare_qwen_semantic_hardcase_sft import (
    TARGETS,
    _bounded_content,
    classify_hardcase,
    packet_to_content,
    prepare,
)


def test_realized_chapter_11_is_priority() -> None:
    decision = classify_hardcase(
        "The debtors filed the Chapter 11 plan. All common stock was cancelled and is of no force and effect."
    )
    assert decision == (TARGETS["PRIORITY"], "bankruptcy_restructuring_or_equity_cancellation")


def test_capital_failure_with_curtailment_is_priority() -> None:
    decision = classify_hardcase(
        "If we are unable to raise additional capital, we may need to reduce expenses to continue as a going concern."
    )
    assert decision == (TARGETS["PRIORITY"], "capital_exhaustion_or_operating_curtailment")


def test_bare_form_25_is_priority() -> None:
    decision = classify_hardcase('headline":"25 - Example Corp (Filer) document_type":"25"')
    assert decision == (TARGETS["PRIORITY"], "binding_listing_removal_or_suspension")


def test_paid_form_25_exit_is_a_contrast() -> None:
    decision = classify_hardcase(
        "Form 25 followed the completed acquisition in which holders received $18.50 per share in cash."
    )
    assert decision == (TARGETS["NEUTRAL"], "paid_or_completed_listing_exit")


def test_hypothetical_spac_liquidation_is_a_contrast() -> None:
    decision = classify_hardcase(
        "If we are unable to consummate a business combination, we may be required to liquidate the trust account."
    )
    assert decision == (TARGETS["NEUTRAL"], "hypothetical_liquidation_or_default")


def test_spac_going_concern_is_not_a_general_operating_company_alarm() -> None:
    decision = classify_hardcase(
        "Yorkville Acquisition Corp seeks an initial business combination. These conditions raise "
        "substantial doubt about its ability to continue as a going concern."
    )
    assert decision == (TARGETS["NEUTRAL"], "spac_going_concern_is_lifecycle_risk")


def test_packet_conversion_keeps_source_text_but_not_evidence_status() -> None:
    content = packet_to_content(
        {
            "event_date": "2026-08-01",
            "claim": {
                "title": "10-Q - Example",
                "summary": "Filed report",
                "local_received_at": "2026-08-01T01:00:00Z",
                "source_published_at": "2026-08-01T00:00:00Z",
            },
            "evidence": [
                {
                    "form": "10-Q",
                    "items": ["Part I"],
                    "filing_date": "2026-08-01",
                    "evidence_passage": "Substantial doubt exists about our ability to continue as a going concern.",
                    "evidence_status": "DISCOVERY_ONLY",
                }
            ],
        }
    )
    assert content["headline"] == "10-Q - Example"
    assert content["passages"][0]["passage"].startswith("Substantial doubt")
    assert "evidence_status" not in content["passages"][0]


def test_bounded_content_centers_the_rule_match() -> None:
    content = {
        "headline": "10-Q - Example",
        "summary": "Filed report",
        "passages": [
            {
                "passage": "A" * 5000
                + " unable to raise additional capital and may need to reduce operations "
                + "B" * 5000
            }
        ],
    }
    bounded = _bounded_content(content, "capital_exhaustion_or_operating_curtailment")
    passage = bounded["passages"][0]["passage"]
    assert "unable to raise additional capital" in passage
    assert len(passage) < 4000


def test_prepare_excludes_every_owner_manifest_event(tmp_path) -> None:
    package = tmp_path / "kit.zip"
    owner_content = {
        "headline": "25 - Sealed Example",
        "summary": "",
        "passages": [{"passage": "Notification of removal from listing."}],
    }
    owner = {
        "manifest_sha256": "owner-hash",
        "samples": [{"event_id": "SEALED", "content": owner_content}],
    }
    packet = {
        "record_type": "event",
        "event_id": "INDEPENDENT",
        "event_date": "2026-08-01",
        "company_name": "Example",
        "claim": {"title": "25 - Independent Example", "summary": "Filed Form 25"},
        "evidence": [{"form": "25", "evidence_passage": "Notification of removal from listing."}],
    }
    sealed_packet = dict(packet, event_id="SEALED", claim={"title": "25 - Sealed Example"})
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("owner_manifest.json", json.dumps(owner))
        archive.writestr(
            "A-0001.input.jsonl",
            "\n".join(
                [
                    json.dumps({"record_type": "manifest"}),
                    json.dumps(sealed_packet),
                    json.dumps(packet),
                ]
            ),
        )
    target = expected_semantic_payload("NOT_MATERIAL_ADVERSE", "NEUTRAL")
    base_row = {
        "messages": [
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": json.dumps({"headline": "Routine", "passages": []})},
            {"role": "assistant", "content": json.dumps(target)},
        ],
        "metadata": {"sample_id": "base", "event_id": "BASE", "content_sha256": "old"},
    }
    base_train = tmp_path / "base.jsonl"
    base_train.write_text(json.dumps(base_row) + "\n", encoding="utf-8")
    manifest = prepare(
        owner_package=package,
        base_train=base_train,
        output_dir=tmp_path / "out",
        per_rule_cap=4,
        base_repeat=1,
    )
    assert manifest["owner_manifest_events_excluded"] == 1
    assert manifest["exclusion_counts"]["owner_manifest_event"] == 1
    assert manifest["weak_supervision_rows"] == 1
    assert manifest["owner_holdout_opened"] is False
    weak = (tmp_path / "out" / "qwen_risk_sft_weak_hardcases.jsonl").read_text()
    assert "INDEPENDENT" in weak
    assert "SEALED" not in weak
