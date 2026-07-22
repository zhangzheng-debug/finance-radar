#!/usr/bin/env python3
"""Archive bounded official HTML/PDF evidence as immutable SHA-256 objects."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.storage import EvidenceObjectStore, OperationsRepository
from event_ledger import open_ledger, utc_now
from telegram_mtproto_listener import load_dotenv


DEFAULT_ENV = ROOT / ".env"
DEFAULT_REPORT_JSON = ROOT / "reports" / "evidence_source_snapshots_latest.json"
DEFAULT_REPORT_MD = ROOT / "reports" / "evidence_source_snapshots_latest.md"
DEFAULT_CACHE = ROOT / "data" / "cache" / "official_primary_pages"
MAX_SNAPSHOT_BYTES = 10 * 1024 * 1024
TERMINAL_VALUE_ERROR_MARKERS = (
    "evidence object exceeds",
    "redirected outside registered official source domain",
    "unsupported evidence content type",
    "invalid json evidence object",
    "binary payload declared as text/plain",
    "non-utf-8 payload declared as text/plain",
)

# A source id must be known by the collector and the final URL must remain on
# its registered official domain. This is deliberately narrower than accepting
# any hostname already present in an observation.
SOURCE_HOST_SUFFIXES = {
    "federal_reserve_press": ("federalreserve.gov",),
    "federal_reserve": ("federalreserve.gov",),
    "bls_key_indicators": ("bls.gov",),
    "sec_current_filings": ("sec.gov",),
    "sec_edgar": ("sec.gov",),
    "sec_litigation_releases": ("sec.gov",),
    "sec_trading_suspensions": ("sec.gov",),
    "cftc_enforcement": ("cftc.gov",),
    "fda_medwatch": ("fda.gov",),
    "ftc_press": ("ftc.gov",),
    "fdic_press_releases": ("fdic.gov", "govdelivery.com"),
    "nvidia_official_news": ("nvidia.com",),
    "ecb_press": ("ecb.europa.eu",),
    "ecb_statistical_press": ("ecb.europa.eu",),
    "eia_press": ("eia.gov",),
    "us_marad": ("maritime.dot.gov",),
    "us_treasury": ("treasury.gov",),
}


@dataclass(frozen=True)
class FetchResult:
    body: bytes
    final_url: str
    content_type: str


Fetcher = Callable[[str, str, float, int], FetchResult]


def host_allowed(source_id: str, url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username
        or parsed.password
        or port not in {None, 443}
    ):
        return False
    host = (parsed.hostname or "").casefold().rstrip(".")
    return any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in SOURCE_HOST_SUFFIXES.get(source_id, ())
    )


def canonical_source_url(source_id: str, url: str) -> str | None:
    """Canonicalize a registered official URL to fetch-only HTTPS.

    A few government feeds still emit ``http://`` item links although their
    pages are served over HTTPS. Only registered hosts may be upgraded; user
    info and non-default ports remain rejected, and redirects are revalidated.
    """

    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if parsed.username or parsed.password:
        return None
    host = (parsed.hostname or "").casefold().rstrip(".")
    if not any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in SOURCE_HOST_SUFFIXES.get(source_id, ())
    ):
        return None
    scheme = parsed.scheme.casefold()
    if scheme == "https" and port in {None, 443}:
        return urllib.parse.urlunsplit(("https", host, parsed.path, parsed.query, ""))
    if scheme == "http" and port in {None, 80}:
        return urllib.parse.urlunsplit(("https", host, parsed.path, parsed.query, ""))
    return None


def detect_mime(payload: bytes, content_type: str) -> str:
    normalized = content_type.split(";", 1)[0].strip().casefold()
    if payload.startswith(b"%PDF"):
        return "application/pdf"
    prefix = payload[:4096].lstrip().lower()
    if normalized in {"text/html", "application/xhtml+xml"}:
        return "text/html"
    if prefix.startswith((b"<!doctype html", b"<html", b"<?xml")) and b"<html" in prefix:
        return "text/html"
    if normalized in {"application/json", "application/ld+json"} or prefix.startswith(
        (b"{", b"[")
    ):
        try:
            json.loads(payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON evidence object") from exc
        return "application/json"
    if normalized == "text/plain":
        if b"\x00" in payload:
            raise ValueError("binary payload declared as text/plain")
        try:
            payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("non-UTF-8 payload declared as text/plain") from exc
        return "text/plain"
    raise ValueError(f"unsupported evidence content type: {normalized or 'unknown'}")


def fetch_source(url: str, user_agent: str, timeout: float, max_bytes: int) -> FetchResult:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,text/plain,application/pdf;q=0.9",
            "Accept-Encoding": "identity",
        },
    )
    retryable = {429, 500, 502, 503, 504}
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > max_bytes:
                    raise ValueError(f"evidence object exceeds {max_bytes} bytes")
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise ValueError(f"evidence object exceeds {max_bytes} bytes")
                if not body:
                    raise ValueError("empty evidence object")
                return FetchResult(
                    body=body,
                    final_url=response.geturl(),
                    content_type=response.headers.get("Content-Type", ""),
                )
        except urllib.error.HTTPError as exc:
            if exc.code not in retryable or attempt == 2:
                raise
    raise RuntimeError("unreachable fetch retry state")


def cache_path(cache_dir: Path, url: str) -> Path:
    return cache_dir / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.html"


def candidate_rows(
    connection: Any, *, scan_limit: int, scan_offset: int = 0
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in SOURCE_HOST_SUFFIXES)
    rows = connection.execute(
        f"""
        SELECT ev.event_id,ev.evidence_id,ev.evidence_url,ev.updated_at,
               o.source_id,e.status AS event_status
        FROM event_evidence ev
        JOIN raw_observations o ON o.observation_id=ev.observation_id
        JOIN canonical_events e ON e.event_id=ev.event_id
        WHERE o.source_id IN ({placeholders})
          AND ev.evidence_url IS NOT NULL AND TRIM(ev.evidence_url)!=''
        ORDER BY CASE e.status WHEN 'verified' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END,
                 ev.updated_at DESC,ev.evidence_id
        LIMIT ? OFFSET ?
        """,
        (
            *sorted(SOURCE_HOST_SUFFIXES),
            max(1, int(scan_limit)),
            max(0, int(scan_offset)),
        ),
    ).fetchall()
    return [dict(row) for row in rows]


def archive_pending(
    connection: Any,
    operations: OperationsRepository,
    object_store: EvidenceObjectStore,
    *,
    user_agent: str,
    cache_dir: Path = DEFAULT_CACHE,
    limit: int = 4,
    timeout: float = 25.0,
    max_bytes: int = MAX_SNAPSHOT_BYTES,
    fetcher: Fetcher = fetch_source,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 50))
    max_bytes = max(1024, min(int(max_bytes), MAX_SNAPSHOT_BYTES))
    result: dict[str, Any] = {
        "selected": 0,
        "attempted": 0,
        "archived": 0,
        "already_archived": 0,
        "policy_skipped": 0,
        "http_upgraded_to_https": 0,
        "policy_skip_examples": [],
        "cache_hits": 0,
        "network_fetches": 0,
        "archived_bytes": 0,
        "by_mime": {},
        "errors": [],
        "deferred_failures": 0,
        "terminal_policy_failures": 0,
        "failure_state_migrations": 0,
        "policy": {
            "official_source_allowlist": True,
            "https_only": True,
            "registered_http_links_upgraded_to_https": True,
            "redirect_host_revalidated": True,
            "max_snapshot_bytes": max_bytes,
            "immutable_content_address": "sha256",
            "auto_verification_allowed": False,
            "allowed_as_model_feature": False,
            "no_trading": True,
        },
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    failure_key = "source_snapshot_failures_v1"
    failure_state: dict[str, Any] = (
        operations.get_state(failure_key, {})
        if hasattr(operations, "get_state")
        else {}
    )
    failure_state = failure_state if isinstance(failure_state, dict) else {}
    failure_state_changed = False
    for failure_id, item in list(failure_state.items()):
        if (
            isinstance(item, dict)
            and item.get("terminal_policy") is not True
            and any(
                marker in str(item.get("last_error", "")).casefold()
                for marker in TERMINAL_VALUE_ERROR_MARKERS
            )
        ):
            failure_state[failure_id] = {
                **item,
                "retry_after": None,
                "terminal_policy": True,
                "failure_category": "POLICY_CONTENT_LIMIT",
            }
            failure_state_changed = True
            result["failure_state_migrations"] += 1
    now = datetime.now(timezone.utc)
    batch_size = max(100, limit * 25)
    scan_offset = 0
    while result["attempted"] < limit:
        rows = candidate_rows(
            connection, scan_limit=batch_size, scan_offset=scan_offset
        )
        if not rows:
            break
        result["selected"] += len(rows)
        scan_offset += len(rows)
        for row in rows:
            if result["attempted"] >= limit:
                break
            if operations.has_source_snapshot(row["event_id"], row["evidence_id"]):
                result["already_archived"] += 1
                continue
            original_url = str(row["evidence_url"])
            source_id = str(row["source_id"])
            failure_id = f"{row['event_id']}:{row['evidence_id']}"
            prior_failure = failure_state.get(failure_id, {})
            legacy_terminal = (
                prior_failure.get("source_url") == original_url
                and prior_failure.get("terminal_policy") is not True
                and any(
                    marker in str(prior_failure.get("last_error", "")).casefold()
                    for marker in TERMINAL_VALUE_ERROR_MARKERS
                )
            )
            if legacy_terminal:
                prior_failure = {
                    **prior_failure,
                    "retry_after": None,
                    "terminal_policy": True,
                    "failure_category": "POLICY_CONTENT_LIMIT",
                }
                failure_state[failure_id] = prior_failure
                failure_state_changed = True
            retry_at = datetime.fromisoformat(str(prior_failure.get("retry_after", "")).replace("Z", "+00:00")) if prior_failure.get("retry_after") else None
            if (
                prior_failure.get("source_url") == original_url
                and prior_failure.get("terminal_policy") is True
            ):
                result["terminal_policy_failures"] += 1
                continue
            if (
                prior_failure.get("source_url") == original_url
                and retry_at is not None
                and retry_at > now
            ):
                result["deferred_failures"] += 1
                continue
            url = canonical_source_url(source_id, original_url)
            if url is None or not host_allowed(source_id, url):
                result["policy_skipped"] += 1
                result["terminal_policy_failures"] += 1
                failure_state[failure_id] = {
                    "source_url": original_url,
                    "attempts": int(prior_failure.get("attempts", 0)),
                    "last_error": f"URL outside registered source domain {source_id}",
                    "last_attempt_at": now.isoformat(),
                    "retry_after": None,
                    "terminal_policy": True,
                    "failure_category": "URL_POLICY",
                }
                failure_state_changed = True
                if len(result["policy_skip_examples"]) < 10:
                    result["policy_skip_examples"].append(
                        f"{row['evidence_id']}: URL outside registered source domain {source_id}"
                    )
                continue
            if urllib.parse.urlsplit(original_url).scheme.casefold() == "http":
                result["http_upgraded_to_https"] += 1
            result["attempted"] += 1
            cached = cache_path(cache_dir, url)
            try:
                if cached.is_file():
                    payload = cached.read_bytes()
                    if not payload or len(payload) > max_bytes:
                        raise ValueError("cached evidence object is empty or exceeds size limit")
                    fetched = FetchResult(payload, url, "")
                    result["cache_hits"] += 1
                else:
                    fetched = fetcher(url, user_agent, timeout, max_bytes)
                    result["network_fetches"] += 1
                if not fetched.body or len(fetched.body) > max_bytes:
                    raise ValueError("evidence object is empty or exceeds size limit")
                if not host_allowed(source_id, fetched.final_url):
                    raise ValueError("redirected outside registered official source domain")
                mime_type = detect_mime(fetched.body, fetched.content_type)
                metadata = object_store.put_bytes(fetched.body, mime_type=mime_type)
                if not object_store.verify(metadata["relative_path"], metadata["sha256"]):
                    raise RuntimeError("content-address verification failed after write")
                operations.record_evidence_object(
                    row["event_id"],
                    row["evidence_id"],
                    metadata,
                    source_url=fetched.final_url,
                    fetched_at=utc_now(),
                    object_kind="SOURCE_SNAPSHOT",
                )
            except (OSError, RuntimeError, ValueError, urllib.error.URLError) as exc:
                attempts = int(prior_failure.get("attempts", 0)) + 1
                error_text = f"{type(exc).__name__}: {str(exc)[:240]}"
                normalized_error = str(exc).casefold()
                terminal_policy = isinstance(exc, ValueError) and any(
                    marker in normalized_error for marker in TERMINAL_VALUE_ERROR_MARKERS
                )
                terminal_access = (
                    isinstance(exc, urllib.error.HTTPError)
                    and exc.code in {401, 403, 404}
                    and attempts >= 3
                )
                terminal = terminal_policy or terminal_access
                retry_hours = min(
                    168,
                    24
                    if isinstance(exc, urllib.error.HTTPError) and exc.code in {401, 403, 404}
                    else 2 ** min(attempts, 7),
                )
                failure_state[failure_id] = {
                    "source_url": original_url,
                    "attempts": attempts,
                    "last_error": error_text,
                    "last_attempt_at": now.isoformat(),
                    "retry_after": None if terminal else (now + timedelta(hours=retry_hours)).isoformat(),
                    "terminal_policy": terminal,
                    "failure_category": (
                        "POLICY_CONTENT_LIMIT"
                        if terminal_policy
                        else "ACCESS_UNAVAILABLE"
                        if terminal_access
                        else "RETRYABLE_FETCH"
                    ),
                }
                failure_state_changed = True
                if terminal:
                    result["terminal_policy_failures"] += 1
                result["errors"].append(
                    f"{row['evidence_id']}: {error_text}"
                )
                continue
            if failure_id in failure_state:
                failure_state.pop(failure_id, None)
                failure_state_changed = True
            result["archived"] += 1
            result["archived_bytes"] += int(metadata["byte_length"])
            result["by_mime"][mime_type] = result["by_mime"].get(mime_type, 0) + 1
    if failure_state_changed and hasattr(operations, "set_state"):
        operations.set_state(failure_key, failure_state)
    result["status"] = "PASS" if not result["errors"] else "DEGRADED"
    return result


def write_report(json_path: Path, markdown_path: Path, result: dict[str, Any]) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Raw evidence source snapshots",
        "",
        f"- Status: **{result['status']}**",
        f"- Candidate evidence rows scanned: `{result['selected']}`",
        f"- Fetch/cache attempts this cycle: `{result['attempted']}`",
        f"- New immutable objects archived: `{result['archived']}`",
        f"- Existing snapshots skipped idempotently: `{result['already_archived']}`",
        f"- Out-of-policy links skipped without fetching: `{result['policy_skipped']}`",
        f"- Registered official HTTP links upgraded to HTTPS: `{result['http_upgraded_to_https']}`",
        f"- Cache hits / network fetches: `{result['cache_hits']}` / `{result['network_fetches']}`",
        f"- New archived bytes: `{result['archived_bytes']}`",
        f"- Persistently deferred failed links: `{result.get('deferred_failures', 0)}`",
        f"- Terminal policy/access exclusions: `{result.get('terminal_policy_failures', 0)}`",
        f"- Legacy failure states migrated: `{result.get('failure_state_migrations', 0)}`",
        "- Boundary: registered official domains, safe HTTP-to-HTTPS canonicalization, redirect revalidation, 10 MiB cap, no auto-verification, no model feature use and no trading.",
        "",
        "## MIME types",
        "",
    ]
    for mime_type, count in sorted(result["by_mime"].items()):
        lines.append(f"- `{mime_type}`: `{count}`")
    if result["errors"]:
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {error}" for error in result["errors"])
    if result["policy_skip_examples"]:
        lines.extend(["", "## Policy skip examples", ""])
        lines.extend(f"- {item}" for item in result["policy_skip_examples"])
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--operations-db", type=Path)
    parser.add_argument("--object-dir", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--report-json", type=Path, default=DEFAULT_REPORT_JSON)
    parser.add_argument("--report-md", type=Path, default=DEFAULT_REPORT_MD)
    args = parser.parse_args()
    load_dotenv(args.env_file)
    settings = Settings.from_env()
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent or "@" not in user_agent:
        raise SystemExit("SEC_USER_AGENT with a contact email is required")
    connection = open_ledger(args.db or settings.ledger_db)
    try:
        result = archive_pending(
            connection,
            OperationsRepository(args.operations_db or settings.operations_db),
            EvidenceObjectStore(args.object_dir or settings.evidence_object_dir),
            user_agent=user_agent,
            cache_dir=args.cache_dir,
            limit=args.limit,
            timeout=args.timeout,
        )
    finally:
        connection.close()
    write_report(args.report_json, args.report_md, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print(f"REPORT={args.report_json}")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
