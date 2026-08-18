"""Human-only, dual-review workflow for the risk-label v3 pre-freeze dataset."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
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
PRINCIPAL_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
HUMAN_BLIND_CONTRACT_VERSION = "human-blind-v3.1"


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


def _as_utc(value: Any) -> datetime | None:
    raw = _clean_text(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromisoformat(raw + "T00:00:00+00:00")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _passage_sort_key(row: dict[str, Any]) -> tuple[int, float, str]:
    try:
        score = float(row.get("passage_score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    return (*_authority_rank(str(row.get("authority_tier") or ""))[:1], -score, str(row.get("evidence_id") or ""))


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
        as_of = _as_utc(event.get("first_seen_at"))
        if as_of is None:
            raise ValueError("event has no parseable first_seen_at cutoff")
        useful_evidence = []
        for row in evidence:
            passage = _clean_text(row.get("evidence_passage"))
            published_at = _as_utc(row.get("source_published_at") or row.get("filing_date"))
            received_at = _as_utc(row.get("local_received_at"))
            evidence_time = published_at or received_at
            if passage and evidence_time is not None and evidence_time <= as_of:
                useful_evidence.append(row)
        if not useful_evidence:
            raise ValueError("event has no exact evidence passage available by the event-time cutoff")
        useful_evidence.sort(key=_passage_sort_key)
        primary = useful_evidence[0]
        passages = [
            {
                "evidence_id": _clean_text(row.get("evidence_id")),
                "authority_class": _authority_class(row.get("authority_tier", "")),
                "document_type": _clean_text(row.get("form") or row.get("source_type")),
                "item_section": _clean_text(row.get("items")),
                "published_at": row.get("source_published_at") or row.get("filing_date"),
                "received_at": row.get("local_received_at"),
                "passage": _clean_text(row.get("evidence_passage")),
                "evidence_status": _clean_text(row.get("evidence_status")),
            }
            for row in useful_evidence[:8]
        ]
        facts_available_at_cutoff = (
            _as_utc(version.get("changed_at")) is not None
            and _as_utc(version.get("changed_at")) <= as_of
        )
        content = {
            "contract_version": HUMAN_BLIND_CONTRACT_VERSION,
            "as_of": as_of.isoformat(),
            "cutoff_policy": "source_published_or_received_at_lte_first_seen_at",
            "headline": _clean_text(
                primary.get("observation_title")
                or f"{event.get('company_name') or event_id} · {event.get('event_type') or 'event'}"
            ),
            "summary": _clean_text(
                (facts.get("evidence_summary") if facts_available_at_cutoff else None)
                or primary.get("observation_summary")
                or primary.get("evidence_passage")
            ),
            "confirmed_facts": (
                [
                    _clean_text(item)
                    for item in (facts.get("confirmed_facts") or [])
                    if _clean_text(item)
                ][:8]
                if facts_available_at_cutoff
                else []
            ),
            "passages": passages,
            "event_date": event.get("event_date"),
            "source_identity_hidden": True,
            "target_label_hidden": True,
            "post_event_market_data_included": False,
            "model_output_included": False,
        }
        canonical_text = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        text_sha256 = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
        sample_id = f"v3-{hashlib.sha256(f'{event_id}:{text_sha256}'.encode()).hexdigest()[:24]}"
        with closing(self.ledger.connect()) as connection:
            subject_row = connection.execute(
                """SELECT entity_id FROM event_entities
                   WHERE event_id=? AND role='SUBJECT'
                   ORDER BY confidence DESC,entity_id LIMIT 1""",
                (event_id,),
            ).fetchone()
            chain_row = connection.execute(
                """SELECT chain_id FROM event_chain_members
                   WHERE event_id=? ORDER BY chain_id LIMIT 1""",
                (event_id,),
            ).fetchone()
        entity = (
            f"issuer:{subject_row['entity_id']}"
            if subject_row is not None
            else f"provisional-issuer:{_clean_text(event.get('stable_id') or event_id)}"
        )
        chain = (
            f"chain:{chain_row['chain_id']}"
            if chain_row is not None
            else f"provisional-chain:{_clean_text(event.get('stable_id') or event_id)}"
        )
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
            "contract_version": HUMAN_BLIND_CONTRACT_VERSION,
            "issuer_group_resolved": not entity.startswith("provisional-"),
            "event_chain_group_resolved": not chain.startswith("provisional-"),
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

    def queue(
        self,
        reviewer_id: str,
        *,
        role: str = "REVIEWER",
        limit: int = 50,
        principal_alias: str | None = None,
    ) -> dict[str, Any]:
        reviewer_id = _clean_text(reviewer_id)
        if PRINCIPAL_HASH_PATTERN.fullmatch(reviewer_id) is None:
            raise ValueError("reviewer principal must be a credential-bound SHA-256 identity")
        role = role.upper()
        if role == "REVIEWER":
            statuses = {"OPEN", "IN_REVIEW"}
        elif role == "ARBITER":
            statuses = {"CONFLICT"}
        else:
            raise ValueError("role must be REVIEWER or ARBITER")
        items = []
        for sample in self.operations.adjudication_samples(statuses=statuses, limit=5000):
            if sample.get("content", {}).get("contract_version") != HUMAN_BLIND_CONTRACT_VERSION:
                continue
            reviews = self.operations.adjudication_reviews(sample["sample_id"])
            if any(row["reviewer_id"] == reviewer_id for row in reviews):
                continue
            items.append(self._masked_sample(sample, reviewer_id=reviewer_id, role=role))
            if len(items) >= max(1, min(int(limit), 200)):
                break
        return {
            "reviewer_principal": principal_alias or f"human-{reviewer_id[:10]}",
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
        if PRINCIPAL_HASH_PATTERN.fullmatch(reviewer_id) is None:
            raise ValueError("reviewer principal must be a credential-bound SHA-256 identity")
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
        contract_ineligible = [
            sample["sample_id"]
            for sample in samples
            if sample.get("content", {}).get("contract_version") != HUMAN_BLIND_CONTRACT_VERSION
        ]
        unresolved_groups = [
            sample["sample_id"]
            for sample in samples
            if sample.get("content", {}).get("contract_version") == HUMAN_BLIND_CONTRACT_VERSION
            and sample.get("status") in {"READY", "FROZEN"}
            and (
                str(sample.get("entity_group") or "").startswith("provisional-")
                or str(sample.get("event_chain_group") or "").startswith("provisional-")
            )
        ]
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
            and not unresolved_groups
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
            "contract_version": HUMAN_BLIND_CONTRACT_VERSION,
            "contract_ineligible_samples": contract_ineligible,
            "unresolved_group_samples": unresolved_groups,
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

    @staticmethod
    def _near_duplicate_key(annotation: dict[str, Any]) -> str:
        content = annotation.get("content") or {}
        text = " ".join(
            [
                _clean_text(content.get("headline")),
                _clean_text(content.get("summary")),
                *[
                    _clean_text(item.get("passage"))
                    for item in (content.get("passages") or [])
                    if isinstance(item, dict)
                ],
            ]
        ).casefold()
        normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text)
        tokens = normalized.split()
        return hashlib.sha256(" ".join(tokens[:240]).encode("utf-8")).hexdigest()

    def build_freeze_candidate(
        self,
        *,
        minimums: dict[str, int] | None = None,
        minimum_source_groups: int = 4,
        excluded_text_sha256: set[str] | None = None,
        excluded_near_duplicate_keys: set[str] | None = None,
    ) -> dict[str, Any]:
        """Build a deterministic blind set without mutating samples or invoking a model."""

        minimums = minimums or DEFAULT_MINIMUMS
        report = self.pre_freeze_report(
            minimums=minimums,
            minimum_source_groups=minimum_source_groups,
        )
        if report["status"] != "READY_FOR_OVERLAP_AUDIT":
            raise ValueError("human annotations are not ready for overlap audit")
        excluded_exact = {str(item).lower() for item in (excluded_text_sha256 or set())}
        excluded_near = set(excluded_near_duplicate_keys or set())
        candidates = [
            row
            for row in report["annotations"]
            if str(row.get("text_sha256") or "").lower() not in excluded_exact
            and self._near_duplicate_key(row) not in excluded_near
        ]
        selected: list[dict[str, Any]] = []
        used_entities: set[str] = set()
        used_chains: set[str] = set()
        used_text: set[str] = set()
        used_near: set[str] = set()
        for label, target in minimums.items():
            label_rows = sorted(
                (row for row in candidates if row.get("label") == label),
                key=lambda row: (
                    str(row.get("source_id") or ""),
                    str((row.get("content") or {}).get("event_date") or ""),
                    str(row.get("sample_id") or ""),
                ),
            )
            source_buckets: dict[str, list[dict[str, Any]]] = {}
            for row in label_rows:
                source_buckets.setdefault(str(row.get("source_id") or "unknown"), []).append(row)
            sources = sorted(source_buckets)
            while len([row for row in selected if row.get("label") == label]) < int(target):
                progressed = False
                for source in sources:
                    bucket = source_buckets[source]
                    while bucket:
                        row = bucket.pop(0)
                        entity = str(row.get("entity_group") or "")
                        chain = str(row.get("event_chain_group") or "")
                        text_hash = str(row.get("text_sha256") or "").lower()
                        near = self._near_duplicate_key(row)
                        if (
                            entity in used_entities
                            or chain in used_chains
                            or text_hash in used_text
                            or near in used_near
                        ):
                            continue
                        selected.append(row)
                        used_entities.add(entity)
                        used_chains.add(chain)
                        used_text.add(text_hash)
                        used_near.add(near)
                        progressed = True
                        break
                    if len([row for row in selected if row.get("label") == label]) >= int(target):
                        break
                if not progressed:
                    have = len([row for row in selected if row.get("label") == label])
                    raise ValueError(
                        f"insufficient zero-overlap {label} annotations after grouping: {have}/{target}"
                    )
        source_groups = sorted({str(row.get("source_id") or "") for row in selected})
        if len(source_groups) < minimum_source_groups:
            raise ValueError("selected blind set does not meet the source-family minimum")
        frozen_rows = [{**row, "split": "HUMAN_BLIND_V3"} for row in selected]
        dataset_bytes = "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for row in frozen_rows
        ).encode("utf-8")
        dataset_sha256 = hashlib.sha256(dataset_bytes).hexdigest()
        return {
            "schema_version": 1,
            "freeze_id": f"human-blind-v3-{dataset_sha256[:12]}",
            "dataset_sha256": dataset_sha256,
            "rows": frozen_rows,
            "row_count": len(frozen_rows),
            "label_counts": dict(sorted(Counter(row["label"] for row in frozen_rows).items())),
            "source_groups": source_groups,
            "entity_overlap_count": len(frozen_rows) - len(used_entities),
            "event_chain_overlap_count": len(frozen_rows) - len(used_chains),
            "exact_text_overlap_count": len(frozen_rows) - len(used_text),
            "near_duplicate_overlap_count": len(frozen_rows) - len(used_near),
            "model_predictions_included": False,
            "post_event_market_data_included": False,
            "production_changed": False,
            "no_trading": True,
        }
