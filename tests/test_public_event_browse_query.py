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


def test_deep_public_page_uses_stable_browse_indexes(tmp_path: Path) -> None:
    ledger_path = tmp_path / "deep.sqlite3"
    _large_unreviewed_ledger(ledger_path, event_count=14_500)

    repository = _CountingRepository(ledger_path)
    deep_page = repository.list_events(
        sort="latest",
        limit=48,
        offset=14_400,
        source_excerpt_chars=512,
    )
    strict_page = LedgerRepository(ledger_path).list_events(
        sort="latest",
        reader_ready=False,
        limit=48,
        offset=14_400,
    )

    assert deep_page["total"] == 14_500
    assert len(deep_page["items"]) == 48
    assert deep_page["items"] == strict_page["items"]
    # This upper bound catches a regression to sorting full canonical rows in a
    # temporary B-tree before discarding the first 300 public pages.
    assert repository.progress_callbacks < 150

    with repository.connect() as connection:
        indexes = {
            str(row["name"])
            for row in connection.execute("PRAGMA index_list(canonical_events)")
        }
    assert {
        "idx_events_public_latest",
        "idx_events_public_event_date",
        "idx_events_public_subject",
    } <= indexes


def test_shadow_batch_scopes_source_revision_work_to_bounded_window(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "shadow.sqlite3"
    event_count = 5_000
    _large_unreviewed_ledger(ledger_path, event_count=event_count)
    timestamp = "2026-08-21T12:00:00+00:00"
    with open_ledger(ledger_path) as connection:
        observations = []
        revisions = []
        relations = []
        for index in range(event_count):
            event_id = f"browse-{index:05d}"
            observation_id = f"observation-{index:05d}"
            external_id = f"external-{index:05d}"
            title = f"Source {index:05d}"
            observations.append(
                (
                    observation_id,
                    "src",
                    external_id,
                    timestamp,
                    timestamp,
                    title,
                    f"Summary {index:05d}",
                    "https://example.test/source",
                    f"{index:064x}"[-64:],
                    "{}",
                    "captured",
                )
            )
            revisions.append(
                (
                    f"revision-{index:05d}",
                    observation_id,
                    "src",
                    external_id,
                    1,
                    "new",
                    timestamp,
                    f"{index:064x}"[-64:],
                    title,
                    f"Summary {index:05d}",
                    "{}",
                )
            )
            relations.append((event_id, observation_id, "primary", timestamp))
        connection.executemany(
            "INSERT INTO raw_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            observations,
        )
        connection.executemany(
            "INSERT INTO source_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            revisions,
        )
        connection.executemany(
            "INSERT INTO event_observations VALUES (?,?,?,?)",
            relations,
        )
        connection.commit()

    repository = _CountingRepository(ledger_path)
    batch = repository.shadow_batch(limit=48)

    assert len(batch) == 48
    assert all(item["detail"]["preferred_source"]["title"] for item in batch)
    # The bounded batch may use the latest-revision index for its selected
    # observations, but it must not rank all 5,000 historical revisions.
    assert repository.progress_callbacks < 1_000


def test_public_excerpt_bound_does_not_change_internal_source_semantics(tmp_path: Path) -> None:
    ledger_path = tmp_path / "source-excerpt.sqlite3"
    _large_unreviewed_ledger(ledger_path, event_count=1)
    timestamp = "2026-08-21T12:00:00+00:00"
    long_summary = "source supplied text " * 800
    with open_ledger(ledger_path) as connection:
        observations = (
            ("active", "active", "2026-08-21T10:00:00+00:00", "Active", long_summary),
            ("noise", "noise", "2026-08-21T12:00:00+00:00", "Filtered", "noise"),
            ("deleted", "deleted", "2026-08-21T11:00:00+00:00", "Deleted", long_summary),
        )
        for observation_id, external_id, received_at, title, summary in observations:
            connection.execute(
                """INSERT INTO raw_observations VALUES (
                   ?,'src',?,?,?, ?,?,'https://example.test/source',?,'{}','captured'
                )""",
                (
                    observation_id,
                    external_id,
                    timestamp,
                    received_at,
                    title,
                    summary,
                    observation_id[0] * 64,
                ),
            )
            connection.execute(
                """INSERT INTO source_revisions VALUES (
                   ?,?,'src',?,1,?,?,?, ?,?,'{}'
                )""",
                (
                    f"revision-{observation_id}",
                    observation_id,
                    external_id,
                    "delete" if observation_id == "deleted" else "new",
                    received_at,
                    observation_id[0] * 64,
                    title,
                    summary,
                ),
            )
            connection.execute(
                "INSERT INTO event_observations VALUES ('browse-00000',?,?,?)",
                (
                    observation_id,
                    "filtered_aggregated_noise" if observation_id == "noise" else "primary",
                    received_at,
                ),
            )
        connection.commit()

    repository = LedgerRepository(ledger_path)
    bounded = repository.list_events(limit=1, source_excerpt_chars=512)["items"][0]
    unbounded = repository.list_events(limit=1)["items"][0]

    assert bounded["captured_source_count"] == 2
    assert bounded["displayable_source_count"] == 1
    assert bounded["primary_source_url_count"] == 1
    assert bounded["public_source_url_count"] == 1
    assert bounded["captured_text_count"] == 1
    assert bounded["source_problem_count"] == 1
    assert bounded["source_title"] == "Active"
    assert len(bounded["source_summary"]) == 512
    assert unbounded["source_summary"] == long_summary


def test_public_source_counts_do_not_treat_private_url_as_reader_link(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "private-source-url.sqlite3"
    _large_unreviewed_ledger(ledger_path, event_count=1)
    timestamp = "2026-08-21T12:00:00+00:00"
    with open_ledger(ledger_path) as connection:
        connection.execute(
            """INSERT INTO raw_observations VALUES (
               'private','src','private',?,?,?,?,'http://127.0.0.1/source',?,'{}','captured'
            )""",
            (
                timestamp,
                timestamp,
                "Retained private source",
                "The captured text is retained for audit use.",
                "a" * 64,
            ),
        )
        connection.execute(
            """INSERT INTO source_revisions VALUES (
               'revision-private','private','src','private',1,'new',?,?,?,?,'{}'
            )""",
            (
                timestamp,
                "a" * 64,
                "Retained private source",
                "The captured text is retained for audit use.",
            ),
        )
        connection.execute(
            "INSERT INTO event_observations VALUES ('browse-00000','private','primary',?)",
            (timestamp,),
        )
        connection.commit()

    item = LedgerRepository(ledger_path).list_events(limit=1)["items"][0]

    assert item["captured_source_count"] == 1
    assert item["displayable_source_count"] == 1
    assert item["primary_source_url_count"] == 0
    assert item["public_source_url_count"] == 0
    assert item["captured_text_count"] == 1


def test_empty_capture_is_not_described_as_saved_reader_text(tmp_path: Path) -> None:
    ledger_path = tmp_path / "empty-capture.sqlite3"
    _large_unreviewed_ledger(ledger_path, event_count=1)
    timestamp = "2026-08-21T12:00:00+00:00"
    with open_ledger(ledger_path) as connection:
        connection.execute(
            """INSERT INTO raw_observations VALUES (
               'empty','src','empty',?,?,'','','http://127.0.0.1/source',?,'{}','captured'
            )""",
            (timestamp, timestamp, "b" * 64),
        )
        connection.execute(
            """INSERT INTO source_revisions VALUES (
               'revision-empty','empty','src','empty',1,'new',?,?,'','','{}'
            )""",
            (timestamp, "b" * 64),
        )
        connection.execute(
            "INSERT INTO event_observations VALUES ('browse-00000','empty','primary',?)",
            (timestamp,),
        )
        connection.commit()

    item = LedgerRepository(ledger_path).list_events(limit=1)["items"][0]

    assert item["captured_source_count"] == 1
    assert item["displayable_source_count"] == 0
    assert item["public_source_url_count"] == 0
    assert item["captured_text_count"] == 0


def test_event_detail_separates_text_preference_from_primary_source_link(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "separate-source-link.sqlite3"
    _large_unreviewed_ledger(ledger_path, event_count=1)
    received_at = "2026-08-21T12:00:00+00:00"
    with open_ledger(ledger_path) as connection:
        connection.execute(
            "INSERT INTO sources VALUES ('src-p2','News','news_secondary','P2',1,1,?,?)",
            (received_at, received_at),
        )
        connection.commit()
    sources = (
        (
            "same-day-news",
            "src-p2",
            "2026-08-01T10:00:00+00:00",
            "Same-day publisher text",
            "The detailed same-day source excerpt is retained for reading.",
            "https://news.example/story",
        ),
        (
            "official-source",
            "src",
            "2026-07-31T10:00:00+00:00",
            "Official public source",
            "",
            "https://regulator.example/notice",
        ),
    )
    with open_ledger(ledger_path) as connection:
        for observation_id, source_id, published_at, title, summary, source_url in sources:
            connection.execute(
                "INSERT INTO raw_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    observation_id,
                    source_id,
                    observation_id,
                    published_at,
                    received_at,
                    title,
                    summary,
                    source_url,
                    observation_id[0] * 64,
                    "{}",
                    "captured",
                ),
            )
            connection.execute(
                "INSERT INTO source_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"revision-{observation_id}",
                    observation_id,
                    source_id,
                    observation_id,
                    1,
                    "new",
                    received_at,
                    observation_id[0] * 64,
                    title,
                    summary,
                    "{}",
                ),
            )
            connection.execute(
                "INSERT INTO event_observations VALUES ('browse-00000',?,'primary',?)",
                (observation_id, received_at),
            )
        connection.commit()

    detail = LedgerRepository(ledger_path).event_detail("browse-00000")

    assert detail is not None
    assert detail["preferred_source"]["title"] == "Same-day publisher text"
    assert detail["preferred_source"]["canonical_url"] == "https://news.example/story"
    assert detail["source_link"]["title"] == "Official public source"
    assert detail["source_link"]["canonical_url"] == (
        "https://regulator.example/notice"
    )


def test_public_browse_prefers_same_day_source_text_over_later_form_title(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "source-ranking.sqlite3"
    _large_unreviewed_ledger(ledger_path, event_count=1)
    received_at = "2026-08-21T12:00:00+00:00"
    with open_ledger(ledger_path) as connection:
        for observation_id, published_at, title, summary in (
            ("same-day", "2026-08-01", "SEC 8-K SAME", "The company filed for Chapter 11 protection on August 1."),
            ("later", "2026-08-20", "SEC 8-K LATER", ""),
        ):
            connection.execute(
                """INSERT INTO raw_observations VALUES (
                   ?,'src',?,?,?, ?,?,'https://example.test/source',?,'{}','captured'
                )""",
                (
                    observation_id,
                    observation_id,
                    published_at,
                    received_at,
                    title,
                    summary,
                    observation_id[0] * 64,
                ),
            )
            connection.execute(
                "INSERT INTO event_observations VALUES ('browse-00000',?,'primary',?)",
                (observation_id, received_at),
            )
        connection.commit()

    item = LedgerRepository(ledger_path).list_events(limit=1)["items"][0]
    assert item["source_title"] == "SEC 8-K SAME"
    assert item["source_summary"].startswith("The company filed for Chapter 11")


def test_explicit_reader_ready_filter_still_uses_full_gate(tmp_path: Path) -> None:
    ledger_path = tmp_path / "strict.sqlite3"
    _large_unreviewed_ledger(ledger_path, event_count=25)

    repository = LedgerRepository(ledger_path)
    assert repository.list_events(reader_ready=True, limit=50)["total"] == 0
    assert repository.list_events(reader_ready=False, limit=50)["total"] == 25
    assert repository.list_events(public_state="pending_verification", limit=50)["total"] == 25
    assert repository.list_events(public_state="verified", limit=50)["total"] == 0

    empty_page = repository.list_events(limit=10, offset=1000)
    assert empty_page["total"] == 25
    assert empty_page["items"] == []


def test_public_browse_excludes_only_explicit_nonfinancial_retractions(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "filtered-only.sqlite3"
    _large_unreviewed_ledger(ledger_path, event_count=3)
    timestamp = "2026-08-21T12:00:00+00:00"
    with open_ledger(ledger_path) as connection:
        for index, relation_type in (
            (0, "filtered_aggregated_noise"),
            (1, "filtered_aggregated_noise"),
            (1, "primary"),
        ):
            observation_id = f"noise-{index}-{relation_type}"
            connection.execute(
                """INSERT INTO raw_observations VALUES (
                   ?,'src',?,?,?,?,?,'https://example.test/source',?,'{}','captured'
                )""",
                (
                    observation_id,
                    observation_id,
                    timestamp,
                    timestamp,
                    observation_id,
                    observation_id,
                    f"{index + 1:064x}",
                ),
            )
            connection.execute(
                "INSERT INTO event_observations VALUES (?,?,?,?)",
                (
                    f"browse-{index:05d}",
                    observation_id,
                    relation_type,
                    timestamp,
                ),
            )
        connection.execute(
            """INSERT INTO event_versions VALUES (
               'browse-00000',2,?,'rejected','rejected','regulatory','filing',
               NULL,'{}','official_nonfinancial_notice')""",
            (timestamp,),
        )
        connection.execute(
            """UPDATE canonical_events
               SET current_version=2,status='rejected',label_status='rejected'
               WHERE event_id='browse-00000'"""
        )
        connection.commit()

    repository = LedgerRepository(ledger_path)
    public_page = repository.list_events(
        exclude_nonfinancial_retractions=True,
        limit=10,
    )
    internal_page = repository.list_events(limit=10)
    public_facets = repository.event_facets(
        exclude_nonfinancial_retractions=True
    )

    assert internal_page["total"] == 3
    assert public_page["total"] == 2
    assert {item["event_id"] for item in public_page["items"]} == {
        "browse-00001",
        "browse-00002",
    }
    assert sum(item["count"] for item in public_facets["families"]) == 2
