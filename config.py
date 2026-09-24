"""Application configuration.

All settings come from environment variables (optionally via a ``.env`` file).
``load_config()`` validates everything up front so the bot fails fast with a
clear message instead of crashing mid-run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

KEY_GENERATION_HINT = (
    "Generate one with:\n"
    "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _require(name: str, hint: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable {name}. {hint}")
    return value


def _optional_int(name: str) -> int | None:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}.") from exc


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable, validated runtime configuration."""

    discord_token: str
    encryption_key: str
    announce_channel_id: int | None
    guild_id: int | None
    command_prefix: str
    fetch_interval_minutes: int
    db_path: str
    log_level: str


def load_config() -> Config:
    """Load configuration from the environment (and ``.env`` if present)."""
    load_dotenv()

    discord_token = _require(
        "DISCORD_TOKEN",
        "Create a bot at https://discord.com/developers/applications and copy its token.",
    )
    encryption_key = _require("ENCRYPTION_KEY", KEY_GENERATION_HINT)

    announce_channel_id = _optional_int("CHANNEL_ID")
    guild_id = _optional_int("GUILD_ID")

    fetch_interval_minutes = _optional_int("FETCH_INTERVAL_MINUTES") or 5
    if fetch_interval_minutes < 1:
        raise ConfigError("FETCH_INTERVAL_MINUTES must be at least 1.")

    command_prefix = (os.getenv("COMMAND_PREFIX") or "!").strip() or "!"
    db_path = (os.getenv("DB_PATH") or "anilist_bot.db").strip() or "anilist_bot.db"
    log_level = (os.getenv("LOG_LEVEL") or "INFO").strip().upper() or "INFO"

    return Config(
        discord_token=discord_token,
        encryption_key=encryption_key,
        announce_channel_id=announce_channel_id,
        guild_id=guild_id,
        command_prefix=command_prefix,
        fetch_interval_minutes=fetch_interval_minutes,
        db_path=db_path,
        log_level=log_level,
    )
