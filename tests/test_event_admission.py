from __future__ import annotations

from app.services.event_admission import (
    extract_evidence_fact_slots,
    evaluate_event_admission,
    public_fact_summary,
    requires_specific_fact_extraction,
    supports_deterministic_fact_extraction,
)


BASE = {
    "event_id": "event-1",
    "event_version": 1,
    "evidence_id": "evidence-1",
    "subject": "Example Corporation",
    "action": "management_change",
    "stage": "DISCLOSED",
    "known_at": "2026-08-20T01:02:03+00:00",
    "source_authority_tier": "P0_official",
    "evidence_url": "https://www.sec.gov/Archives/example.htm",
    "evidence_passage": (
        "Example Corporation disclosed that its chief financial officer resigned "
        "effective immediately on August 20, 2026."
    ),
    "evidence_status": "machine_extracted_unreviewed",
    "content_sha256": "a" * 64,
    "subject_match": True,
    "event_claim_supported": True,
    "date_coherent": True,
}


def decision(**overrides):
    payload = {**BASE, **overrides}
    extraction = payload.pop(
        "fact_extraction",
        extract_evidence_fact_slots(
            evidence_passage=payload["evidence_passage"],
            event_type=payload["action"],
            expected_subject=payload["subject"],
        ),
    )
    summary = payload.pop(
        "public_fact_summary_text",
        public_fact_summary(
            subject=payload["subject"],
            action_label=payload["action"],
            stage_label=payload["stage"],
            extraction=extraction,
        ),
    )
    payload["fact_extraction"] = extraction
    payload["public_fact_summary_text"] = summary
    return evaluate_event_admission(**payload)


def test_admission_accepts_only_a_scoped_primary_evidence_claim() -> None:
    result = decision()

    assert result.admitted is True
    assert result.workflow_state == "EVIDENCE_READY"
    assert result.reasons == ()
    assert len(result.evidence_fingerprint) == 64
    assert len(result.fact_slot_receipt_sha256) == 64


def test_v2_admission_replays_slots_and_binds_passage_slots_and_summary() -> None:
    baseline = decision()
    changed_passage = decision(
        evidence_passage=(
            "Example Corporation disclosed that its chief executive officer retired "
            "effective immediately on August 20, 2026."
        )
    )
    forged_slots = extract_evidence_fact_slots(
        evidence_passage=BASE["evidence_passage"],
        event_type=BASE["action"],
        expected_subject="Customer Finance Inc.",
    )
    slot_mismatch = decision(fact_extraction=forged_slots)
    summary_mismatch = decision(public_fact_summary_text="A substituted public summary.")

    assert baseline.admitted is True
    assert changed_passage.admitted is True
    assert changed_passage.evidence_fingerprint != baseline.evidence_fingerprint
    assert slot_mismatch.admitted is False
    assert "FACT_SLOT_EXTRACTION_NOT_REPRODUCIBLE" in slot_mismatch.reasons
    assert slot_mismatch.evidence_fingerprint != baseline.evidence_fingerprint
    assert summary_mismatch.admitted is False
    assert "PUBLIC_FACT_SUMMARY_NOT_REPRODUCIBLE" in summary_mismatch.reasons
    assert summary_mismatch.evidence_fingerprint != baseline.evidence_fingerprint


def test_admission_rejects_non_decision_and_subject_mismatch() -> None:
    result = decision(
        evidence_status="machine_extracted_non_decision",
        subject_match=False,
    )

    assert result.admitted is False
    assert result.workflow_state == "NEEDS_EVIDENCE"
    assert "EVIDENCE_STATUS_NOT_SUPPORTIVE" in result.reasons
    assert "EVIDENCE_STATUS_EXPLICITLY_BLOCKED" in result.reasons
    assert "SUBJECT_NOT_BOUND_TO_EVIDENCE" in result.reasons


def test_admission_rejects_missing_time_hash_and_non_primary_source() -> None:
    result = decision(
        known_at="2026-08-20",
        content_sha256="not-a-hash",
        source_authority_tier="P2_discovery",
    )

    assert result.admitted is False
    assert "MISSING_OR_NAIVE_KNOWN_AT" in result.reasons
    assert "MISSING_SOURCE_CONTENT_HASH" in result.reasons
    assert "SOURCE_NOT_P0_P1" in result.reasons


def test_admission_rejects_a_link_without_an_exact_supporting_passage() -> None:
    result = decision(evidence_passage="See filing.", event_claim_supported=False)

    assert result.admitted is False
    assert "MISSING_EXACT_PASSAGE" in result.reasons
    assert "EVENT_PREDICATE_NOT_SUPPORTED" in result.reasons


def test_management_slots_answer_who_did_what_without_inventing_values() -> None:
    passage = (
        "Example Corp reported that its chief financial officer resigned effective "
        "immediately on August 20, 2026. Example Corp named Jane Doe as the interim "
        "chief financial officer effective August 20, 2026."
    )

    extraction = extract_evidence_fact_slots(
        evidence_passage=passage,
        event_type="management_change",
        expected_subject="Example Corp",
    )
    summary = public_fact_summary(
        subject="Example Corp",
        action_label="管理层任免或离职",
        stage_label="DISCLOSED",
        extraction=extraction,
    )

    assert [fact.predicate for fact in extraction.facts] == [
        "OFFICER_DEPARTURE",
        "OFFICER_APPOINTMENT",
    ]
    assert extraction.facts[0].role_text == "chief financial officer"
    assert extraction.facts[0].effective_text == "effective immediately"
    assert extraction.facts[1].person_text == "Jane Doe"
    assert extraction.facts[1].role_text == "interim chief financial officer"
    assert "Example Corp" in summary
    assert "Jane Doe" in summary
    assert "resigned" in summary
    assert "named" in summary
    assert "与“管理层任免或离职”有关" not in summary
    for fact in extraction.facts:
        for value in (
            fact.subject_text,
            fact.actor_text,
            fact.action_text,
            fact.object_text,
            fact.role_text,
            fact.person_text,
            fact.amount_text,
            fact.date_text,
            fact.effective_text,
            fact.modality_text,
        ):
            if value:
                assert value in fact.evidence_sentence

    for other_company_role in (
        "Example Corp appointed John Doe as chief financial officer of Target Corp.",
        "Example Corp appointed John Doe as director of Target Corp.",
        "Example Corp appointed John Doe as chief financial officer for Target Corp.",
        "Example Corp appointed John Doe as director at Target Corp.",
    ):
        mismatch = extract_evidence_fact_slots(
            evidence_passage=other_company_role,
            event_type=(
                "chief_financial_officer_appointment"
                if "financial" in other_company_role
                else "management_change"
            ),
            expected_subject="Example Corp",
        )
        assert mismatch.facts
        assert mismatch.facts[0].subject_binding == "OTHER_NAMED_ENTITY"
        assert mismatch.supports_specific_fact is False


def test_financing_slots_preserve_exact_instrument_amount_and_date() -> None:
    passage = (
        "Example Corp issued $150 million aggregate principal amount of convertible "
        "senior notes on August 20, 2026."
    )

    extraction = extract_evidence_fact_slots(
        evidence_passage=passage,
        event_type="convertible_debt_financing",
        expected_subject="Example Corp",
    )

    assert len(extraction.facts) == 1
    fact = extraction.facts[0]
    assert fact.predicate == "SECURITIES_ISSUANCE"
    assert fact.action_text == "issued"
    assert fact.object_text == "convertible senior notes"
    assert fact.amount_text == "$150 million"
    assert fact.date_text == "August 20, 2026"

    unrelated = extract_evidence_fact_slots(
        evidence_passage=(
            "Example Corp issued a press release and separately described a public offering."
        ),
        event_type="offering_or_dilution",
        expected_subject="Example Corp",
    )
    assert unrelated.facts == ()

    for other_company_financing in (
        "Example Corp announced a public offering by Target Corp.",
        "Example Corp announced a private placement for Target Corp.",
        "Example Corp announced a public offering of Target Corp.",
        "Example Corp announced a private placement involving Target Corp.",
    ):
        mismatch = extract_evidence_fact_slots(
            evidence_passage=other_company_financing,
            event_type="offering_or_dilution",
            expected_subject="Example Corp",
        )
        assert mismatch.facts
        assert mismatch.facts[0].subject_binding == "OTHER_NAMED_ENTITY"
        assert mismatch.supports_specific_fact is False


def test_issuer_named_board_can_prove_an_officer_appointment() -> None:
    passages = (
        (
            "MicroVision, Inc.",
            "On August 7, 2026, the Board of Directors (the Board) of MicroVision, "
            "Inc. (the Company) appointed Christine Chambers as the Company's Chief "
            "Financial Officer, effective as of August 27, 2026.",
        ),
        (
            "Polomar Health Services, Inc.",
            "On July 15, 2026, the Board of Directors of Polomar Health Services, "
            "Inc. (the Company) appointed Douglas Beck as the Company's Chief "
            "Financial Officer and Treasurer, effective July 15, 2026.",
        ),
        (
            "Example Corp",
            "Example Corp (the Company), today announced that the board of directors "
            "of the Company (the Board) has appointed Jane Doe as the Company's Chief "
            "Financial Officer, effective August 3, 2026.",
        ),
    )
    for subject, passage in passages:
        extraction = extract_evidence_fact_slots(
            evidence_passage=passage,
            event_type="chief_financial_officer_appointment",
            expected_subject=subject,
        )
        assert extraction.supports_specific_fact is True, passage
        assert extraction.supported_facts[0].subject_text == subject
        assert extraction.supported_facts[0].subject_binding == "EXPLICIT_ISSUER_CONTEXT"


def test_unbound_or_other_company_board_cannot_prove_issuer_appointment() -> None:
    for passage in (
        "The Board appointed Jane Doe as chief financial officer.",
        "Target Corp's Board appointed Jane Doe as chief financial officer.",
        "The Board of Directors of Target Corp appointed Jane Doe as chief financial officer.",
        "Example Corp described Target Corp, whose Board appointed Jane Doe as chief financial officer.",
    ):
        extraction = extract_evidence_fact_slots(
            evidence_passage=passage,
            event_type="chief_financial_officer_appointment",
            expected_subject="Example Corp",
        )
        assert extraction.supports_specific_fact is False, passage


def test_another_issuers_board_cannot_bind_through_an_apposition() -> None:
    """An intervening apposition must not hand one issuer another's board.

    In ``Board of Directors of Parent Corp, the parent of <issuer>`` the
    trailing ``of`` governs ``the parent``, not the board.  Binding it would
    publish a citable claim that <issuer>'s board made an appointment that
    another issuer's board actually made.
    """

    for passage in (
        "The Board of Directors of Parent Corp, the parent of Example Corp, "
        "appointed Jane Doe as chief financial officer.",
        "The Board of Directors of Holdings Inc, sole shareholder of Example "
        "Corp, appointed Jane Doe as chief financial officer.",
        "The Board of Directors of Acquirer Inc, the acquirer of Example Corp, "
        "appointed Jane Doe as chief financial officer.",
        "The Board of Directors of Target Corp, an affiliate of Example Corp, "
        "appointed Jane Doe as chief financial officer.",
    ):
        extraction = extract_evidence_fact_slots(
            evidence_passage=passage,
            event_type="chief_financial_officer_appointment",
            expected_subject="Example Corp",
        )
        assert extraction.supports_specific_fact is False, passage


def test_delisting_slots_preserve_future_modality_and_effective_date() -> None:
    passage = (
        "Nasdaq notified Example Corp that its common stock would be delisted from "
        "The Nasdaq Capital Market effective September 1, 2026."
    )

    extraction = extract_evidence_fact_slots(
        evidence_passage=passage,
        event_type="delisting",
        expected_subject="Example Corp",
    )

    assert len(extraction.facts) == 1
    fact = extraction.facts[0]
    assert fact.predicate == "DELISTING_ACTION"
    assert fact.actor_text == "Nasdaq"
    assert fact.object_text == "common stock"
    assert fact.modality == "ANNOUNCED_FUTURE"
    assert fact.modality_text == "would be"
    assert fact.effective_text == "effective September 1, 2026"

    background_only = extract_evidence_fact_slots(
        evidence_passage="The notice of delisting is attached solely as background.",
        event_type="delisting",
        expected_subject="Example Corp",
    )
    assert background_only.facts == ()

    for mismatched_notice in (
        "Example Corp announced a minimum bid price deficiency notice issued to Target Corp.",
        "Example Corp announced a notice of delisting issued to Target Corp.",
        "Example Corp announced Target Corp's notice of delisting.",
        "Example Corp announced the Target Corp minimum bid price deficiency notice.",
    ):
        mismatch = extract_evidence_fact_slots(
            evidence_passage=mismatched_notice,
            event_type=(
                "minimum_bid_price_deficiency_notice"
                if "minimum bid" in mismatched_notice
                else "delisting"
            ),
            expected_subject="Example Corp",
        )
        assert mismatch.facts
        assert mismatch.facts[0].subject_binding == "OTHER_NAMED_ENTITY"
        assert mismatch.supports_specific_fact is False


def test_merger_slots_capture_counterparty_but_reject_negated_actions() -> None:
    positive = extract_evidence_fact_slots(
        evidence_passage=(
            "Example Corp entered into a merger agreement with Target Inc. "
            "on August 20, 2026."
        ),
        event_type="merger_or_acquisition",
        expected_subject="Example Corp",
    )
    negative = extract_evidence_fact_slots(
        evidence_passage=(
            "This exhibit includes the merger agreement but does not state that it "
            "was signed or completed."
        ),
        event_type="merger_or_acquisition",
        expected_subject="Example Corp",
    )
    unrelated_completion = extract_evidence_fact_slots(
        evidence_passage="Example Corp completed due diligence concerning a merger agreement.",
        event_type="merger_or_acquisition",
        expected_subject="Example Corp",
    )

    assert positive.facts[0].predicate == "TRANSACTION_AGREEMENT_ENTERED"
    assert positive.facts[0].counterparty_text == "Target Inc."
    assert positive.facts[0].date_text == "August 20, 2026"
    assert negative.facts == ()
    assert negative.missing_slots == ("predicate", "action_text")
    assert unrelated_completion.facts == ()


def test_metadata_subject_is_not_inserted_when_passage_only_uses_a_pronoun() -> None:
    extraction = extract_evidence_fact_slots(
        evidence_passage="The company issued $10 million of common stock.",
        event_type="offering_or_dilution",
        expected_subject="Example Corp",
    )
    summary = public_fact_summary(
        subject="Example Corp",
        action_label="融资",
        stage_label="DISCLOSED",
        extraction=extraction,
    )

    assert extraction.facts[0].subject_text == "The company"
    assert extraction.facts[0].issuer_name_explicit is False
    assert extraction.limitation == "event_subject_not_bound_to_fact_actor"
    assert "Example Corp" not in summary
    assert "尚不能通过确定性规则" in summary


def test_other_company_action_is_not_misbound_to_filing_issuer() -> None:
    extraction = extract_evidence_fact_slots(
        evidence_passage=(
            "Example Corp disclosed that Customer Finance Inc. issued $25 million "
            "of convertible senior notes."
        ),
        event_type="convertible_debt_financing",
        expected_subject="Example Corp",
    )

    assert len(extraction.facts) == 1
    assert extraction.facts[0].subject_text == "Customer Finance Inc"
    assert extraction.facts[0].subject_binding == "OTHER_NAMED_ENTITY"
    assert extraction.supports_specific_fact is False
    assert "event_subject_binding" in extraction.missing_slots
    assert extraction.limitation == "event_subject_not_bound_to_fact_actor"

    mixed = extract_evidence_fact_slots(
        evidence_passage=(
            "Example Corp issued $10 million of common stock; Customer Finance Inc. "
            "issued $25 million of convertible senior notes."
        ),
        event_type="offering_or_dilution",
        expected_subject="Example Corp",
    )
    summary = public_fact_summary(
        subject="Example Corp",
        action_label="融资",
        stage_label="DISCLOSED",
        extraction=mixed,
    )
    assert len(mixed.facts) == 2
    assert len(mixed.supported_facts) == 1
    assert mixed.supported_facts[0].subject_text == "Example Corp"
    assert "Customer Finance Inc" not in summary
    assert "其他具名主体" in summary

    ambiguous_pronoun = extract_evidence_fact_slots(
        evidence_passage=(
            "Target Corp stated that the company issued $25 million of common stock."
        ),
        event_type="offering_or_dilution",
        expected_subject="Example Corp",
    )
    assert ambiguous_pronoun.facts[0].subject_text == "the company"
    assert ambiguous_pronoun.facts[0].subject_binding == "AMBIGUOUS_COMPANY_PRONOUN"
    assert ambiguous_pronoun.supports_specific_fact is False

    cross_sentence_antecedent = extract_evidence_fact_slots(
        evidence_passage=(
            "Target Corp is a customer mentioned in the filing. "
            "The company issued $25 million of common stock."
        ),
        event_type="offering_or_dilution",
        expected_subject="Example Corp",
    )
    assert cross_sentence_antecedent.facts == ()
    assert cross_sentence_antecedent.supports_specific_fact is False

    for passage, event_type in (
        (
            "Target stated that the company issued common stock in a public offering.",
            "offering_or_dilution",
        ),
        (
            "Microsoft announced that the company completed a merger.",
            "merger_or_acquisition",
        ),
        (
            "Nasdaq reported that the company received a notice of delisting.",
            "delisting",
        ),
        (
            "Microsoft stated in a press release that the company completed a merger.",
            "merger_or_acquisition",
        ),
        (
            "Microsoft stated "
            + "after reviewing extensive background materials and projections " * 5
            + "that the company completed a merger.",
            "merger_or_acquisition",
        ),
        (
            "Microsoft is the transaction sponsor. The company completed a merger.",
            "merger_or_acquisition",
        ),
    ):
        attribution_ambiguity = extract_evidence_fact_slots(
            evidence_passage=passage,
            event_type=event_type,
            expected_subject="Example Corp",
        )
        assert attribution_ambiguity.supports_specific_fact is False, passage

    document_pronoun_only = extract_evidence_fact_slots(
        evidence_passage="The company completed a merger.",
        event_type="merger_or_acquisition",
        expected_subject="Example Corp",
    )
    assert document_pronoun_only.facts
    assert document_pronoun_only.facts[0].subject_binding == "DOCUMENT_ISSUER_PRONOUN"
    assert document_pronoun_only.supports_specific_fact is False


def test_postposed_denial_removes_the_prior_machine_fact() -> None:
    next_sentence_denial = extract_evidence_fact_slots(
        evidence_passage=(
            "Example Corp issued $25 million of common stock. "
            "Example Corp later denied that it had issued common stock."
        ),
        event_type="offering_or_dilution",
        expected_subject="Example Corp",
    )
    same_sentence_rejection = extract_evidence_fact_slots(
        evidence_passage=(
            "Example Corp issued $25 million of common stock, which Example Corp "
            "later rejected as inaccurate."
        ),
        event_type="offering_or_dilution",
        expected_subject="Example Corp",
    )
    separated_denial = extract_evidence_fact_slots(
        evidence_passage=(
            "Example Corp completed a merger. "
            "The transaction was reported in the press. "
            "Example Corp denied that the merger was completed."
        ),
        event_type="merger_or_acquisition",
        expected_subject="Example Corp",
    )
    later_clarification = extract_evidence_fact_slots(
        evidence_passage=(
            "Example Corp completed a merger. The filing described the transaction. "
            "Example Corp clarified that no merger was completed."
        ),
        event_type="merger_or_acquisition",
        expected_subject="Example Corp",
    )
    direct_later_negation = extract_evidence_fact_slots(
        evidence_passage=(
            "Example Corp completed a merger. The merger did not occur."
        ),
        event_type="merger_or_acquisition",
        expected_subject="Example Corp",
    )
    separated_correction = extract_evidence_fact_slots(
        evidence_passage=(
            "Example Corp completed a merger. Example Corp issued a correction. "
            "The merger did not occur."
        ),
        event_type="merger_or_acquisition",
        expected_subject="Example Corp",
    )
    false_issuance = extract_evidence_fact_slots(
        evidence_passage=(
            "Example Corp issued common stock. The reported issuance was false."
        ),
        event_type="offering_or_dilution",
        expected_subject="Example Corp",
    )
    long_same_sentence_negation = extract_evidence_fact_slots(
        evidence_passage=(
            "Example Corp completed a merger, "
            + "after extensive descriptions of background materials and projections " * 5
            + "but the merger did not occur."
        ),
        event_type="merger_or_acquisition",
        expected_subject="Example Corp",
    )

    assert next_sentence_denial.facts == ()
    assert next_sentence_denial.supports_specific_fact is False
    assert same_sentence_rejection.facts == ()
    assert same_sentence_rejection.supports_specific_fact is False
    assert separated_denial.facts == ()
    assert separated_denial.supports_specific_fact is False
    assert later_clarification.facts == ()
    assert later_clarification.supports_specific_fact is False
    for extraction in (
        direct_later_negation,
        separated_correction,
        false_issuance,
        long_same_sentence_negation,
    ):
        assert extraction.facts == ()
        assert extraction.supports_specific_fact is False


def test_conditional_actions_never_become_machine_facts() -> None:
    for prefix in (
        "If",
        "Unless",
        "Whether",
        "Assuming that",
        "In the event that",
    ):
        extraction = extract_evidence_fact_slots(
            evidence_passage=(
                f"{prefix} Example Corp completed a merger, the parties would "
                "publish a closing notice."
            ),
            event_type="merger_or_acquisition",
            expected_subject="Example Corp",
        )
        assert extraction.facts == (), prefix
        assert extraction.supports_specific_fact is False, prefix

    long_conditional = extract_evidence_fact_slots(
        evidence_passage=(
            "If "
            + "the board reviewed extensive background materials and projections, " * 4
            + "Example Corp completed a merger, the parties would publish a notice."
        ),
        event_type="merger_or_acquisition",
        expected_subject="Example Corp",
    )
    assert long_conditional.facts == ()
    assert long_conditional.supports_specific_fact is False

    long_prior_denial = extract_evidence_fact_slots(
        evidence_passage=(
            "Example Corp denied "
            + "after extensive descriptions of background materials and projections " * 5
            + "that Example Corp completed a merger."
        ),
        event_type="merger_or_acquisition",
        expected_subject="Example Corp",
    )
    assert long_prior_denial.facts == ()
    assert long_prior_denial.supports_specific_fact is False


def test_related_family_predicates_cannot_prove_the_wrong_event_type() -> None:
    cases = (
        (
            "debt_refinancing",
            "Example Corp issued common stock in a public offering.",
        ),
        (
            "minimum_bid_price_deficiency_notice",
            "Example Corp received a notice of delisting from Nasdaq.",
        ),
        (
            "chief_financial_officer_appointment",
            "Example Corp disclosed that its chief financial officer resigned.",
        ),
        (
            "convertible_debt_financing",
            "Example Corp issued common stock in a public offering.",
        ),
        (
            "business_combination_shareholder_approval",
            "Example Corp approved a business combination.",
        ),
        (
            "business_combination_shareholder_approval",
            "Example Corp approved a business combination, but shareholders rejected it.",
        ),
        (
            "business_combination_shareholder_approval",
            "Shareholders were informed that Example Corp approved a business combination.",
        ),
        (
            "business_combination_shareholder_approval",
            "Shareholders sued after Example Corp approved a business combination.",
        ),
        (
            "credit_facility_amendment",
            "Example Corp entered into a credit facility.",
        ),
        (
            "credit_facility_expansion_extension_and_margin_reduction",
            "Example Corp extended its credit facility.",
        ),
        (
            "credit_facility_expansion_extension_and_margin_reduction",
            "Example Corp increased its credit facility.",
        ),
        (
            "credit_facility_expansion_extension_and_margin_reduction",
            "Example Corp amended its credit facility.",
        ),
        (
            "senior_unsecured_debt_financing",
            "Example Corp issued senior notes.",
        ),
        (
            "senior_unsecured_debt_financing",
            "Example Corp issued unsecured notes.",
        ),
        (
            "debt_refinancing",
            "Example Corp repaid its outstanding debt.",
        ),
        (
            "spac_sponsor_working_capital_note",
            "Example Corp advanced working capital.",
        ),
    )
    for event_type, passage in cases:
        extraction = extract_evidence_fact_slots(
            evidence_passage=passage,
            event_type=event_type,
            expected_subject="Example Corp",
        )
        assert extraction.supported_facts == (), event_type
        assert extraction.supports_specific_fact is False, event_type
        assert extraction.as_dict()["compatible_fact_count"] == 0


def test_owner_without_legal_suffix_cannot_be_reassigned_to_filing_issuer() -> None:
    cases = (
        ("offering_or_dilution", "Example Corp announced the public offering of Microsoft."),
        ("offering_or_dilution", "Example Corp announced a private placement involving NVIDIA."),
        ("delisting", "Example Corp announced Microsoft's notice of delisting."),
        ("minimum_bid_price_deficiency_notice", "Example Corp announced the Microsoft minimum bid price deficiency notice."),
        ("chief_financial_officer_appointment", "Example Corp appointed John Doe as chief financial officer of Microsoft."),
        ("management_change", "Example Corp appointed John Doe as director at NVIDIA."),
    )
    for event_type, passage in cases:
        extraction = extract_evidence_fact_slots(
            evidence_passage=passage,
            event_type=event_type,
            expected_subject="Example Corp",
        )
        assert extraction.supported_facts == (), passage
        assert extraction.supports_specific_fact is False, passage


def test_same_or_later_sentence_retraction_cannot_become_a_fact() -> None:
    cases = (
        "Example Corp completed a merger, but the merger never occurred.",
        "Example Corp issued common stock, but no issuance actually took place.",
        "Example Corp completed a merger, a claim later withdrawn by the company.",
        "Example Corp completed a merger. The merger never occurred.",
        "Example Corp completed a merger. The merger had never occurred.",
        "Example Corp completed a merger. The merger was merely proposed and never completed.",
        "Example Corp issued common stock. The issuance never happened.",
        "Example Corp issued common stock. No issuance actually took place.",
        "Example Corp issued common stock. The issuance was cancelled before closing.",
    )
    for passage in cases:
        event_type = (
            "offering_or_dilution" if "stock" in passage or "issuance" in passage else "merger_or_acquisition"
        )
        extraction = extract_evidence_fact_slots(
            evidence_passage=passage,
            event_type=event_type,
            expected_subject="Example Corp",
        )
        assert extraction.supported_facts == (), passage
        assert extraction.supports_specific_fact is False, passage


def test_allegation_or_unverified_possibility_stays_a_discovery_lead() -> None:
    cases = (
        ("merger_or_acquisition", "It was alleged that Example Corp completed a merger."),
        ("merger_or_acquisition", "The complaint alleges that Example Corp completed a merger."),
        ("merger_or_acquisition", "Plaintiffs claimed that Example Corp completed a merger."),
        ("merger_or_acquisition", "Reports suggested that Example Corp completed a merger."),
        ("merger_or_acquisition", "It is possible that Example Corp completed a merger."),
        ("merger_or_acquisition", "According to unverified reports, Example Corp completed a merger."),
        ("merger_or_acquisition", "Target Corp alleged that Example Corp completed a merger."),
        ("offering_or_dilution", "It was alleged that Example Corp issued common stock."),
        ("chief_financial_officer_appointment", "Reports suggested that Example Corp appointed Jane Doe as chief financial officer."),
        ("delisting", "It is possible that Example Corp received a notice of delisting."),
    )
    for event_type, passage in cases:
        extraction = extract_evidence_fact_slots(
            evidence_passage=passage,
            event_type=event_type,
            expected_subject="Example Corp",
        )
        assert extraction.supported_facts == (), passage
        assert extraction.supports_specific_fact is False, passage


def test_counterfactual_or_illustrative_fact_stays_a_discovery_lead() -> None:
    cases = (
        "Had Example Corp completed a merger, earnings would have increased.",
        "Suppose Example Corp completed a merger, then the ratio would change.",
        "The scenario assumes Example Corp completed a merger.",
        "On the assumption that Example Corp completed a merger, the pro forma results follow.",
        "For illustrative purposes, Example Corp completed a merger and the ratio would change.",
    )
    for passage in cases:
        extraction = extract_evidence_fact_slots(
            evidence_passage=passage,
            event_type="merger_or_acquisition",
            expected_subject="Example Corp",
        )
        assert extraction.supported_facts == (), passage
        assert extraction.supports_specific_fact is False, passage


def test_quoted_mention_is_not_a_fact_assertion() -> None:
    cases = (
        ("merger_or_acquisition", '"Example Corp completed a merger," the complaint alleged.'),
        ("offering_or_dilution", 'The keyword phrase "Example Corp issued common stock" appears in the exhibit.'),
        ("delisting", 'The headline "Example Corp received a notice of delisting" was reproduced.'),
    )
    for event_type, passage in cases:
        extraction = extract_evidence_fact_slots(
            evidence_passage=passage,
            event_type=event_type,
            expected_subject="Example Corp",
        )
        assert extraction.supported_facts == (), passage
        assert extraction.supports_specific_fact is False, passage


def test_admission_rejects_a_concrete_fact_for_a_different_event_type() -> None:
    passage = "Example Corporation issued common stock in a public offering."
    result = decision(
        action="debt_refinancing",
        evidence_passage=passage,
        subject="Example Corporation",
    )

    assert result.admitted is False
    assert "FACT_SLOT_HAS_NO_ISSUER_BOUND_FACT" in result.reasons


def test_specific_fact_gate_covers_requested_high_risk_event_families() -> None:
    for event_type in (
        "management_change",
        "delisting",
        "offering_or_dilution",
        "convertible_debt_financing",
        "merger_or_acquisition",
    ):
        assert requires_specific_fact_extraction(event_type) is True
        assert supports_deterministic_fact_extraction(event_type) is True

    # Classification families without an implemented extractor are still
    # required to provide a specific fact and therefore fail closed.
    for event_type in ("bankruptcy", "earnings_or_guidance"):
        assert requires_specific_fact_extraction(event_type) is True
        assert supports_deterministic_fact_extraction(event_type) is False

    # These composite types require relations that a loose keyword extractor
    # cannot prove safely.  They remain discovery leads for human review.
    for event_type in (
        "business_combination_shareholder_approval",
        "credit_facility_expansion_extension_and_margin_reduction",
        "spac_sponsor_working_capital_note",
    ):
        assert requires_specific_fact_extraction(event_type) is True
        assert supports_deterministic_fact_extraction(event_type) is False
