from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from app.models.qwen_risk_contract import expected_semantic_payload
from scripts.build_canonical_issuer_map import build_canonical_issuer_map
from scripts.build_qwen_semantic_core_v4_weak_dataset import build_dataset, stable_json


def _content(headline: str, *, focal_subject: dict | None = None) -> dict:
    value = {
        "as_of": "2026-01-01T00:00:00Z",
        "event_date": "2026-01-01",
        "headline": headline,
        "summary": "Label-free source summary.",
        "passages": [],
    }
    if focal_subject is not None:
        value["focal_subject"] = focal_subject
    return value


def _sft(sample: str, event: str, entity: str, content: dict) -> dict:
    target = expected_semantic_payload("NOT_MATERIAL_ADVERSE", "NEUTRAL")
    return {
        "messages": [
            {"role": "system", "content": "old"},
            {"role": "user", "content": stable_json(content)},
            {"role": "assistant", "content": stable_json(target)},
        ],
        "metadata": {
            "sample_id": sample,
            "event_id": event,
            "entity_group": entity,
            "event_chain_group": f"chain:{event}",
        },
    }


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(stable_json(row) + "\n" for row in rows), encoding="utf-8")


def _map_rows(path: Path) -> dict[str, dict]:
    return {
        row["sample_id"]: row
        for row in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line)
    }


def _raw_zip(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "raw-packets.zip"
    payload = "".join(stable_json(row) + "\n" for row in rows)
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("delivery/任务分片/test.input.jsonl", payload)
    return path


def test_same_cik_across_legacy_hashes_is_canonical_strict_leak(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.jsonl"
    provider = tmp_path / "strict-provider.jsonl"
    index = tmp_path / "strict-index.jsonl"
    issuer_map = tmp_path / "canonical-issuer-map.jsonl"
    raw_zip = _raw_zip(tmp_path, [
        {
            "event_id": "event-weak", "stable_id": "permaticker:123456",
            "ticker_at_event": "EXM", "company_name": "Example Corp",
        },
        {
            "event_id": "event-strict", "stable_id": "cik:0000123456",
            "ticker_at_event": "EXM", "company_name": "Example Corp",
        },
    ])
    _write(candidate, [
        _sft("weak", "event-weak", "issuer:legacy-a", _content(
            "8-K - Example Corp (0000123456) (Filer)",
        )),
    ])
    _write(provider, [{
        "sample_id": "strict",
        "content": _content("25-NSE - Example Corp (0000123456) (Subject)"),
    }])
    _write(index, [{
        "sample_id": "strict", "source_event_id": "event-strict",
        "entity_group": "issuer:legacy-b", "event_chain_group": "chain:strict",
    }])

    summary = build_canonical_issuer_map(
        candidate_sft=[candidate], strict_provider_input=provider,
        strict_owner_index=index, raw_packet_zip=raw_zip, output=issuer_map,
    )
    mapped = _map_rows(issuer_map)
    assert summary["labels_read"] is False
    assert summary["raw_packet_stats"]["unique_event_packets"] == 2
    assert Path(summary["manifest"]).is_file()
    assert mapped["weak"]["canonical_issuer_key"] == "issuer:v1:sec_cik:0000123456"
    assert mapped["strict"]["canonical_issuer_key"] == mapped["weak"]["canonical_issuer_key"]
    assert mapped["strict"]["resolution_quality"] == "STRONG_CIK"

    output = tmp_path / "dataset"
    manifest = build_dataset(
        dual_consensus=[candidate], ai_assisted=[], deterministic_weak=[],
        strict_indices=[index], canonical_issuer_map=issuer_map, output_dir=output,
    )
    assert manifest["leakage_excluded_rows"] == 1
    assert manifest["strict_set_counts"]["canonical_issuer_key"] == 1
    assert manifest["canonical_issuer_map"]["resolved_rows"] == 2
    assert manifest["train_dev_canonical_issuer_overlap"] == 0
    leakage = json.loads((output / "leakage_exclusions.jsonl").read_text(encoding="utf-8"))
    assert "canonical_issuer_key" in leakage["reasons"]


def test_same_cik_across_legacy_hashes_stays_in_one_split(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.jsonl"
    provider = tmp_path / "strict-provider.jsonl"
    index = tmp_path / "strict-index.jsonl"
    issuer_map = tmp_path / "canonical-issuer-map.jsonl"
    raw_zip = _raw_zip(tmp_path, [
        {
            "event_id": "event-first", "stable_id": "permaticker:123456",
            "ticker_at_event": "EXM", "company_name": "Example Corp",
        },
        {
            "event_id": "event-second", "stable_id": "cik:0000123456",
            "ticker_at_event": "EXM", "company_name": "Example Corp",
        },
    ])
    _write(candidate, [
        _sft("first", "event-first", "issuer:legacy-a", _content(
            "8-K - Example Corp (0000123456) (Filer)",
        )),
        _sft("second", "event-second", "issuer:legacy-b", _content(
            "10-Q - Example Corp (0000123456) (Filer)",
        )),
    ])
    _write(provider, [])
    _write(index, [])
    build_canonical_issuer_map(
        candidate_sft=[candidate], strict_provider_input=provider,
        strict_owner_index=index, raw_packet_zip=raw_zip, output=issuer_map,
    )
    output = tmp_path / "dataset"
    manifest = build_dataset(
        dual_consensus=[candidate], ai_assisted=[], deterministic_weak=[],
        strict_indices=[index], canonical_issuer_map=issuer_map, output_dir=output,
    )
    locations: dict[str, str] = {}
    for split, name in (("TRAIN", "qwen_core_v4_train_unique.jsonl"), ("DEV", "qwen_core_v4_dev.jsonl")):
        for line in (output / name).read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            locations[row["metadata"]["sample_id"]] = split
    assert locations["first"] == locations["second"]
    assert manifest["component_count"] == 1
    assert manifest["train_dev_canonical_issuer_overlap"] == 0
    assert manifest["train_dev_unresolved_canonical_issuer_rows"] == 0
    registry_path = output / "training_exposure_registry.jsonl"
    registry = [json.loads(line) for line in registry_path.read_text(encoding="utf-8").splitlines()]
    assert len(registry) == 2
    assert all(set(row) == {
        "schema_version", "sample_id", "event_id", "entity_group",
        "canonical_issuer_key", "event_chain_group", "content_sha256", "exposure_split",
    } for row in registry)
    assert all("messages" not in stable_json(row) and "label" not in stable_json(row) for row in registry)
    assert (output / "training_exposure_registry.jsonl.sha256").is_file()
    assert manifest["output_sha256"]["training_exposure_registry"]


def test_filed_by_exchange_cik_is_ignored_not_merged(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.jsonl"
    provider = tmp_path / "strict-provider.jsonl"
    index = tmp_path / "strict-index.jsonl"
    issuer_map = tmp_path / "canonical-issuer-map.jsonl"
    raw_zip = _raw_zip(tmp_path, [
        {
            "event_id": "event-a", "stable_id": None,
            "ticker_at_event": "AAAA", "company_name": "Subject A Corp",
        },
        {
            "event_id": "event-b", "stable_id": None,
            "ticker_at_event": "BBBB", "company_name": "Subject B Corp",
        },
    ])
    _write(candidate, [
        _sft("subject-a", "event-a", "legacy:a", _content(
            "25-NSE - Nasdaq Stock Market LLC (0001354457) (Filed by)",
            focal_subject={"ticker": "AAAA"},
        )),
        _sft("subject-b", "event-b", "legacy:b", _content(
            "25-NSE - Nasdaq Stock Market LLC (0001354457) (Filed by)",
            focal_subject={"ticker": "BBBB"},
        )),
    ])
    _write(provider, [])
    _write(index, [])

    build_canonical_issuer_map(
        candidate_sft=[candidate], strict_provider_input=provider,
        strict_owner_index=index, raw_packet_zip=raw_zip, output=issuer_map,
    )
    mapped = _map_rows(issuer_map)
    assert mapped["subject-a"]["canonical_issuer_key"] != mapped["subject-b"]["canonical_issuer_key"]
    assert "0001354457" not in mapped["subject-a"]["canonical_issuer_key"]
    assert "0001354457" not in mapped["subject-b"]["canonical_issuer_key"]
    assert mapped["subject-a"]["resolution_quality"] == "PROVISIONAL_RAW_NAME"
    assert mapped["subject-b"]["resolution_quality"] == "PROVISIONAL_RAW_NAME"
    assert "HEADLINE_FILED_BY_CIK_IGNORED:0001354457" in mapped["subject-a"]["resolution_provenance"]


def test_nasdaq_proxy_name_does_not_merge_rsvr_pcsc_ffai(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.jsonl"
    provider = tmp_path / "strict-provider.jsonl"
    index = tmp_path / "strict-index.jsonl"
    issuer_map = tmp_path / "canonical-issuer-map.jsonl"
    tickers = ["RSVR", "PCSC", "FFAI"]
    _write(candidate, [
        _sft(ticker.lower(), f"event-{ticker.lower()}", f"legacy:{ticker}", _content(
            "25-NSE - Nasdaq Stock Market LLC (0001354457) (Filed by)",
            focal_subject={"ticker": ticker},
        ))
        for ticker in tickers
    ])
    _write(provider, [])
    _write(index, [])
    raw_zip = _raw_zip(tmp_path, [
        {
            "event_id": f"event-{ticker.lower()}", "stable_id": None,
            "ticker_at_event": ticker, "company_name": "Nasdaq Stock Market LLC",
        }
        for ticker in tickers
    ])
    build_canonical_issuer_map(
        candidate_sft=[candidate], strict_provider_input=provider,
        strict_owner_index=index, raw_packet_zip=raw_zip, output=issuer_map,
    )
    mapped = _map_rows(issuer_map)
    keys = {mapped[ticker.lower()]["canonical_issuer_key"] for ticker in tickers}
    assert len(keys) == 3
    assert all(key.startswith("issuer:v1:raw_ticker:") for key in keys)
    assert all(mapped[ticker.lower()]["resolution_quality"] == "PROVISIONAL_RAW_TICKER" for ticker in tickers)
    assert all(
        "RAW_PROXY_OR_MULTI_TICKER_NAME_IGNORED"
        in mapped[ticker.lower()]["resolution_provenance"]
        for ticker in tickers
    )


def test_dynamic_name_with_more_than_three_tickers_is_ignored(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.jsonl"
    provider = tmp_path / "strict-provider.jsonl"
    index = tmp_path / "strict-index.jsonl"
    issuer_map = tmp_path / "canonical-issuer-map.jsonl"
    tickers = ["AAAA", "BBBB", "CCCC", "DDDD"]
    _write(candidate, [
        _sft(ticker, f"event-{ticker}", f"legacy:{ticker}", _content(
            f"Issuer status update for {ticker}", focal_subject={"ticker": ticker},
        ))
        for ticker in tickers
    ])
    _write(provider, [])
    _write(index, [])
    raw_zip = _raw_zip(tmp_path, [
        {
            "event_id": f"event-{ticker}", "stable_id": None,
            "ticker_at_event": ticker, "company_name": "Generic Listing Agent LLC",
        }
        for ticker in tickers
    ])
    build_canonical_issuer_map(
        candidate_sft=[candidate], strict_provider_input=provider,
        strict_owner_index=index, raw_packet_zip=raw_zip, output=issuer_map,
    )
    mapped = _map_rows(issuer_map)
    assert len({row["canonical_issuer_key"] for row in mapped.values()}) == 4
    assert all(row["resolution_quality"] == "PROVISIONAL_RAW_TICKER" for row in mapped.values())


def test_ticker_linked_to_multiple_ciks_is_unresolved(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.jsonl"
    provider = tmp_path / "strict-provider.jsonl"
    index = tmp_path / "strict-index.jsonl"
    issuer_map = tmp_path / "canonical-issuer-map.jsonl"
    _write(candidate, [
        _sft("ambiguous", "event-ambiguous", "legacy:ambiguous", _content("Issuer status update")),
    ])
    _write(provider, [])
    _write(index, [])
    raw_zip = _raw_zip(tmp_path, [
        {
            "event_id": "anchor-one", "stable_id": "cik:0000000001",
            "ticker_at_event": "DUP", "company_name": "First Corp",
        },
        {
            "event_id": "anchor-two", "stable_id": "cik:0000000002",
            "ticker_at_event": "DUP", "company_name": "Second Corp",
        },
        {
            "event_id": "event-ambiguous", "stable_id": None,
            "ticker_at_event": "DUP", "company_name": None,
        },
    ])
    build_canonical_issuer_map(
        candidate_sft=[candidate], strict_provider_input=provider,
        strict_owner_index=index, raw_packet_zip=raw_zip, output=issuer_map,
    )
    mapped = _map_rows(issuer_map)["ambiguous"]
    assert mapped["canonical_issuer_key"] is None
    assert mapped["resolution_quality"] == "UNRESOLVED"
    assert "RAW_MULTI_CIK_TICKER_IGNORED" in mapped["resolution_provenance"]


def test_ambiguous_raw_graph_is_counted_as_unresolved(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.jsonl"
    provider = tmp_path / "strict-provider.jsonl"
    index = tmp_path / "strict-index.jsonl"
    issuer_map = tmp_path / "canonical-issuer-map.jsonl"
    _write(candidate, [
        _sft("ambiguous", "event-ambiguous", "legacy:ambiguous", _content("Issuer status update")),
    ])
    _write(provider, [])
    _write(index, [])
    raw_zip = _raw_zip(tmp_path, [
        {
            "event_id": "anchor-name", "stable_id": "cik:0000000001",
            "ticker_at_event": "ONE", "company_name": "Shared Name Corp",
        },
        {
            "event_id": "anchor-ticker", "stable_id": "cik:0000000002",
            "ticker_at_event": "TWO", "company_name": "Other Corp",
        },
        {
            "event_id": "event-ambiguous", "stable_id": None,
            "ticker_at_event": "TWO", "company_name": "Shared Name Corp",
        },
    ])
    summary = build_canonical_issuer_map(
        candidate_sft=[candidate], strict_provider_input=provider,
        strict_owner_index=index, raw_packet_zip=raw_zip, output=issuer_map,
    )
    mapped = _map_rows(issuer_map)["ambiguous"]
    assert mapped["canonical_issuer_key"] is None
    assert mapped["resolution_quality"] == "AMBIGUOUS_RAW_GRAPH"
    assert summary["resolved_rows"] == 0
    assert summary["unresolved_rows"] == 1


def test_maxn_maxnq_requires_and_uses_frozen_alias(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.jsonl"
    provider = tmp_path / "strict-provider.jsonl"
    index = tmp_path / "strict-index.jsonl"
    aliases = tmp_path / "aliases.json"
    issuer_map = tmp_path / "canonical-issuer-map.jsonl"
    raw_zip = _raw_zip(tmp_path, [
        {
            "event_id": "event-maxnq", "stable_id": None,
            "ticker_at_event": "MAXNQ", "company_name": "Maxeon Alias B",
        },
        {
            "event_id": "event-maxn", "stable_id": None,
            "ticker_at_event": "MAXN", "company_name": "Maxeon Alias A",
        },
    ])
    _write(candidate, [
        _sft("maxnq", "event-maxnq", "issuer:legacy-maxnq", _content(
            "Accepted official evidence for MAXNQ",
        )),
    ])
    _write(provider, [{
        "sample_id": "maxn", "content": _content("Accepted official evidence for MAXN"),
    }])
    _write(index, [{
        "sample_id": "maxn", "source_event_id": "event-maxn",
        "entity_group": "issuer:legacy-maxn", "event_chain_group": "chain:maxn",
    }])
    aliases.write_text(stable_json([{
        "canonical_issuer_key": "issuer:v1:alias:maxeon-solar",
        "tickers": ["MAXN", "MAXNQ"],
    }]), encoding="utf-8")

    build_canonical_issuer_map(
        candidate_sft=[candidate], strict_provider_input=provider,
        strict_owner_index=index, raw_packet_zip=raw_zip,
        alias_file=aliases, output=issuer_map,
    )
    mapped = _map_rows(issuer_map)
    assert mapped["maxn"]["canonical_issuer_key"] == "issuer:v1:alias:maxeon-solar"
    assert mapped["maxnq"]["canonical_issuer_key"] == mapped["maxn"]["canonical_issuer_key"]
    assert mapped["maxn"]["resolution_quality"] == "STRONG_FROZEN_ALIAS"
    assert mapped["maxnq"]["resolution_quality"] == "STRONG_FROZEN_ALIAS"

    output = tmp_path / "dataset"
    manifest = build_dataset(
        dual_consensus=[candidate], ai_assisted=[], deterministic_weak=[],
        strict_indices=[index], canonical_issuer_map=issuer_map, output_dir=output,
    )
    assert manifest["leakage_excluded_rows"] == 1


def test_missing_canonical_map_entry_fails_fast(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.jsonl"
    strict = tmp_path / "strict.jsonl"
    issuer_map = tmp_path / "canonical-issuer-map.jsonl"
    _write(candidate, [
        _sft("missing", "event-missing", "issuer:old", _content(
            "8-K - Missing Corp (0000000001) (Filer)",
        )),
    ])
    _write(strict, [])
    _write(issuer_map, [])
    with pytest.raises(ValueError, match="missing canonical issuer mapping"):
        build_dataset(
            dual_consensus=[candidate], ai_assisted=[], deterministic_weak=[],
            strict_indices=[strict], canonical_issuer_map=issuer_map,
            output_dir=tmp_path / "dataset",
        )

    _write(issuer_map, [{
        "sample_id": "missing", "event_id": "event-missing",
        "canonical_issuer_key": None, "resolution_quality": "UNRESOLVED",
    }])
    unresolved_manifest = build_dataset(
        dual_consensus=[candidate], ai_assisted=[], deterministic_weak=[],
        strict_indices=[strict], canonical_issuer_map=issuer_map,
        output_dir=tmp_path / "dataset-unresolved",
    )
    assert unresolved_manifest["canonical_unresolved_excluded_rows"] == 1
    assert unresolved_manifest["train_dev_unresolved_canonical_issuer_rows"] == 0

    _write(strict, [{
        "sample_id": "strict-unresolved", "source_event_id": "strict-event",
        "entity_group": "issuer:strict", "event_chain_group": "chain:strict",
    }])
    _write(issuer_map, [
        {
            "sample_id": "missing", "event_id": "event-missing",
            "canonical_issuer_key": "issuer:v1:test:resolved", "resolution_quality": "STRONG_TEST",
        },
        {
            "sample_id": "strict-unresolved", "event_id": "strict-event",
            "canonical_issuer_key": None, "resolution_quality": "UNRESOLVED",
        },
    ])
    with pytest.raises(ValueError, match="unresolved canonical issuer"):
        build_dataset(
            dual_consensus=[candidate], ai_assisted=[], deterministic_weak=[],
            strict_indices=[strict], canonical_issuer_map=issuer_map,
            output_dir=tmp_path / "dataset-strict-unresolved",
        )


def test_conflicting_sample_event_or_alias_fails_fast(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.jsonl"
    provider = tmp_path / "strict-provider.jsonl"
    index = tmp_path / "strict-index.jsonl"
    aliases = tmp_path / "aliases.json"
    raw_zip = _raw_zip(tmp_path, [{
        "event_id": "event-conflict", "stable_id": None,
        "ticker_at_event": "CNFL", "company_name": "Conflict Corp",
    }])
    _write(candidate, [
        _sft("conflict", "event-conflict", "issuer:old", _content(
            "8-K - Conflict Corp (0000123456) (Filer)",
            focal_subject={"ticker": "CNFL"},
        )),
    ])
    _write(provider, [])
    _write(index, [])
    aliases.write_text(stable_json([{
        "canonical_issuer_key": "issuer:v1:alias:not-the-cik",
        "tickers": ["CNFL"],
    }]), encoding="utf-8")
    with pytest.raises(ValueError, match="CIK/alias conflict"):
        build_canonical_issuer_map(
            candidate_sft=[candidate], strict_provider_input=provider,
            strict_owner_index=index, raw_packet_zip=raw_zip, alias_file=aliases,
            output=tmp_path / "canonical-issuer-map.jsonl",
        )

    conflicting_map = tmp_path / "conflicting-map.jsonl"
    _write(conflicting_map, [
        {"sample_id": "one", "event_id": "shared", "canonical_issuer_key": "issuer:a"},
        {"sample_id": "two", "event_id": "shared", "canonical_issuer_key": "issuer:b"},
    ])
    with pytest.raises(ValueError, match="conflicting canonical issuer keys for event_id shared"):
        build_dataset(
            dual_consensus=[candidate], ai_assisted=[], deterministic_weak=[],
            strict_indices=[index], canonical_issuer_map=conflicting_map,
            output_dir=tmp_path / "dataset-conflict",
        )
