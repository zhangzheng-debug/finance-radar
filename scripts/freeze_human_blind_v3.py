"""Build and optionally commit an immutable authentic-human blind-v3 freeze."""

from __future__ import annotations

import argparse
import json
import os
import sys
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
) -> tuple[set[str], set[str], set[str], set[str], set[str], set[str], set[str]]:
    exact: set[str] = set()
    near: set[str] = set()
    event_ids: set[str] = set()
    entities: set[str] = set()
    chains: set[str] = set()
    entity_hashes: set[str] = set()
    chain_hashes: set[str] = set()
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
            if isinstance(row.get("content"), dict):
                near.add(service._near_duplicate_key(row))
            elif str(row.get("text") or "").strip():
                near.add(
                    service._near_duplicate_key(
                        {"content": {"summary": str(row["text"])}}
                    )
                )
    return exact, near, event_ids, entities, chains, entity_hashes, chain_hashes


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
    os.chmod(path, 0o600)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--operations", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--exclude-jsonl", type=Path, action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--authorization-id", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--purpose", default="")
    parser.add_argument("--expires-at", default="")
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
        excluded_events,
        excluded_entities,
        excluded_chains,
        excluded_entity_hashes,
        excluded_chain_hashes,
    ) = _load_exclusions(exclusion_paths, service)
    candidate = service.build_freeze_candidate(
        excluded_text_sha256=excluded_exact,
        excluded_near_duplicate_keys=excluded_near,
        excluded_event_ids=excluded_events,
        excluded_entity_groups=excluded_entities,
        excluded_event_chain_groups=excluded_chains,
        excluded_entity_group_sha256=excluded_entity_hashes,
        excluded_event_chain_group_sha256=excluded_chain_hashes,
    )

    freeze_id = candidate["freeze_id"]
    dataset_path = args.output_dir.resolve() / f"{freeze_id}.jsonl"
    manifest_path = args.output_dir.resolve() / f"{freeze_id}.manifest.json"
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
        }
    )
    _write_atomic(dataset_path, dataset_bytes)

    if args.apply:
        if not all(
            [
                args.authorization_id.strip(),
                args.actor.strip(),
                len(args.purpose.strip()) >= 20,
                args.expires_at.strip(),
            ]
        ):
            raise ValueError("apply requires action-scoped authorization, actor, purpose and expiry")
        expires_at = _iso_future(args.expires_at)
        frozen = operations.freeze_adjudication_samples(
            [str(row["sample_id"]) for row in candidate["rows"]],
            freeze_id,
        )
        manifest.update(
            {
                "applied": True,
                "frozen_samples": frozen,
                "authorization": {
                    "authorization_id": args.authorization_id.strip(),
                    "actor": args.actor.strip(),
                    "purpose": args.purpose.strip(),
                    "expires_at": expires_at,
                },
                "adjudication_state_changed": True,
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
