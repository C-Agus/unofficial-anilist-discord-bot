"""Account commands: link, update, unlink, and inspect AniList account links.

Every command is a *hybrid* command — it works both as a prefix command
(``!setusername``) and a slash command (``/setusername``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from discord import app_commands
from discord.ext import commands

from database.db import UsernameTakenError
from services.anilist import AniListError, AniListUserNotFound
from utils import embeds
from utils.views import ConfirmView, LinkButtonView

if TYPE_CHECKING:
    from bot import AniListBot

log = logging.getLogger(__name__)

MAX_USERNAME_LENGTH = 30


def _looks_like_username(candidate: str) -> bool:
    return 1 < len(candidate) <= MAX_USERNAME_LENGTH and not any(
        ch.isspace() for ch in candidate
    )


class AccountCog(commands.Cog, name="Account"):
    """Linking Discord accounts to AniList accounts."""

    def __init__(self, bot: AniListBot) -> None:
        self.bot = bot

    # -- shared implementation -------------------------------------------------

    async def _link(self, ctx: commands.Context, username: str, verb: str) -> None:
        username = username.strip()
        if not _looks_like_username(username):
            await ctx.send(
                embed=embeds.error_embed(
                    "That doesn't look like a valid AniList username "
                    f"(2–{MAX_USERNAME_LENGTH} characters, no spaces)."
                ),
                ephemeral=True,
            )
            return

        await ctx.defer()

        # 1. Validate the username against the AniList API.
        try:
            user = await self.bot.anilist.resolve_user(username)
        except AniListUserNotFound:
            await ctx.send(
                embed=embeds.error_embed(
                    f"I couldn't find an AniList user named **{username}**. "
                    "Double-check the spelling on your profile page."
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

        # 2. Enforce one-AniList-account-per-Discord-user (friendly pre-check;
        #    the DB UNIQUE constraint is the final backstop).
        owner = await self.bot.db.owner_of_username(user.name)
        if owner is not None and owner != ctx.author.id:
            await ctx.send(
                embed=embeds.error_embed(
                    f"**{user.name}** is already linked to another Discord account "
                    "on this bot."
                ),
                ephemeral=True,
            )
            return

        # 3. Baseline the activity checkpoint so old activity isn't re-announced.
        baseline: int | None = None
        try:
            baseline = await self.bot.anilist.fetch_latest_activity_id(user.id)
        except AniListError:
            log.warning("Could not baseline activity for a new link; will baseline later.")

        try:
            await self.bot.db.link_user(ctx.author.id, user.name, user.id, baseline)
        except UsernameTakenError:
            await ctx.send(
                embed=embeds.error_embed(
                    f"**{user.name}** is already linked to another Discord account "
                    "on this bot."
                ),
                ephemeral=True,
            )
            return

        embed = embeds.success_embed(
            f"{ctx.author.mention} is now {verb} to AniList user **{user.name}**.\n"
            "I'll announce new activity automatically — or run "
            f"`{ctx.clean_prefix or '!'}checkactivity` any time."
        )
        if user.avatar_url:
            embed.set_thumbnail(url=user.avatar_url)
        await ctx.send(
            embed=embed,
            view=LinkButtonView(("View AniList profile", user.site_url)),
        )
        log.info("AniList account %s for discord_id=%s.", verb, ctx.author.id)

    # -- commands ----------------------------------------------------------------

    @commands.hybrid_command(
        name="setusername",
        aliases=["link"],
        description="Link your AniList account to your Discord account.",
        usage="setusername <AniListUsername>",
        extras={"example": "setusername GustyFrog"},
    )
    @app_commands.describe(username="Your AniList username, exactly as shown on your profile")
    @commands.cooldown(2, 30, commands.BucketType.user)
    async def setusername(self, ctx: commands.Context, username: str) -> None:
        await self._link(ctx, username, verb="linked")

    @commands.hybrid_command(
        name="updateusername",
        description="Change which AniList account is linked to you.",
        usage="updateusername <AniListUsername>",
        extras={"example": "updateusername MyNewName"},
    )
    @app_commands.describe(username="The new AniList username to link")
    @commands.cooldown(2, 30, commands.BucketType.user)
    async def updateusername(self, ctx: commands.Context, username: str) -> None:
        await self._link(ctx, username, verb="updated and linked")

    @commands.hybrid_command(
        name="removeusername",
        aliases=["unlink"],
        description="Unlink your AniList account (asks for confirmation).",
        usage="removeusername",
        extras={"example": "removeusername"},
    )
    async def removeusername(self, ctx: commands.Context) -> None:
        record = await self.bot.db.get_user(ctx.author.id)
        if record is None:
            await ctx.send(
                embed=embeds.info_embed(
                    "You don't have an AniList account linked yet. Use "
                    f"`{ctx.clean_prefix or '!'}setusername <AniListUsername>` first."
                ),
                ephemeral=True,
            )
            return

        view = ConfirmView(ctx.author.id)
        view.message = await ctx.send(
            embed=embeds.info_embed(
                f"Unlink AniList account **{record.anilist_username}** from your "
                "Discord account? Automatic activity announcements will stop."
            ),
            view=view,
        )
        await view.wait()

        if view.value is True:
            await self.bot.db.remove_user(ctx.author.id)
            await ctx.send(embed=embeds.success_embed("Your AniList account has been unlinked."))
            log.info("AniList account unlinked for discord_id=%s.", ctx.author.id)
        elif view.value is False:
            await ctx.send(embed=embeds.info_embed("Cancelled — nothing was changed."))
        # On timeout the buttons are disabled by the view; nothing else to do.

    @commands.hybrid_command(
        name="whoami",
        aliases=["mystatus"],
        description="Show which AniList account is linked to you.",
        usage="whoami",
        extras={"example": "whoami"},
    )
    async def whoami(self, ctx: commands.Context) -> None:
        record = await self.bot.db.get_user(ctx.author.id)
        if record is None:
            await ctx.send(
                embed=embeds.info_embed(
                    "You don't have an AniList account linked yet. Use "
                    f"`{ctx.clean_prefix or '!'}setusername <AniListUsername>` to link one."
                ),
                ephemeral=True,
            )
            return
        fallback = embeds.make_fallback_user(record.anilist_username, record.anilist_user_id)
        await ctx.send(
            embed=embeds.info_embed(
                f"You are linked to AniList user **{record.anilist_username}**."
            ),
            view=LinkButtonView(("View AniList profile", fallback.site_url)),
            ephemeral=True,
        )
