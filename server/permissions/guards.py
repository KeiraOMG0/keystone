"""
require_permission(permission_str) — a FastAPI dependency FACTORY.

Usage in a route:
    @router.get("/dashboard/stats")
    async def stats(session: Session = Depends(require_permission("staff_base"))):
        ...

Order of checks:
  1. Resolve the current session (401 {"error": "unauthenticated"} if none).
  2. If the session isn't a guild member (determined once, at login time in
     auth/oauth.py's callback — not re-checked against Discord here), reject
     with 403 {"error": "not_a_member"}.
  3. Call Step 1's internal API (services/internal_client.check_permission)
     with the session's discord_user_id. This re-derives permissions from
     LIVE discord.Member.roles on every call (source of truth is the live
     bot cache, not session.cached_role_ids, which is only used for display
     purposes in /api/me) — 403 {"error": "forbidden", ...} if denied.
     A bot that cannot answer (see bot_unreachable) -> 502
     {"error": "internal_api_unavailable"}; a bot that answers 404 "Member ...
     not found" (left/kicked/banned since login) -> 403 not_a_member, never the
     "bot offline" 502.
"""

from __future__ import annotations

import json
import os

from fastapi import Depends, HTTPException, Request, status
from pydantic import BaseModel, ValidationError

from server import config
from server.auth.dependencies import require_session
from server.db.models import Session
from server.services import internal_client
from server.services.internal_client import InternalAPIError


NOT_A_MEMBER_DETAIL = {
    "error": "not_a_member",
    "message": "You are not a member of this server (anymore).",
}
STAFF_FORBIDDEN_DETAIL = {
    "error": "forbidden",
    "required_permission": "staff",
    "message": "This page is restricted to default staff roles or the server owner.",
}


# ---------------------------------------------------------------------------
# "Can the bot answer?" vs "the bot answered no". Shared by require_permission,
# require_staff, /api/me and the live hub (server/live/hub.py resolve_access).
# server/routes/diagnostics.py has its own copy of the first two (_bot_unreachable,
# _cached_staff) for require_staff_even_if_bot_down; keep them in sync.
# ---------------------------------------------------------------------------

def bot_unreachable(exc: InternalAPIError) -> bool:
    """True only when the bot genuinely cannot answer: no HTTP answer at all
    (connection refused / timeout), a 5xx (bot instance not attached yet = 503,
    Discord API error = 502, crash = 500), or the guild not cached yet during
    startup (404 "Guild ... not found"). Anything else (404 "Member ... not
    found", 401, 422, an unrecognised 404) is the bot answering: fail closed."""
    code = exc.status_code
    if code is None or code >= 500:
        return True
    if code == 404 and isinstance(exc.detail, str) and exc.detail.startswith("Guild "):
        return True
    return False


def member_gone(exc: InternalAPIError) -> bool:
    """The bot is up and says this user is not in the guild (left, kicked or
    banned since login): 404 "Member ... not found"."""
    return exc.status_code == 404 and isinstance(exc.detail, str) and exc.detail.startswith("Member ")


def cached_staff(session: Session) -> bool:
    """Mirror of PermissionService.is_staff on the roles cached at login. ONLY
    for when the bot cannot answer (see bot_unreachable)."""
    owner = int(os.environ.get("OWNER_ID") or "0")
    if owner and int(session.discord_user_id) == owner:
        return True
    staff_roles = {int(x) for x in (os.environ.get("STAFF_ROLE_IDS") or "").split(",") if x.strip().isdigit()}
    return bool(staff_roles & {int(r) for r in (session.cached_role_ids or [])})


def require_permission(permission: str):
    async def _dependency(session: Session = Depends(require_session)) -> Session:
        if not session.is_guild_member:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "not_a_member"},
            )

        try:
            allowed = await internal_client.check_permission(
                config.DASHBOARD_GUILD_ID, session.discord_user_id, permission
            )
        except InternalAPIError as exc:
            if member_gone(exc):
                # The bot is up and says this user is no longer in the guild
                # (kicked/banned/left since login): a verdict, not "bot offline".
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail=NOT_A_MEMBER_DETAIL
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error": "internal_api_unavailable"},
            ) from exc

        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "forbidden",
                    "required_permission": permission,
                    "message": f"You do not have the '{permission}' permission.",
                },
            )

        return session

    return _dependency


def require_any_bot_permission(permissions):
    """Passes when the user holds AT LEAST ONE of `permissions` (staff hold
    all of them: has_permission's staff bypass), checked in ONE check-bulk
    round trip (require_any_permission below checks a few in sequence; this
    one is meant for the whole permission list). For read-only metadata that
    every feature page needs (e.g. /api/resolve: the Moderation page is gated
    on moderate_members, Tickets on manage_tickets, so staff_base alone would
    403 them). One check-bulk round trip; same error rules as
    require_permission (member gone -> 403 not_a_member, bot unable to
    answer -> 502, no fallback to cached grants)."""
    wanted = list(dict.fromkeys(permissions))

    async def _dependency(session: Session = Depends(require_session)) -> Session:
        if not session.is_guild_member:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "not_a_member"})
        try:
            results = await internal_client.check_permissions_bulk(
                config.DASHBOARD_GUILD_ID, session.discord_user_id, wanted
            )
        except InternalAPIError as exc:
            if member_gone(exc):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=NOT_A_MEMBER_DETAIL) from exc
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": "internal_api_unavailable"},
            ) from exc
        if not any(bool(results.get(p)) for p in wanted):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "forbidden",
                    "required_permission": "any",
                    "message": "You need at least one dashboard permission.",
                },
            )
        return session

    return _dependency


def require_any_permission(*permissions: str):
    """Like require_permission, but passes when the user holds ANY of
    `permissions` (checked in order, stops at the first grant). Same
    not-a-member / bot-unreachable handling. The 403 names the first
    permission as `required_permission` and lists all of them in `any_of`.
    Used where several Discord commands share one read (e.g. the Moderation
    page's member search: moderate_members, kick_members or ban_members)."""
    async def _dependency(session: Session = Depends(require_session)) -> Session:
        if not session.is_guild_member:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "not_a_member"},
            )
        for permission in permissions:
            try:
                allowed = await internal_client.check_permission(
                    config.DASHBOARD_GUILD_ID, session.discord_user_id, permission
                )
            except InternalAPIError as exc:
                if member_gone(exc):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN, detail=NOT_A_MEMBER_DETAIL
                    ) from exc
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail={"error": "internal_api_unavailable"},
                ) from exc
            if allowed:
                return session
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "forbidden",
                "required_permission": permissions[0],
                "any_of": list(permissions),
                "message": "You need one of: " + ", ".join(f"'{p}'" for p in permissions) + ".",
            },
        )

    return _dependency


# ---------------------------------------------------------------------------
# require_staff — stricter than require_permission(...).
#
# Used ONLY for the permission-grants management page (server/routes/
# permissions.py), which lets staff edit which roles hold which fine-grained
# permission strings. Gating that page on has_permission()-with-a-grant
# (e.g. a hypothetical "manage_permissions" string added to VALID_PERMISSIONS)
# would create a self-service privilege-escalation path: any role holding
# that grant could grant itself — or any other role — further permissions,
# with no ceiling. is_staff() (owner or a DEFAULT_STAFF_ROLE_IDS role, both
# fixed at deploy/config time — see services/permission_service.py) cannot be
# escalated via the grants table at all, which makes it the correct trust
# boundary for a page whose entire purpose is managing that table.
#
# Calls internal_client.check_is_staff, which wraps
# PermissionService.is_staff() (internal_api/routes/permissions.py's
# POST /guilds/{guild_id}/permissions/is-staff) — same "re-derive from LIVE
# discord.Member.roles on every call" contract as require_permission above.
# ---------------------------------------------------------------------------


async def require_staff(session: Session = Depends(require_session)) -> Session:
    if not session.is_guild_member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "not_a_member"},
        )

    try:
        is_staff = await internal_client.check_is_staff(config.DASHBOARD_GUILD_ID, session.discord_user_id)
    except InternalAPIError as exc:
        # `from exc` on purpose: server/routes/diagnostics.py's bot-down
        # fallback reads the InternalAPIError (status code + detail) off
        # __cause__ to tell "bot unreachable" from "member left the guild".
        if member_gone(exc):
            # Definitive answer from a running bot: no access (never "offline").
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=STAFF_FORBIDDEN_DETAIL) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "internal_api_unavailable"},
        ) from exc

    if not is_staff:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=STAFF_FORBIDDEN_DETAIL)

    return session


# ---------------------------------------------------------------------------
# require_owner — the hidden owner-only Debug page (server/routes/debug.py).
#
# Compares the session's Discord user ID with OWNER_ID from the root .env
# (server/config.py). Deliberately answers 404 {"error": "not_found"} (never
# 401/403) for EVERYONE else — no session, non-member, staff, anyone — so the
# section's existence is not revealed. Needs no bot round trip, so it keeps
# working while the bot is down. OWNER_ID unset (0) disables it for all.
# The 404 must be the same whatever the method or body: hidden routers use it
# as a router-level dependency, POST bodies go through owner_body() (validated
# only after this check), and server/app.py's /api catch-all turns wrong
# methods / trailing slashes into the same bare 404.
# ---------------------------------------------------------------------------

from server.auth.dependencies import get_current_session  # noqa: E402


class HiddenRouteNotFound(Exception):
    """Raised by require_owner. server/app.py maps it to a bare
    JSONResponse(404, {"error": "not_found"}), byte-identical to the SPA
    catch-all's answer for an unknown /api/* path (an HTTPException would
    wrap it as {"detail": {...}} and give the hidden routes away)."""


def is_owner_session(session: Session | None) -> bool:
    return bool(
        session is not None
        and config.OWNER_ID
        and session.is_guild_member
        and int(session.discord_user_id) == config.OWNER_ID
    )


async def require_owner(session: Session | None = Depends(get_current_session)) -> Session:
    if not is_owner_session(session):
        raise HiddenRouteNotFound()
    assert session is not None
    return session


async def owner_body(request: Request, model: type[BaseModel]) -> BaseModel:
    """Read + validate a JSON body for a hidden owner-only POST route, AFTER
    require_owner has passed. Such routes must NOT declare a pydantic body
    parameter: FastAPI parses a declared JSON body before running any
    dependency, so a malformed body would answer 422 to everyone and give the
    route away. Empty body or `null` -> the model's defaults. Invalid ->
    422 {"error": "invalid_request"}, which only the owner can ever see."""
    try:
        raw = await request.body()
        data = json.loads(raw) if raw else None
        return model.model_validate({} if data is None else data)
    except (ValueError, ValidationError):  # json.JSONDecodeError is a ValueError
        raise HTTPException(status_code=422, detail={"error": "invalid_request"})
