#!/usr/bin/env python3
"""Independently verify an extracted Finance Radar offline-demo bundle."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import urllib.parse
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_ROUTE_TERMS = ("order", "position", "balance", "broker", "trade_execution")
FORBIDDEN_SOURCE_MARKERS = (b"api.telegram.org", b"sendMessage", b"placeOrder(")
SECRET_PATTERNS = (
    re.compile(rb"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(rb"-----BEGIN (?:OPENSSH|RSA|EC) PRIVATE KEY-----"),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_manifest(bundle: Path) -> dict[str, Any]:
    manifest_path = bundle / "MANIFEST.sha256"
    if not manifest_path.is_file():
        raise FileNotFoundError("offline bundle manifest is missing")
    manifest: dict[str, str] = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match:
            raise ValueError(f"invalid manifest line: {line!r}")
        digest, name = match.groups()
        if name in manifest:
            raise ValueError(f"duplicate manifest path: {name}")
        manifest[name] = digest
    actual = {
        path.relative_to(bundle).as_posix(): path
        for path in bundle.rglob("*")
        if path.is_file()
        and path.relative_to(bundle).as_posix() != "MANIFEST.sha256"
        and not path.relative_to(bundle).as_posix().startswith("runtime/")
    }
    if set(actual) != set(manifest):
        missing = sorted(set(manifest) - set(actual))
        extra = sorted(set(actual) - set(manifest))
        raise ValueError(f"manifest inventory mismatch: missing={missing[:3]} extra={extra[:3]}")
    mismatches = [name for name, path in actual.items() if sha256_file(path) != manifest[name]]
    if mismatches:
        raise ValueError(f"manifest hash mismatch: {mismatches[:3]}")
    for name, path in actual.items():
        lowered = name.lower()
        if ".env" in lowered or "id_ed25519" in lowered or "telegram_" in lowered:
            raise ValueError(f"forbidden filename in bundle: {name}")
        data = path.read_bytes()
        if any(pattern.search(data) for pattern in SECRET_PATTERNS):
            raise ValueError(f"secret-like content in bundle: {name}")
        if (
            name.startswith("app/")
            and path.suffix == ".py"
            and any(marker in data for marker in FORBIDDEN_SOURCE_MARKERS)
        ):
            raise ValueError(f"forbidden send/trading source marker in bundle: {name}")
    return {"files": len(manifest), "hashes": "PASS", "secret_scan": "PASS"}


def verify_archive(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    with zipfile.ZipFile(path, "r") as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError(f"offline archive CRC failed: {bad}")
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("offline archive has duplicate paths")
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "crc": "PASS",
        "entries": len(names),
    }


def sqlite_report(path: Path, *, operations: bool = False) -> dict[str, Any]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    quick = connection.execute("PRAGMA quick_check").fetchone()[0]
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = [dict(row) for row in connection.execute("PRAGMA foreign_key_check")]
    if operations:
        schema = connection.execute("SELECT MAX(version) FROM operations_schema").fetchone()[0]
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("replay_runs", "model_runs", "worker_cycles", "adjudication_samples", "adjudication_reviews")
        }
        boundaries = {"fabricated_human_reviews": counts["adjudication_reviews"]}
    else:
        schema = connection.execute("SELECT MAX(version) FROM event_ledger_schema").fetchone()[0]
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("sources", "raw_observations", "canonical_events", "event_evidence", "event_market_metrics")
        }
        boundaries = {
            "trading": connection.execute("SELECT COUNT(*) FROM canonical_events WHERE no_trading != 1").fetchone()[0],
            "auto_verification": connection.execute("SELECT COUNT(*) FROM event_evidence WHERE auto_verification_allowed != 0").fetchone()[0],
            "market_feature_leakage": connection.execute("SELECT COUNT(*) FROM event_market_metrics WHERE allowed_as_model_feature != 0").fetchone()[0],
        }
    connection.close()
    if quick != "ok" or integrity != "ok" or foreign_keys or any(boundaries.values()):
        raise ValueError(
            f"SQLite verification failed for {path.name}: quick={quick} integrity={integrity} "
            f"foreign_keys={len(foreign_keys)} boundaries={boundaries}"
        )
    return {
        "schema_version": schema,
        "quick_check": quick,
        "integrity_check": integrity,
        "foreign_key_violations": len(foreign_keys),
        "counts": counts,
        "boundary_violations": boundaries,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def configure_bundle(bundle: Path, *, operations_path: Path | None = None) -> None:
    sys.path.insert(0, str(bundle))
    values = {
        "FINANCE_RADAR_DB": bundle / "data" / "finance_radar_demo.sqlite3",
        "FINANCE_RADAR_OPS_DB": operations_path or bundle / "data" / "finance_radar_demo_operations.sqlite3",
        "FINANCE_RADAR_ARTIFACT_DIR": bundle / "artifacts",
        "FINANCE_RADAR_EVIDENCE_OBJECT_DIR": bundle / "data" / "evidence_objects",
        "FINANCE_RADAR_REPLAY_DIR": bundle / "replay" / "cases",
    }
    for key, value in values.items():
        os.environ[key] = str(value)
    os.environ.update(
        {
            "FINANCE_RADAR_API_URL": "http://127.0.0.1:18700",
            "FINANCE_RADAR_WEB_URL": "http://127.0.0.1:18701",
            "FINANCE_RADAR_DEMO_MODE": "REPLAY",
            "FINANCE_RADAR_ADMIN_TOKEN": "offline-demo-local-only",
            "FINANCE_RADAR_OFFLINE_NETWORK_GUARD": "1",
            "FINANCE_RADAR_REVIEW_UI_ENABLED": "0",
            "FINANCE_RADAR_SHOW_DEBUG": "0",
        }
    )
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_API_ID",
        "TELEGRAM_API_HASH",
        "BINANCE_API_KEY",
        "BINANCE_API_SECRET",
        "IBKR_ACCOUNT",
        "FINANCE_RADAR_EVIDENCE_LLM_URL",
    ):
        os.environ.pop(key, None)
    guard_path = bundle / "sitecustomize.py"
    spec = importlib.util.spec_from_file_location("finance_radar_offline_guard", guard_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load offline network guard")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    guard.install_guard()


def verify_network_guard() -> dict[str, Any]:
    external_blocked = False
    try:
        socket.getaddrinfo("example.com", 443)
    except OSError as exc:
        external_blocked = "offline guard blocked" in str(exc)
    if not external_blocked:
        raise ValueError("offline network guard did not block an external destination")
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    loopback_port = probe.getsockname()[1]
    probe.close()
    return {
        "external_dns_and_connect": "BLOCKED",
        "loopback_bind": "PASS",
        "loopback_probe_port": loopback_port,
    }


def verify_product(bundle: Path) -> dict[str, Any]:
    from fastapi.testclient import TestClient
    from streamlit.testing.v1 import AppTest

    from app.api.main import create_app
    from app.config import Settings
    import app.web.common as web_common

    settings = Settings.from_env()
    client = TestClient(create_app(settings))
    checks: list[str] = []

    def get(path: str, **params: Any) -> dict[str, Any]:
        response = client.get(path, params=params or None)
        if response.status_code != 200:
            raise ValueError(f"GET {path} failed: {response.status_code} {response.text[:300]}")
        checks.append(f"GET {path}")
        return response.json()["data"]

    health = get("/api/v1/health")
    if health["status"] != "ok" or health["model"]["status"] != "ready":
        raise ValueError("offline API or model is not ready")
    if set(health["forbidden_capabilities"]) != {"orders", "positions", "balances", "trade_execution"}:
        raise ValueError("forbidden capability declaration changed")
    overview = get("/api/v1/overview")
    verified = get("/api/v1/events", status="verified", limit=20)
    candidates = get("/api/v1/events", status="candidate", limit=20)
    if not verified["items"] or not candidates["items"]:
        raise ValueError("offline ledger lacks verified or candidate demonstration events")
    event_id = verified["items"][0]["event_id"]
    detail = get(f"/api/v1/events/{event_id}")
    evidence = get(f"/api/v1/events/{event_id}/evidence")
    get(f"/api/v1/events/{event_id}/timeline")
    get(f"/api/v1/events/{event_id}/trace")
    model = get("/api/v1/model/status")
    replays = get("/api/v1/replays")
    adjudication = get("/api/v1/adjudication/status")
    if not evidence["items"] or not detail["model_shadow_output"]["shadow"]:
        raise ValueError("evidence or shadow model output missing")
    if model.get("external_blind", {}).get("gate_pass") is not False:
        raise ValueError("failed external-blind gate is not preserved honestly")
    if adjudication["valid_annotations"] != 0:
        raise ValueError("offline bundle must not fabricate adjudication labels")

    routes = {route.path for route in client.app.routes}
    bad_routes = sorted(
        route for route in routes if any(term in route.lower() for term in FORBIDDEN_ROUTE_TERMS)
    )
    if bad_routes:
        raise ValueError(f"trading-like API routes present: {bad_routes}")
    case_id = replays["items"][0]["case_id"]
    denied = client.post(f"/api/v1/replays/{case_id}/run")
    if denied.status_code != 403:
        raise ValueError("offline replay write boundary did not reject missing admin token")
    replay = client.post(
        f"/api/v1/replays/{case_id}/run",
        headers={"X-Admin-Token": "offline-demo-local-only"},
    )
    if replay.status_code != 200:
        raise ValueError(f"offline replay failed: {replay.status_code} {replay.text[:300]}")
    replay_data = replay.json()["data"]
    if replay_data["external_network_used"] or not replay_data["no_trading"]:
        raise ValueError("replay violated offline or no-trading boundary")

    def local_api(path: str, *, method: str = "GET", json_body: dict[str, Any] | None = None):
        parsed = urllib.parse.urlsplit(path)
        headers = {"X-Admin-Token": "offline-demo-local-only"}
        response = client.request(
            method,
            parsed.path + (("?" + parsed.query) if parsed.query else ""),
            json=json_body,
            headers=headers,
        )
        if response.status_code >= 400:
            raise web_common.ApiError(f"offline API {response.status_code}")
        return response.json()["data"]

    web_common.api_request = local_api
    pages = {
        "Home": bundle / "app" / "web" / "Home.py",
        "Event Intelligence": bundle / "app" / "web" / "pages" / "1_Event_Intelligence.py",
        "Replay Lab": bundle / "app" / "web" / "pages" / "2_Replay_Lab.py",
        "Operations and Model": bundle / "app" / "web" / "pages" / "3_Operations_and_Model.py",
        "Adjudication Studio": bundle / "app" / "web" / "pages" / "4_Adjudication_Studio.py",
    }
    rendered: dict[str, str] = {}
    for name, path in pages.items():
        page = AppTest.from_file(str(path), default_timeout=20).run()
        if page.exception:
            raise ValueError(f"offline page failed: {name}: {page.exception}")
        rendered[name] = "PASS"

    return {
        "api_checks": len(checks),
        "api_status": "PASS",
        "verified_events": len(verified["items"]),
        "candidate_events": len(candidates["items"]),
        "overview_events": overview["counts"]["canonical_events"],
        "evidence_rows_for_probe": len(evidence["items"]),
        "model_status": model["status"],
        "model_mode": "SHADOW",
        "external_blind_gate_pass": model["external_blind"]["gate_pass"],
        "replay_case": case_id,
        "replay_expectation_met": replay_data["expectation_met"],
        "replay_external_network_used": replay_data["external_network_used"],
        "replay_no_trading": replay_data["no_trading"],
        "missing_admin_token_status": denied.status_code,
        "trading_like_routes": bad_routes,
        "pages": rendered,
    }


def markdown_report(report: dict[str, Any]) -> str:
    product = report["product"]
    return "\n".join(
        [
            "# Offline demo acceptance",
            "",
            f"- Status: **{report['status']}** ({report['passed_checks']}/{report['total_checks']})",
            f"- Bundle: `{report['bundle_root']}`",
            f"- Manifest: {report['manifest']['files']} files, hashes and secret scan PASS",
            f"- Ledger: Schema {report['ledger']['schema_version']}, {report['ledger']['counts']['canonical_events']} selected real events",
            f"- Pages: {len(product['pages'])}/5 rendered without exceptions",
            f"- Replay: `{product['replay_case']}`, expectation={product['replay_expectation_met']}, external network={product['replay_external_network_used']}",
            f"- External network guard: {report['network_guard']['external_dns_and_connect']}",
            f"- Model: {product['model_status']} / SHADOW; external-blind gate pass={product['external_blind_gate_pass']}",
            f"- Trading-like API routes: {len(product['trading_like_routes'])}",
            f"- Human labels fabricated: {report['operations']['boundary_violations']['fabricated_human_reviews']}",
            "",
            "This proves a frozen, executable defense fallback. It does not claim live collection while offline.",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", type=Path, default=SCRIPT_ROOT)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--report-dir", type=Path)
    args = parser.parse_args()
    bundle = args.bundle_root.resolve()
    manifest = verify_manifest(bundle)
    archive = verify_archive(args.archive.resolve() if args.archive else None)
    ledger = sqlite_report(bundle / "data" / "finance_radar_demo.sqlite3")
    operations = sqlite_report(
        bundle / "data" / "finance_radar_demo_operations.sqlite3", operations=True
    )
    restore = sqlite_report(bundle / "data" / "snapshot_backup.sqlite3")
    if restore["sha256"] != ledger["sha256"]:
        raise ValueError("restore-copy hash does not match the primary demo ledger")
    with tempfile.TemporaryDirectory(prefix="finance-radar-offline-verify-") as temporary:
        mutable_operations = Path(temporary) / "operations.sqlite3"
        shutil.copy2(
            bundle / "data" / "finance_radar_demo_operations.sqlite3",
            mutable_operations,
        )
        configure_bundle(bundle, operations_path=mutable_operations)
        network_guard = verify_network_guard()
        product = verify_product(bundle)
    checks = {
        "manifest": manifest["hashes"] == "PASS" and manifest["secret_scan"] == "PASS",
        "ledger": ledger["quick_check"] == "ok" and not any(ledger["boundary_violations"].values()),
        "operations": operations["quick_check"] == "ok" and not any(operations["boundary_violations"].values()),
        "restore_copy": restore["sha256"] == ledger["sha256"],
        "network_guard": network_guard["external_dns_and_connect"] == "BLOCKED",
        "api": product["api_status"] == "PASS",
        "five_pages": len(product["pages"]) == 5 and set(product["pages"].values()) == {"PASS"},
        "replay": product["replay_expectation_met"] and not product["replay_external_network_used"],
        "no_trading": product["replay_no_trading"] and not product["trading_like_routes"],
        "honest_model_gate": product["external_blind_gate_pass"] is False,
        "write_boundary": product["missing_admin_token_status"] == 403,
    }
    report = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "status": "PASS" if all(checks.values()) else "FAIL",
        "passed_checks": sum(checks.values()),
        "total_checks": len(checks),
        "checks": checks,
        "bundle_root": str(bundle),
        "manifest": manifest,
        "archive": archive,
        "ledger": ledger,
        "operations": operations,
        "restore_copy": restore,
        "network_guard": network_guard,
        "product": product,
    }
    report_dir = (args.report_dir or bundle.parent).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "offline_demo_acceptance_latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (report_dir / "offline_demo_acceptance_latest.md").write_text(
        markdown_report(report), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
