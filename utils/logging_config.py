"""Structured logging configuration.

A single consistent format across the app and discord.py. To avoid leaking the
sensitive Discord-account -> AniList-account mapping, application code logs
Discord IDs on their own and never pairs them with AniList usernames.
"""

from __future__ import annotations

import logging

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO") -> None:
    resolved = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=resolved, format=LOG_FORMAT, datefmt=DATE_FORMAT)

    # discord.py is chatty at INFO; keep it quieter unless we're debugging.
    if resolved > logging.DEBUG:
        logging.getLogger("discord").setLevel(logging.WARNING)
        logging.getLogger("discord.http").setLevel(logging.WARNING)
