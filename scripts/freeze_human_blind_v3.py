"""Build and optionally commit an immutable authentic-human blind-v3 freeze."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_EXCLUSION_PATHS = (
    ROOT / "artifacts" / "risk_router_training_manifest.jsonl",
    ROOT / "artifacts" / "risk_router_external_blind_v1.jsonl",
    ROOT / "artifacts" / "risk_router_external_blind_v2.jsonl",
    ROOT / "artifacts" / "risk_router_external_blind_v3.jsonl",
    ROOT / "artifacts" / "risk_router_v3_ai_adjudications_dev.jsonl",
    ROOT / "artifacts" / "risk_router_v4_semantic_dev.jsonl",
)

from app.config import Settings
from app.services import AdjudicationService
from app.services.adjudication import normalize_source_family
from app.storage import LedgerRepository, OperationsRepository


def _iso_future(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        raise ValueError("authorization expiry must be in the future")
    return parsed.astimezone(timezone.utc).isoformat()


def _load_exclusions(
    paths: list[Path], service: AdjudicationService
) -> tuple[
    set[str],
    set[str],
    list[tuple[frozenset[str], frozenset[str]]],
    set[str],
    set[str],
    set[str],
    set[str],
    set[str],
    set[str],
]:
    exact: set[str] = set()
    near: set[str] = set()
    near_signatures: list[tuple[frozenset[str], frozenset[str]]] = []
    event_ids: set[str] = set()
    entities: set[str] = set()
    chains: set[str] = set()
    entity_hashes: set[str] = set()
    chain_hashes: set[str] = set()
    source_families: set[str] = set()
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"non-object exclusion row in {path}")
            text_hash = str(row.get("text_sha256") or "").lower()
            if len(text_hash) == 64:
                exact.add(text_hash)
            event_id = str(row.get("event_id") or "").strip()
            entity = str(row.get("entity_group") or "").strip()
            chain = str(row.get("event_chain_group") or "").strip()
            entity_hash = str(row.get("entity_group_sha256") or "").strip().lower()
            chain_hash = str(row.get("event_chain_group_sha256") or "").strip().lower()
            source_family = normalize_source_family(
                row.get("source_group") or row.get("source_id")
            )
            if event_id:
                event_ids.add(event_id)
            if entity:
                entities.add(entity)
            if chain:
                chains.add(chain)
            if len(entity_hash) == 64:
                entity_hashes.add(entity_hash)
            if len(chain_hash) == 64:
                chain_hashes.add(chain_hash)
            if source_family:
                source_families.add(source_family)
            if isinstance(row.get("content"), dict):
                near.add(service._near_duplicate_key(row))
                near_signatures.append(service._near_duplicate_signature(row))
            elif str(row.get("text") or "").strip():
                annotation = {"content": {"summary": str(row["text"])}}
                near.add(service._near_duplicate_key(annotation))
                near_signatures.append(service._near_duplicate_signature(annotation))
    return (
        exact,
        near,
        near_signatures,
        event_ids,
        entities,
        chains,
        entity_hashes,
        chain_hashes,
        source_families,
    )


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _sample_ids_sha256(rows: list[dict[str, Any]]) -> str:
    sample_ids = sorted(str(row["sample_id"]) for row in rows)
    payload = json.dumps(sample_ids, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _authorization_template(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "action": "FREEZE_HUMAN_BLIND_V3",
        "approved": False,
        "authorization_id": "",
        "actor": "",
        "purpose": "",
        "expires_at": "",
        "freeze_id": candidate["freeze_id"],
        "dataset_sha256": candidate["dataset_sha256"],
        "sample_ids_sha256": candidate["sample_ids_sha256"],
        "sample_count": candidate["row_count"],
        "held_out_source_families": candidate["held_out_source_families"],
    }


def _load_authorization(path: Path, candidate: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError("apply requires an existing authorization contract file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    template = _authorization_template(candidate)
    if not isinstance(payload, dict) or set(payload) != set(template):
        raise ValueError("authorization contract fields do not match schema v1")
    exact_fields = (
        "schema_version",
        "action",
        "freeze_id",
        "dataset_sha256",
        "sample_ids_sha256",
        "sample_count",
        "held_out_source_families",
    )
    for field in exact_fields:
        if payload.get(field) != template[field]:
            raise ValueError(f"authorization contract does not bind the candidate {field}")
    if payload.get("approved") is not True:
        raise ValueError("authorization contract is not approved")
    if not str(payload.get("authorization_id") or "").strip():
        raise ValueError("authorization contract requires authorization_id")
    if len(str(payload.get("actor") or "").strip()) < 3:
        raise ValueError("authorization contract requires an external actor identity")
    if len(str(payload.get("purpose") or "").strip()) < 20:
        raise ValueError("authorization contract purpose is too short")
    payload["expires_at"] = _iso_future(str(payload.get("expires_at") or ""))
    if not payload["held_out_source_families"]:
        raise ValueError("apply requires at least one fully held-out source family")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--operations", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--exclude-jsonl", type=Path, action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--authorization-file", type=Path)
    args = parser.parse_args()

    settings = Settings.from_env()
    ledger_path = (args.ledger or settings.ledger_db).resolve()
    operations_path = (args.operations or settings.operations_db).resolve()
    operations = OperationsRepository(operations_path)
    service = AdjudicationService(LedgerRepository(ledger_path), operations)
    exclusion_paths = [*DEFAULT_EXCLUSION_PATHS, *args.exclude_jsonl]
    missing = [path for path in exclusion_paths if not path.is_file()]
    if missing:
        raise ValueError("required overlap reference is missing: " + ", ".join(map(str, missing)))
    (
        excluded_exact,
        excluded_near,
        excluded_near_signatures,
        excluded_events,
        excluded_entities,
        excluded_chains,
        excluded_entity_hashes,
        excluded_chain_hashes,
        excluded_source_families,
    ) = _load_exclusions(exclusion_paths, service)
    candidate = service.build_freeze_candidate(
        excluded_text_sha256=excluded_exact,
        excluded_near_duplicate_keys=excluded_near,
        excluded_near_duplicate_signatures=excluded_near_signatures,
        excluded_event_ids=excluded_events,
        excluded_entity_groups=excluded_entities,
        excluded_event_chain_groups=excluded_chains,
        excluded_entity_group_sha256=excluded_entity_hashes,
        excluded_event_chain_group_sha256=excluded_chain_hashes,
    )

    candidate["sample_ids_sha256"] = _sample_ids_sha256(candidate["rows"])
    candidate_source_families = sorted(
        {
            normalize_source_family(source)
            for source in candidate["source_groups"]
            if normalize_source_family(source)
        }
    )
    held_out_source_families = sorted(
        set(candidate_source_families) - excluded_source_families
    )
    candidate.update(
        {
            "candidate_source_families": candidate_source_families,
            "prior_exposed_source_families": sorted(excluded_source_families),
            "held_out_source_families": held_out_source_families,
            "source_holdout_status": (
                "ELIGIBLE_WITH_FULLY_HELD_OUT_FAMILY"
                if held_out_source_families
                else "BLOCKED_NO_FULLY_HELD_OUT_FAMILY"
            ),
        }
    )

    freeze_id = candidate["freeze_id"]
    dataset_path = args.output_dir.resolve() / f"{freeze_id}.jsonl"
    manifest_path = args.output_dir.resolve() / f"{freeze_id}.manifest.json"
    authorization_template_path = (
        args.output_dir.resolve() / f"{freeze_id}.authorization.template.json"
    )
    dataset_bytes = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in candidate["rows"]
    ).encode("utf-8")
    manifest: dict[str, Any] = {
        key: value for key, value in candidate.items() if key != "rows"
    }
    manifest.update(
        {
            "dataset_path": str(dataset_path),
            "ledger_path": str(ledger_path),
            "operations_path": str(operations_path),
            "excluded_manifests": [str(path.resolve()) for path in exclusion_paths],
            "applied": False,
            "commit_state": "DRY_RUN",
            "artifact_permission_contract": (
                "POSIX_MODE_0600" if os.name != "nt" else "WINDOWS_CALLER_ACL_NOT_PROVEN_BY_CHMOD"
            ),
        }
    )
    if not args.apply:
        manifest["authorization_template_path"] = str(authorization_template_path)

    authorization: dict[str, Any] | None = None
    if args.apply:
        if not args.authorization_file:
            raise ValueError("apply requires --authorization-file")
        authorization = _load_authorization(args.authorization_file.resolve(), candidate)
        manifest.update(
            {
                "commit_state": "PREPARED",
                "authorization_file": str(args.authorization_file.resolve()),
                "authorization": authorization,
            }
        )

    # No artifact is written until every apply-time authorization and holdout
    # gate has passed. A PREPARED manifest then precedes the database commit.
    _write_atomic(dataset_path, dataset_bytes)
    if not args.apply:
        _write_atomic(
            authorization_template_path,
            (
                json.dumps(_authorization_template(candidate), ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8"),
        )
    _write_atomic(
        manifest_path,
        (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )

    if args.apply and authorization is not None:
        commit = operations.commit_adjudication_freeze(
            [str(row["sample_id"]) for row in candidate["rows"]],
            freeze_id,
            dataset_sha256=str(candidate["dataset_sha256"]),
            sample_ids_sha256=str(candidate["sample_ids_sha256"]),
            authorization=authorization,
            dataset_path=str(dataset_path),
            manifest_path=str(manifest_path),
        )
        manifest.update(
            {
                "applied": True,
                "commit_state": "COMMITTED",
                "frozen_samples": commit["frozen_samples"],
                "idempotent_reconciliation": commit["idempotent"],
                "authorization_sha256": commit["receipt"]["authorization_sha256"],
                "database_receipt_persisted": True,
                "adjudication_state_changed": not commit["idempotent"],
                "production_model_changed": False,
                "canonical_event_state_changed": False,
            }
        )
    _write_atomic(
        manifest_path,
        (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
