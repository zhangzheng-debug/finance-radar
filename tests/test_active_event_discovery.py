from __future__ import annotations

import json
import csv
import sys
import tempfile
import unittest
from pathlib import Path

import polars as pl


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import active_event_discovery as discovery


def candidate(
    event_id: str,
    *,
    stable_id: str,
    security_id: str,
    ticker: str,
    event_date: str,
    family: str,
    event_type: str,
    source: str,
    severity: float,
    match_status: str = "matched",
    security_type: str = "Domestic Common Stock",
) -> dict[str, object]:
    return {
        "event_candidate_id": event_id,
        "stable_id_match_status": match_status,
        "stable_id": stable_id,
        "security_master_id": security_id,
        "ticker_at_event": ticker,
        "category": security_type,
        "security_type": security_type,
        "event_date": event_date,
        "event_family": family,
        "event_type": event_type,
        "detection_rule": "test rule",
        "detection_value": "test value",
        "severity_raw": severity,
        "source_table": source,
        "label_status": "candidate",
    }


class ActiveEventDiscoveryTests(unittest.TestCase):
    def test_module_root_points_to_workspace(self) -> None:
        self.assertEqual(discovery.ROOT, ROOT)
        self.assertTrue((discovery.ROOT / "config" / "active_event_research.json").is_file())

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        curated = self.root / "data" / "curated"
        curated.mkdir(parents=True)

        rows = [
            candidate(
                "C1",
                stable_id="P1",
                security_id="S1",
                ticker="AAA",
                event_date="2024-01-01",
                family="bankruptcy_or_distress",
                event_type="bankruptcy_liquidation",
                source="ACTIONS",
                severity=3,
            ),
            candidate(
                "C2",
                stable_id="P2",
                security_id="S2",
                ticker="BBB",
                event_date="2024-02-01",
                family="price_crash",
                event_type="one_day_crash",
                source="SEP",
                severity=0.4,
            ),
            candidate(
                "C3",
                stable_id="P3",
                security_id="S3",
                ticker="CCC",
                event_date="2024-03-01",
                family="fundamental_shock",
                event_type="negative_equity",
                source="SF1",
                severity=2,
            ),
            candidate(
                "REVIEWED",
                stable_id="P4",
                security_id="S4",
                ticker="DDD",
                event_date="2024-04-01",
                family="equity_dilution",
                event_type="reverse_split",
                source="ACTIONS",
                severity=2,
            ),
            candidate(
                "UNMATCHED",
                stable_id="P5",
                security_id="S5",
                ticker="EEE",
                event_date="2024-05-01",
                family="price_crash",
                event_type="volume_crash",
                source="SEP",
                severity=1,
                match_status="unmatched",
            ),
            candidate(
                "PREFERRED",
                stable_id="P6",
                security_id="S6",
                ticker="FFF-P",
                event_date="2024-06-01",
                family="price_crash",
                event_type="volume_crash",
                source="SEP",
                severity=1,
                security_type="Domestic Common Stock",
            ),
            candidate(
                "UNIT_DOT",
                stable_id="P7",
                security_id="S7",
                ticker="WRAC.U",
                event_date="2024-07-01",
                family="price_crash",
                event_type="volume_crash",
                source="SEP",
                severity=1,
                security_type="Domestic Common Stock",
            ),
            candidate(
                "UNIT_NASDAQ",
                stable_id="P8",
                security_id="S8",
                ticker="APADU",
                event_date="2024-08-01",
                family="delisting_or_suspension",
                event_type="delisted",
                source="ACTIONS",
                severity=2,
                security_type="Domestic Common Stock",
            ),
        ]
        pl.DataFrame(rows).write_parquet(curated / "event_candidates.parquet")
        pl.DataFrame(
            [
                {
                    "security_master_id": f"S{index}",
                    "company_name": f"Company {index}",
                    "exchange": "NYSE",
                    "sector": "Test",
                    "industry": "Test",
                    "secfilings": (
                        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK="
                        f"{index:010d}"
                    ),
                }
                for index in range(1, 9)
            ]
        ).write_parquet(curated / "security_master.parquet")
        pl.DataFrame({"event_id": ["REVIEWED"]}).write_parquet(
            curated / "event_label_book_v0.parquet"
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_queue_is_balanced_safe_and_excludes_reviewed(self) -> None:
        queue = discovery.build_active_queue(
            self.root,
            start_date="2020-01-01",
            end_date=None,
            per_family=2,
            max_total=10,
        )
        self.assertEqual(set(queue["event_candidate_id"]), {"C1", "C2", "C3"})
        self.assertFalse(discovery.POST_EVENT_OUTCOME_COLUMNS.intersection(queue.columns))
        self.assertIn("family_rank", queue.columns)
        self.assertTrue(queue["cik"].is_not_null().all())
        price = queue.filter(pl.col("event_candidate_id") == "C2").row(0, named=True)
        self.assertEqual(price["provisional_grade_cap"], "C_price_only")
        self.assertEqual(price["selection_strategy"], "price_dislocation_evidence_search")
        bankruptcy = queue.filter(pl.col("event_candidate_id") == "C1").row(0, named=True)
        self.assertEqual(bankruptcy["provisional_grade_cap"], "A++_candidate")

    def test_misclassified_unit_tickers_are_not_common_equity(self) -> None:
        queue = discovery.build_active_queue(
            self.root,
            start_date="2020-01-01",
            end_date=None,
            per_family=10,
            max_total=50,
        )
        self.assertFalse({"UNIT_DOT", "UNIT_NASDAQ"}.intersection(queue["event_candidate_id"]))

    def test_five_letter_otc_alias_is_retained_and_routed_to_identity_review(self) -> None:
        curated = self.root / "data" / "curated"
        candidates = pl.read_parquet(curated / "event_candidates.parquet")
        candidates = pl.concat(
            [
                candidates,
                pl.DataFrame(
                    [
                        candidate(
                            "OTC_ALIAS",
                            stable_id="P9",
                            security_id="S9",
                            ticker="BRCNF",
                            event_date="2022-09-09",
                            family="delisting_or_suspension",
                            event_type="delisted",
                            source="ACTIONS",
                            severity=2,
                        )
                    ]
                ),
            ],
            how="vertical_relaxed",
        )
        candidates.write_parquet(curated / "event_candidates.parquet")

        security = pl.read_parquet(curated / "security_master.parquet")
        security = pl.concat(
            [
                security,
                pl.DataFrame(
                    [
                        {
                            "security_master_id": "S9",
                            "company_name": "Alias Test Company",
                            "exchange": "NYSE",
                            "sector": "Test",
                            "industry": "Test",
                            "secfilings": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000000009",
                        }
                    ]
                ),
            ],
            how="vertical_relaxed",
        )
        security.write_parquet(curated / "security_master.parquet")

        queue = discovery.build_active_queue(
            self.root,
            start_date="2020-01-01",
            end_date=None,
            per_family=10,
            max_total=50,
        )
        row = queue.filter(pl.col("event_candidate_id") == "OTC_ALIAS").row(0, named=True)
        self.assertEqual(row["ticker_at_event"], "BRCNF")
        self.assertTrue(row["identity_review_flag"])
        self.assertEqual(row["selection_strategy"], "event_time_identity_review")
        self.assertEqual(row["selection_status"], "needs_event_time_identity_review")
        self.assertIn("do not backfill OTC alias", row["required_evidence"])
        self.assertEqual(row["provisional_grade_cap"], "A_candidate")

    def test_otc_alias_pattern_is_a_narrow_routing_hint(self) -> None:
        frame = pl.DataFrame(
            {"ticker_at_event": ["PTRCY", "BRCNF", "IDEXQ", "EVKG", "AAPL", "ABCDE"]}
        ).with_columns(discovery._possible_post_event_otc_alias().alias("flag"))
        self.assertEqual(frame["flag"].to_list(), [True, True, True, False, False, False])

    def test_fundamental_semantic_filter_blocks_known_ratio_artifacts(self) -> None:
        frame = pl.DataFrame(
            [
                {"event_type": "revenue_collapse_yoy", "detection_value": "-4.5", "detection_rule": "MRQ revenue yoy", "sector": "Technology", "industry": "Software"},
                {"event_type": "revenue_collapse_yoy", "detection_value": "-0.75", "detection_rule": "MRQ revenue yoy", "sector": "Technology", "industry": "Software"},
                {"event_type": "gross_margin_collapse", "detection_value": "-5.0", "detection_rule": "margin", "sector": "Healthcare", "industry": "Biotechnology"},
                {"event_type": "free_cash_flow_turn_negative", "detection_value": "fcf=-1;prev=1", "detection_rule": "previous quarter", "sector": "Consumer", "industry": "Retail"},
                {"event_type": "cash_short_debt_stress", "detection_value": "0", "detection_rule": "cash ratio", "sector": "Industrials", "industry": "Shell Companies"},
                {"event_type": "interest_coverage_below_1", "detection_value": "-3", "detection_rule": "coverage", "sector": "Industrials", "industry": "Machinery"},
            ]
        ).filter(discovery._fundamental_semantic_filter())
        self.assertEqual(frame["event_type"].to_list(), ["revenue_collapse_yoy", "interest_coverage_below_1"])

    def test_outputs_record_invariants(self) -> None:
        queue = discovery.build_active_queue(
            self.root,
            start_date="2020-01-01",
            end_date=None,
            per_family=2,
            max_total=10,
        )
        output_dir = self.root / "finance" / "data" / "research"
        result = discovery.write_outputs(
            queue,
            short_root=self.root,
            output_dir=output_dir,
            start_date="2020-01-01",
            end_date=None,
            per_family=2,
            max_total=10,
        )
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["selection"]["rows"], 3)
        self.assertFalse(manifest["invariants"]["post_event_outcomes_used_for_ranking"])
        self.assertTrue(manifest["invariants"]["family_and_event_type_balanced"])
        self.assertFalse(manifest["invariants"]["live_trading_allowed"])
        self.assertTrue(result.report_path.is_file())

    def test_additional_completed_ids_are_excluded(self) -> None:
        queue = discovery.build_active_queue(
            self.root,
            start_date="2020-01-01",
            end_date=None,
            per_family=2,
            max_total=10,
            additional_excluded_ids={"C2"},
        )
        self.assertEqual(set(queue["event_candidate_id"]), {"C1", "C3"})

    def test_additional_completed_threads_exclude_new_sibling_ids(self) -> None:
        queue = discovery.build_active_queue(
            self.root,
            start_date="2020-01-01",
            end_date=None,
            per_family=2,
            max_total=10,
            additional_excluded_threads={("P2", "2024-02-01", "price_crash")},
        )
        self.assertEqual(set(queue["event_candidate_id"]), {"C1", "C3"})

    def test_completed_price_episode_excludes_nearby_cross_batch_detector(self) -> None:
        queue = discovery.build_active_queue(
            self.root,
            start_date="2020-01-01",
            end_date=None,
            per_family=2,
            max_total=10,
            additional_excluded_threads={("P2", "2024-01-15", "price_crash")},
        )
        self.assertEqual(set(queue["event_candidate_id"]), {"C1", "C3"})

    def test_reviewed_thread_excludes_all_sibling_detectors(self) -> None:
        queue_rows = [
            {"event_candidate_id": "a", "stable_id": "p1", "event_date": "2024-01-01", "event_family": "fundamental_shock"},
            {"event_candidate_id": "b", "stable_id": "p1", "event_date": "2024-01-01", "event_family": "fundamental_shock"},
            {"event_candidate_id": "c", "stable_id": "p2", "event_date": "2024-01-01", "event_family": "fundamental_shock"},
        ]
        completed = discovery.completed_review_event_ids(
            queue_rows, [{"event_candidate_id": "a"}]
        )
        self.assertEqual(completed, {"a", "b"})

    def test_adjudication_metadata_recovers_thread_without_old_queue_row(self) -> None:
        threads = discovery.completed_review_threads(
            [],
            [
                {
                    "event_candidate_id": "old",
                    "stable_id": "p1",
                    "event_date": "2024-01-01",
                    "detected_event_type": "negative_equity",
                }
            ],
        )
        self.assertEqual(threads, {("p1", "2024-01-01", "fundamental_shock")})

    def test_completed_registry_keeps_adjudications_outside_current_queue(self) -> None:
        queue_path = self.root / "queue.csv"
        adjudications_path = self.root / "adjudications.csv"
        registry_path = self.root / "registry.csv"
        with queue_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["event_candidate_id", "stable_id", "event_date", "event_family"],
            )
            writer.writeheader()
            writer.writerow(
                {"event_candidate_id": "queued", "stable_id": "p1", "event_date": "2024-01-01", "event_family": "price_crash"}
            )
        with adjudications_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["event_candidate_id"])
            writer.writeheader()
            writer.writerow({"event_candidate_id": "outside"})
        completed = discovery.update_completed_registry(
            registry_path,
            queue_path=queue_path,
            adjudications_path=adjudications_path,
        )
        self.assertEqual(completed, {"outside"})


if __name__ == "__main__":
    unittest.main()
