"""Contract tests for the one-way authentic-human blind-v3 freeze script.

``scripts/freeze_human_blind_v3.py`` performs the only irreversible step on the
path out of ``QUALIFIED_SHADOW``: it hashes a selected human-adjudicated set and
commits those samples to ``FROZEN``. A freeze cannot be changed or undone;
only an exact idempotent retry may reconcile a post-commit artifact failure.

Every test below asserts one of four invariants:

1. Building a candidate never mutates adjudication state.
2. Committing requires complete, unexpired, action-scoped authorization.
3. A refused commit leaves every sample unfrozen.
4. The same corpus always produces the same freeze identity and bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import freeze_human_blind_v3 as freeze_script  # noqa: E402
from app.services import AdjudicationService  # noqa: E402
from app.storage import LedgerRepository, OperationsRepository  # noqa: E402
from event_ledger import open_ledger, utc_now  # noqa: E402


SOURCES = ("fixture_source_alpha", "fixture_source_beta", "fixture_source_gamma", "fixture_source_delta")
TIERS = ("P0_official", "P0_official", "P0_official", "P0_official")

# The fixture holds exactly the production minimums so that excluding a single
# event must break the freeze. A corpus with slack would let an exclusion bug
# pass unnoticed.
LABEL_PLAN = (("RISK_REVIEW", 30), ("NON_TARGET", 30), ("ABSTAIN", 20))

REVIEW_AXES = {
    "RISK_REVIEW": ("MATERIAL_ADVERSE", "ADVERSE", "PRIMARY_SUPPORTED"),
    "NON_TARGET": ("NOT_MATERIAL_ADVERSE", "POSITIVE", "PRIMARY_SUPPORTED"),
    "ABSTAIN": ("UNCLEAR", "UNCLEAR", "INSUFFICIENT"),
}


def principal(name: str) -> str:
    return hashlib.sha256(
        f"finance-radar-reviewer-principal-v1:{name.casefold()}".encode("utf-8")
    ).hexdigest()


def build_ledger(path: Path, plan: list[tuple[str, int]]) -> list[tuple[str, str]]:
    """Create one distinct issuer, chain, observation and passage per event.

    Distinct text matters: the selector rejects entity, chain, exact-text and
    near-duplicate collisions, so a lazy fixture would fail for the wrong reason.
    """

    connection = open_ledger(path)
    now = utc_now()
    for source_id, tier in zip(SOURCES, TIERS):
        connection.execute(
            "INSERT INTO sources VALUES (?,?,'official_primary',?,1,1,?,?)",
            (source_id, source_id.upper(), tier, now, now),
        )

    planned: list[tuple[str, str]] = []
    for label, count in plan:
        planned.extend((label, f"{label.lower()}-{index}") for index in range(count))

    for index, (label, slug) in enumerate(planned):
        source_id = SOURCES[index % len(SOURCES)]
        event_id = f"evt-{slug}"
        observation_id = f"obs-{slug}"
        headline = f"Issuer {slug} files a {label.lower()} disclosure"
        unique_terms = " ".join(
            f"u{hashlib.sha256(f'{slug}-{term}'.encode()).hexdigest()[:10]}"
            for term in range(16)
        )
        summary = (
            f"Filing {slug} from {source_id} describes a distinct {label.lower()} "
            f"matter for issuer number {index} with corpus-specific terms {unique_terms}."
        )
        passage = (
            f"The registrant {slug} disclosed an independently reviewable "
            f"{label.lower()} item numbered {index} in this exact passage."
        )
        connection.execute(
            """INSERT INTO raw_observations VALUES (
               ?,?,?, '2026-07-17',?,?,?,?,?, '{}','captured')""",
            (
                observation_id,
                source_id,
                f"filing-{slug}",
                now,
                headline,
                summary,
                f"https://{source_id}.example/{slug}",
                f"hash-{slug}",
            ),
        )
        connection.execute(
            """INSERT INTO canonical_events VALUES (
               ?,1,'verified','verified','corporate','filing','2026-07-17',
               ?,?,?,?,?,'A++','A++','test',1)""",
            (event_id, now, now, f"stable-{slug}", f"T{index:03d}", f"Company {slug}"),
        )
        connection.execute(
            """INSERT INTO event_versions VALUES (
               ?,1,?,'verified','verified','corporate','filing','A++',?,'fixture')""",
            (
                event_id,
                now,
                json.dumps(
                    {
                        "evidence_summary": summary,
                        "confirmed_facts": [passage],
                    }
                ),
            ),
        )
        connection.execute(
            "INSERT INTO event_observations VALUES (?,?, 'primary',?)",
            (event_id, observation_id, now),
        )
        entity_id = f"issuer-{slug}"
        connection.execute(
            "INSERT INTO entities VALUES (?,?,?,?,?,?)",
            (entity_id, "ISSUER", f"Company {slug}", "[]", now, now),
        )
        connection.execute(
            "INSERT INTO event_entities VALUES (?,?,?,?,?)",
            (event_id, entity_id, "SUBJECT", 1.0, now),
        )
        chain_id = f"chain-{slug}"
        connection.execute(
            "INSERT INTO event_chains VALUES (?,?,?,?,?,?,1)",
            (chain_id, "issuer_event", chain_id, event_id, now, now),
        )
        connection.execute(
            "INSERT INTO event_chain_members VALUES (?,?, 'primary_event',1,?,?)",
            (chain_id, event_id, "fixture primary event", now),
        )
        connection.execute(
            """INSERT INTO event_evidence VALUES (
               ?,?,?,?,'2026-07-17','8-K','1.03',?, 'material filing',10,
               'confirmed',0,?,?)""",
            (
                f"evidence-{slug}",
                event_id,
                observation_id,
                f"https://{source_id}.example/{slug}",
                passage,
                now,
                now,
            ),
        )
    connection.commit()
    connection.close()
    return [(label, f"evt-{slug}") for label, slug in planned]


def dual_review(service: AdjudicationService, sample_id: str, label: str) -> None:
    materiality, polarity, evidence_state = REVIEW_AXES[label]
    for reviewer in ("reviewer-a", "reviewer-b"):
        service.submit_review(
            sample_id,
            reviewer_id=principal(reviewer),
            role="REVIEWER",
            materiality=materiality,
            polarity=polarity,
            evidence_state=evidence_state,
            rationale=(
                "Two independent reviewers read the exact primary passage and "
                f"agreed this is a {label} outcome under the v3.1 contract."
            ),
        )


@pytest.fixture()
def corpus(tmp_path: Path):
    """A human-adjudicated corpus sitting exactly at the production minimums."""

    ledger_path = tmp_path / "ledger.sqlite3"
    operations_path = tmp_path / "operations.sqlite3"
    planned = build_ledger(ledger_path, list(LABEL_PLAN))
    operations = OperationsRepository(operations_path)
    service = AdjudicationService(LedgerRepository(ledger_path), operations)

    sample_ids: list[str] = []
    for label, event_id in planned:
        sample_id = service.create_sample_from_event(event_id)["sample_id"]
        dual_review(service, sample_id, label)
        sample_ids.append(sample_id)

    report = service.pre_freeze_report()
    assert report["status"] == "READY_FOR_OVERLAP_AUDIT", report["label_deficits"]
    return tmp_path, ledger_path, operations_path, operations, sample_ids


def run_script(monkeypatch, corpus, output_dir: Path, *extra: str) -> None:
    _root, ledger_path, operations_path, _operations, _ids = corpus
    argv = [
        "freeze_human_blind_v3.py",
        "--ledger",
        str(ledger_path),
        "--operations",
        str(operations_path),
        "--output-dir",
        str(output_dir),
        *extra,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    freeze_script.main()


def read_manifest(output_dir: Path) -> dict:
    manifests = sorted(output_dir.glob("*.manifest.json"))
    assert len(manifests) == 1, manifests
    return json.loads(manifests[0].read_text(encoding="utf-8"))


def statuses(operations: OperationsRepository, sample_ids: list[str]) -> set[str]:
    return {operations.adjudication_sample(item)["status"] for item in sample_ids}


def future_iso(hours: int = 6) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def authorization_file(
    monkeypatch,
    corpus,
    root: Path,
    **updates,
) -> Path:
    candidate_dir = root / "candidate"
    run_script(monkeypatch, corpus, candidate_dir)
    manifest = read_manifest(candidate_dir)
    template = json.loads(
        Path(manifest["authorization_template_path"]).read_text(encoding="utf-8")
    )
    template.update(
        {
            "approved": True,
            "authorization_id": "AUTH-2026-08-18-blind-v3",
            "actor": "owner-external-approval",
            "purpose": "Freeze the authentic-human blind-v3 evaluation set for governance review.",
            "expires_at": future_iso(),
        }
    )
    template.update(updates)
    path = root / "authorization.json"
    path.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 1. Building a candidate must not mutate adjudication state
# --------------------------------------------------------------------------


def test_dry_run_writes_artifacts_and_freezes_nothing(monkeypatch, corpus) -> None:
    root, _ledger, _operations_path, operations, sample_ids = corpus
    output_dir = root / "out"

    run_script(monkeypatch, corpus, output_dir)

    manifest = read_manifest(output_dir)
    assert manifest["applied"] is False
    assert manifest["row_count"] == 80
    assert manifest["label_counts"] == {"ABSTAIN": 20, "NON_TARGET": 30, "RISK_REVIEW": 30}
    assert len(manifest["source_groups"]) == len(SOURCES)
    assert manifest["adjudication_state_changed"] is False
    assert manifest["production_model_changed"] is False
    assert manifest["canonical_event_state_changed"] is False
    assert manifest["no_trading"] is True
    assert manifest["model_predictions_included"] is False
    assert manifest["post_event_market_data_included"] is False

    # The candidate rows never travel inside the manifest; the dataset file is
    # the single hashed carrier of the frozen content.
    assert "rows" not in manifest

    dataset = Path(manifest["dataset_path"])
    assert dataset.is_file()
    assert hashlib.sha256(dataset.read_bytes()).hexdigest() == manifest["dataset_sha256"]
    assert manifest["freeze_id"].startswith("human-blind-v3-")
    assert manifest["source_holdout_status"] == "ELIGIBLE_WITH_FULLY_HELD_OUT_FAMILY"
    assert manifest["held_out_source_families"] == sorted(SOURCES)

    assert statuses(operations, sample_ids) == {"READY"}


def test_dry_run_reports_zero_overlap_across_every_grouping(monkeypatch, corpus) -> None:
    root, *_ = corpus
    run_script(monkeypatch, corpus, root / "out")
    manifest = read_manifest(root / "out")
    assert manifest["entity_overlap_count"] == 0
    assert manifest["event_chain_overlap_count"] == 0
    assert manifest["exact_text_overlap_count"] == 0
    assert manifest["near_duplicate_overlap_count"] == 0


def test_artifacts_are_owner_only_and_leave_no_temporary_file(monkeypatch, corpus) -> None:
    root, *_ = corpus
    output_dir = root / "nested" / "out"

    run_script(monkeypatch, corpus, output_dir)

    written = sorted(output_dir.iterdir())
    assert [path.suffix for path in written].count(".tmp") == 0
    for path in written:
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    if os.name == "nt":
        assert read_manifest(output_dir)["artifact_permission_contract"] == (
            "WINDOWS_CALLER_ACL_NOT_PROVEN_BY_CHMOD"
        )


def test_same_corpus_produces_the_same_freeze_identity(monkeypatch, corpus) -> None:
    root, *_ = corpus
    run_script(monkeypatch, corpus, root / "first")
    run_script(monkeypatch, corpus, root / "second")

    first = read_manifest(root / "first")
    second = read_manifest(root / "second")
    assert first["freeze_id"] == second["freeze_id"]
    assert first["dataset_sha256"] == second["dataset_sha256"]
    assert (
        Path(first["dataset_path"]).read_bytes() == Path(second["dataset_path"]).read_bytes()
    )


# --------------------------------------------------------------------------
# 2 & 3. Committing requires authorization; a refusal freezes nothing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("authorization_id", ""),
        ("actor", ""),
        ("purpose", "too short"),
    ],
)
def test_apply_refuses_incomplete_authorization(
    monkeypatch, corpus, field: str, replacement: str
) -> None:
    root, _ledger, _operations_path, operations, sample_ids = corpus
    authorization = authorization_file(
        monkeypatch, corpus, root, **{field: replacement}
    )

    with pytest.raises(ValueError, match="authorization contract"):
        run_script(
            monkeypatch,
            corpus,
            root / "out",
            "--apply",
            "--authorization-file",
            str(authorization),
        )

    assert statuses(operations, sample_ids) == {"READY"}
    # The refusal happens before any manifest is written, so no artifact can
    # later be mistaken for an authorized freeze receipt.
    assert not list((root / "out").glob("*.manifest.json"))


def test_apply_refuses_a_missing_expiry(monkeypatch, corpus) -> None:
    root, _ledger, _operations_path, operations, sample_ids = corpus
    authorization = authorization_file(monkeypatch, corpus, root, expires_at="")

    with pytest.raises((ValueError, TypeError), match="isoformat|expiry"):
        run_script(
            monkeypatch,
            corpus,
            root / "out",
            "--apply",
            "--authorization-file",
            str(authorization),
        )

    assert statuses(operations, sample_ids) == {"READY"}


@pytest.mark.parametrize("delta_hours", [-1, -24])
def test_apply_refuses_an_expired_authorization(
    monkeypatch, corpus, delta_hours: int
) -> None:
    root, _ledger, _operations_path, operations, sample_ids = corpus
    expired = (datetime.now(timezone.utc) + timedelta(hours=delta_hours)).isoformat()
    authorization = authorization_file(monkeypatch, corpus, root, expires_at=expired)

    with pytest.raises(ValueError, match="expiry must be in the future"):
        run_script(
            monkeypatch,
            corpus,
            root / "out",
            "--apply",
            "--authorization-file",
            str(authorization),
        )

    assert statuses(operations, sample_ids) == {"READY"}
    assert not list((root / "out").glob("*.manifest.json"))


def test_missing_overlap_reference_refuses_before_any_write(
    monkeypatch, corpus, tmp_path: Path
) -> None:
    root, _ledger, _operations_path, operations, sample_ids = corpus
    absent = tmp_path / "nonexistent-exclusions.jsonl"
    output_dir = root / "out"

    with pytest.raises(ValueError, match="required overlap reference is missing"):
        run_script(
            monkeypatch, corpus, output_dir, "--exclude-jsonl", str(absent)
        )

    assert not output_dir.exists()
    assert statuses(operations, sample_ids) == {"READY"}


# --------------------------------------------------------------------------
# The authorized path
# --------------------------------------------------------------------------


def test_authorized_apply_freezes_exactly_the_selected_samples(
    monkeypatch, corpus
) -> None:
    root, _ledger, _operations_path, operations, sample_ids = corpus
    authorization = authorization_file(monkeypatch, corpus, root)

    run_script(
        monkeypatch,
        corpus,
        root / "out",
        "--apply",
        "--authorization-file",
        str(authorization),
    )

    manifest = read_manifest(root / "out")
    assert manifest["applied"] is True
    assert manifest["frozen_samples"] == manifest["row_count"] == len(sample_ids)
    assert manifest["adjudication_state_changed"] is True
    # Freezing an evaluation set must never touch the model or the ledger.
    assert manifest["production_model_changed"] is False
    assert manifest["canonical_event_state_changed"] is False

    authorization = manifest["authorization"]
    assert authorization["authorization_id"] == "AUTH-2026-08-18-blind-v3"
    assert authorization["actor"] == "owner-external-approval"
    assert len(authorization["purpose"]) >= 20
    assert datetime.fromisoformat(authorization["expires_at"]) > datetime.now(timezone.utc)

    assert statuses(operations, sample_ids) == {"FROZEN"}
    for sample_id in sample_ids:
        assert operations.adjudication_sample(sample_id)["freeze_id"] == manifest["freeze_id"]


def test_freeze_is_one_way_and_cannot_be_repeated(monkeypatch, corpus) -> None:
    root, _ledger, _operations_path, operations, sample_ids = corpus
    authorization = authorization_file(monkeypatch, corpus, root)

    run_script(monkeypatch, corpus, root / "out", "--apply", "--authorization-file", str(authorization))
    first = read_manifest(root / "out")

    # A second authorized run must not silently re-freeze or re-identify the set.
    with pytest.raises(ValueError):
        run_script(
            monkeypatch,
            corpus,
            root / "again",
            "--apply",
            "--authorization-file",
            str(authorization),
        )

    assert statuses(operations, sample_ids) == {"FROZEN"}
    for sample_id in sample_ids:
        assert operations.adjudication_sample(sample_id)["freeze_id"] == first["freeze_id"]


def test_exact_retry_reconciles_after_database_commit(monkeypatch, corpus) -> None:
    root, _ledger, _operations_path, operations, sample_ids = corpus
    authorization = authorization_file(monkeypatch, corpus, root)
    output_dir = root / "out"

    run_script(monkeypatch, corpus, output_dir, "--apply", "--authorization-file", str(authorization))
    run_script(monkeypatch, corpus, output_dir, "--apply", "--authorization-file", str(authorization))

    manifest = read_manifest(output_dir)
    assert manifest["commit_state"] == "COMMITTED"
    assert manifest["idempotent_reconciliation"] is True
    assert manifest["adjudication_state_changed"] is False
    receipt = operations.adjudication_freeze(manifest["freeze_id"])
    assert receipt is not None
    assert receipt["dataset_sha256"] == manifest["dataset_sha256"]
    assert receipt["sample_count"] == len(sample_ids)


def test_manifest_write_failure_leaves_durable_receipt_for_reconciliation(
    monkeypatch, corpus
) -> None:
    root, _ledger, _operations_path, operations, sample_ids = corpus
    authorization = authorization_file(monkeypatch, corpus, root)
    output_dir = root / "out"
    original = freeze_script._write_atomic
    calls = 0

    def fail_final_manifest(path: Path, data: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("simulated final manifest write failure")
        original(path, data)

    monkeypatch.setattr(freeze_script, "_write_atomic", fail_final_manifest)
    with pytest.raises(OSError, match="simulated final manifest"):
        run_script(monkeypatch, corpus, output_dir, "--apply", "--authorization-file", str(authorization))

    assert statuses(operations, sample_ids) == {"FROZEN"}
    prepared = read_manifest(output_dir)
    assert prepared["commit_state"] == "PREPARED"
    assert operations.adjudication_freeze(prepared["freeze_id"]) is not None

    monkeypatch.setattr(freeze_script, "_write_atomic", original)
    run_script(monkeypatch, corpus, output_dir, "--apply", "--authorization-file", str(authorization))
    assert read_manifest(output_dir)["idempotent_reconciliation"] is True


# --------------------------------------------------------------------------
# 4. Exclusion references really reach the selector
# --------------------------------------------------------------------------


def test_supplied_exclusions_remove_rows_from_the_candidate(
    monkeypatch, corpus, tmp_path: Path
) -> None:
    """The corpus sits at the minimum, so excluding one event must break it.

    This is the assertion that proves ``--exclude-jsonl`` is wired through to
    the selector rather than merely being read and discarded.
    """

    root, _ledger, _operations_path, operations, sample_ids = corpus
    exclusions = tmp_path / "prior-exposure.jsonl"
    exclusions.write_text(
        json.dumps({"event_id": "evt-risk_review-0"}) + "\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="insufficient zero-overlap RISK_REVIEW"):
        run_script(
            monkeypatch, corpus, root / "out", "--exclude-jsonl", str(exclusions)
        )

    assert statuses(operations, sample_ids) == {"READY"}


def test_fuzzy_near_duplicate_exclusion_is_not_just_an_exact_hash(
    monkeypatch, corpus, tmp_path: Path
) -> None:
    root, _ledger, _operations_path, operations, sample_ids = corpus
    prior = operations.adjudication_sample(sample_ids[0])
    content = dict(prior["content"])
    content["summary"] = str(content["summary"]) + " minor editorial correction"
    exclusions = tmp_path / "near-duplicate.jsonl"
    exclusions.write_text(json.dumps({"content": content}) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="insufficient zero-overlap"):
        run_script(monkeypatch, corpus, root / "out", "--exclude-jsonl", str(exclusions))


def test_near_duplicate_similarity_catches_small_edits_but_not_unrelated_text() -> None:
    def annotation(words: list[str]) -> dict:
        return {"content": {"summary": " ".join(words)}}

    original = [f"term{index}" for index in range(40)]
    edited = [*original]
    edited[20] = "correctedterm"
    unrelated = [f"other{index}" for index in range(40)]
    first = AdjudicationService._near_duplicate_signature(annotation(original))
    second = AdjudicationService._near_duplicate_signature(annotation(edited))
    third = AdjudicationService._near_duplicate_signature(annotation(unrelated))

    assert AdjudicationService._is_near_duplicate(first, second) is True
    assert AdjudicationService._is_near_duplicate(first, third) is False

    chinese = annotation(list("公司公告显示该发行人已完成债务重组并取消原有普通股权益"))
    chinese_edit = annotation(list("公司公告显示该发行人已完成债务重组并注销原有普通股权益"))
    assert AdjudicationService._is_near_duplicate(
        AdjudicationService._near_duplicate_signature(chinese),
        AdjudicationService._near_duplicate_signature(chinese_edit),
    ) is True


def test_exclusion_loader_rejects_a_non_object_row(tmp_path: Path) -> None:
    service = AdjudicationService(
        LedgerRepository(tmp_path / "ledger.sqlite3"),
        OperationsRepository(tmp_path / "operations.sqlite3"),
    )
    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text('["not-an-object"]\n', encoding="utf-8")

    with pytest.raises(ValueError, match="non-object exclusion row"):
        freeze_script._load_exclusions([malformed], service)


def test_exclusion_loader_collects_every_overlap_dimension(tmp_path: Path) -> None:
    service = AdjudicationService(
        LedgerRepository(tmp_path / "ledger.sqlite3"),
        OperationsRepository(tmp_path / "operations.sqlite3"),
    )
    text_hash = "a" * 64
    entity_hash = "b" * 64
    chain_hash = "c" * 64
    rows = tmp_path / "prior.jsonl"
    rows.write_text(
        "\n".join(
            [
                "",  # blank lines are skipped rather than failing the load
                json.dumps(
                    {
                        "text_sha256": text_hash.upper(),
                        "event_id": "evt-prior",
                        "entity_group": "issuer:prior",
                        "event_chain_group": "chain:prior",
                        "entity_group_sha256": entity_hash.upper(),
                        "event_chain_group_sha256": chain_hash.upper(),
                        "source_group": "sec_edgar",
                        "content": {"summary": "A previously exposed summary."},
                    }
                ),
                json.dumps({"text_sha256": "too-short", "text": "Plain text row."}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    (
        exact,
        near,
        near_signatures,
        events,
        entities,
        chains,
        entity_hashes,
        chain_hashes,
        source_families,
    ) = (
        freeze_script._load_exclusions([rows], service)
    )

    # Hashes are normalized to lower case so a differently cased manifest still
    # counts as prior exposure.
    assert exact == {text_hash}
    assert entity_hashes == {entity_hash}
    assert chain_hashes == {chain_hash}
    assert events == {"evt-prior"}
    assert entities == {"issuer:prior"}
    assert chains == {"chain:prior"}
    assert source_families == {"sec"}
    # Both the structured row and the plain-text row contribute a near key.
    assert len(near) == 2
    assert len(near_signatures) == 2


# --------------------------------------------------------------------------
# Authorization expiry parsing
# --------------------------------------------------------------------------


def test_expiry_parser_normalizes_future_values_to_utc() -> None:
    parsed = freeze_script._iso_future("2999-01-01T00:00:00Z")
    assert datetime.fromisoformat(parsed).tzinfo is not None
    assert datetime.fromisoformat(parsed) == datetime(2999, 1, 1, tzinfo=timezone.utc)


def test_expiry_parser_treats_a_naive_value_as_utc() -> None:
    parsed = freeze_script._iso_future("2999-01-01T00:00:00")
    assert datetime.fromisoformat(parsed) == datetime(2999, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", ["1999-01-01T00:00:00Z", "2000-06-01T12:00:00+00:00"])
def test_expiry_parser_rejects_past_values(value: str) -> None:
    with pytest.raises(ValueError, match="expiry must be in the future"):
        freeze_script._iso_future(value)


# --------------------------------------------------------------------------
# Atomic artifact writing
# --------------------------------------------------------------------------


def test_atomic_write_creates_parents_replaces_and_restricts_mode(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "freeze.jsonl"

    freeze_script._write_atomic(target, b"first\n")
    assert target.read_bytes() == b"first\n"

    freeze_script._write_atomic(target, b"second\n")
    assert target.read_bytes() == b"second\n"
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(target.parent.glob("*.tmp"))
