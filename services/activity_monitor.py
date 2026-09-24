"""Background task that announces new AniList activity for all linked users.

Key behaviors:

* Checkpoints (``last_activity_id``) live in the database, so restarts never
  cause duplicate or missed announcements.
* A user whose checkpoint is ``NULL`` (fresh migration) is *baselined*
  silently instead of having week-old activity re-announced.
* The checkpoint is only advanced **after** the announcement is successfully
  sent, so a failed send is retried on the next cycle instead of being lost.
* Errors for one user never break the loop for everyone else.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from database.db import UserRecord
from services.anilist import AniListError, AniListUserNotFound
from utils import embeds

if TYPE_CHECKING:
    from bot import AniListBot

log = logging.getLogger(__name__)

MAX_EMBEDS_PER_MESSAGE = 5


class ActivityMonitor(commands.Cog):
    """Periodically checks AniList and announces new activity."""

    def __init__(self, bot: AniListBot) -> None:
        self.bot = bot
        self._channel: discord.abc.Messageable | None = None
        self._warned_no_channel = False

        if bot.config.announce_channel_id is None:
            log.warning(
                "CHANNEL_ID is not set — automatic activity announcements are disabled. "
                "Commands still work normally."
            )
            return

        self.check_activity_feed.change_interval(
            minutes=bot.config.fetch_interval_minutes
        )
        self.check_activity_feed.start()

    async def cog_unload(self) -> None:
        self.check_activity_feed.cancel()

    # -- the loop -----------------------------------------------------------------

    @tasks.loop(minutes=5)
    async def check_activity_feed(self) -> None:
        channel = await self._get_channel()
        if channel is None:
            return

        users = await self.bot.db.get_all_users()
        if not users:
            return
        log.debug("Checking AniList activity for %d linked user(s).", len(users))

        for record in users:
            try:
                await self._process_user(record, channel)
            except Exception:
                log.exception(
                    "Unexpected error while checking activity for discord_id=%s",
                    record.discord_id,
                )

    @check_activity_feed.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    @check_activity_feed.error
    async def _on_loop_error(self, error: BaseException) -> None:
        log.exception("Activity feed loop crashed; it will restart.", exc_info=error)
        self.check_activity_feed.restart()

    # -- helpers --------------------------------------------------------------------

    async def _get_channel(self) -> discord.abc.Messageable | None:
        if self._channel is not None:
            return self._channel
        channel_id = self.bot.config.announce_channel_id
        assert channel_id is not None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                channel = None
        if channel is None or not isinstance(channel, discord.abc.Messageable):
            if not self._warned_no_channel:
                log.error(
                    "Could not access announcement channel %s. Check CHANNEL_ID and the "
                    "bot's channel permissions.",
                    channel_id,
                )
                self._warned_no_channel = True
            return None
        self._warned_no_channel = False
        self._channel = channel
        return channel

    async def _process_user(
        self, record: UserRecord, channel: discord.abc.Messageable
    ) -> None:
        # Backfill the AniList numeric ID for accounts migrated from the old schema.
        anilist_user_id = record.anilist_user_id
        if anilist_user_id is None:
            try:
                resolved = await self.bot.anilist.resolve_user(record.anilist_username)
            except AniListUserNotFound:
                log.warning(
                    "Stored AniList username for discord_id=%s no longer exists; "
                    "skipping until they re-link.",
                    record.discord_id,
                )
                return
            except AniListError as exc:
                log.warning(
                    "Could not resolve AniList ID for discord_id=%s: %s",
                    record.discord_id,
                    exc,
                )
                return
            anilist_user_id = resolved.id
            await self.bot.db.set_anilist_user_id(record.discord_id, anilist_user_id)

        try:
            activities = await self.bot.anilist.fetch_activities(
                anilist_user_id, use_cache=False
            )
        except AniListError as exc:
            log.warning(
                "Could not fetch activities for discord_id=%s: %s", record.discord_id, exc
            )
            return

        if not activities:
            return

        # Fresh link/migration: establish a baseline silently.
        if record.last_activity_id is None:
            await self.bot.db.set_last_activity_id(record.discord_id, activities[0].id)
            log.info("Baselined activity checkpoint for discord_id=%s.", record.discord_id)
            return

        new_activities = sorted(
            (a for a in activities if a.id > record.last_activity_id),
            key=lambda a: a.id,
        )
        if not new_activities:
            return

        shown = new_activities[-MAX_EMBEDS_PER_MESSAGE:]
        fallback = embeds.make_fallback_user(record.anilist_username, anilist_user_id)
        try:
            await channel.send(
                content=f"<@{record.discord_id}> has new AniList activity!",
                embeds=[embeds.activity_embed(a, fallback) for a in shown],
            )
        except discord.Forbidden:
            log.error(
                "Missing permission to post in the announcement channel; will retry "
                "next cycle."
            )
            return
        except discord.HTTPException as exc:
            log.error("Failed to send announcement (will retry next cycle): %s", exc)
            return

        # Advance the checkpoint only after a successful send.
        await self.bot.db.set_last_activity_id(record.discord_id, new_activities[-1].id)
        log.info(
            "Announced %d new activit%s for discord_id=%s.",
            len(new_activities),
            "y" if len(new_activities) == 1 else "ies",
            record.discord_id,
        )
