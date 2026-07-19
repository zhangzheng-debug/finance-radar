#!/usr/bin/env python3
"""Gate, preview, probe, and deliver Finance Radar Telegram alerts.

Historical research never qualifies merely because it was imported recently.
Delivery is disabled unless --send is passed explicitly.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from event_ledger import open_ledger, stable_id, stable_json, utc_now
from telegram_mtproto_listener import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_ENV = ROOT / ".env"
ALLOWED_GRADES = {"S", "A++", "A"}


def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value[:10])


def qualified_events(
    connection: Any, *, freshness_days: int, today: dt.date
) -> list[dict[str, Any]]:
    cutoff = today - dt.timedelta(days=freshness_days)
    rows = connection.execute(
        """
        SELECT e.*,
               (SELECT evidence_url FROM event_evidence x
                WHERE x.event_id=e.event_id AND x.evidence_url != ''
                ORDER BY COALESCE(x.filing_date,''), x.evidence_id LIMIT 1) AS evidence_url,
               (SELECT evidence_passage FROM event_evidence x
                WHERE x.event_id=e.event_id AND x.evidence_url != ''
                ORDER BY COALESCE(x.filing_date,''), x.evidence_id LIMIT 1) AS evidence_passage
        FROM canonical_events e
        WHERE e.status='verified' AND e.label_status='verified'
          AND e.manual_grade IN ('S','A++','A') AND e.no_trading=1
          AND date(e.event_date) BETWEEN date(?) AND date(?)
          AND EXISTS (
              SELECT 1 FROM event_evidence x
              WHERE x.event_id=e.event_id AND x.evidence_url != ''
          )
        ORDER BY e.event_date DESC, e.event_id
        """,
        (cutoff.isoformat(), today.isoformat()),
    ).fetchall()
    return [dict(row) for row in rows]


def render_alert(event: dict[str, Any]) -> str:
    grade = event.get("manual_grade") or "未评级"
    ticker = event.get("ticker_at_event") or "N/A"
    company = event.get("company_name") or ticker
    evidence = event.get("evidence_url") or ""
    web_base = os.getenv("FINANCE_RADAR_WEB_URL", "http://127.0.0.1:8501").rstrip("/")
    event_link = f"{web_base}/Event_Intelligence?event_id={urllib.parse.quote(event['event_id'])}"
    lines = [
        "Finance Radar｜已核验事件",
        f"{ticker} · {company}",
        f"事件：{event['event_type']}",
        f"日期：{event['event_date']}",
        f"证据等级：{grade}",
        f"主证据：{evidence}",
        f"证据链详情：{event_link}",
        "用途：事件监测与研究记录，不构成投资建议。系统不含交易执行能力。",
    ]
    return "\n".join(lines)


def enqueue_verified_alerts(
    connection: Any, *, freshness_days: int, today: dt.date
) -> int:
    inserted = 0
    for event in qualified_events(connection, freshness_days=freshness_days, today=today):
        payload = {
            "event_id": event["event_id"],
            "event_version": event["current_version"],
            "text": render_alert(event),
            "freshness_days": freshness_days,
            "event_date": event["event_date"],
            "manual_grade": event["manual_grade"],
            "evidence_url": event["evidence_url"],
        }
        outbox_id = stable_id(
            "OUTBOX", event["event_id"], str(event["current_version"]), "verified_event"
        )
        before = connection.total_changes
        connection.execute(
            """
            INSERT OR IGNORE INTO alert_outbox(
                outbox_id,event_id,event_version,message_type,status,payload_json,
                created_at,sent_at,external_message_id,last_error
            ) VALUES (?,?,?,'verified_event','PENDING',?,?,NULL,NULL,NULL)
            """,
            (
                outbox_id,
                event["event_id"],
                event["current_version"],
                stable_json(payload),
                utc_now(),
            ),
        )
        inserted += connection.total_changes - before
    connection.commit()
    return inserted


def refresh_pending_payloads(connection: Any) -> int:
    """Re-render unsent payloads after the public Web base URL changes."""
    rows = connection.execute(
        """
        SELECT o.outbox_id,o.payload_json,e.*,
               (SELECT evidence_url FROM event_evidence x
                WHERE x.event_id=e.event_id AND x.evidence_url != ''
                ORDER BY COALESCE(x.filing_date,''), x.evidence_id LIMIT 1) AS evidence_url,
               (SELECT evidence_passage FROM event_evidence x
                WHERE x.event_id=e.event_id AND x.evidence_url != ''
                ORDER BY COALESCE(x.filing_date,''), x.evidence_id LIMIT 1) AS evidence_passage
        FROM alert_outbox o
        JOIN canonical_events e ON e.event_id=o.event_id
        WHERE o.status IN ('PENDING','RETRY')
        ORDER BY o.created_at,o.outbox_id
        """
    ).fetchall()
    refreshed = 0
    for row in rows:
        event = dict(row)
        payload = json.loads(event.pop("payload_json"))
        new_text = render_alert(event)
        if payload.get("text") == new_text:
            continue
        payload["text"] = new_text
        payload["evidence_url"] = event.get("evidence_url")
        connection.execute(
            "UPDATE alert_outbox SET payload_json=? WHERE outbox_id=?",
            (stable_json(payload), event["outbox_id"]),
        )
        refreshed += 1
    connection.commit()
    return refreshed


def pending_rows(connection: Any) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT * FROM alert_outbox WHERE status IN ('PENDING','RETRY')
           ORDER BY created_at,outbox_id"""
    ).fetchall()
    return [dict(row) for row in rows]


def http_post(url: str, data: dict[str, str], timeout: float) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram HTTP {exc.code}: {detail[:300]}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Telegram request failed: {exc}") from exc
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API rejected request: {stable_json(payload)[:300]}")
    return payload


class TelegramBotClient:
    def __init__(
        self,
        token: str,
        *,
        requester: Callable[[str, dict[str, str], float], dict[str, Any]] = http_post,
        timeout: float = 15.0,
    ) -> None:
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._requester = requester
        self._timeout = timeout

    def call(self, method: str, data: dict[str, str]) -> dict[str, Any]:
        return self._requester(f"{self._base_url}/{method}", data, self._timeout)


def require_bot_config() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env")
    return token, chat_id


def probe_bot(client: TelegramBotClient, chat_id: str) -> dict[str, Any]:
    identity = client.call("getMe", {})["result"]
    chat = client.call("getChat", {"chat_id": chat_id})["result"]
    return {
        "bot_username": identity.get("username"),
        "chat_id": str(chat.get("id")),
        "chat_type": chat.get("type"),
    }


def acquire_delivery_lease(connection: Any, outbox_id: str, ttl_seconds: int = 120) -> str | None:
    now_value = dt.datetime.now(dt.timezone.utc)
    acquired_at = now_value.isoformat()
    expires_at = (now_value + dt.timedelta(seconds=ttl_seconds)).isoformat()
    token = str(uuid.uuid4())
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "DELETE FROM alert_delivery_leases WHERE outbox_id=? AND expires_at<=?",
            (outbox_id, acquired_at),
        )
        before = connection.total_changes
        connection.execute(
            """INSERT OR IGNORE INTO alert_delivery_leases(
               outbox_id,lease_token,acquired_at,expires_at) VALUES (?,?,?,?)""",
            (outbox_id, token, acquired_at, expires_at),
        )
        acquired = connection.total_changes > before
        connection.commit()
        return token if acquired else None
    except Exception:
        connection.rollback()
        raise


def release_delivery_lease(connection: Any, outbox_id: str, token: str) -> None:
    connection.execute(
        "DELETE FROM alert_delivery_leases WHERE outbox_id=? AND lease_token=?",
        (outbox_id, token),
    )
    connection.commit()


def deliver_pending(connection: Any, client: TelegramBotClient, chat_id: str) -> tuple[int, int]:
    sent = 0
    errors = 0
    for row in pending_rows(connection):
        lease_token = acquire_delivery_lease(connection, row["outbox_id"])
        if lease_token is None:
            continue
        payload = json.loads(row["payload_json"])
        previous = connection.execute(
            """SELECT external_message_id FROM alert_outbox
               WHERE event_id=? AND status='SENT' AND external_message_id IS NOT NULL
                 AND event_version < ?
               ORDER BY event_version DESC LIMIT 1""",
            (row["event_id"], row["event_version"]),
        ).fetchone()
        operation = "editMessageText" if previous else "sendMessage"
        data = {"chat_id": chat_id, "text": payload["text"], "disable_web_page_preview": "true"}
        if previous:
            data["message_id"] = str(previous["external_message_id"])
        attempted_at = utc_now()
        try:
            response = client.call(operation, data)
            message_id = str(response["result"]["message_id"])
            connection.execute(
                """UPDATE alert_outbox SET status='SENT',sent_at=?,external_message_id=?,last_error=NULL
                   WHERE outbox_id=?""",
                (attempted_at, message_id, row["outbox_id"]),
            )
            connection.execute(
                """INSERT INTO alert_delivery_attempts(
                       attempt_id,outbox_id,attempted_at,operation,outcome,response_json,error_text
                   ) VALUES (?,?,?,?,? ,?,NULL)""",
                (
                    stable_id("ATTEMPT", row["outbox_id"], attempted_at, operation),
                    row["outbox_id"],
                    attempted_at,
                    operation,
                    "sent",
                    stable_json(response),
                ),
            )
            sent += 1
        except RuntimeError as exc:
            error = str(exc)[:1000]
            connection.execute(
                "UPDATE alert_outbox SET status='RETRY',last_error=? WHERE outbox_id=?",
                (error, row["outbox_id"]),
            )
            connection.execute(
                """INSERT INTO alert_delivery_attempts(
                       attempt_id,outbox_id,attempted_at,operation,outcome,response_json,error_text
                   ) VALUES (?,?,?,?,?,NULL,?)""",
                (
                    stable_id("ATTEMPT", row["outbox_id"], attempted_at, operation),
                    row["outbox_id"],
                    attempted_at,
                    operation,
                    "error",
                    error,
                ),
            )
            errors += 1
        connection.commit()
        release_delivery_lease(connection, row["outbox_id"], lease_token)
    return sent, errors


def cleanup_duplicate_deliveries(
    connection: Any, client: TelegramBotClient, chat_id: str
) -> tuple[int, int]:
    rows = connection.execute(
        """SELECT a.outbox_id,a.response_json,o.external_message_id
           FROM alert_delivery_attempts a
           JOIN alert_outbox o ON o.outbox_id=a.outbox_id
           WHERE a.outcome='sent' AND a.response_json IS NOT NULL"""
    ).fetchall()
    deleted = 0
    errors = 0
    for row in rows:
        response = json.loads(row["response_json"])
        message_id = str(response.get("result", {}).get("message_id", ""))
        if not message_id or message_id == str(row["external_message_id"]):
            continue
        already = connection.execute(
            """SELECT 1 FROM alert_delivery_cleanup
               WHERE outbox_id=? AND external_message_id=?""",
            (row["outbox_id"], message_id),
        ).fetchone()
        if already:
            continue
        attempted_at = utc_now()
        cleanup_id = stable_id("CLEANUP", row["outbox_id"], message_id)
        try:
            result = client.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
            connection.execute(
                """INSERT INTO alert_delivery_cleanup(
                   cleanup_id,outbox_id,external_message_id,attempted_at,outcome,response_json,error_text
                   ) VALUES (?,?,?,?,'deleted',?,NULL)""",
                (
                    cleanup_id,
                    row["outbox_id"],
                    message_id,
                    attempted_at,
                    stable_json(result),
                ),
            )
            deleted += 1
        except RuntimeError as exc:
            connection.execute(
                """INSERT INTO alert_delivery_cleanup(
                   cleanup_id,outbox_id,external_message_id,attempted_at,outcome,response_json,error_text
                   ) VALUES (?,?,?,?,'error',NULL,?)""",
                (cleanup_id, row["outbox_id"], message_id, attempted_at, str(exc)[:1000]),
            )
            errors += 1
        connection.commit()
    return deleted, errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--freshness-days", type=int, default=3)
    parser.add_argument("--enqueue", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--send", action="store_true", help="perform external Telegram writes")
    parser.add_argument(
        "--cleanup-duplicates",
        action="store_true",
        help="delete superseded duplicate Telegram messages recorded in the audit log",
    )
    parser.add_argument("--dry-run", action="store_true", help="preview pending messages")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.freshness_days < 0:
        print("ERROR: --freshness-days must be non-negative", file=sys.stderr)
        return 2
    load_dotenv(args.env_file)
    connection = open_ledger(args.db)
    try:
        if args.enqueue:
            inserted = enqueue_verified_alerts(
                connection,
                freshness_days=args.freshness_days,
                today=dt.datetime.now(dt.timezone.utc).date(),
            )
            print(f"Outbox gate complete: inserted={inserted}")
            refreshed = refresh_pending_payloads(connection)
            print(f"Pending payload refresh: updated={refreshed}")
        if args.probe or args.send or args.cleanup_duplicates:
            token, chat_id = require_bot_config()
            client = TelegramBotClient(token)
            if args.probe:
                result = probe_bot(client, chat_id)
                print(
                    "Telegram probe PASS: "
                    f"bot=@{result['bot_username']} chat_id={result['chat_id']} type={result['chat_type']}"
                )
            if args.send:
                sent, errors = deliver_pending(connection, client, chat_id)
                print(f"Telegram delivery complete: sent={sent} errors={errors}")
                return 1 if errors else 0
            if args.cleanup_duplicates:
                deleted, errors = cleanup_duplicate_deliveries(connection, client, chat_id)
                print(f"Telegram duplicate cleanup: deleted={deleted} errors={errors}")
                return 1 if errors else 0
        if args.dry_run or not (args.enqueue or args.probe or args.send):
            rows = pending_rows(connection)
            print(f"Pending alerts: {len(rows)}")
            for row in rows:
                print(f"--- {row['outbox_id']} ---")
                print(json.loads(row["payload_json"])["text"])
        return 0
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
