"""
core/ui/paginator.py — Reusable paginator View for discord.py.
"""
from __future__ import annotations

import discord
from typing import Callable


class Paginator(discord.ui.View):
    """
    A paginator view that splits a list of items into pages and renders them
    via a caller-supplied formatter function.

    Parameters
    ----------
    items:
        The full list of items to paginate.
    page_size:
        How many items appear on each page (default 10).
    formatter:
        Callable ``(slice: list, page: int, max_page: int) -> discord.Embed``
        that converts one page's worth of items into an embed.
    timeout:
        Seconds before the view expires (default 120).
    """

    def __init__(
        self,
        items: list,
        page_size: int = 10,
        formatter: Callable[[list, int, int], discord.Embed] = None,
        timeout: float = 120,
    ):
        super().__init__(timeout=timeout)
        self.items = items
        self.page_size = page_size
        self.formatter = formatter
        self._page = 0
        self._max_page = max(0, (len(items) - 1) // page_size) if items else 0

        self._update_buttons()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _update_buttons(self) -> None:
        self.prev_button.disabled = self._page == 0
        self.next_button.disabled = self._page >= self._max_page

    def _current_slice(self) -> list:
        start = self._page * self.page_size
        return self.items[start : start + self.page_size]

    def render_embed(self) -> discord.Embed:
        """Return the embed for the current page via the formatter."""
        if self.formatter is None:
            raise ValueError("Paginator.formatter must be set before rendering.")
        return self.formatter(self._current_slice(), self._page, self._max_page)

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self._page > 0:
            self._page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self._page < self._max_page:
            self._page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.render_embed(), view=self)
