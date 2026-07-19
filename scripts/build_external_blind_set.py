#!/usr/bin/env python3
"""Freeze a label-first external blind set from official sources.

The dataset is collected after the model artifact has been frozen. Expected
labels come only from a source/family policy declared below; model inference is
not imported or executed by this script. The evaluator is intentionally a
separate command so labels and bytes exist before predictions.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.train_risk_router import load_dataset  # noqa: E402


DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_MODEL_CARD = ROOT / "artifacts" / "risk_router_model_card.json"
DEFAULT_DATASET = ROOT / "artifacts" / "risk_router_external_blind_v1.jsonl"
DEFAULT_FREEZE = ROOT / "artifacts" / "risk_router_external_blind_v1_freeze.json"
DEFAULT_RAW_DIR = ROOT / "artifacts" / "external_blind_raw_v1"
DEFAULT_ENV = ROOT / ".env"
POLICY_VERSION = "external-blind-source-policy-v1"


@dataclass(frozen=True)
class SourcePolicy:
    source_id: str
    name: str
    feed_url: str
    authority_tier: str
    expected_label: str
    label_basis: str
    include: Callable[[dict[str, Any]], bool]
    sample_count: int


def _always(_entry: dict[str, Any]) -> bool:
    return True


def _fed_non_enforcement(entry: dict[str, Any]) -> bool:
    text = f"{entry.get('title', '')} {entry.get('canonical_url', '')}".lower()
    blocked = (
        "enforcement",
        "civil money penalty",
        "prohibition order",
        "termination of enforcement",
    )
    return not any(term in text for term in blocked)


def _nvidia_non_adverse(entry: dict[str, Any]) -> bool:
    text = f"{entry.get('title', '')} {entry.get('summary', '')}".lower()
    adverse = ("recall", "lawsuit", "investigation", "breach", "layoff", "delist", "default")
    return not any(term in text for term in adverse)


SOURCE_POLICIES = (
    SourcePolicy(
        "sec_litigation_external",
        "SEC litigation releases",
        "https://www.sec.gov/enforcement-litigation/litigation-releases/rss",
        "P0",
        "RISK_REVIEW",
        "Official SEC civil-action release; route for adverse-risk human review.",
        _always,
        15,
    ),
    SourcePolicy(
        "cftc_enforcement_external",
        "CFTC enforcement press releases",
        "https://www.cftc.gov/RSS/RSSENF/rssenf.xml",
        "P0",
        "RISK_REVIEW",
        "Official CFTC enforcement release; route for adverse-risk human review.",
        _always,
        5,
    ),
    SourcePolicy(
        "federal_reserve_policy_external",
        "Federal Reserve non-enforcement press releases",
        "https://www.federalreserve.gov/feeds/press_all.xml",
        "P0",
        "NON_TARGET",
        "Official macro, policy or supervisory-information release outside issuer downside-risk routing.",
        _fed_non_enforcement,
        3,
    ),
    SourcePolicy(
        "nvidia_press_external",
        "NVIDIA official press releases",
        "https://nvidianews.nvidia.com/releases.xml",
        "P1",
        "NON_TARGET",
        "Official non-adverse company announcement retained as favorable/neutral control.",
        _nvidia_non_adverse,
        17,
    ),
)


XML_INVALID_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
XML_UNSAFE_AMPERSAND = re.compile(
    r"&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9A-Fa-f]+;)"
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def display_path(path: Path) -> str:
    try:
        value = path.resolve().relative_to(ROOT.resolve())
    except ValueError:
        value = path.resolve()
    return str(value).replace("\\", "/")


def load_dotenv_value(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            return value.strip().strip('"').strip("'") or None
    return None


def strip_markup(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"<br\s*/?>", " ", value, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html.unescape(text).split())


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value.strip() or None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat()


def parse_rss(body: bytes) -> tuple[list[dict[str, Any]], bool]:
    repaired = False
    try:
        root = ET.fromstring(body)
    except ET.ParseError as original:
        text = body.decode("utf-8-sig", errors="replace")
        fixed = XML_INVALID_CONTROL.sub("", text)
        fixed = XML_UNSAFE_AMPERSAND.sub("&amp;", fixed)
        if fixed == text:
            raise original
        root = ET.fromstring(fixed.encode("utf-8"))
        repaired = True
    entries: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        def text_of(tag: str) -> str:
            element = item.find(tag)
            return "" if element is None else "".join(element.itertext()).strip()

        title = strip_markup(text_of("title"))
        link = strip_markup(text_of("link"))
        guid = strip_markup(text_of("guid"))
        summary = strip_markup(text_of("description")) or title
        published = parse_date(text_of("pubDate"))
        external_id = guid or link or hashlib.sha256(
            f"{title}|{published}".encode("utf-8")
        ).hexdigest()
        if title:
            entries.append(
                {
                    "external_id": external_id,
                    "title": title,
                    "summary": summary,
                    "canonical_url": link or None,
                    "published_at": published,
                }
            )
    return entries, repaired


def build_session(user_agent: str) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.5",
            "Accept-Encoding": "gzip, deflate",
        }
    )
    return session


def normalize_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def shingles(value: str, size: int = 3) -> set[tuple[str, ...]]:
    tokens = normalize_text(value).split()
    if len(tokens) < size:
        return {tuple(tokens)} if tokens else set()
    return {tuple(tokens[index:index + size]) for index in range(len(tokens) - size + 1)}


def jaccard(left: set[Any], right: set[Any]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def overlap_evidence(
    title: str,
    text: str,
    training_texts: list[str],
    training_shingles: list[set[tuple[str, ...]]],
) -> dict[str, Any]:
    normalized_title = normalize_text(title)
    title_overlap = any(
        len(normalized_title) >= 20 and normalized_title in normalize_text(training)
        for training in training_texts
    )
    sample_shingles = shingles(text)
    max_jaccard = max(
        (jaccard(sample_shingles, candidate) for candidate in training_shingles),
        default=0.0,
    )
    return {
        "title_substring_overlap": title_overlap,
        "max_training_shingle_jaccard": round(max_jaccard, 6),
    }


def collect(
    *,
    db_path: Path,
    model_card_path: Path,
    env_file: Path,
    per_source: int | None,
    raw_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    card = json.loads(model_card_path.read_text(encoding="utf-8"))
    sec_user_agent = load_dotenv_value(env_file, "SEC_USER_AGENT")
    if not sec_user_agent:
        raise RuntimeError("SEC_USER_AGENT is required for the official SEC blind source")
    session = build_session(sec_user_agent)
    training_ids, training_texts, _labels, _records, training_meta = load_dataset(db_path)
    training_id_set = set(training_ids)
    training_shingle_sets = [shingles(text) for text in training_texts]

    fetched_at = utc_now()
    raw_dir.mkdir(parents=True, exist_ok=True)
    samples: list[dict[str, Any]] = []
    raw_sources: list[dict[str, Any]] = []
    seen_text_hashes: set[str] = set()

    for policy in SOURCE_POLICIES:
        target_count = per_source or policy.sample_count
        response = session.get(policy.feed_url, timeout=30)
        response.raise_for_status()
        body = response.content
        raw_path = raw_dir / f"{policy.source_id}.xml"
        raw_path.write_bytes(body)
        entries, xml_repaired = parse_rss(body)
        entries.sort(
            key=lambda item: (item.get("published_at") or "", item.get("canonical_url") or ""),
            reverse=True,
        )
        accepted = 0
        for entry in entries:
            if not policy.include(entry):
                continue
            text = " ".join(f"{entry['title']} {entry['summary']}".split())[:20000]
            text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if text_hash in seen_text_hashes:
                continue
            overlap = overlap_evidence(
                entry["title"], text, training_texts, training_shingle_sets
            )
            if overlap["title_substring_overlap"] or overlap["max_training_shingle_jaccard"] >= 0.8:
                continue
            sample_id = "EXT-" + hashlib.sha256(
                f"{policy.source_id}|{entry['external_id']}|{text_hash}".encode("utf-8")
            ).hexdigest()[:20]
            if sample_id in training_id_set:
                continue
            samples.append(
                {
                    "sample_id": sample_id,
                    "source_id": policy.source_id,
                    "source_name": policy.name,
                    "source_feed_url": policy.feed_url,
                    "authority_tier": policy.authority_tier,
                    "external_id": entry["external_id"],
                    "canonical_url": entry.get("canonical_url"),
                    "published_at": entry.get("published_at"),
                    "fetched_at": fetched_at,
                    "title": entry["title"],
                    "text": text,
                    "text_sha256": text_hash,
                    "expected_label": policy.expected_label,
                    "label_policy_id": POLICY_VERSION,
                    "label_basis": policy.label_basis,
                    "prediction": None,
                    "overlap_evidence": overlap,
                }
            )
            seen_text_hashes.add(text_hash)
            accepted += 1
            if accepted >= target_count:
                break
        if accepted < target_count:
            raise RuntimeError(
                f"{policy.source_id} yielded {accepted}/{target_count} non-overlapping samples"
            )
        raw_sources.append(
            {
                "source_id": policy.source_id,
                "feed_url": policy.feed_url,
                "http_status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "xml_repaired": xml_repaired,
                "raw_path": display_path(raw_path),
                "expected_label": policy.expected_label,
                "label_basis": policy.label_basis,
                "accepted_samples": accepted,
            }
        )

    samples.sort(key=lambda item: item["sample_id"])
    if any(item["prediction"] is not None for item in samples):
        raise AssertionError("collector must freeze labels before prediction")
    metadata = {
        "model_card": card,
        "training_rows": training_meta["rows"],
        "training_dataset_sha256": card["dataset"]["dataset_sha256"],
        "raw_sources": raw_sources,
        "fetched_at": fetched_at,
    }
    return samples, metadata


def write_freeze(
    samples: list[dict[str, Any]],
    metadata: dict[str, Any],
    *,
    dataset_path: Path,
    freeze_path: Path,
) -> dict[str, Any]:
    dataset_bytes = (
        "\n".join(stable_json(sample) for sample in samples) + "\n"
    ).encode("utf-8")
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    dataset_path.write_bytes(dataset_bytes)
    dataset_sha = hashlib.sha256(dataset_bytes).hexdigest()
    card = metadata["model_card"]
    freeze = {
        "schema_version": 1,
        "freeze_id": f"external-blind-v1-{dataset_sha[:12]}",
        "frozen_at": utc_now(),
        "collection_fetched_at": metadata["fetched_at"],
        "label_policy_id": POLICY_VERSION,
        "label_policy_locked_before_inference": True,
        "predictions_present": False,
        "dataset_path": display_path(dataset_path),
        "dataset_sha256": dataset_sha,
        "rows": len(samples),
        "label_counts": dict(Counter(sample["expected_label"] for sample in samples)),
        "source_counts": dict(Counter(sample["source_id"] for sample in samples)),
        "model_version": card["model_version"],
        "model_trained_at": card["trained_at"],
        "model_artifact_sha256": card["artifact_sha256"],
        "training_rows": metadata["training_rows"],
        "training_dataset_sha256": metadata["training_dataset_sha256"],
        "overlap_audit": {
            "event_or_sample_id_overlap_count": 0,
            "title_substring_overlap_count": sum(
                bool(sample["overlap_evidence"]["title_substring_overlap"])
                for sample in samples
            ),
            "max_training_shingle_jaccard": max(
                sample["overlap_evidence"]["max_training_shingle_jaccard"]
                for sample in samples
            ),
            "rejection_threshold": 0.8,
        },
        "raw_sources": metadata["raw_sources"],
        "contract": [
            "Dataset and expected labels were written before any model inference.",
            "Labels derive only from the declared source-family policy, not model output.",
            "No row may be added, removed or relabeled after this hash without a new version.",
            "This set is external to train/validation and must never be used for retraining v1.",
        ],
    }
    freeze_path.write_text(json.dumps(freeze, ensure_ascii=False, indent=2), encoding="utf-8")
    return freeze


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--model-card", type=Path, default=DEFAULT_MODEL_CARD)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--per-source", type=int)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.per_source is not None and args.per_source < 3:
        parser.error("--per-source must be at least 3")
    existing = [path for path in (args.dataset, args.freeze) if path.exists()]
    if existing and not args.force:
        parser.error("refusing to overwrite frozen artifacts: " + ", ".join(map(str, existing)))
    samples, metadata = collect(
        db_path=args.db,
        model_card_path=args.model_card,
        env_file=args.env_file,
        per_source=args.per_source,
        raw_dir=args.raw_dir,
    )
    freeze = write_freeze(samples, metadata, dataset_path=args.dataset, freeze_path=args.freeze)
    print(json.dumps(freeze, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
