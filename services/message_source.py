"""
services/message_source.py — who really posted a message, and its full text.

Shared by the honeypot (services/honeypot_service.py), anti-spam
(cogs/anti_spam.py) and the moderation message log (cogs/message_log.py).
Pure, duck-typed helpers (no Discord calls), unit-testable with fakes.

An app reply (a user-installed app, which needs no server authorisation while
@everyone has "Use External Apps", or a server bot answering a slash
command) is authored by the app but carries `interaction_metadata.user`, the
member who ran the command. Discord also sets `webhook_id` on these replies,
so check app_source() BEFORE treating a message as a webhook post.
"""

from __future__ import annotations


def message_text(message) -> str:
    """Text of a message including its embeds' title/description/fields (apps
    and bots usually put everything in embeds)."""
    parts = [getattr(message, "content", None) or ""]
    for embed in list(getattr(message, "embeds", None) or [])[:3]:
        for piece in (getattr(embed, "title", None), getattr(embed, "description", None)):
            if piece:
                parts.append(str(piece))
        for f in list(getattr(embed, "fields", None) or [])[:5]:
            parts.append(f"{f.name}: {f.value}")
    return "\n".join(p for p in parts if p).strip()


def app_source(message) -> dict | None:
    """For a message an app posted because a member ran one of its commands:
    {invoker, app_id, app_name, user_installed}. None for anything else
    (including webhooks and bots posting on their own)."""
    meta = getattr(message, "interaction_metadata", None)
    invoker = getattr(meta, "user", None) if meta is not None else None
    if invoker is None:
        return None
    app = getattr(message, "author", None)
    try:
        user_installed = bool(meta.is_user_integration())
    except Exception:
        user_installed = False
    return {
        "invoker": invoker,
        "app_id": getattr(message, "application_id", None) or getattr(app, "id", None),
        "app_name": getattr(app, "name", None),
        "user_installed": user_installed,
    }


def describe_app(via_app: dict | None) -> str | None:
    if not via_app:
        return None
    kind = "user-installed app" if via_app.get("user_installed") else "app"
    return f"{via_app.get('app_name') or 'unknown'} ({kind}, id {via_app.get('app_id')})"


def author_kind(message, via_app: dict | None = None) -> str:
    """"app" | "webhook" | "bot" | "member" (an app reply is "app" even though
    Discord gives it a webhook_id)."""
    if via_app is not None:
        return "app"
    if getattr(message, "webhook_id", None) is not None:
        return "webhook"
    if getattr(getattr(message, "author", None), "bot", False):
        return "bot"
    return "member"
