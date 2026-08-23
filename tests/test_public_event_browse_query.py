from __future__ import annotations

from pathlib import Path

from app.storage.ledger import LedgerRepository
from scripts.event_ledger import open_ledger


class _CountingRepository(LedgerRepository):
    progress_callbacks = 0

    def connect(self):
        connection = super().connect()

        def record_progress() -> int:
            self.progress_callbacks += 1
            return 0

        connection.set_progress_handler(record_progress, 1000)
        return connection


def _large_unreviewed_ledger(path: Path, *, event_count: int = 5000) -> None:
    connection = open_ledger(path)
    timestamp = "2026-08-01T12:00:00+00:00"
    try:
        connection.execute(
            "INSERT INTO sources VALUES ('src','Source','official_primary','P0',1,1,?,?)",
            (timestamp, timestamp),
        )
        event_rows = []
        version_rows = []
        for index in range(event_count):
            event_id = f"browse-{index:05d}"
            event_date = f"2026-08-{1 + index % 20:02d}"
            updated_at = f"2026-08-21T12:{index % 60:02d}:00+00:00"
            event_rows.append(
                (event_id, event_date, timestamp, updated_at, f"Company {index:05d}")
            )
            version_rows.append(
                (
                    event_id,
                    1,
                    updated_at,
                    "candidate",
                    "candidate",
                    "regulatory",
                    "filing",
                    "{}",
                )
            )
        connection.executemany(
            """INSERT INTO canonical_events VALUES (
               ?,1,'candidate','candidate','regulatory','filing',?,?,?,
               NULL,NULL,?,NULL,NULL,'src',1)""",
            event_rows,
        )
        connection.executemany(
            """INSERT INTO event_versions VALUES (
               ?,?,?,?,?,?,?,NULL,?,'seed')""",
            version_rows,
        )
        connection.commit()
    finally:
        connection.close()


def test_unfiltered_public_browse_scopes_quality_work_to_the_page(tmp_path: Path) -> None:
    ledger_path = tmp_path / "large.sqlite3"
    _large_unreviewed_ledger(ledger_path)

    repository = _CountingRepository(ledger_path)
    fast_page = repository.list_events(sort="latest", limit=48)

    # The full reader-ready filter remains the semantic reference for these
    # deliberately unreviewed fixtures.  The fast public projection must return
    # the same item fields and values without running that gate over all 5,000 ids.
    strict_page = LedgerRepository(ledger_path).list_events(
        sort="latest",
        reader_ready=False,
        limit=48,
    )

    assert fast_page["total"] == 5000
    assert len(fast_page["items"]) == 48
    assert fast_page["items"] == strict_page["items"]
    assert all(item["reader_ready"] == 0 for item in fast_page["items"])
    assert repository.progress_callbacks < 400


def test_explicit_reader_ready_filter_still_uses_full_gate(tmp_path: Path) -> None:
    ledger_path = tmp_path / "strict.sqlite3"
    _large_unreviewed_ledger(ledger_path, event_count=25)

    repository = LedgerRepository(ledger_path)
    assert repository.list_events(reader_ready=True, limit=50)["total"] == 0
    assert repository.list_events(reader_ready=False, limit=50)["total"] == 25

    empty_page = repository.list_events(limit=10, offset=1000)
    assert empty_page["total"] == 25
    assert empty_page["items"] == []
