"""
services/debug_lab_service.py: the "Lab" tab of the hidden, owner-only
dashboard Debug page (/dashboard/debug -> server/routes/debug.py ->
internal_api/routes/debug.py -> here). Companion of debug_actions_service.py.

Every tool here is SIDE-EFFECT FREE:
  * nothing writes feature state, the DB or an audit row (read-only repo calls
    only: permission grants, event bindings, templates, saved settings);
  * the only thing that can reach Discord is an explicit "send a copy" of a
    rendered preview, and that goes through debug_actions_service.SafeChannel
    (owner-only pings, "[TEST]" prefix, live views dropped) into a channel the
    owner picked. Rendering a preview for a chosen member never pings or DMs
    that member.

Tools:
  * simulate_permissions(): what a member (or a hypothetical role set) can do
    under the bot's own permission system: staff bypass, which grant gives
    each permission, and which Discord commands they pass (requirements are
    read from each command's source, see command_requirements()).
  * render_preview(): welcome / boost / event-binding / template output for a
    chosen member, captured instead of sent (CaptureChannel), as JSON.
  * simulate_antispam(): run sample messages through services/spam_detector.py
    with a fake clock and the saved (optionally overridden) thresholds.
"""

from __future__ import annotations

import ast
import inspect
import logging
import re
import textwrap
from typing import Any

import discord

log = logging.getLogger(__name__)


class LabError(Exception):
    """User-facing problem with a Lab request (unknown role, no binding, ...)."""


def _sid(value: Any) -> str | None:
    return str(value) if value else None


# ---------------------------------------------------------------------------
# Command requirements (read from source)
# ---------------------------------------------------------------------------

_STAFF_CALLS = {"is_staff", "is_staff_interaction"}
_ANY_STAFF_CALLS = {"is_any_staff", "is_any_staff_interaction"}  # == has_permission("staff_base")
_SCAN_DEPTH = 3


def _call_name(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _resolve_str(node: ast.AST, globs: dict) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        value = globs.get(node.id)
        return value if isinstance(value, str) else None
    return None


def _mentions_owner_id(node: ast.Compare) -> bool:
    for part in [node.left, *node.comparators]:
        if isinstance(part, ast.Name) and part.id == "OWNER_ID":
            return True
        if isinstance(part, ast.Attribute) and part.attr == "OWNER_ID":
            return True
    return False


def scan_checks(fn: Any, owner: Any = None, depth: int = _SCAN_DEPTH, _seen: set | None = None) -> list[dict]:
    """Permission checks found in `fn`'s source, in order, following helper
    calls (self.<method> or module functions defined in cogs.*) up to `depth`
    levels. Each check is {"type": "permission"|"staff"|"owner", "permission"?}.
    Heuristic by design: it finds has_permission("x") / is_staff(...) /
    is_any_staff(...) / `== OWNER_ID` calls, not arbitrary logic."""
    from services.permission_service import VALID_PERMISSIONS

    seen = _seen if _seen is not None else set()
    fn = inspect.unwrap(fn)
    if id(fn) in seen:
        return []
    seen.add(id(fn))
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError, IndentationError):
        return []
    globs = getattr(fn, "__globals__", {}) or {}
    # (line, col, check): ast.walk is breadth-first, so sort by source position
    # afterwards; the first check is treated as the command's entry gate.
    found: list[tuple[int, int, dict]] = []

    def add(node: ast.AST, check: dict) -> None:
        found.append((getattr(node, "lineno", 0), getattr(node, "col_offset", 0), check))

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and _mentions_owner_id(node):
            add(node, {"type": "owner"})
            continue
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name == "has_permission":
            args = list(node.args[1:2]) + [k.value for k in node.keywords if k.arg == "permission"]
            for arg in args:
                perm = _resolve_str(arg, globs)
                if perm in VALID_PERMISSIONS:
                    add(node, {"type": "permission", "permission": perm})
        elif name in _STAFF_CALLS:
            add(node, {"type": "staff"})
        elif name in _ANY_STAFF_CALLS:
            add(node, {"type": "permission", "permission": "staff_base"})
        elif depth > 0 and name:
            target = None
            f = node.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "self" and owner is not None:
                target = getattr(type(owner), name, None)
            elif isinstance(f, ast.Name):
                target = globs.get(name)
            if inspect.isfunction(target) and str(getattr(target, "__module__", "")).startswith("cogs."):
                for check in scan_checks(target, owner, depth - 1, seen):
                    add(node, check)
    out: list[dict] = []
    for _, _, c in sorted(found, key=lambda t: (t[0], t[1])):
        if c not in out:
            out.append(c)
    return out


def _perm_names(perms: discord.Permissions | None) -> list[str]:
    if perms is None:
        return []
    return [name for name, value in perms if value]


def command_requirements(bot: Any) -> list[dict]:
    """Every loaded prefix and slash command with the checks found in its
    source. Groups themselves are skipped (their subcommands are listed)."""
    from discord import app_commands
    from discord.ext import commands as ext

    out: list[dict] = []
    try:
        prefix_cmds = list(bot.walk_commands())
    except Exception:  # noqa: BLE001 - a MagicMock bot in tests, etc.
        prefix_cmds = []
    for cmd in prefix_cmds:
        if isinstance(cmd, ext.Group) and not cmd.invoke_without_command:
            continue
        out.append({
            "name": f"!{cmd.qualified_name}",
            "kind": "prefix",
            "cog": cmd.cog_name,
            "aliases": list(cmd.aliases or []),
            "checks": scan_checks(cmd.callback, cmd.cog),
            "custom_checks": len(getattr(cmd, "checks", []) or []),
            "default_permissions": [],
        })
    try:
        slash_cmds = list(bot.tree.walk_commands())
    except Exception:  # noqa: BLE001
        slash_cmds = []
    for cmd in slash_cmds:
        if isinstance(cmd, app_commands.Group):
            continue
        root = cmd
        while getattr(root, "parent", None) is not None:
            root = root.parent
        binding = getattr(cmd, "binding", None)
        out.append({
            "name": f"/{cmd.qualified_name}",
            "kind": "slash",
            "cog": getattr(binding, "qualified_name", None),
            "aliases": [],
            "checks": scan_checks(cmd.callback, binding),
            "custom_checks": len(getattr(cmd, "checks", []) or []),
            # Discord-side visibility (the server's Integrations settings can override it).
            "default_permissions": _perm_names(getattr(root, "default_permissions", None)),
        })
    out.sort(key=lambda c: (c["kind"], c["name"]))
    return out


# ---------------------------------------------------------------------------
# Permission simulator
# ---------------------------------------------------------------------------

async def simulate_permissions(
    bot: Any, guild: discord.Guild, *, member: discord.Member | None = None, role_ids: list[int] | None = None
) -> dict:
    """What `member` (or a member holding exactly `role_ids` + @everyone) can
    do: staff bypass, per-permission grants, and Discord commands. Read-only."""
    from services import permission_service as ps

    service = getattr(bot, "permission_service", None)
    if service is None:
        raise LabError("The permission service is not loaded.")
    notes: list[str] = []
    if member is not None:
        roles = list(member.roles)
        is_owner = bool(ps.OWNER_ID) and member.id == ps.OWNER_ID
        subject = {
            "mode": "member", "user_id": str(member.id), "name": member.name,
            "display_name": member.display_name, "avatar_url": str(member.display_avatar.url),
            "is_bot": bool(member.bot),
        }
    else:
        wanted = list(dict.fromkeys(role_ids or []))
        roles = [guild.default_role]
        for rid in wanted:
            role = guild.get_role(rid)
            if role is None:
                raise LabError(f"No role with ID {rid} in the server.")
            if role.id != guild.default_role.id:
                roles.append(role)
        is_owner = False
        subject = {"mode": "roles", "user_id": None, "name": None, "display_name": None,
                   "avatar_url": None, "is_bot": False}
        notes.append("Hypothetical member holding only these roles (plus @everyone); the owner bypass can't be simulated this way.")

    staff_ids = set(ps.DEFAULT_STAFF_ROLE_IDS or ())
    role_set = {r.id for r in roles}
    staff_roles = [r for r in roles if r.id in staff_ids]
    is_staff = is_owner or bool(staff_roles)
    if member is not None and service.is_staff(member) != is_staff:
        notes.append("Staff check disagrees with PermissionService.is_staff (report this).")

    grants = await service.list_grants(guild.id)
    by_role: dict[int, list[str]] = {}
    for g in grants:
        by_role.setdefault(int(g["role_id"]), []).append(g["permission"])

    perms_out: list[dict] = []
    allowed_set: set[str] = set()
    for perm in sorted(ps.VALID_PERMISSIONS):
        via_grants = [
            {"role_id": str(r.id), "role_name": r.name}
            for r in roles if perm in by_role.get(r.id, [])
        ]
        allowed = is_staff or bool(via_grants)
        via = "owner" if is_owner else ("staff_role" if staff_roles else ("grant" if via_grants else None))
        entry = {"permission": perm, "allowed": allowed, "via": via, "grants": via_grants, "bot_says": None}
        if member is not None:
            actual = bool(await service.has_permission(member, perm))
            entry["bot_says"] = actual
            if actual != allowed:
                notes.append(f"{perm}: simulated {allowed} but has_permission() says {actual}.")
                allowed = actual
                entry["allowed"] = actual
        if allowed:
            allowed_set.add(perm)
        perms_out.append(entry)

    # Discord permissions of the subject (slash default_permissions visibility).
    if member is not None:
        discord_perms = member.guild_permissions
    else:
        value = 0
        for r in roles:
            value |= r.permissions.value
        discord_perms = discord.Permissions(value)
        if discord_perms.administrator:
            discord_perms = discord.Permissions.all()

    def passes(check: dict) -> bool:
        if check["type"] == "owner":
            return is_owner
        if check["type"] == "staff":
            return is_staff
        return check.get("permission") in allowed_set

    commands_out = []
    for cmd in command_requirements(bot):
        checks = [{**c, "passes": passes(c)} for c in cmd["checks"]]
        needed = cmd["default_permissions"]
        discord_ok = all(getattr(discord_perms, p, False) for p in needed) or discord_perms.administrator
        if not checks:
            access = "yes"
        elif all(c["passes"] for c in checks):
            access = "yes"
        elif checks[0]["passes"]:
            access = "partial"  # passes the entry check, not every sub-action's
        else:
            access = "no"
        commands_out.append({**cmd, "checks": checks, "access": access, "discord_visible": discord_ok})

    role_rows = [
        {
            "id": str(r.id), "name": r.name, "position": r.position,
            "is_default": r.id == guild.default_role.id, "staff": r.id in staff_ids,
            "grants": sorted(by_role.get(r.id, [])),
        }
        for r in sorted(roles, key=lambda r: r.position, reverse=True)
    ]
    staff_reason = "owner" if is_owner else ("staff_role" if staff_roles else None)
    return {
        "subject": {**subject, "roles": role_rows},
        "is_owner": is_owner,
        "is_staff": is_staff,
        "staff_reason": staff_reason,
        "staff_roles": [{"id": str(r.id), "name": r.name} for r in staff_roles],
        "permissions": perms_out,
        "commands": commands_out,
        "discord_permissions": _perm_names(discord_perms),
        "unknown_role_grants": sorted(
            {str(rid) for rid in by_role if guild.get_role(rid) is None}
        ),
        "notes": notes,
        "role_set": [str(r) for r in sorted(role_set)],
    }


# ---------------------------------------------------------------------------
# Message previews (captured, not sent)
# ---------------------------------------------------------------------------

PREVIEW_KINDS = ("welcome", "boost", "event", "template")


class CaptureChannel:
    """Stand-in channel that records sends instead of posting. Attribute reads
    (name, mention, id, ...) go to the real channel the feature would use, so
    {channel} placeholders render as they would in production."""

    def __init__(self, channel: Any = None) -> None:
        self._channel = channel
        self.sent: list[dict] = []

    def __getattr__(self, name: str) -> Any:
        channel = self.__dict__.get("_channel")
        if channel is None:
            raise AttributeError(name)
        return getattr(channel, name)

    async def send(self, content: str | None = None, **kwargs: Any) -> None:
        embeds = list(kwargs.get("embeds") or [])
        if kwargs.get("embed") is not None:
            embeds.append(kwargs["embed"])
        self.sent.append({"content": content, "embeds": embeds})
        return None


def _jsonable(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        embeds = [e.to_dict() if isinstance(e, discord.Embed) else e for e in (m.get("embeds") or [])[:10]]
        out.append({"content": m.get("content"), "embeds": embeds})
    return out


def placeholder_values(guild: discord.Guild, member: discord.Member | None, channel: Any = None,
                       role: discord.Role | None = None) -> dict[str, str]:
    """{name: rendered value} for every registered template placeholder."""
    from core.placeholder_engine import _REGISTRY, RenderContext

    ctx = RenderContext(guild=guild, member=member, channel=channel, extra={"role": role})
    out: dict[str, str] = {}
    for name, fn in sorted(_REGISTRY.items()):
        try:
            out[name] = str(fn(ctx))
        except Exception:  # noqa: BLE001 - same fallback as resolve_text
            out[name] = ""
    return out


async def _feature_channel(service: Any, guild: discord.Guild) -> tuple[Any, list[str]]:
    notes: list[str] = []
    cfg = await service.get_config(guild.id) if hasattr(service, "get_config") else None
    channel = guild.get_channel(cfg["channel_id"]) if cfg and cfg.get("channel_id") else None
    if not cfg or not cfg.get("channel_id"):
        notes.append("No channel is configured, so the real feature would not post anything.")
    elif channel is None:
        notes.append("The configured channel no longer exists.")
    if cfg and not cfg.get("enabled", True):
        notes.append("This feature is currently disabled.")
    return channel, notes


async def render_preview(
    bot: Any,
    guild: discord.Guild,
    member: discord.Member,
    kind: str,
    *,
    event_type: str | None = None,
    role: discord.Role | None = None,
    template: str | None = None,
) -> dict:
    """Render what a feature would post for `member` without posting it.
    Returns {"kind", "messages": [{"content", "embeds": [Embed]}], "channel_id",
    "notes", "placeholders"}; `messages` still holds discord.Embed objects
    (to_json() turns them into dicts for the API)."""
    from core.placeholder_engine import RenderContext
    from core.renderer import TemplateRenderer
    from database.repositories.template_repo import TemplateRepository

    notes: list[str] = []
    channel: Any = None
    messages: list[dict] = []
    if kind in ("welcome", "boost"):
        cog = bot.get_cog("Welcome" if kind == "welcome" else "Boost")
        service = getattr(cog, "_service", None) if cog is not None else None
        if service is None:
            raise LabError(f"The {kind} cog is not loaded.")
        channel, notes = await _feature_channel(service, guild)
        cap = CaptureChannel(channel)
        if kind == "welcome":
            await service.send_welcome(cap, member)
        else:
            await service.send_boost(cap, member)
        messages = cap.sent
    elif kind == "event":
        from database.repositories.event_repo import EventRepository
        from services.event_service import VALID_EVENT_TYPES

        if event_type not in VALID_EVENT_TYPES:
            raise LabError("Pick an event type.")
        db = getattr(bot, "db", None)
        if db is None:
            raise LabError("bot.db is unavailable.")
        events = EventRepository(db)
        binding = await events.get_binding(guild.id, event_type)
        if not binding:
            raise LabError(f"No message is bound to {event_type}.")
        if not binding.get("enabled", 1):
            notes.append("This binding is disabled, so nothing is posted for real.")
        channel = guild.get_channel(binding["channel_id"])
        if channel is None:
            notes.append("The bound channel no longer exists.")
        if binding.get("send_dm"):
            notes.append("The member would also get this as a DM.")
        if event_type in ("role_add", "role_remove") and role is None:
            notes.append("No role picked, so {role} renders empty.")
        rows = await events.get_templates_for_binding(binding["id"])
        if not rows:
            notes.append("No template is attached to this binding.")
        templates = TemplateRepository(db)
        renderer = TemplateRenderer()
        ctx = RenderContext(guild=guild, member=member, channel=channel, extra={"role": role})
        for row in rows:
            full = await templates.get_full(row["id"])
            if not full:
                continue
            rendered = renderer.render_message(full, ctx)
            if rendered.get("content") or rendered.get("embeds"):
                messages.append({"content": rendered.get("content"), "embeds": rendered.get("embeds") or [],
                                 "template": row.get("name")})
    elif kind == "template":
        db = getattr(bot, "db", None)
        if db is None:
            raise LabError("bot.db is unavailable.")
        name = (template or "").strip()
        if not name:
            raise LabError("Pick a template.")
        repo = TemplateRepository(db)
        row = await repo.get_by_name(guild.id, name)
        if not row or row.get("guild_id") not in (None, guild.id):
            raise LabError("Template not found.")
        full = await repo.get_full(row["id"])
        rendered = TemplateRenderer().render_message(
            full or {}, RenderContext(guild=guild, member=member, channel=None, extra={"role": role})
        )
        messages.append({"content": rendered.get("content"), "embeds": rendered.get("embeds") or [], "template": name})
    else:
        raise LabError("Unknown preview kind.")

    if not messages:
        notes.append("It rendered nothing.")
    return {
        "kind": kind,
        "event_type": event_type,
        "messages": messages,
        "channel_id": _sid(getattr(channel, "id", None)),
        "channel_name": getattr(channel, "name", None),
        "notes": notes,
        "placeholders": placeholder_values(guild, member, channel, role),
        "member": {"user_id": str(member.id), "display_name": member.display_name, "name": member.name},
    }


def to_json(preview: dict) -> dict:
    out = {k: v for k, v in preview.items() if k != "messages"}
    out["messages"] = [
        {**_jsonable([m])[0], "template": m.get("template")} for m in preview.get("messages", [])
    ]
    return out


async def send_preview(preview: dict, channel: discord.TextChannel, owner_id: int) -> dict:
    """Post a rendered preview into the owner's picked channel through
    SafeChannel (owner-only pings, [TEST] label, never the member's DMs)."""
    from services.debug_actions_service import SafeChannel

    label = f"Lab preview: {preview.get('event_type') or preview.get('kind')}"
    safe = SafeChannel(channel, owner_id, label)
    for m in preview.get("messages", []):
        embeds = [e for e in (m.get("embeds") or []) if isinstance(e, discord.Embed)][:10]
        await safe.send(m.get("content") or None, embeds=embeds)
    return {
        "channel_id": str(channel.id),
        "message_ids": [str(m.id) for m in safe.sent],
        "jump_url": safe.sent[0].jump_url if safe.sent else None,
    }


# ---------------------------------------------------------------------------
# Anti-spam tester (pure)
# ---------------------------------------------------------------------------

MAX_SAMPLE_MESSAGES = 200
_USER_MENTION_RE = re.compile(r"<@!?([0-9]{15,20})>")
_ROLE_MENTION_RE = re.compile(r"<@&([0-9]{15,20})>")


def count_mentions(content: str | None) -> int:
    """Distinct user + role mentions in raw text (cogs/anti_spam.mention_count
    reads discord.py's raw_mentions / raw_role_mentions, which use the same
    patterns)."""
    text = content or ""
    return len(set(_USER_MENTION_RE.findall(text))) + len(set(_ROLE_MENTION_RE.findall(text)))


def simulate_antispam(rules: Any, messages: list[dict]) -> dict:
    """Feed sample messages through a fresh SpamDetector with a fake clock.
    Each message: {"at": seconds from start, "user"?: label, "channel"?: label,
    "content"?: str, "mentions"?: int (default: counted from content)}.
    Messages are processed in time order (stable). Nothing is sent or stored."""
    from services.spam_detector import SpamDetector

    if not isinstance(messages, list) or not messages:
        raise LabError("Add at least one message.")
    if len(messages) > MAX_SAMPLE_MESSAGES:
        raise LabError(f"At most {MAX_SAMPLE_MESSAGES} messages.")
    parsed = []
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            raise LabError(f"Message {i + 1} is not an object.")
        try:
            at = float(m.get("at", 0) or 0)
        except (TypeError, ValueError):
            raise LabError(f"Message {i + 1}: time must be a number of seconds.")
        if at < 0 or at > 86_400:
            raise LabError(f"Message {i + 1}: time must be 0 to 86400 seconds.")
        content = m.get("content")
        content = str(content)[:4000] if content not in (None, "") else None
        mentions = m.get("mentions")
        if mentions in (None, ""):
            mentions = count_mentions(content)
        else:
            try:
                mentions = max(0, min(int(mentions), 1000))
            except (TypeError, ValueError):
                raise LabError(f"Message {i + 1}: mentions must be a whole number.")
        parsed.append({
            "index": i, "at": at, "user": str(m.get("user") or "A")[:32],
            "channel": str(m.get("channel") or "general")[:64], "content": content, "mentions": mentions,
        })
    order = sorted(parsed, key=lambda p: p["at"])
    now = [0.0]
    detector = SpamDetector(clock=lambda: now[0])
    user_ids: dict[str, int] = {}
    channel_ids: dict[str, int] = {}
    results: list[dict] = [dict() for _ in parsed]
    deleted: set[int] = set()
    counts: dict[str, int] = {}
    timeouts: list[dict] = []
    for p in order:
        now[0] = p["at"]
        uid = user_ids.setdefault(p["user"], len(user_ids) + 1)
        cid = channel_ids.setdefault(p["channel"], len(channel_ids) + 1)
        verdict = detector.observe(uid, cid, p["index"] + 1, p["content"], p["mentions"], rules)
        row = {**p, "verdict": None}
        if verdict is not None:
            deletes = sorted(mid - 1 for _, mid in verdict.messages)
            deleted.update(deletes)
            counts[verdict.rule] = counts.get(verdict.rule, 0) + 1
            row["verdict"] = {"rule": verdict.rule, "detail": verdict.detail, "first": verdict.first, "deletes": deletes}
            if verdict.first and rules.timeout_minutes > 0:
                timeouts.append({"user": p["user"], "at": p["at"], "minutes": rules.timeout_minutes, "rule": verdict.rule})
        results[p["index"]] = row
    notes = []
    if not rules.enabled:
        notes.append("Anti-spam is OFF in the saved settings: the bot would not check any of these.")
    notes.append("Staff, admins, the server owner and members with moderate_members/manage_messages_bulk are never checked by the real bot.")
    return {
        "rules": {
            "enabled": rules.enabled, "flood_messages": rules.flood_messages, "flood_seconds": rules.flood_seconds,
            "duplicate_messages": rules.duplicate_messages, "duplicate_seconds": rules.duplicate_seconds,
            "mention_limit": rules.mention_limit, "timeout_minutes": rules.timeout_minutes,
        },
        "results": results,
        "deleted": sorted(deleted),
        "counts": counts,
        "timeouts": timeouts,
        "notes": notes,
    }


async def effective_spam_rules(bot: Any, guild_id: int, overrides: dict | None = None) -> Any:
    """The saved anti-spam rules (what the cog uses), with optional per-test
    overrides validated exactly like a Settings save would be."""
    from database.repositories.settings_repo import SettingsRepository
    from services.settings_service import SETTINGS_SCHEMA, SettingsService, parse_value
    from services.spam_detector import SpamRules

    db = getattr(bot, "db", None)
    if db is None:
        raise LabError("bot.db is unavailable.")
    values = await SettingsService(SettingsRepository(db)).get_effective(guild_id)
    errors: dict[str, str] = {}
    for key, value in (overrides or {}).items():
        field = SETTINGS_SCHEMA.get(key)
        if field is None or field.group != "antispam":
            errors[str(key)] = "Not an anti-spam setting."
            continue
        if value is None or value == "":
            continue
        try:
            values[key] = parse_value(field, value)
        except ValueError as exc:
            errors[key] = str(exc)
    if errors:
        raise LabError("; ".join(f"{k}: {v}" for k, v in errors.items()))
    return SpamRules.from_settings(values)


# ---------------------------------------------------------------------------
# Options for the Lab pickers
# ---------------------------------------------------------------------------

async def lab_options(bot: Any, guild: discord.Guild) -> dict:
    from database.repositories.event_repo import EventRepository
    from database.repositories.template_repo import TemplateRepository
    from services.event_service import VALID_EVENT_TYPES
    from services.settings_service import SETTINGS_SCHEMA

    db = getattr(bot, "db", None)
    templates: list[str] = []
    bindings: list[dict] = []
    antispam: dict = {}
    if db is not None:
        try:
            templates = sorted({r["name"] for r in await TemplateRepository(db).list_for_guild(guild.id) if r.get("name")})
        except Exception:  # noqa: BLE001 - pickers are best-effort
            log.exception("lab: could not list templates")
        try:
            events = EventRepository(db)
            for b in await events.list_bindings(guild.id):
                rows = await events.get_templates_for_binding(b["id"])
                ch = guild.get_channel(b["channel_id"])
                bindings.append({
                    "event_type": b["event_type"], "channel_id": _sid(b["channel_id"]),
                    "channel_name": getattr(ch, "name", None), "enabled": bool(b.get("enabled", 1)),
                    "send_dm": bool(b.get("send_dm")), "templates": [r["name"] for r in rows],
                })
        except Exception:  # noqa: BLE001
            log.exception("lab: could not list event bindings")
        try:
            rules = await effective_spam_rules(bot, guild.id)
            antispam = {
                "enabled": rules.enabled, "flood_messages": rules.flood_messages, "flood_seconds": rules.flood_seconds,
                "duplicate_messages": rules.duplicate_messages, "duplicate_seconds": rules.duplicate_seconds,
                "mention_limit": rules.mention_limit, "timeout_minutes": rules.timeout_minutes,
            }
        except Exception:  # noqa: BLE001
            log.exception("lab: could not read anti-spam rules")
    from services import permission_service as ps

    roles = [
        {"id": str(r.id), "name": r.name, "position": r.position, "color": r.color.value,
         "managed": r.managed, "staff": r.id in set(ps.DEFAULT_STAFF_ROLE_IDS or ())}
        for r in sorted(guild.roles, key=lambda r: r.position, reverse=True)
        if r.id != guild.default_role.id
    ]
    return {
        "roles": roles,
        "templates": templates,
        "event_types": sorted(VALID_EVENT_TYPES),
        "bindings": bindings,
        "features": {
            "welcome": bot.get_cog("Welcome") is not None,
            "boost": bot.get_cog("Boost") is not None,
        },
        "antispam": antispam,
        "antispam_fields": [
            {"key": f.key, "label": f.label, "min": f.min, "max": f.max, "type": f.type}
            for f in SETTINGS_SCHEMA.values() if f.group == "antispam"
        ],
        "valid_permissions": sorted(ps.VALID_PERMISSIONS),
    }
