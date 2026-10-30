"""
POST /guilds/{guild_id}/resolve

Batch Discord-ID -> display-metadata resolver. Every dashboard page today
prints raw snowflake IDs (user/moderator/role/channel) with no name
resolution; this endpoint lets the frontend turn a page's worth of IDs into
names/avatars/colors in a single round trip instead of one request per ID.

Users: cache (guild.get_member) first for every id, then batch-fetch the
cache misses via guild.query_members (cheap gateway op, safe since the bot
runs with Intents.members = True — see main.py) with individual
guild.fetch_member as a fallback for any ids query_members still can't
resolve (query_members matches by name/nickname prefix in some client
versions and is not guaranteed to return every id you asked for). The
fetch_member fallback is wrapped in asyncio.gather(..., return_exceptions=True)
so ONE user who left the guild (discord.NotFound) can't fail the whole
batch — such ids simply resolve to null in the response. A transient error
(rate limit, Discord 5xx, timeout) leaves the id OUT of the response instead,
so the frontend retries rather than showing "(user left)". Ids are deduped
first (max _MAX_BATCH per list after dedupe), and the fetch_member fallback
runs at most _FETCH_CONCURRENCY calls at a time (asyncio.Semaphore) so one
page of unknown ids cannot fire 100 parallel Discord REST calls. This matters
because e.g. a ticket's chat log references users who may have since left;
their old messages must still render, just without a resolvable name.

Departed users (2026-10-28): an id that is definitively not in the guild
(discord.NotFound) is looked up in the user cache (services/
user_cache_service.py, its own file USER_CACHE_PATH, kept by
cogs/user_cache.py). A cached user answers with the member shape PLUS
`departed: true`, `status` (left / kicked / banned / unbanned),
`departed_at`, `status_changed_at`, `departed_by_id`, `departure_source`,
`global_name`, `nick`, `color`, `bot`, `joined_at`, `first_seen`,
`last_seen`, `cached_at` (see serialize_departed). Ids with no cached
profile, or one older than _REFRESH_AFTER, are refreshed with
bot.fetch_user (at most _REFRESH_MAX per request, _FETCH_CONCURRENCY at a
time) and the result is written back to the cache; only when that also
fails does the id resolve to null, as before. Members (found in the guild)
keep exactly the old four-field shape.

Also here (small, same data): GET/DELETE /guilds/{guild_id}/user-cache/{user_id}
read or forget one cached profile (the dashboard's DELETE is staff-only and
audited as `user_cache_forget`).

Roles and channels are always fully cached on discord.Guild (guild.get_role /
guild.get_channel) — no API calls, no batching concerns, so a cache miss
there just means the id genuinely doesn't exist in this guild anymore.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from internal_api.auth import require_internal_secret
from internal_api.deps import get_bot, get_guild
from services import user_cache_service as ucs

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_internal_secret)])

_MAX_BATCH = 100
_FETCH_CONCURRENCY = 5  # parallel guild.fetch_member / fetch_user calls per request
_REFRESH_MAX = 10  # fetch_user refreshes of departed users per request
_REFRESH_AFTER = timedelta(days=7)  # a departed user's cached profile older than this is refreshed
_SQLITE_MAX_INT = 2**63 - 1


def _dedupe(ids: list[int]) -> list[int]:
    return list(dict.fromkeys(ids))


class ResolveRequest(BaseModel):
    user_ids: list[int] = []
    role_ids: list[int] = []
    channel_ids: list[int] = []


def _serialize_member(member: discord.Member) -> dict:
    return {
        "id": str(member.id),
        "username": member.name,
        "display_name": member.display_name,
        "avatar_url": member.display_avatar.url,
    }


def _serialize_role(role: discord.Role) -> dict:
    return {
        "id": str(role.id),
        "name": role.name,
        "color": f"#{role.color.value:06x}" if role.color.value else None,
    }


def _serialize_channel(channel) -> dict:
    return {"id": str(channel.id), "name": channel.name}


def _merge_user_profile(row: dict | None, user, now: str) -> dict:
    """A cache row refreshed with a fetched discord.User (same rules as the
    store's user-profile write: nick / guild avatar / colour are kept)."""
    p = ucs.profile_from(user)
    if row is None:
        return {**p, "status": "left", "updated_at": now, "first_seen": now}
    merged = dict(row)
    merged.update(
        username=p["username"], global_name=p["global_name"], avatar_hash=p["avatar_hash"],
        is_bot=p["is_bot"], updated_at=now,
    )
    if not row.get("nick"):
        merged["display_name"] = p["display_name"]
    if not row.get("guild_avatar_hash"):
        merged["avatar_url"] = p["avatar_url"]
    return merged


async def _departed_users(bot, gone: list[int]) -> dict[str, dict | None]:
    """Cached (and, when cheap, freshly fetched) profiles for ids that are not
    in the guild. Never raises; anything unknown stays null."""
    out: dict[str, dict | None] = {str(uid): None for uid in gone}
    store = ucs.get_active()
    rows: dict[int, dict] = {}
    if store is not None:
        try:
            rows = await store.get_many(gone)
        except Exception:
            log.exception("resolve: user cache read failed")
            rows = {}

    now = datetime.now(timezone.utc)
    stale = [uid for uid in gone if ucs.needs_refresh(rows.get(uid), now, _REFRESH_AFTER)][:_REFRESH_MAX]
    if stale and bot is not None:
        sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

        async def fetch(uid: int):
            async with sem:
                return await bot.fetch_user(uid)

        results = await asyncio.gather(*(fetch(uid) for uid in stale), return_exceptions=True)
        stamp = ucs._ts(now)
        for uid, user in zip(stale, results):
            if isinstance(user, BaseException) or user is None:
                continue
            try:
                rows[uid] = _merge_user_profile(rows.get(uid), user, stamp)
                if store is not None:
                    store.touch_profile(user, insert_status="left")
            except Exception:
                log.exception("resolve: could not cache fetched user %s", uid)

    for uid in gone:
        row = rows.get(uid)
        if row is not None and row.get("username"):
            out[str(uid)] = ucs.serialize_departed({**row, "user_id": uid})
    return out


async def _resolve_users(guild: discord.Guild, user_ids: list[int], bot=None) -> dict[str, dict | None]:
    users: dict[str, dict | None] = {}
    misses: list[int] = []

    for user_id in user_ids:
        member = guild.get_member(user_id)
        if member is not None:
            users[str(user_id)] = _serialize_member(member)
        else:
            misses.append(user_id)

    if not misses:
        return users

    try:
        # limit defaults to 5 and caps at 100 — query_members with user_ids
        # returns at most `limit` members regardless of how many ids were
        # requested, so it must be set to len(misses) or matches beyond the
        # 5th id silently vanish (they'd still get caught by the
        # fetch_member fallback below, just at the cost of N extra API
        # calls instead of one batched gateway op).
        fetched = await guild.query_members(user_ids=misses, limit=len(misses))
    except (discord.HTTPException, discord.ClientException):
        fetched = []

    still_missing = set(misses)
    for member in fetched:
        users[str(member.id)] = _serialize_member(member)
        still_missing.discard(member.id)

    gone: list[int] = []
    if still_missing:
        ordered = [uid for uid in misses if uid in still_missing]
        sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

        async def fetch(uid: int):
            async with sem:
                return await guild.fetch_member(uid)

        results = await asyncio.gather(*(fetch(uid) for uid in ordered), return_exceptions=True)
        for uid, result in zip(ordered, results):
            if isinstance(result, discord.Member):
                users[str(uid)] = _serialize_member(result)
            elif isinstance(result, discord.NotFound):
                # Definitively not in the guild: the user cache (below) or null.
                users[str(uid)] = None
                gone.append(uid)
            # Any other error (rate limit, Discord 5xx, timeout) is transient:
            # the id is left OUT of the response, which the frontend's
            # useResolve treats as "retry later, show the raw id meanwhile".

    if gone:
        users.update(await _departed_users(bot, gone))
    return users


@router.post("/guilds/{guild_id}/resolve")
async def resolve_ids(
    body: ResolveRequest,
    guild: discord.Guild = Depends(get_guild),
    bot=Depends(get_bot),
) -> dict:
    body.user_ids = _dedupe(body.user_ids)
    body.role_ids = _dedupe(body.role_ids)
    body.channel_ids = _dedupe(body.channel_ids)
    for field_name, ids in (
        ("user_ids", body.user_ids),
        ("role_ids", body.role_ids),
        ("channel_ids", body.channel_ids),
    ):
        if len(ids) > _MAX_BATCH:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{field_name} exceeds max batch size of {_MAX_BATCH}.",
            )

    users = await _resolve_users(guild, body.user_ids, bot)

    roles: dict[str, dict | None] = {}
    for role_id in body.role_ids:
        role = guild.get_role(role_id)
        roles[str(role_id)] = _serialize_role(role) if role else None

    channels: dict[str, dict | None] = {}
    for channel_id in body.channel_ids:
        channel = guild.get_channel(channel_id)
        channels[str(channel_id)] = _serialize_channel(channel) if channel else None

    return {"users": users, "roles": roles, "channels": channels}


# ---------------------------------------------------------------------------
# One cached profile: read / forget
# ---------------------------------------------------------------------------

def _store_or_503() -> ucs.UserCacheStore:
    store = ucs.get_active()
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "user_cache_unavailable", "message": "The user cache is off or not loaded."},
        )
    return store


@router.get("/guilds/{guild_id}/user-cache/{user_id}")
async def get_cached_user(
    user_id: int,
    guild: discord.Guild = Depends(get_guild),
) -> dict:
    """The cached profile in the departed-user shape (or 404 not_cached).
    `in_guild` says whether they are a member right now (then `departed` is
    false and `status` the stored one)."""
    store = _store_or_503()
    row = await store.get(user_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "not_cached"})
    entry = ucs.serialize_departed(row)
    in_guild = guild.get_member(user_id) is not None
    if in_guild:
        entry.update(departed=False, status="member")
    return {"user": entry, "in_guild": in_guild}


@router.delete("/guilds/{guild_id}/user-cache/{user_id}")
async def forget_cached_user(
    user_id: int,
    actor_id: int = Query(..., ge=1, le=_SQLITE_MAX_INT),
    guild: discord.Guild = Depends(get_guild),
    bot=Depends(get_bot),
) -> dict:
    """Delete one user's cached profile now (audited as user_cache_forget).
    A current member is re-added by the next member event or daily sweep."""
    store = _store_or_503()
    removed = await store.forget(user_id)
    db = getattr(bot, "db", None)
    if db is not None:
        try:
            from database.repositories.audit_repo import AuditRepository
            from services.audit_service import AuditService

            await AuditService(AuditRepository(db)).log(
                guild.id, actor_id, "user_cache_forget", target_type="user", target_id=user_id,
                detail={"removed": removed, "source": "dashboard"},
            )
        except Exception:
            log.exception("resolve: audit row for user_cache_forget failed")
    return {"ok": True, "removed": removed, "in_guild": guild.get_member(user_id) is not None}
