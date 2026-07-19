from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from audit_live_pipeline import audit
from event_ledger import (
    link_event_chain_member,
    open_ledger,
    upsert_event_chain,
    upsert_source,
    utc_now,
)


class LivePipelineAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection = open_ledger(Path(self.temp_dir.name) / "audit.sqlite3")
        self.now = utc_now()
        upsert_source(
            self.connection,
            source_id="sec_current_filings",
            name="SEC current filings",
            source_type="official_primary",
            authority_tier="P0",
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def add_event(self, event_id: str, *, status: str, manual: bool = False) -> None:
        self.connection.execute(
            """INSERT INTO canonical_events(
               event_id,current_version,status,label_status,event_family,event_type,event_date,
               first_seen_at,last_updated_at,discovery_source,no_trading
               ) VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
            (
                event_id,
                2 if manual else 1,
                status,
                status,
                "regulatory",
                "test_event",
                "2026-07-16",
                self.now,
                self.now,
                "sec_current_filings",
            ),
        )
        self.connection.execute(
            """INSERT INTO event_versions(
               event_id,version,changed_at,status,label_status,event_family,event_type,
               facts_json,change_reason
               ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                event_id,
                2 if manual else 1,
                self.now,
                status,
                status,
                "regulatory",
                "test_event",
                "{}",
                "manual_primary_evidence_review" if manual else "live_rule_candidate",
            ),
        )

    def add_observation(self, event_id: str, suffix: str, relation_type: str) -> None:
        observation_id = f"obs-{event_id}-{suffix}"
        self.connection.execute(
            """INSERT INTO raw_observations(
               observation_id,source_id,external_id,local_received_at,title,summary,
               content_sha256,raw_json,observation_status
               ) VALUES (?,?,?,?,?,?,?,?,'captured')""",
            (
                observation_id,
                "sec_current_filings",
                suffix,
                self.now,
                suffix,
                suffix,
                suffix,
                "{}",
            ),
        )
        self.connection.execute(
            """INSERT INTO event_observations(event_id,observation_id,relation_type,linked_at)
               VALUES (?,?,?,?)""",
            (event_id, observation_id, relation_type, self.now),
        )

    def test_manual_promotion_and_confirming_evidence_are_not_auto_violations(self) -> None:
        self.add_event("manual", status="verified", manual=True)
        self.add_observation("manual", "discovery", "official_primary_candidate")
        self.add_observation("manual", "review", "confirming_primary_evidence")
        result = audit(self.connection)
        self.assertEqual(result["checks"]["official_auto_promotion_violations"], 0)
        self.assertEqual(result["checks"]["official_multi_event_cluster_violations"], 0)

    def test_nonmanual_promotion_and_multiple_discovery_rows_still_fail(self) -> None:
        self.add_event("automatic", status="verified", manual=False)
        self.add_observation("automatic", "one", "official_primary_candidate")
        self.add_observation("automatic", "two", "official_primary_candidate")
        result = audit(self.connection)
        self.assertEqual(result["checks"]["official_auto_promotion_violations"], 1)
        self.assertEqual(result["checks"]["official_multi_event_cluster_violations"], 1)

    def test_event_chain_requires_one_primary_and_matching_pointer(self) -> None:
        self.add_event("primary", status="verified", manual=True)
        self.add_event("support", status="verified", manual=True)
        upsert_event_chain(
            self.connection,
            chain_id="CHAIN-fomc",
            chain_type="monetary_policy_meeting",
            canonical_key="fomc-2026-06-16-17",
        )
        link_event_chain_member(
            self.connection,
            chain_id="CHAIN-fomc",
            event_id="primary",
            chain_role="primary_event",
            counts_as_primary_event=True,
            rationale="Statement is the decision.",
        )
        link_event_chain_member(
            self.connection,
            chain_id="CHAIN-fomc",
            event_id="support",
            chain_role="same_episode_support",
            counts_as_primary_event=False,
            rationale="Projection is supporting evidence.",
        )
        result = audit(self.connection)
        self.assertEqual(result["checks"]["event_chain_primary_count_violations"], 0)
        self.assertEqual(result["checks"]["event_chain_primary_pointer_violations"], 0)
        self.connection.execute(
            "UPDATE event_chains SET primary_event_id='support' WHERE chain_id='CHAIN-fomc'"
        )
        result = audit(self.connection)
        self.assertEqual(result["checks"]["event_chain_primary_pointer_violations"], 1)


if __name__ == "__main__":
    unittest.main()
