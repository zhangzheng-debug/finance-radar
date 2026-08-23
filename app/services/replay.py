from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.evidence_policy import is_primary_authority_tier
from app.models import RiskRouter, derive_evidence_context
from app.storage.operations import OperationsRepository


class ReplayCaseNotFound(KeyError):
    pass


class ReplayService:
    """Deterministic event-clock replay using the production shadow router."""

    def __init__(
        self,
        replay_dir: str | Path,
        router: RiskRouter,
        operations: OperationsRepository,
    ):
        self.replay_dir = Path(replay_dir)
        self.router = router
        self.operations = operations

    def cases(self) -> list[dict[str, Any]]:
        cases: list[dict[str, Any]] = []
        for path in sorted(self.replay_dir.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            items = payload.get("cases", [payload])
            for item in items:
                case = dict(item)
                case["fixture"] = path.name
                case["observation_count"] = len(case.get("observations", []))
                cases.append(case)
        return cases

    def get_case(self, case_id: str) -> dict[str, Any]:
        for case in self.cases():
            if case["case_id"] == case_id:
                return case
        raise ReplayCaseNotFound(case_id)

    @staticmethod
    def _router_evidence(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Adapt frozen teaching observations to the production evidence contract."""
        evidence: list[dict[str, Any]] = []
        for observation in observations:
            authority_tier = str(observation.get("authority_tier") or "P3")
            passage = " ".join(str(observation.get("passage") or "").split())
            is_primary = is_primary_authority_tier(authority_tier)
            if bool(observation.get("contradicts")) and is_primary:
                evidence_status = "contradicted_by_primary"
            elif passage and is_primary:
                # Replay cases are frozen, curated teaching fixtures rather
                # than live machine extraction.  Their quoted P0/P1 passages
                # therefore use the reviewed-primary branch of the same
                # derive_evidence_context contract as the API.
                evidence_status = "confirmed_primary"
            elif passage:
                evidence_status = "candidate_passage"
            else:
                evidence_status = ""
            evidence.append(
                {
                    "evidence_status": evidence_status,
                    "source_id": observation.get("source"),
                    "authority_tier": authority_tier,
                    "evidence_passage": passage,
                }
            )
        return evidence

    def run(self, case_id: str) -> dict[str, Any]:
        case = self.get_case(case_id)
        run_id = self.operations.create_replay_run(case_id)
        steps: list[dict[str, Any]] = []
        observed: list[dict[str, Any]] = []
        last_model: dict[str, Any] | None = None
        try:
            for observation in sorted(case["observations"], key=lambda item: item["at_seconds"]):
                observed.append(observation)
                combined_text = "\n".join(
                    f"{item.get('title','')}\n{item.get('passage','')}" for item in observed
                )
                evidence_context = derive_evidence_context(
                    self._router_evidence(observed)
                )
                model = self.router.predict(
                    combined_text,
                    evidence_context=evidence_context,
                )
                evidence_state = str(evidence_context.get("state") or "INSUFFICIENT")
                primary_supported = evidence_state.startswith("PRIMARY_SUPPORTED")
                if evidence_state == "CONFLICTED":
                    final_label = "ABSTAIN"
                    reason = "evidence_conflict_cap"
                elif model["label"] == "RISK_REVIEW" and not primary_supported:
                    final_label = "ABSTAIN"
                    reason = "evidence_gate_hold_pending_primary"
                else:
                    final_label = model["label"]
                    reason = "shadow_router_after_evidence_gate"
                decision = {
                    **model,
                    "label": final_label,
                    "model_label": model["label"],
                    "decision_reason": reason,
                    "alert_eligible": bool(
                        final_label == "RISK_REVIEW"
                        and primary_supported
                        and evidence_state != "CONFLICTED"
                    ),
                }
                steps.append(
                    {
                        "simulated_at_seconds": observation["at_seconds"],
                        "observation": observation,
                        "evidence_state": evidence_context,
                        "shadow_decision": decision,
                    }
                )
                last_model = decision
            result = {
                "run_id": run_id,
                "case_id": case_id,
                "title": case["title"],
                "expected_label": case.get("expected_label"),
                "final_label": last_model["label"] if last_model else "ABSTAIN",
                "expectation_met": bool(last_model and last_model["label"] == case.get("expected_label")),
                "steps": steps,
                "same_downstream_router": "app.models.risk_router.RiskRouter.predict",
                "simulated_clock": True,
                "external_network_used": False,
                "no_trading": True,
            }
            self.operations.finish_replay_run(run_id, result, last_model["model_version"] if last_model else None)
            self.operations.record_model_run(
                None,
                last_model
                or self.router.predict(
                    "",
                    evidence_context=derive_evidence_context([]),
                ),
            )
            return result
        except Exception as exc:
            self.operations.fail_replay_run(run_id, f"{type(exc).__name__}: {exc}")
            raise

    def reset(self, case_id: str) -> int:
        self.get_case(case_id)
        return self.operations.reset_replays(case_id)
