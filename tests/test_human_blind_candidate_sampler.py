from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.services.ai_event_census import (
    BOUNDARY_VALUES,
    CONTRACT_VERSION as AI_CENSUS_CONTRACT_VERSION,
    PROMPT_VERSION,
    sha256_json,
    write_jsonl,
)
from app.services.human_blind_candidate_sampler import (
    HUMAN_BLIND_CONTRACT_VERSION,
    build_candidate_set,
    load_packets_from_census_package,
    packet_to_candidate,
)


def _packet(
    number: int,
    *,
    family: str | None = None,
    stable_id: str | None = None,
    chain_id: str | None = None,
) -> dict:
    event_id = f"EV-{number:04d}"
    packet = {
        "record_type": "event_packet",
        "schema_version": 1,
        "contract_version": AI_CENSUS_CONTRACT_VERSION,
        "event_id": event_id,
        "event_version": 1,
        "event_fingerprint": f"fp-{number}",
        "event_date": "2026-01-02",
        "stable_id": stable_id or f"issuer-{number}",
        "ticker_at_event": f"T{number}",
        "company_name": f"Issuer {number}",
        "proposed_event_family": family or f"family-{number % 3}",
        "proposed_event_type": "fixture",
        "discovery_source": "hidden-from-output",
        "claim": {
            "title": f"Issuer {number} disclosed an event",
            "summary": "The disclosure describes a potentially material adverse event.",
            "source_id": "discovery-source",
            "source_published_at": "2026-01-01T10:00:00+00:00",
            "local_received_at": "2026-01-02T12:00:00+00:00",
            "content_sha256": f"claim-{number}",
            "canonical_url": "https://discovery.invalid/item",
        },
        "evidence_count": 4,
        "evidence": [
            {
                "evidence_id": f"P0-{number}",
                "evidence_url": "https://primary.invalid/document",
                "filing_date": "2026-01-02",
                "form": "8-K",
                "items": "1.03",
                "evidence_passage": (
                    f"Issuer {number} filed an exact official passage describing the event "
                    "and its effective date in sufficient detail for independent review."
                ),
                "evidence_status": "confirmed_primary",
                "content_sha256": f"evidence-{number}",
                "source_id": "sec-current",
                "source_name": "SEC",
                "authority_tier": "P0_official",
                "source_type": "official_primary",
            },
            {
                "evidence_id": f"P1-{number}",
                "evidence_url": "https://issuer.invalid/release",
                "source_published_at": "2026-01-02T11:00:00+00:00",
                "filing_date": None,
                "form": "press release",
                "items": "",
                "evidence_passage": (
                    f"Issuer {number} also published an exact first-party passage with "
                    "additional contemporaneous context about the same event."
                ),
                "source_id": "issuer-newsroom",
                "authority_tier": "P1_issuer_official",
                "source_type": "issuer_primary",
            },
            {
                "evidence_id": f"FUTURE-{number}",
                "filing_date": "2026-01-03",
                "evidence_passage": "This exact official passage exists only after the claim cutoff and must be excluded.",
                "source_id": "sec-current",
                "authority_tier": "P0",
            },
            {
                "evidence_id": f"P2-{number}",
                "filing_date": "2026-01-01",
                "evidence_passage": "This long discovery passage is contextual and must not enter the blind sample.",
                "source_id": "news",
                "authority_tier": "P2",
            },
        ],
        "event_chain": {
            "chain_id": chain_id or f"chain-{number}",
            "chain_type": "fixture",
            "chain_role": "PRIMARY",
            "primary_event_id": event_id,
            "counts_as_primary_event": True,
        },
        **BOUNDARY_VALUES,
    }
    packet["packet_sha256"] = sha256_json(packet)
    return packet


def _assignment(batch_id: str, slot: str, packets: list[dict]) -> list[dict]:
    header = {
        "record_type": "assignment_header",
        "schema_version": 1,
        "contract_version": AI_CENSUS_CONTRACT_VERSION,
        "prompt_version": PROMPT_VERSION,
        "batch_id": batch_id,
        "reviewer_slot": slot,
        "shard_id": f"{batch_id}-{slot}-0001",
        "generated_at": "2026-08-20T00:00:00+00:00",
        "event_count": len(packets),
        "overlap_event_count": 0,
        "review_mode": "ai_assisted_advisory_census",
        **BOUNDARY_VALUES,
    }
    header["assignment_sha256"] = sha256_json({"header": header, "events": packets})
    return [header, *packets]


def test_candidate_uses_claim_known_at_and_only_contemporaneous_exact_p0_p1() -> None:
    packet = _packet(1)
    packet["canonical_status"] = "verified"
    packet["model_prediction"] = "RISK_REVIEW"
    packet["market_outcome"] = {"return_1d": -0.2}
    candidate, exclusions = packet_to_candidate(packet)

    assert candidate["content"]["contract_version"] == HUMAN_BLIND_CONTRACT_VERSION
    assert candidate["content"]["as_of"] == "2026-01-02T12:00:00+00:00"
    assert candidate["content"]["confirmed_facts"] == []
    assert [row["evidence_id"] for row in candidate["content"]["passages"]] == [
        "P0-1",
        "P1-1",
    ]
    assert exclusions["post_as_of_evidence"] == 1
    assert exclusions["not_p0_p1"] == 1
    serialized = json.dumps(candidate, ensure_ascii=False)
    assert "verified" not in serialized
    assert "RISK_REVIEW" not in serialized
    assert "return_1d" not in serialized
    assert "discovery.invalid" not in serialized
    assert candidate["split"] == "UNASSIGNED"


def test_selection_is_exact_deterministic_family_balanced_and_group_disjoint() -> None:
    packets = [_packet(index) for index in range(1, 13)]
    packets.append(copy.deepcopy(packets[0]))  # census overlap is harmless
    packets.append(_packet(50, stable_id="issuer-1"))
    packets.append(_packet(51, chain_id="chain-2"))

    first = build_candidate_set(packets, target_count=9, seed="fixed-test-seed")
    second = build_candidate_set(reversed(packets), target_count=9, seed="fixed-test-seed")

    assert first["row_count"] == 9
    assert first["sample_set_sha256"] == second["sample_set_sha256"]
    assert [row["sample_id"] for row in first["samples"]] == [
        row["sample_id"] for row in second["samples"]
    ]
    assert len({row["entity_group"] for row in first["samples"]}) == 9
    assert len({row["event_chain_group"] for row in first["samples"]}) == 9
    assert max(first["selected_family_counts"].values()) - min(
        first["selected_family_counts"].values()
    ) <= 1
    assert first["input_overlap_packet_count"] == 1
    assert first["issuer_overlap_count"] == 0
    assert first["event_chain_overlap_count"] == 0


def test_insufficient_candidates_fail_honestly() -> None:
    packets = [_packet(index, stable_id="same-issuer") for index in range(1, 6)]
    with pytest.raises(
        ValueError,
        match=r"insufficient leakage-safe zero-group-overlap candidates: 1/2",
    ):
        build_candidate_set(packets, target_count=2)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("proposed_event_family", "price_crash"),
        ("proposed_event_type", "one_day_crash_candidate"),
        ("proposed_event_type", "five-day-crash"),
        ("proposed_event_family", "price_gainer"),
        ("proposed_event_family", "market_return_outcome"),
    ],
)
def test_market_outcome_event_families_are_explicitly_ineligible(
    field: str, value: str
) -> None:
    packet = _packet(77)
    packet[field] = value

    with pytest.raises(ValueError, match="market-outcome event family/type is ineligible"):
        packet_to_candidate(packet)


def test_sampler_reports_market_outcome_rejections_instead_of_silently_selecting_them() -> None:
    packets = [_packet(1), _packet(2)]
    packets[1]["proposed_event_family"] = "price_loser"

    result = build_candidate_set(packets, target_count=1)

    assert result["row_count"] == 1
    assert any(
        key.startswith("market-outcome event family/type is ineligible")
        for key in result["rejection_counts"]
    )
    assert "price_loser" in result["market_outcome_event_identifiers_denied"]


def test_package_loader_requires_owner_proven_full_coverage(tmp_path: Path) -> None:
    root = tmp_path / "census"
    (root / "成员A" / "任务分片").mkdir(parents=True)
    (root / "成员B" / "任务分片").mkdir(parents=True)
    (root / "负责人材料").mkdir()
    packet_a = _packet(1)
    packet_b = _packet(2)
    write_jsonl(
        root / "成员A" / "任务分片" / "a.input.jsonl",
        _assignment("BATCH-1", "A", [packet_a]),
    )
    write_jsonl(
        root / "成员B" / "任务分片" / "b.input.jsonl",
        _assignment("BATCH-1", "B", [packet_b]),
    )
    owner = {
        "batch_id": "BATCH-1",
        "event_count": 2,
        "events": [{"event_id": "EV-0001"}, {"event_id": "EV-0002"}],
    }
    (root / "负责人材料" / "owner_index.json").write_text(
        json.dumps(owner), encoding="utf-8"
    )

    packets, metadata = load_packets_from_census_package(root)
    assert {row["event_id"] for row in packets} == {"EV-0001", "EV-0002"}
    assert metadata["owner_event_count"] == 2

    owner["events"].append({"event_id": "EV-MISSING"})
    owner["event_count"] = 3
    (root / "负责人材料" / "owner_index.json").write_text(
        json.dumps(owner), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="coverage does not match owner index"):
        load_packets_from_census_package(root)
