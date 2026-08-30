from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import scripts.audit_qwen_train_dev_source_similarity as audit_module
from scripts.audit_qwen_train_dev_source_similarity import (
    AUDIT_NAME,
    EXCLUSIONS_NAME,
    audit_qwen_train_dev_source_similarity,
    main,
    stable_json,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(stable_json(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def _content(headline: str, passage: str, summary: str = "") -> dict:
    return {
        "as_of": "2026-08-30T00:00:00+00:00",
        "event_date": "2026-08-30",
        "headline": headline,
        "passages": [
            {
                "document_type": "8-K",
                "item_section": "1.01",
                "passage": passage,
                "published_at": "2026-08-30",
            }
        ],
        "summary": summary,
    }


def _dataset_row(
    sample_id: str,
    event_id: str,
    content: dict,
    *,
    assistant_secret: str,
) -> dict:
    return {
        "messages": [
            {"role": "system", "content": "classify source text"},
            {"role": "user", "content": stable_json(content)},
            # The audit must not parse, consume, or publish this target content.
            {"role": "assistant", "content": assistant_secret},
        ],
        "metadata": {
            "sample_id": sample_id,
            "event_id": event_id,
            "label_provenance": "SECRET_PROVENANCE_NOT_FOR_AUDIT",
            "weak_rule": "SECRET_WEAK_RULE_NOT_FOR_AUDIT",
        },
    }


def _words(prefix: str, count: int) -> str:
    return " ".join(f"{prefix}{index}" for index in range(count))


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _assert_sidecar(path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    sidecar = path.with_name(path.name + ".sha256")
    assert sidecar.read_text(encoding="ascii") == f"{digest}  {path.name}\n"


def test_detects_shingles_and_bidirectional_long_headlines_without_labels(
    tmp_path: Path,
) -> None:
    train = tmp_path / "train.jsonl"
    dev = tmp_path / "dev.jsonl"
    output = tmp_path / "audit-output"
    shared = _words("shared", 80)
    train_title = "Issuer Meridian definitive financing agreement notice"
    dev_title = "Issuer Nova corporate transaction disclosure notice"
    _write_jsonl(
        train,
        [
            _dataset_row(
                "train-shingle",
                "event-train-shingle",
                _content("Train A", shared),
                assistant_secret="SECRET_TRAIN_SHINGLE_LABEL",
            ),
            _dataset_row(
                "train-headline",
                "event-train-headline",
                _content(
                    train_title,
                    dev_title + " " + _words("trainonly", 80),
                ),
                assistant_secret="SECRET_TRAIN_HEADLINE_LABEL",
            ),
        ],
    )
    _write_jsonl(
        dev,
        [
            _dataset_row(
                "dev-shingle",
                "event-dev-shingle",
                _content("Dev B", shared),
                assistant_secret="SECRET_DEV_SHINGLE_LABEL",
            ),
            _dataset_row(
                "dev-headline",
                "event-dev-headline",
                _content(
                    dev_title,
                    train_title + " " + _words("devonly", 80),
                ),
                assistant_secret="SECRET_DEV_HEADLINE_LABEL",
            ),
        ],
    )

    report = audit_qwen_train_dev_source_similarity(
        train_unique=train,
        dev=dev,
        output_dir=output,
    )

    assert report["result"] == "LEAKAGE_DETECTED"
    assert report["labels_read"] is False
    assert report["assistant_message_content_consumed"] is False
    assert report["counts"] == {
        "train_rows": 2,
        "dev_rows": 2,
        "pair_comparisons": 4,
        "headline_overlap_pairs": 1,
        "shingle_threshold_pairs": 1,
        "violating_pairs": 2,
        "train_samples_with_violations": 2,
        "dev_samples_with_violations": 2,
        "quality_exclusion_rows": 2,
    }
    by_pair = {
        (row["train_sample_id"], row["dev_sample_id"]): row
        for row in report["violations"]
    }
    assert by_pair[("train-shingle", "dev-shingle")]["reasons"] == [
        "SHINGLE_JACCARD_AT_OR_ABOVE_THRESHOLD"
    ]
    assert by_pair[("train-headline", "dev-headline")]["reasons"] == [
        "TRAIN_HEADLINE_IN_DEV_SOURCE",
        "DEV_HEADLINE_IN_TRAIN_SOURCE",
    ]

    exclusions = _read_jsonl(output / EXCLUSIONS_NAME)
    assert exclusions == [
        {
            "event_id": "event-dev-headline",
            "reason": "TRAIN_DEV_HEADLINE_OVERLAP",
            "sample_id": "dev-headline",
        },
        {
            "event_id": "event-dev-shingle",
            "reason": "TRAIN_DEV_SHINGLE_JACCARD_AT_OR_ABOVE_THRESHOLD",
            "sample_id": "dev-shingle",
        },
    ]
    assert all(set(row) == {"sample_id", "event_id", "reason"} for row in exclusions)

    combined_outputs = b"".join(path.read_bytes() for path in output.iterdir())
    assert b"SECRET_" not in combined_outputs
    assert json.loads((output / AUDIT_NAME).read_text(encoding="utf-8")) == report
    assert report["inputs"]["train_unique"]["sha256"] == hashlib.sha256(
        train.read_bytes()
    ).hexdigest()
    assert report["inputs"]["dev"]["sha256"] == hashlib.sha256(
        dev.read_bytes()
    ).hexdigest()
    assert report["outputs"]["quality_exclusions"]["sha256"] == hashlib.sha256(
        (output / EXCLUSIONS_NAME).read_bytes()
    ).hexdigest()
    _assert_sidecar(output / AUDIT_NAME)
    _assert_sidecar(output / EXCLUSIONS_NAME)


def test_clean_cli_publishes_empty_exclusions_and_returns_zero(tmp_path: Path) -> None:
    train = tmp_path / "train.jsonl"
    dev = tmp_path / "dev.jsonl"
    output = tmp_path / "audit-output"
    _write_jsonl(
        train,
        [
            _dataset_row(
                "train-clean",
                "event-train-clean",
                _content("Issuer Alpha quarterly filing", _words("alpha", 40)),
                assistant_secret="NOT_JSON_AND_NOT_READ",
            )
        ],
    )
    _write_jsonl(
        dev,
        [
            _dataset_row(
                "dev-clean",
                "event-dev-clean",
                _content("Issuer Omega clinical update", _words("omega", 40)),
                assistant_secret="ALSO_NOT_JSON_AND_NOT_READ",
            )
        ],
    )

    exit_code = main(
        [
            "--train-unique",
            str(train),
            "--dev",
            str(dev),
            "--output-dir",
            str(output),
        ]
    )

    assert exit_code == 0
    report = json.loads((output / AUDIT_NAME).read_text(encoding="utf-8"))
    assert report["result"] == "PASS"
    assert report["counts"]["violating_pairs"] == 0
    assert (output / EXCLUSIONS_NAME).read_bytes() == b""
    assert report["outputs"]["quality_exclusions"]["sha256"] == hashlib.sha256(
        b""
    ).hexdigest()
    _assert_sidecar(output / AUDIT_NAME)
    _assert_sidecar(output / EXCLUSIONS_NAME)


def test_cli_publishes_before_nonzero_leakage_exit(tmp_path: Path) -> None:
    train = tmp_path / "train.jsonl"
    dev = tmp_path / "dev.jsonl"
    output = tmp_path / "audit-output"
    content = _content("Shared issuer headline long enough", _words("same", 40))
    _write_jsonl(
        train,
        [
            _dataset_row(
                "train", "event-train", content, assistant_secret="TRAIN_SECRET"
            )
        ],
    )
    _write_jsonl(
        dev,
        [
            _dataset_row(
                "dev", "event-dev", content, assistant_secret="DEV_SECRET"
            )
        ],
    )

    exit_code = main(
        [
            "--train-unique",
            str(train),
            "--dev",
            str(dev),
            "--output-dir",
            str(output),
        ]
    )

    assert exit_code == 1
    assert (output / AUDIT_NAME).is_file()
    assert _read_jsonl(output / EXCLUSIONS_NAME) == [
        {
            "event_id": "event-dev",
            "reason": "TRAIN_DEV_HEADLINE_AND_SHINGLE_SIMILARITY",
            "sample_id": "dev",
        }
    ]


def test_existing_output_directory_is_never_overwritten(tmp_path: Path) -> None:
    train = tmp_path / "train.jsonl"
    dev = tmp_path / "dev.jsonl"
    output = tmp_path / "audit-output"
    _write_jsonl(
        train,
        [
            _dataset_row(
                "train",
                "event-train",
                _content("Train source", _words("train", 20)),
                assistant_secret="TRAIN_SECRET",
            )
        ],
    )
    _write_jsonl(
        dev,
        [
            _dataset_row(
                "dev",
                "event-dev",
                _content("Dev source", _words("dev", 20)),
                assistant_secret="DEV_SECRET",
            )
        ],
    )
    audit_qwen_train_dev_source_similarity(
        train_unique=train,
        dev=dev,
        output_dir=output,
    )
    before = {path.name: path.read_bytes() for path in output.iterdir()}

    with pytest.raises(FileExistsError, match="already exists"):
        audit_qwen_train_dev_source_similarity(
            train_unique=train,
            dev=dev,
            output_dir=output,
        )

    assert {path.name: path.read_bytes() for path in output.iterdir()} == before


def test_atomic_failure_leaves_no_partial_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    train = tmp_path / "train.jsonl"
    dev = tmp_path / "dev.jsonl"
    output = tmp_path / "audit-output"
    _write_jsonl(
        train,
        [
            _dataset_row(
                "train",
                "event-train",
                _content("Train source", _words("train", 20)),
                assistant_secret="TRAIN_SECRET",
            )
        ],
    )
    _write_jsonl(
        dev,
        [
            _dataset_row(
                "dev",
                "event-dev",
                _content("Dev source", _words("dev", 20)),
                assistant_secret="DEV_SECRET",
            )
        ],
    )
    original = audit_module._write_bytes_sync
    calls = 0

    def fail_second_write(path: Path, payload: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated atomic publication failure")
        original(path, payload)

    monkeypatch.setattr(audit_module, "_write_bytes_sync", fail_second_write)

    with pytest.raises(OSError, match="simulated atomic publication failure"):
        audit_qwen_train_dev_source_similarity(
            train_unique=train,
            dev=dev,
            output_dir=output,
        )

    assert not output.exists()
    assert not list(tmp_path.glob(".audit-output.*.in-progress"))


def test_prohibited_supervision_key_fails_before_publication(tmp_path: Path) -> None:
    train = tmp_path / "train.jsonl"
    dev = tmp_path / "dev.jsonl"
    output = tmp_path / "audit-output"
    contaminated = _content("Train source", _words("train", 20))
    contaminated["expected"] = {"materiality": "MATERIAL_ADVERSE"}
    _write_jsonl(
        train,
        [
            _dataset_row(
                "train",
                "event-train",
                contaminated,
                assistant_secret="TRAIN_SECRET",
            )
        ],
    )
    _write_jsonl(
        dev,
        [
            _dataset_row(
                "dev",
                "event-dev",
                _content("Dev source", _words("dev", 20)),
                assistant_secret="DEV_SECRET",
            )
        ],
    )

    with pytest.raises(ValueError, match="prohibited supervision keys"):
        audit_qwen_train_dev_source_similarity(
            train_unique=train,
            dev=dev,
            output_dir=output,
        )

    assert not output.exists()


@pytest.mark.parametrize("threshold", [-0.01, 1.01])
def test_threshold_must_be_probability(tmp_path: Path, threshold: float) -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        audit_qwen_train_dev_source_similarity(
            train_unique=tmp_path / "unused-train.jsonl",
            dev=tmp_path / "unused-dev.jsonl",
            output_dir=tmp_path / "audit-output",
            threshold=threshold,
        )
