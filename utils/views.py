"""Interactive Discord UI components (buttons, confirmation dialogs)."""

from __future__ import annotations

import discord


class LinkButtonView(discord.ui.View):
    """One or more URL buttons (e.g. 'View on AniList').

    Link buttons never expire and require no callbacks, so timeout is disabled.
    """

    def __init__(self, *buttons: tuple[str, str]) -> None:
        super().__init__(timeout=None)
        for label, url in buttons:
            self.add_item(discord.ui.Button(label=label, url=url))


class ConfirmView(discord.ui.View):
    """A Confirm/Cancel button pair restricted to the invoking user.

    After :meth:`wait` returns, ``self.value`` is True (confirmed),
    False (cancelled), or None (timed out).
    """

    def __init__(self, author_id: int, *, timeout: float = 60.0) -> None:
        super().__init__(timeout=timeout)
        self.value: bool | None = None
        self.message: discord.Message | None = None
        self._author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._author_id:
            await interaction.response.send_message(
                "This confirmation belongs to someone else.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Yes, unlink", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.value = True
        await self._finish(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.value = False
        await self._finish(interaction)

    async def _finish(self, interaction: discord.Interaction) -> None:
        self._disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self) -> None:
        self._disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    def _disable_all(self) -> None:
        for child in self.children:
            if isinstance(child, (discord.ui.Button, discord.ui.Select)):
                child.disabled = True
