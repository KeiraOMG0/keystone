"""
services/debug_actions_service.py — test actions for the hidden, owner-only
dashboard Debug page (/dashboard/debug -> server/routes/debug.py ->
internal_api/routes/debug.py -> here).

Every action renders a feature's REAL channel output (same service/builder the
feature itself uses) into a channel the owner picks, with the owner as the
sample member, and is SAFE by construction:

  - Nothing writes feature state: no QOTD history row, no birthday
    mark_celebrated / role grant, no Fortnite dedupe update, no ticket or
    reaction-role panel message_id, no honeypot catch/appeal/token, no
    moderation action, no audit row.
  - All sends go through SafeChannel, which forces AllowedMentions to "only
    the owner" (never @everyone/@here/roles), prefixes every message with
    "[TEST] <action>", and drops any interactive view unless every component
    is disabled (so a preview can never open a ticket or grant a role).

Features that cannot be exercised without real side effects are listed in
SKIPPED (shown on the page) instead of being faked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import discord

log = logging.getLogger(__name__)

TEST_PREFIX = "[TEST]"
MAX_TEXT = 1800

SKIPPED: list[dict[str, str]] = [
    {"feature": "QOTD staff approval message", "reason": "Its Approve/Reject buttons post for real and write QOTD history."},
    {"feature": "Ticket open / close / transcript", "reason": "Creates real ticket channels and DB rows."},
    {"feature": "!verify / Verify & Close", "reason": "Swaps real roles; the verify message template itself is covered by 'Render a template'."},
    {"feature": "Honeypot catch flow", "reason": "Bans/kicks the author and DMs them; only its embeds are previewed here."},
    {"feature": "Kick / ban / timeout / warn", "reason": "Real moderation actions; their log embeds are covered by 'Log channel embed'."},
    {"feature": "Birthday role grant", "reason": "Grants a real role and marks the birthday celebrated; only the announcement is previewed."},
    {"feature": "Scheduled sends / managed messages", "reason": "Register DB rows for live message tracking."},
    {"feature": "Music player", "reason": "Needs a voice connection; use !musicstatus instead."},
]


class DebugActionError(Exception):
    """User-facing failure of a test action (bad params, feature not configured)."""


class SafeChannel:
    """Proxy around a TextChannel that makes every send safe for testing."""

    def __init__(self, channel: discord.TextChannel, owner_id: int, label: str) -> None:
        self._channel = channel
        self._label = label
        self._allowed = discord.AllowedMentions(
            everyone=False, roles=False, users=[discord.Object(owner_id)], replied_user=False
        )
        self.sent: list[discord.Message] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._channel, name)

    @staticmethod
    def view_is_inert(view: discord.ui.View | None) -> bool:
        if view is None:
            return True
        return all(getattr(item, "disabled", False) or isinstance(item, discord.ui.Button) and item.url
                   for item in view.children)

    async def send(self, content: str | None = None, **kwargs: Any) -> discord.Message:
        if not self.view_is_inert(kwargs.get("view")):
            kwargs.pop("view", None)
        kwargs.pop("delete_after", None)
        kwargs["allowed_mentions"] = self._allowed
        prefix = f"{TEST_PREFIX} {self._label}"
        content = f"{prefix}\n{content}" if content else prefix
        if len(content) > 2000:
            content = content[:1997] + "..."
        msg = await self._channel.send(content, **kwargs)
        self.sent.append(msg)
        return msg


def disabled_button_view(labels: list[tuple[str, str | None]]) -> discord.ui.View:
    """A non-persistent view whose buttons are all disabled (preview only)."""
    view = discord.ui.View(timeout=1)
    for i, (label, emoji) in enumerate(labels[:25]):
        try:
            em = discord.PartialEmoji.from_str(emoji) if emoji else None
        except Exception:
            em = None
        view.add_item(
            discord.ui.Button(
                label=(label or "Button")[:80], emoji=em if em and (em.id or em.name) else None,
                style=discord.ButtonStyle.secondary, disabled=True, custom_id=f"debug:noop:{i}",
            )
        )
    return view


@dataclass
class DebugContext:
    bot: Any
    guild: discord.Guild
    channel: SafeChannel
    owner: discord.Member
    params: dict[str, Any]


ParamsBuilder = Callable[[Any, discord.Guild], Awaitable[list[dict]]]
Runner = Callable[[DebugContext], Awaitable[str | None]]


@dataclass
class DebugAction:
    id: str
    label: str
    group: str
    description: str
    run: Runner
    params: ParamsBuilder | None = None
    requires: Callable[[Any], str | None] = field(default=lambda bot: None)  # -> reason unavailable


ACTIONS: dict[str, DebugAction] = {}


def action(id: str, label: str, group: str, description: str, params: ParamsBuilder | None = None,
           requires: Callable[[Any], str | None] | None = None):
    def deco(fn: Runner) -> Runner:
        ACTIONS[id] = DebugAction(id, label, group, description, fn, params, requires or (lambda bot: None))
        return fn
    return deco


def _needs_cog(name: str, attr: str | None = None) -> Callable[[Any], str | None]:
    def check(bot) -> str | None:
        cog = bot.get_cog(name)
        if cog is None or (attr and not hasattr(cog, attr)):
            return f"cog {name} is not loaded"
        return None
    return check


def _text(params: dict, key: str, default: str = "") -> str:
    value = params.get(key)
    return str(value).strip()[:MAX_TEXT] if value not in (None, "") else default


def _int_param(params: dict, key: str) -> int | None:
    value = params.get(key)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise DebugActionError(f"{key} must be a number")


def _bool(params: dict, key: str) -> bool:
    v = params.get(key)
    return v is True or str(v).lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# General
# ---------------------------------------------------------------------------


async def _text_params(_bot, _guild) -> list[dict]:
    return [{"name": "text", "label": "Message", "type": "textarea", "placeholder": "Hello from the debug page"}]


@action("plain_message", "Plain test message", "General", "Send a plain text message.", params=_text_params)
async def _plain_message(ctx: DebugContext) -> str | None:
    await ctx.channel.send(_text(ctx.params, "text", "Hello from the debug page."))
    return None


async def _embed_params(_bot, _guild) -> list[dict]:
    return [
        {"name": "title", "label": "Title", "type": "text", "placeholder": "Test embed"},
        {"name": "description", "label": "Description", "type": "textarea", "placeholder": "Embed body"},
    ]


@action("plain_embed", "Plain test embed", "General", "Send a simple embed.", params=_embed_params)
async def _plain_embed(ctx: DebugContext) -> str | None:
    emb = discord.Embed(
        title=_text(ctx.params, "title", "Test embed")[:256],
        description=_text(ctx.params, "description", "Sent from the owner-only debug page."),
        colour=discord.Colour.blurple(),
    )
    emb.set_author(name=str(ctx.owner), icon_url=ctx.owner.display_avatar.url)
    emb.set_footer(text=f"Bot latency {round((ctx.bot.latency or 0) * 1000)} ms")
    await ctx.channel.send(embed=emb)
    return None


# ---------------------------------------------------------------------------
# Fortnite
# ---------------------------------------------------------------------------


async def _fortnite_params(_bot, _guild) -> list[dict]:
    return [{"name": "include_jam_tracks", "label": "List Jam Tracks individually", "type": "bool"}]


@action("fortnite_shop_post", "Fortnite shop post", "Fortnite",
        "Post today's shop exactly like the daily post (no role ping, no wishlist pings, dedupe state untouched).",
        params=_fortnite_params, requires=_needs_cog("FortniteShop", "shop_service"))
async def _fortnite_post(ctx: DebugContext) -> str | None:
    from services.fortnite_shop_service import MANUAL_FETCH_ATTEMPTS, FortniteShopError

    service = ctx.bot.get_cog("FortniteShop").shop_service
    try:
        shop = await service.get_shop(attempts=MANUAL_FETCH_ATTEMPTS)
    except FortniteShopError as exc:
        raise DebugActionError(f"shop fetch failed: {exc}")
    await service.post_shop(ctx.channel, shop, include_jam_tracks=_bool(ctx.params, "include_jam_tracks"), test=True)
    return f"Shop {shop.shop_date}, {len(shop.entries)} offers."


@action("fortnite_shop_page", "Fortnite !shop page", "Fortnite",
        "Send the first page of the !shop browser (without its buttons).",
        requires=_needs_cog("FortniteShop", "shop_service"))
async def _fortnite_page(ctx: DebugContext) -> str | None:
    from services.fortnite_shop_service import (
        MANUAL_FETCH_ATTEMPTS,
        FortniteShopError,
        build_item_page,
        ordered_entries,
    )

    service = ctx.bot.get_cog("FortniteShop").shop_service
    try:
        shop = await service.get_shop(attempts=MANUAL_FETCH_ATTEMPTS)
    except FortniteShopError as exc:
        raise DebugActionError(f"shop fetch failed: {exc}")
    entries = ordered_entries(shop)
    if not entries:
        raise DebugActionError("the shop is empty")
    await ctx.channel.send(embed=build_item_page(shop, entries[0], 1, len(entries)))
    return None


# ---------------------------------------------------------------------------
# Member events
# ---------------------------------------------------------------------------


@action("welcome_message", "Welcome message", "Members",
        "The welcome message (template 'welcome' or the built-in embed) with you as the new member.",
        requires=_needs_cog("Welcome", "_service"))
async def _welcome(ctx: DebugContext) -> str | None:
    await ctx.bot.get_cog("Welcome")._service.send_welcome(ctx.channel, ctx.owner)
    return None


@action("boost_message", "Boost message", "Members",
        "The boost thank-you (template 'boost' or the built-in embed) with you as the booster.",
        requires=_needs_cog("Boost", "_service"))
async def _boost(ctx: DebugContext) -> str | None:
    await ctx.bot.get_cog("Boost")._service.send_boost(ctx.channel, ctx.owner)
    return None


def _birthday_ready(bot) -> str | None:
    return None if getattr(bot, "birthday_core", None) is not None else "birthday cogs are not loaded"


@action("birthday_announcement", "Birthday announcement", "Members",
        "The configured birthday announcement for you. No role is granted and nothing is marked celebrated.",
        requires=_birthday_ready)
async def _birthday(ctx: DebugContext) -> str | None:
    core = ctx.bot.birthday_core
    settings = await core.repo.get_settings(ctx.guild.id) or {}  # read-only (never get_or_create)
    await core._send_announcement(dict(settings), ctx.channel, [ctx.owner])
    return None if settings else "No birthday settings saved yet; used the default message."


# ---------------------------------------------------------------------------
# Community
# ---------------------------------------------------------------------------


async def _qotd_params(_bot, _guild) -> list[dict]:
    return [{"name": "question", "label": "Question", "type": "text", "placeholder": "Blank = a fallback question"}]


@action("qotd_post", "QOTD post", "Community",
        "The QOTD embed. Uses a fallback question, does not ping, does not write QOTD history.",
        params=_qotd_params, requires=_needs_cog("QOTD", "_service"))
async def _qotd(ctx: DebugContext) -> str | None:
    import random

    from services.qotd_service import FALLBACK_QUESTIONS

    question = _text(ctx.params, "question") or random.choice(FALLBACK_QUESTIONS)
    cfg = await ctx.bot.get_cog("QOTD")._repo.get_config(ctx.guild.id)
    embed = ctx.bot.get_cog("QOTD")._service._build_embed(question, use_emojis=bool(cfg and cfg.get("use_emojis")))
    await ctx.channel.send(embed=embed)
    return None


async def _rr_params(bot, guild) -> list[dict]:
    cog = bot.get_cog("ReactionRoles")
    panels = await cog.reaction_role_service.list_panels(guild.id) if cog else []
    return [{
        "name": "panel_id", "label": "Panel", "type": "select", "required": True,
        "options": [{"value": str(p["id"]), "label": f"#{p['id']} {p['title']}"} for p in panels],
    }]


@action("reaction_role_panel", "Reaction-role panel preview", "Community",
        "A self-role panel's embed with disabled buttons (clicking does nothing).",
        params=_rr_params, requires=_needs_cog("ReactionRoles", "reaction_role_service"))
async def _rr_panel(ctx: DebugContext) -> str | None:
    from services.reaction_role_service import build_panel_embed

    panel_id = _int_param(ctx.params, "panel_id")
    if panel_id is None:
        raise DebugActionError("pick a panel")
    got = await ctx.bot.get_cog("ReactionRoles").reaction_role_service.get_panel_with_entries(panel_id)
    if not got or got[0].get("guild_id") not in (None, ctx.guild.id):
        raise DebugActionError("panel not found")
    panel, entries = got
    view = disabled_button_view([(e.get("label") or e["emoji"], e["emoji"]) for e in entries])
    await ctx.channel.send(embed=build_panel_embed(panel, entries), view=view)
    return None


async def _ticket_params(bot, guild) -> list[dict]:
    cog = bot.get_cog("Tickets")
    panels = await cog.ticket_service.list_panels(guild.id) if cog else []
    return [{
        "name": "panel_id", "label": "Panel", "type": "select", "required": True,
        "options": [
            {"value": str(p["id"]), "label": f"#{p['id']} {p.get('button_label') or 'panel'} ({p.get('purpose') or 'support'})"}
            for p in panels
        ],
    }]


@action("ticket_panel", "Ticket panel preview", "Community",
        "A ticket panel's message with a disabled button (cannot open a ticket; panel message id is not changed).",
        params=_ticket_params, requires=_needs_cog("Tickets", "ticket_service"))
async def _ticket_panel(ctx: DebugContext) -> str | None:
    from core.placeholder_engine import RenderContext

    svc = ctx.bot.get_cog("Tickets").ticket_service
    panel_id = _int_param(ctx.params, "panel_id")
    if panel_id is None:
        raise DebugActionError("pick a panel")
    panel = await svc._tickets.get_panel(panel_id)
    if not panel or panel.get("guild_id") not in (None, ctx.guild.id):
        raise DebugActionError("panel not found")
    content, embeds = None, []
    if panel.get("template_id"):
        full = await svc._templates.get_full(panel["template_id"])
        if full:
            rendered = svc._renderer.render_message(full, RenderContext(guild=ctx.guild, member=ctx.owner, channel=ctx.channel))
            content, embeds = rendered.get("content") or None, rendered.get("embeds") or []
    view = disabled_button_view([(panel.get("button_label") or "Open Ticket", panel.get("button_emoji"))])
    await ctx.channel.send(content, embeds=embeds[:10], view=view)
    return None if panel.get("template_id") else "Panel has no template; only the button is shown."


async def _template_params(bot, guild) -> list[dict]:
    from database.repositories.template_repo import TemplateRepository

    rows = await TemplateRepository(bot.db).list_for_guild(guild.id)
    return [{
        "name": "template", "label": "Template", "type": "select", "required": True,
        "options": [{"value": r["name"], "label": r["name"]} for r in rows],
    }]


@action("template_render", "Render a template", "Community",
        "Render any saved message template with you as the member (covers verify, welcome, ticket and DM templates).",
        params=_template_params, requires=lambda bot: None if getattr(bot, "db", None) else "bot.db unavailable")
async def _template(ctx: DebugContext) -> str | None:
    from core.placeholder_engine import RenderContext
    from core.renderer import TemplateRenderer
    from database.repositories.template_repo import TemplateRepository

    name = _text(ctx.params, "template")
    if not name:
        raise DebugActionError("pick a template")
    repo = TemplateRepository(ctx.bot.db)
    row = await repo.get_by_name(ctx.guild.id, name)
    if not row or row.get("guild_id") != ctx.guild.id:
        raise DebugActionError("template not found")
    full = await repo.get_full(row["id"])
    rendered = TemplateRenderer().render_message(full or {}, RenderContext(guild=ctx.guild, member=ctx.owner, channel=ctx.channel))
    content, embeds = rendered.get("content") or None, rendered.get("embeds") or []
    if not content and not embeds:
        raise DebugActionError("template rendered empty")
    await ctx.channel.send(content, embeds=embeds[:10])
    return None


# ---------------------------------------------------------------------------
# Logging + honeypot
# ---------------------------------------------------------------------------


async def _log_params(_bot, _guild) -> list[dict]:
    from services.log_router import _ACTION_TITLES

    return [{
        "name": "log_action", "label": "Log event", "type": "select", "required": True,
        "options": [{"value": k, "label": v} for k, v in _ACTION_TITLES.items()],
    }]


@action("log_embed", "Log channel embed", "Logging",
        "A moderation/join-leave log embed (log_router styling) with you as the member and the bot as moderator.",
        params=_log_params)
async def _log_embed(ctx: DebugContext) -> str | None:
    from services.log_router import _ACTION_TITLES, build_embed

    kind = _text(ctx.params, "log_action") or "member_joined"
    if kind not in _ACTION_TITLES:
        raise DebugActionError("unknown log event")
    emb = build_embed(kind, member=ctx.owner, actor=ctx.bot.user, reason="Debug page test (no action taken)")
    await ctx.channel.send(embed=emb)
    return None


def _honeypot_ready(bot) -> str | None:
    return _needs_cog("Honeypot", "honeypot_service")(bot)


@action("honeypot_warning", "Honeypot warning embed", "Honeypot",
        "The 'do not type here' warning pinned in the trap channel.", requires=_honeypot_ready)
async def _hp_warning(ctx: DebugContext) -> str | None:
    svc = ctx.bot.get_cog("Honeypot").honeypot_service
    await ctx.channel.send(embed=svc._build_warning_embed())
    return None


@action("honeypot_caught", "Honeypot 'caught so far' embed", "Honeypot",
        "The live counter embed with the real (read-only) totals.", requires=_honeypot_ready)
async def _hp_caught(ctx: DebugContext) -> str | None:
    svc = ctx.bot.get_cog("Honeypot").honeypot_service
    cfg = await svc.get_config(ctx.guild.id)
    count, last = await svc._catch_summary(ctx.guild.id)
    await ctx.channel.send(embed=svc._build_caught_embed(cfg, count, last))
    return None


async def _hp_dm_params(_bot, _guild) -> list[dict]:
    return [{
        "name": "hp_action", "label": "Action", "type": "select",
        "options": [{"value": a, "label": a} for a in ("ban", "softban", "kick")],
    }]


@action("honeypot_dm", "Honeypot DM preview", "Honeypot",
        "The DM a caught account receives, posted in the channel (never DMed). The appeal link is a fake placeholder.",
        params=_hp_dm_params, requires=_honeypot_ready)
async def _hp_dm(ctx: DebugContext) -> str | None:
    import config as _config

    svc = ctx.bot.get_cog("Honeypot").honeypot_service
    cfg = await svc.get_config(ctx.guild.id)
    act = _text(ctx.params, "hp_action") or str(cfg.get("action") or "ban")
    if act not in ("ban", "softban", "kick"):
        raise DebugActionError("unknown action")
    trap = ctx.guild.get_channel(cfg.get("channel_id") or 0) or ctx.channel
    appeal = f"{_config.APPEAL_BASE_URL}/appeal/TEST-NOT-A-REAL-TOKEN" if cfg.get("appeal_enabled", True) else None
    await ctx.channel.send(embed=svc._build_dm_embed(ctx.guild, trap, act, appeal))
    return None


# ---------------------------------------------------------------------------
# Catalogue + runner
# ---------------------------------------------------------------------------


async def catalogue(bot, guild: discord.Guild) -> dict:
    out = []
    for a in ACTIONS.values():
        reason = a.requires(bot)
        params: list[dict] = []
        if a.params is not None and reason is None:
            try:
                params = await a.params(bot, guild)
            except Exception as exc:
                log.exception("debug: params for %s failed", a.id)
                reason = f"could not load options: {type(exc).__name__}"
        out.append({
            "id": a.id, "label": a.label, "group": a.group, "description": a.description,
            "params": params, "available": reason is None, "unavailable_reason": reason,
        })
    return {"actions": out, "skipped": SKIPPED}


async def run_action(bot, guild: discord.Guild, channel: discord.TextChannel, owner: discord.Member,
                     action_id: str, params: dict | None) -> dict:
    """Runs one action. Raises KeyError for an unknown id; returns a result dict
    (ok False + error for feature-level failures)."""
    a = ACTIONS[action_id]
    reason = a.requires(bot)
    if reason:
        return {"ok": False, "action": action_id, "error": reason, "message_ids": [], "jump_url": None, "note": None}
    safe = SafeChannel(channel, owner.id, a.label)
    ctx = DebugContext(bot=bot, guild=guild, channel=safe, owner=owner, params=dict(params or {}))
    error = note = None
    try:
        note = await a.run(ctx)
    except DebugActionError as exc:
        error = str(exc)
    except discord.Forbidden:
        error = f"missing permissions to post in #{channel.name}"
    except discord.HTTPException as exc:
        error = f"Discord error: {exc}"
    except Exception as exc:
        log.exception("debug: action %s failed", action_id)
        error = f"{type(exc).__name__}: {exc}"
    return {
        "ok": error is None and bool(safe.sent),
        "action": action_id,
        "error": error if error or safe.sent else "nothing was sent",
        "note": note,
        "channel_id": channel.id,
        "message_ids": [m.id for m in safe.sent],
        "jump_url": safe.sent[0].jump_url if safe.sent else None,
    }
