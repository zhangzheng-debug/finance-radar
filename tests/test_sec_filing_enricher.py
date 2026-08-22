from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from event_ledger import open_ledger, stable_id, utc_now
import sec_filing_enricher as enricher


INDEX = b"""<html><body>
<table class="tableFile" summary="Document Format Files">
<tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
<tr><td>1</td><td>CURRENT REPORT</td><td><a href="/ix?doc=/Archives/a/main.htm">main.htm</a></td><td>8-K</td><td>1000</td></tr>
<tr><td>2</td><td>PRESS RELEASE</td><td><a href="/Archives/a/ex991.htm">ex991.htm</a></td><td>EX-99.1</td><td>2000</td></tr>
<tr><td></td><td>Complete submission text file</td><td><a href="/Archives/a/all.txt">all.txt</a></td><td></td><td>5000</td></tr>
</table></body></html>"""


class FakeClient:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    def get(self, url: str) -> bytes:
        self.calls.append(url)
        return self.payloads[url]


class PartialFailureClient(FakeClient):
    def __init__(self, payloads: dict[str, bytes], failing_urls: set[str]) -> None:
        super().__init__(payloads)
        self.failing_urls = failing_urls

    def get(self, url: str) -> bytes:
        self.calls.append(url)
        if url in self.failing_urls:
            raise RuntimeError(
                f"SEC document exceeds safe capture limit (5000000 bytes): {url}"
            )
        return self.payloads[url]


class SecFilingEnricherTests(unittest.TestCase):
    def _seed_discovery_lead(self, connection, *, suffix: str = "lead") -> None:
        now = "2026-08-20T01:02:03+00:00"
        raw_json = json.dumps(
            {"item": {"form": "8-K", "items": ["5.02"], "company": "Example Corp"}}
        )
        connection.execute(
            """INSERT INTO sources VALUES (
               'sec_current_filings','SEC','official_primary_feed','P0_official',1,1,?,?)""",
            (now, now),
        )
        connection.execute(
            """INSERT INTO raw_observations VALUES (
               ?,'sec_current_filings',?,'2026-08-20T00:30:00+00:00',?,
               '8-K - Example Corp','Item 5.02','https://www.sec.gov/Archives/a/index.htm',
               ?,?,'completed_discovery_lead')""",
            (
                f"obs-{suffix}",
                suffix,
                now,
                hashlib.sha256(raw_json.encode()).hexdigest(),
                raw_json,
            ),
        )
        connection.execute(
            """INSERT INTO discovery_leads VALUES (
               ?,?,'sec_current_filings','PENDING_ENRICHMENT','governance','management_change',
               'Example Corp','EXM','2026-08-20',?,NULL,NULL,NULL,
               'https://www.sec.gov/Archives/a/index.htm',NULL,
               'link_only_no_relevant_passage',?,'[]','["SEC_REQUIRES_DOCUMENT_SEMANTIC_MATCH"]',
               'event-admission-v1',NULL,?,?,1)""",
            (
                f"lead-{suffix}",
                f"obs-{suffix}",
                now,
                hashlib.sha256(raw_json.encode()).hexdigest(),
                now,
                now,
            ),
        )
        connection.commit()

    def test_discovery_lead_is_promoted_only_after_scoped_document_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = open_ledger(Path(directory) / "db.sqlite3")
            self._seed_discovery_lead(connection)
            client = FakeClient(
                {
                    "https://www.sec.gov/Archives/a/index.htm": INDEX,
                    "https://www.sec.gov/Archives/a/main.htm": (
                        b"<html><body>Item 5.02. Example Corp reported that its chief "
                        b"financial officer resigned effective immediately on August 20, 2026.</body></html>"
                    ),
                    "https://www.sec.gov/Archives/a/ex991.htm": (
                        b"<html><body>Example Corp announces the executive transition and "
                        b"names the interim chief financial officer.</body></html>"
                    ),
                }
            )

            result = enricher.enrich_pending_discovery_leads(connection, client, limit=5)
            lead = connection.execute("SELECT * FROM discovery_leads").fetchone()
            event = connection.execute("SELECT * FROM canonical_events").fetchone()
            relation = connection.execute("SELECT * FROM event_evidence_relations").fetchone()
            workflow = connection.execute("SELECT * FROM event_fact_workflow").fetchone()
            job = connection.execute("SELECT * FROM pipeline_jobs").fetchone()
            facts = json.loads(
                connection.execute("SELECT facts_json FROM event_versions").fetchone()[0]
            )
            connection.close()

        self.assertEqual(result["promoted"], 1)
        self.assertEqual(lead["status"], "PROMOTED")
        self.assertEqual(lead["canonical_event_id"], event["event_id"])
        self.assertEqual(event["status"], "candidate")
        self.assertEqual(event["event_type"], "management_change")
        self.assertEqual(relation["relation_status"], "SCOPED_MATCH")
        self.assertEqual(relation["subject_match"], 1)
        self.assertEqual(relation["event_claim_supported"], 1)
        self.assertEqual(workflow["workflow_state"], "EVIDENCE_READY")
        self.assertEqual(job["status"], "PENDING_EVIDENCE_REVIEW")
        self.assertEqual(facts["claim_subject"], "Example Corp")
        self.assertEqual(facts["claim_stage"], "DISCLOSED")
        self.assertEqual(
            facts["claim_fact_slots"]["contract_version"],
            "deterministic-evidence-fact-slots-v2",
        )
        self.assertEqual(
            facts["claim_fact_slots"]["facts"][0]["predicate"],
            "OFFICER_DEPARTURE",
        )
        self.assertEqual(facts["claim_fact_slots"]["facts"][0]["action_text"], "resigned")
        self.assertIn("chief financial officer", facts["public_fact_summary"])
        self.assertIn("resigned", facts["public_fact_summary"])
        self.assertNotIn("与“管理层任免或离职”有关", facts["public_fact_summary"])
        self.assertEqual(lead["claim_summary"], facts["public_fact_summary"])
        self.assertFalse(facts["formal_verification"])

    def test_oversize_primary_does_not_block_smaller_exhibit_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = open_ledger(Path(directory) / "db.sqlite3")
            self._seed_discovery_lead(connection, suffix="oversize-primary")
            client = PartialFailureClient(
                {
                    "https://www.sec.gov/Archives/a/index.htm": INDEX,
                    "https://www.sec.gov/Archives/a/ex991.htm": (
                        b"<html><body>Example Corp reported that its chief financial officer "
                        b"resigned effective immediately on August 20, 2026.</body></html>"
                    ),
                },
                {"https://www.sec.gov/Archives/a/main.htm"},
            )

            result = enricher.enrich_pending_discovery_leads(connection, client, limit=5)
            lead = connection.execute("SELECT * FROM discovery_leads").fetchone()
            event_count = connection.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0]
            connection.close()

        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["needs_evidence"], 1)
        self.assertEqual(result["promoted"], 0)
        self.assertEqual(lead["status"], "NEEDS_EVIDENCE")
        self.assertEqual(lead["evidence_status"], "attachment_incomplete")
        self.assertIn("resigned", lead["evidence_passage"])
        self.assertEqual(event_count, 0)
        self.assertIn("https://www.sec.gov/Archives/a/main.htm", client.calls)
        self.assertIn("https://www.sec.gov/Archives/a/ex991.htm", client.calls)

    def test_specific_event_phrase_without_affirmed_action_stays_needs_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = open_ledger(Path(directory) / "db.sqlite3")
            self._seed_discovery_lead(connection, suffix="unsupported-merger")
            client = FakeClient(
                {
                    "https://www.sec.gov/Archives/a/index.htm": INDEX,
                    "https://www.sec.gov/Archives/a/main.htm": (
                        b"<html><body>Example Corp attached a merger agreement for reference, "
                        b"but the filing does not state that it was signed or completed.</body></html>"
                    ),
                    "https://www.sec.gov/Archives/a/ex991.htm": (
                        b"<html><body>The merger agreement is included only as background; "
                        b"it was not signed or completed.</body></html>"
                    ),
                }
            )

            result = enricher.enrich_pending_discovery_leads(connection, client, limit=5)
            lead = connection.execute("SELECT * FROM discovery_leads").fetchone()
            event_count = connection.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0]
            connection.close()

        self.assertEqual(result["needs_evidence"], 1)
        self.assertEqual(result["promoted"], 0)
        self.assertEqual(lead["status"], "NEEDS_EVIDENCE")
        self.assertIn("EVENT_PREDICATE_NOT_SUPPORTED", lead["admission_reasons_json"])
        self.assertIn("尚不能通过确定性规则回答", lead["claim_summary"])
        self.assertEqual(event_count, 0)

    def test_unimplemented_fact_extractors_fail_closed_after_sec_classification(self) -> None:
        cases = {
            "bankruptcy": (
                "Example Corp filed a bankruptcy petition under chapter 11 and began proceedings."
            ),
            "earnings_or_guidance": (
                "Example Corp reported results of operations and financial condition and "
                "financial results for the quarter."
            ),
        }
        for expected_type, passage in cases.items():
            with self.subTest(expected_type=expected_type), tempfile.TemporaryDirectory() as directory:
                connection = open_ledger(Path(directory) / "db.sqlite3")
                self._seed_discovery_lead(connection, suffix=expected_type)
                payload = f"<html><body>{passage}</body></html>".encode()
                client = FakeClient(
                    {
                        "https://www.sec.gov/Archives/a/index.htm": INDEX,
                        "https://www.sec.gov/Archives/a/main.htm": payload,
                        "https://www.sec.gov/Archives/a/ex991.htm": payload,
                    }
                )

                result = enricher.enrich_pending_discovery_leads(connection, client, limit=5)
                lead = connection.execute("SELECT * FROM discovery_leads").fetchone()
                event_count = connection.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0]
                relation_count = connection.execute(
                    "SELECT COUNT(*) FROM event_evidence_relations"
                ).fetchone()[0]
                connection.close()

            self.assertEqual(result["needs_evidence"], 1)
            self.assertEqual(result["promoted"], 0)
            self.assertEqual(lead["status"], "NEEDS_EVIDENCE")
            self.assertEqual(lead["claim_action"], expected_type)
            self.assertIn("EVENT_PREDICATE_NOT_SUPPORTED", lead["admission_reasons_json"])
            self.assertIn("候选分类，不能当作已发生事实", lead["claim_summary"])
            self.assertEqual(event_count, 0)
            self.assertEqual(relation_count, 0)

    def test_fact_slot_gate_rejects_cross_entity_and_denied_actions_end_to_end(self) -> None:
        cases = {
            "other-company-management": (
                "Example Corp disclosed that Customer Finance Inc. chief financial "
                "officer resigned effective immediately."
            ),
            "other-company-listing": (
                "Example Corp reported that Target Corp received a notice of "
                "delisting from Nasdaq."
            ),
            "subsidiary-offering": (
                "Target Corp, a subsidiary of Example Corp, issued $25 million of "
                "common stock in a public offering."
            ),
            "other-company-pronoun": (
                "Target Corp stated that the company issued $25 million of common "
                "stock in a public offering."
            ),
            "cross-sentence-other-company-pronoun": (
                "Target Corp is a customer mentioned in the filing. The company "
                "issued $25 million of common stock in a public offering."
            ),
            "declined-appointment": (
                "Item 5.02. Example Corp declined to appoint Jane Doe as chief "
                "financial officer."
            ),
            "denied-merger-completion": (
                "Example Corp denied the merger agreement statement; it is false "
                "that Example Corp completed the acquisition."
            ),
            "postposed-denial": (
                "Example Corp issued $25 million of common stock in a public offering. "
                "Example Corp later denied that it had issued common stock."
            ),
            "separated-postposed-denial": (
                "Example Corp disclosed that its chief financial officer resigned. "
                "The report described an executive transition. Example Corp denied "
                "that the resignation occurred."
            ),
            "relative-rejection": (
                "Example Corp issued $25 million of common stock in a public offering, "
                "which Example Corp later rejected as inaccurate."
            ),
        }
        for suffix, passage in cases.items():
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                connection = open_ledger(Path(directory) / "db.sqlite3")
                self._seed_discovery_lead(connection, suffix=suffix)
                payload = f"<html><body>{passage}</body></html>".encode()
                client = FakeClient(
                    {
                        "https://www.sec.gov/Archives/a/index.htm": INDEX,
                        "https://www.sec.gov/Archives/a/main.htm": payload,
                        "https://www.sec.gov/Archives/a/ex991.htm": payload,
                    }
                )

                result = enricher.enrich_pending_discovery_leads(connection, client, limit=5)
                lead = connection.execute("SELECT * FROM discovery_leads").fetchone()
                event_count = connection.execute(
                    "SELECT COUNT(*) FROM canonical_events"
                ).fetchone()[0]
                relation_count = connection.execute(
                    "SELECT COUNT(*) FROM event_evidence_relations"
                ).fetchone()[0]
                connection.close()

            self.assertEqual(result["promoted"], 0, suffix)
            self.assertEqual(result["needs_evidence"], 1, suffix)
            self.assertEqual(lead["status"], "NEEDS_EVIDENCE")
            self.assertIn("EVENT_PREDICATE_NOT_SUPPORTED", lead["admission_reasons_json"])
            self.assertEqual(event_count, 0)
            self.assertEqual(relation_count, 0)

    def test_management_roles_are_bound_to_their_nearest_action_end_to_end(self) -> None:
        passage = (
            "Item 5.02. Example Corp appointed Jane Doe as interim chief financial "
            "officer after its director resigned."
        )
        with tempfile.TemporaryDirectory() as directory:
            connection = open_ledger(Path(directory) / "db.sqlite3")
            self._seed_discovery_lead(connection, suffix="nearest-role")
            payload = f"<html><body>{passage}</body></html>".encode()
            client = FakeClient(
                {
                    "https://www.sec.gov/Archives/a/index.htm": INDEX,
                    "https://www.sec.gov/Archives/a/main.htm": payload,
                    "https://www.sec.gov/Archives/a/ex991.htm": payload,
                }
            )

            result = enricher.enrich_pending_discovery_leads(connection, client, limit=5)
            lead = connection.execute("SELECT * FROM discovery_leads").fetchone()
            facts = json.loads(
                connection.execute("SELECT facts_json FROM event_versions").fetchone()[0]
            )
            connection.close()

        self.assertEqual(result["promoted"], 1)
        self.assertEqual(lead["status"], "PROMOTED")
        extracted = facts["claim_fact_slots"]["facts"]
        departure = next(row for row in extracted if row["predicate"] == "OFFICER_DEPARTURE")
        self.assertEqual(departure["role"], "director")
        self.assertIn("director", facts["public_fact_summary"])
        self.assertNotIn("interim chief financial officer”辞任", facts["public_fact_summary"])

    def test_unscoped_filing_remains_a_lead_and_never_creates_a_canonical_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = open_ledger(Path(directory) / "db.sqlite3")
            self._seed_discovery_lead(connection, suffix="ordinary")
            client = FakeClient(
                {
                    "https://www.sec.gov/Archives/a/index.htm": INDEX,
                    "https://www.sec.gov/Archives/a/main.htm": (
                        b"<html><body>Example Corp filed an ordinary administrative update "
                        b"and incorporated prior disclosures by reference.</body></html>"
                    ),
                    "https://www.sec.gov/Archives/a/ex991.htm": (
                        b"<html><body>This exhibit contains only routine administrative "
                        b"information and no described corporate action.</body></html>"
                    ),
                }
            )

            result = enricher.enrich_pending_discovery_leads(connection, client, limit=5)
            lead = connection.execute("SELECT * FROM discovery_leads").fetchone()
            event_count = connection.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0]
            connection.close()

        self.assertEqual(result["no_scoped_event"], 1)
        self.assertEqual(lead["status"], "LEAD_NO_SCOPED_EVENT")
        self.assertEqual(event_count, 0)

    def test_index_parser_canonicalizes_ix_and_keeps_exhibit(self) -> None:
        rows = enricher.parse_filing_index(INDEX)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].url, "https://www.sec.gov/Archives/a/main.htm")
        self.assertEqual(rows[1].document_type, "EX-99.1")

    def test_negated_bankruptcy_language_is_not_an_event_match(self) -> None:
        negative = enricher.classify_filing_text(
            "We have never declared bankruptcy and have never been in receivership."
        )
        positive = enricher.classify_filing_text(
            "The court appointed a receiver and the company entered receivership."
        )
        self.assertIsNone(negative.event_type)
        self.assertEqual(positive.event_type, "bankruptcy")

    def test_item_101_is_refined_by_specific_financing_semantics(self) -> None:
        samples = {
            "debt_refinancing": "Item 1.01 Material Definitive Agreement. The company completed the refinancing in a CLO Reset Transaction.",
            "credit_facility_amendment": "Item 1.01 Material Definitive Agreement. The Credit Facility Amendment amended the Credit Agreement.",
            "convertible_debt_financing": "Item 1.01 Material Definitive Agreement. The company issued convertible senior notes in an aggregate principal amount bearing cash interest.",
            "senior_unsecured_debt_financing": "Item 1.01 Material Definitive Agreement. The company issued additional senior unsecured notes in a private placement.",
            "offering_or_dilution": "Item 1.01 Material Definitive Agreement. Under a warrant inducement the holder exercised existing warrants and received new warrants.",
            "going_concern_financing_dependency": "The company has recurring losses and substantial doubt exists about its ability to continue as a going concern without additional funding.",
            "spac_sponsor_working_capital_note": "Item 1.01. The sponsor advanced an additional amount and the company issued an amended and restated working capital note. It is convertible only after an initial business combination.",
            "spac_ipo_closing": "Item 1.01. The company completed its IPO and deposited the proceeds into a trust account pending a future initial business combination.",
            "merger_or_acquisition": "Item 1.01 and Item 2.01. The merger agreement closed; the merger consideration was issued and the closing occurred simultaneously with execution.",
        }
        for expected, text in samples.items():
            with self.subTest(expected=expected):
                self.assertEqual(enricher.classify_filing_text(text).event_type, expected)
        self.assertEqual(
            enricher.classify_filing_text(
                "Item 1.01. The company will use the net proceeds of the offering to repay in full its senior secured term loan."
            ).event_type,
            "debt_refinancing",
        )

    def test_administrative_filing_semantics_override_broad_form_labels(self) -> None:
        samples = {
            "pro_forma_merger_financial_statement_amendment": (
                "Item 9.01. The amendment provides unaudited pro forma condensed combined "
                "statements for the previously disclosed merger and merger consideration."
            ),
            "routine_board_committee_appointment": (
                "Item 5.02. The director was appointed to serve as a member of the "
                "Management Development and Compensation Committee."
            ),
            "equity_incentive_plan_share_reserve_reduction": (
                "Item 5.02. The committee amended the inducement plan to reduce the number "
                "of shares of our common stock authorized for issuance."
            ),
        }
        for expected, text in samples.items():
            with self.subTest(expected=expected):
                self.assertEqual(enricher.classify_filing_text(text).event_type, expected)

    def test_reviewed_sec_boundary_patterns_override_generic_filing_labels(self) -> None:
        samples = {
            "auditor_change_without_disagreement": (
                "The auditor declined to stand for reappointment. The company reported no disagreements."
            ),
            "share_repurchase_authorization_expansion": (
                "The board expanded its share repurchase program and increased the aggregate authorization."
            ),
            "routine_nt_10q_extension_request": (
                "The company is finalizing the financial statements and expects to file within the five-day extension period."
            ),
            "minimum_bid_price_deficiency_notice": (
                "Nasdaq sent a Minimum Bid Price Deficiency notice that is not of imminent delisting."
            ),
            "nda_resubmission_regulatory_process_update": (
                "The FDA meeting concerned potential resubmission of the New Drug Application."
            ),
        }
        for expected, text in samples.items():
            with self.subTest(expected=expected):
                self.assertEqual(enricher.classify_filing_text(text).event_type, expected)

    def test_specific_spac_semantics_repair_broad_candidate_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = open_ledger(Path(directory) / "db.sqlite3")
            now = utc_now()
            connection.execute(
                """INSERT INTO sources VALUES (
                   'sec_current_filings','SEC','official_primary_feed','P0_official',1,1,?,?)""",
                (now, now),
            )
            observation_id = stable_id("OBS", "sec_current_filings", "spac-ipo")
            raw_json = json.dumps({"item": {"form": "8-K"}})
            connection.execute(
                """INSERT INTO raw_observations VALUES (
                   ?,'sec_current_filings','spac-ipo','2026-07-15',?,'8-K SPAC IPO','Item 1.01',
                   'https://www.sec.gov/Archives/a/index.htm',?,?, 'captured')""",
                (observation_id, now, hashlib.sha256(raw_json.encode()).hexdigest(), raw_json),
            )
            connection.execute(
                """INSERT INTO canonical_events VALUES (
                   'evt',1,'candidate','candidate','corporate_action','merger_or_acquisition',
                   '2026-07-15',?,?,NULL,NULL,'Example SPAC',NULL,'A_P0','sec_current_filings',1)""",
                (now, now),
            )
            connection.execute(
                """INSERT INTO event_versions VALUES (
                   'evt',1,?,'candidate','candidate','corporate_action','merger_or_acquisition',
                   NULL,'{}','live_rule_candidate')""",
                (now,),
            )
            connection.execute(
                "INSERT INTO event_observations VALUES ('evt',?,'official_primary_candidate',?)",
                (observation_id, now),
            )
            excerpt = (
                "Item 1.01. The company completed its IPO and deposited $200 million "
                "into a trust account pending a future initial business combination."
            )
            connection.execute(
                """INSERT INTO sec_filing_enrichments(
                   enrichment_id,event_id,observation_id,accession_number,form,
                   filing_index_url,primary_document_url,documents_json,evidence_excerpt,
                   text_sha256,matched_event_family,matched_event_type,matched_keywords_json,
                   confidence,status,attempts,last_error,fetched_at,updated_at,read_only,no_trading
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'PARSED',1,NULL,?,?,1,1)""",
                (
                    'enrichment','evt',observation_id,'0000000000-26-000001','8-K',
                    'https://www.sec.gov/Archives/a/index.htm',
                    'https://www.sec.gov/Archives/a/main.htm','[]',excerpt,
                    hashlib.sha256(excerpt.encode()).hexdigest(),
                    'spac_capital_formation','spac_ipo_closing','["completed its ipo"]',0.96,
                    now,now,
                ),
            )
            connection.commit()
            repaired = enricher.reclassify_parsed_enrichments(connection)
            event = connection.execute(
                "SELECT event_type,current_version,status FROM canonical_events WHERE event_id='evt'"
            ).fetchone()
            connection.close()
        self.assertEqual(repaired, 1)
        self.assertEqual(event["event_type"], "spac_ipo_closing")
        self.assertEqual(event["current_version"], 2)
        self.assertEqual(event["status"], "candidate")

    def test_evidence_excerpt_prefers_quantified_exhibit_over_item_boilerplate(self) -> None:
        text = (
            "Item 2.02 Results of Operations and Financial Condition. "
            "A copy of the press release is attached. The information shall not be deemed to be filed. "
            + "ordinary filing boilerplate " * 120
            + "Exhibit 99.1 Preliminary financial results. The company expects operating income "
            "$153.0 million to $160.0 million, net income $124.8 million to $130.3 million, "
            "and diluted EPS to increase 15.2%. Revenue and cash flow also increased."
        )
        excerpt = enricher.evidence_excerpt(
            text,
            ("item 2.02", "results of operations and financial condition", "financial results"),
            max_chars=900,
        )
        self.assertIn("$153.0 million", excerpt)
        self.assertIn("15.2%", excerpt)

    def test_primary_document_refines_generic_candidate_without_verifying(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = open_ledger(Path(directory) / "db.sqlite3")
            now = utc_now()
            connection.execute(
                """INSERT INTO sources VALUES (
                   'sec_current_filings','SEC','official_primary_feed','P0_official',1,1,?,?)""",
                (now, now),
            )
            raw_json = json.dumps(
                {"item": {"form": "8-K", "external_id": "urn:accession-number=0001234567-26-000001"}}
            )
            observation_id = stable_id("OBS", "sec_current_filings", "acc")
            connection.execute(
                """INSERT INTO raw_observations VALUES (
                   ?,'sec_current_filings','acc','2026-07-15',?,'8-K Example','Item 8.01',
                   'https://www.sec.gov/Archives/a/index.htm',?,?, 'captured')""",
                (observation_id, now, hashlib.sha256(raw_json.encode()).hexdigest(), raw_json),
            )
            connection.execute(
                """INSERT INTO canonical_events VALUES (
                   'evt',1,'candidate','candidate','regulatory_filing','sec_material_filing',
                   '2026-07-15',?,?,NULL,NULL,'Example Corp',NULL,'A_P0','sec_current_filings',1)""",
                (now, now),
            )
            connection.execute(
                """INSERT INTO event_versions VALUES (
                   'evt',1,?,'candidate','candidate','regulatory_filing','sec_material_filing',
                   NULL,'{}','live_rule_candidate')""",
                (now,),
            )
            connection.execute(
                "INSERT INTO event_observations VALUES ('evt',?,'official_primary_candidate',?)",
                (observation_id, now),
            )
            connection.execute(
                """INSERT INTO pipeline_jobs VALUES (
                   'job','evt','live_primary_evidence_review','PENDING_PRIMARY_EVIDENCE',
                   90,0,?,NULL,'{}',?,?)""",
                (now, now, now),
            )
            connection.commit()
            client = FakeClient(
                {
                    "https://www.sec.gov/Archives/a/index.htm": INDEX,
                    "https://www.sec.gov/Archives/a/main.htm": b"<html><body>Item 5.02 Departure of Directors or Certain Officers.</body></html>",
                    "https://www.sec.gov/Archives/a/ex991.htm": b"<html><body>The chief financial officer resigned effective immediately.</body></html>",
                }
            )
            result = enricher.enrich_pending(connection, client, limit=5)
            materialized = enricher.materialize_parsed_enrichment_evidence(connection)
            materialized_again = enricher.materialize_parsed_enrichment_evidence(connection)
            event = connection.execute("SELECT * FROM canonical_events").fetchone()
            enrichment = connection.execute("SELECT * FROM sec_filing_enrichments").fetchone()
            evidence = connection.execute("SELECT * FROM event_evidence").fetchone()
            job = connection.execute("SELECT * FROM pipeline_jobs").fetchone()
            versions = connection.execute("SELECT COUNT(*) FROM event_versions").fetchone()[0]
            ordinary_pending = enricher.pending_rows(connection, limit=5)
            refresh_pending = enricher.pending_rows(connection, limit=5, refresh_parsed=True)
            connection.execute(
                """UPDATE event_evidence
                   SET evidence_status='confirmed_primary',evidence_passage='human reviewed passage'"""
            )
            connection.execute(
                "UPDATE sec_filing_enrichments SET evidence_excerpt='later machine passage'"
            )
            connection.commit()
            preserved = enricher.materialize_parsed_enrichment_evidence(connection)
            reviewed_evidence = connection.execute("SELECT * FROM event_evidence").fetchone()
            connection.close()
        self.assertEqual(result["parsed"], 1)
        self.assertEqual(result["refined"], 1)
        self.assertEqual(event["event_type"], "management_change")
        self.assertEqual(event["status"], "candidate")
        self.assertEqual(event["current_version"], 2)
        self.assertEqual(enrichment["status"], "PARSED")
        self.assertEqual(enrichment["no_trading"], 1)
        self.assertEqual(materialized["evidence_inserted"], 1)
        self.assertEqual(materialized["evidence_updated"], 0)
        self.assertEqual(materialized["jobs_advanced"], 1)
        self.assertEqual(materialized_again["evidence_inserted"], 0)
        self.assertEqual(materialized_again["evidence_updated"], 0)
        self.assertEqual(materialized_again["evidence_unchanged"], 1)
        self.assertEqual(materialized_again["jobs_advanced"], 0)
        self.assertEqual(preserved["reviewed_status_preserved"], 1)
        self.assertEqual(preserved["evidence_updated"], 0)
        self.assertEqual(reviewed_evidence["evidence_status"], "confirmed_primary")
        self.assertEqual(reviewed_evidence["evidence_passage"], "human reviewed passage")
        self.assertEqual(evidence["evidence_status"], "machine_extracted_unreviewed")
        self.assertEqual(evidence["auto_verification_allowed"], 0)
        self.assertEqual(job["status"], "PENDING_EVIDENCE_REVIEW")
        self.assertEqual(versions, 2)
        self.assertEqual(ordinary_pending, [])
        self.assertEqual(len(refresh_pending), 1)

    def test_parsed_generic_sec_filing_is_evidenced_but_kept_nonterminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = open_ledger(Path(directory) / "db.sqlite3")
            now = utc_now()
            connection.execute(
                """INSERT INTO sources VALUES (
                   'sec_current_filings','SEC','official_primary_feed','P0_official',1,1,?,?)""",
                (now, now),
            )
            raw_json = json.dumps({"item": {"form": "8-K", "items": ["8.01"]}})
            observation_id = stable_id("OBS", "sec_current_filings", "generic")
            connection.execute(
                """INSERT INTO raw_observations VALUES (
                   ?,'sec_current_filings','generic','2026-07-15',?,'8-K Example','Item 8.01',
                   'https://www.sec.gov/Archives/generic/index.htm',?,?,'captured')""",
                (observation_id, now, hashlib.sha256(raw_json.encode()).hexdigest(), raw_json),
            )
            connection.execute(
                """INSERT INTO canonical_events VALUES (
                   'generic-event',1,'candidate','candidate','regulatory_filing','sec_material_filing',
                   '2026-07-15',?,?,NULL,NULL,'Example Corp',NULL,'A_P0','sec_current_filings',1)""",
                (now, now),
            )
            connection.execute(
                """INSERT INTO event_versions VALUES (
                   'generic-event',1,?,'candidate','candidate','regulatory_filing','sec_material_filing',
                   NULL,'{}','live_rule_candidate')""",
                (now,),
            )
            connection.execute(
                "INSERT INTO event_observations VALUES ('generic-event',?,'official_primary_candidate',?)",
                (observation_id, now),
            )
            connection.execute(
                """INSERT INTO pipeline_jobs VALUES (
                   'generic-job','generic-event','live_primary_evidence_review',
                   'PENDING_PRIMARY_EVIDENCE',90,0,?,NULL,'{}',?,?)""",
                (now, now, now),
            )
            connection.execute(
                """INSERT INTO sec_filing_enrichments(
                   enrichment_id,event_id,observation_id,accession_number,form,
                   filing_index_url,primary_document_url,documents_json,evidence_excerpt,
                   text_sha256,matched_event_family,matched_event_type,matched_keywords_json,
                   confidence,status,attempts,last_error,fetched_at,updated_at,read_only,no_trading
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'PARSED',1,NULL,?,?,1,1)""",
                (
                    "generic-enrichment",
                    "generic-event",
                    observation_id,
                    "0000000000-26-000002",
                    "8-K",
                    "https://www.sec.gov/Archives/generic/index.htm",
                    "https://www.sec.gov/Archives/generic/main.htm",
                    "[]",
                    "The company announced an ordinary administrative update.",
                    hashlib.sha256(b"ordinary").hexdigest(),
                    None,
                    None,
                    "[]",
                    0.0,
                    now,
                    now,
                ),
            )
            connection.commit()
            result = enricher.materialize_parsed_enrichment_evidence(connection)
            event = connection.execute(
                "SELECT status,label_status,current_version FROM canonical_events"
            ).fetchone()
            job = connection.execute("SELECT status,last_error FROM pipeline_jobs").fetchone()
            evidence_count = connection.execute("SELECT COUNT(*) FROM event_evidence").fetchone()[0]
            connection.close()
        self.assertEqual(result["semantic_noise_rejected"], 0)
        self.assertEqual(result["semantic_inconclusive"], 1)
        self.assertEqual(event["status"], "candidate")
        self.assertEqual(event["label_status"], "candidate")
        self.assertEqual(event["current_version"], 1)
        self.assertEqual(job["status"], "PENDING_EVIDENCE_REVIEW")
        self.assertIn("no_scoped_event_match", job["last_error"])
        self.assertEqual(evidence_count, 1)

    def test_document_selection_keeps_multiple_decision_relevant_exhibits(self) -> None:
        documents = [
            enricher.FilingDocument("1", "CURRENT REPORT", "main.htm", "8-K", "100", "https://x/main"),
            enricher.FilingDocument("2", "PRESS RELEASE", "ex991.htm", "EX-99.1", "100", "https://x/99"),
            enricher.FilingDocument("3", "MATERIAL AGREEMENT", "ex101.htm", "EX-10.1", "100", "https://x/10"),
            enricher.FilingDocument("4", "MERGER AGREEMENT", "ex21.htm", "EX-2.1", "100", "https://x/2"),
            enricher.FilingDocument("5", "DEBT INSTRUMENT", "ex41.htm", "EX-4.1", "100", "https://x/4"),
        ]
        selected = enricher.choose_documents(documents, "8-K")
        self.assertEqual([item.url for item in selected], [item.url for item in documents])

    def test_pending_evidence_job_is_not_starved_by_newer_filings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = open_ledger(Path(directory) / "db.sqlite3")
            now = utc_now()
            connection.execute(
                """INSERT INTO sources VALUES (
                   'sec_current_filings','SEC','official_primary_feed','P0_official',1,1,?,?)""",
                (now, now),
            )
            for suffix, published in (("older", "2026-07-22T10:00:00+00:00"), ("newer", "2026-07-23T10:00:00+00:00")):
                observation_id = f"obs-{suffix}"
                event_id = f"event-{suffix}"
                raw_json = json.dumps({"item": {"form": "8-K"}})
                connection.execute(
                    """INSERT INTO raw_observations VALUES (
                       ?,'sec_current_filings',?,?,?,'8-K Example','Item 8.01',
                       ?,?,?,'captured')""",
                    (
                        observation_id,
                        suffix,
                        published,
                        now,
                        "https://www.sec.gov/Archives/a/index.htm",
                        hashlib.sha256(raw_json.encode()).hexdigest(),
                        raw_json,
                    ),
                )
                connection.execute(
                    """INSERT INTO canonical_events VALUES (
                       ?,1,'candidate','candidate','regulatory_filing','sec_material_filing',
                       '2026-07-23',?,?,NULL,NULL,?,NULL,'A_P0','sec_current_filings',1)""",
                    (event_id, now, now, f"Example {suffix}"),
                )
                connection.execute(
                    """INSERT INTO event_versions VALUES (
                       ?,1,?,'candidate','candidate','regulatory_filing','sec_material_filing',
                       NULL,'{}','live_rule_candidate')""",
                    (event_id, now),
                )
                connection.execute(
                    "INSERT INTO event_observations VALUES (?,?, 'official_primary_candidate',?)",
                    (event_id, observation_id, now),
                )
            connection.execute(
                """INSERT INTO pipeline_jobs VALUES (
                   'older-job','event-older','live_primary_evidence_review',
                   'PENDING_PRIMARY_EVIDENCE',26,0,?,?,'{}',?,?)""",
                (now, "reprocess_after_nonterminal_no_match_policy", now, now),
            )
            connection.execute(
                """INSERT INTO pipeline_jobs VALUES (
                   'newer-job','event-newer','live_primary_evidence_review',
                   'PENDING_PRIMARY_EVIDENCE',90,0,?,NULL,'{}',?,?)""",
                (now, now, now),
            )
            connection.commit()
            selected = enricher.pending_rows(connection, limit=1)
            exact = enricher.pending_rows(connection, limit=1, event_id="event-newer")
            connection.close()
        self.assertEqual(selected[0]["event_id"], "event-older")
        self.assertEqual(exact[0]["event_id"], "event-newer")

    def test_item_901_without_exhibit_is_marked_incomplete(self) -> None:
        documents = [
            enricher.FilingDocument("1", "CURRENT REPORT", "main.htm", "8-K", "100", "https://x/main")
        ]
        selected = enricher.choose_documents(documents, "8-K")
        manifest = enricher.document_manifest(
            documents,
            selected,
            {"https://x/main": "Item 9.01 Financial Statements and Exhibits."},
            item_codes=("7.01", "9.01"),
        )
        self.assertEqual(manifest["coverage_status"], "INCOMPLETE_EXPECTED_EXHIBIT")


if __name__ == "__main__":
    unittest.main()
