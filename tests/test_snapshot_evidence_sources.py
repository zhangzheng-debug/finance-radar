from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from app.storage import EvidenceObjectStore, OperationsRepository
from event_ledger import open_ledger, utc_now
import snapshot_evidence_sources as snapshots


def _fixture(
    root: Path,
    *,
    url: str = "https://www.sec.gov/example.htm",
    source_id: str = "sec_current_filings",
):
    ledger = open_ledger(root / "ledger.sqlite3")
    now = utc_now()
    ledger.execute(
        "INSERT INTO sources VALUES (?,?, 'official_primary','P0',1,1,?,?)",
        (source_id, source_id, now, now),
    )
    ledger.execute(
        """INSERT INTO raw_observations VALUES (
           'obs-1',?,'filing-1',?,?,'Example filing','Example summary',
           ?,?,'{}','captured')""",
        (source_id, now, now, url, hashlib.sha256(url.encode()).hexdigest()),
    )
    ledger.execute(
        """INSERT INTO canonical_events VALUES (
           'evt-1',1,'verified','verified','regulatory','filing','2026-07-19',
           ?,?,'stable-1','TEST','Example','A','A','fixture',1)""",
        (now, now),
    )
    ledger.execute(
        """INSERT INTO event_versions VALUES (
           'evt-1',1,?,'verified','verified','regulatory','filing','A','{}','fixture')""",
        (now,),
    )
    ledger.execute(
        "INSERT INTO event_observations VALUES ('evt-1','obs-1','primary',?)",
        (now,),
    )
    ledger.execute(
        """INSERT INTO event_evidence VALUES (
           'evid-1','evt-1','obs-1',?,'2026-07-19','8-K','8.01',
           'Exact primary passage','primary',10,'confirmed',0,?,?)""",
        (url, now, now),
    )
    ledger.commit()
    return ledger, OperationsRepository(root / "operations.sqlite3"), EvidenceObjectStore(root / "objects")


def test_archives_html_and_is_idempotent() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ledger, operations, store = _fixture(root)
        calls = []

        def fetcher(url: str, user_agent: str, timeout: float, max_bytes: int):
            calls.append((url, user_agent, timeout, max_bytes))
            return snapshots.FetchResult(
                b"<!doctype html><html><body>official filing</body></html>",
                url,
                "text/html; charset=utf-8",
            )

        first = snapshots.archive_pending(
            ledger,
            operations,
            store,
            user_agent="FinanceRadar test@example.com",
            cache_dir=root / "cache",
            limit=4,
            timeout=1,
            fetcher=fetcher,
        )
        assert first["status"] == "PASS"
        assert first["archived"] == 1
        assert first["network_fetches"] == 1
        assert len(calls) == 1
        archive = operations.evidence_archive_summary()
        assert archive["source_snapshots"] == 1
        assert archive["by_mime"]["text/html"]["objects"] == 1
        item = archive["recent_objects"][0]
        assert item["object_kind"] == "SOURCE_SNAPSHOT"
        assert store.verify(item["relative_path"], item["object_sha256"])

        second = snapshots.archive_pending(
            ledger,
            operations,
            store,
            user_agent="FinanceRadar test@example.com",
            cache_dir=root / "cache",
            limit=4,
            timeout=1,
            fetcher=lambda *args: (_ for _ in ()).throw(AssertionError("must not refetch")),
        )
        assert second["archived"] == 0
        assert second["already_archived"] == 1
        assert second["attempted"] == 0
        ledger.close()


def test_disallowed_host_and_redirect_are_rejected_before_persistence() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ledger, operations, store = _fixture(root, url="https://malicious.example/file.htm")
        result = snapshots.archive_pending(
            ledger,
            operations,
            store,
            user_agent="FinanceRadar test@example.com",
            cache_dir=root / "cache",
            limit=1,
            fetcher=lambda *args: (_ for _ in ()).throw(AssertionError("must not fetch")),
        )
        assert result["status"] == "PASS"
        assert result["archived"] == 0
        assert result["policy_skipped"] == 1
        assert "outside registered source domain" in result["policy_skip_examples"][0]
        ledger.close()

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ledger, operations, store = _fixture(root)
        result = snapshots.archive_pending(
            ledger,
            operations,
            store,
            user_agent="FinanceRadar test@example.com",
            cache_dir=root / "cache",
            limit=1,
            fetcher=lambda url, *_: snapshots.FetchResult(
                b"<html><body>redirect</body></html>",
                "https://malicious.example/redirect",
                "text/html",
            ),
        )
        assert result["archived"] == 0
        assert "redirected outside" in result["errors"][0]
        ledger.close()


def test_pdf_detection_and_size_contract() -> None:
    assert snapshots.detect_mime(b"%PDF-1.7\nfixture", "application/octet-stream") == "application/pdf"
    assert snapshots.host_allowed("sec_current_filings", "https://www.sec.gov:443/file.pdf")
    assert not snapshots.host_allowed("sec_current_filings", "http://www.sec.gov/file.pdf")
    assert not snapshots.host_allowed("sec_current_filings", "https://user@www.sec.gov/file.pdf")
    assert not snapshots.host_allowed("sec_current_filings", "https://www.sec.gov:bad/file.pdf")
    assert snapshots.host_allowed("sec_edgar", "https://data.sec.gov/submissions/example.json")
    assert snapshots.host_allowed("bls_key_indicators", "https://www.bls.gov/news.release/x.htm")
    assert snapshots.canonical_source_url(
        "fda_medwatch", "http://www.fda.gov/medical-devices/recall?x=1#section"
    ) == "https://www.fda.gov/medical-devices/recall?x=1"
    assert snapshots.canonical_source_url(
        "fda_medwatch", "http://user@www.fda.gov/medical-devices/recall"
    ) is None
    assert snapshots.canonical_source_url(
        "fda_medwatch", "http://www.fda.gov:8080/medical-devices/recall"
    ) is None
    assert snapshots.canonical_source_url(
        "fda_medwatch", "http://malicious.example/medical-devices/recall"
    ) is None
    assert snapshots.detect_mime(b'{"official":true}', "application/json") == "application/json"

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ledger, operations, store = _fixture(root)
        result = snapshots.archive_pending(
            ledger,
            operations,
            store,
            user_agent="FinanceRadar test@example.com",
            cache_dir=root / "cache",
            limit=1,
            max_bytes=1024,
            fetcher=lambda url, *_: snapshots.FetchResult(
                b"<html>" + b"x" * 2000 + b"</html>", url, "text/html"
            ),
        )
        assert result["archived"] == 0
        assert "size limit" in result["errors"][0]
        ledger.close()


def test_registered_official_http_link_is_upgraded_before_fetch() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ledger, operations, store = _fixture(
            root,
            source_id="fda_medwatch",
            url="http://www.fda.gov/medical-devices/recall",
        )
        fetched_urls: list[str] = []

        def fetcher(url: str, *_):
            fetched_urls.append(url)
            return snapshots.FetchResult(
                b"<!doctype html><html><body>FDA recall</body></html>",
                url,
                "text/html",
            )

        result = snapshots.archive_pending(
            ledger,
            operations,
            store,
            user_agent="FinanceRadar test@example.com",
            cache_dir=root / "cache",
            limit=1,
            fetcher=fetcher,
        )
        assert result["status"] == "PASS"
        assert result["archived"] == 1
        assert result["http_upgraded_to_https"] == 1
        assert fetched_urls == ["https://www.fda.gov/medical-devices/recall"]
        ledger.close()


def test_paginates_past_archived_head_rows(monkeypatch) -> None:
    """Old or failing head rows must not permanently block later evidence."""

    archived_rows = [
        {
            "event_id": f"evt-{index}",
            "evidence_id": f"evid-{index}",
            "evidence_url": f"https://www.sec.gov/archive/{index}.htm",
            "source_id": "sec_edgar",
        }
        for index in range(100)
    ]
    pending = {
        "event_id": "evt-pending",
        "evidence_id": "evid-pending",
        "evidence_url": "https://www.sec.gov/archive/pending.htm",
        "source_id": "sec_edgar",
    }
    offsets: list[int] = []

    def paged_rows(_connection, *, scan_limit: int, scan_offset: int = 0):
        assert scan_limit == 100
        offsets.append(scan_offset)
        if scan_offset == 0:
            return archived_rows
        if scan_offset == 100:
            return [pending]
        return []

    class Operations:
        recorded = []

        @staticmethod
        def has_source_snapshot(_event_id: str, evidence_id: str) -> bool:
            return evidence_id != "evid-pending"

        @classmethod
        def record_evidence_object(cls, *args, **kwargs) -> None:
            cls.recorded.append((args, kwargs))

    monkeypatch.setattr(snapshots, "candidate_rows", paged_rows)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        result = snapshots.archive_pending(
            object(),
            Operations(),
            EvidenceObjectStore(root / "objects"),
            user_agent="FinanceRadar test@example.com",
            cache_dir=root / "cache",
            limit=1,
            fetcher=lambda url, *_: snapshots.FetchResult(
                b"<!doctype html><html><body>later evidence</body></html>",
                url,
                "text/html",
            ),
        )

    assert offsets == [0, 100]
    assert result["selected"] == 101
    assert result["already_archived"] == 100
    assert result["attempted"] == 1
    assert result["archived"] == 1
    assert len(Operations.recorded) == 1
