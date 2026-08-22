"""Versioned financial knowledge search and traceable deterministic calculators."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from app.models.event_playbook import PlaybookCard, cards_for_family, load_playbook


KNOWLEDGE_SCHEMA_VERSION = 1


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _as_decimal(value: Any, *, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return result


def _amount(item: dict[str, Any], *, field: str) -> tuple[Decimal, str]:
    if not isinstance(item, dict):
        raise ValueError(f"{field} must carry value and source_ref")
    source_ref = str(item.get("source_ref") or "").strip()
    if not source_ref:
        raise ValueError(f"{field}.source_ref is required")
    return _as_decimal(item.get("value"), field=field), source_ref


def _number(value: Decimal) -> str:
    return format(value.normalize(), "f")


def fully_diluted_share_count(
    *,
    common_outstanding: dict[str, Any],
    convertible_shares: Iterable[dict[str, Any]] = (),
    warrant_shares: Iterable[dict[str, Any]] = (),
    option_and_award_shares: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Add disclosed share-equivalent amounts without inventing conversion terms."""

    buckets = {
        "common_outstanding": [common_outstanding],
        "convertible_shares": list(convertible_shares),
        "warrant_shares": list(warrant_shares),
        "option_and_award_shares": list(option_and_award_shares),
    }
    subtotals: dict[str, Decimal] = {}
    sources: list[dict[str, str]] = []
    for bucket, items in buckets.items():
        subtotal = Decimal("0")
        for index, item in enumerate(items):
            value, source_ref = _amount(item, field=f"{bucket}[{index}]")
            subtotal += value
            sources.append({"bucket": bucket, "value": _number(value), "source_ref": source_ref})
        subtotals[bucket] = subtotal
    total = sum(subtotals.values(), Decimal("0"))
    return {
        "calculator": "fully_diluted_share_count_v1",
        "result": _number(total),
        "unit": "shares",
        "subtotals": {key: _number(value) for key, value in subtotals.items()},
        "inputs": sources,
        "assumptions": ["Only explicitly supplied share-equivalents are included."],
    }


def cash_runway_months(
    *,
    cash_and_equivalents: dict[str, Any],
    restricted_cash: dict[str, Any],
    monthly_operating_burn: dict[str, Any],
) -> dict[str, Any]:
    cash, cash_ref = _amount(cash_and_equivalents, field="cash_and_equivalents")
    restricted, restricted_ref = _amount(restricted_cash, field="restricted_cash")
    burn, burn_ref = _amount(monthly_operating_burn, field="monthly_operating_burn")
    if restricted > cash:
        raise ValueError("restricted_cash cannot exceed cash_and_equivalents")
    if burn == 0:
        raise ValueError("monthly_operating_burn must be greater than zero")
    usable = cash - restricted
    runway = usable / burn
    return {
        "calculator": "cash_runway_months_v1",
        "result": _number(runway.quantize(Decimal("0.01"))),
        "unit": "months",
        "usable_cash": _number(usable),
        "inputs": [
            {"field": "cash_and_equivalents", "value": _number(cash), "source_ref": cash_ref},
            {"field": "restricted_cash", "value": _number(restricted), "source_ref": restricted_ref},
            {"field": "monthly_operating_burn", "value": _number(burn), "source_ref": burn_ref},
        ],
        "assumptions": ["Monthly burn remains constant.", "No future financing or asset sale is assumed."],
    }


def financing_dilution(
    *, existing_common: dict[str, Any], new_share_equivalents: dict[str, Any]
) -> dict[str, Any]:
    existing, existing_ref = _amount(existing_common, field="existing_common")
    new, new_ref = _amount(new_share_equivalents, field="new_share_equivalents")
    total = existing + new
    if total == 0:
        raise ValueError("post-financing share count must be greater than zero")
    new_pct = (new / total * Decimal("100")).quantize(Decimal("0.01"))
    retained_pct = (existing / total * Decimal("100")).quantize(Decimal("0.01"))
    return {
        "calculator": "financing_dilution_v1",
        "post_financing_shares": _number(total),
        "new_share_equivalent_pct": _number(new_pct),
        "existing_holder_retained_pct": _number(retained_pct),
        "unit": "percent_of_post_financing_shares",
        "inputs": [
            {"field": "existing_common", "value": _number(existing), "source_ref": existing_ref},
            {"field": "new_share_equivalents", "value": _number(new), "source_ref": new_ref},
        ],
        "assumptions": ["Every supplied share-equivalent is treated as issued or converted."],
    }


@dataclass(frozen=True)
class FinancialKnowledgeIndex:
    path: Path

    def rebuild(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_meta(
                    key TEXT PRIMARY KEY,value TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_cards USING fts5(
                    card_id UNINDEXED,event_family,kind UNINDEXED,title,body,
                    tokenize='unicode61'
                );
                DELETE FROM knowledge_cards;
                """
            )
            for card in load_playbook():
                connection.execute(
                    "INSERT INTO knowledge_cards(card_id,event_family,kind,title,body) VALUES (?,?,?,?,?)",
                    (
                        card.id,
                        card.event_family,
                        card.kind,
                        card.title,
                        " ".join(
                            [
                                *card.authoritative_sources,
                                *card.required_language,
                                *card.insufficient_when,
                                *card.impostors,
                                card.reader_note,
                            ]
                        ),
                    ),
                )
            connection.execute(
                "INSERT OR REPLACE INTO knowledge_meta(key,value) VALUES ('schema_version',?)",
                (str(KNOWLEDGE_SCHEMA_VERSION),),
            )
            connection.commit()
        return len(load_playbook())

    def search(self, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        tokens = re.findall(r"[\w\u3400-\u9fff]+", str(query), flags=re.UNICODE)
        if not tokens:
            return []
        expression = " OR ".join(f'"{token}"' for token in tokens[:12])
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT card_id,event_family,kind,title,
                          snippet(knowledge_cards,4,'[',']','…',16) AS snippet,
                          bm25(knowledge_cards) AS rank
                   FROM knowledge_cards WHERE knowledge_cards MATCH ?
                   ORDER BY rank,card_id LIMIT ?""",
                (expression, max(1, min(int(limit), 50))),
            ).fetchall()
        return [dict(row) for row in rows]


def knowledge_context(event_family: str, event_type: str | None = None) -> dict[str, Any]:
    cards = cards_for_family(event_family, event_type)
    confirm = next((card for card in cards if card.kind == "confirm"), None)
    impostor = next((card for card in cards if card.kind == "impostor"), None)
    return {
        "schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "event_family": event_family,
        "event_type": event_type,
        "covered": bool(cards),
        "why_it_matters": confirm.reader_note if confirm else "尚无对应的专业规则卡。",
        "facts_to_confirm": list(confirm.required_language) if confirm else [],
        "authoritative_sources": list(confirm.authoritative_sources) if confirm else [],
        "still_missing_when": list(confirm.insufficient_when) if confirm else [],
        "what_would_change_the_view": list(impostor.impostors) if impostor else [],
        "cards": [card.as_dict() for card in cards],
        "formal_event_state_mutated": False,
        "no_trading": True,
    }
