from __future__ import annotations

import hashlib
from pathlib import Path

from app.services.subjectless_event_cleanup import (
    plan_subjectless_cleanup,
    purge_subjectless_events,
)
from scripts.event_ledger import open_ledger, stable_id, upsert_source, utc_now


def test_subjectless_cleanup_removes_event_projection_but_preserves_source(tmp_path: Path) -> None:
    connection = open_ledger(tmp_path / "ledger.sqlite3")
    upsert_source(
        connection,
        source_id="opennews_free",
        name="OpenNews",
        source_type="aggregated_discovery",
        authority_tier="P2_experimental",
    )
    now = utc_now()
    for suffix, company in (("empty", None), ("named", "Named Corp")):
        observation_id = stable_id("OBS", suffix)
        event_id = stable_id("EVENT", suffix)
        title = f"{suffix} bankruptcy report"
        connection.execute(
            """INSERT INTO raw_observations VALUES (
               ?,?,?,?,?,?,?,'',?,?,'captured')""",
            (
                observation_id,
                "opennews_free",
                suffix,
                now,
                now,
                title,
                title,
                hashlib.sha256(title.encode()).hexdigest(),
                "{}",
            ),
        )
        connection.execute(
            """INSERT INTO canonical_events VALUES (
               ?,1,'candidate','candidate','distress','bankruptcy','2026-08-21',
               ?,?,NULL,NULL,?,NULL,'B_P2_discovery_only','opennews_free',1)""",
            (event_id, now, now, company),
        )
        connection.execute(
            "INSERT INTO event_versions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                1,
                now,
                "candidate",
                "candidate",
                "distress",
                "bankruptcy",
                None,
                "{}",
                "fixture",
            ),
        )
        connection.execute(
            "INSERT INTO event_observations VALUES (?,?,?,?)",
            (event_id, observation_id, "aggregated_discovery_candidate", now),
        )
        connection.execute(
            """INSERT INTO pipeline_jobs VALUES (
               ?,?,'live_primary_evidence_review','PENDING_PRIMARY_EVIDENCE',
               10,0,?,NULL,'{}',?,?)""",
            (stable_id("JOB", suffix), event_id, now, now, now),
        )
    connection.commit()

    plan = plan_subjectless_cleanup(connection)
    result = purge_subjectless_events(connection)

    assert plan.event_count == 1
    assert result.deleted == 1
    assert result.raw_observations_preserved == 1
    assert connection.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0] == 1
    assert connection.execute("SELECT company_name FROM canonical_events").fetchone()[0] == "Named Corp"
    assert connection.execute("SELECT COUNT(*) FROM raw_observations").fetchone()[0] == 2
    assert connection.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0] == 1
    connection.close()

