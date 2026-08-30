from __future__ import annotations

from scripts.train_risk_router_ai_adjudicated import (
    _metrics,
    _risk_first_policy,
    _select_threshold,
    _split_consensus,
)


def _row(index: int, label: str) -> dict:
    return {
        "sample_id": f"sample-{label}-{index}",
        "consensus_label": label,
    }


def test_risk_policy_precedes_positive_or_routine_language() -> None:
    label, reason = _risk_first_policy(
        "The company announced a share repurchase but also filed a Chapter 11 petition."
    )
    assert label == "RISK_REVIEW"
    assert reason


def test_resolved_context_precedes_distress_keywords() -> None:
    label, reason = _risk_first_policy(
        "Management concluded that substantial doubt about the company's ability "
        "to continue as a going concern has been alleviated by the financing plan."
    )
    assert label == "NON_TARGET"
    assert reason == "resolved_or_alleviated_risk"


def test_whistleblower_award_is_not_misattributed_to_named_subject() -> None:
    label, reason = _risk_first_policy(
        "The CFTC grants five whistleblower awards totaling $8 million for information "
        "that contributed to an enforcement action involving a fraudulent scheme."
    )
    assert label == "NON_TARGET"
    assert reason == "whistleblower_award_not_subject_enforcement"


def test_final_regulatory_order_is_risk() -> None:
    label, reason = _risk_first_policy(
        "The FTC approves a final order requiring the company to pay $750,000 and "
        "barring deceptive and unsupported health claims."
    )
    assert label == "RISK_REVIEW"
    assert reason == "binding_enforcement_or_accounting_failure"


def test_pivotal_trial_primary_endpoint_failure_is_risk() -> None:
    label, reason = _risk_first_policy(
        "The Phase 3 trial did not meet statistical significance on its primary endpoint."
    )
    assert label == "RISK_REVIEW"
    assert reason == "pivotal_clinical_failure"


def test_form25_removal_is_risk_but_paid_exit_is_not() -> None:
    form25 = (
        "FORM 25 NOTIFICATION OF REMOVAL FROM LISTING AND/OR REGISTRATION "
        "UNDER SECTION 12(b)."
    )
    assert _risk_first_policy(form25)[0] == "RISK_REVIEW"
    assert _risk_first_policy(form25 + " The acquisition was completed for $18 per share in cash.")[0] == "NON_TARGET"


def test_adverse_internal_control_opinion_is_risk() -> None:
    label, reason = _risk_first_policy(
        "The auditor expressed an adverse opinion as a result of material weaknesses "
        "in internal control over financial reporting."
    )
    assert label == "RISK_REVIEW"
    assert reason == "adverse_control_opinion"


def test_consensus_split_protects_validation_and_holdout() -> None:
    rows = [*(_row(i, "RISK_REVIEW") for i in range(43))]
    rows += [*(_row(i, "NON_TARGET") for i in range(353))]
    rows += [*(_row(i, "ABSTAIN") for i in range(19))]
    train, validation, holdout, abstain = _split_consensus(rows, salt="test")
    assert len({row["sample_id"] for row in train + validation + holdout + abstain}) == 415
    assert not ({row["sample_id"] for row in train} & {row["sample_id"] for row in holdout})
    assert {row["consensus_label"] for row in validation} == {
        "RISK_REVIEW",
        "NON_TARGET",
    }
    assert len(abstain) == 19


def test_metrics_reports_false_risk_separately() -> None:
    result = _metrics(
        ["RISK_REVIEW", "NON_TARGET", "NON_TARGET"],
        ["RISK_REVIEW", "RISK_REVIEW", "NON_TARGET"],
    )
    assert result["risk_recall"] == 1.0
    assert result["non_target_false_risk_rate"] == 0.5


def test_threshold_selection_uses_validation_only_and_prefers_lowest_eligible() -> None:
    class Pipeline:
        classes_ = ["NON_TARGET", "RISK_REVIEW"]

        def predict_proba(self, values):
            probability = 0.9 if "risk" in values[0] else 0.1
            return [[1.0 - probability, probability]]

    rows = [
        {"text": "risk", "expected_label": "RISK_REVIEW"},
        {"text": "routine", "expected_label": "NON_TARGET"},
    ]
    threshold, policy, selected, candidates = _select_threshold(Pipeline(), rows)
    assert threshold == 0.35
    assert policy.startswith("LOWEST_VALIDATION_THRESHOLD")
    assert selected["metrics"]["risk_recall"] == 1.0
    assert len(candidates) == len(__import__("scripts.train_risk_router_ai_adjudicated", fromlist=["THRESHOLDS"]).THRESHOLDS)
