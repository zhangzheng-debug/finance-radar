from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = os.environ.get("FINANCE_RADAR_AUDIT_API_URL")
DEFAULT_WEB = os.environ.get("FINANCE_RADAR_PUBLIC_WEB_URL")
FORBIDDEN_ROUTE_TERMS = ("orders", "positions", "balances", "brokerage", "trade_execution")


def get_json(client: httpx.Client, url: str) -> dict[str, Any]:
    for attempt in range(3):
        try:
            response = client.get(url)
            response.raise_for_status()
            return response.json()
        except httpx.TransportError:
            if attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))
    raise AssertionError("unreachable")


def get_text(client: httpx.Client, url: str) -> tuple[int, str]:
    for attempt in range(3):
        try:
            response = client.get(url)
            return response.status_code, response.text.strip()
        except httpx.TransportError:
            if attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))
    raise AssertionError("unreachable")


def tls_certificate(url: str, timeout: float) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    if not host:
        raise ValueError(f"URL has no host: {url}")
    port = parsed.port or 443
    context = ssl.create_default_context()
    last_error: OSError | ssl.SSLError | None = None
    for attempt in range(3):
        try:
            with socket.create_connection((host, port), timeout=timeout) as raw:
                with context.wrap_socket(raw, server_hostname=host) as secure:
                    certificate = secure.getpeercert()
                    protocol = secure.version()
            break
        except (OSError, ssl.SSLError) as exc:
            last_error = exc
            if attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))
    else:
        raise RuntimeError(f"TLS certificate unavailable: {last_error}")
    return {
        "host": host,
        "port": port,
        "protocol": protocol,
        "not_after": certificate.get("notAfter"),
        "subject_alt_names": [value for kind, value in certificate.get("subjectAltName", ()) if kind == "DNS"],
    }


def collect(base_url: str, web_url: str, timeout: float) -> dict[str, Any]:
    base = base_url.rstrip("/")
    web = web_url.rstrip("/")
    with httpx.Client(
        timeout=httpx.Timeout(timeout, connect=timeout),
        trust_env=False,
        headers={"Accept": "application/json", "User-Agent": "FinanceRadar-Acceptance/1.0"},
    ) as client:
        health = get_json(client, f"{base}/api/v1/health")["data"]
        evidence_archive = get_json(client, f"{base}/api/v1/evidence/archive")["data"]
        event_facets = get_json(client, f"{base}/api/v1/events/facets")["data"]
        top_family = str(((event_facets.get("families") or [{}])[0]).get("value") or "")
        top_source = str(((event_facets.get("sources") or [{}])[0]).get("value") or "")
        family_filter_sample = get_json(
            client,
            f"{base}/api/v1/events?{urllib.parse.urlencode({'family': top_family, 'limit': 25})}",
        )["data"] if top_family else {"items": [], "total": 0}
        source_filter_sample = get_json(
            client,
            f"{base}/api/v1/events?{urllib.parse.urlencode({'source': top_source, 'limit': 25})}",
        )["data"] if top_source else {"items": [], "total": 0}
        replays = get_json(client, f"{base}/api/v1/replays")["data"]
        openapi = get_json(client, f"{base}/openapi.json")
        web_status, web_body = get_text(client, f"{web}/_stcore/health")
    route_paths = sorted(openapi.get("paths", {}))
    forbidden_paths = [
        path for path in route_paths if any(term in path.lower() for term in FORBIDDEN_ROUTE_TERMS)
    ]
    operations = health["operations"]
    latest_worker = operations.get("latest_worker_cycle") or {}
    latest_backup = operations.get("latest_backup") or {}
    external_blind = health["model"].get("external_blind") or {}
    overlap = external_blind.get("overlap_audit") or {}
    replay_case_ids = {item["case_id"] for item in replays["items"]}
    required_replay_case_ids = {
        "sec_bankruptcy_verified",
        "positive_earnings_non_target",
        "rumor_correction_abstain",
        "sec_filing_corrected_abstain",
    }
    archive_policy = evidence_archive.get("policy") or {}
    recent_source_snapshots = [
        item
        for item in (evidence_archive.get("recent_objects") or [])
        if item.get("object_kind") == "SOURCE_SNAPSHOT"
    ]
    checks = {
        "https_certificate_valid": True,
        "web_health_ok": web_status == 200 and web_body == "ok",
        "api_health_ok": health["status"] == "ok",
        "ledger_schema_15": health["ledger"]["schema_version"] == 15,
        "ledger_quick_check_ok": health["ledger"]["quick_check"] == "ok",
        "operations_quick_check_ok": operations["quick_check"] == "ok",
        "worker_success": latest_worker.get("status") == "SUCCESS",
        "backup_verified": latest_backup.get("status") == "VERIFIED" and latest_backup.get("quick_check") == "ok",
        "model_ready_shadow_only": (
            health["model"]["status"] == "ready"
            and health["model"]["shadow"] is True
            and health["model"]["no_trading"] is True
        ),
        "external_blind_evidence_present": (
            external_blind.get("evaluation_type") == "true_external_blind_label_first"
            and int(external_blind.get("rows") or 0) >= 40
            and int(overlap.get("event_or_sample_id_overlap_count") or 0) == 0
            and int(overlap.get("title_substring_overlap_count") or 0) == 0
        ),
        "external_blind_promotion_guard": (
            external_blind.get("promotion_decision") == "REMAIN_SHADOW"
            and external_blind.get("no_trading") is True
            and health["model"]["shadow"] is True
        ),
        "required_frozen_replays": required_replay_case_ids.issubset(replay_case_ids),
        "no_trading_routes": not forbidden_paths,
        "safe_demo_default": health["demo_mode"] == "RECENT_CAPTURE",
        "safety_audits_zero": sum(health["ledger"]["audit"].values()) == 0,
        "raw_evidence_archive_present": (
            int(evidence_archive.get("source_snapshots") or 0) >= 1
            and int(evidence_archive.get("archived_bytes") or 0) > 0
        ),
        "raw_evidence_integrity_sample": (
            bool(recent_source_snapshots)
            and int(evidence_archive.get("integrity_failures_in_recent_sample") or 0) == 0
            and all(item.get("integrity_verified") is True for item in recent_source_snapshots)
        ),
        "raw_evidence_policy_safe": (
            archive_policy.get("immutable") is True
            and archive_policy.get("content_address") == "sha256"
            and archive_policy.get("no_trading") is True
            and archive_policy.get("allowed_as_model_feature") is False
        ),
        "event_facets_and_exact_filters_read_only": (
            event_facets.get("read_only") is True
            and event_facets.get("no_trading") is True
            and bool(event_facets.get("families"))
            and bool(event_facets.get("sources"))
            and all(int(item.get("count") or 0) > 0 for item in event_facets.get("families") or [])
            and all(int(item.get("count") or 0) > 0 for item in event_facets.get("sources") or [])
            and int(family_filter_sample.get("total") or 0) > 0
            and all(item.get("event_family") == top_family for item in family_filter_sample.get("items") or [])
            and int(source_filter_sample.get("total") or 0) > 0
            and all(item.get("discovery_source") == top_source for item in source_filter_sample.get("items") or [])
        ),
    }
    tls = tls_certificate(web, timeout)
    passed = all(checks.values())
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": {"api": base, "web": web},
        "passed": passed,
        "checks": checks,
        "tls": tls,
        "snapshot": {
            "ledger": health["ledger"],
            "operations_counts": operations["counts"],
            "latest_worker": {
                "cycle_id": latest_worker.get("cycle_id"),
                "status": latest_worker.get("status"),
                "started_at": latest_worker.get("started_at"),
                "finished_at": latest_worker.get("finished_at"),
            },
            "latest_backup": latest_backup,
            "evidence_archive": evidence_archive,
            "event_facets": {
                **event_facets,
                "verified_family_filter": top_family,
                "verified_family_filter_total": family_filter_sample.get("total"),
                "verified_source_filter": top_source,
                "verified_source_filter_total": source_filter_sample.get("total"),
            },
            "model": {
                "status": health["model"]["status"],
                "model_version": health["model"]["model_version"],
                "artifact_sha256": health["model"]["artifact_sha256"],
                "shadow": health["model"]["shadow"],
                "no_trading": health["model"]["no_trading"],
                "external_blind": external_blind,
            },
            "replay_case_ids": [item["case_id"] for item in replays["items"]],
            "api_route_count": len(route_paths),
            "forbidden_paths": forbidden_paths,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect a reproducible live acceptance snapshot.")
    parser.add_argument("--base-url", default=DEFAULT_BASE, required=DEFAULT_BASE is None)
    parser.add_argument("--web-url", default=DEFAULT_WEB, required=DEFAULT_WEB is None)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "product_acceptance_live_latest.json")
    args = parser.parse_args()
    try:
        report = collect(args.base_url, args.web_url, args.timeout)
    except Exception as exc:
        print(json.dumps({"passed": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2))
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "checks": report["checks"], "output": str(args.output)}, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
