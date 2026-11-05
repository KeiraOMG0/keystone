"""
core/ui/confirm.py — Simple confirm/cancel view.
"""
from __future__ import annotations

import discord


class ConfirmView(discord.ui.View):
    """
    A two-button view that resolves to either confirmed or cancelled.

    Usage::

        view = ConfirmView()
        await interaction.response.send_message("Are you sure?", view=view, ephemeral=True)
        await view.wait()
        if view.confirmed:
            ...

    Pass ``allowed_user_id`` when the prompt is posted in a public channel
    (e.g. from a prefix command) so only that user's clicks count; anyone
    else gets an ephemeral refusal.
    """

    def __init__(self, timeout: float = 30, allowed_user_id: int | None = None):
        super().__init__(timeout=timeout)
        self.confirmed: bool = False
        self.allowed_user_id = allowed_user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.allowed_user_id is None or interaction.user.id == self.allowed_user_id:
            return True
        await interaction.response.send_message("Only the person who ran the command can answer this.", ephemeral=True)
        return False

    def _disable_all(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.confirmed = True
        self._disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.confirmed = False
        self._disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()
