from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from app.services.source_observation_recovery import build_source_observation_recovery_plan
from app.storage import LedgerRepository
from build_source_observation_recovery import build
from event_ledger import open_ledger, upsert_source


def _seed(root: Path) -> Path:
    ledger = root / "ledger.sqlite3"
    connection = open_ledger(ledger)
    now = "2026-08-21T02:33:00+00:00"
    for source_id, tier in (
        ("sec_current_filings", "P0_official"),
        ("official", "P1_issuer_official"),
        ("opennews_free", "P2_experimental"),
    ):
        upsert_source(
            connection,
            source_id=source_id,
            name=source_id,
            source_type="official_primary" if tier.startswith(("P0", "P1")) else "aggregated",
            authority_tier=tier,
        )

    def add_event(event_id: str) -> None:
        connection.execute(
            """INSERT INTO canonical_events(
               event_id,current_version,status,label_status,event_family,event_type,
               event_date,first_seen_at,last_updated_at,company_name,discovery_source,no_trading
               ) VALUES (?,1,'candidate','candidate','test','test','2026-08-21',?,?,?, 'test',1)""",
            (event_id, now, now, event_id),
        )

    def add_capture(
        event_id: str | None,
        observation_id: str,
        source_id: str,
        url: str | None,
    ) -> None:
        title = f"Captured source for {observation_id}"
        connection.execute(
            """INSERT INTO raw_observations(
               observation_id,source_id,external_id,source_published_at,local_received_at,
               title,summary,canonical_url,content_sha256,raw_json,observation_status
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                observation_id,
                source_id,
                f"external-{observation_id}",
                "2026-08-21T01:00:00+00:00",
                now,
                title,
                f"Summary for {observation_id}",
                url,
                hashlib.sha256(title.encode()).hexdigest(),
                json.dumps({"item": {"title": title, "score": 99}}),
                "captured",
            ),
        )
        if event_id:
            connection.execute(
                "INSERT INTO event_observations VALUES (?,?,?,?)",
                (event_id, observation_id, "filtered_aggregated_noise", now),
            )

    for event_id in ("e-sec", "e-official", "e-p2", "e-raw", "e-deleted", "e-empty"):
        add_event(event_id)
    add_capture("e-sec", "obs-sec", "sec_current_filings", "https://www.sec.gov/a")
    add_capture("e-official", "obs-official", "official", "https://issuer.test/a")
    add_capture("e-p2", "obs-p2", "opennews_free", "https://news.test/a")
    add_capture("e-raw", "obs-raw", "opennews_free", None)
    add_capture("e-deleted", "obs-deleted", "opennews_free", "https://news.test/deleted")
    add_capture(None, "obs-orphan", "opennews_free", "https://news.test/orphan")
    connection.execute(
        """INSERT INTO source_revisions(
           revision_id,observation_id,source_id,external_id,revision_no,revision_kind,
           revision_at,content_sha256,title,summary,raw_json
           ) VALUES (?,?,?,?,1,'delete',?,?,?,?,?)""",
        (
            "rev-delete",
            "obs-deleted",
            "opennews_free",
            "external-obs-deleted",
            now,
            "d" * 64,
            "Deleted",
            "",
            "{}",
        ),
    )
    connection.execute(
        """INSERT INTO sec_filing_enrichments(
           enrichment_id,event_id,observation_id,accession_number,form,filing_index_url,
           primary_document_url,documents_json,evidence_excerpt,text_sha256,
           matched_event_family,matched_event_type,matched_keywords_json,confidence,status,
           attempts,last_error,fetched_at,updated_at,read_only,no_trading
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "enrich-sec",
            "e-sec",
            "obs-sec",
            "0000000000-26-000001",
            "10-Q",
            "https://www.sec.gov/a",
            "https://www.sec.gov/a/primary.htm",
            "{}",
            "",
            None,
            None,
            None,
            "[]",
            0.0,
            "ERROR",
            3,
            "SEC document exceeds safe capture limit (5000000 bytes)",
            None,
            now,
            1,
            1,
        ),
    )
    connection.commit()
    connection.close()
    return ledger


def test_source_observation_recovery_is_complete_read_only_and_deterministic() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _seed(root)
        before = ledger.read_bytes()

        first = build_source_observation_recovery_plan(ledger)
        second = build_source_observation_recovery_plan(ledger)

        assert first["zero_evidence_event_count"] == 6
        assert first["orphan_capture_count"] == 1
        assert first["source_record_count"] == 7
        assert first["partition_complete"] is True
        assert first["network_requests_performed"] == 0
        assert first["canonical_mutations_performed"] == 0
        assert first["bucket_counts"] == {
            "SEC_OVERSIZE_REFETCH_READY": 1,
            "OFFICIAL_REFETCH_READY": 1,
            "P2_CAPTURE_ONLY": 1,
            "NO_URL_RAW_ONLY": 1,
            "SOURCE_DELETED": 1,
            "NO_CAPTURE": 1,
            "ORPHAN_CAPTURE_REBUILD_DISCOVERY": 1,
        }
        assert first["logical_snapshot_sha256"] == second["logical_snapshot_sha256"]
        assert ledger.read_bytes() == before
        sec = next(record for record in first["records"] if record.get("event", {}).get("event_id") == "e-sec")
        assert sec["bucket"] == "SEC_OVERSIZE_REFETCH_READY"
        assert sec["captures"][0]["raw_payload_sha256"] != sec["captures"][0][
            "semantic_content_sha256"
        ]
        assert "raw_json" not in sec["captures"][0]
        ledger_capture = LedgerRepository(ledger).captured_sources("e-sec")[0]
        assert sec["captures"][0]["capture_receipt_sha256"] == ledger_capture[
            "capture_receipt_sha256"
        ]


def test_cli_builds_manifest_records_and_hashes() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        output = root / "output"
        manifest = build(_seed(root), output)

        assert manifest["partition_complete"] is True
        assert (output / "manifest.json").is_file()
        assert (output / "recovery_records.jsonl").is_file()
        assert (output / "README.md").is_file()
        sums = (output / "SHA256SUMS.txt").read_text(encoding="utf-8")
        assert "manifest.json" in sums
        assert "recovery_records.jsonl" in sums
        assert "SHA256SUMS.txt" not in sums
