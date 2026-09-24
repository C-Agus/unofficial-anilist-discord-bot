"""AniList Discord bot — entry point.

Run with:

    python bot.py

All configuration comes from environment variables / a ``.env`` file; see
``.env.example`` and the README.
"""

from __future__ import annotations

import logging
import math
import sys

import discord
from discord.ext import commands

from commands.account import AccountCog
from commands.activity import ActivityCog
from commands.help_command import HelpCog
from config import Config, ConfigError, load_config
from database.db import Database
from services.activity_monitor import ActivityMonitor
from services.anilist import AniListClient
from utils import embeds
from utils.crypto import CryptoError, SecretBox
from utils.logging_config import setup_logging

log = logging.getLogger(__name__)


class AniListBot(commands.Bot):
    """The bot, with its database and AniList client attached."""

    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # required for prefix commands only

        super().__init__(
            command_prefix=commands.when_mentioned_or(config.command_prefix),
            intents=intents,
            help_command=None,  # replaced by our interactive hybrid /help
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True
            ),
        )
        self.config = config
        self.secrets = SecretBox(config.encryption_key)
        self.db = Database(config.db_path, self.secrets)
        self.anilist = AniListClient()

    # -- lifecycle ------------------------------------------------------------

    async def setup_hook(self) -> None:
        await self.db.connect()

        await self.add_cog(AccountCog(self))
        await self.add_cog(ActivityCog(self))
        await self.add_cog(HelpCog(self))
        await self.add_cog(ActivityMonitor(self))

        # Sync slash commands. With GUILD_ID set they appear instantly in that
        # guild; global sync can take up to an hour to propagate.
        if self.config.guild_id is not None:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d slash command(s) to guild %s.", len(synced), guild.id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global slash command(s).", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s).", self.user, self.user.id if self.user else "?")

    async def close(self) -> None:
        await self.anilist.close()
        await self.db.close()
        await super().close()

    # -- global error handling ---------------------------------------------------

    async def on_command_error(  # type: ignore[override]
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        # Unwrap hybrid/invoke wrappers to the root cause.
        while isinstance(
            error, (commands.HybridCommandError, commands.CommandInvokeError)
        ) and getattr(error, "original", None) is not None:
            error = error.original  # type: ignore[assignment]

        if isinstance(error, commands.CommandNotFound):
            return

        if isinstance(error, (commands.MissingRequiredArgument, commands.UserInputError)):
            await self._safe_send(ctx, embed=embeds.usage_embed(ctx))
            return

        if isinstance(error, commands.CommandOnCooldown):
            wait = max(1, math.ceil(error.retry_after))
            await self._safe_send(
                ctx,
                embed=embeds.info_embed(
                    f"⏳ Easy there — try that again in **{wait}s**."
                ),
            )
            return

        if isinstance(error, commands.CheckFailure):
            await self._safe_send(
                ctx, embed=embeds.error_embed("You can't use that command here.")
            )
            return

        log.error(
            "Unhandled error in command %r",
            ctx.command.qualified_name if ctx.command else "?",
            exc_info=error,
        )
        await self._safe_send(
            ctx,
            embed=embeds.error_embed(
                "Something went wrong on my end — please try again."
            ),
        )

    @staticmethod
    async def _safe_send(ctx: commands.Context, **kwargs) -> None:
        try:
            await ctx.send(ephemeral=True, **kwargs)
        except discord.HTTPException:
            log.debug("Could not deliver an error message to the channel.")


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    setup_logging(config.log_level)

    try:
        bot = AniListBot(config)
    except CryptoError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    try:
        bot.run(config.discord_token, log_handler=None)
    except discord.LoginFailure:
        log.critical("Discord rejected the token. Double-check DISCORD_TOKEN in your .env.")
        return 1
    except discord.PrivilegedIntentsRequired:
        log.critical(
            "Enable 'Message Content Intent' for your bot in the Discord Developer "
            "Portal (Bot → Privileged Gateway Intents), then restart."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
