from __future__ import annotations

from scripts.audit_risk_router_input_contract import audit_rows, residual_body


def test_repeated_title_is_not_mistaken_for_body_evidence() -> None:
    assert residual_body("Example Defendant", "Example Defendant Example Defendant") == ""


def test_title_only_enforcement_rows_fail_content_contract() -> None:
    rows = [
        {
            "sample_id": f"risk-{index}",
            "source_id": "sec_litigation_external",
            "expected_label": "RISK_REVIEW",
            "title": "Example Defendant",
            "text": "Example Defendant Example Defendant",
        }
        for index in range(20)
    ]
    rows.extend(
        {
            "sample_id": f"control-{index}",
            "source_id": "ordinary_official",
            "expected_label": "NON_TARGET",
            "title": "Example product update",
            "text": "Example product update The company introduced a routine product update.",
        }
        for index in range(20)
    )
    report = audit_rows(rows)
    assert report["risk_title_only_rows"] == 20
    assert report["risk_content_ambiguous_rows"] == 20
    assert report["gates"]["minimum_rows"] is True
    assert report["gates"]["risk_body_coverage"] is False
    assert report["benchmark_contract_valid"] is False


def test_evidence_rich_risk_rows_pass_content_contract() -> None:
    rows = [
        {
            "sample_id": f"risk-{index}",
            "source_id": "official_enforcement",
            "expected_label": "RISK_REVIEW",
            "title": "Example Defendant",
            "text": (
                "Example Defendant The Commission filed a complaint alleging fraud and "
                "requested an injunction and civil penalty."
            ),
        }
        for index in range(20)
    ]
    rows.extend(
        {
            "sample_id": f"control-{index}",
            "source_id": "ordinary_official",
            "expected_label": "NON_TARGET",
            "title": "Example product update",
            "text": "Example product update The company introduced a routine product update.",
        }
        for index in range(20)
    )
    report = audit_rows(rows)
    assert report["risk_body_coverage"] == 1.0
    assert report["benchmark_contract_valid"] is True
