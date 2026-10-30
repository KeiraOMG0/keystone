"""
The ONLY module in server/ allowed to make HTTP calls to Step 1's internal
API (internal_api/, embedded in the bot process at
http://{INTERNAL_API_HOST}:{INTERNAL_API_PORT}).

Every call attaches `Authorization: Bearer {INTERNAL_API_SECRET}` — the
shared-secret scheme internal_api.auth.require_internal_secret expects.

Route shapes this wraps (confirmed from internal_api/routes/*.py):
  GET    /guilds/{guild_id}/stats/live
  POST   /guilds/{guild_id}/permissions/check        {"user_id", "permission"} -> {"allowed": bool}
  POST   /guilds/{guild_id}/permissions/check-bulk   {"user_id", "permissions"} -> {perm: bool, ...}
  GET    /guilds/{guild_id}/moderation/warnings/recent?limit=N      -> {"warnings": [...]}
  GET    /guilds/{guild_id}/moderation/warnings/{user_id}          -> {"user_id", "warnings": [...]}
  POST   /guilds/{guild_id}/moderation/warnings     {"user_id", "moderator_id", "reason"} -> {"warning_id": int}
  DELETE /guilds/{guild_id}/moderation/warnings/{warning_id}?moderator_id=&user_id= -> {"removed": bool}
  GET    /guilds/{guild_id}/moderation/history/{user_id}           -> {"user_id", "actions": [...]}
  POST   /guilds/{guild_id}/moderation/kick/{user_id}      {"moderator_id", "reason"} -> {"action_id": int}
  POST   /guilds/{guild_id}/moderation/ban                {"user_id", "moderator_id", "reason", "delete_message_days"} -> {"action_id": int}
  POST   /guilds/{guild_id}/moderation/unban               {"user_id", "moderator_id", "reason"} -> {"action_id": int}
  POST   /guilds/{guild_id}/moderation/timeout/{user_id}   {"moderator_id", "duration_minutes", "reason"} -> {"action_id": int}
  POST   /guilds/{guild_id}/moderation/untimeout/{user_id} {"moderator_id", "reason"} -> {"action_id": int}
  GET    /guilds/{guild_id}/moderation/members/search?q=&limit=  -> {"members": [...]}
  GET    /guilds/{guild_id}/tickets?status_filter=open|closed|archived|all -> [ticket, ...]
  GET    /guilds/{guild_id}/tickets/{ticket_id}                    -> ticket
  POST   /guilds/{guild_id}/tickets/{ticket_id}/close   {"actor_user_id"} -> {"success": bool}
  POST   /guilds/{guild_id}/tickets/{ticket_id}/claim   {"actor_user_id"} -> {"success": bool}
  POST   /guilds/{guild_id}/tickets/{ticket_id}/archive {"actor_user_id"} -> {"success": bool}
  GET    /guilds/{guild_id}/ticket-panels                          -> [panel, ...]
  POST   /guilds/{guild_id}/permissions/is-staff     {"user_id"} -> {"is_staff": bool}
  GET    /guilds/{guild_id}/roles                                  -> {"roles": [...]}
  GET    /guilds/{guild_id}/permission-grants                      -> {"grants": [...], "valid_permissions": [...]}
  POST   /guilds/{guild_id}/permission-grants  {"role_id", "permission", "granted_by"} -> {"granted": bool}
  DELETE /guilds/{guild_id}/permission-grants  {"role_id", "permission", "actor_id"} -> {"revoked": bool}
  GET    /guilds/{guild_id}/honeypot/config                        -> config
  PUT    /guilds/{guild_id}/honeypot/config       {partial}         -> config
  POST   /guilds/{guild_id}/honeypot/setup        {actor_id, ...}   -> {"config", "warnings"}
  POST   /guilds/{guild_id}/honeypot/scramble     {actor_id}        -> {"config", "channel", "category", "warnings"}
  POST   /guilds/{guild_id}/honeypot/invite       {actor_id, channel_id} -> config (rejoin link created)
  GET    /guilds/{guild_id}/honeypot/appeals/for-user/{user_id}     -> {"user_id", "eligible", "appeal"}
  POST   /guilds/{guild_id}/honeypot/appeals/submit-verified {user_id, username, name, message} -> {"ok", "status"}
  POST   /guilds/{guild_id}/honeypot/appeals/rejoin {user_id, access_token} -> {"result"}
  GET    /guilds/{guild_id}/honeypot/catches?limit=&before_id=      -> {"catches": [...]}
  GET    /guilds/{guild_id}/honeypot/stats                          -> stats
  GET    /guilds/{guild_id}/honeypot/appeals?status=                -> {"appeals": [...]}
  POST   /guilds/{guild_id}/honeypot/appeals/{id}/approve {reviewer_id, note?} -> {"ok", "unbanned", "dm_sent"}
  POST   /guilds/{guild_id}/honeypot/appeals/{id}/deny    {reviewer_id, note?} -> {"ok"}
  GET    /guilds/{guild_id}/honeypot/appeals/lookup/{token}         -> {"user_id", "username", "status"}
  GET    /guilds/{guild_id}/diagnostics                             -> full bot diagnostics dump (staff-only page)
  POST   /guilds/{guild_id}/diagnostics/cache/clear {requested_by}  -> {"cleared", "entries", "log_cache_cleared", "at"}
  GET    /guilds/{guild_id}/audit?action&actor_id&target_id&target_type&since&until&before_id&limit -> {"entries", "next_before_id"}
  GET    /guilds/{guild_id}/audit/actions                           -> {"actions": [{"action", "count"}]}
  GET    /health
"""

from __future__ import annotations

import httpx

from server import config

_HEADERS = {"Authorization": f"Bearer {config.INTERNAL_API_SECRET}"}
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class InternalAPIError(Exception):
    """Raised when the internal API is unreachable or returns an unexpected status."""

    def __init__(self, message: str, status_code: int | None = None, detail: object = None):
        super().__init__(message)
        self.status_code = status_code
        # Parsed `detail` from the internal API's error body, when present
        # (e.g. "already_configured" on honeypot setup). Never forward this
        # verbatim to public callers.
        self.detail = detail


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=config.INTERNAL_API_BASE_URL, headers=_HEADERS, timeout=_TIMEOUT)


async def get_guild_stats(guild_id: int) -> dict:
    async with _client() as client:
        try:
            resp = await client.get(f"/guilds/{guild_id}/stats/live")
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for stats/live", status_code=resp.status_code
        )
    return resp.json()


async def check_permission(guild_id: int, user_id: int, permission: str) -> bool:
    async with _client() as client:
        try:
            resp = await client.post(
                f"/guilds/{guild_id}/permissions/check",
                json={"user_id": user_id, "permission": permission},
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        # Keep `detail` (see _permission_error_detail) so callers can tell a
        # bot that cannot answer from a definitive "member not in the guild".
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for permissions/check", status_code=resp.status_code,
            detail=_permission_error_detail(resp),
        )
    return bool(resp.json().get("allowed", False))


async def check_permissions_bulk(guild_id: int, user_id: int, permissions: list[str]) -> dict[str, bool]:
    async with _client() as client:
        try:
            resp = await client.post(
                f"/guilds/{guild_id}/permissions/check-bulk",
                json={"user_id": user_id, "permissions": permissions},
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for permissions/check-bulk", status_code=resp.status_code,
            detail=_permission_error_detail(resp),
        )
    return resp.json()


def _permission_error_detail(resp: httpx.Response) -> object:
    """The internal API's `detail` string on a failed permission check (e.g.
    404 "Member ... not found" vs 404 "Guild ... not found"); None if absent.
    server/permissions/guards.py's bot_unreachable/member_gone read it."""
    try:
        body = resp.json()
    except ValueError:
        return None
    return body.get("detail") if isinstance(body, dict) else None


def _moderation_error(resp: httpx.Response, what: str) -> InternalAPIError:
    """Moderation routes: keep the internal API's `detail` (a dict
    {"error", "message"} for hierarchy/Discord/validation refusals, or a
    string) so server/routes/moderation.py can pass it through."""
    detail = None
    try:
        body = resp.json()
        detail = body.get("detail") if isinstance(body, dict) else None
    except ValueError:
        pass
    return InternalAPIError(
        f"Internal API returned {resp.status_code} for {what}", status_code=resp.status_code, detail=detail
    )


async def get_warnings(guild_id: int, user_id: int) -> dict:
    async with _client() as client:
        try:
            resp = await client.get(f"/guilds/{guild_id}/moderation/warnings/{user_id}")
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise _moderation_error(resp, "moderation/warnings")
    return resp.json()


async def get_recent_warnings(guild_id: int, limit: int = 20) -> dict:
    async with _client() as client:
        try:
            resp = await client.get(
                f"/guilds/{guild_id}/moderation/warnings/recent", params={"limit": limit}
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise _moderation_error(resp, "moderation/warnings/recent")
    return resp.json()


async def add_warning(guild_id: int, user_id: int, moderator_id: int, reason: str | None) -> dict:
    async with _client() as client:
        try:
            resp = await client.post(
                f"/guilds/{guild_id}/moderation/warnings",
                json={"user_id": user_id, "moderator_id": moderator_id, "reason": reason},
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 201:
        raise _moderation_error(resp, "POST moderation/warnings")
    return resp.json()


async def remove_warning(
    guild_id: int, warning_id: int, moderator_id: int, user_id: int | None = None,
) -> None:
    """`moderator_id` (from the session) is recorded as who removed it; with
    `user_id` only a warning belonging to that user is removed."""
    params: dict = {"moderator_id": moderator_id}
    if user_id is not None:
        params["user_id"] = user_id
    async with _client() as client:
        try:
            resp = await client.delete(f"/guilds/{guild_id}/moderation/warnings/{warning_id}", params=params)
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise _moderation_error(resp, "DELETE moderation/warnings")


async def list_tickets(guild_id: int, status_filter: str) -> list[dict]:
    async with _client() as client:
        try:
            resp = await client.get(
                f"/guilds/{guild_id}/tickets", params={"status_filter": status_filter}
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for GET tickets", status_code=resp.status_code
        )
    return resp.json()


async def get_ticket(guild_id: int, ticket_id: int) -> dict:
    async with _client() as client:
        try:
            resp = await client.get(f"/guilds/{guild_id}/tickets/{ticket_id}")
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code == 404:
        raise InternalAPIError("Ticket not found", status_code=404)
    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for GET ticket", status_code=resp.status_code
        )
    return resp.json()


async def _ticket_action(guild_id: int, ticket_id: int, action: str, actor_user_id: int) -> dict:
    # Keeps the internal API's {"error", "message"} detail (ticket service
    # refusals such as "already closed") so the dashboard route can pass a
    # 4xx through instead of reporting a 502.
    return await _feature_request(
        "POST", f"/guilds/{guild_id}/tickets/{ticket_id}/{action}", json={"actor_user_id": actor_user_id}
    )


async def close_ticket(guild_id: int, ticket_id: int, actor_user_id: int) -> dict:
    return await _ticket_action(guild_id, ticket_id, "close", actor_user_id)


async def claim_ticket(guild_id: int, ticket_id: int, actor_user_id: int) -> dict:
    return await _ticket_action(guild_id, ticket_id, "claim", actor_user_id)


async def archive_ticket(guild_id: int, ticket_id: int, actor_user_id: int) -> dict:
    return await _ticket_action(guild_id, ticket_id, "archive", actor_user_id)


async def list_ticket_panels(guild_id: int) -> list[dict]:
    async with _client() as client:
        try:
            resp = await client.get(f"/guilds/{guild_id}/ticket-panels")
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for GET ticket-panels", status_code=resp.status_code
        )
    return resp.json()


# ---------------------------------------------------------------------------
# Unified mod-action history + kick/ban/unban/timeout/untimeout.
#
# Mirrors the warnings functions above: same _client()/error-wrapping shape,
# same "raise InternalAPIError with status_code, let the caller in
# server/routes/moderation.py translate it into an HTTPException" contract.
# ---------------------------------------------------------------------------

async def get_mod_history(guild_id: int, user_id: int) -> dict:
    async with _client() as client:
        try:
            resp = await client.get(f"/guilds/{guild_id}/moderation/history/{user_id}")
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise _moderation_error(resp, "moderation/history")
    return resp.json()


async def kick_member(guild_id: int, user_id: int, moderator_id: int, reason: str | None) -> dict:
    async with _client() as client:
        try:
            resp = await client.post(
                f"/guilds/{guild_id}/moderation/kick/{user_id}",
                json={"moderator_id": moderator_id, "reason": reason},
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 201:
        raise _moderation_error(resp, "POST moderation/kick")
    return resp.json()


async def ban_user(
    guild_id: int, user_id: int, moderator_id: int, reason: str | None, delete_message_days: int = 0,
) -> dict:
    async with _client() as client:
        try:
            resp = await client.post(
                f"/guilds/{guild_id}/moderation/ban",
                json={
                    "user_id": user_id,
                    "moderator_id": moderator_id,
                    "reason": reason,
                    "delete_message_days": delete_message_days,
                },
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 201:
        raise _moderation_error(resp, "POST moderation/ban")
    return resp.json()


async def unban_user(guild_id: int, user_id: int, moderator_id: int, reason: str | None) -> dict:
    async with _client() as client:
        try:
            resp = await client.post(
                f"/guilds/{guild_id}/moderation/unban",
                json={"user_id": user_id, "moderator_id": moderator_id, "reason": reason},
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 201:
        raise _moderation_error(resp, "POST moderation/unban")
    return resp.json()


async def timeout_member(
    guild_id: int, user_id: int, moderator_id: int, duration_minutes: int, reason: str | None,
) -> dict:
    async with _client() as client:
        try:
            resp = await client.post(
                f"/guilds/{guild_id}/moderation/timeout/{user_id}",
                json={"moderator_id": moderator_id, "duration_minutes": duration_minutes, "reason": reason},
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 201:
        raise _moderation_error(resp, "POST moderation/timeout")
    return resp.json()


async def search_members(guild_id: int, query: str, limit: int = 20) -> dict:
    async with _client() as client:
        try:
            resp = await client.get(
                f"/guilds/{guild_id}/moderation/members/search", params={"q": query, "limit": limit}
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for moderation/members/search", status_code=resp.status_code
        )
    return resp.json()


async def check_is_staff(guild_id: int, user_id: int) -> bool:
    """Wraps POST /guilds/{guild_id}/permissions/is-staff — the stricter
    is_staff() check (owner or DEFAULT_STAFF_ROLE_IDS role) used to gate the
    permission-grants management page. See server/permissions/guards.py's
    require_staff for why this is deliberately separate from
    check_permission/check_permissions_bulk above."""
    async with _client() as client:
        try:
            resp = await client.post(
                f"/guilds/{guild_id}/permissions/is-staff",
                json={"user_id": user_id},
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        # Keep the internal API's `detail` (a string): the Diagnostics /
        # supervisor bot-down fallback must tell "bot unreachable / not ready"
        # apart from "member not in the guild" (404 "Member ... not found").
        detail = None
        try:
            body = resp.json()
            detail = body.get("detail") if isinstance(body, dict) else None
        except ValueError:
            pass
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for permissions/is-staff",
            status_code=resp.status_code,
            detail=detail,
        )
    return bool(resp.json().get("is_staff", False))


# ---------------------------------------------------------------------------
# Permission-grants management (staff permission-grants dashboard page) —
# live role list + grant/revoke/list. Mirrors the error-wrapping shape used
# throughout this module.
# ---------------------------------------------------------------------------

async def list_guild_roles(guild_id: int) -> list[dict]:
    async with _client() as client:
        try:
            resp = await client.get(f"/guilds/{guild_id}/roles")
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for GET roles", status_code=resp.status_code
        )
    return resp.json().get("roles", [])


async def list_permission_grants(guild_id: int) -> dict:
    async with _client() as client:
        try:
            resp = await client.get(f"/guilds/{guild_id}/permission-grants")
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for GET permission-grants", status_code=resp.status_code
        )
    return resp.json()


async def grant_permission(guild_id: int, role_id: int, permission: str, granted_by: int) -> dict:
    async with _client() as client:
        try:
            resp = await client.post(
                f"/guilds/{guild_id}/permission-grants",
                json={"role_id": role_id, "permission": permission, "granted_by": granted_by},
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code == 400:
        # detail = {"error": "invalid_grant", "message": ..., "errors": [...]}
        raise InternalAPIError("Invalid grant request", status_code=400, detail=_permission_error_detail(resp))
    if resp.status_code != 201:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for POST permission-grants", status_code=resp.status_code
        )
    return resp.json()


async def revoke_permission(guild_id: int, role_id: int, permission: str, actor_id: int | None = None) -> dict:
    # actor_id = the session user; only used for the permission_revoke audit row.
    async with _client() as client:
        try:
            resp = await client.request(
                "DELETE",
                f"/guilds/{guild_id}/permission-grants",
                json={"role_id": role_id, "permission": permission, "actor_id": actor_id},
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for DELETE permission-grants", status_code=resp.status_code
        )
    return resp.json()


async def untimeout_member(guild_id: int, user_id: int, moderator_id: int, reason: str | None) -> dict:
    async with _client() as client:
        try:
            resp = await client.post(
                f"/guilds/{guild_id}/moderation/untimeout/{user_id}",
                json={"moderator_id": moderator_id, "reason": reason},
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 201:
        raise _moderation_error(resp, "POST moderation/untimeout")
    return resp.json()


# ---------------------------------------------------------------------------
# Reaction-role panel builder (self-roles dashboard page). Wraps
# internal_api/routes/reaction_roles.py, which itself wraps
# services.reaction_role_service.ReactionRoleService — the same service
# cogs/reaction_roles.py's /reactionrole slash commands use, so dashboard
# edits and Discord-command edits are indistinguishable in storage and both
# keep the live Discord panel message in sync (see that service's docstring).
#   GET    /guilds/{guild_id}/reaction-roles/panels                              -> [panel+entries, ...]
#   GET    /guilds/{guild_id}/reaction-roles/panels/{panel_id}                   -> panel+entries
#   POST   /guilds/{guild_id}/reaction-roles/panels     {channel_id, created_by, title, exclusive} -> {"panel_id"}
#   PATCH  /guilds/{guild_id}/reaction-roles/panels/{panel_id}  {title?, exclusive?} -> {"success"}
#   DELETE /guilds/{guild_id}/reaction-roles/panels/{panel_id}                   -> {"success"}
#   POST   /guilds/{guild_id}/reaction-roles/panels/{panel_id}/post              -> {"message_id"}
#   POST   /guilds/{guild_id}/reaction-roles/panels/{panel_id}/entries {emoji, role_id, label?, actor_id} -> {"entry_id"} (409 role_not_allowed)
#   GET    /guilds/{guild_id}/reaction-roles/roles?actor_id=                    -> {"roles": [... + blocked_reason]}
#   DELETE /guilds/{guild_id}/reaction-roles/panels/{panel_id}/entries/{entry_id} -> {"success"}
#   GET    /guilds/{guild_id}/channels                                          -> [{"id","name","position"}, ...]
# ---------------------------------------------------------------------------

async def list_reaction_role_panels(guild_id: int) -> list[dict]:
    async with _client() as client:
        try:
            resp = await client.get(f"/guilds/{guild_id}/reaction-roles/panels")
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for GET reaction-roles/panels", status_code=resp.status_code
        )
    return resp.json()


async def get_reaction_role_panel(guild_id: int, panel_id: int) -> dict:
    async with _client() as client:
        try:
            resp = await client.get(f"/guilds/{guild_id}/reaction-roles/panels/{panel_id}")
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code == 404:
        raise InternalAPIError("Panel not found", status_code=404)
    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for GET reaction-roles panel", status_code=resp.status_code
        )
    return resp.json()


async def create_reaction_role_panel(
    guild_id: int, channel_id: int, created_by: int, title: str, exclusive: bool,
) -> dict:
    async with _client() as client:
        try:
            resp = await client.post(
                f"/guilds/{guild_id}/reaction-roles/panels",
                json={
                    "channel_id": channel_id,
                    "created_by": created_by,
                    "title": title,
                    "exclusive": exclusive,
                },
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code == 400:
        raise InternalAPIError("Invalid panel request", status_code=400)
    if resp.status_code != 201:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for POST reaction-roles/panels", status_code=resp.status_code
        )
    return resp.json()


async def update_reaction_role_panel(
    guild_id: int, panel_id: int, title: str | None = None, exclusive: bool | None = None,
) -> dict:
    async with _client() as client:
        try:
            resp = await client.request(
                "PATCH",
                f"/guilds/{guild_id}/reaction-roles/panels/{panel_id}",
                json={"title": title, "exclusive": exclusive},
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code == 404:
        raise InternalAPIError("Panel not found", status_code=404)
    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for PATCH reaction-roles panel", status_code=resp.status_code
        )
    return resp.json()


async def delete_reaction_role_panel(guild_id: int, panel_id: int) -> None:
    async with _client() as client:
        try:
            resp = await client.delete(f"/guilds/{guild_id}/reaction-roles/panels/{panel_id}")
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code == 404:
        raise InternalAPIError("Panel not found", status_code=404)
    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for DELETE reaction-roles panel", status_code=resp.status_code
        )


async def post_reaction_role_panel(guild_id: int, panel_id: int) -> dict:
    """Triggers ReactionRoleService.post_panel — first post, or an explicit
    re-post (delete+resend) if the panel already has a live message."""
    async with _client() as client:
        try:
            resp = await client.post(f"/guilds/{guild_id}/reaction-roles/panels/{panel_id}/post")
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code == 404:
        raise InternalAPIError("Panel not found", status_code=404)
    if resp.status_code == 400:
        raise InternalAPIError("Cannot post panel", status_code=400, detail=_rr_detail(resp))
    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for POST reaction-roles panel post", status_code=resp.status_code
        )
    return resp.json()


def _rr_detail(resp: httpx.Response) -> object:
    """Parsed `detail` of a reaction-roles error body (dict with
    error/message for blocks), or None."""
    try:
        body = resp.json()
    except ValueError:
        return None
    return body.get("detail") if isinstance(body, dict) else None


async def add_reaction_role_entry(
    guild_id: int, panel_id: int, emoji: str, role_id: int, label: str | None, actor_id: int,
) -> dict:
    # actor_id = the session user; the bot runs the self-role escalation
    # check against them and answers 409 role_not_allowed / actor_not_found.
    async with _client() as client:
        try:
            resp = await client.post(
                f"/guilds/{guild_id}/reaction-roles/panels/{panel_id}/entries",
                json={"emoji": emoji, "role_id": role_id, "label": label, "actor_id": actor_id},
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code == 404:
        raise InternalAPIError("Panel not found", status_code=404)
    if resp.status_code in (400, 409, 422):
        raise InternalAPIError("Invalid entry request", status_code=resp.status_code, detail=_rr_detail(resp))
    if resp.status_code != 201:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for POST reaction-roles entry", status_code=resp.status_code
        )
    return resp.json()


async def list_self_role_candidates(guild_id: int, actor_id: int) -> list[dict]:
    """GET /guilds/{id}/reaction-roles/roles: the Self-roles role picker,
    each role with `blocked_reason` (null = this actor may add it)."""
    async with _client() as client:
        try:
            resp = await client.get(f"/guilds/{guild_id}/reaction-roles/roles", params={"actor_id": actor_id})
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for GET reaction-roles/roles", status_code=resp.status_code
        )
    return resp.json().get("roles", [])


async def remove_reaction_role_entry(guild_id: int, panel_id: int, entry_id: int) -> None:
    async with _client() as client:
        try:
            resp = await client.delete(
                f"/guilds/{guild_id}/reaction-roles/panels/{panel_id}/entries/{entry_id}"
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code == 404:
        raise InternalAPIError("Entry not found", status_code=404)
    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for DELETE reaction-roles entry", status_code=resp.status_code
        )


async def list_guild_channels(guild_id: int) -> list[dict]:
    async with _client() as client:
        try:
            resp = await client.get(f"/guilds/{guild_id}/channels")
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for GET channels", status_code=resp.status_code
        )
    return resp.json()


# ---------------------------------------------------------------------------
# Template CRUD (dashboard template editor). Wraps
# internal_api/routes/templates.py, which itself wraps
# services.template_service.TemplateService — the SAME service
# cogs/tickets.py's template-editing slash commands construct on demand, so
# dashboard edits and command edits share storage, version snapshots, and
# audit log. See that route file's module docstring for why routes are
# template_id-keyed here even though TemplateService itself is name-keyed
# (the route layer resolves id -> name via TemplateRepository.get_by_id).
#   GET    /guilds/{guild_id}/templates?category=          -> [template, ...] (no embeds)
#   GET    /guilds/{guild_id}/templates/{template_id}       -> template with nested embeds+fields
#   POST   /guilds/{guild_id}/templates  {name, created_by, description?, category?, content?} -> {"template_id"}
#   PATCH  /guilds/{guild_id}/templates/{template_id}  {actor_id, name?, description?, category?, content?} -> {"success"}
#   PUT    /guilds/{guild_id}/templates/{template_id}/embeds  {actor_id, embeds:[...]} -> full template
#   DELETE /guilds/{guild_id}/templates/{template_id}?actor_id= -> {"success"}
# ---------------------------------------------------------------------------

async def list_templates(guild_id: int, category: str | None = None) -> list[dict]:
    async with _client() as client:
        try:
            params = {"category": category} if category else {}
            resp = await client.get(f"/guilds/{guild_id}/templates", params=params)
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for GET templates", status_code=resp.status_code
        )
    return resp.json()


async def get_template(guild_id: int, template_id: int) -> dict:
    async with _client() as client:
        try:
            resp = await client.get(f"/guilds/{guild_id}/templates/{template_id}")
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code == 404:
        raise InternalAPIError("Template not found", status_code=404)
    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for GET template", status_code=resp.status_code
        )
    return resp.json()


async def create_template(
    guild_id: int, name: str, created_by: int,
    description: str | None = None, category: str | None = None, content: str | None = None,
) -> dict:
    async with _client() as client:
        try:
            resp = await client.post(
                f"/guilds/{guild_id}/templates",
                json={
                    "name": name, "created_by": created_by,
                    "description": description, "category": category, "content": content,
                },
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code == 400:
        raise InternalAPIError(str(resp.json().get("detail")), status_code=400)
    if resp.status_code != 201:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for POST templates", status_code=resp.status_code
        )
    return resp.json()


async def update_template(
    guild_id: int, template_id: int, actor_id: int,
    name: str | None = None, description: str | None = None,
    category: str | None = None, content: str | None = None,
) -> dict:
    body: dict = {"actor_id": actor_id}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if category is not None:
        body["category"] = category
    if content is not None:
        body["content"] = content

    async with _client() as client:
        try:
            resp = await client.request(
                "PATCH", f"/guilds/{guild_id}/templates/{template_id}", json=body,
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code == 404:
        raise InternalAPIError("Template not found", status_code=404)
    if resp.status_code == 400:
        raise InternalAPIError(str(resp.json().get("detail")), status_code=400)
    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for PATCH template", status_code=resp.status_code
        )
    return resp.json()


async def replace_template_embeds(guild_id: int, template_id: int, actor_id: int, embeds: list[dict]) -> dict:
    async with _client() as client:
        try:
            resp = await client.put(
                f"/guilds/{guild_id}/templates/{template_id}/embeds",
                json={"actor_id": actor_id, "embeds": embeds},
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code == 404:
        raise InternalAPIError("Template not found", status_code=404)
    if resp.status_code == 400:
        raise InternalAPIError(str(resp.json().get("detail")), status_code=400)
    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for PUT template embeds", status_code=resp.status_code
        )
    return resp.json()


async def post_template(guild_id: int, template_id: int, channel_id: int, actor_id: int) -> dict:
    async with _client() as client:
        try:
            resp = await client.post(
                f"/guilds/{guild_id}/templates/{template_id}/post",
                json={"channel_id": channel_id, "actor_id": actor_id},
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code == 404:
        raise InternalAPIError("Template not found", status_code=404)
    if resp.status_code == 400:
        raise InternalAPIError(str(resp.json().get("detail")), status_code=400)
    if resp.status_code != 201:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for POST template post", status_code=resp.status_code
        )
    return resp.json()


async def delete_template(guild_id: int, template_id: int, actor_id: int) -> None:
    async with _client() as client:
        try:
            resp = await client.delete(
                f"/guilds/{guild_id}/templates/{template_id}", params={"actor_id": actor_id},
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code == 404:
        raise InternalAPIError("Template not found", status_code=404)
    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for DELETE template", status_code=resp.status_code
        )


# ---------------------------------------------------------------------------
# Ticket panel BUILDER (dashboard panel-creation form). Wraps
# internal_api/routes/ticket_panels.py's POST /ticket-panels, which calls
# the real TicketService.create_panel then TicketService.post_panel so a
# dashboard-created panel actually posts a live Discord message — same two
# calls cogs/tickets.py's `/ticket setup` slash command makes. See that
# route file's docstring for why post_panel failures don't roll back the
# created DB row (post_errors surfaces them instead).
#   POST /guilds/{guild_id}/ticket-panels {channel_id, created_by, template_name?,
#        open_category_id?, closed_category_id?, transcript_channel_id?,
#        max_open_per_user, naming_pattern, button_label, button_emoji?, purpose}
#        -> {"panel_id", "message_id", "post_errors", "panel"}
#   GET  /guilds/{guild_id}/categories -> [{"id","name","position"}, ...]
# ---------------------------------------------------------------------------

async def create_ticket_panel(guild_id: int, **kwargs) -> dict:
    async with _client() as client:
        try:
            resp = await client.post(f"/guilds/{guild_id}/ticket-panels", json=kwargs)
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code == 400:
        detail = resp.json().get("detail")
        # create_panel returns a list of problems; the channel checks a string.
        message = "; ".join(str(d) for d in detail) if isinstance(detail, list) else str(detail)
        raise InternalAPIError(message, status_code=400)
    if resp.status_code != 201:
        raise _moderation_error(resp, "POST ticket-panels")
    return resp.json()


async def list_guild_categories(guild_id: int) -> list[dict]:
    async with _client() as client:
        try:
            resp = await client.get(f"/guilds/{guild_id}/categories")
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for GET categories", status_code=resp.status_code
        )
    return resp.json()


# ---------------------------------------------------------------------------
# Archived-ticket deletion + chat-log retrieval. Wraps the routes appended to
# internal_api/routes/tickets.py for the claim-permission-fix / auto-purge /
# chat-log task. Same error-wrapping shape as the ticket functions above.
#   DELETE /guilds/{guild_id}/tickets/{ticket_id}                  -> {"success": bool}
#   GET    /guilds/{guild_id}/tickets/{ticket_id}/messages         -> {"enabled": bool, "messages": [...]}
# ---------------------------------------------------------------------------

async def delete_ticket(guild_id: int, ticket_id: int, actor_user_id: int | None = None) -> dict:
    params = {"actor_user_id": actor_user_id} if actor_user_id is not None else None
    return await _feature_request("DELETE", f"/guilds/{guild_id}/tickets/{ticket_id}", params=params)


async def get_ticket_messages(guild_id: int, ticket_id: int) -> dict:
    async with _client() as client:
        try:
            resp = await client.get(f"/guilds/{guild_id}/tickets/{ticket_id}/messages")
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code == 404:
        raise InternalAPIError("Ticket not found", status_code=404)
    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for GET ticket messages", status_code=resp.status_code
        )
    return resp.json()


# ---------------------------------------------------------------------------
# QOTD management (dashboard QOTD page). Wraps internal_api/routes/qotd.py,
# which itself wraps services.qotd_service.QotdService + database.
# repositories.qotd_repo.QotdRepository via the live QotdCog instance — the
# SAME instances cogs/qotd.py's `/qotd` slash-command group uses, so
# dashboard-issued generate/post actions and Discord-command-issued ones are
# indistinguishable in storage. QOTD already has a fully automatic daily
# scheduler (cogs/qotd.py's `_scheduler` tasks.loop, driven by the
# qotd_config table) — these functions expose that existing config for
# editing rather than introducing a second scheduling mechanism.
#   GET  /guilds/{guild_id}/qotd/config                -> {"config", "last_posted_at", "pending_approval"}
#   PUT  /guilds/{guild_id}/qotd/config      {...fields...} -> {"config"}
#   GET  /guilds/{guild_id}/qotd/history?limit=N        -> {"history": [...]}
#   GET  /guilds/{guild_id}/qotd/models                 -> {"models": [...]}
#   GET  /guilds/{guild_id}/qotd/fallback-questions     -> {"questions": [...]}
#   POST /guilds/{guild_id}/qotd/generate    {"model"?}  -> 202 {"job_id", "status", ...}
#   GET  /guilds/{guild_id}/qotd/generate/{job_id}       -> {"status", "question", "source", "errors", "error"}
#   POST /guilds/{guild_id}/qotd/post  {question, source, channel_id, use_emojis, ping_role_id?, actor_user_id?} -> {"message_id", "channel_id"}
# ---------------------------------------------------------------------------

async def get_qotd_config(guild_id: int) -> dict:
    async with _client() as client:
        try:
            resp = await client.get(f"/guilds/{guild_id}/qotd/config")
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for GET qotd/config", status_code=resp.status_code
        )
    return resp.json()


async def upsert_qotd_config(guild_id: int, **fields) -> dict:
    async with _client() as client:
        try:
            resp = await client.put(f"/guilds/{guild_id}/qotd/config", json=fields)
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for PUT qotd/config", status_code=resp.status_code
        )
    return resp.json()


async def get_qotd_history(guild_id: int, limit: int = 50) -> dict:
    async with _client() as client:
        try:
            resp = await client.get(f"/guilds/{guild_id}/qotd/history", params={"limit": limit})
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for GET qotd/history", status_code=resp.status_code
        )
    return resp.json()


async def get_qotd_models(guild_id: int) -> dict:
    async with _client() as client:
        try:
            resp = await client.get(f"/guilds/{guild_id}/qotd/models")
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for GET qotd/models", status_code=resp.status_code
        )
    return resp.json()


async def get_qotd_fallback_questions(guild_id: int) -> dict:
    async with _client() as client:
        try:
            resp = await client.get(f"/guilds/{guild_id}/qotd/fallback-questions")
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for GET qotd/fallback-questions", status_code=resp.status_code
        )
    return resp.json()


async def start_qotd_generation(guild_id: int, model: str | None = None) -> dict:
    """Starts a QOTD generation job on the bot (202, returns at once); poll
    get_qotd_generation. Replaces the old blocking generate call, which could
    outlive Cloudflare's 100 s limit."""
    async with _client() as client:
        try:
            resp = await client.post(f"/guilds/{guild_id}/qotd/generate", json={"model": model})
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code not in (200, 202):
        detail = None
        try:
            body = resp.json()
            detail = body.get("detail") if isinstance(body, dict) else None
        except ValueError:
            pass
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for POST qotd/generate", status_code=resp.status_code, detail=detail
        )
    return resp.json()


async def get_qotd_generation(guild_id: int, job_id: str) -> dict:
    return await _feature_request("GET", f"/guilds/{guild_id}/qotd/generate/{job_id}")


async def post_qotd(
    guild_id: int,
    *,
    question: str,
    source: str,
    channel_id: int,
    use_emojis: bool = False,
    ping_role_id: int | None = None,
    actor_user_id: int | None = None,
) -> dict:
    # _feature_request keeps the bot's {"error", "message"} detail so the
    # dashboard can show why Discord refused the post.
    return await _feature_request(
        "POST",
        f"/guilds/{guild_id}/qotd/post",
        json={
            "question": question,
            "source": source,
            "channel_id": channel_id,
            "use_emojis": use_emojis,
            "ping_role_id": ping_role_id,
            "actor_user_id": actor_user_id,
        },
    )


# ---------------------------------------------------------------------------
# Dashboard-editable operational settings (admin-only Settings page). Wraps
# internal_api/routes/settings.py, which wraps services.settings_service.
# SettingsService — see that module's docstring for the field allowlist
# (SETTINGS_SCHEMA) and the live-reload-vs-restart-required design.
#   GET /guilds/{guild_id}/settings                    -> report (values, schema, fields, options, ...)
#   PUT /guilds/{guild_id}/settings  {updated_by, values: {...}} -> report + changed
# Non-200 -> InternalAPIError(status, detail) with the bot's {"error", "message",
# "fields"?} dict, which server/routes/settings.py passes through (422 etc.).
# ---------------------------------------------------------------------------

async def _settings_request(method: str, guild_id: int, json: dict | None = None) -> dict:
    async with _client() as client:
        try:
            resp = await client.request(method, f"/guilds/{guild_id}/settings", json=json)
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        detail = None
        try:
            body = resp.json()
            detail = body.get("detail") if isinstance(body, dict) else None
        except ValueError:
            pass
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for {method} settings",
            status_code=resp.status_code,
            detail=detail,
        )
    return resp.json()


async def get_settings(guild_id: int) -> dict:
    return await _settings_request("GET", guild_id)


async def update_settings(guild_id: int, values: dict, updated_by: int) -> dict:
    return await _settings_request("PUT", guild_id, json={"updated_by": updated_by, "values": values})


# ---------------------------------------------------------------------------
# Batch ID -> display-metadata resolver (used by nearly every dashboard page
# to turn raw snowflakes into names/avatars/colors). Wraps
# internal_api/routes/resolve.py's POST /guilds/{guild_id}/resolve — see that
# module's docstring for the cache/query_members/fetch_member fallback chain
# and why a user who left the guild resolves to null rather than 404ing the
# whole batch.
#   POST /guilds/{guild_id}/resolve {user_ids, role_ids, channel_ids} ->
#        {"users": {id: {...}|null}, "roles": {...}, "channels": {...}}
# ---------------------------------------------------------------------------

async def resolve_ids(
    guild_id: int, user_ids: list[int], role_ids: list[int], channel_ids: list[int],
) -> dict:
    async with _client() as client:
        try:
            resp = await client.post(
                f"/guilds/{guild_id}/resolve",
                json={"user_ids": user_ids, "role_ids": role_ids, "channel_ids": channel_ids},
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code == 400:
        raise InternalAPIError("Invalid resolve request", status_code=400)
    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for POST resolve", status_code=resp.status_code
        )
    return resp.json()


# User cache (departed users' remembered profiles, internal_api/routes/resolve.py):
#   GET    /guilds/{guild_id}/user-cache/{user_id}             -> {"user": {...}, "in_guild": bool}
#   DELETE /guilds/{guild_id}/user-cache/{user_id}?actor_id=   -> {"ok", "removed", "in_guild"}
# Non-200 -> InternalAPIError(status_code, detail) (404 not_cached, 503 user_cache_unavailable).

async def _user_cache_request(method: str, guild_id: int, user_id: int, params: dict | None = None) -> dict:
    async with _client() as client:
        try:
            resp = await client.request(method, f"/guilds/{guild_id}/user-cache/{user_id}", params=params)
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = None
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for {method} user-cache",
            status_code=resp.status_code, detail=detail,
        )
    return resp.json()


async def get_cached_user(guild_id: int, user_id: int) -> dict:
    return await _user_cache_request("GET", guild_id, user_id)


async def forget_cached_user(guild_id: int, user_id: int, actor_id: int) -> dict:
    return await _user_cache_request("DELETE", guild_id, user_id, params={"actor_id": actor_id})


# ---------------------------------------------------------------------------
# Honeypot + ban appeals. Wraps internal_api/routes/honeypot.py (contract:
# docs/honeypot_contract.md). One shared helper because every route follows
# the same "200 -> json, anything else -> InternalAPIError(status, detail)"
# shape; callers translate status codes (404/409/400/...) themselves.
# ---------------------------------------------------------------------------

async def _honeypot_request(
    method: str, guild_id: int, path: str, *, params: dict | None = None, json: dict | None = None
) -> dict:
    async with _client() as client:
        try:
            resp = await client.request(method, f"/guilds/{guild_id}/honeypot{path}", params=params, json=json)
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code not in (200, 201):
        detail = None
        try:
            body = resp.json()
            detail = body.get("detail") if isinstance(body, dict) else None
        except ValueError:
            pass
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for {method} honeypot{path.split('?')[0]}",
            status_code=resp.status_code,
            detail=detail,
        )
    return resp.json()


async def get_honeypot_config(guild_id: int) -> dict:
    return await _honeypot_request("GET", guild_id, "/config")


async def update_honeypot_config(guild_id: int, values: dict) -> dict:
    return await _honeypot_request("PUT", guild_id, "/config", json=values)


async def setup_honeypot(guild_id: int, actor_id: int, values: dict) -> dict:
    return await _honeypot_request("POST", guild_id, "/setup", json={"actor_id": actor_id, **values})


async def scramble_honeypot_names(guild_id: int, actor_id: int) -> dict:
    # The bot bounds the rename to SCRAMBLE_MANUAL_TIMEOUT (8 s) so this fits in _TIMEOUT.
    return await _honeypot_request("POST", guild_id, "/scramble", json={"actor_id": actor_id})


async def get_honeypot_catches(guild_id: int, limit: int = 50, before_id: int | None = None) -> dict:
    params: dict = {"limit": limit}
    if before_id is not None:
        params["before_id"] = before_id
    return await _honeypot_request("GET", guild_id, "/catches", params=params)


async def get_honeypot_stats(guild_id: int) -> dict:
    return await _honeypot_request("GET", guild_id, "/stats")


async def get_honeypot_appeals(guild_id: int, status: str = "all") -> dict:
    return await _honeypot_request("GET", guild_id, "/appeals", params={"status": status})


async def approve_honeypot_appeal(guild_id: int, appeal_id: int, reviewer_id: int, note: str | None) -> dict:
    return await _honeypot_request(
        "POST", guild_id, f"/appeals/{appeal_id}/approve", json={"reviewer_id": reviewer_id, "note": note}
    )


async def deny_honeypot_appeal(guild_id: int, appeal_id: int, reviewer_id: int, note: str | None) -> dict:
    return await _honeypot_request(
        "POST", guild_id, f"/appeals/{appeal_id}/deny", json={"reviewer_id": reviewer_id, "note": note}
    )


async def lookup_appeal_token(guild_id: int, token: str) -> dict:
    # token is percent-encoded so a hostile value can't alter the path.
    from urllib.parse import quote

    return await _honeypot_request("GET", guild_id, f"/appeals/lookup/{quote(token, safe='')}")


# Appeal sign-in (server/routes/appeal_auth.py): only ever called with the user id that
# Discord login just proved, never with a typed one.
async def get_appeal_status_for_user(guild_id: int, user_id: int) -> dict:
    return await _honeypot_request("GET", guild_id, f"/appeals/for-user/{int(user_id)}")


async def submit_verified_appeal(guild_id: int, user_id: int, username: str | None, name: str, message: str) -> dict:
    return await _honeypot_request(
        "POST", guild_id, "/appeals/submit-verified",
        json={"user_id": user_id, "username": username, "name": name, "message": message},
    )


async def rejoin_after_appeal(guild_id: int, user_id: int, access_token: str) -> dict:
    """Hands the person's guilds.join token to the bot for ONE add-member call (never stored)."""
    return await _honeypot_request(
        "POST", guild_id, "/appeals/rejoin", json={"user_id": user_id, "access_token": access_token},
    )


async def create_honeypot_rejoin_invite(guild_id: int, actor_id: int, channel_id: int) -> dict:
    return await _honeypot_request(
        "POST", guild_id, "/invite", json={"actor_id": actor_id, "channel_id": channel_id},
    )


async def get_diagnostics(guild_id: int) -> dict:
    """GET /guilds/{guild_id}/diagnostics -> the bot process's full diagnostics
    dump (staff-only page; the caller gates on require_staff). Longer timeout
    than the default: the first call after a cache expiry runs the heavy
    (DB/GC/socket) collectors."""
    async with httpx.AsyncClient(
        base_url=config.INTERNAL_API_BASE_URL, headers=_HEADERS, timeout=httpx.Timeout(20.0, connect=5.0)
    ) as client:
        try:
            resp = await client.get(f"/guilds/{guild_id}/diagnostics")
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for GET diagnostics", status_code=resp.status_code
        )
    return resp.json()


async def clear_diagnostics_cache(guild_id: int, requested_by: str) -> dict:
    """POST /guilds/{guild_id}/diagnostics/cache/clear -> empties the bot's
    diagnostics caches; returns {"cleared": [...], "entries", "log_cache_cleared", "at"}.
    Any failure (bot down included) -> InternalAPIError."""
    async with httpx.AsyncClient(base_url=config.INTERNAL_API_BASE_URL, headers=_HEADERS, timeout=_TIMEOUT) as client:
        try:
            resp = await client.post(
                f"/guilds/{guild_id}/diagnostics/cache/clear", json={"requested_by": requested_by}
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for POST diagnostics/cache/clear", status_code=resp.status_code
        )
    return resp.json()


# ---------------------------------------------------------------------------
# Role management (dashboard Roles page). Wraps
# internal_api/routes/role_admin.py (/guilds/{guild_id}/role-admin/roles...).
# Same shared-helper shape as _honeypot_request: 200/201 -> json, anything
# else -> InternalAPIError(status, detail) where detail is the internal
# {"error", "message", ...} dict; server/routes/role_admin.py translates it.
# ---------------------------------------------------------------------------

async def _role_admin_request(
    method: str, guild_id: int, path: str = "", *, params: dict | None = None, json: dict | None = None
) -> dict:
    async with _client() as client:
        try:
            resp = await client.request(
                method, f"/guilds/{guild_id}/role-admin/roles{path}", params=params, json=json
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc

    if resp.status_code not in (200, 201):
        detail = None
        try:
            body = resp.json()
            detail = body.get("detail") if isinstance(body, dict) else None
        except ValueError:
            pass
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for {method} role-admin{path}",
            status_code=resp.status_code,
            detail=detail,
        )
    return resp.json()


async def get_role_admin_overview(guild_id: int, actor_id: int | None = None) -> dict:
    # actor_id = the session user: the bot then applies their role hierarchy
    # to each role's `manageable` flag and returns an `actor` block.
    params = {"actor_id": actor_id} if actor_id is not None else None
    return await _role_admin_request("GET", guild_id, params=params)


async def create_role_admin(guild_id: int, payload: dict) -> dict:
    return await _role_admin_request("POST", guild_id, json=payload)


async def update_role_admin(guild_id: int, role_id: int, payload: dict) -> dict:
    return await _role_admin_request("PATCH", guild_id, f"/{role_id}", json=payload)


async def move_role_admin(guild_id: int, role_id: int, payload: dict) -> dict:
    return await _role_admin_request("POST", guild_id, f"/{role_id}/move", json=payload)


async def duplicate_role_admin(guild_id: int, role_id: int, payload: dict) -> dict:
    return await _role_admin_request("POST", guild_id, f"/{role_id}/duplicate", json=payload)


async def delete_role_admin(guild_id: int, role_id: int, payload: dict) -> dict:
    return await _role_admin_request("DELETE", guild_id, f"/{role_id}", json=payload)


async def list_role_admin_members(guild_id: int, role_id: int, params: dict) -> dict:
    return await _role_admin_request("GET", guild_id, f"/{role_id}/members", params=params)


async def add_role_admin_member(guild_id: int, role_id: int, user_id: int, payload: dict) -> dict:
    return await _role_admin_request("POST", guild_id, f"/{role_id}/members/{user_id}", json=payload)


async def remove_role_admin_member(guild_id: int, role_id: int, user_id: int, payload: dict) -> dict:
    return await _role_admin_request("DELETE", guild_id, f"/{role_id}/members/{user_id}", json=payload)


# ---------------------------------------------------------------------------
# Fortnite item shop + owner-only Debug page. Wraps internal_api/routes/
# fortnite.py and internal_api/routes/debug.py. Same "200 -> json, else
# InternalAPIError(status, detail)" shape as _honeypot_request.
#   GET  /guilds/{guild_id}/fortnite/status                 -> {"config", "next_post_at", "cache", "wishlist", ...}
#   PUT  /guilds/{guild_id}/fortnite/config   {partial}     -> same as status
#   POST /guilds/{guild_id}/fortnite/post-now               -> {"shop_date", "jump_url", ...}
#   GET  /guilds/{guild_id}/debug/actions                   -> {"actions": [...], "skipped": [...]}
#   POST /guilds/{guild_id}/debug/run  {action, channel_id, owner_id, params} -> result
#   GET  /guilds/{guild_id}/debug/fortnite/preview?refresh= -> parsed shop
#   GET  /guilds/{guild_id}/debug/health                    -> latency/uptime/cogs/loops
# Posting to Discord / fetching the shop can take a while. Manual posts fetch
# with a single 30s attempt (MANUAL_FETCH_ATTEMPTS) but may wait on the shop
# lock behind the daily loop's ~97s retry run, so 150s keeps this timeout above
# the bot's worst case: the dashboard never reports "failed" for a post that
# then goes out anyway (which invited a duplicate second click).
# ---------------------------------------------------------------------------

_SLOW_TIMEOUT = httpx.Timeout(150.0, connect=5.0)


async def _feature_request(
    method: str, path: str, *, params: dict | None = None, json: dict | None = None, slow: bool = False
) -> dict:
    async with _client() as client:
        try:
            resp = await client.request(
                method, path, params=params, json=json, timeout=_SLOW_TIMEOUT if slow else _TIMEOUT
            )
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc
    if resp.status_code != 200:
        detail = None
        try:
            body = resp.json()
            detail = body.get("detail") if isinstance(body, dict) else None
        except ValueError:
            pass
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for {method} {path}", status_code=resp.status_code, detail=detail
        )
    return resp.json()


async def get_fortnite_status(guild_id: int) -> dict:
    return await _feature_request("GET", f"/guilds/{guild_id}/fortnite/status")


async def update_fortnite_config(guild_id: int, values: dict) -> dict:
    return await _feature_request("PUT", f"/guilds/{guild_id}/fortnite/config", json=values)


async def fortnite_post_now(guild_id: int) -> dict:
    return await _feature_request("POST", f"/guilds/{guild_id}/fortnite/post-now", slow=True)


# Audit log page (internal_api/routes/audit.py). `params` holds only the
# filters that are set; None values are dropped here.
async def get_audit_log(guild_id: int, params: dict) -> dict:
    clean = {k: v for k, v in params.items() if v is not None}
    return await _feature_request("GET", f"/guilds/{guild_id}/audit", params=clean)


async def get_audit_actions(guild_id: int) -> dict:
    return await _feature_request("GET", f"/guilds/{guild_id}/audit/actions")


async def get_debug_actions(guild_id: int) -> dict:
    return await _feature_request("GET", f"/guilds/{guild_id}/debug/actions")


async def run_debug_action(guild_id: int, *, action: str, channel_id: int, owner_id: int, params: dict) -> dict:
    return await _feature_request(
        "POST",
        f"/guilds/{guild_id}/debug/run",
        json={"action": action, "channel_id": channel_id, "owner_id": owner_id, "params": params},
        slow=True,
    )


async def get_debug_fortnite_preview(guild_id: int, refresh: bool = False) -> dict:
    return await _feature_request(
        "GET", f"/guilds/{guild_id}/debug/fortnite/preview", params={"refresh": str(refresh).lower()}, slow=True
    )


async def get_debug_health(guild_id: int) -> dict:
    return await _feature_request("GET", f"/guilds/{guild_id}/debug/health")


# Honeypot appeal-flow test cases (Debug page, migration 031).
async def list_appeal_test_cases(guild_id: int) -> dict:
    return await _feature_request("GET", f"/guilds/{guild_id}/debug/appeal-test")


async def create_appeal_test_case(guild_id: int, *, owner_id: int, user_id: int, state: str) -> dict:
    return await _feature_request(
        "POST", f"/guilds/{guild_id}/debug/appeal-test",
        json={"owner_id": owner_id, "user_id": user_id, "state": state},
    )


async def clear_appeal_test_cases(guild_id: int, *, owner_id: int) -> dict:
    return await _feature_request(
        "POST", f"/guilds/{guild_id}/debug/appeal-test/clear", json={"owner_id": owner_id},
    )


# Debug page "Lab" tab (internal_api/routes/debug.py lab routes,
# services/debug_lab_service.py). owner_id always comes from the session.
async def get_debug_lab_options(guild_id: int) -> dict:
    return await _feature_request("GET", f"/guilds/{guild_id}/debug/lab/options")


async def debug_lab_permissions(guild_id: int, *, owner_id: int, user_id: int | None, role_ids: list[int] | None) -> dict:
    return await _feature_request(
        "POST", f"/guilds/{guild_id}/debug/lab/permissions",
        json={"owner_id": owner_id, "user_id": user_id, "role_ids": role_ids},
    )


async def debug_lab_preview(guild_id: int, *, owner_id: int, payload: dict) -> dict:
    return await _feature_request(
        "POST", f"/guilds/{guild_id}/debug/lab/preview", json={**payload, "owner_id": owner_id}, slow=True,
    )


async def debug_lab_antispam(guild_id: int, *, owner_id: int, overrides: dict, messages: list[dict]) -> dict:
    return await _feature_request(
        "POST", f"/guilds/{guild_id}/debug/lab/antispam",
        json={"owner_id": owner_id, "overrides": overrides, "messages": messages},
    )


# Debug > Errors: the bot's in-memory error ring (core/error_ring.py).
async def get_debug_errors(guild_id: int) -> dict:
    return await _feature_request("GET", f"/guilds/{guild_id}/debug/errors")


async def clear_debug_errors(guild_id: int, *, fingerprint: str | None = None) -> dict:
    return await _feature_request("POST", f"/guilds/{guild_id}/debug/errors/clear", json={"fingerprint": fingerprint})


async def probe_get(path: str, *, timeout_s: float = 10.0) -> dict:
    """Time one read-only GET against the internal API (Debug Lab latency
    matrix). Never raises: {"status": int|None, "ms": float, "bytes": int,
    "error": str|None}. The response body is discarded (only its size kept)."""
    import time

    started = time.perf_counter()
    try:
        async with _client() as client:
            resp = await client.get(path, timeout=httpx.Timeout(timeout_s, connect=5.0))
        return {"status": resp.status_code, "ms": round((time.perf_counter() - started) * 1000, 1),
                "bytes": len(resp.content), "error": None}
    except httpx.RequestError as exc:
        return {"status": None, "ms": round((time.perf_counter() - started) * 1000, 1), "bytes": 0,
                "error": type(exc).__name__}


# Member profile / verify / purge job / message-log DM export / server info
# (internal_api/routes/member_tools.py). Errors keep the bot's
# {"error", "message"} detail so the dashboard routes can pass it through.
async def get_member_profile(guild_id: int, user_id: int, *, actor_id: int, include_honeypot: bool) -> dict:
    return await _feature_request(
        "GET", f"/guilds/{guild_id}/members/{user_id}/profile",
        params={"actor_id": actor_id, "include_honeypot": str(include_honeypot).lower()},
    )


async def get_verify_channels(guild_id: int) -> dict:
    return await _feature_request("GET", f"/guilds/{guild_id}/members/verify-channels")


async def verify_member(guild_id: int, user_id: int, *, actor_id: int, channel_id: int) -> dict:
    return await _feature_request(
        "POST", f"/guilds/{guild_id}/members/{user_id}/verify",
        json={"actor_id": actor_id, "channel_id": channel_id}, slow=True,
    )


async def get_purge_channels(guild_id: int, *, actor_id: int) -> dict:
    return await _feature_request("GET", f"/guilds/{guild_id}/moderation/purge-channels", params={"actor_id": actor_id})


async def start_purge(
    guild_id: int, *, moderator_id: int, channel_id: int, amount: str, user_id: int | None, confirm: bool
) -> dict:
    return await _feature_request(
        "POST", f"/guilds/{guild_id}/moderation/purge",
        json={"moderator_id": moderator_id, "channel_id": channel_id, "amount": amount,
              "user_id": user_id, "confirm": confirm},
    )


async def get_purge_job(guild_id: int, job_id: str) -> dict:
    return await _feature_request("GET", f"/guilds/{guild_id}/moderation/purge/{job_id}")


async def get_message_log_status(guild_id: int) -> dict:
    return await _feature_request("GET", f"/guilds/{guild_id}/message-log/status")


async def export_message_log(guild_id: int, *, actor_id: int, kind: str, user_id: int | None,
                             channel_id: int | None, window: str | None) -> dict:
    return await _feature_request(
        "POST", f"/guilds/{guild_id}/message-log/export",
        json={"actor_id": actor_id, "kind": kind, "user_id": user_id, "channel_id": channel_id, "window": window},
        slow=True,
    )


async def get_server_info(guild_id: int) -> dict:
    return await _feature_request("GET", f"/guilds/{guild_id}/server-info")


# ---------------------------------------------------------------------------
# Dashboard Automation page (internal_api/routes/automation.py) and the
# Templates page's history / rollback / export / import / bot preview
# (internal_api/routes/templates.py). Same "200 -> json, else
# InternalAPIError(status, detail)" shape as _feature_request; detail is the
# bot's {"error", "message"} dict (or a list of messages for templates).
# Event/greeting tests and managed sync post to Discord, so they use the slow
# timeout.
# ---------------------------------------------------------------------------

def _automation(guild_id: int, path: str = "") -> str:
    return f"/guilds/{guild_id}/automation{path}"


async def get_automation_options(guild_id: int) -> dict:
    return await _feature_request("GET", _automation(guild_id, "/options"))


async def list_event_bindings(guild_id: int) -> dict:
    return await _feature_request("GET", _automation(guild_id, "/events"))


async def set_event_binding(guild_id: int, event_type: str, payload: dict) -> dict:
    return await _feature_request("PUT", _automation(guild_id, f"/events/{event_type}"), json=payload)


async def clear_event_binding(guild_id: int, event_type: str, actor_id: int) -> dict:
    return await _feature_request("DELETE", _automation(guild_id, f"/events/{event_type}"), params={"actor_id": actor_id})


async def test_event_binding(guild_id: int, event_type: str, actor_id: int) -> dict:
    return await _feature_request(
        "POST", _automation(guild_id, f"/events/{event_type}/test"), json={"actor_id": actor_id}, slow=True
    )


async def get_greetings(guild_id: int) -> dict:
    return await _feature_request("GET", _automation(guild_id, "/greetings"))


async def update_greeting(guild_id: int, kind: str, payload: dict) -> dict:
    return await _feature_request("PUT", _automation(guild_id, f"/greetings/{kind}"), json=payload)


async def test_greeting(guild_id: int, kind: str, actor_id: int) -> dict:
    return await _feature_request(
        "POST", _automation(guild_id, f"/greetings/{kind}/test"), json={"actor_id": actor_id}, slow=True
    )


async def list_schedules(guild_id: int) -> dict:
    return await _feature_request("GET", _automation(guild_id, "/schedules"))


async def preview_schedule_cron(guild_id: int, cron: str) -> dict:
    return await _feature_request("POST", _automation(guild_id, "/schedules/preview"), json={"cron": cron})


async def create_schedule(guild_id: int, payload: dict) -> dict:
    return await _feature_request("POST", _automation(guild_id, "/schedules"), json=payload)


async def toggle_schedule(guild_id: int, schedule_id: int, enabled: bool, actor_id: int) -> dict:
    return await _feature_request(
        "PATCH", _automation(guild_id, f"/schedules/{schedule_id}"), json={"enabled": enabled, "actor_id": actor_id}
    )


async def remove_schedule(guild_id: int, schedule_id: int, actor_id: int) -> dict:
    return await _feature_request("DELETE", _automation(guild_id, f"/schedules/{schedule_id}"), params={"actor_id": actor_id})


async def list_autoroles(guild_id: int) -> dict:
    return await _feature_request("GET", _automation(guild_id, "/autoroles"))


async def list_autorole_roles(guild_id: int, actor_id: int) -> dict:
    return await _feature_request("GET", _automation(guild_id, "/autoroles/roles"), params={"actor_id": actor_id})


async def create_autorole(guild_id: int, payload: dict) -> dict:
    return await _feature_request("POST", _automation(guild_id, "/autoroles"), json=payload)


async def remove_autorole(guild_id: int, rule_id: int, actor_id: int) -> dict:
    return await _feature_request("DELETE", _automation(guild_id, f"/autoroles/{rule_id}"), params={"actor_id": actor_id})


async def list_managed_messages(guild_id: int) -> dict:
    return await _feature_request("GET", _automation(guild_id, "/managed"))


async def untrack_managed_message(guild_id: int, message_id: int, actor_id: int) -> dict:
    return await _feature_request("DELETE", _automation(guild_id, f"/managed/{message_id}"), params={"actor_id": actor_id})


async def sync_managed_messages(guild_id: int, template_id: int, actor_id: int) -> dict:
    return await _feature_request(
        "POST", _automation(guild_id, "/managed/sync"), json={"template_id": template_id, "actor_id": actor_id}, slow=True
    )


async def list_template_versions(guild_id: int, template_id: int) -> dict:
    return await _feature_request("GET", f"/guilds/{guild_id}/templates/{template_id}/versions")


async def rollback_template(guild_id: int, template_id: int, version_number: int, actor_id: int) -> dict:
    return await _feature_request(
        "POST", f"/guilds/{guild_id}/templates/{template_id}/rollback",
        json={"version_number": version_number, "actor_id": actor_id},
    )


async def export_template(guild_id: int, template_id: int) -> dict:
    return await _feature_request("GET", f"/guilds/{guild_id}/templates/{template_id}/export")


async def import_template(guild_id: int, data: dict, name_override: str | None, actor_id: int) -> dict:
    return await _feature_request(
        "POST", f"/guilds/{guild_id}/templates/import",
        json={"data": data, "name_override": name_override, "actor_id": actor_id},
    )


async def preview_template(guild_id: int, template_id: int, *, user_id: int | None = None) -> dict:
    params = {"user_id": user_id} if user_id else None
    return await _feature_request("GET", f"/guilds/{guild_id}/templates/{template_id}/preview", params=params)


async def list_placeholders(guild_id: int, *, user_id: int | None = None) -> dict:
    params = {"user_id": user_id} if user_id else None
    return await _feature_request("GET", f"/guilds/{guild_id}/placeholders", params=params)


# ---------------------------------------------------------------------------
# Birthdays page (internal_api/routes/birthdays.py). Same "200 -> json, else
# InternalAPIError(status, detail)" shape as _feature_request.
#   GET    /guilds/{gid}/birthdays                         -> {"settings", "status", "entries", "nearest", "limits"}
#   GET    /guilds/{gid}/birthdays/timezones               -> {"timezones": [...]}
#   GET    /guilds/{gid}/birthdays/role-options?actor_id=  -> {"roles": [{..., "blocked_reason"}]}
#   PUT    /guilds/{gid}/birthdays/config  {actor_id, partial} -> overview + {"warnings", "changed"}
#   PUT    /guilds/{gid}/birthdays/entries/{uid} {actor_id, month, day, timezone?, clear_timezone?}
#   DELETE /guilds/{gid}/birthdays/entries/{uid}?actor_id=  -> {"removed"}
#   POST   /guilds/{gid}/birthdays/test  {actor_id, user_ids} -> {"ok", "channel_id", "member_count"}
#   POST   /guilds/{gid}/birthdays/reset-timers {actor_id} -> {"reset", "scheduled"}
#   GET    /guilds/{gid}/birthdays/export?format=&actor_id= -> {"filename", "content", "count"}
#   POST   /guilds/{gid}/birthdays/import {actor_id, filename, content, dry_run} -> preview / result
# ---------------------------------------------------------------------------


def _birthdays(guild_id: int, suffix: str = "") -> str:
    return f"/guilds/{guild_id}/birthdays{suffix}"


async def get_birthdays(guild_id: int) -> dict:
    return await _feature_request("GET", _birthdays(guild_id))


async def get_birthday_timezones(guild_id: int) -> dict:
    return await _feature_request("GET", _birthdays(guild_id, "/timezones"))


async def get_birthday_role_options(guild_id: int, actor_id: int) -> dict:
    return await _feature_request("GET", _birthdays(guild_id, "/role-options"), params={"actor_id": actor_id})


async def update_birthday_config(guild_id: int, values: dict) -> dict:
    return await _feature_request("PUT", _birthdays(guild_id, "/config"), json=values)


async def set_birthday_entry(guild_id: int, user_id: int, values: dict) -> dict:
    return await _feature_request("PUT", _birthdays(guild_id, f"/entries/{user_id}"), json=values)


async def remove_birthday_entry(guild_id: int, user_id: int, actor_id: int) -> dict:
    return await _feature_request(
        "DELETE", _birthdays(guild_id, f"/entries/{user_id}"), params={"actor_id": actor_id}
    )


async def birthday_test_announcement(guild_id: int, actor_id: int, user_ids: list[int]) -> dict:
    return await _feature_request(
        "POST", _birthdays(guild_id, "/test"), json={"actor_id": actor_id, "user_ids": user_ids}, slow=True
    )


async def birthday_reset_timers(guild_id: int, actor_id: int) -> dict:
    return await _feature_request("POST", _birthdays(guild_id, "/reset-timers"), json={"actor_id": actor_id})


async def export_birthdays(guild_id: int, fmt: str, actor_id: int) -> dict:
    return await _feature_request(
        "GET", _birthdays(guild_id, "/export"), params={"format": fmt, "actor_id": actor_id}
    )


async def import_birthdays(guild_id: int, *, actor_id: int, filename: str, content: str, dry_run: bool) -> dict:
    return await _feature_request(
        "POST", _birthdays(guild_id, "/import"),
        json={"actor_id": actor_id, "filename": filename, "content": content, "dry_run": dry_run},
        slow=True,
    )


# ---------------------------------------------------------------------------
# Music page (internal_api/routes/music.py). 200 or 202 -> json, else
# InternalAPIError(status, detail) with detail {"error", "message"}.
#   GET    /guilds/{id}/music/state | /voice-channels?actor_id= | /status?staff=
#   POST   /guilds/{id}/music/control  {actor_id, action, value?, index?, to_index?, channel_id?}
#   POST   /guilds/{id}/music/play | /search | /debugurl  -> 202 job; GET /music/jobs/{job_id}
#   POST   /guilds/{id}/music/search/{job_id}/pick {actor_id, index, channel_id?}
#   GET    /guilds/{id}/music/playlists; POST /music/playlists {actor_id, name}
#   POST   /guilds/{id}/music/playlists/{name}/load; DELETE /music/playlists/{name}?actor_id=
# Joining a voice channel happens inside control/play/pick/load (bounded to
# ~20 s bot-side) and the status call shells out for versions, so these use a
# 45 s timeout; yt-dlp work always runs as a job (never inside one request).
# ---------------------------------------------------------------------------

_MUSIC_TIMEOUT = httpx.Timeout(45.0, connect=5.0)


async def _music_request(method: str, guild_id: int, path: str, *, params: dict | None = None, json: dict | None = None) -> dict:
    full = f"/guilds/{guild_id}/music{path}"
    async with _client() as client:
        try:
            resp = await client.request(method, full, params=params, json=json, timeout=_MUSIC_TIMEOUT)
        except httpx.RequestError as exc:
            raise InternalAPIError(f"Internal API unreachable: {exc}") from exc
    if resp.status_code not in (200, 202):
        detail = None
        try:
            body = resp.json()
            detail = body.get("detail") if isinstance(body, dict) else None
        except ValueError:
            pass
        raise InternalAPIError(
            f"Internal API returned {resp.status_code} for {method} {full}", status_code=resp.status_code, detail=detail
        )
    return resp.json()


async def get_music_state(guild_id: int) -> dict:
    return await _music_request("GET", guild_id, "/state")


async def get_music_voice_channels(guild_id: int, actor_id: int) -> dict:
    return await _music_request("GET", guild_id, "/voice-channels", params={"actor_id": actor_id})


async def get_music_status(guild_id: int, staff: bool) -> dict:
    return await _music_request("GET", guild_id, "/status", params={"staff": str(bool(staff)).lower()})


async def music_control(guild_id: int, payload: dict) -> dict:
    return await _music_request("POST", guild_id, "/control", json=payload)


async def music_start_play(guild_id: int, *, actor_id: int, query: str, channel_id: int | None) -> dict:
    return await _music_request("POST", guild_id, "/play", json={"actor_id": actor_id, "query": query, "channel_id": channel_id})


async def music_start_search(guild_id: int, *, actor_id: int, query: str) -> dict:
    return await _music_request("POST", guild_id, "/search", json={"actor_id": actor_id, "query": query})


async def music_pick(guild_id: int, job_id: str, *, actor_id: int, index: int, channel_id: int | None) -> dict:
    return await _music_request(
        "POST", guild_id, f"/search/{job_id}/pick", json={"actor_id": actor_id, "index": index, "channel_id": channel_id}
    )


async def music_start_debugurl(guild_id: int, *, actor_id: int, query: str | None) -> dict:
    return await _music_request("POST", guild_id, "/debugurl", json={"actor_id": actor_id, "query": query})


async def get_music_job(guild_id: int, job_id: str) -> dict:
    return await _music_request("GET", guild_id, f"/jobs/{job_id}")


async def get_music_playlists(guild_id: int) -> dict:
    return await _music_request("GET", guild_id, "/playlists")


async def music_save_playlist(guild_id: int, *, actor_id: int, name: str) -> dict:
    return await _music_request("POST", guild_id, "/playlists", json={"actor_id": actor_id, "name": name})


async def music_load_playlist(guild_id: int, name: str, *, actor_id: int, channel_id: int | None) -> dict:
    return await _music_request(
        "POST", guild_id, f"/playlists/{name}/load", json={"actor_id": actor_id, "channel_id": channel_id}
    )


async def music_delete_playlist(guild_id: int, name: str, *, actor_id: int) -> dict:
    return await _music_request("DELETE", guild_id, f"/playlists/{name}", params={"actor_id": actor_id})
