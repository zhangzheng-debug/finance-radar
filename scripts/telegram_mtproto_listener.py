#!/usr/bin/env python3
"""Read-only Telegram MTProto source collector for Finance Radar.

This process uses a personal Telegram session to read channels the account can
already access. It does not send messages, join/leave channels, or touch the
Bot API output path.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import getpass
import hashlib
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from event_ledger import open_ledger, record_source_observation, stable_json, upsert_source


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = ROOT / ".env"
DEFAULT_SESSION = ROOT / "data" / "telegram" / "finance_radar_user"
DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_CONFIG = ROOT / "config" / "telegram_channels.json"
ALLOWED_TIERS = {"primary", "secondary", "discovery"}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE entries without overriding process variables."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    path = Path(raw).expanduser() if raw else default
    return path if path.is_absolute() else ROOT / path


@dataclass(frozen=True)
class ChannelSource:
    handle: str
    tier: str = "discovery"
    enabled: bool = True
    note: str = ""


def normalize_handle(value: str) -> str:
    handle = value.strip()
    handle = re.sub(r"^https?://(?:www\.)?t\.me/", "", handle, flags=re.I)
    handle = handle.split("?", 1)[0].strip("/")
    if handle.startswith("@"):
        handle = handle[1:]
    if not handle or "/" in handle:
        raise ValueError(f"Unsupported channel reference: {value!r}")
    return handle


def load_sources(path: Path) -> list[ChannelSource]:
    if not path.exists():
        raise FileNotFoundError(f"Channel config not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("channels") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Channel config must contain a 'channels' list")

    result: list[ChannelSource] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Channel entry {index} must be an object")
        handle = normalize_handle(str(row.get("handle", "")))
        tier = str(row.get("tier", "discovery")).strip().lower()
        if tier not in ALLOWED_TIERS:
            raise ValueError(f"Channel {handle!r} has unsupported tier {tier!r}")
        key = handle.casefold()
        if key in seen:
            raise ValueError(f"Duplicate channel handle: {handle}")
        seen.add(key)
        result.append(
            ChannelSource(
                handle=handle,
                tier=tier,
                enabled=bool(row.get("enabled", True)),
                note=str(row.get("note", "")).strip(),
            )
        )
    return result


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS telegram_source_channels (
    chat_id INTEGER PRIMARY KEY,
    configured_ref TEXT NOT NULL,
    username TEXT,
    title TEXT NOT NULL,
    source_tier TEXT NOT NULL CHECK (source_tier IN ('primary','secondary','discovery')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    note TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telegram_source_messages (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    posted_at TEXT,
    edited_at TEXT,
    captured_at TEXT NOT NULL,
    permalink TEXT,
    text TEXT NOT NULL DEFAULT '',
    text_sha256 TEXT NOT NULL,
    has_media INTEGER NOT NULL DEFAULT 0 CHECK (has_media IN (0,1)),
    media_type TEXT,
    views INTEGER,
    forwards INTEGER,
    post_author TEXT,
    grouped_id INTEGER,
    reply_to_message_id INTEGER,
    deleted_at TEXT,
    source_tier TEXT NOT NULL CHECK (source_tier IN ('primary','secondary','discovery')),
    PRIMARY KEY (chat_id, message_id),
    FOREIGN KEY (chat_id) REFERENCES telegram_source_channels(chat_id)
);

CREATE INDEX IF NOT EXISTS idx_tg_messages_posted_at
    ON telegram_source_messages(posted_at);
CREATE INDEX IF NOT EXISTS idx_tg_messages_text_sha256
    ON telegram_source_messages(text_sha256);
CREATE INDEX IF NOT EXISTS idx_tg_messages_deleted_at
    ON telegram_source_messages(deleted_at);
"""


def open_database(path: Path) -> sqlite3.Connection:
    connection = open_ledger(path)
    connection.executescript(SCHEMA)
    connection.commit()
    return connection


def iso_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    return str(value)


def entity_chat_id(entity: Any) -> int:
    try:
        from telethon import utils
    except ImportError as exc:  # pragma: no cover - dependency check path
        raise RuntimeError("Telethon is not installed; run pip install -r requirements.txt") from exc
    return int(utils.get_peer_id(entity))


def media_type(message: Any) -> str | None:
    if getattr(message, "photo", None) is not None:
        return "photo"
    if getattr(message, "video", None) is not None:
        return "video"
    if getattr(message, "voice", None) is not None:
        return "voice"
    if getattr(message, "audio", None) is not None:
        return "audio"
    if getattr(message, "sticker", None) is not None:
        return "sticker"
    if getattr(message, "document", None) is not None:
        return "document"
    return type(message.media).__name__ if getattr(message, "media", None) else None


def permalink(username: str | None, chat_id: int, message_id: int) -> str | None:
    if username:
        return f"https://t.me/{username}/{message_id}"
    raw = str(chat_id)
    if raw.startswith("-100"):
        return f"https://t.me/c/{raw[4:]}/{message_id}"
    return None


def upsert_channel(
    connection: sqlite3.Connection,
    *,
    chat_id: int,
    configured_ref: str,
    username: str | None,
    title: str,
    source_tier: str,
    enabled: bool,
    note: str,
) -> None:
    now = utc_now()
    if source_tier not in ALLOWED_TIERS:
        raise sqlite3.IntegrityError(f"unsupported Telegram source tier: {source_tier}")
    authority_tier = {
        "primary": "P1",
        "secondary": "P2",
        "discovery": "P3",
    }[source_tier]
    upsert_source(
        connection,
        source_id=f"telegram_mtproto:{chat_id}",
        name=title,
        source_type="telegram_mtproto",
        authority_tier=authority_tier,
    )
    connection.execute(
        """
        INSERT INTO telegram_source_channels (
            chat_id, configured_ref, username, title, source_tier, enabled,
            note, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            configured_ref=excluded.configured_ref,
            username=excluded.username,
            title=excluded.title,
            source_tier=excluded.source_tier,
            enabled=excluded.enabled,
            note=excluded.note,
            last_seen_at=excluded.last_seen_at
        """,
        (chat_id, configured_ref, username, title, source_tier, int(enabled), note, now, now),
    )
    connection.commit()


def upsert_message(
    connection: sqlite3.Connection,
    *,
    chat_id: int,
    username: str | None,
    source_tier: str,
    message: Any,
) -> None:
    text = str(getattr(message, "message", None) or getattr(message, "raw_text", None) or "")
    message_id = int(message.id)
    existing = connection.execute(
        """SELECT text_sha256 FROM telegram_source_messages
           WHERE chat_id=? AND message_id=?""",
        (chat_id, message_id),
    ).fetchone()
    captured_at = utc_now()
    posted_at = iso_datetime(getattr(message, "date", None))
    edited_at = iso_datetime(getattr(message, "edit_date", None))
    link = permalink(username, chat_id, message_id)
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    kind = "edit" if existing is not None else "new"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "posted_at": posted_at,
        "edited_at": edited_at,
        "captured_at": captured_at,
        "permalink": link,
        "text": text,
        "text_sha256": text_hash,
        "has_media": bool(getattr(message, "media", None)),
        "media_type": media_type(message),
        "views": getattr(message, "views", None),
        "forwards": getattr(message, "forwards", None),
        "post_author": getattr(message, "post_author", None),
        "grouped_id": getattr(message, "grouped_id", None),
        "reply_to_message_id": getattr(
            getattr(message, "reply_to", None), "reply_to_msg_id", None
        ),
        "source_tier": source_tier,
    }
    connection.execute(
        """
        INSERT INTO telegram_source_messages (
            chat_id, message_id, posted_at, edited_at, captured_at, permalink,
            text, text_sha256, has_media, media_type, views, forwards,
            post_author, grouped_id, reply_to_message_id, deleted_at, source_tier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
        ON CONFLICT(chat_id, message_id) DO UPDATE SET
            posted_at=excluded.posted_at,
            edited_at=excluded.edited_at,
            captured_at=excluded.captured_at,
            permalink=excluded.permalink,
            text=excluded.text,
            text_sha256=excluded.text_sha256,
            has_media=excluded.has_media,
            media_type=excluded.media_type,
            views=excluded.views,
            forwards=excluded.forwards,
            post_author=excluded.post_author,
            grouped_id=excluded.grouped_id,
            reply_to_message_id=excluded.reply_to_message_id,
            deleted_at=NULL,
            source_tier=excluded.source_tier
        """,
        (
            chat_id,
            message_id,
            posted_at,
            edited_at,
            captured_at,
            link,
            text,
            text_hash,
            int(getattr(message, "media", None) is not None),
            media_type(message),
            getattr(message, "views", None),
            getattr(message, "forwards", None),
            getattr(message, "post_author", None),
            getattr(message, "grouped_id", None),
            getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None),
            source_tier,
        ),
    )
    record_source_observation(
        connection,
        source_id=f"telegram_mtproto:{chat_id}",
        external_id=str(message_id),
        source_published_at=posted_at,
        local_received_at=captured_at,
        title=f"Telegram message {message_id}",
        summary=text,
        canonical_url=link,
        content_sha256=text_hash,
        raw_json=stable_json(payload),
        revision_kind=kind,
        revision_at=edited_at or posted_at or captured_at,
    )
    connection.commit()


def mark_deleted(
    connection: sqlite3.Connection, chat_id: int, message_ids: Iterable[int]
) -> int:
    deleted_at = utc_now()
    ids = [int(message_id) for message_id in message_ids]
    existing_rows = connection.execute(
        f"""SELECT * FROM telegram_source_messages
            WHERE chat_id=? AND deleted_at IS NULL
              AND message_id IN ({','.join('?' for _ in ids)})""",
        (chat_id, *ids),
    ).fetchall() if ids else []
    for row in existing_rows:
        connection.execute(
            """UPDATE telegram_source_messages SET deleted_at=?
               WHERE chat_id=? AND message_id=? AND deleted_at IS NULL""",
            (deleted_at, chat_id, row["message_id"]),
        )
    for original in existing_rows:
        message_id = int(original["message_id"])
        row = connection.execute(
            """SELECT * FROM telegram_source_messages
               WHERE chat_id=? AND message_id=? AND deleted_at=?""",
            (chat_id, message_id, deleted_at),
        ).fetchone()
        if row is None:
            continue
        payload = {key: row[key] for key in row.keys()}
        payload["revision_kind"] = "delete"
        deletion_hash = hashlib.sha256(
            stable_json(
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "deleted_at": deleted_at,
                }
            ).encode("utf-8")
        ).hexdigest()
        record_source_observation(
            connection,
            source_id=f"telegram_mtproto:{chat_id}",
            external_id=str(message_id),
            source_published_at=row["posted_at"],
            local_received_at=row["captured_at"],
            title=f"Telegram message {message_id} deleted",
            summary=row["text"],
            canonical_url=row["permalink"],
            content_sha256=deletion_hash,
            raw_json=stable_json(payload),
            revision_kind="delete",
            revision_at=deleted_at,
        )
    connection.commit()
    return len(existing_rows)


def require_credentials() -> tuple[int, str]:
    raw_id = os.environ.get("TELEGRAM_API_ID", "").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    if not raw_id or not api_hash:
        raise RuntimeError("Missing TELEGRAM_API_ID / TELEGRAM_API_HASH in .env")
    try:
        api_id = int(raw_id)
    except ValueError as exc:
        raise RuntimeError("TELEGRAM_API_ID must be numeric") from exc
    return api_id, api_hash


def import_telethon() -> tuple[Any, Any, Any]:
    try:
        from telethon import TelegramClient, events
        from telethon.errors import SessionPasswordNeededError
    except ImportError as exc:
        raise RuntimeError("Telethon is not installed; run pip install -r requirements.txt") from exc
    return TelegramClient, events, SessionPasswordNeededError


async def authorize_qr(client: Any, password_error: Any, qr_path: Path) -> None:
    await client.connect()
    if await client.is_user_authorized():
        print("Telegram MTProto session is already authorized.")
        return

    try:
        import qrcode
    except ImportError as exc:
        raise RuntimeError("QR support is missing; run pip install -r requirements.txt") from exc

    login = await client.qr_login()
    qr_path.parent.mkdir(parents=True, exist_ok=True)
    qrcode.make(login.url).save(qr_path)
    print(f"Scan the QR image with Telegram: {qr_path}")
    print("Waiting up to 180 seconds; the QR may refresh once if it expires.")
    try:
        await login.wait(timeout=180)
    except password_error:
        password = getpass.getpass("Telegram two-step verification password: ")
        await client.sign_in(password=password)
    finally:
        qr_path.unlink(missing_ok=True)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram authorization did not complete")
    print("Telegram MTProto authorization succeeded.")


async def resolve_sources(client: Any, sources: list[ChannelSource]) -> list[tuple[ChannelSource, Any]]:
    resolved: list[tuple[ChannelSource, Any]] = []
    for source in sources:
        if not source.enabled:
            continue
        entity = await client.get_entity(source.handle)
        resolved.append((source, entity))
    return resolved


async def run_collector(args: argparse.Namespace) -> None:
    TelegramClient, events, password_error = import_telethon()
    api_id, api_hash = require_credentials()
    session_path = env_path("TELEGRAM_SESSION", DEFAULT_SESSION)
    db_path = env_path("TELEGRAM_DB", DEFAULT_DB)
    config_path = env_path("TELEGRAM_CHANNELS_CONFIG", DEFAULT_CONFIG)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(session_path), api_id, api_hash)

    if args.authorize_qr:
        qr_path = ROOT / "data" / "telegram" / "telegram_login_qr.png"
        try:
            await authorize_qr(client, password_error, qr_path)
        finally:
            await client.disconnect()
        return

    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Session is not authorized; run with --authorize-qr first")

    sources = load_sources(config_path)
    active = [source for source in sources if source.enabled]
    if not active:
        await client.disconnect()
        raise RuntimeError(f"No enabled channels in {config_path}")

    connection = open_database(db_path)
    try:
        resolved = await resolve_sources(client, sources)
        by_chat_id: dict[int, tuple[ChannelSource, Any]] = {}
        for source, entity in resolved:
            chat_id = entity_chat_id(entity)
            by_chat_id[chat_id] = (source, entity)
            upsert_channel(
                connection,
                chat_id=chat_id,
                configured_ref=source.handle,
                username=getattr(entity, "username", None),
                title=str(getattr(entity, "title", None) or source.handle),
                source_tier=source.tier,
                enabled=source.enabled,
                note=source.note,
            )

        if args.probe:
            print(f"MTProto probe PASS: authorized; resolved_channels={len(resolved)}")
            return

        if args.backfill:
            stored = 0
            for source, entity in resolved:
                chat_id = entity_chat_id(entity)
                username = getattr(entity, "username", None)
                async for message in client.iter_messages(entity, limit=args.backfill):
                    upsert_message(
                        connection,
                        chat_id=chat_id,
                        username=username,
                        source_tier=source.tier,
                        message=message,
                    )
                    stored += 1
            print(f"Backfill complete: stored_or_updated={stored}")
            return

        entities = [entity for _, entity in resolved]

        @client.on(events.NewMessage(chats=entities))
        @client.on(events.MessageEdited(chats=entities))
        async def on_message(event: Any) -> None:
            pair = by_chat_id.get(int(event.chat_id))
            if pair is None:
                return
            source, entity = pair
            upsert_message(
                connection,
                chat_id=int(event.chat_id),
                username=getattr(entity, "username", None),
                source_tier=source.tier,
                message=event.message,
            )
            print(f"captured chat_id={event.chat_id} message_id={event.message.id}")

        @client.on(events.MessageDeleted(chats=entities))
        async def on_deleted(event: Any) -> None:
            if event.chat_id is None:
                return
            changed = mark_deleted(connection, int(event.chat_id), event.deleted_ids)
            print(f"marked_deleted chat_id={event.chat_id} rows={changed}")

        print(f"Listening read-only: channels={len(resolved)} db={db_path}")
        await client.run_until_disconnected()
    finally:
        connection.close()
        await client.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file", type=Path, default=DEFAULT_ENV, help="dotenv file (default: project .env)"
    )
    parser.add_argument(
        "--init-db", action="store_true", help="create or upgrade the local SQLite schema and exit"
    )
    parser.add_argument(
        "--authorize-qr", action="store_true", help="authorize the personal session by QR scan"
    )
    parser.add_argument(
        "--probe", action="store_true", help="verify authorization and resolve configured channels"
    )
    parser.add_argument(
        "--backfill", type=int, metavar="N", help="store the latest N messages per configured channel"
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    load_dotenv(args.env_file)
    db_path = env_path("TELEGRAM_DB", DEFAULT_DB)
    if args.init_db:
        connection = open_database(db_path)
        connection.close()
        print(f"SQLite schema ready: {db_path}")
        return 0
    if args.backfill is not None and args.backfill <= 0:
        print("ERROR: --backfill must be greater than zero", file=sys.stderr)
        return 2
    try:
        asyncio.run(run_collector(args))
        return 0
    except (RuntimeError, ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Stopped.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
