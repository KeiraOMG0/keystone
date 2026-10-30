"""
services/reaction_role_service.py — Discord-aware service layer over
database/repositories/reaction_role_repo.py.

Added alongside the dashboard self-roles page (server/frontend's
SelfRolesPage.tsx) so that BOTH the Discord /reactionrole commands
(cogs/reaction_roles.py) and the dashboard-driven internal API
(internal_api/routes/reaction_roles.py) mutate panels through the exact
same code path — mirroring how services/ticket_service.py is the single
place TicketService-backed mutations happen for both cogs/tickets.py and
internal_api/routes/tickets.py.

Why this exists (the live-message-desync problem):
  cogs/reaction_roles.py's original /reactionrole post command works by
  DELETING the old panel message and sending a brand new one. That's fine
  for a one-off Discord slash command, but if the dashboard used the same
  delete+resend approach for every small edit (e.g. changing a button
  label), the message would visibly flicker/reappear in the channel and
  its message_id/link would keep changing under users who have it
  bookmarked or pinned.

  Instead, `sync_panel_message()` EDITS the existing message in place
  (embed + view) whenever a panel already has a message_id. This is what
  every dashboard mutation (add/remove/reorder entry, rename, toggle
  exclusive) calls afterward, so the live Discord message reflects the DB
  immediately — no bot restart, no re-post, no new message_id. The
  persistent view registered at startup (bot.add_view in
  cogs/reaction_roles.py's setup()) has custom_ids of the form
  "rr:{panel_id}:{role_id}" which are recomputed fresh from current
  entries every time this method builds a view, so edited/removed entries
  are reflected in the buttons immediately too — there is no separate
  "stale view" object hanging around.

  Only if the panel has NEVER been posted (message_id is NULL) does
  sync_panel_message do nothing — the dashboard surfaces an explicit
  "Post to Discord" action for that first post (post_panel), same
  distinction /reactionrole setup vs. /reactionrole post makes today.
"""

from __future__ import annotations

import logging

import discord

from database.repositories.reaction_role_repo import ReactionRoleRepository
from services import self_role_policy

log = logging.getLogger(__name__)


def build_panel_view(panel: dict, entries: list[dict]) -> discord.ui.View:
    """Shared with cogs/reaction_roles.py's _build_panel_view — kept here so
    both the cog and this service build buttons identically. cogs/reaction_roles.py
    re-exports this name for backward compatibility with its own module-level
    _build_panel_view helper (see bottom of that file)."""
    view = discord.ui.View(timeout=None)
    for entry in entries[:25]:
        emoji_str = entry["emoji"]
        try:
            emoji = discord.PartialEmoji.from_str(emoji_str)
        except Exception:
            emoji = None

        btn = discord.ui.Button(
            label=entry.get("label") or entry["emoji"],
            emoji=emoji if emoji and emoji.id else None,
            style=discord.ButtonStyle.secondary,
            custom_id=f"rr:{panel['id']}:{entry['role_id']}",
        )
        view.add_item(btn)
    return view


def build_panel_embed(panel: dict, entries: list[dict]) -> discord.Embed:
    """Shared with cogs/reaction_roles.py's _build_panel_embed — see build_panel_view docstring."""
    embed = discord.Embed(
        title=panel["title"],
        color=discord.Color.blurple(),
        description="\n".join(
            f"{e['emoji']} — <@&{e['role_id']}>" + (f" {e['label']}" if e.get("label") else "")
            for e in entries
        ) or "*No roles added yet.*",
    )
    if panel.get("exclusive"):
        embed.set_footer(text="You can only hold one role from this panel.")
    return embed


class ReactionRoleService:
    def __init__(self, repo: ReactionRoleRepository, bot: discord.Client, permission_service=None):
        self._repo = repo
        self._bot = bot
        # For the self-role safety checks (services/self_role_policy.py).
        self._perms = permission_service if permission_service is not None else getattr(bot, "permission_service", None)

    async def entry_block_reason(self, guild: discord.Guild, role, actor) -> str | None:
        """Why `actor` may not put `role` on a panel (None = allowed). Same
        rules for /reactionrole add and the dashboard; see self_role_policy."""
        return await self_role_policy.configure_block_reason(role, guild, actor, self._perms)

    async def candidate_block_reasons(self, guild: discord.Guild, roles, actor) -> dict[int, str | None]:
        """role id -> reason (None = allowed) for the dashboard role picker.
        Raises if the permission grants can't be read."""
        privileged = await self_role_policy.privileged_role_ids(self._perms, guild.id)
        return self_role_policy.block_reasons(roles, guild, actor, privileged)

    async def click_block_reason(self, guild: discord.Guild, role) -> str | None:
        """Click-time re-check (no actor): may the bot still hand `role` out?"""
        return await self_role_policy.grant_block_reason(role, guild, self._perms)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def list_panels(self, guild_id: int) -> list[dict]:
        return await self._repo.list_panels(guild_id)

    async def get_panel(self, panel_id: int) -> dict | None:
        return await self._repo.get_panel(panel_id)

    async def get_entries(self, panel_id: int) -> list[dict]:
        return await self._repo.get_entries(panel_id)

    async def get_panel_with_entries(self, panel_id: int) -> tuple[dict, list[dict]] | None:
        panel = await self._repo.get_panel(panel_id)
        if not panel:
            return None
        entries = await self._repo.get_entries(panel_id)
        return panel, entries

    # ------------------------------------------------------------------
    # Writes — each mutation re-syncs the live Discord message afterward
    # (if one exists) so the dashboard can never leave Discord stale.
    # ------------------------------------------------------------------

    async def create_panel(
        self,
        guild_id: int,
        channel_id: int,
        created_by: int,
        title: str = "React for a role!",
        exclusive: bool = False,
    ) -> int:
        return await self._repo.create_panel(
            guild_id=guild_id,
            channel_id=channel_id,
            created_by=created_by,
            title=title,
            exclusive=exclusive,
        )

    async def update_panel(
        self,
        panel_id: int,
        guild: discord.Guild | None = None,
        title: str | None = None,
        exclusive: bool | None = None,
    ) -> list[str]:
        panel = await self._repo.get_panel(panel_id)
        if not panel:
            return ["Panel not found."]
        await self._repo.update_panel(panel_id, title=title, exclusive=exclusive)
        if guild is not None:
            await self.sync_panel_message(panel_id, guild)
        return []

    async def add_entry(
        self,
        panel_id: int,
        emoji: str,
        role_id: int,
        label: str | None,
        guild: discord.Guild | None = None,
        actor=None,
    ) -> tuple[int | None, list[str]]:
        """`guild` + `actor` (the configuring member) are required: every
        entry is checked with entry_block_reason before it is stored, so a
        moderator can't turn a staff / granted / dangerous role into a
        self-role (privilege escalation)."""
        panel = await self._repo.get_panel(panel_id)
        if not panel:
            return None, ["Panel not found."]
        if guild is None:
            return None, ["A guild is required to add a role."]
        reason = await self.entry_block_reason(guild, guild.get_role(role_id), actor)
        if reason:
            return None, [reason]
        entries = await self._repo.get_entries(panel_id)
        if len(entries) >= 25 and not any(e["emoji"] == emoji for e in entries):
            return None, ["Panels can have at most 25 roles."]
        entry_id = await self._repo.add_entry(panel_id, emoji.strip(), role_id, label, position=len(entries))
        if guild is not None:
            await self.sync_panel_message(panel_id, guild)
        return entry_id, []

    async def remove_entry(
        self,
        panel_id: int,
        entry_id: int,
        guild: discord.Guild | None = None,
    ) -> list[str]:
        removed = await self._repo.remove_entry_by_id(panel_id, entry_id)
        if not removed:
            return ["Entry not found."]
        if guild is not None:
            await self.sync_panel_message(panel_id, guild)
        return []

    async def delete_panel(self, panel_id: int, guild: discord.Guild | None = None) -> list[str]:
        panel = await self._repo.get_panel(panel_id)
        if not panel:
            return ["Panel not found."]
        if guild is not None and panel.get("message_id"):
            channel = guild.get_channel(panel["channel_id"])
            if channel:
                try:
                    msg = await channel.fetch_message(panel["message_id"])
                    await msg.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass
        await self._repo.delete_panel(panel_id)
        return []

    # ------------------------------------------------------------------
    # Discord message (post / in-place sync)
    # ------------------------------------------------------------------

    async def post_panel(self, panel_id: int, guild: discord.Guild) -> tuple[int | None, list[str]]:
        """First-time post (or explicit re-post): deletes any old message and
        sends a fresh one. Matches cogs/reaction_roles.py's rr_post command
        exactly — kept as its own method (rather than folded into
        sync_panel_message) because the DASHBOARD deliberately exposes this
        as a separate "Post to Discord" action from ordinary edits, so staff
        aren't surprised by a channel message reappearing/moving position
        just because they, say, fixed a typo (see sync_panel_message)."""
        panel = await self._repo.get_panel(panel_id)
        if not panel:
            return None, ["Panel not found."]

        entries = await self._repo.get_entries(panel_id)
        if not entries:
            return None, ["Add at least one role before posting."]

        channel = guild.get_channel(panel["channel_id"])
        if not channel:
            return None, ["Panel channel not found."]

        embed = build_panel_embed(panel, entries)
        view = build_panel_view(panel, entries)

        if panel.get("message_id"):
            try:
                old_msg = await channel.fetch_message(panel["message_id"])
                await old_msg.delete()
            except (discord.NotFound, discord.HTTPException):
                pass

        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.HTTPException as exc:
            return None, [f"Failed to send panel: {exc}"]

        await self._repo.set_message(panel_id, msg.id)
        if view.children:
            self._bot.add_view(view, message_id=msg.id)
        return msg.id, []

    async def sync_panel_message(self, panel_id: int, guild: discord.Guild) -> list[str]:
        """Edit the existing live panel message in place to match current DB
        state (title/exclusive/entries). No-op (not an error) if the panel
        has never been posted — callers don't need to special-case that.

        This is what keeps dashboard edits from desyncing the live Discord
        message: every mutating dashboard call (update_panel/add_entry/
        remove_entry) calls this immediately afterward. If the edit/HTTP
        call fails (message deleted out from under us, missing permissions,
        etc.) we return a human-readable error string instead of raising —
        the DB write itself already succeeded, so this is reported as a
        soft warning by the API layer, not rolled back.
        """
        panel = await self._repo.get_panel(panel_id)
        if not panel or not panel.get("message_id"):
            return []

        channel = guild.get_channel(panel["channel_id"])
        if not channel:
            return ["Panel channel not found; live message could not be updated."]

        entries = await self._repo.get_entries(panel_id)
        embed = build_panel_embed(panel, entries)
        view = build_panel_view(panel, entries)

        try:
            msg = await channel.fetch_message(panel["message_id"])
            await msg.edit(embed=embed, view=view)
        except discord.NotFound:
            return ["Live panel message no longer exists in Discord (was it deleted manually?). Re-post it from the dashboard."]
        except discord.HTTPException as exc:
            return [f"Failed to update live panel message: {exc}"]

        # Re-register the (rebuilt) persistent view so button custom_ids for
        # any newly-added/removed entries keep working after this edit,
        # without waiting for a bot restart.
        if view.children:
            self._bot.add_view(view, message_id=panel["message_id"])
        return []
