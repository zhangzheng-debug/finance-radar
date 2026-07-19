#!/usr/bin/env python3
"""Train a non-production v2 risk-router candidate under the locked no-leak protocol."""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.metrics import accuracy_score, confusion_matrix


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.risk_router import RiskRouter  # noqa: E402
from scripts.audit_risk_router_input_contract import audit_rows  # noqa: E402
from scripts.evaluate_external_blind import evaluate, load_and_verify  # noqa: E402
from scripts.train_risk_router import build_pipeline, stable_json, time_issuer_chain_split, utc_now  # noqa: E402


DEFAULT_DB = ROOT / "data" / "research" / "finance-radar-v2-input-20260718T1824Z.sqlite3"
DEFAULT_ARTIFACT = ROOT / "artifacts" / "risk_router_v2_candidate.joblib"
DEFAULT_CARD = ROOT / "artifacts" / "risk_router_v2_candidate_model_card.json"
DEFAULT_REPORT = ROOT / "artifacts" / "risk_router_v2_candidate_report.json"
DEFAULT_MARKDOWN = ROOT / "artifacts" / "risk_router_v2_candidate_report.md"
DEFAULT_MANIFEST = ROOT / "artifacts" / "risk_router_v2_candidate_manifest.jsonl"
DEFAULT_RAW_DIR = ROOT / "artifacts" / "risk_router_v2_dev_raw"
DEFAULT_LEGACY_DATASET = ROOT / "artifacts" / "risk_router_external_blind_v1.jsonl"
DEFAULT_LEGACY_FREEZE = ROOT / "artifacts" / "risk_router_external_blind_v1_freeze.json"
DEFAULT_LEGACY_DIAGNOSTIC = ROOT / "artifacts" / "risk_router_v2_on_legacy_blind_v1_diagnostic.json"

RISK_PRIMARY_SOURCES = {
    "cftc_enforcement",
    "fda_medwatch",
    "ftc_press",
    "sec_litigation_releases",
    "sec_trading_suspensions",
}
HARD_NEGATIVE_SOURCES = {
    "microsoft_official_blog_dev": "https://blogs.microsoft.com/feed/",
    "apple_newsroom_dev": "https://www.apple.com/newsroom/rss-feed.rss",
}
SOURCE_HOLDOUT_IDS = {"ecb_press", "ecb_statistical_press", "eia_press"}
INTERNAL_CONTROL_MARKERS = {
    "candidate official",
    "official sec",
    "recovery value",
    "source metadata control",
}
ADVERSE_TERMS = (
    "bankruptcy",
    "breach",
    "charges",
    "complaint",
    "default",
    "delisting",
    "fraud",
    "investigation",
    "layoff",
    "lawsuit",
    "penalty",
    "recall",
    "suspension",
    "violated",
)


def normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def sanitize_publish_time_text(value: str, taxonomy_markers: set[str]) -> str:
    """Normalize content and remove internal labels leaked into legacy observations."""
    text = normalize(value)
    markers = sorted(
        taxonomy_markers | INTERNAL_CONTROL_MARKERS,
        key=len,
        reverse=True,
    )
    for marker in markers:
        if len(marker) >= 4:
            text = text.replace(marker, " ")
    return " ".join(text.split())


def strip_markup(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"<br\s*/?>", " ", value, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html.unescape(text).split())


def element_text(element: ET.Element | None) -> str:
    return "" if element is None else "".join(element.itertext()).strip()


def normalize_date(value: str | None) -> str:
    if not value:
        return "0000-00-00"
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value[:10]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).date().isoformat()


def parse_official_feed(body: bytes) -> list[dict[str, str]]:
    root = ET.fromstring(body)
    rows: list[dict[str, str]] = []
    for item in root.findall(".//item"):
        title = strip_markup(element_text(item.find("title")))
        summary = strip_markup(element_text(item.find("description"))) or title
        link = strip_markup(element_text(item.find("link")))
        published = element_text(item.find("pubDate"))
        if title:
            rows.append({"title": title, "summary": summary, "url": link, "date": normalize_date(published)})
    for entry in root.findall("{*}entry"):
        title = strip_markup(element_text(entry.find("{*}title")))
        summary = strip_markup(
            element_text(entry.find("{*}summary")) or element_text(entry.find("{*}content"))
        ) or title
        link = next(
            (
                str(node.attrib.get("href"))
                for node in entry.findall("{*}link")
                if node.attrib.get("href") and node.attrib.get("rel", "alternate") == "alternate"
            ),
            "",
        )
        published = element_text(entry.find("{*}updated")) or element_text(entry.find("{*}published"))
        if title:
            rows.append({"title": title, "summary": summary, "url": link, "date": normalize_date(published)})
    return rows


def fetch_bytes(url: str, user_agent: str, timeout: float = 30.0) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/atom+xml,application/rss+xml,application/xml,text/xml",
            "Accept-Encoding": "identity",
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
        except urllib.error.URLError:
            if attempt == 2:
                raise
        time.sleep(0.5 * (2**attempt))
    raise RuntimeError("unreachable retry state")


def legacy_exclusions(path: Path) -> tuple[set[str], set[str]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return (
        {normalize(str(row.get("title") or "")) for row in rows},
        {hashlib.sha256(normalize(str(row.get("text") or "")).encode()).hexdigest() for row in rows},
    )


def collect_hard_negatives(
    raw_dir: Path,
    *,
    user_agent: str,
    legacy_dataset: Path,
    per_source: int = 25,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    excluded_titles, excluded_text_hashes = legacy_exclusions(legacy_dataset)
    raw_dir.mkdir(parents=True, exist_ok=True)
    samples: list[dict[str, str]] = []
    source_metrics: dict[str, Any] = {}
    for source_id, url in HARD_NEGATIVE_SOURCES.items():
        body = fetch_bytes(url, user_agent)
        (raw_dir / f"{source_id}.xml").write_bytes(body)
        entries = sorted(parse_official_feed(body), key=lambda row: (row["date"], row["url"]), reverse=True)
        accepted = 0
        filtered_adverse = 0
        filtered_overlap = 0
        for entry in entries:
            text = " ".join(f"{entry['title']} {entry['summary']}".split())[:20000]
            normalized_text = normalize(text)
            if any(term in normalized_text for term in ADVERSE_TERMS):
                filtered_adverse += 1
                continue
            text_hash = hashlib.sha256(normalized_text.encode()).hexdigest()
            if normalize(entry["title"]) in excluded_titles or text_hash in excluded_text_hashes:
                filtered_overlap += 1
                continue
            sample_id = "V2DEV-" + hashlib.sha256(
                f"{source_id}|{entry['url']}|{text_hash}".encode()
            ).hexdigest()[:20]
            samples.append(
                {
                    "event_id": sample_id,
                    "text": text,
                    "label": "NON_TARGET",
                    "event_date": entry["date"],
                    "issuer_key": sample_id.casefold(),
                    "chain_id": "",
                    "source_group": source_id,
                    "label_basis": "ordinary official publisher hard-negative policy",
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
            )
            accepted += 1
            if accepted >= per_source:
                break
        source_metrics[source_id] = {
            "url": url,
            "raw_sha256": hashlib.sha256(body).hexdigest(),
            "parsed": len(entries),
            "accepted": accepted,
            "filtered_adverse": filtered_adverse,
            "filtered_legacy_overlap": filtered_overlap,
        }
        if accepted < 10:
            raise RuntimeError(f"insufficient safe hard negatives from {source_id}: {accepted}")
    return samples, source_metrics


def load_source_holdout_controls(
    db_path: Path,
    *,
    legacy_dataset: Path,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    excluded_titles, excluded_text_hashes = legacy_exclusions(legacy_dataset)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in SOURCE_HOLDOUT_IDS)
    rows = connection.execute(
        f"""SELECT observation_id,source_id,title,summary,canonical_url,source_published_at
            FROM raw_observations WHERE source_id IN ({placeholders})
            ORDER BY source_published_at DESC,observation_id""",
        tuple(sorted(SOURCE_HOLDOUT_IDS)),
    ).fetchall()
    connection.close()
    samples: list[dict[str, str]] = []
    counts: Counter[str] = Counter()
    for row in rows:
        title = str(row["title"] or "")
        text = " ".join(f"{title} {row['summary'] or ''}".split())[:20000]
        normalized_text = normalize(text)
        text_hash = hashlib.sha256(normalized_text.encode()).hexdigest()
        if normalize(title) in excluded_titles or text_hash in excluded_text_hashes:
            counts["filtered_legacy_overlap"] += 1
            continue
        sample_id = "V2HOLD-" + hashlib.sha256(
            f"{row['source_id']}|{row['observation_id']}|{text_hash}".encode()
        ).hexdigest()[:20]
        samples.append(
            {
                "event_id": sample_id,
                "text": text,
                "label": "NON_TARGET",
                "event_date": str(row["source_published_at"] or "0000-00-00")[:10],
                "issuer_key": sample_id.casefold(),
                "chain_id": "",
                "source_group": "ecb_eia_source_holdout",
                "label_basis": "official macro or energy information source holdout",
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
        )
        counts[str(row["source_id"])] += 1
    if len(samples) < 20:
        raise RuntimeError(f"insufficient ECB/EIA source-holdout controls: {len(samples)}")
    return samples, {"source_counts": dict(counts), "rows": len(samples)}


def load_content_dataset(db_path: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    events = [
        dict(row)
        for row in connection.execute(
            """SELECT e.event_id,e.status,e.event_date,e.stable_id,e.company_name,
                      e.ticker_at_event,e.event_family,e.event_type,e.discovery_source,v.facts_json,
                      (SELECT chain_id FROM event_chain_members cm WHERE cm.event_id=e.event_id LIMIT 1) AS chain_id
               FROM canonical_events e
               LEFT JOIN event_versions v ON v.event_id=e.event_id AND v.version=e.current_version
               WHERE e.status IN ('verified','rejected') ORDER BY e.event_id"""
        )
    ]
    observations: dict[str, list[str]] = defaultdict(list)
    for row in connection.execute(
        """SELECT eo.event_id,o.title,o.summary
           FROM event_observations eo JOIN raw_observations o ON o.observation_id=eo.observation_id"""
    ):
        observations[str(row["event_id"])].append(f"{row['title'] or ''} {row['summary'] or ''}")
    evidence: dict[str, list[str]] = defaultdict(list)
    for row in connection.execute(
        "SELECT event_id,evidence_passage FROM event_evidence WHERE evidence_passage IS NOT NULL"
    ):
        evidence[str(row["event_id"])].append(str(row["evidence_passage"] or ""))
    connection.close()

    taxonomy_markers = {
        normalize(str(event.get(key) or ""))
        for event in events
        for key in ("event_family", "event_type")
        if event.get(key)
    }

    samples: list[dict[str, str]] = []
    exclusions: Counter[str] = Counter()
    label_counts: Counter[str] = Counter()
    for event in events:
        source_id = str(event.get("discovery_source") or "")
        if event["status"] == "rejected":
            label = "NON_TARGET"
            label_basis = "manual rejected historical control"
        elif source_id == "sharadar_active_research":
            label = "RISK_REVIEW"
            label_basis = "manual verified historical adverse-research corpus"
        elif source_id in RISK_PRIMARY_SOURCES:
            label = "RISK_REVIEW"
            label_basis = "verified official enforcement or safety source"
        else:
            exclusions[f"verified_outside_downside_scope:{source_id}"] += 1
            continue
        try:
            facts = json.loads(event.get("facts_json") or "{}")
        except json.JSONDecodeError:
            facts = {}
        confirmed = facts.get("confirmed_facts") or []
        if not isinstance(confirmed, list):
            confirmed = [confirmed]
        parts = [
            str(event.get("company_name") or ""),
            str(facts.get("evidence_summary") or ""),
            *[str(value) for value in confirmed],
            *observations.get(str(event["event_id"]), []),
            *evidence.get(str(event["event_id"]), []),
        ]
        text = sanitize_publish_time_text(" ".join(parts), taxonomy_markers)[:30000]
        if len(text) < 12:
            exclusions["insufficient_publish_time_content"] += 1
            continue
        issuer_key = str(
            event.get("stable_id")
            or event.get("ticker_at_event")
            or event.get("company_name")
            or event["event_id"]
        ).strip().casefold()
        samples.append(
            {
                "event_id": str(event["event_id"]),
                "text": text,
                "label": label,
                "event_date": str(event.get("event_date") or "0000-00-00"),
                "issuer_key": issuer_key,
                "chain_id": str(event.get("chain_id") or "").strip().casefold(),
                "source_group": "historical_content_corpus",
                "label_basis": label_basis,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
        )
        label_counts[label] += 1
    return samples, {
        "rows": len(samples),
        "label_counts": dict(label_counts),
        "excluded": dict(exclusions),
        "feature_contract": "company name + observed title/summary + confirmed facts + exact evidence passage",
        "prohibited_model_features": [
            "event_family",
            "event_type",
            "discovery_source",
            "status",
            "manual_grade",
            "post_event_market_data",
        ],
        "taxonomy_markers_removed": len(taxonomy_markers),
        "internal_control_markers_removed": sorted(INTERNAL_CONTROL_MARKERS),
    }


def evaluate_probabilities(pipeline: Any, texts: list[str], labels: list[str], threshold: float) -> dict[str, Any]:
    probabilities = pipeline.predict_proba(texts)
    classes = [str(item) for item in pipeline.classes_]
    predictions: list[str] = []
    covered_truth: list[str] = []
    covered_predictions: list[str] = []
    for truth, row in zip(labels, probabilities):
        index = int(row.argmax())
        prediction = classes[index] if float(row[index]) >= threshold else "ABSTAIN"
        predictions.append(prediction)
        if prediction != "ABSTAIN":
            covered_truth.append(truth)
            covered_predictions.append(prediction)
    return {
        "rows": len(labels),
        "coverage": len(covered_predictions) / len(labels),
        "strict_accuracy": sum(a == b for a, b in zip(labels, predictions)) / len(labels),
        "covered_accuracy": accuracy_score(covered_truth, covered_predictions) if covered_predictions else None,
        "route_distribution": dict(Counter(predictions)),
        "confusion_matrix_covered": confusion_matrix(
            covered_truth, covered_predictions, labels=classes
        ).tolist() if covered_predictions else [],
    }


def top_features(pipeline: Any, limit: int = 20) -> dict[str, Any]:
    feature_names = pipeline.named_steps["features"].get_feature_names_out()
    classifier = pipeline.named_steps["classifier"]
    coefficients = np.mean(
        [calibrated.estimator.coef_[0] for calibrated in classifier.calibrated_classifiers_],
        axis=0,
    )
    return {
        "toward_risk": [
            {"feature": str(feature_names[i]), "coefficient": round(float(coefficients[i]), 6)}
            for i in np.argsort(coefficients)[-limit:][::-1]
        ],
        "toward_non_target": [
            {"feature": str(feature_names[i]), "coefficient": round(float(coefficients[i]), 6)}
            for i in np.argsort(coefficients)[:limit]
        ],
    }


def train_candidate(
    db_path: Path,
    artifact_path: Path,
    card_path: Path,
    manifest_path: Path,
    raw_dir: Path,
    legacy_dataset: Path,
    *,
    user_agent: str,
    threshold: float,
) -> dict[str, Any]:
    base, base_meta = load_content_dataset(db_path)
    hard_negatives, hard_negative_meta = collect_hard_negatives(
        raw_dir, user_agent=user_agent, legacy_dataset=legacy_dataset
    )
    source_holdout_rows, source_holdout_meta = load_source_holdout_controls(
        db_path, legacy_dataset=legacy_dataset
    )
    base_labels = [row["label"] for row in base]
    split_records = [
        {"event_date": row["event_date"], "issuer_key": row["issuer_key"], "chain_id": row["chain_id"]}
        for row in base
    ]
    train_indices, test_indices, split_audit = time_issuer_chain_split(base_labels, split_records)
    train_rows = [base[index] for index in train_indices]
    test_rows = [base[index] for index in test_indices]
    train_rows.extend(hard_negatives)
    test_rows.extend(source_holdout_rows)
    pipeline = build_pipeline("combined")
    pipeline.fit([row["text"] for row in train_rows], [row["label"] for row in train_rows])
    metrics = evaluate_probabilities(
        pipeline,
        [row["text"] for row in test_rows],
        [row["label"] for row in test_rows],
        threshold,
    )
    source_holdout = [row for row in test_rows if row["source_group"] == "ecb_eia_source_holdout"]
    source_holdout_metrics = evaluate_probabilities(
        pipeline,
        [row["text"] for row in source_holdout],
        [row["label"] for row in source_holdout],
        threshold,
    )
    all_rows = base + hard_negatives + source_holdout_rows
    fingerprint = hashlib.sha256(
        stable_json(
            [
                {"event_id": row["event_id"], "label": row["label"], "text_sha256": row["text_sha256"]}
                for row in all_rows
            ]
        ).encode()
    ).hexdigest()
    model_version = f"risk-router-v2-candidate-{fingerprint[:12]}"
    bundle = {
        "pipeline": pipeline,
        "model_version": model_version,
        "abstain_threshold": threshold,
        "trained_at": utc_now(),
        "dataset_sha256": fingerprint,
        "classes": [str(value) for value in pipeline.classes_],
        "input_contract": "publish_time_content_and_exact_evidence_only",
        "candidate_only": True,
        "no_trading": True,
        "shadow": True,
    }
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, artifact_path, compress=3)
    artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    features = top_features(pipeline)
    forbidden_markers = ("source_metadata_control", "distress_equity_death", "candidate official", "official sec")
    shortcut_hits = [
        row
        for direction in features.values()
        for row in direction
        if any(marker in row["feature"].removeprefix("word_tfidf__") for marker in forbidden_markers)
    ]
    candidate_gates = {
        "development_coverage": metrics["coverage"] >= 0.65,
        "development_covered_accuracy": float(metrics["covered_accuracy"] or 0) >= 0.80,
        "source_holdout_coverage": source_holdout_metrics["coverage"] >= 0.65,
        "source_holdout_covered_accuracy": float(source_holdout_metrics["covered_accuracy"] or 0) >= 0.80,
        "zero_top_coefficient_shortcut_hits": not shortcut_hits,
    }
    candidate_gate_pass = all(candidate_gates.values())
    card = {
        "schema_version": 2,
        "model_name": "Finance Radar Downside-Risk Review Router v2 candidate",
        "model_version": model_version,
        "artifact_sha256": artifact_sha256,
        "trained_at": bundle["trained_at"],
        "status": (
            "CANDIDATE_PENDING_NEW_BLIND_V2"
            if candidate_gate_pass
            else "REJECTED_CANDIDATE_NOT_DEPLOYED"
        ),
        "task": "Evidence-stage adverse-risk queue prioritization",
        "input_contract": bundle["input_contract"],
        "dataset": {
            **base_meta,
            "hard_negative_sources": hard_negative_meta,
            "source_holdout": source_holdout_meta,
            "total_rows": len(all_rows),
            "dataset_sha256": fingerprint,
            "train_rows": len(train_rows),
            "test_rows": len(test_rows),
            "split": split_audit,
        },
        "metrics": metrics,
        "source_holdout_metrics": source_holdout_metrics,
        "feature_audit": {"shortcut_hits": shortcut_hits, **features},
        "candidate_thresholds": {
            "development_coverage_gte": 0.65,
            "development_covered_accuracy_gte": 0.80,
            "source_holdout_coverage_gte": 0.65,
            "source_holdout_covered_accuracy_gte": 0.80,
            "zero_top_coefficient_shortcut_hits": True,
        },
        "candidate_gates": candidate_gates,
        "candidate_gate_pass": candidate_gate_pass,
        "governance": {
            "legacy_blind_v1_promotion_eligible": False,
            "new_enriched_blind_v2_required": candidate_gate_pass,
            "production_artifact_replaced": False,
            "promotion_decision": "REMAIN_SHADOW",
            "no_trading": True,
        },
    }
    card_path.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    split_by_id = {row["event_id"]: "train" for row in train_rows}
    split_by_id.update({row["event_id"]: "test" for row in test_rows})
    manifest_path.write_text(
        "\n".join(
            stable_json(
                {
                    "event_id": row["event_id"],
                    "label": row["label"],
                    "label_basis": row["label_basis"],
                    "source_group": row["source_group"],
                    "split": split_by_id[row["event_id"]],
                    "text_sha256": row["text_sha256"],
                    "publish_time_content_only": True,
                }
            )
            for row in all_rows
        ) + "\n",
        encoding="utf-8",
    )
    return card


def write_markdown(card: dict[str, Any], legacy: dict[str, Any], path: Path) -> None:
    lines = [
        "# Risk Router v2 candidate report",
        "",
        f"- Model: `{card['model_version']}`",
        f"- Status: `{card['status']}`",
        f"- Content-only rows: `{card['dataset']['rows']}`",
        f"- Total rows with hard negatives: `{card['dataset']['total_rows']}`",
        f"- Development coverage / covered accuracy: `{card['metrics']['coverage']:.1%}` / `{card['metrics']['covered_accuracy']:.1%}`",
        f"- ECB/EIA source-held-out coverage / accuracy: `{card['source_holdout_metrics']['coverage']:.1%}` / `{card['source_holdout_metrics']['covered_accuracy']:.1%}`",
        f"- Forbidden shortcut hits in top coefficients: `{len(card['feature_audit']['shortcut_hits'])}`",
        f"- Development candidate gate: `{'PASS' if card['candidate_gate_pass'] else 'FAIL'}`",
        f"- Legacy blind-v1 diagnostic false-risk rate: `{legacy['metrics']['non_target_false_risk_rate']:.1%}`",
        f"- Legacy blind-v1 diagnostic risk recall: `{legacy['metrics']['risk_recall']:.1%}`",
        "- Promotion: `REMAIN_SHADOW`; this artifact is not deployed.",
        "",
        "v2 removes event family, event type and discovery source from learned text and strips taxonomy strings leaked into legacy observation text. Microsoft and Apple official posts are training hard negatives; ECB/EIA are held out by source. Exact legacy-blind rows are excluded from development data.",
        "",
        "The development candidate gate failed, so no blind-v2 was frozen and no deployment was attempted. The legacy blind remains diagnostic only after its first failure; its input contract is invalid for evidence-stage promotion because all 20 risk rows are title-only.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--card", type=Path, default=DEFAULT_CARD)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--legacy-dataset", type=Path, default=DEFAULT_LEGACY_DATASET)
    parser.add_argument("--legacy-freeze", type=Path, default=DEFAULT_LEGACY_FREEZE)
    parser.add_argument("--legacy-diagnostic", type=Path, default=DEFAULT_LEGACY_DIAGNOSTIC)
    parser.add_argument("--threshold", type=float, default=0.62)
    parser.add_argument("--user-agent", default="FinanceRadar-v2-research/1.0 zz13240206005@gmail.com")
    args = parser.parse_args()
    card = train_candidate(
        args.db,
        args.artifact,
        args.card,
        args.manifest,
        args.raw_dir,
        args.legacy_dataset,
        user_agent=args.user_agent,
        threshold=args.threshold,
    )
    legacy_rows, legacy_freeze = load_and_verify(args.legacy_dataset, args.legacy_freeze)
    legacy = evaluate(legacy_rows, legacy_freeze, RiskRouter(args.artifact, args.card))
    legacy["evaluation_type"] = "legacy_blind_v1_diagnostic_after_failure_analysis"
    legacy["promotion_eligible"] = False
    legacy["benchmark_input_contract"] = audit_rows(legacy_rows)
    legacy["gate_pass"] = False
    legacy["promotion_decision"] = "REMAIN_SHADOW"
    args.legacy_diagnostic.write_text(
        json.dumps(legacy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report = {
        "schema_version": 1,
        "candidate": card,
        "legacy_blind_v1_diagnostic": {
            "evaluation_type": legacy["evaluation_type"],
            "metrics": legacy["metrics"],
            "promotion_eligible": False,
            "benchmark_contract_valid": legacy["benchmark_input_contract"]["benchmark_contract_valid"],
        },
        "production_artifact_modified": False,
        "promotion_decision": "REMAIN_SHADOW",
        "next_gate": "freeze evidence-enriched label-first external-blind-v2",
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown(card, legacy, args.markdown)
    print(
        json.dumps(
            {
                "model_version": card["model_version"],
                "development_metrics": card["metrics"],
                "source_holdout_metrics": card["source_holdout_metrics"],
                "shortcut_hits": len(card["feature_audit"]["shortcut_hits"]),
                "candidate_gates": card["candidate_gates"],
                "candidate_gate_pass": card["candidate_gate_pass"],
                "legacy_blind_v1_diagnostic_metrics": legacy["metrics"],
                "production_artifact_modified": False,
                "promotion_decision": "REMAIN_SHADOW",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
