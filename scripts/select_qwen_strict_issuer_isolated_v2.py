#!/usr/bin/env python3
"""Freeze a label-blind, issuer-isolated Qwen strict benchmark selection.

The selector deliberately consumes only provider text and owner-side identity
metadata.  Labels, model predictions, market outcomes, and price responses are
not inputs and are rejected when encountered.  Selection is deterministic from
a frozen salt and two pre-registered source-only strata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]

SCHEMA_VERSION = 2
SELECTION_CONTRACT = "qwen-strict-issuer-isolated-benchmark-v2"
DEFAULT_SELECTION_SALT = "finance-radar-qwen-strict-issuer-isolated-v2-20260830"
DEFAULT_GENERAL_COUNT = 30
DEFAULT_HIGH_RISK_COUNT = 30
DEFAULT_LEGACY_STRICT_COUNT = 60

GENERAL_STRATUM = "GENERAL"
HIGH_RISK_STRATUM = "HIGH_RISK_MECHANISM_ENRICHED"
PREDICATE_VERSION = "HIGH_RISK_MECHANISM_KEYWORD_DOC_V1"
STRONG_PARSE_VERSION = "STRICT_SOURCE_AND_STRONG_CANONICAL_ISSUER_V1"

PROVIDER_OUTPUT = "strict_benchmark_v2_provider_input.jsonl"
OWNER_OUTPUT = "strict_benchmark_v2_owner_index.owner-only.jsonl"
MANIFEST_OUTPUT = "manifest.json"
SUPERSESSION_OUTPUT = "legacy_strict60_v1_supersession_manifest.template.json"

# These are source document types whose existence is itself a deterministic
# high-risk mechanism signal.  Broad forms such as 8-K and 10-K are omitted.
HIGH_RISK_DOCUMENT_TYPES = (
    "25",
    "25-NSE",
    "FORM 25",
    "NT 10-K",
    "NT 10-Q",
)

# SEC item numbers with a sufficiently direct downside mechanism.  The match
# is token based, so e.g. 3.01 does not match 3.010.
HIGH_RISK_ITEM_SECTIONS = (
    "1.03",  # bankruptcy or receivership
    "2.04",  # triggering events that accelerate obligations
    "3.01",  # delisting / listing-standard notice
    "4.02",  # non-reliance on previously issued financial statements
)

# Regexes are frozen here and serialized verbatim into the manifest.  They are
# evaluated only against headline, summary, and primary-source passage text.
HIGH_RISK_MECHANISM_PATTERNS: tuple[tuple[str, str], ...] = (
    ("BANKRUPTCY_CHAPTER", r"\bchapter\s+(?:7|11)\b"),
    ("INSOLVENCY_RECEIVERSHIP", r"\b(?:bankrupt(?:cy)?|insolven(?:cy|t)|receivership)\b"),
    ("LIQUIDATION_DISSOLUTION", r"\b(?:liquidat(?:e|ed|ing|ion)|dissol(?:ve|ved|ution))\b"),
    ("WIND_DOWN_CEASE_OPERATIONS", r"\b(?:wind(?:ing)?\s+down|ceas(?:e|ed|ing)\s+(?:all\s+)?operations)\b"),
    ("GOING_CONCERN", r"\b(?:going\s+concern|substantial\s+doubt)\b"),
    (
        "DEBT_DEFAULT_ACCELERATION",
        r"\b(?:default(?:ed)?\s+(?:under|on)|covenant\s+(?:default|breach)|breach\s+of\s+(?:a\s+)?covenant|accelerat(?:e|ed|ion)\s+(?:of\s+)?(?:the\s+)?(?:debt|indebtedness|obligations?))\b",
    ),
    (
        "DELISTING_TRADING_SUSPENSION",
        r"\b(?:delist(?:ed|ing)?|listing\s+deficien(?:cy|cies)|suspend(?:ed|ing)?\s+(?:in\s+)?trading|trading\s+suspension)\b",
    ),
    (
        "RESTATEMENT_NON_RELIANCE",
        r"\b(?:financial\s+restatement|restate(?:d|ment)\s+(?:its|the|previously)|should\s+no\s+longer\s+be\s+relied\s+upon|non-reliance\s+on\s+previously\s+issued)\b",
    ),
    ("MATERIAL_WEAKNESS", r"\bmaterial\s+weakness(?:es)?\b"),
    (
        "REGULATORY_ENFORCEMENT_PENALTY",
        r"\b(?:criminal\s+charges?|civil\s+penalt(?:y|ies)|regulatory\s+enforcement\s+action|consent\s+decree)\b",
    ),
    (
        "CLINICAL_REGULATORY_FAILURE",
        r"\b(?:clinical\s+hold|complete\s+response\s+letter|failed\s+to\s+meet\s+(?:its\s+)?(?:primary|main)\s+endpoint|did\s+not\s+meet\s+(?:its\s+)?(?:primary|main)\s+endpoint)\b",
    ),
    ("MATERIAL_CYBER_INCIDENT", r"\b(?:material\s+cybersecurity\s+incident|material\s+cyber\s+incident)\b"),
)
_HIGH_RISK_REGEXES = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in HIGH_RISK_MECHANISM_PATTERNS
)

HASH_FIELDS = (
    "content_sha256",
    "provider_text_sha256",
    "provider_text_sha256_v1",
    "source_text_sha256",
    "semantic_context_sha256",
    "source_packet_sha256",
    "hash",
)

# Exact structural keys that identify answer-bearing, model-output, or
# post-event market data.  Text values are never scanned for these words.
PROHIBITED_INPUT_KEYS = frozenset(
    {
        "adverse_strength",
        "arbiter",
        "arbiter_final",
        "assistant_answer",
        "brief_reason",
        "expected",
        "forward_return",
        "forward_returns",
        "gold",
        "ground_truth",
        "human_gold",
        "impact_strength",
        "label",
        "label_classification",
        "label_provenance",
        "labels",
        "logits",
        "market_outcome",
        "market_outcomes",
        "materiality",
        "model_output",
        "model_outputs",
        "model_prediction",
        "model_predictions",
        "novelty",
        "polarity",
        "post_event_market_data",
        "prediction",
        "predictions",
        "price_change",
        "price_response",
        "probabilities",
        "qwen_output",
        "qwen_prediction",
        "qwen_predictions",
        "reason_codes",
        "ret_1d",
        "ret_3d",
        "ret_5d",
        "ret_10d",
        "ret_20d",
        "review",
        "review_a",
        "review_b",
        "reviewer_labels",
        "reviews",
        "risk_status",
        "semantic_priority",
        "subject_relation",
        "target",
        "targets",
        "event_realization",
    }
)


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _identifier(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalized_identifier(value: Any) -> str | None:
    text = _identifier(value)
    if text is None:
        return None
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(normalized.split()) or None


def _valid_sha256(value: Any, *, context: str) -> str:
    text = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ValueError(f"{context}: invalid SHA-256 value {value!r}")
    return text


def _is_strong_resolution_quality(value: Any) -> bool:
    """Accept only issuer-map qualities explicitly declared STRONG.

    Identity-graph producers may use more specific values such as STRONG_CIK,
    STRONG_STABLE_ID, or STRONG_IDENTITY_GRAPH.  A mixed join remains strong
    only when every constituent quality is explicitly strong.
    """

    quality = str(value or "").strip().upper()
    if quality.startswith("MIXED:"):
        parts = [part for part in quality.removeprefix("MIXED:").split("+") if part]
        return bool(parts) and all(part.startswith("STRONG") for part in parts)
    return quality.startswith("STRONG")


def _read_records(path: Path, *, container_keys: Iterable[str] = ()) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8-sig").strip()
    if not raw:
        return []
    parsed: Any
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if isinstance(parsed, dict):
        for key in container_keys:
            if key in parsed:
                parsed = parsed[key]
                break
        else:
            parsed = [parsed]
    if not isinstance(parsed, list) or any(not isinstance(row, dict) for row in parsed):
        raise ValueError(f"{path}: expected JSONL objects or a JSON array of objects")
    return parsed


def _assert_no_prohibited_keys(value: Any, *, context: str, path: str = "$") -> None:
    if isinstance(value, dict):
        for raw_key, nested in value.items():
            key = str(raw_key).strip().casefold()
            nested_path = f"{path}.{raw_key}"
            if key in PROHIBITED_INPUT_KEYS:
                raise ValueError(f"{context}: prohibited answer/prediction/outcome key at {nested_path}")
            _assert_no_prohibited_keys(nested, context=context, path=nested_path)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_no_prohibited_keys(nested, context=context, path=f"{path}[{index}]")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> str:
    payload = "".join(stable_json(row) + "\n" for row in rows).encode("utf-8")
    _atomic_write(path, payload)
    return sha256_bytes(payload)


def _write_json(path: Path, value: Any) -> str:
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write(path, payload)
    return sha256_bytes(payload)


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        commit = result.stdout.strip()
        tracked = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if tracked.stdout.strip():
            return f"UNCOMMITTED_TRACKED_CHANGES:{commit}"
        return commit
    except (OSError, subprocess.CalledProcessError):
        return "UNAVAILABLE"


class CanonicalIssuerLookup:
    """Resolve a frozen canonical issuer by sample/event with conflict checks."""

    def __init__(self, path: Path) -> None:
        rows = _read_records(path, container_keys=("issuers", "mappings", "rows"))
        self.rows = rows
        self.by_sample: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for number, row in enumerate(rows, 1):
            _assert_no_prohibited_keys(row, context=f"{path}:{number}")
            sample_id = _normalized_identifier(row.get("sample_id"))
            event_id = _normalized_identifier(row.get("event_id") or row.get("source_event_id"))
            canonical = _identifier(
                row.get("canonical_issuer_key")
                or row.get("canonical_issuer_group")
                or row.get("canonical_entity_group")
            )
            quality = _identifier(row.get("resolution_quality") or row.get("identity_quality"))
            if not sample_id and not event_id:
                raise ValueError(f"{path}:{number}: canonical issuer row lacks sample_id and event_id")
            normalized = {
                "sample_id": sample_id,
                "event_id": event_id,
                "canonical_issuer_key": canonical,
                "resolution_quality": (quality or "UNSPECIFIED").upper(),
            }
            if sample_id:
                self.by_sample[sample_id].append(normalized)
            if event_id:
                self.by_event[event_id].append(normalized)
        self._validate_groups(path)

    def _validate_groups(self, path: Path) -> None:
        for kind, groups in (("sample_id", self.by_sample), ("event_id", self.by_event)):
            for identifier, rows in groups.items():
                keys = {row["canonical_issuer_key"] for row in rows}
                if len(keys) > 1:
                    raise ValueError(
                        f"{path}: conflicting canonical issuer keys for {kind} {identifier}: "
                        f"{sorted(str(key) for key in keys)}"
                    )

    def resolve(
        self, sample_id: Any, event_id: Any, *, context: str,
    ) -> tuple[str, str]:
        sample = _normalized_identifier(sample_id)
        event = _normalized_identifier(event_id)
        matches: list[dict[str, Any]] = []
        if sample:
            sample_matches = self.by_sample.get(sample, [])
            for match in sample_matches:
                if event and match["event_id"] and match["event_id"] != event:
                    raise ValueError(
                        f"{context}: canonical issuer sample/event conflict for {sample}: "
                        f"{match['event_id']} != {event}"
                    )
            matches.extend(sample_matches)
        if event:
            matches.extend(self.by_event.get(event, []))
        if not matches:
            raise ValueError(
                f"{context}: missing canonical issuer mapping for sample_id={sample!r}, event_id={event!r}"
            )
        keys = {match["canonical_issuer_key"] for match in matches}
        if len(keys) != 1 or None in keys:
            raise ValueError(
                f"{context}: unresolved or conflicting canonical issuer for "
                f"sample_id={sample!r}, event_id={event!r}"
            )
        qualities = {str(match["resolution_quality"]) for match in matches}
        quality = next(iter(qualities)) if len(qualities) == 1 else "MIXED:" + "+".join(sorted(qualities))
        return str(next(iter(keys))), quality

    def resolve_any(
        self, sample_ids: Iterable[Any], event_id: Any, *, context: str,
    ) -> tuple[str, str]:
        results: set[tuple[str, str]] = set()
        for sample_id in sample_ids:
            sample = _normalized_identifier(sample_id)
            if not sample or sample not in self.by_sample:
                continue
            # A mapped sample that conflicts with the supplied event is a hard
            # registry error; it must not be hidden by a valid origin alias.
            results.add(self.resolve(sample_id, event_id, context=context))
        if _normalized_identifier(event_id) in self.by_event:
            results.add(self.resolve(None, event_id, context=context))
        if not results:
            raise ValueError(
                f"{context}: no canonical issuer mapping for sample aliases or event_id={event_id!r}"
            )
        canonical_keys = {result[0] for result in results}
        if len(canonical_keys) != 1:
            raise ValueError(f"{context}: sample aliases resolve to conflicting canonical issuers")
        qualities = {result[1] for result in results}
        quality = next(iter(qualities)) if len(qualities) == 1 else "MIXED:" + "+".join(sorted(qualities))
        return next(iter(canonical_keys)), quality


def _provider_rows(path: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    rows = _read_records(path)
    by_id: dict[str, dict[str, Any]] = {}
    for number, row in enumerate(rows, 1):
        if set(row) != {"sample_id", "content"}:
            raise ValueError(
                f"{path}:{number}: provider rows must contain exactly sample_id and content"
            )
        _assert_no_prohibited_keys(row, context=f"{path}:{number}")
        sample_id = _identifier(row.get("sample_id"))
        content = row.get("content")
        if not sample_id or sample_id in by_id or not isinstance(content, dict):
            raise ValueError(f"{path}:{number}: missing/duplicate sample_id or non-object content")
        by_id[sample_id] = row
    if not rows:
        raise ValueError(f"{path}: provider input is empty")
    return rows, by_id


def _content_hashes(row: dict[str, Any], *, context: str) -> set[str]:
    hashes: set[str] = set()
    for field in HASH_FIELDS:
        raw = row.get(field)
        values = raw if isinstance(raw, list) else [raw]
        for value in values:
            if value in (None, ""):
                continue
            hashes.add(_valid_sha256(value, context=f"{context}.{field}"))
    for value in row.get("content_hashes") or []:
        hashes.add(_valid_sha256(value, context=f"{context}.content_hashes"))
    return hashes


def _owner_rows(
    path: Path, provider_by_id: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    rows = _read_records(path)
    by_id: dict[str, dict[str, Any]] = {}
    for number, row in enumerate(rows, 1):
        _assert_no_prohibited_keys(row, context=f"{path}:{number}")
        sample_id = _identifier(row.get("sample_id"))
        event_id = _identifier(row.get("source_event_id") or row.get("event_id"))
        entity = _identifier(row.get("entity_group"))
        chain = _identifier(row.get("event_chain_group"))
        if not sample_id or sample_id in by_id or not event_id or not entity or not chain:
            raise ValueError(f"{path}:{number}: owner row lacks unique sample/event/entity/chain")
        hashes = _content_hashes(row, context=f"{path}:{number}")
        if not hashes:
            raise ValueError(f"{path}:{number}: owner row has no content hash")
        provider = provider_by_id.get(sample_id)
        if provider is None:
            raise ValueError(f"{path}:{number}: owner sample is absent from provider input: {sample_id}")
        computed = sha256_bytes(stable_json(provider["content"]).encode("utf-8"))
        declared_provider = row.get("provider_text_sha256") or row.get("content_sha256")
        if declared_provider is None:
            raise ValueError(f"{path}:{number}: owner row lacks provider/content SHA-256")
        if _valid_sha256(declared_provider, context=f"{path}:{number}.provider_hash") != computed:
            raise ValueError(f"{path}:{number}: provider content hash mismatch for {sample_id}")
        by_id[sample_id] = row
    provider_ids = set(provider_by_id)
    owner_ids = set(by_id)
    if provider_ids != owner_ids:
        raise ValueError(
            "strict500 provider/owner sample mismatch: "
            f"missing_owner={sorted(provider_ids - owner_ids)[:5]}, "
            f"missing_provider={sorted(owner_ids - provider_ids)[:5]}"
        )
    return by_id


def _legacy_sample_ids(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8-sig").strip()
    if not raw:
        raise ValueError(f"{path}: legacy strict sample-id list is empty")
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        parsed = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if isinstance(parsed, dict):
        # The existing label-free audit60 manifest contains a numeric selection
        # target.  Only sample_ids and explicit non-exposure declarations are
        # consumed; structural buckets are intentionally ignored.
        for flag in ("labels_read", "model_outputs_read", "market_results_read"):
            if parsed.get(flag) not in (None, False):
                raise ValueError(f"{path}: legacy manifest declares {flag}=true")
        forbidden_roots = {
            key for key in parsed
            if str(key).casefold() in PROHIBITED_INPUT_KEYS and str(key).casefold() != "target"
        }
        if forbidden_roots:
            raise ValueError(f"{path}: legacy sample manifest contains prohibited keys {sorted(forbidden_roots)}")
        parsed = parsed.get("sample_ids")
    if not isinstance(parsed, list):
        raise ValueError(f"{path}: expected sample_ids array, JSON array, or JSONL sample rows")
    result: list[str] = []
    for number, value in enumerate(parsed, 1):
        if isinstance(value, dict):
            _assert_no_prohibited_keys(value, context=f"{path}:{number}")
            if set(value) - {"sample_id", "schema_version"}:
                raise ValueError(f"{path}:{number}: legacy JSONL row must be sample-id only")
            value = value.get("sample_id")
        sample_id = _identifier(value)
        if not sample_id:
            raise ValueError(f"{path}:{number}: missing legacy sample_id")
        result.append(sample_id)
    if len(set(result)) != len(result):
        raise ValueError(f"{path}: duplicate legacy sample IDs")
    return result


def _normalized_document_type(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).upper()


def high_risk_mechanism_matches(content: dict[str, Any]) -> list[str]:
    """Return frozen source-only predicate matches, never semantic labels."""

    matches: set[str] = set()
    passages = content.get("passages") if isinstance(content.get("passages"), list) else []
    text_parts = [str(content.get("headline") or ""), str(content.get("summary") or "")]
    for passage in passages:
        if not isinstance(passage, dict):
            continue
        document_type = _normalized_document_type(passage.get("document_type"))
        if document_type in HIGH_RISK_DOCUMENT_TYPES:
            matches.add(f"DOCUMENT_TYPE:{document_type}")
        item_section = str(passage.get("item_section") or "")
        for item in HIGH_RISK_ITEM_SECTIONS:
            if re.search(rf"(?<![0-9.]){re.escape(item)}(?![0-9.])", item_section):
                matches.add(f"ITEM_SECTION:{item}")
        text_parts.append(str(passage.get("passage") or ""))
    joined = "\n".join(text_parts)
    for name, regex in _HIGH_RISK_REGEXES:
        if regex.search(joined):
            matches.add(f"MECHANISM:{name}")
    return sorted(matches)


def _strong_parse_reasons(
    content: dict[str, Any], owner: dict[str, Any], resolution_quality: str,
) -> list[str]:
    reasons: list[str] = []
    if not _is_strong_resolution_quality(resolution_quality):
        reasons.append("CANONICAL_ISSUER_NOT_STRONG_RESOLVED")
    subject = content.get("focal_subject") if isinstance(content.get("focal_subject"), dict) else {}
    semantic = content.get("semantic_context") if isinstance(content.get("semantic_context"), dict) else {}
    semantic_subject = semantic.get("focal_subject") if isinstance(semantic.get("focal_subject"), dict) else {}
    roles = {str(subject.get("role") or "").upper(), str(semantic_subject.get("role") or "").upper()}
    if "ISSUER" not in roles:
        reasons.append("FOCAL_SUBJECT_NOT_ISSUER")
    if content.get("source_identity_hidden") is not True:
        reasons.append("SOURCE_IDENTITY_NOT_HIDDEN")
    content_complete = content.get("source_excerpt_complete") is True or semantic.get("source_excerpt_complete") is True
    if not content_complete or owner.get("source_excerpt_complete") is not True:
        reasons.append("SOURCE_EXCERPT_NOT_COMPLETE")
    if not _identifier(content.get("headline")):
        reasons.append("HEADLINE_MISSING")
    evidence_text = " ".join(
        [str(content.get("headline") or ""), str(content.get("summary") or "")]
        + [
            str(passage.get("passage") or "")
            for passage in (content.get("passages") or [])
            if isinstance(passage, dict)
        ]
    ).strip()
    if len(evidence_text) < 80:
        reasons.append("SOURCE_EVIDENCE_TOO_SHORT")
    return reasons


def _rank(salt: str, purpose: str, *values: Any) -> str:
    material = "\x1f".join([salt, purpose, *(str(value) for value in values)])
    return sha256_bytes(material.encode("utf-8"))


def _axis_value(row: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = _normalized_identifier(row.get(name))
        if value:
            return value
    return None


def _empty_exposure_sets() -> dict[str, set[str]]:
    return {
        "sample_id": set(),
        "event_id": set(),
        "entity_group": set(),
        "canonical_issuer_key": set(),
        "event_chain_group": set(),
        "content_hash": set(),
    }


def _add_exposure(
    sets: dict[str, set[str]], *, sample_ids: Iterable[Any], event_id: Any,
    entity_group: Any, canonical_issuer: Any, event_chain: Any, content_hashes: Iterable[str],
) -> None:
    for sample_id in sample_ids:
        normalized = _normalized_identifier(sample_id)
        if normalized:
            sets["sample_id"].add(normalized)
    mappings = {
        "event_id": event_id,
        "entity_group": entity_group,
        "canonical_issuer_key": canonical_issuer,
        "event_chain_group": event_chain,
    }
    for axis, value in mappings.items():
        normalized = _normalized_identifier(value)
        if normalized:
            sets[axis].add(normalized)
    sets["content_hash"].update(str(value).lower() for value in content_hashes)


def _load_training_exposures(
    paths: list[Path], canonical: CanonicalIssuerLookup,
) -> tuple[dict[str, set[str]], dict[str, Any]]:
    sets = _empty_exposure_sets()
    row_count = 0
    origins: set[str] = set()
    splits: Counter[str] = Counter()
    input_records: list[dict[str, Any]] = []
    for path in paths:
        rows = _read_records(path, container_keys=("exposures", "rows"))
        input_records.append({"sha256": sha256_file(path), "row_count": len(rows)})
        for number, row in enumerate(rows, 1):
            context = f"{path}:{number}"
            _assert_no_prohibited_keys(row, context=context)
            sample_id = _identifier(row.get("sample_id"))
            origin_id = _identifier(row.get("origin_sample_id"))
            event_id = _identifier(row.get("event_id") or row.get("source_event_id"))
            chain = _identifier(row.get("event_chain_group"))
            if not sample_id or not event_id or not chain:
                raise ValueError(f"{context}: exposure lacks sample_id, event_id, or event_chain_group")
            hashes = _content_hashes(row, context=context)
            if not hashes:
                raise ValueError(f"{context}: exposure has no content/source hash")
            canonical_key, resolution_quality = canonical.resolve_any(
                [sample_id, origin_id], event_id, context=f"{context} exposure",
            )
            if not _is_strong_resolution_quality(resolution_quality):
                raise ValueError(
                    f"{context}: exposure canonical issuer is not strong-resolved: "
                    f"{resolution_quality}"
                )
            declared = _identifier(row.get("canonical_issuer_key") or row.get("canonical_issuer_group"))
            if declared and _normalized_identifier(declared) != _normalized_identifier(canonical_key):
                raise ValueError(f"{context}: exposure canonical issuer conflicts with frozen map")
            entity = _identifier(row.get("entity_group"))
            _add_exposure(
                sets, sample_ids=[sample_id, origin_id], event_id=event_id,
                entity_group=entity, canonical_issuer=canonical_key,
                event_chain=chain, content_hashes=hashes,
            )
            origin = _normalized_identifier(origin_id or sample_id)
            if origin:
                origins.add(origin)
            splits[str(row.get("exposure_split") or "UNSPECIFIED")] += 1
            dataset_hash = row.get("source_dataset_sha256")
            if dataset_hash not in (None, ""):
                _valid_sha256(dataset_hash, context=f"{context}.source_dataset_sha256")
            row_count += 1
    if not paths:
        raise ValueError("at least one training exposure registry is required")
    return sets, {
        "files": input_records,
        "row_count": row_count,
        "unique_origin_sample_count": len(origins),
        "exposure_split_counts": dict(sorted(splits.items())),
    }


def _candidate_axes(
    sample_id: str, owner: dict[str, Any], canonical_issuer: str,
) -> dict[str, Any]:
    return {
        "sample_id": _normalized_identifier(sample_id),
        "event_id": _axis_value(owner, "source_event_id", "event_id"),
        "entity_group": _axis_value(owner, "entity_group"),
        "canonical_issuer_key": _normalized_identifier(canonical_issuer),
        "event_chain_group": _axis_value(owner, "event_chain_group"),
        "content_hash": _content_hashes(owner, context=f"owner sample {sample_id}"),
    }


def _leak_reasons(axes: dict[str, Any], exposures: dict[str, set[str]]) -> list[str]:
    reasons: list[str] = []
    for axis in ("sample_id", "event_id", "entity_group", "canonical_issuer_key", "event_chain_group"):
        if axes[axis] and axes[axis] in exposures[axis]:
            reasons.append(f"EXPOSURE_{axis.upper()}")
    if axes["content_hash"] & exposures["content_hash"]:
        reasons.append("EXPOSURE_CONTENT_HASH")
    return reasons


def _legacy_supersession_template(
    *, legacy_path_hash: str, legacy_ids: list[str], selected_count: int,
) -> dict[str, Any]:
    id_set_hash = sha256_bytes((stable_json(sorted(legacy_ids)) + "\n").encode("utf-8"))
    return {
        "schema_version": 1,
        "supersession_contract": "qwen-strict60-v1-supersession-v1",
        "legacy_benchmark": "qwen-triple-ai-strict60-v1",
        "status": "CONSUMED_DIAGNOSTIC_AI_REFERENCE_NOT_HUMAN_GOLD",
        "classification": "AI_NOT_HUMAN_GOLD",
        "provenance": {
            "adjudication": "THREE_INDEPENDENT_AI_REVIEWS_WITH_AI_ARBITRATION",
            "reviewer_type": "AI",
            "human_gold_claimed": False,
        },
        "legacy_sample_ids": {
            "count": len(legacy_ids),
            "source_file_sha256": legacy_path_hash,
            "sorted_set_sha256": id_set_hash,
        },
        "legacy_artifact_sha256_to_fill_before_release": {
            "core_v1": None,
            "full_v2_truth": None,
            "manifest": None,
            "provider_input": None,
            "owner_index": None,
        },
        "superseded_by": {
            "selection_contract": SELECTION_CONTRACT,
            "manifest": MANIFEST_OUTPUT,
            "provider_input": PROVIDER_OUTPUT,
            "owner_index": OWNER_OUTPUT,
            "unlabeled_selected_rows": selected_count,
        },
        "legacy_artifacts_immutable": True,
        "allowed_uses": [
            "DIAGNOSTIC_ERROR_ANALYSIS",
            "REGRESSION_TRACKING_WITH_AI_REFERENCE_CAVEAT",
            "LEAKAGE_AND_PIPELINE_AUDIT",
        ],
        "forbidden_uses": [
            "FINAL_BLIND_MODEL_SELECTION",
            "PRODUCTION_QUALIFICATION",
            "HUMAN_GOLD_CLAIM",
            "TRAINING_OR_THRESHOLD_TUNING",
        ],
        "selector_labels_read": False,
        "selector_model_predictions_read": False,
        "selector_market_outcomes_read": False,
        "human_gold_claimed": False,
    }


def select_issuer_isolated_benchmark_v2(
    *,
    strict500_provider_input: Path,
    strict500_owner_index: Path,
    canonical_issuer_map: Path,
    training_exposure_registries: list[Path],
    legacy_strict60_sample_ids: Path,
    output_dir: Path,
    selection_salt: str = DEFAULT_SELECTION_SALT,
    general_count: int = DEFAULT_GENERAL_COUNT,
    high_risk_count: int = DEFAULT_HIGH_RISK_COUNT,
    expected_legacy_count: int = DEFAULT_LEGACY_STRICT_COUNT,
    code_commit: str | None = None,
) -> dict[str, Any]:
    """Select and atomically freeze the unlabeled v2 benchmark inputs."""

    if general_count < 0 or high_risk_count < 0 or general_count + high_risk_count <= 0:
        raise ValueError("strata counts must be non-negative and sum to a positive target")
    if expected_legacy_count < 0:
        raise ValueError("expected legacy count must be non-negative")
    if not selection_salt.strip():
        raise ValueError("selection salt must be non-empty")
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing output directory: {output_dir}")

    providers, provider_by_id = _provider_rows(strict500_provider_input)
    owner_by_id = _owner_rows(strict500_owner_index, provider_by_id)
    canonical = CanonicalIssuerLookup(canonical_issuer_map)
    legacy_ids = _legacy_sample_ids(legacy_strict60_sample_ids)
    if len(legacy_ids) != expected_legacy_count:
        raise ValueError(
            f"legacy strict sample count mismatch: expected {expected_legacy_count}, got {len(legacy_ids)}"
        )
    missing_legacy = sorted(set(legacy_ids) - set(provider_by_id))
    if missing_legacy:
        raise ValueError(f"legacy strict IDs absent from strict500 input: {missing_legacy[:5]}")

    exposures, exposure_summary = _load_training_exposures(training_exposure_registries, canonical)
    training_axis_counts = {axis: len(values) for axis, values in exposures.items()}

    # The consumed strict60 contributes every isolation axis, not just its IDs.
    # It is a superseded diagnostic set, so a resolved provisional issuer may be
    # used here for conservative exposure blocking.  This does not make that
    # row benchmark-eligible: newly selected candidates still must resolve with
    # an explicit STRONG quality in _strong_parse_reasons().
    legacy_resolution_quality_counts: Counter[str] = Counter()
    for sample_id in legacy_ids:
        owner = owner_by_id[sample_id]
        event_id = _identifier(owner.get("source_event_id") or owner.get("event_id"))
        canonical_key, legacy_quality = canonical.resolve(
            sample_id, event_id, context=f"legacy strict60 {sample_id}",
        )
        legacy_resolution_quality_counts[legacy_quality] += 1
        declared = _identifier(owner.get("canonical_issuer_key") or owner.get("canonical_issuer_group"))
        if declared and _normalized_identifier(declared) != _normalized_identifier(canonical_key):
            raise ValueError(f"legacy strict60 {sample_id}: owner/map canonical issuer conflict")
        _add_exposure(
            exposures, sample_ids=[sample_id], event_id=event_id,
            entity_group=owner.get("entity_group"), canonical_issuer=canonical_key,
            event_chain=owner.get("event_chain_group"),
            content_hashes=_content_hashes(owner, context=f"legacy strict60 {sample_id}"),
        )

    candidates: list[dict[str, Any]] = []
    exclusion_counts: Counter[str] = Counter()
    for input_position, provider in enumerate(providers):
        sample_id = str(provider["sample_id"])
        owner = owner_by_id[sample_id]
        event_id = _identifier(owner.get("source_event_id") or owner.get("event_id"))
        try:
            canonical_key, quality = canonical.resolve(
                sample_id, event_id, context=f"strict500 candidate {sample_id}",
            )
        except ValueError:
            # Unresolved candidate rows are ineligible.  Exposure and consumed
            # legacy rows are different: either being unresolved aborts the run
            # because zero issuer overlap could not then be proved.
            exclusion_counts["CANONICAL_ISSUER_UNRESOLVED"] += 1
            continue
        declared = _identifier(owner.get("canonical_issuer_key") or owner.get("canonical_issuer_group"))
        if declared and _normalized_identifier(declared) != _normalized_identifier(canonical_key):
            raise ValueError(f"strict500 candidate {sample_id}: owner/map canonical issuer conflict")
        axes = _candidate_axes(sample_id, owner, canonical_key)
        reasons = _strong_parse_reasons(provider["content"], owner, quality)
        reasons.extend(_leak_reasons(axes, exposures))
        if reasons:
            exclusion_counts.update(set(reasons))
            continue
        predicate_matches = high_risk_mechanism_matches(provider["content"])
        candidates.append({
            "sample_id": sample_id,
            "provider": provider,
            "owner": owner,
            "canonical_issuer_key": canonical_key,
            "canonical_issuer_resolution_quality": quality,
            "axes": axes,
            "predicate_matches": predicate_matches,
            "input_position": input_position,
        })

    by_issuer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        by_issuer[str(candidate["axes"]["canonical_issuer_key"])].append(candidate)

    stratum_representatives: dict[str, list[dict[str, Any]]] = {
        GENERAL_STRATUM: [],
        HIGH_RISK_STRATUM: [],
    }
    for issuer, issuer_candidates in by_issuer.items():
        enriched = [candidate for candidate in issuer_candidates if candidate["predicate_matches"]]
        stratum = HIGH_RISK_STRATUM if enriched else GENERAL_STRATUM
        representative_pool = enriched or issuer_candidates
        representative = min(
            representative_pool,
            key=lambda candidate: _rank(
                selection_salt, "ISSUER_REPRESENTATIVE", stratum, issuer, candidate["sample_id"],
            ),
        )
        representative = {**representative, "benchmark_stratum": stratum}
        stratum_representatives[stratum].append(representative)

    requested = {GENERAL_STRATUM: general_count, HIGH_RISK_STRATUM: high_risk_count}
    selected: list[dict[str, Any]] = []
    for stratum in (GENERAL_STRATUM, HIGH_RISK_STRATUM):
        ranked = sorted(
            stratum_representatives[stratum],
            key=lambda candidate: _rank(
                selection_salt, "STRATUM_SELECTION", stratum,
                candidate["canonical_issuer_key"], candidate["sample_id"],
            ),
        )
        if len(ranked) < requested[stratum]:
            raise ValueError(
                f"insufficient {stratum} unique issuers after isolation: "
                f"requested {requested[stratum]}, eligible {len(ranked)}"
            )
        for rank_number, candidate in enumerate(ranked[: requested[stratum]], 1):
            selected.append({**candidate, "selection_rank_in_stratum": rank_number})

    # Interleave the two strata by a separate hash so provider reviewers cannot
    # infer a stratum from row position.
    selected.sort(
        key=lambda candidate: _rank(selection_salt, "OUTPUT_ORDER", candidate["sample_id"])
    )

    selected_issuers = [str(candidate["axes"]["canonical_issuer_key"]) for candidate in selected]
    if len(set(selected_issuers)) != len(selected):
        raise AssertionError("selector produced duplicate canonical issuers")
    selected_overlap_counts: dict[str, int] = {}
    for axis in ("sample_id", "event_id", "entity_group", "canonical_issuer_key", "event_chain_group"):
        selected_overlap_counts[axis] = sum(
            1 for candidate in selected
            if candidate["axes"][axis] and candidate["axes"][axis] in exposures[axis]
        )
    selected_overlap_counts["content_hash"] = sum(
        1 for candidate in selected
        if candidate["axes"]["content_hash"] & exposures["content_hash"]
    )
    if any(selected_overlap_counts.values()):
        raise AssertionError(f"selected output overlaps exposure registry: {selected_overlap_counts}")

    provider_output_rows = [candidate["provider"] for candidate in selected]
    owner_output_rows: list[dict[str, Any]] = []
    for candidate in selected:
        owner_output_rows.append({
            **candidate["owner"],
            "canonical_issuer_key": candidate["canonical_issuer_key"],
            "canonical_issuer_resolution_quality": candidate["canonical_issuer_resolution_quality"],
            "benchmark_selection_contract": SELECTION_CONTRACT,
            "benchmark_stratum": candidate["benchmark_stratum"],
            "benchmark_stratum_predicate_version": PREDICATE_VERSION,
            "selection_rank_in_stratum": candidate["selection_rank_in_stratum"],
            "selection_hash_sha256": _rank(
                selection_salt, "STRATUM_SELECTION", candidate["benchmark_stratum"],
                candidate["canonical_issuer_key"], candidate["sample_id"],
            ),
            "high_risk_predicate_matches": candidate["predicate_matches"],
        })

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=output_dir.name + ".staging.", dir=output_dir.parent))
    try:
        provider_hash = _write_jsonl(staging / PROVIDER_OUTPUT, provider_output_rows)
        owner_hash = _write_jsonl(staging / OWNER_OUTPUT, owner_output_rows)
        legacy_path_hash = sha256_file(legacy_strict60_sample_ids)
        supersession = _legacy_supersession_template(
            legacy_path_hash=legacy_path_hash, legacy_ids=legacy_ids, selected_count=len(selected),
        )
        supersession_hash = _write_json(staging / SUPERSESSION_OUTPUT, supersession)

        stratum_counts = Counter(candidate["benchmark_stratum"] for candidate in selected)
        predicate_match_counts = Counter(
            match
            for candidate in selected
            for match in candidate["predicate_matches"]
        )
        selected_id_set_hash = sha256_bytes(
            (stable_json(sorted(candidate["sample_id"] for candidate in selected)) + "\n").encode("utf-8")
        )
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "selection_contract": SELECTION_CONTRACT,
            "status": "FROZEN_UNLABELED_AWAITING_INDEPENDENT_REVIEW",
            "classification": "UNLABELED_PRE_ADJUDICATION_SELECTION",
            "code_commit": code_commit or _git_commit(),
            "selector_script_sha256": sha256_file(Path(__file__).resolve()),
            "input_sha256": {
                "strict500_provider_input": sha256_file(strict500_provider_input),
                "strict500_owner_index": sha256_file(strict500_owner_index),
                "canonical_issuer_map": sha256_file(canonical_issuer_map),
                "training_exposure_registries": [
                    sha256_file(path) for path in training_exposure_registries
                ],
                "legacy_strict60_sample_ids": legacy_path_hash,
            },
            "input_counts": {
                "strict500_provider_rows": len(providers),
                "canonical_issuer_map_rows": len(canonical.rows),
                "legacy_strict60_sample_ids": len(legacy_ids),
                "training_exposure_rows": exposure_summary["row_count"],
                "training_exposure_unique_origin_samples": exposure_summary["unique_origin_sample_count"],
            },
            "selection": {
                "salt": selection_salt,
                "algorithm": "SHA256_RANK_ONE_REPRESENTATIVE_PER_CANONICAL_ISSUER_V1",
                "output_order": "SHA256_INTERLEAVED_ACROSS_STRATA",
                "target_rows": general_count + high_risk_count,
                "one_sample_per_canonical_issuer": True,
                "canonical_resolution_required": True,
                "allowed_candidate_resolution_qualities": ["PREFIX:STRONG"],
                "strong_parse_contract": {
                    "version": STRONG_PARSE_VERSION,
                    "requirements": [
                        "CANONICAL_ISSUER_RESOLUTION_QUALITY_EXPLICIT_STRONG",
                        "FOCAL_SUBJECT_ROLE_ISSUER",
                        "SOURCE_IDENTITY_HIDDEN_TRUE",
                        "PROVIDER_AND_OWNER_SOURCE_EXCERPT_COMPLETE_TRUE",
                        "NONEMPTY_HEADLINE",
                        "SOURCE_EVIDENCE_TEXT_AT_LEAST_80_CHARACTERS",
                    ],
                },
                "candidate_rows_after_strong_parse_and_isolation": len(candidates),
                "eligible_unique_canonical_issuers": len(by_issuer),
                "duplicate_issuer_candidate_rows_removed": len(candidates) - len(by_issuer),
                "exclusion_reason_counts": dict(sorted(exclusion_counts.items())),
            },
            "strata": {
                GENERAL_STRATUM: {
                    "definition": "issuer has no eligible sample matching the frozen high-risk predicate",
                    "requested_rows": general_count,
                    "eligible_unique_issuers": len(stratum_representatives[GENERAL_STRATUM]),
                    "selected_rows": stratum_counts[GENERAL_STRATUM],
                },
                HIGH_RISK_STRATUM: {
                    "definition": "issuer has at least one eligible sample matching a frozen document type, SEC item section, or mechanism-text regex",
                    "requested_rows": high_risk_count,
                    "eligible_unique_issuers": len(stratum_representatives[HIGH_RISK_STRATUM]),
                    "selected_rows": stratum_counts[HIGH_RISK_STRATUM],
                    "predicate": {
                        "version": PREDICATE_VERSION,
                        "signal_fields": [
                            "content.headline",
                            "content.summary",
                            "content.passages[].document_type",
                            "content.passages[].item_section",
                            "content.passages[].passage",
                        ],
                        "document_types": list(HIGH_RISK_DOCUMENT_TYPES),
                        "item_sections": list(HIGH_RISK_ITEM_SECTIONS),
                        "mechanism_regexes": [
                            {"name": name, "pattern": pattern}
                            for name, pattern in HIGH_RISK_MECHANISM_PATTERNS
                        ],
                        "selected_match_counts": dict(sorted(predicate_match_counts.items())),
                    },
                },
            },
            "exposure_isolation": {
                "axes": [
                    "sample_id",
                    "event_id",
                    "entity_group",
                    "canonical_issuer_key",
                    "event_chain_group",
                    "content_hash",
                ],
                "training_axis_unique_counts_before_legacy_union": training_axis_counts,
                "all_exposure_axis_unique_counts_after_legacy_union": {
                    axis: len(values) for axis, values in exposures.items()
                },
                "training_exposure_split_counts": exposure_summary["exposure_split_counts"],
                "legacy_strict60_all_axes_added_to_exposure_union": True,
                "legacy_strict60_resolved_for_exposure_only": True,
                "legacy_strict60_resolution_quality_counts": dict(
                    sorted(legacy_resolution_quality_counts.items())
                ),
                "selected_overlap_counts": selected_overlap_counts,
                "all_selected_overlap_counts_zero": not any(selected_overlap_counts.values()),
            },
            "metrics_reporting_contract": {
                "required_views": ["OVERALL", GENERAL_STRATUM, HIGH_RISK_STRATUM],
                "overall_denominator": len(selected),
                "stratum_denominators": dict(sorted(stratum_counts.items())),
                "report_overall_and_each_stratum_separately": True,
            },
            "output": {
                "provider_input": {
                    "file": PROVIDER_OUTPUT,
                    "row_count": len(provider_output_rows),
                    "sha256": provider_hash,
                    "fields": ["sample_id", "content"],
                    "stratum_exposed_to_provider": False,
                },
                "owner_index": {
                    "file": OWNER_OUTPUT,
                    "row_count": len(owner_output_rows),
                    "sha256": owner_hash,
                },
                "legacy_supersession_template": {
                    "file": SUPERSESSION_OUTPUT,
                    "sha256": supersession_hash,
                    "status": "CONSUMED_DIAGNOSTIC_AI_REFERENCE_NOT_HUMAN_GOLD",
                },
                "selected_sample_id_sorted_set_sha256": selected_id_set_hash,
            },
            "signal_policy": {
                "selection_inputs_used": [
                    "FROZEN_SALT",
                    "SOURCE_TEXT",
                    "SOURCE_DOCUMENT_TYPE",
                    "SOURCE_ITEM_SECTION",
                    "SOURCE_COMPLETENESS_FIELDS",
                    "FOCAL_SUBJECT_ROLE",
                    "OWNER_IDENTITY_AND_CONTENT_HASH_AXES",
                    "CANONICAL_ISSUER_MAP",
                    "EXPOSURE_REGISTRY",
                ],
                "category_balancing_used": False,
                "model_output_sampling_used": False,
                "price_or_market_outcome_sampling_used": False,
            },
            "provenance": {
                "labels_read": False,
                "reviewer_a_b_labels_read": False,
                "arbiter_labels_read": False,
                "qwen_predictions_read": False,
                "other_model_predictions_read": False,
                "market_outcomes_read": False,
                "prices_used": False,
                "human_gold_claimed": False,
                "legacy_v1_classification": "AI_NOT_HUMAN_GOLD",
                "legacy_strict60_resolution_quality_counts": dict(
                    sorted(legacy_resolution_quality_counts.items())
                ),
            },
            "production_model_changed": False,
        }
        manifest_hash = _write_json(staging / MANIFEST_OUTPUT, manifest)
        _atomic_write(
            staging / (MANIFEST_OUTPUT + ".sha256"),
            f"{manifest_hash}  {MANIFEST_OUTPUT}\n".encode("ascii"),
        )
        os.replace(staging, output_dir)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


# Short alias for callers that do not need the version in the function name.
select_benchmark = select_issuer_isolated_benchmark_v2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict500-provider-input", type=Path, required=True)
    parser.add_argument("--strict500-owner-index", type=Path, required=True)
    parser.add_argument("--canonical-issuer-map", type=Path, required=True)
    parser.add_argument(
        "--training-exposure-registry", type=Path, action="append", required=True,
        help="Repeat for every frozen train/dev/exposure registry.",
    )
    parser.add_argument("--legacy-strict60-sample-ids", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--selection-salt", default=DEFAULT_SELECTION_SALT)
    parser.add_argument("--general-count", type=int, default=DEFAULT_GENERAL_COUNT)
    parser.add_argument("--high-risk-count", type=int, default=DEFAULT_HIGH_RISK_COUNT)
    parser.add_argument("--expected-legacy-count", type=int, default=DEFAULT_LEGACY_STRICT_COUNT)
    parser.add_argument("--code-commit")
    args = parser.parse_args()
    manifest = select_issuer_isolated_benchmark_v2(
        strict500_provider_input=args.strict500_provider_input.resolve(),
        strict500_owner_index=args.strict500_owner_index.resolve(),
        canonical_issuer_map=args.canonical_issuer_map.resolve(),
        training_exposure_registries=[path.resolve() for path in args.training_exposure_registry],
        legacy_strict60_sample_ids=args.legacy_strict60_sample_ids.resolve(),
        output_dir=args.output_dir.resolve(),
        selection_salt=args.selection_salt,
        general_count=args.general_count,
        high_risk_count=args.high_risk_count,
        expected_legacy_count=args.expected_legacy_count,
        code_commit=args.code_commit,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
