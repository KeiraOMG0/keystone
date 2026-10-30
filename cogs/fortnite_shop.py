"""
cogs/fortnite_shop.py — Fortnite item shop.

Prefix command:
    !shop                      today's shop, one item per page with buttons and a section jump menu

Slash commands (group /fortnite):
    /fortnite channel set <channel>   config, gated on the `manage_schedules` grant (staff bypass)
    /fortnite channel clear
    /fortnite enable | disable
    /fortnite jamtracks <on|off>      list Jam Tracks individually in the daily post (default: one summary line)
    /fortnite pingrole [role]         role pinged by the daily post (omit to clear)
    /fortnite postnow                 post today's shop to the configured channel now (does NOT mark it posted)
    /fortnite status
    /fortnite wishlist add|remove|list   any member: get pinged in the daily post when a matching item is in the shop

Daily auto-post: a 60s tasks.loop calls FortniteShopService.run_daily(), which
posts once per shop date shortly after the 00:00 UTC rotation
(FORTNITE_SHOP_POST_DELAY_MINUTES). Dedupe is stored in the DB
(fortnite_shop_config.last_posted_date), so restarts never double-post. After a
fetch failure or a not-yet-rotated API response the loop waits RETRY_MINUTES
before fetching again (the gate lives on the service as retry_at so status()
can report it). A post that fails partway is NOT retried (see run_daily). All logic lives in services/fortnite_shop_service.py;
the internal API reaches it via bot.get_cog("FortniteShop").shop_service.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from database.repositories.fortnite_repo import FortniteRepository, MAX_WISHLIST_PER_USER
from services.fortnite_shop_service import (
    FortniteShopError,
    FortniteShopService,
    MANUAL_FETCH_ATTEMPTS,
    Shop,
    build_item_page,
    ordered_entries,
    section_starts,
)

log = logging.getLogger(__name__)

CONFIG_PERMISSION = "manage_schedules"
RETRY_MINUTES = 5
_NO_PERMISSION = f"You need the `{CONFIG_PERMISSION}` permission to configure the Fortnite shop."


class ShopPaginator(discord.ui.View):
    """One shop entry per page. Only the invoking user can navigate."""

    def __init__(self, shop: Shop, author_id: int) -> None:
        super().__init__(timeout=300)
        self.shop = shop
        self.entries = ordered_entries(shop)
        self.author_id = author_id
        self.index = 0
        self.message: discord.Message | None = None
        starts = section_starts(shop)[:25]
        if starts:
            select = discord.ui.Select(
                placeholder="Jump to a section",
                options=[
                    discord.SelectOption(label=name[:100], value=str(first), description=f"{count} items")
                    for name, first, count in starts
                ],
                row=1,
            )
            select.callback = self._on_select  # type: ignore[assignment]
            self.add_item(select)
        self._sync_buttons()

    def page(self) -> discord.Embed:
        return build_item_page(self.shop, self.entries[self.index], self.index + 1, len(self.entries))

    def _sync_buttons(self) -> None:
        last = len(self.entries) - 1
        self.first_btn.disabled = self.prev_btn.disabled = self.index <= 0
        self.next_btn.disabled = self.last_btn.disabled = self.index >= last

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Run `!shop` yourself to browse.", ephemeral=True)
            return False
        return True

    async def _go(self, interaction: discord.Interaction, index: int) -> None:
        self.index = max(0, min(index, len(self.entries) - 1))
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.page(), view=self)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", []) if interaction.data else []
        await self._go(interaction, int(values[0]) if values else 0)

    @discord.ui.button(label="<<", style=discord.ButtonStyle.secondary, row=0)
    async def first_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._go(interaction, 0)

    @discord.ui.button(label="<", style=discord.ButtonStyle.primary, row=0)
    async def prev_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._go(interaction, self.index - 1)

    @discord.ui.button(label=">", style=discord.ButtonStyle.primary, row=0)
    async def next_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._go(interaction, self.index + 1)

    @discord.ui.button(label=">>", style=discord.ButtonStyle.secondary, row=0)
    async def last_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._go(interaction, len(self.entries) - 1)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class FortniteShopCog(commands.Cog, name="FortniteShop"):
    def __init__(self, bot: commands.Bot, shop_service: FortniteShopService) -> None:
        self.bot = bot
        self.shop_service = shop_service
        self._daily_loop.start()

    async def cog_unload(self) -> None:
        self._daily_loop.cancel()

    fortnite = app_commands.Group(name="fortnite", description="Fortnite item shop.", guild_only=True)
    channel_group = app_commands.Group(name="channel", description="Daily shop channel.", parent=fortnite)
    wishlist_group = app_commands.Group(
        name="wishlist", description="Get pinged when an item is in the shop.", parent=fortnite
    )

    # ------------------------------------------------------------------
    # Daily loop
    # ------------------------------------------------------------------
    @tasks.loop(seconds=60)
    async def _daily_loop(self) -> None:
        now = datetime.now(timezone.utc)
        for guild in self.bot.guilds:
            gate = self.shop_service.retry_at.get(guild.id)
            if gate is not None and now < gate:
                continue
            try:
                result = await self.shop_service.run_daily(guild, now=now)
            except Exception:
                log.exception("fortnite: daily loop error for guild %s", guild.id)
                result = "error"
            if result in ("fetch_failed", "stale_shop", "post_failed", "no_channel", "error"):
                self.shop_service.retry_at[guild.id] = now + timedelta(minutes=RETRY_MINUTES)
            else:
                self.shop_service.retry_at.pop(guild.id, None)

    @_daily_loop.before_loop
    async def _before_daily(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _allowed(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        return await self.bot.permission_service.has_permission(interaction.user, CONFIG_PERMISSION)  # type: ignore[attr-defined]

    async def _deny(self, interaction: discord.Interaction) -> bool:
        if await self._allowed(interaction):
            return False
        await interaction.response.send_message(_NO_PERMISSION, ephemeral=True)
        return True

    # ------------------------------------------------------------------
    # !shop
    # ------------------------------------------------------------------
    @commands.command(name="shop", help="Browse today's Fortnite item shop.")
    @commands.guild_only()
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def shop_cmd(self, ctx: commands.Context) -> None:
        async with ctx.typing():
            try:
                shop = await self.shop_service.get_shop()
            except FortniteShopError as exc:
                log.warning("fortnite: !shop fetch failed: %s", exc)
                await ctx.reply("The Fortnite shop API is unavailable right now. Try again in a few minutes.",
                                mention_author=False)
                return
        if not shop.entries:
            await ctx.reply("The shop is empty right now.", mention_author=False)
            return
        view = ShopPaginator(shop, ctx.author.id)
        view.message = await ctx.reply(embed=view.page(), view=view, mention_author=False)

    @shop_cmd.error
    async def _shop_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.reply(f"Slow down, try again in {error.retry_after:.0f}s.", mention_author=False)

    # ------------------------------------------------------------------
    # /fortnite config
    # ------------------------------------------------------------------
    @channel_group.command(name="set", description="Set the channel the daily shop is posted in")
    @app_commands.describe(channel="Text channel for the daily shop post")
    async def channel_set(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if await self._deny(interaction):
            return
        await self.shop_service.repo.update_config(interaction.guild_id, channel_id=channel.id)
        await interaction.response.send_message(
            f"The daily Fortnite shop will post in {channel.mention}. Use `/fortnite enable` to turn it on.",
            ephemeral=True,
        )

    @channel_group.command(name="clear", description="Clear the daily shop channel (stops the daily post)")
    async def channel_clear(self, interaction: discord.Interaction) -> None:
        if await self._deny(interaction):
            return
        await self.shop_service.repo.update_config(interaction.guild_id, channel_id=None)
        await interaction.response.send_message("Daily shop channel cleared.", ephemeral=True)

    @fortnite.command(name="enable", description="Turn the daily Fortnite shop post on")
    async def enable(self, interaction: discord.Interaction) -> None:
        if await self._deny(interaction):
            return
        cfg = await self.shop_service.repo.update_config(interaction.guild_id, enabled=True)
        note = "" if cfg.get("channel_id") else " No channel is set yet: use `/fortnite channel set`."
        await interaction.response.send_message(
            "Daily shop post enabled. If today's shop has not been posted yet it goes out within a minute." + note,
            ephemeral=True,
        )

    @fortnite.command(name="disable", description="Turn the daily Fortnite shop post off")
    async def disable(self, interaction: discord.Interaction) -> None:
        if await self._deny(interaction):
            return
        await self.shop_service.repo.update_config(interaction.guild_id, enabled=False)
        await interaction.response.send_message("Daily shop post disabled.", ephemeral=True)

    @fortnite.command(name="jamtracks", description="List Jam Tracks one by one in the daily post")
    @app_commands.describe(enabled="On: list every Jam Track. Off (default): one summary line.")
    async def jamtracks(self, interaction: discord.Interaction, enabled: bool) -> None:
        if await self._deny(interaction):
            return
        await self.shop_service.repo.update_config(interaction.guild_id, include_jam_tracks=enabled)
        await interaction.response.send_message(
            f"Jam Tracks will be {'listed individually' if enabled else 'summarised'} in the daily post.",
            ephemeral=True,
        )

    @fortnite.command(name="pingrole", description="Role to ping with the daily shop post (omit to clear)")
    async def pingrole(self, interaction: discord.Interaction, role: discord.Role | None = None) -> None:
        if await self._deny(interaction):
            return
        if role is not None and (role.is_default() or role.managed):
            # @everyone would render as a broken <@&guild_id> mention; managed
            # (bot/integration) roles are not meaningful ping targets.
            await interaction.response.send_message(
                "Pick a normal role (not @everyone or a bot/integration role).", ephemeral=True
            )
            return
        await self.shop_service.repo.update_config(interaction.guild_id, ping_role_id=role.id if role else None)
        await interaction.response.send_message(
            f"The daily post will ping {role.mention}." if role else "The daily post will not ping a role.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @fortnite.command(name="postnow", description="Post today's shop to the configured channel now")
    async def postnow(self, interaction: discord.Interaction) -> None:
        if await self._deny(interaction):
            return
        cfg = await self.shop_service.repo.get_config(interaction.guild_id)
        channel = interaction.guild.get_channel(cfg["channel_id"]) if cfg and cfg.get("channel_id") else None
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Set a channel first with `/fortnite channel set`.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            shop = await self.shop_service.get_shop(attempts=MANUAL_FETCH_ATTEMPTS)
            msgs = await self.shop_service.post_shop(
                channel, shop, include_jam_tracks=bool(cfg.get("include_jam_tracks"))
            )
        except FortniteShopError as exc:
            await interaction.followup.send(f"Could not fetch the shop: {exc}", ephemeral=True)
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(f"Could not post in {channel.mention}: {exc}", ephemeral=True)
            return
        link = msgs[0].jump_url if msgs else channel.mention
        await interaction.followup.send(
            f"Posted the {shop.shop_date} shop: {link}\n(The daily auto-post still runs as normal.)", ephemeral=True
        )

    @fortnite.command(name="status", description="Show the Fortnite shop configuration")
    async def status(self, interaction: discord.Interaction) -> None:
        st = await self.shop_service.status(interaction.guild_id)
        cfg = st["config"]
        emb = discord.Embed(title="Fortnite shop", colour=discord.Colour.blurple())
        emb.add_field(name="Daily post", value="enabled" if cfg.get("enabled") else "disabled", inline=True)
        emb.add_field(name="Channel", value=f"<#{cfg['channel_id']}>" if cfg.get("channel_id") else "not set", inline=True)
        emb.add_field(name="Ping role", value=f"<@&{cfg['ping_role_id']}>" if cfg.get("ping_role_id") else "none", inline=True)
        emb.add_field(name="Jam Tracks", value="listed" if cfg.get("include_jam_tracks") else "summarised", inline=True)
        emb.add_field(name="Last posted", value=cfg.get("last_posted_date") or "never", inline=True)
        if st.get("next_post_at"):
            ts = int(datetime.fromisoformat(st["next_post_at"]).timestamp())
            if st.get("post_pending") and ts <= int(datetime.now(timezone.utc).timestamp()) + 5:
                value = "today's post is due, within a minute"
            elif st.get("post_pending"):
                value = f"retrying <t:{ts}:R>"
            else:
                value = f"<t:{ts}:R>"
            emb.add_field(name="Next post", value=value, inline=True)
        if cfg.get("last_error"):
            emb.add_field(name="Last error", value=str(cfg["last_error"])[:1024], inline=False)
        wl = st["wishlist"]
        emb.set_footer(text=f"Wishlists: {wl['entries']} items from {wl['users']} members · data: fortnite-api.com")
        await interaction.response.send_message(embed=emb, ephemeral=True)

    # ------------------------------------------------------------------
    # /fortnite wishlist (any member)
    # ------------------------------------------------------------------
    @wishlist_group.command(name="add", description="Get pinged in the daily shop post when this item is in the shop")
    @app_commands.describe(item="Item name or part of it, e.g. 'renegade raider'")
    async def wish_add(self, interaction: discord.Interaction, item: app_commands.Range[str, 2, 60]) -> None:
        try:
            added = await self.shop_service.repo.add_wish(interaction.guild_id, interaction.user.id, item)
        except ValueError as exc:
            msg = (f"Your wishlist is full ({MAX_WISHLIST_PER_USER} items)." if str(exc) == "limit"
                   else "Give an item name.")
            await interaction.response.send_message(msg, ephemeral=True)
            return
        await interaction.response.send_message(
            f"Added **{item}** to your wishlist. You'll be pinged in the daily shop post when it shows up."
            if added else f"**{item}** is already on your wishlist.",
            ephemeral=True,
        )

    @wishlist_group.command(name="remove", description="Remove an item from your wishlist")
    async def wish_remove(self, interaction: discord.Interaction, item: str) -> None:
        removed = await self.shop_service.repo.remove_wish(interaction.guild_id, interaction.user.id, item)
        await interaction.response.send_message(
            f"Removed **{item}**." if removed else f"**{item}** is not on your wishlist.", ephemeral=True
        )

    @wish_remove.autocomplete("item")
    async def _wish_remove_ac(self, interaction: discord.Interaction, current: str):
        rows = await self.shop_service.repo.list_wishes(interaction.guild_id, interaction.user.id)
        cur = current.lower()
        return [app_commands.Choice(name=r["query"][:100], value=r["query"]) for r in rows if cur in r["query"]][:25]

    @wishlist_group.command(name="list", description="Show your wishlist")
    async def wish_list(self, interaction: discord.Interaction) -> None:
        rows = await self.shop_service.repo.list_wishes(interaction.guild_id, interaction.user.id)
        if not rows:
            await interaction.response.send_message(
                "Your wishlist is empty. Add something with `/fortnite wishlist add`.", ephemeral=True
            )
            return
        lines = "\n".join(f"- {r['query']}" for r in rows)
        await interaction.response.send_message(f"Your wishlist ({len(rows)}/{MAX_WISHLIST_PER_USER}):\n{lines}",
                                                ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "db") or bot.db is None:
        log.warning("bot.db not available - fortnite shop cog not loaded.")
        return
    service = FortniteShopService(
        FortniteRepository(bot.db),
        api_key=config.FORTNITE_API_KEY,
        language=config.FORTNITE_SHOP_LANGUAGE,
        post_delay_minutes=config.FORTNITE_SHOP_POST_DELAY_MINUTES,
    )
    await bot.add_cog(FortniteShopCog(bot, service))
