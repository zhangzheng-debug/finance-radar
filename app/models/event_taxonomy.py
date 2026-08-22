"""One deterministic taxonomy shared by collectors, knowledge cards and audits."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
TAXONOMY_PATH = ROOT / "config" / "event_taxonomy_v1.json"


@dataclass(frozen=True)
class TaxonomyMatch:
    taxonomy_version: str
    category: str
    rule_id: str | None
    time_anchor: str | None
    playbook_family: str | None
    fact_event: bool
    input_family: str
    input_type: str

    @property
    def mapped(self) -> bool:
        return self.category != "OTHER_UNMAPPED"


@lru_cache(maxsize=1)
def load_taxonomy(path: str | None = None) -> dict[str, Any]:
    source = Path(path) if path else TAXONOMY_PATH
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported event taxonomy schema")
    categories = payload.get("categories")
    rules = payload.get("rules")
    if not isinstance(categories, dict) or "OTHER_UNMAPPED" not in categories:
        raise ValueError("event taxonomy categories are invalid")
    if not isinstance(rules, list) or not rules:
        raise ValueError("event taxonomy contains no rules")
    seen: set[str] = set()
    prior_priority = -1
    for rule in rules:
        rule_id = str(rule.get("id") or "")
        priority = int(rule.get("priority"))
        category = str(rule.get("category") or "")
        if not rule_id or rule_id in seen:
            raise ValueError(f"invalid or duplicate taxonomy rule id: {rule_id}")
        if priority <= prior_priority:
            raise ValueError("taxonomy rules must have strictly increasing priority")
        if category not in categories:
            raise ValueError(f"taxonomy rule {rule_id} references unknown category")
        re.compile(str(rule.get("pattern") or ""))
        seen.add(rule_id)
        prior_priority = priority
    return payload


def classify_event(event_family: Any, event_type: Any = None) -> TaxonomyMatch:
    payload = load_taxonomy()
    family = str(event_family or "").strip()
    event_type_text = str(event_type or "").strip()
    combined = re.sub(
        r"[^a-z0-9]+", "_", f"{family} {event_type_text}".casefold()
    ).strip("_")
    selected: dict[str, Any] | None = None
    for rule in payload["rules"]:
        if re.search(str(rule["pattern"]), combined):
            selected = rule
            break
    category = str(
        selected["category"] if selected is not None else payload["default_category"]
    )
    definition = payload["categories"][category]
    return TaxonomyMatch(
        taxonomy_version=str(payload["taxonomy_version"]),
        category=category,
        rule_id=str(selected["id"]) if selected is not None else None,
        time_anchor=definition.get("time_anchor"),
        playbook_family=definition.get("playbook_family"),
        fact_event=bool(definition.get("fact_event")),
        input_family=family,
        input_type=event_type_text,
    )


def taxonomy_coverage(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items = [dict(row) for row in rows]
    by_category: dict[str, int] = {}
    unmapped: list[dict[str, str]] = []
    for row in items:
        match = classify_event(row.get("event_family"), row.get("event_type"))
        by_category[match.category] = by_category.get(match.category, 0) + 1
        if not match.mapped:
            unmapped.append(
                {
                    "event_id": str(row.get("event_id") or ""),
                    "event_family": match.input_family,
                    "event_type": match.input_type,
                }
            )
    mapped = len(items) - len(unmapped)
    return {
        "taxonomy_version": load_taxonomy()["taxonomy_version"],
        "total": len(items),
        "mapped": mapped,
        "unmapped": len(unmapped),
        "coverage_pct": round(100.0 * mapped / len(items), 2) if items else 100.0,
        "by_category": dict(sorted(by_category.items())),
        "unmapped_examples": unmapped[:100],
    }
