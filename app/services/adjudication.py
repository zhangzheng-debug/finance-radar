"""Human-only, dual-review workflow for the risk-label v3 pre-freeze dataset."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any

from app.models.risk_label_contract import (
    EVIDENCE_STATES,
    MATERIALITY,
    POLARITIES,
    build_dual_review_annotation,
    validate_annotation,
)
from app.storage import LedgerRepository, OperationsRepository


DEFAULT_MINIMUMS = {"RISK_REVIEW": 30, "NON_TARGET": 30, "ABSTAIN": 20}


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _authority_rank(value: str) -> tuple[int, str]:
    match = re.match(r"P(\d+)", str(value or "").upper())
    return (int(match.group(1)) if match else 99, str(value or ""))


def _authority_class(value: str) -> str:
    tier = str(value or "").upper()
    if tier.startswith("P0"):
        return "PRIMARY_OFFICIAL"
    if tier.startswith("P1"):
        return "ISSUER_OFFICIAL"
    return "DISCOVERY_OR_CONTEXT"


class AdjudicationService:
    """Create source-masked samples and derive labels only after human review."""

    def __init__(self, ledger: LedgerRepository, operations: OperationsRepository):
        self.ledger = ledger
        self.operations = operations

    def create_sample_from_event(self, event_id: str) -> dict[str, Any]:
        detail = self.ledger.event_detail(event_id)
        if detail is None:
            raise KeyError(event_id)
        event = detail["event"]
        evidence = self.ledger.event_evidence(event_id)
        version = detail.get("current_version") or {}
        facts = version.get("facts") or {}
        useful_evidence = [
            row for row in evidence if _clean_text(row.get("evidence_passage"))
        ]
        if not useful_evidence:
            raise ValueError("event has no exact evidence passage")
        primary = min(useful_evidence, key=lambda row: _authority_rank(row.get("authority_tier", "")))
        passages = [
            {
                "authority_class": _authority_class(row.get("authority_tier", "")),
                "document_type": _clean_text(row.get("form") or row.get("source_type")),
                "item_section": _clean_text(row.get("items")),
                "published_at": row.get("source_published_at") or row.get("filing_date"),
                "passage": _clean_text(row.get("evidence_passage")),
                "evidence_status": _clean_text(row.get("evidence_status")),
            }
            for row in useful_evidence[:8]
        ]
        content = {
            "headline": _clean_text(
                primary.get("observation_title")
                or f"{event.get('company_name') or event_id} · {event.get('event_type') or 'event'}"
            ),
            "summary": _clean_text(
                facts.get("evidence_summary")
                or primary.get("observation_summary")
                or primary.get("evidence_passage")
            ),
            "confirmed_facts": [
                _clean_text(item) for item in (facts.get("confirmed_facts") or []) if _clean_text(item)
            ][:8],
            "passages": passages,
            "event_date": event.get("event_date"),
            "source_identity_hidden": True,
            "target_label_hidden": True,
            "post_event_market_data_included": False,
        }
        canonical_text = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        text_sha256 = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
        sample_id = f"v3-{hashlib.sha256(f'{event_id}:{text_sha256}'.encode()).hexdigest()[:24]}"
        entity = _clean_text(event.get("ticker_at_event") or event.get("company_name") or event_id)
        chain = _clean_text(event.get("stable_id") or event_id)
        sample = {
            "sample_id": sample_id,
            "event_id": event_id,
            "text_sha256": text_sha256,
            "content": content,
            "source_id": str(primary.get("source_id") or "unknown"),
            "authority_tier": str(primary.get("authority_tier") or "P2"),
            "entity_group": entity,
            "event_chain_group": chain,
        }
        stored_id, created = self.operations.create_adjudication_sample(sample)
        return {
            "sample_id": stored_id,
            "event_id": event_id,
            "created": created,
            "text_sha256": text_sha256,
            "status": "OPEN" if created else self.operations.adjudication_sample(stored_id)["status"],
            "source_identity_hidden_from_reviewers": True,
            "target_label_preassigned": False,
        }

    def _masked_sample(
        self,
        sample: dict[str, Any],
        *,
        reviewer_id: str,
        role: str,
    ) -> dict[str, Any]:
        reviews = self.operations.adjudication_reviews(sample["sample_id"])
        own = next((row for row in reviews if row["reviewer_id"] == reviewer_id), None)
        item = {
            "sample_id": sample["sample_id"],
            "event_id": sample["event_id"],
            "text_sha256": sample["text_sha256"],
            "status": sample["status"],
            "content": sample["content"],
            "source_token": "src-" + hashlib.sha256(sample["source_id"].encode()).hexdigest()[:10],
            "authority_context": _authority_class(sample["authority_tier"]),
            "review_count": sum(row["review_role"] == "REVIEWER" for row in reviews),
            "arbitration_count": sum(row["review_role"] == "ARBITER" for row in reviews),
            "peer_answers_hidden": role == "REVIEWER",
            "own_submission": own,
            "no_model_prediction_shown": True,
            "no_market_outcome_shown": True,
        }
        if role == "ARBITER":
            item["conflict_options"] = [
                {
                    "materiality": row["materiality"],
                    "polarity": row["polarity"],
                    "evidence_state": row["evidence_state"],
                    "rationale": row["rationale"],
                }
                for row in reviews
                if row["review_role"] == "REVIEWER"
            ]
        return item

    def queue(self, reviewer_id: str, *, role: str = "REVIEWER", limit: int = 50) -> dict[str, Any]:
        reviewer_id = _clean_text(reviewer_id)
        if len(reviewer_id) < 2:
            raise ValueError("reviewer_id must contain at least two characters")
        role = role.upper()
        if role == "REVIEWER":
            statuses = {"OPEN", "IN_REVIEW"}
        elif role == "ARBITER":
            statuses = {"CONFLICT"}
        else:
            raise ValueError("role must be REVIEWER or ARBITER")
        items = []
        for sample in self.operations.adjudication_samples(statuses=statuses, limit=5000):
            reviews = self.operations.adjudication_reviews(sample["sample_id"])
            if any(row["reviewer_id"] == reviewer_id for row in reviews):
                continue
            items.append(self._masked_sample(sample, reviewer_id=reviewer_id, role=role))
            if len(items) >= max(1, min(int(limit), 200)):
                break
        return {
            "reviewer_id": reviewer_id,
            "role": role,
            "items": items,
            "peer_answers_hidden": role == "REVIEWER",
            "target_label_is_derived_after_review": True,
            "source_used_as_label": False,
        }

    def submit_review(
        self,
        sample_id: str,
        *,
        reviewer_id: str,
        role: str,
        materiality: str,
        polarity: str,
        evidence_state: str,
        rationale: str,
    ) -> dict[str, Any]:
        values = {
            "materiality": materiality.upper(),
            "polarity": polarity.upper(),
            "evidence_state": evidence_state.upper(),
        }
        if values["materiality"] not in MATERIALITY:
            raise ValueError("invalid materiality")
        if values["polarity"] not in POLARITIES:
            raise ValueError("invalid polarity")
        if values["evidence_state"] not in EVIDENCE_STATES:
            raise ValueError("invalid evidence_state")
        reviewer_id = _clean_text(reviewer_id)
        rationale = _clean_text(rationale)
        if len(reviewer_id) < 2:
            raise ValueError("reviewer_id must contain at least two characters")
        if len(rationale) < 20:
            raise ValueError("rationale must contain at least 20 characters")
        result = self.operations.record_adjudication_review(
            sample_id,
            reviewer_id=reviewer_id,
            review_role=role,
            rationale=rationale,
            **values,
        )
        result.update(
            {
                "target_label_submitted": False,
                "source_used_as_label": False,
                "peer_answers_were_hidden": role.upper() == "REVIEWER",
            }
        )
        if result["status"] == "READY":
            annotation = self.annotation(sample_id)
            result["derived_label"] = annotation["label"]
            result["resolution"] = annotation["resolution"]
        return result

    def annotation(self, sample_id: str) -> dict[str, Any]:
        sample = self.operations.adjudication_sample(sample_id)
        if sample is None:
            raise KeyError(sample_id)
        if sample["status"] not in {"READY", "FROZEN"}:
            raise ValueError("sample is not ready for annotation export")
        return build_dual_review_annotation(
            sample, self.operations.adjudication_reviews(sample_id)
        )

    def pre_freeze_report(
        self,
        *,
        minimums: dict[str, int] | None = None,
        minimum_source_groups: int = 4,
    ) -> dict[str, Any]:
        minimums = minimums or DEFAULT_MINIMUMS
        samples = self.operations.adjudication_samples(limit=5000)
        status_counts = Counter(sample["status"] for sample in samples)
        annotations: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        for sample in samples:
            if sample["status"] not in {"READY", "FROZEN"}:
                continue
            try:
                row = self.annotation(sample["sample_id"])
                issues = validate_annotation(row)
            except (KeyError, ValueError) as exc:
                invalid.append({"sample_id": sample["sample_id"], "issues": [str(exc)]})
                continue
            if issues:
                invalid.append({"sample_id": sample["sample_id"], "issues": issues})
            else:
                annotations.append(row)
        labels = Counter(row["label"] for row in annotations)
        source_groups = {row["source_id"] for row in annotations}
        deficits = {
            label: max(0, int(required) - labels.get(label, 0))
            for label, required in minimums.items()
        }
        ready = (
            bool(annotations)
            and not invalid
            and all(value == 0 for value in deficits.values())
            and len(source_groups) >= minimum_source_groups
        )
        return {
            "schema_version": 1,
            "status": "READY_FOR_OVERLAP_AUDIT" if ready else "NOT_READY_FOR_FREEZE",
            "samples": len(samples),
            "status_counts": dict(sorted(status_counts.items())),
            "valid_annotations": len(annotations),
            "invalid_annotations": invalid,
            "label_counts": dict(sorted(labels.items())),
            "label_minimums": minimums,
            "label_deficits": deficits,
            "source_groups": len(source_groups),
            "minimum_source_groups": minimum_source_groups,
            "annotations": annotations,
            "split": "UNASSIGNED",
            "production_changed": False,
            "blind_v2_frozen": False,
            "next_gate": "deduplicate against v1 training/blind text, then group split and freeze",
            "reviewer_inputs_target_label": False,
            "peer_answers_hidden_during_independent_review": True,
            "model_and_market_outcomes_hidden": True,
            "source_used_as_label": False,
            "public_review_ui_default_closed": True,
            "no_trading": True,
        }
