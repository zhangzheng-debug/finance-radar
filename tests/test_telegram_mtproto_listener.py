from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import telegram_mtproto_listener as listener


class TelegramStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "radar.sqlite3"
        self.connection = listener.open_database(self.db_path)
        listener.upsert_channel(
            self.connection,
            chat_id=-100123456,
            configured_ref="market_news",
            username="market_news",
            title="Market News",
            source_tier="discovery",
            enabled=True,
            note="test",
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def message(self, text: str, *, edited: bool = False) -> SimpleNamespace:
        return SimpleNamespace(
            id=42,
            message=text,
            raw_text=text,
            date=dt.datetime(2026, 7, 15, 1, 2, tzinfo=dt.timezone.utc),
            edit_date=(
                dt.datetime(2026, 7, 15, 1, 3, tzinfo=dt.timezone.utc) if edited else None
            ),
            media=None,
            photo=None,
            video=None,
            voice=None,
            audio=None,
            sticker=None,
            document=None,
            views=10,
            forwards=2,
            post_author=None,
            grouped_id=None,
            reply_to=None,
        )

    def test_upsert_edit_and_delete(self) -> None:
        listener.upsert_message(
            self.connection,
            chat_id=-100123456,
            username="market_news",
            source_tier="discovery",
            message=self.message("first"),
        )
        listener.upsert_message(
            self.connection,
            chat_id=-100123456,
            username="market_news",
            source_tier="discovery",
            message=self.message("edited", edited=True),
        )
        listener.upsert_message(
            self.connection,
            chat_id=-100123456,
            username="market_news",
            source_tier="discovery",
            message=self.message("edited", edited=True),
        )
        row = self.connection.execute(
            "SELECT * FROM telegram_source_messages WHERE chat_id=? AND message_id=?",
            (-100123456, 42),
        ).fetchone()
        self.assertEqual(row["text"], "edited")
        self.assertEqual(row["permalink"], "https://t.me/market_news/42")
        self.assertIsNotNone(row["edited_at"])
        self.assertEqual(listener.mark_deleted(self.connection, -100123456, [42]), 1)
        deleted = self.connection.execute(
            "SELECT deleted_at FROM telegram_source_messages WHERE chat_id=? AND message_id=?",
            (-100123456, 42),
        ).fetchone()[0]
        self.assertIsNotNone(deleted)
        self.assertEqual(listener.mark_deleted(self.connection, -100123456, [42]), 0)
        raw = self.connection.execute(
            "SELECT summary FROM raw_observations"
        ).fetchone()
        self.assertEqual(raw["summary"], "first")
        revisions = self.connection.execute(
            """SELECT revision_no,revision_kind,summary
               FROM source_revisions ORDER BY revision_no"""
        ).fetchall()
        self.assertEqual(
            [(row["revision_no"], row["revision_kind"], row["summary"]) for row in revisions],
            [(1, "new", "first"), (2, "edit", "edited"), (3, "delete", "edited")],
        )

    def test_schema_rejects_unknown_tier(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            listener.upsert_channel(
                self.connection,
                chat_id=-100999,
                configured_ref="bad",
                username="bad",
                title="Bad",
                source_tier="unknown",
                enabled=True,
                note="",
            )


class TelegramConfigTests(unittest.TestCase):
    def test_load_sources_normalizes_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channels.json"
            path.write_text(
                json.dumps(
                    {
                        "channels": [
                            {
                                "handle": "https://t.me/example_news/",
                                "tier": "primary",
                                "enabled": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            sources = listener.load_sources(path)
        self.assertEqual(sources[0].handle, "example_news")
        self.assertEqual(sources[0].tier, "primary")

    def test_duplicate_handles_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channels.json"
            path.write_text(
                json.dumps({"channels": [{"handle": "@News"}, {"handle": "news"}]}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                listener.load_sources(path)


if __name__ == "__main__":
    unittest.main()
