#!/usr/bin/env python3
"""Collect cached OpenNews free hot feeds into the immutable Finance Radar inbox."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from event_ledger import (
    enqueue_observation_job,
    open_ledger,
    record_source_observation,
    stable_json,
    upsert_source,
    utc_now,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
BASE_URL = "https://ai.6551.io/open"
DEFAULT_CATEGORIES = ("macro", "ai", "web3")

SEMANTIC_ITEM_FIELDS = {
    "news": (
        "title",
        "summary_zh",
        "summary_en",
        "link",
        "url",
        "published_at",
        "created_at",
        "source",
        "company",
        "coins",
    ),
    "tweets": (
        "content",
        "handle",
        "author",
        "url",
        "posted_at",
        "company",
        "coins",
    ),
}


def fetch_json(url: str, timeout: float = 20.0) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "FinanceRadar/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenNews HTTP {exc.code}: {detail[:300]}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"OpenNews request failed: {exc}") from exc


def iter_items(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    if not payload.get("success"):
        raise ValueError("OpenNews response did not report success")
    result: list[tuple[str, dict[str, Any]]] = []
    for kind in ("news", "tweets"):
        section = payload.get(kind) or {}
        rows = section.get("items") or []
        if not isinstance(rows, list):
            raise ValueError(f"OpenNews {kind}.items must be a list")
        result.extend((kind, row) for row in rows if isinstance(row, dict))
    return result


def item_external_id(category: str, kind: str, item: dict[str, Any]) -> str:
    value = item.get("id") or item.get("url") or item.get("link")
    if value:
        return f"{category}:{kind}:{value}"
    digest = hashlib.sha256(stable_json(item).encode("utf-8")).hexdigest()
    return f"{category}:{kind}:sha256:{digest}"


def item_text(kind: str, item: dict[str, Any]) -> tuple[str, str, str | None, str | None]:
    if kind == "news":
        title = str(item.get("title") or "OpenNews item").strip()
        summary = str(item.get("summary_zh") or item.get("summary_en") or title).strip()
        url = str(item.get("link") or "").strip() or None
        published = str(item.get("published_at") or item.get("created_at") or "").strip() or None
    else:
        content = str(item.get("content") or "").strip()
        handle = str(item.get("handle") or item.get("author") or "unknown").strip()
        title = f"@{handle}: {content[:160]}" if content else f"@{handle} OpenNews tweet"
        summary = content
        url = str(item.get("url") or "").strip() or None
        published = str(item.get("posted_at") or "").strip() or None
    return title, summary, url, published


def semantic_content_hash(category: str, kind: str, item: dict[str, Any]) -> str:
    """Hash user-visible content, excluding feed clocks, scores and ranking counters."""
    semantic_item = {
        key: item.get(key)
        for key in SEMANTIC_ITEM_FIELDS[kind]
        if key in item
    }
    payload = {"provider": "opennews_free", "category": category, "kind": kind, "item": semantic_item}
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


def collect_category(
    connection: Any,
    *,
    category: str,
    requester: Callable[[str, float], dict[str, Any]] = fetch_json,
    timeout: float = 20.0,
) -> dict[str, int]:
    url = f"{BASE_URL}/free_hot?{urllib.parse.urlencode({'category': category})}"
    payload = requester(url, timeout)
    received_at = utc_now()
    counts = {"items": 0, "new_revisions": 0, "jobs": 0}
    for kind, item in iter_items(payload):
        counts["items"] += 1
        title, summary, canonical_url, published_at = item_text(kind, item)
        raw_payload = {
            "provider": "opennews_free",
            "category": category,
            "kind": kind,
            "provider_updated_at": (payload.get(kind) or {}).get("updated_at"),
            "item": item,
        }
        raw_json = stable_json(raw_payload)
        content_hash = semantic_content_hash(category, kind, item)
        observation_id, inserted_revision = record_source_observation(
            connection,
            source_id="opennews_free",
            external_id=item_external_id(category, kind, item),
            source_published_at=published_at,
            local_received_at=received_at,
            title=title,
            summary=summary,
            canonical_url=canonical_url,
            content_sha256=content_hash,
            raw_json=raw_json,
            revision_kind="edit",
            revision_at=received_at,
        )
        counts["new_revisions"] += int(inserted_revision)
        score = item.get("score")
        try:
            priority = max(0, min(100, int(score)))
        except (TypeError, ValueError):
            priority = 50
        if enqueue_observation_job(
            connection,
            observation_id=observation_id,
            job_type="extract_live_event_candidate",
            priority=priority,
            payload={"source": "opennews_free", "category": category, "kind": kind},
        ):
            counts["jobs"] += 1
    connection.commit()
    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--categories", nargs="+", default=list(DEFAULT_CATEGORIES))
    parser.add_argument("--timeout", type=float, default=20.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    connection = open_ledger(args.db)
    try:
        upsert_source(
            connection,
            source_id="opennews_free",
            name="OpenNews Free hot feed",
            source_type="aggregated_discovery",
            authority_tier="P2_experimental",
        )
        totals = {"items": 0, "new_revisions": 0, "jobs": 0, "categories": 0}
        for category in args.categories:
            try:
                counts = collect_category(
                    connection, category=category, timeout=args.timeout
                )
            except (RuntimeError, ValueError) as exc:
                print(f"WARN category={category}: {exc}", file=sys.stderr)
                continue
            totals["categories"] += 1
            for key in ("items", "new_revisions", "jobs"):
                totals[key] += counts[key]
            print(
                f"category={category} items={counts['items']} "
                f"new_revisions={counts['new_revisions']} jobs={counts['jobs']}"
            )
        connection.commit()
        print(
            "OpenNews collection complete: "
            f"categories={totals['categories']} items={totals['items']} "
            f"new_revisions={totals['new_revisions']} jobs={totals['jobs']}"
        )
        return 0 if totals["categories"] else 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
