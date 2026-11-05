"""
services/verify_service.py - the shared `!verify` implementation.

Used by the `!verify` prefix command (cogs/verify.py), the ticket system's
"Verify & Close" button (cogs/tickets.py, via cogs.verify.verify_member) and
the dashboard's member profile "Verify" button (internal_api/routes/
member_tools.py), so the three entry points can never drift apart.

verify_member(): removes the Unverified role, adds the Verified role, sends
the `VERIFY_TEMPLATE_NAME` template (or a plain fallback) into a
caller-supplied channel (never a DM) and writes a `member_verified` audit row.

Role / template / category IDs are read from `config` at CALL time (not copied
at import like cogs/verify.py does), so this module is safe to import from the
internal API's import chain (see services/settings_service.IMPORT_TIME_COPIES).
"""

from __future__ import annotations

import logging

import discord

import config as _config
from core.placeholder_engine import RenderContext

log = logging.getLogger(__name__)

# The how-to-verify memo: posted automatically when a verify ticket opens
# (services/ticket_service.py) and again on demand with `!welcome-verify` /
# `!wv` (cogs/verify.py). Edit its wording on the dashboard Templates page.
WELCOME_VERIFY_TEMPLATE_NAME = "welcome-verify"


def welcome_verify_fallback(member) -> str:
    """Plain memo used when the welcome-verify template is missing, empty or broken."""
    return (
        f"Hi {member.mention}! To get verified, post ONE of these here: a selfie holding a sign "
        "saying \"GG HQ\" and your Discord username, screenshots showing you're verified in another "
        "all-girls server, or a short voice memo with your name, age and where you're from."
    )


def welcome_verify_mentions(member) -> discord.AllowedMentions:
    """The memo may ping the ticket opener and nobody else."""
    return discord.AllowedMentions(everyone=False, roles=False, users=[member])


class VerifyResult:
    """Outcome of verify_member(), enough for a caller to report status."""

    __slots__ = ("ok", "already_verified", "message_status", "error")

    def __init__(
        self,
        ok: bool,
        already_verified: bool = False,
        message_status: str | None = None,
        error: str | None = None,
    ) -> None:
        self.ok = ok
        self.already_verified = already_verified
        self.message_status = message_status
        self.error = error


def verify_channels(guild: discord.Guild) -> list:
    """Text channels of the Unverified category: where `!verify` may be run,
    so the only places the dashboard may send the verify message to."""
    category_id = int(getattr(_config, "UNVERIFIED_CATEGORY_ID", 0) or 0)
    if not category_id:
        return []
    return [
        ch for ch in guild.text_channels
        if getattr(ch, "category_id", None) == category_id
    ]


async def _send_verify_message(member, channel, template_service, renderer, actor=None, source=None) -> str:
    """Send the verify template into `channel`; returns a status string."""
    template_name = _config.VERIFY_TEMPLATE_NAME
    template_full = await template_service.get_full(member.guild.id, template_name)
    ctx = RenderContext(guild=member.guild, member=member, channel=channel, actor=actor,
                        extra={"source": source} if source else {})

    if template_full is None:
        try:
            await channel.send(f"You're verified in **{member.guild.name}**! Welcome in.")
            return "sent default message (no template configured)"
        except discord.Forbidden:
            return "could not send message (missing permissions) — no template configured either"

    try:
        rendered = renderer.render_message(template_full, ctx)
    except Exception as e:
        log.error("verify: failed to render template '%s': %s", template_name, e)
        return f"role updated, but the '{template_name}' template failed to render"

    content = rendered.get("content") or None
    embeds = rendered.get("embeds") or []
    if not content and not embeds:
        return f"role updated, but the '{template_name}' template is empty"

    try:
        await channel.send(content=content, embeds=embeds[:10])
        return "sent verify message"
    except discord.Forbidden:
        return "could not send message (missing permissions)"
    except discord.HTTPException as e:
        return f"failed to send verify message: {e}"


async def verify_member(
    member,
    actor,
    channel,
    template_service,
    renderer,
    audit,
    *,
    source: str | None = None,
) -> VerifyResult:
    """Role-swap + channel message + audit log (see module docstring).
    `source` ("dashboard") is added to the audit detail when given."""
    guild = member.guild
    unverified_role = guild.get_role(_config.UNVERIFIED_ROLE_ID)
    verified_role = guild.get_role(_config.VERIFIED_ROLE_ID)
    if verified_role is None:
        return VerifyResult(ok=False, error="VERIFIED_ROLE_ID is not configured or the role no longer exists.")

    if verified_role in member.roles:
        return VerifyResult(ok=False, already_verified=True)

    try:
        if unverified_role is not None and unverified_role in member.roles:
            await member.remove_roles(unverified_role, reason=f"Verified by {actor}")
        await member.add_roles(verified_role, reason=f"Verified by {actor}")
    except discord.Forbidden:
        return VerifyResult(ok=False, error="Missing permissions to update that member's roles.")
    except discord.HTTPException as e:
        return VerifyResult(ok=False, error=f"Failed to update roles: {e}")

    msg_status = await _send_verify_message(member, channel, template_service, renderer, actor=actor, source=source)

    log.info("verify: %s verified by %s (%s)", member, actor, msg_status)
    detail = {"message_status": msg_status}
    if source:
        detail["source"] = source
    await audit.log(
        guild.id, actor.id, "member_verified",
        target_type="member", target_id=member.id,
        detail=detail,
    )

    return VerifyResult(ok=True, message_status=msg_status)
