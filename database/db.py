"""Database abstraction layer (async SQLite via ``aiosqlite``).

All persistence goes through :class:`Database`. Nothing else in the codebase
touches SQL. Every query is parameterized — no string interpolation anywhere.

Schema (version 1):

    users(
        discord_id                  INTEGER PRIMARY KEY,
        anilist_username_encrypted  TEXT NOT NULL,          -- Fernet ciphertext
        anilist_username_hash       TEXT NOT NULL UNIQUE,   -- keyed HMAC fingerprint
        anilist_user_id             INTEGER,                -- AniList numeric ID (cached)
        last_activity_id            INTEGER,                -- last announced/seen activity
        created_at                  TEXT NOT NULL,
        updated_at                  TEXT NOT NULL
    )

The ``UNIQUE`` constraint on the fingerprint enforces "one AniList account per
Discord user" at the database level; :meth:`Database.link_user` enforces it at
the application level with a friendlier error.

Databases created by the original version of this bot (plain-text usernames,
no extra columns) are detected and migrated automatically on startup.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

import aiosqlite

from utils.crypto import CryptoError, SecretBox

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    discord_id                 INTEGER PRIMARY KEY,
    anilist_username_encrypted TEXT    NOT NULL,
    anilist_username_hash      TEXT    NOT NULL UNIQUE,
    anilist_user_id            INTEGER,
    last_activity_id           INTEGER,
    created_at                 TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at                 TEXT    NOT NULL DEFAULT (datetime('now'))
)
"""


class DatabaseError(RuntimeError):
    """Base class for database-layer errors."""


class UsernameTakenError(DatabaseError):
    """The AniList username is already linked to a different Discord user."""


@dataclass(slots=True)
class UserRecord:
    """A decrypted row from the ``users`` table."""

    discord_id: int
    anilist_username: str
    anilist_user_id: int | None
    last_activity_id: int | None


class Database:
    """Async persistence layer for user links and activity checkpoints."""

    def __init__(self, path: str, secrets: SecretBox) -> None:
        self._path = path
        self._secrets = secrets
        self._conn: aiosqlite.Connection | None = None

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        """Open the database, creating/migrating the schema as needed."""
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._migrate()
        log.info("Database ready at %s (schema v%d).", self._path, SCHEMA_VERSION)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise DatabaseError("Database.connect() has not been called.")
        return self._conn

    # -- schema management ----------------------------------------------------

    async def _migrate(self) -> None:
        conn = self.conn
        async with conn.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        version = row[0] if row else 0

        if version >= SCHEMA_VERSION:
            return

        legacy_rows: list[tuple[int, str]] = []
        if await self._table_exists("users"):
            columns = await self._table_columns("users")
            if "anilist_username" in columns and "anilist_username_hash" not in columns:
                log.info("Legacy database schema detected; migrating...")
                async with conn.execute(
                    "SELECT discord_id, anilist_username FROM users"
                ) as cursor:
                    legacy_rows = [(r[0], r[1]) async for r in cursor]
                await conn.execute("ALTER TABLE users RENAME TO users_legacy")

        await conn.execute(CREATE_USERS_TABLE)

        migrated = skipped = 0
        for discord_id, username in legacy_rows:
            try:
                await conn.execute(
                    """
                    INSERT INTO users
                        (discord_id, anilist_username_encrypted, anilist_username_hash)
                    VALUES (?, ?, ?)
                    """,
                    (
                        discord_id,
                        self._secrets.encrypt(username),
                        self._secrets.fingerprint(username),
                    ),
                )
                migrated += 1
            except sqlite3.IntegrityError:
                skipped += 1
                log.warning(
                    "Migration: skipped discord_id=%s because their AniList username "
                    "was already claimed by another migrated user. They will need to "
                    "re-link.",
                    discord_id,
                )

        if legacy_rows or await self._table_exists("users_legacy"):
            await conn.execute("DROP TABLE IF EXISTS users_legacy")
        if migrated or skipped:
            log.info("Migration complete: %d user(s) migrated, %d skipped.", migrated, skipped)

        await conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        await conn.commit()

    async def _table_exists(self, name: str) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _table_columns(self, name: str) -> set[str]:
        async with self.conn.execute(f"PRAGMA table_info({name})") as cursor:
            return {row[1] async for row in cursor}

    # -- row helpers -----------------------------------------------------------

    def _to_record(self, row: aiosqlite.Row) -> UserRecord:
        return UserRecord(
            discord_id=row["discord_id"],
            anilist_username=self._secrets.decrypt(row["anilist_username_encrypted"]),
            anilist_user_id=row["anilist_user_id"],
            last_activity_id=row["last_activity_id"],
        )

    # -- queries ---------------------------------------------------------------

    async def get_user(self, discord_id: int) -> UserRecord | None:
        async with self.conn.execute(
            "SELECT * FROM users WHERE discord_id = ?", (discord_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return self._to_record(row) if row else None

    async def get_all_users(self) -> list[UserRecord]:
        records: list[UserRecord] = []
        async with self.conn.execute("SELECT * FROM users ORDER BY discord_id") as cursor:
            async for row in cursor:
                try:
                    records.append(self._to_record(row))
                except CryptoError:
                    log.error(
                        "Could not decrypt stored username for discord_id=%s "
                        "(ENCRYPTION_KEY changed?); skipping.",
                        row["discord_id"],
                    )
        return records

    async def owner_of_username(self, username: str) -> int | None:
        """Return the Discord ID that has linked ``username``, if any."""
        fingerprint = self._secrets.fingerprint(username)
        async with self.conn.execute(
            "SELECT discord_id FROM users WHERE anilist_username_hash = ?",
            (fingerprint,),
        ) as cursor:
            row = await cursor.fetchone()
        return row["discord_id"] if row else None

    async def link_user(
        self,
        discord_id: int,
        anilist_username: str,
        anilist_user_id: int | None,
        last_activity_id: int | None,
    ) -> None:
        """Create or update the link between a Discord user and an AniList account.

        Raises :class:`UsernameTakenError` if another Discord user already
        claimed this AniList username.
        """
        owner = await self.owner_of_username(anilist_username)
        if owner is not None and owner != discord_id:
            raise UsernameTakenError(
                f"AniList username {anilist_username!r} is already linked to another "
                "Discord account."
            )
        try:
            await self.conn.execute(
                """
                INSERT INTO users
                    (discord_id, anilist_username_encrypted, anilist_username_hash,
                     anilist_user_id, last_activity_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(discord_id) DO UPDATE SET
                    anilist_username_encrypted = excluded.anilist_username_encrypted,
                    anilist_username_hash      = excluded.anilist_username_hash,
                    anilist_user_id            = excluded.anilist_user_id,
                    last_activity_id           = excluded.last_activity_id,
                    updated_at                 = datetime('now')
                """,
                (
                    discord_id,
                    self._secrets.encrypt(anilist_username),
                    self._secrets.fingerprint(anilist_username),
                    anilist_user_id,
                    last_activity_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # Race-condition backstop: the UNIQUE constraint is the source of truth.
            raise UsernameTakenError(
                f"AniList username {anilist_username!r} is already linked to another "
                "Discord account."
            ) from exc
        await self.conn.commit()

    async def remove_user(self, discord_id: int) -> bool:
        """Remove a user's link. Returns True if a row was deleted."""
        cursor = await self.conn.execute(
            "DELETE FROM users WHERE discord_id = ?", (discord_id,)
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def set_last_activity_id(self, discord_id: int, activity_id: int) -> None:
        await self.conn.execute(
            """
            UPDATE users
               SET last_activity_id = ?, updated_at = datetime('now')
             WHERE discord_id = ?
            """,
            (activity_id, discord_id),
        )
        await self.conn.commit()

    async def set_anilist_user_id(self, discord_id: int, anilist_user_id: int) -> None:
        await self.conn.execute(
            """
            UPDATE users
               SET anilist_user_id = ?, updated_at = datetime('now')
             WHERE discord_id = ?
            """,
            (anilist_user_id, discord_id),
        )
        await self.conn.commit()
