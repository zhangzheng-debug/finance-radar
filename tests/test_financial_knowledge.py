from __future__ import annotations

from pathlib import Path

import pytest

from app.services.financial_knowledge import (
    FinancialKnowledgeIndex,
    cash_runway_months,
    financing_dilution,
    fully_diluted_share_count,
    knowledge_context,
)


def ref(value: str, source: str) -> dict[str, str]:
    return {"value": value, "source_ref": source}


def test_knowledge_covers_dilution_with_confirmation_and_counterexamples() -> None:
    context = knowledge_context("equity_dilution", "atm_offering")
    assert context["covered"] is True
    assert len(context["cards"]) == 2
    assert context["facts_to_confirm"]
    assert context["what_would_change_the_view"]
    assert context["formal_event_state_mutated"] is False


def test_fts_index_searches_versioned_cards(tmp_path: Path) -> None:
    index = FinancialKnowledgeIndex(tmp_path / "knowledge.sqlite3")
    assert index.rebuild() == 24
    results = index.search("现金 跑道")
    assert results
    assert any(row["event_family"] == "liquidity_and_credit" for row in results)


def test_calculators_are_traceable_and_reproducible() -> None:
    diluted = fully_diluted_share_count(
        common_outstanding=ref("100", "10-Q cover"),
        convertible_shares=[ref("20", "note 7")],
        warrant_shares=[ref("10", "exhibit 4.1")],
    )
    assert diluted["result"] == "130"
    assert {item["source_ref"] for item in diluted["inputs"]} == {
        "10-Q cover",
        "note 7",
        "exhibit 4.1",
    }

    runway = cash_runway_months(
        cash_and_equivalents=ref("12", "balance sheet"),
        restricted_cash=ref("2", "note 2"),
        monthly_operating_burn=ref("2.5", "cash flow statement"),
    )
    assert runway["result"] == "4"

    dilution = financing_dilution(
        existing_common=ref("100", "cover"),
        new_share_equivalents=ref("25", "offering terms"),
    )
    assert dilution["new_share_equivalent_pct"] == "20"
    assert dilution["existing_holder_retained_pct"] == "80"


def test_calculator_refuses_untraceable_or_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="source_ref"):
        financing_dilution(existing_common={"value": 100}, new_share_equivalents=ref("1", "x"))
    with pytest.raises(ValueError, match="cannot exceed"):
        cash_runway_months(
            cash_and_equivalents=ref("1", "a"),
            restricted_cash=ref("2", "b"),
            monthly_operating_burn=ref("1", "c"),
        )
