from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.models import RiskRouter
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
    def _evidence_state(observations: list[dict[str, Any]]) -> dict[str, Any]:
        tiers = [item.get("authority_tier", "P3") for item in observations]
        has_primary = "P0" in tiers
        has_conflict = any(bool(item.get("contradicts")) for item in observations)
        passage_count = sum(bool(item.get("passage")) for item in observations)
        if has_conflict:
            status = "CONFLICT_REVIEW"
        elif has_primary and passage_count:
            status = "PRIMARY_SUPPORTED"
        elif passage_count:
            status = "DISCOVERY_ONLY"
        else:
            status = "INSUFFICIENT"
        return {
            "status": status,
            "has_primary": has_primary,
            "has_conflict": has_conflict,
            "passage_count": passage_count,
            "authority_tiers": sorted(set(tiers)),
        }

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
                model = self.router.predict(combined_text)
                evidence = self._evidence_state(observed)
                if evidence["has_conflict"]:
                    final_label = "ABSTAIN"
                    reason = "evidence_conflict_cap"
                elif model["label"] == "RISK_REVIEW" and not evidence["has_primary"]:
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
                        final_label == "RISK_REVIEW" and evidence["has_primary"] and not evidence["has_conflict"]
                    ),
                }
                steps.append(
                    {
                        "simulated_at_seconds": observation["at_seconds"],
                        "observation": observation,
                        "evidence_state": evidence,
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
            self.operations.record_model_run(None, last_model or self.router.predict(""))
            return result
        except Exception as exc:
            self.operations.fail_replay_run(run_id, f"{type(exc).__name__}: {exc}")
            raise

    def reset(self, case_id: str) -> int:
        self.get_case(case_id)
        return self.operations.reset_replays(case_id)
