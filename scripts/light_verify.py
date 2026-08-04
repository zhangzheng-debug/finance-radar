#!/usr/bin/env python3
"""Run bounded light verification under an explicit, expiring scope contract.

The default is read-only.  ``--apply`` is intentionally a manual batch action:
it requires an authorization JSON file that binds a batch id, expiry, maximum
formal applications, and exact event ids (or an exact legacy manifest).  The
continuous worker never supplies this contract and never applies formal state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# The worker invokes this file directly from a release root.  Make the
# repository package importable in that mode as well as ``python -m`` mode.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.models import RiskRouter
from app.services.light_verification import (
    BLOCKING_ROUGH_OUTCOMES,
    LEGACY_LIGHT_VERIFICATION_VERSION,
    LIGHT_FOLLOWUP_JOB_TYPE,
    LIGHT_VERIFICATION_VERSION,
    LIGHT_VERIFIED_EVIDENCE_STATUS,
    apply_event,
    evidence_fingerprint,
    evaluate_event,
    model_delta,
    model_snapshot,
    reconcile_legacy_event,
)
from app.storage import LedgerRepository, OperationsRepository
from scripts.event_ledger import stable_json, utc_now


DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_OPERATIONS_DB = ROOT / "data" / "finance_radar_operations.sqlite3"
DEFAULT_REPORT = ROOT / "reports" / "light_verification_latest.json"
AUTHORIZATION_PHRASE = "user_explicit_light_verification"


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _rough_outcome(connection: sqlite3.Connection, event_id: str) -> str | None:
    row = connection.execute(
        """SELECT payload_json FROM pipeline_jobs
           WHERE event_id=? AND status='COMPLETED_AUTHORIZED_ROUGH_REVIEW'
           ORDER BY updated_at DESC,job_id DESC LIMIT 1""",
        (event_id,),
    ).fetchone()
    if row is None:
        return None
    rough = _json_object(row["payload_json"]).get("rough_review", {})
    return str(rough.get("outcome") or "").upper() if isinstance(rough, dict) else None


def _followup_fingerprint(connection: sqlite3.Connection, event_id: str) -> tuple[str | None, int | None]:
    row = connection.execute(
        "SELECT payload_json FROM pipeline_jobs WHERE event_id=? AND job_type=?",
        (event_id, LIGHT_FOLLOWUP_JOB_TYPE),
    ).fetchone()
    if row is None:
        return None, None
    followup = _json_object(row["payload_json"]).get("light_verification_followup", {})
    if not isinstance(followup, dict):
        return None, None
    version = followup.get("original_event_version")
    return str(followup.get("evidence_fingerprint") or "") or None, int(version) if str(version or "").isdigit() else None


def candidate_ids(
    path: Path,
    *,
    limit: int,
    event_id: str | None,
    require_rough: bool,
    allowed_event_ids: set[str] | None = None,
) -> list[str]:
    """Return changed candidates only, unless an explicit event id is requested.

    Each persisted nonterminal attempt stores its evidence fingerprint.  A
    candidate is reconsidered only when its event version or evidence changes,
    preventing an empty/insufficient early row from consuming every batch.
    """

    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        params: list[Any] = []
        where = ["e.status='candidate'"]
        if event_id:
            where.append("e.event_id=?")
            params.append(event_id)
        if allowed_event_ids is not None:
            if not allowed_event_ids:
                return []
            placeholders = ",".join("?" for _ in allowed_event_ids)
            where.append(f"e.event_id IN ({placeholders})")
            params.extend(sorted(allowed_event_ids))
        rows = connection.execute(
            f"""SELECT e.* FROM canonical_events e
                WHERE {' AND '.join(where)}
                ORDER BY e.last_updated_at DESC,e.event_id ASC""",
            params,
        ).fetchall()
        selected: list[str] = []
        for row in rows:
            current = dict(row)
            current_event_id = str(current["event_id"])
            outcome = _rough_outcome(connection, current_event_id)
            if outcome in BLOCKING_ROUGH_OUTCOMES:
                # A conflict/unresolved rough decision is never a machine apply
                # candidate, even when the caller explicitly names its id.
                continue
            if require_rough and outcome not in {"ROUGH_ACCEPTED", "ROUGH_INSUFFICIENT"}:
                continue
            evidence = [
                dict(item)
                for item in connection.execute(
                    "SELECT * FROM event_evidence WHERE event_id=? ORDER BY evidence_id",
                    (current_event_id,),
                )
            ]
            current_fingerprint = evidence_fingerprint(current, evidence)
            prior_fingerprint, prior_version = _followup_fingerprint(connection, current_event_id)
            if (
                event_id is None
                and prior_fingerprint == current_fingerprint
                and prior_version == int(current["current_version"])
            ):
                continue
            selected.append(current_event_id)
            if len(selected) >= max(1, min(int(limit), 5000)):
                break
        return selected
    finally:
        connection.close()


def _event_with_facts(ledger: LedgerRepository, event_id: str) -> dict[str, Any] | None:
    return ledger.event_detail(event_id)


def _after_event(event: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    copy = dict(event)
    if result["decision"] == "SUPPORTED":
        copy["status"] = "verified"
        copy["current_version"] = int(event.get("current_version") or 0) + 1
    # INSUFFICIENT/SKIPPED/CONFLICT intentionally leave canonical event truth
    # untouched.  The durable follow-up job represents the evidence gap.
    return copy


def _after_evidence(evidence: list[dict[str, Any]], result: dict[str, Any]) -> list[dict[str, Any]]:
    if result["decision"] != "SUPPORTED":
        return evidence
    ids = {str(item) for item in result.get("evidence_ids", [])}
    return [
        {**item, "evidence_status": LIGHT_VERIFIED_EVIDENCE_STATUS}
        if str(item.get("evidence_id")) in ids
        else item
        for item in evidence
    ]


def _parse_expiry(value: Any) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("authorization expires_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _contract_event_ids(contract: dict[str, Any]) -> set[str]:
    raw = contract.get("event_ids")
    if not isinstance(raw, list):
        raise ValueError("authorization contract must contain a non-empty event_ids list")
    values = {str(item).strip() for item in raw if str(item).strip()}
    if not values or len(values) != len(raw):
        raise ValueError("authorization contract event_ids must be non-empty and unique")
    return values


def load_scoped_authorization(
    args: argparse.Namespace,
    *,
    require_event_ids: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], set[str]]:
    """Load and validate the manual authorization contract before any mutation."""

    if not args.authorization_file:
        raise SystemExit("--apply requires --authorization-file with an expiring scoped contract")
    try:
        contract = json.loads(Path(args.authorization_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"unable to read --authorization-file: {exc}") from exc
    if not isinstance(contract, dict):
        raise SystemExit("authorization contract must be a JSON object")
    approval = str(contract.get("authorization") or contract.get("authorization_phrase") or "")
    if args.authorization != AUTHORIZATION_PHRASE or approval != AUTHORIZATION_PHRASE:
        raise SystemExit(f"--apply requires --authorization {AUTHORIZATION_PHRASE!r} and the same contract authorization")
    if not args.batch_id or str(contract.get("batch_id") or "") != str(args.batch_id):
        raise SystemExit("--apply requires --batch-id exactly matching authorization contract batch_id")
    for key in ("authorization_id", "actor", "purpose"):
        if not str(contract.get(key) or "").strip():
            raise SystemExit(f"authorization contract requires non-empty {key}")
    try:
        expires_at = _parse_expiry(contract.get("expires_at"))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if expires_at <= datetime.now(timezone.utc):
        raise SystemExit("authorization contract is expired")
    try:
        max_applies = int(contract.get("max_applies"))
    except (TypeError, ValueError) as exc:
        raise SystemExit("authorization contract max_applies must be a positive integer") from exc
    if max_applies < 1:
        raise SystemExit("authorization contract max_applies must be positive")
    event_ids = _contract_event_ids(contract) if require_event_ids else {
        str(item).strip()
        for item in contract.get("event_ids", [])
        if str(item).strip()
    }
    context = {
        "authorization_id": str(contract["authorization_id"]),
        "actor": str(contract["actor"]),
        "purpose": str(contract["purpose"]),
        "expires_at": expires_at.isoformat(),
        "batch_id": str(contract["batch_id"]),
        "max_applies": max_applies,
        "event_scope_sha256": hashlib.sha256(stable_json(sorted(event_ids)).encode("utf-8")).hexdigest(),
    }
    return contract, context, event_ids


def _validate_selected_scope(
    args: argparse.Namespace,
    contract: dict[str, Any],
    selected: list[str],
    contract_event_ids: set[str],
) -> int:
    if set(selected) != contract_event_ids:
        missing = sorted(contract_event_ids - set(selected))
        unexpected = sorted(set(selected) - contract_event_ids)
        raise SystemExit(
            "authorization scope no longer matches eligible candidates; create a fresh contract "
            f"(missing={missing[:5]}, unexpected={unexpected[:5]})"
        )
    authorized_max = int(contract["max_applies"])
    if args.max_applies < 1:
        raise SystemExit("--max-applies must be positive")
    if args.max_applies > authorized_max:
        raise SystemExit("--max-applies exceeds authorization contract max_applies")
    if len(selected) > authorized_max:
        raise SystemExit("authorization contract max_applies is smaller than its event scope")
    return min(int(args.max_applies), authorized_max)


def _formal_count_today(path: Path) -> int:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return int(
            connection.execute(
                """SELECT COUNT(*) FROM event_versions
                   WHERE change_reason='light_evidence_verification_v2' AND changed_at LIKE ?""",
                (f"{utc_now()[:10]}%",),
            ).fetchone()[0]
        )
    finally:
        connection.close()


def _legacy_records(path: Path, *, limit: int, event_id: str | None) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        where = ["(v.change_reason='light_evidence_verification_v1' OR json_extract(v.facts_json,'$.light_verification.version')=?)"]
        params: list[Any] = [LEGACY_LIGHT_VERIFICATION_VERSION]
        if event_id:
            where.append("e.event_id=?")
            params.append(event_id)
        rows = connection.execute(
            f"""SELECT e.event_id,e.current_version,e.status,v.facts_json,v.change_reason
                FROM canonical_events e
                JOIN event_versions v ON v.event_id=e.event_id AND v.version=e.current_version
                WHERE {' AND '.join(where)}
                ORDER BY e.event_id ASC LIMIT ?""",
            (*params, max(1, min(int(limit), 5000))),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _legacy_manifest(rows: list[dict[str, Any]]) -> str:
    material = [
        {"event_id": str(row["event_id"]), "current_version": int(row["current_version"]), "status": str(row["status"])}
        for row in rows
    ]
    return hashlib.sha256(stable_json(material).encode("utf-8")).hexdigest()


def _validate_legacy_scope(args: argparse.Namespace, contract: dict[str, Any], rows: list[dict[str, Any]]) -> int:
    manifest = _legacy_manifest(rows)
    if str(contract.get("legacy_manifest_sha256") or "") != manifest:
        raise SystemExit("legacy reconciliation manifest no longer matches; generate a fresh dry-run report and contract")
    if str(contract.get("event_selector") or "") != "legacy_light_v1":
        raise SystemExit("legacy authorization contract must set event_selector to legacy_light_v1")
    if len(rows) > int(contract["max_applies"]):
        raise SystemExit("legacy authorization max_applies is smaller than the exact legacy manifest")
    return min(int(args.max_applies), int(contract["max_applies"]))


def _record_nonformal_attempt(operations: OperationsRepository, result: dict[str, Any]) -> None:
    """Persist non-formal retries separately from the durable formal outbox."""

    operations.record_light_verification(result)


def _run_legacy_reconciliation(args: argparse.Namespace) -> dict[str, Any]:
    rows = _legacy_records(args.db, limit=args.limit, event_id=args.event_id)
    manifest = _legacy_manifest(rows)
    batch_id = args.batch_id or f"legacy-light-reconcile-{utc_now().replace(':', '').replace('+00:00', 'Z')}"
    context: dict[str, Any] | None = None
    authorized_max = 0
    if args.apply:
        contract, context, _ = load_scoped_authorization(args, require_event_ids=False)
        authorized_max = _validate_legacy_scope(args, contract, rows)
    reopened: list[dict[str, Any]] = []
    errors: list[str] = []
    if args.apply:
        for row in rows[:authorized_max]:
            connection = sqlite3.connect(args.db, timeout=30)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("BEGIN IMMEDIATE")
                result = reconcile_legacy_event(connection, event_id=str(row["event_id"]), batch_id=batch_id)
                connection.commit()
                reopened.append(result)
            except Exception as exc:
                connection.rollback()
                errors.append(f"{row['event_id']}:{type(exc).__name__}:{str(exc)[:300]}")
            finally:
                connection.close()
    report = {
        "version": LIGHT_VERIFICATION_VERSION,
        "mode": "legacy_reconciliation_apply" if args.apply else "legacy_reconciliation_dry_run",
        "batch_id": batch_id,
        "authorization": context,
        "legacy_manifest_sha256": manifest,
        "suggested_authorization_contract": {
            "authorization": AUTHORIZATION_PHRASE,
            "authorization_id": "replace-with-a-unique-human-approval-id",
            "actor": "replace-with-approver",
            "purpose": "reopen exact legacy v1 evidence/human follow-up without rollback",
            "batch_id": batch_id,
            "expires_at": "replace-with-an-expiring-ISO-8601-time",
            "max_applies": len(rows),
            "event_selector": "legacy_light_v1",
            "legacy_manifest_sha256": manifest,
        },
        "selected": len(rows),
        "reopened": len([item for item in reopened if item.get("reopened")]),
        "errors": errors,
        "records": reopened if args.apply else [
            {
                "event_id": row["event_id"],
                "current_version": row["current_version"],
                "status": row["status"],
                "next_action": "reopen evidence/human task without rolling back the legacy version",
            }
            for row in rows
        ],
        "no_trading": True,
        "created_at": utc_now(),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.report.with_suffix(".md").write_text(
        "\n".join(
            [
                "# Finance Radar legacy light-verification reconciliation",
                "",
                f"- mode: `{report['mode']}`",
                f"- legacy manifest SHA-256: `{manifest}`",
                f"- selected: `{report['selected']}`",
                f"- reopened: `{report['reopened']}`",
                "- boundary: history is preserved; this workflow only reopens evidence/human follow-up and never trades.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return report


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.reconcile_legacy:
        return _run_legacy_reconciliation(args)
    if args.daily_budget < 0:
        raise SystemExit("--daily-budget cannot be negative")

    contract: dict[str, Any] | None = None
    authorization_context: dict[str, Any] | None = None
    contract_ids: set[str] | None = None
    if args.apply:
        contract, authorization_context, contract_ids = load_scoped_authorization(args)
    ledger = LedgerRepository(args.db)
    operations = OperationsRepository(args.operations_db)
    settings = Settings.from_env()
    router = RiskRouter(settings.model_artifact, settings.model_card)
    event_ids = candidate_ids(
        args.db,
        limit=args.limit,
        event_id=args.event_id,
        require_rough=not args.allow_unrough,
        allowed_event_ids=contract_ids,
    )
    if args.apply and contract is not None and contract_ids is not None:
        authorized_max = _validate_selected_scope(args, contract, event_ids, contract_ids)
    else:
        authorized_max = 0
    batch_id = args.batch_id or f"light-{utc_now().replace(':', '').replace('+00:00', 'Z')}"
    applied_before_batch = _formal_count_today(args.db)
    decisions: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    formal_applied = 0
    attempts_persisted = 0

    for event_id in event_ids:
        event = _event_with_facts(ledger, event_id)
        if event is None:
            continue
        evidence = ledger.event_evidence(event_id)
        event_row = dict(event["event"])
        event_row["facts"] = dict(event.get("current_version", {}).get("facts", {}))
        result = evaluate_event(event_row, evidence)
        before_model = model_snapshot(router, event_row, evidence)
        after_event = _after_event(event_row, result)
        after_evidence = _after_evidence(evidence, result)
        # Failed/unchanged evidence gates do not warrant inventing a second model
        # run.  Reusing the snapshot makes the non-applicable delta explicit.
        after_model = (
            model_snapshot(router, after_event, after_evidence)
            if result["decision"] == "SUPPORTED"
            else {**before_model, "comparison_reused": True}
        )
        delta = model_delta(before_model, after_model)
        result["budget"] = {
            **result.get("budget", {}),
            "model_calls": 2 if result["decision"] == "SUPPORTED" else 1,
            "max_model_calls": 2,
        }
        result.update(
            {
                "batch_id": batch_id,
                "before_model": before_model,
                "after_model": after_model,
                "model_delta": delta,
                "applied": False,
                "formal_applied": False,
                "attempt_persisted": False,
                "after_version": None,
                "authorization_context": authorization_context or {},
                "no_trading": True,
            }
        )

        if args.apply:
            mutation_id: str | None = None
            ledger_committed = False
            try:
                # The operations outbox is prepared before the ledger commit only
                # for a potential formal mutation.  Non-formal gap persistence is
                # audited as an ordinary run after its ledger transaction.
                if result["decision"] == "SUPPORTED":
                    mutation_id = operations.prepare_light_verification_mutation(result)
                connection = sqlite3.connect(args.db, timeout=30)
                connection.row_factory = sqlite3.Row
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    applied_result = apply_event(
                        connection,
                        result,
                        batch_id=batch_id,
                        before_model=before_model,
                        after_model=after_model,
                        authorization_context=authorization_context,
                        daily_budget=args.daily_budget,
                        max_batch_applies=authorized_max,
                    )
                    connection.commit()
                    ledger_committed = True
                    result = applied_result
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.close()
                if mutation_id:
                    if result.get("formal_applied"):
                        try:
                            operations.confirm_light_verification_mutation(mutation_id, result)
                        except Exception as exc:
                            # Do not abandon a prepared audit after ledger commit;
                            # the outbox reconciler can safely confirm it later.
                            result["audit_confirmation_pending"] = f"{type(exc).__name__}: {exc}"
                    else:
                        operations.abandon_light_verification_mutation(
                            mutation_id,
                            str(result.get("application_blocked_reason") or "formal mutation was not applied"),
                        )
                        _record_nonformal_attempt(operations, result)
                else:
                    _record_nonformal_attempt(operations, result)
            except Exception as exc:
                if mutation_id and not ledger_committed:
                    operations.abandon_light_verification_mutation(mutation_id, f"{type(exc).__name__}: {exc}")
                result = {
                    **result,
                    "decision": "SKIPPED",
                    "rationale": f"apply skipped after safe rollback: {type(exc).__name__}: {str(exc)[:300]}",
                    "apply_error": f"{type(exc).__name__}: {str(exc)[:500]}",
                    "applied": False,
                    "formal_applied": False,
                    "attempt_persisted": False,
                }
        decisions.append(result)
        counts[str(result["decision"])] += 1
        formal_applied += int(bool(result.get("formal_applied")))
        attempts_persisted += int(bool(result.get("attempt_persisted")))

    report = {
        "version": LIGHT_VERIFICATION_VERSION,
        "batch_id": batch_id,
        "mode": "apply" if args.apply else "dry_run",
        "authorization": authorization_context,
        "require_rough_review": not args.allow_unrough,
        "requested": len(event_ids),
        "evaluated": len(decisions),
        "formal_applied": formal_applied,
        "attempts_persisted": attempts_persisted,
        "counts": dict(sorted(counts.items())),
        "budget": {
            "max_candidates": args.limit,
            "max_formal_applies": authorized_max if args.apply else 0,
            "daily_formal_budget": int(args.daily_budget),
            "formal_applied_before_batch_today": applied_before_batch,
            "max_primary_documents_per_event": 2,
            "max_model_calls_per_event": 2,
            "actual_network_fetches": 0,
            "actual_external_model_calls": 0,
            "automatic_retries": 0,
        },
        "queue_policy": "unchanged evidence fingerprint is skipped until event/evidence changes; explicit --event-id remains inspectable",
        "suggested_authorization_contract": {
            "authorization": AUTHORIZATION_PHRASE,
            "authorization_id": "replace-with-a-unique-human-approval-id",
            "actor": "replace-with-approver",
            "purpose": "bounded light-verification formal application",
            "batch_id": batch_id,
            "expires_at": "replace-with-an-expiring-ISO-8601-time",
            "max_applies": len(event_ids),
            "event_ids": event_ids,
        },
        "no_trading": True,
        "decisions": decisions,
        "created_at": utc_now(),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md = [
        "# Finance Radar light-verification report",
        "",
        f"- mode: `{report['mode']}`",
        f"- batch: `{batch_id}`",
        f"- evaluated: `{report['evaluated']}`",
        f"- formal applications: `{formal_applied}`",
        f"- persisted nonterminal attempts: `{attempts_persisted}`",
        f"- decisions: `{json.dumps(report['counts'], ensure_ascii=False)}`",
        "- gate: P0/P1 primary source, stable identity, event fact, date coherence, and non-negated modality.",
        "- boundary: no trading, no market-outcome use, and no model-only formal conclusion.",
        "",
    ]
    for item in decisions:
        md.append(
            f"- `{item['event_id']}` · `{item['decision']}` · score `{item.get('score', 0)}` · {item.get('rationale', '')}"
        )
    args.report.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--operations-db", type=Path, default=DEFAULT_OPERATIONS_DB)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--event-id")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-applies", type=int, default=100)
    parser.add_argument("--daily-budget", type=int, default=100)
    parser.add_argument("--batch-id")
    parser.add_argument("--allow-unrough", action="store_true", help="test-only: evaluate candidates without a completed non-conflict rough review")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--authorization")
    parser.add_argument("--authorization-file", type=Path)
    parser.add_argument(
        "--reconcile-legacy",
        action="store_true",
        help="report/reopen v1 light-verification evidence tasks without rolling back historical versions",
    )
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(run(parse_args()), ensure_ascii=False, indent=2))
