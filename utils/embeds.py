"""Discord embed builders.

Centralizes all presentation logic so activity announcements, command
responses, and error messages look consistent everywhere.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import discord
from discord.ext import commands

from services.anilist import Activity, AniListUser, ListActivity, TextActivity, profile_url

ANILIST_BLUE = 0x3DB4F2
COLOR_COMPLETED = 0x4CCA51
COLOR_PLANNING = 0x9FA6B2
COLOR_PAUSED = 0xF7BF63
COLOR_DROPPED = 0xE85D75
COLOR_TEXT_POST = 0x8B5CF6
COLOR_SUCCESS = 0x4CCA51
COLOR_ERROR = 0xE85D75
COLOR_WARNING = 0xF7BF63

TEXT_PREVIEW_LIMIT = 350

_IMG_TAG_RE = re.compile(r"img\d*\s*\(\s*[^)\s]*\s*\)", re.IGNORECASE)


def make_fallback_user(username: str, anilist_user_id: int | None) -> AniListUser:
    """Build a minimal AniList user object from stored data (no API call)."""
    return AniListUser(
        id=anilist_user_id or 0,
        name=username,
        site_url=profile_url(username),
        avatar_url=None,
    )


def _status_color(status: str) -> int:
    lowered = status.lower()
    if "complete" in lowered:
        return COLOR_COMPLETED
    if "plan" in lowered:
        return COLOR_PLANNING
    if "paus" in lowered:
        return COLOR_PAUSED
    if "drop" in lowered:
        return COLOR_DROPPED
    return ANILIST_BLUE


def _clean_text(text: str) -> str:
    """Make AniList markdown presentable inside a Discord embed."""
    cleaned = _IMG_TAG_RE.sub("*[image]*", text).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if len(cleaned) > TEXT_PREVIEW_LIMIT:
        cleaned = cleaned[: TEXT_PREVIEW_LIMIT - 1].rstrip() + "…"
    return cleaned or "*[empty post]*"


def activity_embed(activity: Activity, fallback_user: AniListUser) -> discord.Embed:
    """Render one AniList activity as a rich embed."""
    user = activity.user or fallback_user
    timestamp = datetime.fromtimestamp(activity.created_at, tz=timezone.utc)

    if isinstance(activity, ListActivity):
        action = activity.status.capitalize()
        if activity.progress:
            action = f"{action} {activity.progress} of"
        title = discord.utils.escape_markdown(activity.media_title)
        title_md = f"[{title}]({activity.media_url})" if activity.media_url else f"**{title}**"
        emoji = {"ANIME": "📺", "MANGA": "📖"}.get(activity.media_type or "", "📌")
        embed = discord.Embed(
            description=f"{emoji} **{action}** {title_md}",
            color=_status_color(activity.status),
            timestamp=timestamp,
        )
        if activity.cover_image_url:
            embed.set_thumbnail(url=activity.cover_image_url)
        embed.set_footer(text="AniList · List activity")
    else:  # TextActivity
        embed = discord.Embed(
            description=f"💬 {_clean_text(activity.text)}",
            color=COLOR_TEXT_POST,
            timestamp=timestamp,
        )
        embed.set_footer(text="AniList · Status post")

    if user.avatar_url:
        embed.set_author(name=user.name, url=user.site_url, icon_url=user.avatar_url)
    else:
        embed.set_author(name=user.name, url=user.site_url)
    return embed


def success_embed(description: str) -> discord.Embed:
    return discord.Embed(description=f"✅ {description}", color=COLOR_SUCCESS)


def error_embed(description: str) -> discord.Embed:
    return discord.Embed(description=f"❌ {description}", color=COLOR_ERROR)


def info_embed(description: str) -> discord.Embed:
    return discord.Embed(description=description, color=ANILIST_BLUE)


def usage_embed(ctx: commands.Context) -> discord.Embed:
    """Friendly 'how to use this command' embed for missing/invalid arguments."""
    command = ctx.command
    prefix = getattr(ctx, "clean_prefix", None) or "!"
    embed = discord.Embed(
        title=f"How to use {prefix}{command.qualified_name}",
        description=command.description or command.help or "",
        color=COLOR_WARNING,
    )
    usage = command.usage or command.qualified_name
    embed.add_field(name="Usage", value=f"`{prefix}{usage}`", inline=False)
    example = (command.extras or {}).get("example")
    if example:
        embed.add_field(name="Example", value=f"`{prefix}{example}`", inline=False)
    embed.set_footer(text=f"Tip: this also works as the slash command /{command.qualified_name}")
    return embed
