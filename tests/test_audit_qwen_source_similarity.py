from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.audit_qwen_source_similarity import (
    audit_qwen_source_similarity,
    main,
    stable_json,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(stable_json(row) + "\n" for row in rows), encoding="utf-8")


def _content(headline: str, passage: str, summary: str = "") -> dict:
    return {
        "headline": headline,
        "summary": summary,
        "passages": [{"passage": passage}],
    }


def _training(sample: str, event: str, issuer: str, content: dict) -> dict:
    return {
        "messages": [
            {"role": "system", "content": "classify source text"},
            {"role": "user", "content": stable_json(content)},
            {"role": "assistant", "content": '{"materiality":"UNCLEAR"}'},
        ],
        "metadata": {
            "sample_id": sample,
            "event_id": event,
            "canonical_issuer_key": issuer,
        },
    }


def _provider(sample: str, content: dict) -> dict:
    return {"sample_id": sample, "content": content}


def test_audit_detects_bidirectional_headlines_and_shingle_overlap(tmp_path: Path) -> None:
    train = tmp_path / "train.jsonl"
    dev = tmp_path / "dev.jsonl"
    strict = tmp_path / "strict.jsonl"
    output = tmp_path / "report.json"
    shared = "alpha beta gamma delta epsilon zeta eta theta"
    _write_jsonl(train, [
        _training(
            "train-template", "event-template", "issuer:template",
            _content("25-NSE - Nasdaq Stock Market LLC Filed by", shared),
        )
    ])
    _write_jsonl(dev, [
        _training(
            "train-alias", "event-alias", "issuer:raw-name",
            _content("Happy City Holdings Limited", "unrelated source passage"),
        )
    ])
    _write_jsonl(strict, [
        _provider(
            "strict-template",
            _content("25-NSE - Nasdaq Stock Market LLC Filed by", shared),
        ),
        _provider(
            "strict-alias",
            _content(
                "6-K - Happy City Holdings Ltd Filer",
                "The filing by Happy City Holdings Limited described an ordinary update.",
            ),
        ),
    ])

    report = audit_qwen_source_similarity(
        train_unique=train,
        dev=dev,
        strict_provider=strict,
        output=output,
    )

    assert report["result"] == "LEAKAGE_DETECTED"
    assert report["strict_labels_read"] is False
    assert report["counts"] == {
        "training_rows": 2,
        "strict_rows": 2,
        "pair_comparisons": 4,
        "headline_overlap_pairs": 2,
        "shingle_threshold_pairs": 1,
        "violating_pairs": 2,
        "training_samples_with_violations": 2,
        "strict_samples_with_violations": 2,
    }
    by_training = {row["training_sample_id"]: row for row in report["violations"]}
    assert set(by_training["train-template"]["reasons"]) == {
        "STRICT_HEADLINE_IN_TRAINING_SOURCE",
        "TRAINING_HEADLINE_IN_STRICT_SOURCE",
        "SHINGLE_JACCARD_AT_OR_ABOVE_THRESHOLD",
    }
    assert by_training["train-alias"]["reasons"] == [
        "TRAINING_HEADLINE_IN_STRICT_SOURCE"
    ]
    assert json.loads(output.read_text(encoding="utf-8")) == report


def test_clean_audit_passes_and_cli_returns_zero(tmp_path: Path) -> None:
    train = tmp_path / "train.jsonl"
    dev = tmp_path / "dev.jsonl"
    strict = tmp_path / "strict.jsonl"
    output = tmp_path / "report.json"
    _write_jsonl(train, [
        _training(
            "train-clean", "event-clean", "issuer:clean",
            _content("Issuer Alpha quarterly filing", "cash flow and routine governance update"),
        )
    ])
    _write_jsonl(dev, [])
    _write_jsonl(strict, [
        _provider(
            "strict-clean",
            _content("Issuer Omega clinical update", "trial enrollment reached its target"),
        )
    ])

    exit_code = main([
        "--train-unique", str(train),
        "--dev", str(dev),
        "--strict-provider", str(strict),
        "--output", str(output),
    ])

    report = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert report["result"] == "PASS"
    assert report["counts"]["violating_pairs"] == 0


def test_cli_writes_report_before_nonzero_leak_exit(tmp_path: Path) -> None:
    train = tmp_path / "train.jsonl"
    dev = tmp_path / "dev.jsonl"
    strict = tmp_path / "strict.jsonl"
    output = tmp_path / "report.json"
    content = _content("Repeated issuer headline long enough", "same source words here")
    _write_jsonl(train, [_training("train", "event", "issuer", content)])
    _write_jsonl(dev, [])
    _write_jsonl(strict, [_provider("strict", content)])

    exit_code = main([
        "--train-unique", str(train),
        "--dev", str(dev),
        "--strict-provider", str(strict),
        "--output", str(output),
        "--threshold", "0.8",
    ])

    assert exit_code == 1
    assert json.loads(output.read_text(encoding="utf-8"))["result"] == "LEAKAGE_DETECTED"


def test_strict_input_rejects_any_non_provider_field(tmp_path: Path) -> None:
    train = tmp_path / "train.jsonl"
    dev = tmp_path / "dev.jsonl"
    strict = tmp_path / "strict.jsonl"
    output = tmp_path / "report.json"
    _write_jsonl(train, [])
    _write_jsonl(dev, [])
    row = _provider("strict", _content("Strict source headline", "source passage"))
    row["expected"] = {"materiality": "MATERIAL_ADVERSE"}
    _write_jsonl(strict, [row])

    with pytest.raises(ValueError, match="exactly sample_id and content"):
        audit_qwen_source_similarity(
            train_unique=train,
            dev=dev,
            strict_provider=strict,
            output=output,
        )
    assert not output.exists()


@pytest.mark.parametrize("threshold", [-0.01, 1.01])
def test_threshold_must_be_probability(tmp_path: Path, threshold: float) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="between 0 and 1"):
        audit_qwen_source_similarity(
            train_unique=path,
            dev=path,
            strict_provider=path,
            output=tmp_path / "report.json",
            threshold=threshold,
        )
