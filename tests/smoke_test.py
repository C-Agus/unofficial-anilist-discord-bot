"""Offline smoke tests — no Discord or AniList connection required.

Run from the project root:

    python -m tests.smoke_test

Covers: crypto round-trips, database CRUD + uniqueness + legacy migration,
GraphQL payload parsing, embed construction, and bot object wiring.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.fernet import Fernet  # noqa: E402

from config import Config  # noqa: E402
from database.db import Database, UsernameTakenError  # noqa: E402
from services.anilist import (  # noqa: E402
    ListActivity,
    TextActivity,
    _parse_activity,
)
from utils.crypto import CryptoError, SecretBox  # noqa: E402
from utils.embeds import activity_embed, make_fallback_user  # noqa: E402


def make_box() -> SecretBox:
    return SecretBox(Fernet.generate_key().decode())


def test_crypto() -> None:
    box = make_box()
    token = box.encrypt("GustyFrog")
    assert token != "GustyFrog"
    assert box.decrypt(token) == "GustyFrog"
    # Deterministic + case-insensitive fingerprints
    assert box.fingerprint("GustyFrog") == box.fingerprint("  gustyfrog ")
    assert box.fingerprint("GustyFrog") != box.fingerprint("OtherName")
    # A different key cannot decrypt
    other = make_box()
    try:
        other.decrypt(token)
        raise AssertionError("expected CryptoError with wrong key")
    except CryptoError:
        pass
    # Invalid key is rejected up front
    try:
        SecretBox("not-a-valid-key")
        raise AssertionError("expected CryptoError for invalid key")
    except CryptoError:
        pass
    print("ok  crypto")


async def test_database() -> None:
    box = make_box()
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "test.db"), box)
        await db.connect()

        await db.link_user(1, "GustyFrog", 123, 999)
        record = await db.get_user(1)
        assert record is not None
        assert record.anilist_username == "GustyFrog"
        assert record.anilist_user_id == 123
        assert record.last_activity_id == 999

        # Uniqueness is case-insensitive and cross-user
        try:
            await db.link_user(2, "gustyfrog", None, None)
            raise AssertionError("expected UsernameTakenError")
        except UsernameTakenError:
            pass

        # A user may re-link / rename freely
        await db.link_user(1, "NewName", 456, None)
        record = await db.get_user(1)
        assert record is not None and record.anilist_username == "NewName"

        # The old name is now free for someone else
        await db.link_user(2, "GustyFrog", 123, None)

        await db.set_last_activity_id(1, 1234)
        record = await db.get_user(1)
        assert record is not None and record.last_activity_id == 1234

        await db.set_anilist_user_id(2, 777)
        record2 = await db.get_user(2)
        assert record2 is not None and record2.anilist_user_id == 777

        assert await db.remove_user(2) is True
        assert await db.remove_user(2) is False
        assert len(await db.get_all_users()) == 1
        await db.close()
    print("ok  database CRUD + uniqueness")


async def test_migration() -> None:
    box = make_box()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "legacy.db")
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE users (
                discord_id INTEGER PRIMARY KEY,
                anilist_username TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO users VALUES (?, ?)",
            [(111, "Alice"), (222, "Bob"), (333, "alice")],  # 333 duplicates 111
        )
        conn.commit()
        conn.close()

        db = Database(path, box)
        await db.connect()
        users = {u.discord_id: u for u in await db.get_all_users()}
        assert users[111].anilist_username == "Alice"
        assert users[222].anilist_username == "Bob"
        assert 333 not in users, "duplicate username should have been skipped"
        assert users[111].anilist_user_id is None  # backfilled lazily at runtime
        assert users[111].last_activity_id is None  # baselined silently at runtime
        await db.close()

        # Re-opening must be a no-op (schema version recorded)
        db2 = Database(path, box)
        await db2.connect()
        assert len(await db2.get_all_users()) == 2
        await db2.close()
    print("ok  legacy schema migration")


SAMPLE_LIST_ACTIVITY = {
    "__typename": "ListActivity",
    "id": 8123456,
    "status": "watched episode",
    "progress": "5",
    "createdAt": 1751500000,
    "siteUrl": "https://anilist.co/activity/8123456",
    "user": {
        "id": 123,
        "name": "GustyFrog",
        "siteUrl": "https://anilist.co/user/GustyFrog/",
        "avatar": {"large": "https://img.anili.st/avatar.png"},
    },
    "media": {
        "type": "ANIME",
        "siteUrl": "https://anilist.co/anime/1",
        "title": {"romaji": "Cowboy Bebop", "english": "Cowboy Bebop"},
        "coverImage": {"large": "https://img.anili.st/cover.png"},
    },
}

SAMPLE_TEXT_ACTIVITY = {
    "__typename": "TextActivity",
    "id": 8123457,
    "text": "Finally caught up! img220(https://example.com/pic.png)\n\n\n\nGreat season.",
    "createdAt": 1751500100,
    "siteUrl": "https://anilist.co/activity/8123457",
    "user": {"id": 123, "name": "GustyFrog", "siteUrl": None, "avatar": None},
}


def test_parsing_and_embeds() -> None:
    parsed_list = _parse_activity(SAMPLE_LIST_ACTIVITY)
    assert isinstance(parsed_list, ListActivity)
    assert parsed_list.media_title == "Cowboy Bebop"
    assert parsed_list.cover_image_url is not None
    assert parsed_list.user is not None and parsed_list.user.name == "GustyFrog"

    parsed_text = _parse_activity(SAMPLE_TEXT_ACTIVITY)
    assert isinstance(parsed_text, TextActivity)

    # Unknown/malformed entries are skipped, never crash
    assert _parse_activity({}) is None
    assert _parse_activity({"__typename": "MessageActivity", "id": 1}) is None
    assert _parse_activity({"__typename": "ListActivity"}) is None  # missing fields

    fallback = make_fallback_user("GustyFrog", 123)
    embed_list = activity_embed(parsed_list, fallback)
    assert "Cowboy Bebop" in (embed_list.description or "")
    assert "Watched episode 5" in (embed_list.description or "")
    assert embed_list.thumbnail.url == "https://img.anili.st/cover.png"
    assert embed_list.author.name == "GustyFrog"
    assert embed_list.timestamp is not None

    embed_text = activity_embed(parsed_text, fallback)
    assert "*[image]*" in (embed_text.description or "")
    assert "\n\n\n" not in (embed_text.description or "")
    print("ok  activity parsing + embeds")


async def test_bot_construction() -> None:
    from bot import AniListBot
    from commands.account import AccountCog
    from commands.activity import ActivityCog
    from commands.help_command import HelpCog, build_help_embeds
    from services.activity_monitor import ActivityMonitor

    config = Config(
        discord_token="dummy",
        encryption_key=Fernet.generate_key().decode(),
        announce_channel_id=None,  # monitor loop stays disabled
        guild_id=None,
        command_prefix="!",
        fetch_interval_minutes=5,
        db_path=":memory:",
        log_level="INFO",
    )
    bot = AniListBot(config)
    # Cogs are normally loaded in setup_hook at login; load them directly here.
    await bot.add_cog(AccountCog(bot))
    await bot.add_cog(ActivityCog(bot))
    await bot.add_cog(HelpCog(bot))
    await bot.add_cog(ActivityMonitor(bot))

    for name in ("setusername", "updateusername", "removeusername", "checkactivity",
                 "whoami", "help"):
        assert bot.get_command(name) is not None, f"command {name} not registered"
    assert bot.get_command("link") is not None  # alias
    assert bot.get_command("unlink") is not None  # alias

    pages = build_help_embeds(bot)
    assert set(pages) == {"overview", "account", "activity"}
    assert pages["account"].fields, "account help page should list commands"
    print("ok  bot wiring + command registration + help pages")


async def main() -> None:
    test_crypto()
    await test_database()
    await test_migration()
    test_parsing_and_embeds()
    await test_bot_construction()
    print("\nAll smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
