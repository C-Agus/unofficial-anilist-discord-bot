"""Interactive, categorized ``help`` command with a dropdown navigator."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from utils.embeds import ANILIST_BLUE

if TYPE_CHECKING:
    from bot import AniListBot

VIEW_TIMEOUT_SECONDS = 180.0

CATEGORIES: dict[str, dict] = {
    "overview": {
        "label": "Overview",
        "emoji": "🏠",
        "description": "What this bot does and how to get started",
    },
    "account": {
        "label": "Account",
        "emoji": "👤",
        "description": "Link, update, or unlink your AniList account",
        "commands": ["setusername", "updateusername", "removeusername", "whoami"],
    },
    "activity": {
        "label": "Activity",
        "emoji": "📈",
        "description": "Check your AniList activity",
        "commands": ["checkactivity"],
    },
}


def _command_field(bot: AniListBot, name: str) -> tuple[str, str] | None:
    command = bot.get_command(name)
    if command is None:
        return None
    prefix = bot.config.command_prefix
    usage = command.usage or command.qualified_name
    lines = [command.description or command.help or ""]
    example = (command.extras or {}).get("example")
    if example:
        lines.append(f"Example: `{prefix}{example}`")
    if command.aliases:
        aliases = ", ".join(f"`{prefix}{alias}`" for alias in command.aliases)
        lines.append(f"Aliases: {aliases}")
    return f"`{prefix}{usage}`  ·  `/{command.qualified_name}`", "\n".join(filter(None, lines))


def build_help_embeds(bot: AniListBot) -> dict[str, discord.Embed]:
    """Build one embed per help category."""
    prefix = bot.config.command_prefix
    pages: dict[str, discord.Embed] = {}

    overview = discord.Embed(
        title="📖 AniList Bot — Help",
        color=ANILIST_BLUE,
        description=(
            "I connect Discord accounts to [AniList](https://anilist.co) profiles and "
            "announce new anime/manga activity.\n\n"
            f"**Getting started:** link your account with `{prefix}setusername "
            "<AniListUsername>` — after that I'll post your new activity "
            "automatically, and you can check on demand with "
            f"`{prefix}checkactivity`.\n\n"
            "Every command also works as a **slash command** (type `/` to browse). "
            "Use the dropdown below to explore each category."
        ),
    )
    interval = bot.config.fetch_interval_minutes
    overview.add_field(
        name="Automatic announcements",
        value=(
            f"New activity for every linked user is checked every **{interval} min** "
            "and posted to the configured channel."
            if bot.config.announce_channel_id
            else "Not configured on this instance (set `CHANNEL_ID` to enable)."
        ),
        inline=False,
    )
    pages["overview"] = overview

    for key, meta in CATEGORIES.items():
        if "commands" not in meta:
            continue
        embed = discord.Embed(
            title=f"{meta['emoji']} {meta['label']} commands",
            description=meta["description"],
            color=ANILIST_BLUE,
        )
        for command_name in meta["commands"]:
            field = _command_field(bot, command_name)
            if field:
                embed.add_field(name=field[0], value=field[1], inline=False)
        embed.set_footer(text="Angle brackets like <this> mark required arguments.")
        pages[key] = embed

    return pages


class HelpSelect(discord.ui.Select):
    def __init__(self, pages: dict[str, discord.Embed], author_id: int) -> None:
        options = [
            discord.SelectOption(
                label=meta["label"],
                value=key,
                emoji=meta["emoji"],
                description=meta["description"][:100],
            )
            for key, meta in CATEGORIES.items()
        ]
        super().__init__(placeholder="Choose a category…", options=options)
        self._pages = pages
        self._author_id = author_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._author_id:
            await interaction.response.send_message(
                "This help menu belongs to someone else — run the help command yourself!",
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(embed=self._pages[self.values[0]])


class HelpView(discord.ui.View):
    def __init__(self, pages: dict[str, discord.Embed], author_id: int) -> None:
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.message: discord.Message | None = None
        self.add_item(HelpSelect(pages, author_id))

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, (discord.ui.Button, discord.ui.Select)):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class HelpCog(commands.Cog, name="Help"):
    """The interactive help command."""

    def __init__(self, bot: AniListBot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="help",
        description="Show all commands and how to use them.",
        usage="help",
        extras={"example": "help"},
    )
    async def help(self, ctx: commands.Context) -> None:
        pages = build_help_embeds(self.bot)
        view = HelpView(pages, ctx.author.id)
        view.message = await ctx.send(embed=pages["overview"], view=view, ephemeral=True)
