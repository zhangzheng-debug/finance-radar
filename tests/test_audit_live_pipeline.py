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

    def test_mapping_audit_requires_a_current_decision_receipt_projection_chain(self) -> None:
        self.add_event("mapped", status="candidate")
        policy_hash = "a" * 64
        source_hash = "b" * 64
        self.connection.execute(
            """INSERT INTO assets(
                   asset_id,asset_type,symbol,provider_symbol,venue,metadata_json,
                   created_at,updated_at)
               VALUES('AST-M','etf','GLD','GLD','TwelveData','{}',?,?)""",
            (self.now, self.now),
        )
        self.connection.execute(
            """INSERT INTO event_asset_mapping_decisions(
                   decision_id,event_id,event_version,policy_version,policy_sha256,
                   observation_id,source_content_sha256,decision,rule_id,asset_count,
                   created_at,no_trading)
               VALUES('DEC-M','mapped',1,'v1',?,'obs',?,'MAPPED','macro',1,?,1)""",
            (policy_hash, source_hash, self.now),
        )
        self.connection.execute(
            """INSERT INTO event_asset_mapping_receipts(
                   receipt_id,event_id,event_version,mapping_decision_id,asset_id,
                   relation_type,display_role,proxy_label,rule_id,policy_version,
                   policy_sha256,mapping_rank,confidence,decision,reason_codes_json,
                   created_at,no_trading)
               VALUES('REC-M','mapped',1,'DEC-M','AST-M','MACRO_PROXY',
                      'THEMATIC_PROXY','黄金ETF代理','macro','v1',?,1,1.0,
                      'SELECTED','[]',?,1)""",
            (policy_hash, self.now),
        )
        self.connection.commit()

        missing = audit(self.connection)
        self.assertEqual(
            missing["checks"]["automatic_asset_mapping_projection_violations"], 1
        )

        self.connection.execute(
            """INSERT INTO event_asset_impacts(
                   impact_id,event_id,asset_id,relation_type,direction,impact_score,
                   confidence,reason_codes_json,assessment_source,mapping_decision_id,
                   market_observation_allowed,no_trading,created_at,updated_at)
               VALUES('IMP-M','mapped','AST-M','MACRO_PROXY','ABSTAIN',0,1.0,'[]',
                      'automatic_asset_mapping_v1:macro','DEC-M',1,1,?,?)""",
            (self.now, self.now),
        )
        self.connection.commit()
        valid = audit(self.connection)
        self.assertEqual(
            valid["checks"]["automatic_asset_mapping_projection_violations"], 0
        )

    def test_mapping_audit_rejects_non_hex_content_hashes(self) -> None:
        self.add_event("bad-hash", status="candidate")
        self.connection.execute(
            """INSERT INTO event_asset_mapping_decisions(
                   decision_id,event_id,event_version,policy_version,policy_sha256,
                   observation_id,source_content_sha256,decision,asset_count,created_at,no_trading)
               VALUES('DEC-BAD','bad-hash',1,'v1',?,'obs',?,'NO_MATCH',0,?,1)""",
            ("z" * 64, "not-a-sha", self.now),
        )
        self.connection.commit()
        result = audit(self.connection)
        self.assertEqual(result["checks"]["mapping_decision_boundary_violations"], 1)


if __name__ == "__main__":
    unittest.main()
