#!/usr/bin/env python3
"""Build a deterministic, label-free canonical issuer map for Qwen SFT data.

The map is owner-side metadata.  It reads only candidate input content and the
unlabelled strict provider/index pair.  SEC CIKs are accepted from structured
focal-subject fields or from headline roles ``(Filer)`` and ``(Subject)``.
An exchange CIK in a ``(Filed by)`` headline is deliberately ignored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from zipfile import ZipFile


SCHEMA_VERSION = 1
CANONICAL_KEY_PREFIX = "issuer:v1:sec_cik:"
RAW_IDENTITY_FIELDS = (
    "event_id", "event_version", "stable_id", "ticker_at_event", "company_name",
)
STATIC_PROXY_NAMES = {
    "nasdaq stock market llc",
    "the nasdaq stock market llc",
    "new york stock exchange llc",
    "nyse american llc",
    "nyse arca inc",
    "cboe bzx exchange inc",
    "cboe exchange inc",
    "otc markets group inc",
}
HEADLINE_CIK_RE = re.compile(
    r"\((?P<cik>\d{1,10})\)\s*\((?P<role>Filer|Subject|Filed\s+by)\)",
    re.IGNORECASE,
)
SEC_HEADLINE_NAME_RE = re.compile(
    r"^[^-]+-\s*(?P<name>.+?)\s*\(\d{1,10}\)\s*"
    r"\((?P<name_role>Filer|Subject|Filed\s+by)\)",
    re.IGNORECASE,
)
OFFICIAL_EVIDENCE_TICKER_RE = re.compile(
    r"\baccepted\s+official\s+evidence\s+for\s+(?P<ticker>[A-Z][A-Z0-9.\-]{0,14})\b",
    re.IGNORECASE,
)


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{number}: row is not an object")
        rows.append(value)
    return rows


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_raw_identity_packets(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Load only identity fields from the frozen team packet archive."""
    packet_map: dict[str, dict[str, Any]] = {}
    source_names: list[str] = []
    duplicate_rows = 0
    mismatched_duplicate_packets = 0
    with ZipFile(path) as archive:
        names = sorted(
            name for name in archive.namelist()
            if name.endswith(".input.jsonl") and "/任务分片/" in name
        )
        if not names:
            raise ValueError(f"{path}: no task-shard .input.jsonl files")
        for name in names:
            source_names.append(name)
            text = archive.read(name).decode("utf-8-sig")
            for number, line in enumerate(text.splitlines(), 1):
                if not line.strip():
                    continue
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError(f"{path}:{name}:{number}: raw packet is not an object")
                event_id = _identifier(raw.get("event_id"))
                if not event_id:
                    continue
                packet = {field: raw.get(field) for field in RAW_IDENTITY_FIELDS}
                packet["event_id"] = event_id
                if event_id in packet_map:
                    duplicate_rows += 1
                    if stable_json(packet_map[event_id]) != stable_json(packet):
                        mismatched_duplicate_packets += 1
                    continue
                packet_map[event_id] = packet
    if mismatched_duplicate_packets:
        raise ValueError(f"{path}: duplicate raw event identities differ: {mismatched_duplicate_packets}")
    return packet_map, {
        "source_shards": len(source_names),
        "unique_event_packets": len(packet_map),
        "duplicate_event_packet_rows": duplicate_rows,
        "mismatched_duplicate_packets": mismatched_duplicate_packets,
        "source_names_sha256": sha256_bytes(stable_json(source_names).encode("utf-8")),
    }


def _read_records(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8-sig").strip()
    if not raw:
        return []
    values = json.loads(raw) if raw.startswith("[") else [json.loads(line) for line in raw.splitlines() if line.strip()]
    if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
        raise ValueError(f"{path}: expected a JSON array or JSONL objects")
    return values


def _identifier(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _cik(value: Any) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{1,10}", text):
        raise ValueError(f"invalid SEC CIK: {value!r}")
    return text.zfill(10)


def _ticker(value: Any) -> str:
    return re.sub(r"[^A-Z0-9.\-]", "", str(value or "").upper())


def _name(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.findall(r"[a-z0-9]+", normalized))


def _token(kind: str, value: Any) -> str | None:
    normalized_kind = str(kind or "").strip().casefold()
    if normalized_kind in {"cik", "sec_cik"}:
        return f"cik:{_cik(value)}"
    if normalized_kind in {"ticker", "symbol"}:
        normalized = _ticker(value)
        return f"ticker:{normalized}" if normalized else None
    if normalized_kind in {"name", "legal_name", "display_name"}:
        normalized = _name(value)
        return f"name:{normalized}" if normalized else None
    if normalized_kind in {"sample", "sample_id", "event", "event_id"}:
        normalized = _identifier(value)
        prefix = "sample" if normalized_kind.startswith("sample") else "event"
        return f"{prefix}:{normalized}" if normalized else None
    raise ValueError(f"unsupported alias identity type: {kind!r}")


def _load_aliases(path: Path | None) -> tuple[dict[str, str], int]:
    if path is None:
        return {}, 0
    token_to_key: dict[str, str] = {}
    rows = _read_records(path)
    for number, row in enumerate(rows, 1):
        key = _identifier(row.get("canonical_issuer_key"))
        if not key:
            raise ValueError(f"{path}:{number}: alias row missing canonical_issuer_key")
        tokens: set[str] = set()
        for raw in row.get("identity_tokens") or []:
            text = _identifier(raw)
            if not text or ":" not in text:
                raise ValueError(f"{path}:{number}: invalid identity token {raw!r}")
            kind, value = text.split(":", 1)
            token = _token(kind, value)
            if token:
                tokens.add(token)
        for raw in row.get("aliases") or []:
            if not isinstance(raw, dict):
                raise ValueError(f"{path}:{number}: aliases must contain objects")
            token = _token(str(raw.get("type") or ""), raw.get("value"))
            if token:
                tokens.add(token)
        for field, kind in (
            ("ciks", "cik"), ("tickers", "ticker"), ("names", "name"),
            ("sample_ids", "sample_id"), ("event_ids", "event_id"),
        ):
            for raw in row.get(field) or []:
                token = _token(kind, raw)
                if token:
                    tokens.add(token)
        if not tokens:
            raise ValueError(f"{path}:{number}: alias row has no identities")
        for token in sorted(tokens):
            previous = token_to_key.get(token)
            if previous is not None and previous != key:
                raise ValueError(f"{path}:{number}: conflicting alias token {token}: {previous} != {key}")
            token_to_key[token] = key
    return token_to_key, len(rows)


def _candidate_content(row: dict[str, Any], path: Path, number: int) -> tuple[str, str, dict[str, Any]]:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    sample_id = _identifier(metadata.get("sample_id") or row.get("sample_id"))
    event_id = _identifier(metadata.get("event_id") or metadata.get("source_event_id") or row.get("event_id"))
    if not sample_id or not event_id:
        raise ValueError(f"{path}:{number}: candidate missing sample_id or event_id")
    if isinstance(row.get("content"), dict):
        content = row["content"]
    else:
        messages = row.get("messages") or []
        user = [message for message in messages if message.get("role") == "user"]
        if not user:
            raise ValueError(f"{path}:{number}: candidate has no user content")
        content = json.loads(str(user[-1].get("content") or "{}"))
    if not isinstance(content, dict):
        raise ValueError(f"{path}:{number}: candidate content is not an object")
    return sample_id, event_id, content


def _strict_observations(provider_path: Path, index_path: Path) -> list[tuple[str, str, dict[str, Any]]]:
    providers: dict[str, dict[str, Any]] = {}
    for number, row in enumerate(read_jsonl(provider_path), 1):
        sample_id = _identifier(row.get("sample_id"))
        if not sample_id or sample_id in providers:
            raise ValueError(f"{provider_path}:{number}: missing or duplicate sample_id {sample_id!r}")
        content = row.get("content")
        if not isinstance(content, dict):
            raise ValueError(f"{provider_path}:{number}: content is not an object")
        providers[sample_id] = content
    indices: dict[str, str] = {}
    for number, row in enumerate(read_jsonl(index_path), 1):
        sample_id = _identifier(row.get("sample_id"))
        event_id = _identifier(row.get("source_event_id") or row.get("event_id"))
        if not sample_id or not event_id or sample_id in indices:
            raise ValueError(f"{index_path}:{number}: missing identity or duplicate sample_id {sample_id!r}")
        indices[sample_id] = event_id
    if providers.keys() != indices.keys():
        missing_provider = sorted(indices.keys() - providers.keys())
        missing_index = sorted(providers.keys() - indices.keys())
        raise ValueError(
            "strict provider/index sample mismatch: "
            f"missing_provider={missing_provider[:5]}, missing_index={missing_index[:5]}"
        )
    return [(sample_id, indices[sample_id], providers[sample_id]) for sample_id in sorted(indices)]


def _subject_objects(content: dict[str, Any]) -> list[dict[str, Any]]:
    subjects: list[dict[str, Any]] = []
    direct = content.get("focal_subject")
    if isinstance(direct, dict):
        subjects.append(direct)
    semantic = content.get("semantic_context")
    if isinstance(semantic, dict) and isinstance(semantic.get("focal_subject"), dict):
        subjects.append(semantic["focal_subject"])
    return subjects


def _identity_evidence(content: dict[str, Any]) -> tuple[set[str], dict[str, set[str]], list[str]]:
    tokens: set[str] = set()
    ciks: dict[str, set[str]] = {}
    ignored: list[str] = []

    def add_cik(value: Any, provenance: str) -> None:
        normalized = _cik(value)
        ciks.setdefault(normalized, set()).add(provenance)
        tokens.add(f"cik:{normalized}")

    for subject in _subject_objects(content):
        for field in ("cik", "sec_cik", "issuer_cik"):
            if subject.get(field):
                add_cik(subject[field], f"STRUCTURED_FOCAL_SUBJECT_{field.upper()}")
        for field in ("ticker", "symbol"):
            token = _token("ticker", subject.get(field))
            if token:
                tokens.add(token)
        for field in ("display_name", "name", "legal_name"):
            token = _token("name", subject.get(field))
            if token:
                tokens.add(token)

    headline = str(content.get("headline") or "")
    for match in HEADLINE_CIK_RE.finditer(headline):
        normalized = _cik(match.group("cik"))
        role = re.sub(r"\s+", "_", match.group("role").upper())
        if role == "FILED_BY":
            ignored.append(f"HEADLINE_FILED_BY_CIK_IGNORED:{normalized}")
        else:
            add_cik(normalized, f"HEADLINE_{role}_CIK")
    name_match = SEC_HEADLINE_NAME_RE.search(headline)
    if name_match and re.sub(r"\s+", "_", name_match.group("name_role").upper()) != "FILED_BY":
        token = _token("name", name_match.group("name"))
        if token:
            tokens.add(token)
    ticker_match = OFFICIAL_EVIDENCE_TICKER_RE.search(headline)
    if ticker_match:
        token = _token("ticker", ticker_match.group("ticker"))
        if token:
            tokens.add(token)
    return tokens, ciks, ignored


def _stable_token(value: Any) -> str | None:
    text = _identifier(value)
    if not text:
        return None
    kind, separator, raw_value = text.partition(":")
    normalized_kind = kind.casefold()
    if separator and normalized_kind == "cik":
        return f"cik:{_cik(raw_value)}"
    if separator and normalized_kind == "permaticker":
        normalized = _identifier(raw_value)
        return f"permaticker:{normalized.casefold()}" if normalized else None
    normalized = _name(text)
    return f"stable:{normalized}" if normalized else None


def _raw_tokens(packet: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    stable = _stable_token(packet.get("stable_id"))
    ticker = _token("ticker", packet.get("ticker_at_event"))
    name = _token("name", packet.get("company_name"))
    if stable:
        tokens.add(stable)
    if ticker:
        tokens.add(ticker)
    if name:
        tokens.add(name)
    return tokens


def _anchor_kind(token: str) -> str:
    return token.split(":", 1)[0]


class IdentityUnion:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, first: str, second: str) -> None:
        left, right = self.find(first), self.find(second)
        if left != right:
            self.parent[max(left, right)] = min(left, right)


def _component_key(anchors: set[str]) -> tuple[str, str, list[str]]:
    ciks = sorted(token.split(":", 1)[1] for token in anchors if token.startswith("cik:"))
    permatickers = sorted(
        token.split(":", 1)[1] for token in anchors if token.startswith("permaticker:")
    )
    stable = sorted(token.split(":", 1)[1] for token in anchors if token.startswith("stable:"))
    if len(ciks) > 1 or len(permatickers) > 1 or len(stable) > 1:
        raise ValueError(f"raw identity component has conflicting stable anchors: {sorted(anchors)}")
    if ciks:
        return f"{CANONICAL_KEY_PREFIX}{ciks[0]}", "STRONG_RAW_CIK", [
            f"RAW_IDENTITY_GRAPH_CIK:{ciks[0]}"
        ]
    if permatickers:
        return f"issuer:v1:permaticker:{permatickers[0]}", "STRONG_RAW_PERMATICKER", [
            f"RAW_IDENTITY_GRAPH_PERMATICKER:{permatickers[0]}"
        ]
    if stable:
        digest = sha256_bytes(stable[0].encode("utf-8"))
        return f"issuer:v1:stable:{digest}", "STRONG_RAW_STABLE_ID", [
            "RAW_IDENTITY_GRAPH_STABLE_ID"
        ]
    raise ValueError("raw identity component has no stable anchor")


def build_raw_issuer_graph(
    raw_packets: dict[str, dict[str, Any]], observations: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Resolve every raw event through a conservative CIK/permaticker/ticker/name graph."""
    observation_ciks: dict[str, set[str]] = {}
    for sample_id, observation in observations.items():
        event_id = observation["event_id"]
        if event_id not in raw_packets:
            raise ValueError(f"raw packet missing for {sample_id}/{event_id}")
        ciks: set[str] = set()
        for content in observation["contents"]:
            _tokens, content_ciks, _ignored = _identity_evidence(content)
            ciks.update(content_ciks)
        if len(ciks) > 1:
            raise ValueError(f"conflicting content CIKs for raw event {event_id}: {sorted(ciks)}")
        observation_ciks.setdefault(event_id, set()).update(ciks)

    event_anchors: dict[str, set[str]] = {}
    all_anchors: set[str] = set()
    for event_id, packet in raw_packets.items():
        anchors: set[str] = set()
        stable = _stable_token(packet.get("stable_id"))
        if stable:
            anchors.add(stable)
        anchors.update(f"cik:{value}" for value in observation_ciks.get(event_id, set()))
        event_anchors[event_id] = anchors
        all_anchors.update(anchors)

    name_tickers: dict[str, set[str]] = {}
    ticker_ciks: dict[str, set[str]] = {}
    for event_id, packet in raw_packets.items():
        ticker = _ticker(packet.get("ticker_at_event"))
        name = _name(packet.get("company_name"))
        if name and ticker:
            name_tickers.setdefault(name, set()).add(ticker)
        if ticker:
            for anchor in event_anchors[event_id]:
                if anchor.startswith("cik:"):
                    ticker_ciks.setdefault(ticker, set()).add(anchor.split(":", 1)[1])
    rejected_names = STATIC_PROXY_NAMES | {
        name for name, tickers in name_tickers.items() if len(tickers) > 3
    }
    rejected_tickers = {
        ticker for ticker, ciks in ticker_ciks.items() if len(ciks) > 1
    }
    usable_event_tokens: dict[str, set[str]] = {}
    rejected_event_reasons: dict[str, set[str]] = {}
    for event_id, packet in raw_packets.items():
        tokens = _raw_tokens(packet)
        usable = set(tokens)
        reasons: set[str] = set()
        for token in tokens:
            if token.startswith("name:") and token.split(":", 1)[1] in rejected_names:
                usable.discard(token)
                reasons.add("RAW_PROXY_OR_MULTI_TICKER_NAME_IGNORED")
            if token.startswith("ticker:") and token.split(":", 1)[1] in rejected_tickers:
                usable.discard(token)
                reasons.add("RAW_MULTI_CIK_TICKER_IGNORED")
        usable_event_tokens[event_id] = usable
        rejected_event_reasons[event_id] = reasons

    union = IdentityUnion(all_anchors)
    for anchors in event_anchors.values():
        ordered = sorted(anchors)
        for anchor in ordered[1:]:
            union.union(ordered[0], anchor)

    # A name/ticker bridge may join different identifier systems (for example,
    # a CIK packet and a permaticker packet), but never two anchors of the same
    # type.  Same-type ambiguity is left unresolved instead of guessed.
    bridge_groups: dict[str, dict[str, set[str]]] = {
        "name": {}, "ticker": {}, "ticker_name": {},
    }
    for bridge in bridge_groups:
        groups: dict[str, set[str]] = {}
        for event_id, packet in raw_packets.items():
            ticker = _ticker(packet.get("ticker_at_event"))
            name = _name(packet.get("company_name"))
            if name in rejected_names:
                name = ""
            if ticker in rejected_tickers:
                ticker = ""
            if bridge == "name":
                value = name
            elif bridge == "ticker":
                value = ticker
            else:
                value = f"{ticker}|{name}" if ticker and name else ""
            if value and event_anchors[event_id]:
                groups.setdefault(value, set()).update(event_anchors[event_id])
        bridge_groups[bridge] = groups
        for anchors in groups.values():
            kind_counts = Counter(_anchor_kind(anchor) for anchor in anchors)
            if len(anchors) > 1 and all(count == 1 for count in kind_counts.values()):
                ordered = sorted(anchors)
                for anchor in ordered[1:]:
                    union.union(ordered[0], anchor)

    components: dict[str, set[str]] = {}
    for anchor in all_anchors:
        components.setdefault(union.find(anchor), set()).add(anchor)
    component_resolution = {
        root: _component_key(anchors) for root, anchors in components.items()
    }

    event_roots: dict[str, str] = {}
    for event_id, anchors in event_anchors.items():
        roots = {union.find(anchor) for anchor in anchors}
        if len(roots) > 1:
            raise ValueError(f"raw event {event_id} has conflicting issuer components: {sorted(roots)}")
        if roots:
            event_roots[event_id] = next(iter(roots))

    token_roots: dict[str, set[str]] = {}
    for event_id, packet in raw_packets.items():
        root = event_roots.get(event_id)
        if not root:
            continue
        for token in usable_event_tokens[event_id]:
            if token.startswith(("ticker:", "name:")):
                token_roots.setdefault(token, set()).add(root)

    resolved: dict[str, dict[str, Any]] = {}
    for event_id, packet in raw_packets.items():
        tokens = usable_event_tokens[event_id]
        root = event_roots.get(event_id)
        provenance: list[str] = sorted(rejected_event_reasons[event_id])
        if root:
            key, quality, base = component_resolution[root]
            provenance.extend(base)
        else:
            linked_roots: set[str] = set()
            ambiguous_tokens: list[str] = []
            for token in sorted(tokens):
                roots = token_roots.get(token, set())
                if len(roots) == 1:
                    linked_roots.update(roots)
                elif len(roots) > 1:
                    ambiguous_tokens.append(token)
            if len(linked_roots) == 1:
                root = next(iter(linked_roots))
                key, quality, base = component_resolution[root]
                provenance.extend([*base, "RAW_GRAPH_WEAK_IDENTITY_TO_STABLE_ANCHOR"])
            elif len(linked_roots) > 1:
                key, quality = None, "AMBIGUOUS_RAW_GRAPH"
                provenance.append("RAW_GRAPH_TICKER_NAME_CONFLICT")
            else:
                name_token = next((token for token in tokens if token.startswith("name:")), None)
                ticker_token = next((token for token in tokens if token.startswith("ticker:")), None)
                if name_token and not ambiguous_tokens:
                    digest = sha256_bytes(name_token.encode("utf-8"))
                    key, quality = f"issuer:v1:raw_name:{digest}", "PROVISIONAL_RAW_NAME"
                    provenance.append("RAW_PACKET_UNIQUE_NORMALIZED_COMPANY_NAME")
                elif ticker_token and not ambiguous_tokens:
                    key = f"issuer:v1:raw_ticker:{ticker_token.split(':', 1)[1]}"
                    quality = "PROVISIONAL_RAW_TICKER"
                    provenance.append("RAW_PACKET_UNAMBIGUOUS_TICKER_AT_EVENT")
                else:
                    key, quality = None, "UNRESOLVED"
                    provenance.append("RAW_IDENTITY_MISSING_OR_AMBIGUOUS")
        resolved[event_id] = {
            "canonical_issuer_key": key,
            "resolution_quality": quality,
            "resolution_provenance": sorted(set(provenance)),
            "identity_tokens": sorted(tokens),
        }
    return resolved


def _resolve(
    *, sample_id: str, event_id: str, contents: Iterable[dict[str, Any]], aliases: dict[str, str],
    raw_resolution: dict[str, Any],
) -> tuple[str | None, list[str], str, list[str]]:
    tokens = {
        f"sample:{sample_id}", f"event:{event_id}",
        *raw_resolution.get("identity_tokens", []),
    }
    ciks: dict[str, set[str]] = {}
    ignored: set[str] = set()
    for content in contents:
        content_tokens, content_ciks, content_ignored = _identity_evidence(content)
        tokens.update(content_tokens)
        ignored.update(content_ignored)
        for cik, provenance in content_ciks.items():
            ciks.setdefault(cik, set()).update(provenance)
    if len(ciks) > 1:
        raise ValueError(f"conflicting CIK evidence for {sample_id}/{event_id}: {sorted(ciks)}")
    alias_keys = {aliases[token] for token in tokens if token in aliases}
    if len(alias_keys) > 1:
        raise ValueError(f"conflicting frozen aliases for {sample_id}/{event_id}: {sorted(alias_keys)}")
    strong_key = f"{CANONICAL_KEY_PREFIX}{next(iter(ciks))}" if ciks else None
    raw_key = _identifier(raw_resolution.get("canonical_issuer_key"))
    raw_is_cik = bool(raw_key and raw_key.startswith(CANONICAL_KEY_PREFIX))
    if strong_key and raw_is_cik and raw_key != strong_key:
        raise ValueError(
            f"content/raw CIK conflict for {sample_id}/{event_id}: {strong_key} != {raw_key}"
        )
    if not strong_key and raw_is_cik:
        strong_key = raw_key
    alias_key = next(iter(alias_keys), None)
    if strong_key and alias_key and strong_key != alias_key:
        raise ValueError(
            f"CIK/alias conflict for {sample_id}/{event_id}: {strong_key} != {alias_key}"
        )
    if strong_key:
        provenance = {
            item for values in ciks.values() for item in values
        } | set(raw_resolution.get("resolution_provenance") or []) | ignored
        return strong_key, sorted(provenance), "STRONG_CIK", sorted(tokens)
    if alias_key:
        matched = sorted(token for token in tokens if aliases.get(token) == alias_key)
        provenance = {
            *(f"FROZEN_ALIAS:{token}" for token in matched),
            *raw_resolution.get("resolution_provenance", []),
            *ignored,
        }
        return alias_key, sorted(provenance), "STRONG_FROZEN_ALIAS", sorted(tokens)
    if raw_key:
        provenance = set(raw_resolution.get("resolution_provenance") or []) | ignored
        return (
            raw_key, sorted(provenance),
            str(raw_resolution.get("resolution_quality") or "RAW_IDENTITY_GRAPH"), sorted(tokens),
        )
    provenance = set(raw_resolution.get("resolution_provenance") or []) | ignored
    unresolved_quality = str(raw_resolution.get("resolution_quality") or "UNRESOLVED")
    return (
        None,
        sorted(provenance or {"NO_VALID_ISSUER_IDENTITY"}),
        unresolved_quality,
        sorted(tokens),
    )


def build_canonical_issuer_map(
    *, candidate_sft: list[Path], strict_provider_input: Path, strict_owner_index: Path,
    raw_packet_zip: Path, output: Path, alias_file: Path | None = None,
) -> dict[str, Any]:
    aliases, alias_group_count = _load_aliases(alias_file)
    observations: dict[str, dict[str, Any]] = {}

    def add(sample_id: str, event_id: str, content: dict[str, Any], source_kind: str) -> None:
        existing = observations.get(sample_id)
        if existing is not None and existing["event_id"] != event_id:
            raise ValueError(
                f"sample_id {sample_id} has conflicting event_ids: {existing['event_id']} != {event_id}"
            )
        if existing is None:
            existing = {"event_id": event_id, "contents": [], "source_kinds": set()}
            observations[sample_id] = existing
        existing["contents"].append(content)
        existing["source_kinds"].add(source_kind)

    for path in candidate_sft:
        for number, row in enumerate(read_jsonl(path), 1):
            sample_id, event_id, content = _candidate_content(row, path, number)
            add(sample_id, event_id, content, "CANDIDATE_SFT")
    for sample_id, event_id, content in _strict_observations(strict_provider_input, strict_owner_index):
        add(sample_id, event_id, content, "STRICT_PROVIDER_INPUT")

    raw_packets, raw_stats = load_raw_identity_packets(raw_packet_zip)
    raw_resolutions = build_raw_issuer_graph(raw_packets, observations)

    rows: list[dict[str, Any]] = []
    for sample_id in sorted(observations):
        observation = observations[sample_id]
        key, provenance, quality, tokens = _resolve(
            sample_id=sample_id,
            event_id=observation["event_id"],
            contents=observation["contents"],
            aliases=aliases,
            raw_resolution=raw_resolutions[observation["event_id"]],
        )
        rows.append({
            "schema_version": SCHEMA_VERSION,
            "sample_id": sample_id,
            "event_id": observation["event_id"],
            "canonical_issuer_key": key,
            "resolution_provenance": provenance,
            "resolution_quality": quality,
            "identity_tokens": tokens,
            "source_kinds": sorted(observation["source_kinds"]),
        })

    payload = "".join(stable_json(row) + "\n" for row in rows).encode("utf-8")
    _atomic_write(output, payload)
    digest = sha256_bytes(payload)
    _atomic_write(Path(str(output) + ".sha256"), f"{digest}  {output.name}\n".encode("ascii"))
    qualities = Counter(row["resolution_quality"] for row in rows)
    key_resolved_rows = sum(bool(row.get("canonical_issuer_key")) for row in rows)
    strong_resolved_rows = sum(
        bool(row.get("canonical_issuer_key"))
        and str(row.get("resolution_quality") or "").startswith("STRONG_")
        for row in rows
    )
    provisional_resolved_rows = sum(
        bool(row.get("canonical_issuer_key"))
        and str(row.get("resolution_quality") or "").startswith("PROVISIONAL_")
        for row in rows
    )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "rows": len(rows),
        "resolved_rows": key_resolved_rows,
        "strong_resolved_rows": strong_resolved_rows,
        "provisional_resolved_rows": provisional_resolved_rows,
        "unresolved_rows": len(rows) - key_resolved_rows,
        "quality_counts": dict(sorted(qualities.items())),
        "alias_groups": alias_group_count,
        "alias_tokens": len(aliases),
        "raw_packet_zip": str(raw_packet_zip),
        "raw_packet_zip_sha256": sha256_file(raw_packet_zip),
        "raw_packet_stats": raw_stats,
        "input_sha256": {
            "candidate_sft": {str(path): sha256_file(path) for path in candidate_sft},
            "strict_provider_input": sha256_file(strict_provider_input),
            "strict_owner_index": sha256_file(strict_owner_index),
            "raw_packet_zip": sha256_file(raw_packet_zip),
            "alias_file": sha256_file(alias_file) if alias_file else None,
        },
        "output": str(output),
        "output_sha256": digest,
        "labels_read": False,
    }
    manifest_path = Path(str(output) + ".manifest.json")
    _atomic_write(
        manifest_path,
        (json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return {**summary, "manifest": str(manifest_path), "manifest_sha256": sha256_file(manifest_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-sft", type=Path, action="append", default=[])
    parser.add_argument("--strict-provider-input", type=Path, required=True)
    parser.add_argument("--strict-owner-index", type=Path, required=True)
    parser.add_argument("--raw-packet-zip", type=Path, required=True)
    parser.add_argument("--alias-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = build_canonical_issuer_map(
        candidate_sft=[path.resolve() for path in args.candidate_sft],
        strict_provider_input=args.strict_provider_input.resolve(),
        strict_owner_index=args.strict_owner_index.resolve(),
        raw_packet_zip=args.raw_packet_zip.resolve(),
        alias_file=args.alias_file.resolve() if args.alias_file else None,
        output=args.output.resolve(),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
