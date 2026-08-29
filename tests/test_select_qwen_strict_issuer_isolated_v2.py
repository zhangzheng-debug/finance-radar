from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.select_qwen_strict_issuer_isolated_v2 import (
    GENERAL_STRATUM,
    HIGH_RISK_STRATUM,
    OWNER_OUTPUT,
    PROVIDER_OUTPUT,
    SUPERSESSION_OUTPUT,
    high_risk_mechanism_matches,
    select_issuer_isolated_benchmark_v2,
    stable_json,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(stable_json(row) + "\n" for row in rows), encoding="utf-8")


def _content(sample_id: str, *, high_risk: bool = False, headline: str | None = None) -> dict:
    mechanism = (
        "The issuer reported substantial doubt about its ability to continue as a going concern."
        if high_risk
        else "The issuer filed a routine corporate update with complete primary-source evidence."
    )
    return {
        "as_of": "2026-08-30T00:00:00Z",
        "event_date": "2026-08-29",
        "focal_subject": {"anonymous_id": f"subject-{sample_id}", "role": "ISSUER"},
        "headline": headline or f"Issuer source filing for {sample_id}",
        "summary": mechanism,
        "passages": [
            {
                "authority_class": "PRIMARY_OFFICIAL",
                "document_type": "8-K",
                "item_section": "8.01",
                "passage": mechanism + " This complete passage supplies enough context for source parsing.",
            }
        ],
        "semantic_context": {
            "focal_subject": {"role": "ISSUER"},
            "source_excerpt_complete": True,
        },
        "source_excerpt_complete": True,
        "source_identity_hidden": True,
    }


def _provider(sample_id: str, *, high_risk: bool = False, content: dict | None = None) -> dict:
    return {"sample_id": sample_id, "content": content or _content(sample_id, high_risk=high_risk)}


def _digest(content: dict) -> str:
    return hashlib.sha256(stable_json(content).encode("utf-8")).hexdigest()


def _owner(
    provider: dict, *, event: str, entity: str, chain: str, source_hash: str | None = None,
) -> dict:
    digest = _digest(provider["content"])
    return {
        "schema_version": 2,
        "sample_id": provider["sample_id"],
        "source_event_id": event,
        "entity_group": entity,
        "event_chain_group": chain,
        "content_sha256": digest,
        "provider_text_sha256": digest,
        "source_text_sha256": source_hash or hashlib.sha256(("source:" + provider["sample_id"]).encode()).hexdigest(),
        "source_excerpt_complete": True,
    }


def _map_row(sample: str, event: str, issuer: str, quality: str = "STRONG_CIK") -> dict:
    return {
        "schema_version": 1,
        "sample_id": sample,
        "event_id": event,
        "canonical_issuer_key": issuer,
        "resolution_quality": quality,
    }


def _exposure(
    sample: str, event: str, issuer: str, chain: str, content_hash: str,
    *, entity: str | None = None, origin: str | None = None,
) -> dict:
    row = {
        "schema_version": 1,
        "sample_id": sample,
        "event_id": event,
        "canonical_issuer_key": issuer,
        "event_chain_group": chain,
        "content_sha256": content_hash,
        "exposure_split": "TRAIN",
        "source_dataset_sha256": "a" * 64,
    }
    if entity:
        row["entity_group"] = entity
    if origin:
        row["origin_sample_id"] = origin
    return row


def _fixture_files(
    tmp_path: Path, providers: list[dict], owners: list[dict], maps: list[dict],
    exposures: list[dict], legacy_ids: list[str], *, prefix: str = "",
) -> dict[str, Path]:
    provider = tmp_path / f"{prefix}provider.jsonl"
    owner = tmp_path / f"{prefix}owner.jsonl"
    issuer_map = tmp_path / f"{prefix}issuer-map.jsonl"
    exposure = tmp_path / f"{prefix}exposure.jsonl"
    legacy = tmp_path / f"{prefix}legacy.json"
    _write_jsonl(provider, providers)
    _write_jsonl(owner, owners)
    _write_jsonl(issuer_map, maps)
    _write_jsonl(exposure, exposures)
    legacy.write_text(stable_json({
        "schema_version": 1,
        "target": len(legacy_ids),
        "sample_ids": legacy_ids,
        "labels_read": False,
        "model_outputs_read": False,
        "market_results_read": False,
    }), encoding="utf-8")
    return {
        "provider": provider,
        "owner": owner,
        "issuer_map": issuer_map,
        "exposure": exposure,
        "legacy": legacy,
    }


def _run(paths: dict[str, Path], output: Path, *, general: int, high: int, legacy: int) -> dict:
    return select_issuer_isolated_benchmark_v2(
        strict500_provider_input=paths["provider"],
        strict500_owner_index=paths["owner"],
        canonical_issuer_map=paths["issuer_map"],
        training_exposure_registries=[paths["exposure"]],
        legacy_strict60_sample_ids=paths["legacy"],
        output_dir=output,
        selection_salt="frozen-test-salt",
        general_count=general,
        high_risk_count=high,
        expected_legacy_count=legacy,
        code_commit="0123456789abcdef",
    )


def test_deterministic_label_blind_two_stratum_freeze(tmp_path: Path) -> None:
    providers = [
        _provider("old", high_risk=True),
        *[_provider(f"g{number}") for number in range(1, 4)],
        *[_provider(f"h{number}", high_risk=True) for number in range(1, 4)],
    ]
    owners = [
        _owner(row, event=f"event-{row['sample_id']}", entity=f"entity-{row['sample_id']}", chain=f"chain-{row['sample_id']}")
        for row in providers
    ]
    maps = [
        _map_row(row["sample_id"], f"event-{row['sample_id']}", f"issuer:{row['sample_id']}")
        for row in providers
    ]
    maps.append(_map_row("train", "event-train", "issuer:train"))
    exposures = [_exposure("train", "event-train", "issuer:train", "chain-train", "b" * 64)]
    paths = _fixture_files(tmp_path, providers, owners, maps, exposures, ["old"])

    first = _run(paths, tmp_path / "out-a", general=2, high=2, legacy=1)
    second = _run(paths, tmp_path / "out-b", general=2, high=2, legacy=1)

    assert first == second
    for name in (PROVIDER_OUTPUT, OWNER_OUTPUT, SUPERSESSION_OUTPUT, "manifest.json"):
        assert (tmp_path / "out-a" / name).read_bytes() == (tmp_path / "out-b" / name).read_bytes()
    provider_rows = [json.loads(line) for line in (tmp_path / "out-a" / PROVIDER_OUTPUT).read_text().splitlines()]
    assert len(provider_rows) == 4
    assert all(set(row) == {"sample_id", "content"} for row in provider_rows)
    assert all("benchmark_stratum" not in stable_json(row) for row in provider_rows)
    owner_rows = [json.loads(line) for line in (tmp_path / "out-a" / OWNER_OUTPUT).read_text().splitlines()]
    assert len({row["canonical_issuer_key"] for row in owner_rows}) == 4
    assert {row["benchmark_stratum"] for row in owner_rows} == {GENERAL_STRATUM, HIGH_RISK_STRATUM}
    assert first["metrics_reporting_contract"]["required_views"] == [
        "OVERALL", GENERAL_STRATUM, HIGH_RISK_STRATUM,
    ]
    assert first["exposure_isolation"]["all_selected_overlap_counts_zero"] is True
    assert first["signal_policy"]["category_balancing_used"] is False
    supersession = json.loads((tmp_path / "out-a" / SUPERSESSION_OUTPUT).read_text())
    assert supersession["status"] == "CONSUMED_DIAGNOSTIC_AI_REFERENCE_NOT_HUMAN_GOLD"
    assert supersession["classification"] == "AI_NOT_HUMAN_GOLD"
    assert supersession["human_gold_claimed"] is False


def test_all_exposure_axes_and_legacy_issuer_alias_are_excluded(tmp_path: Path) -> None:
    clean_general = _provider("clean-g")
    clean_high = _provider("clean-h", high_risk=True)
    old = _provider("old")
    sample_overlap = _provider("sample-overlap")
    event_overlap = _provider("event-overlap")
    issuer_overlap = _provider("issuer-overlap")
    chain_overlap = _provider("chain-overlap")
    content_overlap = _provider("content-overlap")
    old_alias = _provider("old-alias")
    providers = [
        old, clean_general, clean_high, sample_overlap, event_overlap,
        issuer_overlap, chain_overlap, content_overlap, old_alias,
    ]
    owners = [
        _owner(row, event=f"event-{row['sample_id']}", entity=f"entity-{row['sample_id']}", chain=f"chain-{row['sample_id']}")
        for row in providers
    ]
    by_sample = {row["sample_id"]: row for row in owners}
    by_sample["event-overlap"]["source_event_id"] = "event-exposed"
    by_sample["issuer-overlap"]["source_event_id"] = "event-issuer-candidate"
    by_sample["chain-overlap"]["event_chain_group"] = "chain-exposed"
    exposed_content_hash = _digest(content_overlap["content"])

    maps = []
    for row in providers:
        event = by_sample[row["sample_id"]]["source_event_id"]
        issuer = f"issuer:{row['sample_id']}"
        if row["sample_id"] == "event-overlap":
            issuer = "issuer:event-exposed"
        if row["sample_id"] == "issuer-overlap":
            issuer = "issuer:shared"
        if row["sample_id"] in {"old", "old-alias"}:
            issuer = "issuer:legacy"
        maps.append(_map_row(row["sample_id"], event, issuer))
    maps.extend([
        _map_row("event-exposed-train", "event-exposed", "issuer:event-exposed"),
        _map_row("issuer-exposed-train", "event-issuer-train", "issuer:shared"),
        _map_row("chain-exposed-train", "event-chain-train", "issuer:chain-train"),
        _map_row("content-exposed-train", "event-content-train", "issuer:content-train"),
    ])
    exposures = [
        _exposure("sample-overlap", "event-sample-overlap", "issuer:sample-overlap", "chain-sample-overlap", "1" * 64),
        _exposure("event-exposed-train", "event-exposed", "issuer:event-exposed", "chain-event-train", "2" * 64),
        _exposure("issuer-exposed-train", "event-issuer-train", "issuer:shared", "chain-issuer-train", "3" * 64),
        _exposure("chain-exposed-train", "event-chain-train", "issuer:chain-train", "chain-exposed", "4" * 64),
        _exposure("content-exposed-train", "event-content-train", "issuer:content-train", "chain-content-train", exposed_content_hash),
    ]
    paths = _fixture_files(tmp_path, providers, owners, maps, exposures, ["old"])
    manifest = _run(paths, tmp_path / "out", general=1, high=1, legacy=1)

    reasons = manifest["selection"]["exclusion_reason_counts"]
    for reason in (
        "EXPOSURE_SAMPLE_ID",
        "EXPOSURE_EVENT_ID",
        "EXPOSURE_CANONICAL_ISSUER_KEY",
        "EXPOSURE_EVENT_CHAIN_GROUP",
        "EXPOSURE_CONTENT_HASH",
    ):
        assert reasons[reason] >= 1
    # A new sample for the consumed legacy issuer is excluded by canonical issuer.
    assert "old-alias" not in (tmp_path / "out" / PROVIDER_OUTPUT).read_text()
    assert manifest["exposure_isolation"]["selected_overlap_counts"] == {
        "sample_id": 0,
        "event_id": 0,
        "entity_group": 0,
        "canonical_issuer_key": 0,
        "event_chain_group": 0,
        "content_hash": 0,
    }


def test_rejects_answer_bearing_input_before_writing_output(tmp_path: Path) -> None:
    bad = _provider("bad")
    bad["content"]["materiality"] = "MATERIAL_ADVERSE"
    owner = _owner(bad, event="event-bad", entity="entity-bad", chain="chain-bad")
    paths = _fixture_files(
        tmp_path,
        [bad],
        [owner],
        [_map_row("bad", "event-bad", "issuer:bad"), _map_row("train", "event-train", "issuer:train")],
        [_exposure("train", "event-train", "issuer:train", "chain-train", "f" * 64)],
        ["bad"],
    )
    output = tmp_path / "out"
    with pytest.raises(ValueError, match="prohibited answer/prediction/outcome key"):
        _run(paths, output, general=1, high=0, legacy=1)
    assert not output.exists()


def test_insufficient_stratum_fails_without_partial_artifact(tmp_path: Path) -> None:
    old = _provider("old")
    only_general = _provider("only-general")
    providers = [old, only_general]
    owners = [
        _owner(row, event=f"event-{row['sample_id']}", entity=f"entity-{row['sample_id']}", chain=f"chain-{row['sample_id']}")
        for row in providers
    ]
    maps = [
        _map_row(row["sample_id"], f"event-{row['sample_id']}", f"issuer:{row['sample_id']}")
        for row in providers
    ] + [_map_row("train", "event-train", "issuer:train")]
    paths = _fixture_files(
        tmp_path, providers, owners, maps,
        [_exposure("train", "event-train", "issuer:train", "chain-train", "e" * 64)], ["old"],
    )
    output = tmp_path / "out"
    with pytest.raises(ValueError, match=f"insufficient {HIGH_RISK_STRATUM}"):
        _run(paths, output, general=1, high=1, legacy=1)
    assert not output.exists()


def test_exposure_registry_requires_strong_resolved_issuer(tmp_path: Path) -> None:
    old = _provider("old")
    candidate = _provider("candidate")
    providers = [old, candidate]
    owners = [
        _owner(row, event=f"event-{row['sample_id']}", entity=f"entity-{row['sample_id']}", chain=f"chain-{row['sample_id']}")
        for row in providers
    ]
    maps = [
        _map_row(row["sample_id"], f"event-{row['sample_id']}", f"issuer:{row['sample_id']}")
        for row in providers
    ] + [_map_row("train", "event-train", "issuer:train", quality="FROZEN_ALIAS")]
    paths = _fixture_files(
        tmp_path, providers, owners, maps,
        [_exposure("train", "event-train", "issuer:train", "chain-train", "d" * 64)], ["old"],
    )
    output = tmp_path / "out"
    with pytest.raises(ValueError, match="exposure canonical issuer is not strong-resolved"):
        _run(paths, output, general=1, high=0, legacy=1)
    assert not output.exists()


def test_high_risk_predicate_is_frozen_document_or_mechanism_only() -> None:
    price_only = _content("price-only")
    price_only["summary"] = "The share price fell sharply after the announcement, with no source mechanism stated."
    price_only["passages"][0]["passage"] = "The filing contains an ordinary update and no enumerated downside mechanism."
    assert high_risk_mechanism_matches(price_only) == []

    document_signal = _content("late-filer")
    document_signal["passages"][0]["document_type"] = "NT 10-Q"
    assert "DOCUMENT_TYPE:NT 10-Q" in high_risk_mechanism_matches(document_signal)

    item_signal = _content("bankruptcy-item")
    item_signal["passages"][0]["item_section"] = "1.03;9.01"
    assert "ITEM_SECTION:1.03" in high_risk_mechanism_matches(item_signal)

    text_signal = _content("clinical")
    text_signal["summary"] = "The trial did not meet its primary endpoint."
    assert "MECHANISM:CLINICAL_REGULATORY_FAILURE" in high_risk_mechanism_matches(text_signal)
