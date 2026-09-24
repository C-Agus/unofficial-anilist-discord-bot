"""Activity commands: manually check for new AniList activity."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from discord.ext import commands

from database.db import UserRecord
from services.anilist import AniListError, AniListUserNotFound
from utils import embeds
from utils.views import LinkButtonView

if TYPE_CHECKING:
    from bot import AniListBot

log = logging.getLogger(__name__)

MAX_EMBEDS_PER_MESSAGE = 5


class ActivityCog(commands.Cog, name="Activity"):
    """Checking AniList activity on demand."""

    def __init__(self, bot: AniListBot) -> None:
        self.bot = bot

    async def _ensure_anilist_user_id(self, record: UserRecord) -> int | None:
        """Return the AniList numeric ID, resolving and caching it if missing."""
        if record.anilist_user_id is not None:
            return record.anilist_user_id
        user = await self.bot.anilist.resolve_user(record.anilist_username)
        await self.bot.db.set_anilist_user_id(record.discord_id, user.id)
        return user.id

    @commands.hybrid_command(
        name="checkactivity",
        aliases=["activity"],
        description="Show your new AniList activity since the last check.",
        usage="checkactivity",
        extras={"example": "checkactivity"},
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def checkactivity(self, ctx: commands.Context) -> None:
        record = await self.bot.db.get_user(ctx.author.id)
        if record is None:
            await ctx.send(
                embed=embeds.info_embed(
                    "You haven't linked an AniList account yet. Use "
                    f"`{ctx.clean_prefix or '!'}setusername <AniListUsername>` first."
                ),
                ephemeral=True,
            )
            return

        await ctx.defer()
        fallback = embeds.make_fallback_user(record.anilist_username, record.anilist_user_id)

        try:
            user_id = await self._ensure_anilist_user_id(record)
            activities = await self.bot.anilist.fetch_activities(user_id)
        except AniListUserNotFound:
            await ctx.send(
                embed=embeds.error_embed(
                    f"Your linked AniList username **{record.anilist_username}** no longer "
                    "exists on AniList (renamed or deleted?). Use "
                    f"`{ctx.clean_prefix or '!'}updateusername <NewName>` to fix it."
                ),
                ephemeral=True,
            )
            return
        except AniListError:
            await ctx.send(
                embed=embeds.error_embed(
                    "AniList seems to be having trouble right now — please try again "
                    "in a minute."
                ),
                ephemeral=True,
            )
            return

        if not activities:
            await ctx.send(
                embed=embeds.info_embed(
                    "No activities were found on your AniList profile yet."
                ),
                view=LinkButtonView(("Open your AniList profile", fallback.site_url)),
            )
            return

        # First-ever check (or freshly migrated account): show the latest
        # activity and set the checkpoint.
        if record.last_activity_id is None:
            latest = activities[0]
            await self.bot.db.set_last_activity_id(ctx.author.id, latest.id)
            await ctx.send(
                content=f"{ctx.author.mention}, here is your latest AniList activity:",
                embed=embeds.activity_embed(latest, fallback),
                view=LinkButtonView(("Open your AniList profile", fallback.site_url)),
            )
            return

        new_activities = sorted(
            (a for a in activities if a.id > record.last_activity_id),
            key=lambda a: a.id,
        )
        if not new_activities:
            await ctx.send(
                "No new activity since last checked.",
                view=LinkButtonView(("Open your AniList profile", fallback.site_url)),
            )
            return

        shown = new_activities[-MAX_EMBEDS_PER_MESSAGE:]
        count = len(new_activities)
        plural = "activity" if count == 1 else "activities"
        note = "" if count <= MAX_EMBEDS_PER_MESSAGE else f" (showing the latest {len(shown)})"
        await self.bot.db.set_last_activity_id(ctx.author.id, new_activities[-1].id)
        await ctx.send(
            content=f"{ctx.author.mention}, you have {count} new AniList {plural}{note}:",
            embeds=[embeds.activity_embed(a, fallback) for a in shown],
        )
