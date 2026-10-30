"""
cogs/reaction_roles.py — Reaction role panels.

Slash commands:
    /reactionrole setup  [channel] [title] [exclusive] [template]
    /reactionrole add    [panel_id] [emoji] [role] [label]
    /reactionrole remove [panel_id] [emoji]
    /reactionrole post   [panel_id]
    /reactionrole list
    /reactionrole delete [panel_id]

On post, the bot sends a message with buttons for each entry.
Clicking a button toggles the role. If exclusive=True, any other
roles from this panel are removed first.

Panel/entry mutations are delegated to services.reaction_role_service.
ReactionRoleService, which both this cog AND the dashboard's internal API
route (internal_api/routes/reaction_roles.py) use — see that service
module's docstring for why (keeping the live Discord panel message in sync
with the DB regardless of whether the edit came from a slash command or
the dashboard). _build_panel_view/_build_panel_embed below are thin
re-exports of the service's build_panel_view/build_panel_embed, kept under
their original names so the persistent-view restoration code at the
bottom of this file (and any external references) doesn't need to change.

Self-role safety (services/self_role_policy.py): /reactionrole add refuses
roles that are @everyone/managed, at or above the bot's or the configuring
member's top role, carry moderator permissions, are in STAFF_ROLE_IDS or hold
any bot permission grant. Every button click re-checks: the button must still
be an entry of that panel AND on the panel's current message, and (when
granting) the role must still pass the same rules without the actor part;
otherwise the click is refused with an ephemeral message and a warning log.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from database.repositories.reaction_role_repo import ReactionRoleRepository
from services.permission_service import PermissionService
from services.reaction_role_service import ReactionRoleService
from services.reaction_role_service import build_panel_embed as _build_panel_embed
from services.reaction_role_service import build_panel_view as _build_panel_view

log = logging.getLogger(__name__)


class ReactionRoleCog(commands.Cog, name="ReactionRoles"):
    def __init__(self, bot: commands.Bot, repo: ReactionRoleRepository, perm: PermissionService):
        self.bot = bot
        self._repo = repo
        self.permission_service = perm
        # Exposed for internal_api/deps.py (get_reaction_role_service), mirroring
        # how cogs/tickets.py exposes self.ticket_service for the same reason.
        self.reaction_role_service = ReactionRoleService(repo, bot, perm)

    # ------------------------------------------------------------------
    # Button interaction handler (persistent)
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")
        if not custom_id.startswith("rr:"):
            return

        parts = custom_id.split(":")
        if len(parts) != 3:
            return

        try:
            panel_id = int(parts[1])
            role_id = int(parts[2])
        except ValueError:
            return

        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return

        # Click-time re-check: the button must still be configured in the DB
        # for THIS panel message (removed buttons on a stale message, or an
        # old copy of the panel, must not keep granting), and the role must
        # still be safe to hand out (services/self_role_policy.py).
        panel = await self._repo.get_panel(panel_id)
        if not panel or panel.get("guild_id") != guild.id:
            await interaction.response.send_message("This panel no longer exists.", ephemeral=True)
            return
        message_id = getattr(getattr(interaction, "message", None), "id", None)
        entries = await self._repo.get_entries(panel_id)
        configured = any(int(e["role_id"]) == role_id for e in entries)
        if not configured or (panel.get("message_id") and message_id and message_id != panel["message_id"]):
            log.warning(
                "reaction-roles: refused stale button panel=%s role=%s message=%s user=%s",
                panel_id, role_id, message_id, member.id,
            )
            await interaction.response.send_message(
                "This button is no longer active. Use the current role panel.", ephemeral=True
            )
            return

        role = guild.get_role(role_id)
        if not role:
            await interaction.response.send_message("That role no longer exists.", ephemeral=True)
            return

        has_role = role in member.roles
        if not has_role:
            reason = await self.reaction_role_service.click_block_reason(guild, role)
            if reason:
                log.warning(
                    "reaction-roles: refused to grant role %s (%s) via panel %s to %s: %s",
                    role.id, role.name, panel_id, member.id, reason,
                )
                await interaction.response.send_message(
                    "This role can't be self-assigned right now. Ask a staff member.", ephemeral=True
                )
                return

        await interaction.response.defer(ephemeral=True)

        if panel.get("exclusive") and not has_role:
            # Remove any other roles from this panel first
            panel_role_ids = {e["role_id"] for e in entries}
            roles_to_remove = [r for r in member.roles if r.id in panel_role_ids and r.id != role_id]
            if roles_to_remove:
                try:
                    await member.remove_roles(*roles_to_remove, reason="Reaction role panel (exclusive)")
                except discord.HTTPException:
                    pass

        try:
            if has_role:
                await member.remove_roles(role, reason="Reaction role panel")
                await interaction.followup.send(f"Removed **{role.name}**.", ephemeral=True)
            else:
                await member.add_roles(role, reason="Reaction role panel")
                await interaction.followup.send(f"Given **{role.name}**.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("I don't have permission to manage that role.", ephemeral=True)
        except discord.HTTPException as exc:
            await interaction.followup.send(f"Error: {exc}", ephemeral=True)

    # ------------------------------------------------------------------
    # /reactionrole group
    # ------------------------------------------------------------------

    rr = app_commands.Group(name="reactionrole", description="Manage reaction role panels.")

    @rr.command(name="setup", description="Create a new reaction role panel.")
    @app_commands.describe(
        channel="Channel to post the panel in.",
        title="Title shown on the panel embed.",
        exclusive="If true, members can only hold one role from this panel at a time.",
    )
    async def rr_setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        title: str = "React for a role!",
        exclusive: bool = False,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_reaction_roles"):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return

        panel_id = await self.reaction_role_service.create_panel(
            guild_id=interaction.guild_id,
            channel_id=channel.id,
            created_by=interaction.user.id,
            title=title,
            exclusive=exclusive,
        )
        await interaction.response.send_message(
            f"Panel `{panel_id}` created in {channel.mention}.\n"
            f"Add roles with `/reactionrole add panel_id:{panel_id} emoji:🎨 role:@SomeRole`\n"
            f"Then post it with `/reactionrole post panel_id:{panel_id}`.",
            ephemeral=True,
        )

    @rr.command(name="add", description="Add a role button to a panel.")
    @app_commands.describe(
        panel_id="Panel ID from /reactionrole setup.",
        emoji="Emoji for the button (e.g. 🎨 or a custom emoji).",
        role="Role to assign when clicked.",
        label="Optional text label on the button.",
    )
    async def rr_add(
        self,
        interaction: discord.Interaction,
        panel_id: int,
        emoji: str,
        role: discord.Role,
        label: str = None,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_reaction_roles"):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return

        panel = await self._repo.get_panel(panel_id)
        if not panel or panel["guild_id"] != interaction.guild_id:
            await interaction.response.send_message(f"Panel `{panel_id}` not found.", ephemeral=True)
            return

        entries = await self._repo.get_entries(panel_id)
        if len(entries) >= 25:
            await interaction.response.send_message("Panels can have at most 25 roles.", ephemeral=True)
            return

        _entry_id, errors = await self.reaction_role_service.add_entry(
            panel_id, emoji.strip(), role.id, label, guild=interaction.guild, actor=interaction.user,
        )
        if errors:
            await interaction.response.send_message(errors[0], ephemeral=True)
            return
        await interaction.response.send_message(
            f"Added {emoji} → {role.mention} to panel `{panel_id}`.\n"
            + ("The live panel message was updated automatically." if panel.get("message_id") else
               f"Post it with `/reactionrole post panel_id:{panel_id}`."),
            ephemeral=True,
        )

    @rr.command(name="remove", description="Remove a role button from a panel.")
    @app_commands.describe(panel_id="Panel ID.", emoji="The emoji to remove.")
    async def rr_remove(
        self,
        interaction: discord.Interaction,
        panel_id: int,
        emoji: str,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_reaction_roles"):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return
        panel = await self._repo.get_panel(panel_id)
        if not panel or panel["guild_id"] != interaction.guild_id:
            await interaction.response.send_message(f"Panel `{panel_id}` not found.", ephemeral=True)
            return

        removed = await self._repo.remove_entry(panel_id, emoji.strip())
        if removed:
            await self.reaction_role_service.sync_panel_message(panel_id, interaction.guild)
            await interaction.response.send_message(
                f"Removed {emoji} from panel `{panel_id}`.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"No entry with emoji {emoji} on panel `{panel_id}`.", ephemeral=True
            )

    @rr.command(name="post", description="Post (or re-post) a reaction role panel.")
    @app_commands.describe(panel_id="Panel ID.")
    async def rr_post(
        self,
        interaction: discord.Interaction,
        panel_id: int,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_reaction_roles"):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return

        panel = await self._repo.get_panel(panel_id)
        if not panel or panel["guild_id"] != interaction.guild_id:
            await interaction.response.send_message(f"Panel `{panel_id}` not found.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        _msg_id, errors = await self.reaction_role_service.post_panel(panel_id, interaction.guild)
        if errors:
            await interaction.followup.send(errors[0], ephemeral=True)
            return

        channel = interaction.guild.get_channel(panel["channel_id"])
        await interaction.followup.send(
            f"Panel `{panel_id}` posted in {channel.mention if channel else '#unknown'}.", ephemeral=True
        )

    @rr.command(name="list", description="List all reaction role panels in this server.")
    async def rr_list(self, interaction: discord.Interaction) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_reaction_roles"):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return

        panels = await self._repo.list_panels(interaction.guild_id)
        if not panels:
            await interaction.response.send_message("No reaction role panels configured.", ephemeral=True)
            return

        embed = discord.Embed(
            title="Reaction Role Panels",
            color=discord.Color.blurple(),
            description=f"{len(panels)} panel(s)",
        )
        for p in panels[:25]:
            entries = await self._repo.get_entries(p["id"])
            posted = "yes" if p.get("message_id") else "no"
            exclusive = "exclusive" if p.get("exclusive") else "multi"
            embed.add_field(
                name=f"`{p['id']}` — {p['title']} [{exclusive}]",
                value=(
                    f"Channel: <#{p['channel_id']}> | Posted: {posted}\n"
                    + ", ".join(f"{e['emoji']}" for e in entries) or "*no entries*"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @rr.command(name="delete", description="Delete a reaction role panel and its message.")
    @app_commands.describe(panel_id="Panel ID.")
    async def rr_delete(
        self,
        interaction: discord.Interaction,
        panel_id: int,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_reaction_roles"):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return

        panel = await self._repo.get_panel(panel_id)
        if not panel or panel["guild_id"] != interaction.guild_id:
            await interaction.response.send_message(f"Panel `{panel_id}` not found.", ephemeral=True)
            return

        await self.reaction_role_service.delete_panel(panel_id, guild=interaction.guild)
        await interaction.response.send_message(f"Panel `{panel_id}` deleted.", ephemeral=True)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "db") or bot.db is None:
        return

    repo = ReactionRoleRepository(bot.db)
    perm = bot.permission_service  # type: ignore[attr-defined]

    cog = ReactionRoleCog(bot, repo, perm)
    await bot.add_cog(cog)

    # Restore persistent panel views on restart
    try:
        panels = await repo.get_all_panels()
        for panel in panels:
            entries = await repo.get_entries(panel["id"])
            view = _build_panel_view(panel, entries)
            if view.children:
                bot.add_view(view, message_id=panel["message_id"])
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Failed to restore reaction role views")
