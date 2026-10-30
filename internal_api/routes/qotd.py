"""
GET  /guilds/{guild_id}/qotd/config
PUT  /guilds/{guild_id}/qotd/config
GET  /guilds/{guild_id}/qotd/history
GET  /guilds/{guild_id}/qotd/models
GET  /guilds/{guild_id}/qotd/fallback-questions
POST /guilds/{guild_id}/qotd/generate            -> 202 {job_id, status, ...}
GET  /guilds/{guild_id}/qotd/generate/{job_id}   -> {job_id, status, question, source, errors, error}
POST /guilds/{guild_id}/qotd/post

Thin wrapper over services.qotd_service.QotdService + database.repositories.
qotd_repo.QotdRepository — the SAME instances cogs/qotd.py's `/qotd`
slash-command group already uses (resolved via internal_api.deps.get_qotd_cog
-> bot.get_cog("QOTD")), so dashboard-issued generate/post actions and
Discord-command-issued ones are indistinguishable in storage (same
qotd_config / qotd_history tables) and produce the identical embed post +
history row that `/qotd now` and the staff-lounge approval flow do.

QOTD ALREADY has a fully automatic daily scheduler: cogs/qotd.py's
`_scheduler` (`@tasks.loop(seconds=60)`) reads `qotd_config` (guild_id,
channel_id, post_hour, post_minute, enabled, model, use_emojis, ping_role_id,
staff_ping_role_id, approval_channel_id — migrations 009/010/011/016) every
minute and, when due, generates a question and posts a staff-lounge approval
embed (see ApprovalView) rather than posting straight to the public channel.
This route file does NOT add a second scheduling mechanism — it just exposes
that existing config for editing, plus manual generate/preview/post actions
that call the exact same QotdService methods the slash commands call.

`/qotd/generate` NEVER posts. It starts a background generation JOB
(services.qotd_service.GenerationJobs on the cog) and answers 202 at once;
the dashboard polls GET /qotd/generate/{job_id} until status is "done" (with
question/source/errors) or "failed". A generation can take minutes (cold
model load, 3 attempts), which a single request could not survive behind
Cloudflare's 100 s limit; the job is also bounded by
DASHBOARD_GENERATE_DEADLINE_SECONDS, after which it returns a fallback
question. Jobs are in memory: a bot restart loses them (poll -> 404). Posting
is a separate, explicit `/qotd/post` call (mirrors `/qotd now`, minus the
approval hop — the dashboard IS the staff-approval surface here, so posting
from it does not additionally queue a staff-lounge approval message).

`/qotd/fallback-questions` returns the hardcoded FALLBACK_QUESTIONS list from
services/qotd_service.py verbatim — read-only, since it's still a Python
constant, not a DB table (see that module's docstring / this feature's
design notes for why it was left as a constant).
"""

from __future__ import annotations

import discord
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from internal_api.auth import require_internal_secret
from internal_api.deps import get_guild, get_qotd_cog
from internal_api.snowflakes import stringify_snowflakes
from services.qotd_service import HistoryLogError
from services.qotd_service import DASHBOARD_GENERATE_DEADLINE_SECONDS, FALLBACK_QUESTIONS

router = APIRouter(dependencies=[Depends(require_internal_secret)])


@router.get("/guilds/{guild_id}/qotd/config")
async def get_qotd_config(
    guild: discord.Guild = Depends(get_guild),
    cog=Depends(get_qotd_cog),
) -> dict:
    cfg = await cog._repo.get_config(guild.id)
    last_posted = await cog._repo.get_last_posted_at(guild.id)
    return stringify_snowflakes(
        {
            "config": cfg,
            "last_posted_at": last_posted,
            "pending_approval": guild.id in cog._pending,
        }
    )


class UpsertQotdConfigRequest(BaseModel):
    channel_id: int | None = None
    post_hour: int | None = None
    post_minute: int | None = None
    enabled: bool | None = None
    model: str | None = None
    use_emojis: bool | None = None
    ping_role_id: int | None = None
    clear_ping_role: bool = False
    staff_ping_role_id: int | None = None
    clear_staff_ping_role: bool = False
    approval_channel_id: int | None = None
    clear_approval_channel: bool = False


@router.put("/guilds/{guild_id}/qotd/config")
async def upsert_qotd_config(
    body: UpsertQotdConfigRequest,
    guild: discord.Guild = Depends(get_guild),
    cog=Depends(get_qotd_cog),
) -> dict:
    await cog._repo.upsert_config(
        guild.id,
        channel_id=body.channel_id,
        post_hour=body.post_hour,
        post_minute=body.post_minute,
        enabled=body.enabled,
        model=body.model,
        use_emojis=body.use_emojis,
        ping_role_id=body.ping_role_id,
        clear_ping_role=body.clear_ping_role,
        staff_ping_role_id=body.staff_ping_role_id,
        clear_staff_ping_role=body.clear_staff_ping_role,
        approval_channel_id=body.approval_channel_id,
        clear_approval_channel=body.clear_approval_channel,
    )
    cfg = await cog._repo.get_config(guild.id)
    return stringify_snowflakes({"config": cfg})


@router.get("/guilds/{guild_id}/qotd/history")
async def get_qotd_history(
    guild: discord.Guild = Depends(get_guild),
    cog=Depends(get_qotd_cog),
    limit: int = 50,
) -> dict:
    limit = max(1, min(limit, 200))
    rows = await cog._repo.get_history(guild.id, limit=limit)
    return stringify_snowflakes({"history": rows})


@router.get("/guilds/{guild_id}/qotd/models")
async def get_qotd_models(
    guild: discord.Guild = Depends(get_guild),
    cog=Depends(get_qotd_cog),
) -> dict:
    """Returns the same AVAILABLE_MODELS catalogue /qotd setmodel's select
    menu is built from (cogs/qotd.py), so the dashboard's model picker shows
    identical labels/descriptions."""
    from cogs.qotd import AVAILABLE_MODELS

    return {"models": AVAILABLE_MODELS}


@router.get("/guilds/{guild_id}/qotd/fallback-questions")
async def get_fallback_questions(
    guild: discord.Guild = Depends(get_guild),
) -> dict:
    return {"questions": FALLBACK_QUESTIONS}


class GenerateQotdRequest(BaseModel):
    model: str | None = None


@router.post("/guilds/{guild_id}/qotd/generate", status_code=status.HTTP_202_ACCEPTED)
async def start_generate_qotd(
    body: GenerateQotdRequest,
    guild: discord.Guild = Depends(get_guild),
    cog=Depends(get_qotd_cog),
) -> dict:
    """Starts generating + validating a question WITHOUT posting it (mirrors
    `/qotd preview`) and returns the job at once. Never touches
    qotd_history; only `/qotd/post` below records a history row. A second
    call while a job runs for this guild returns that job (reused=true)."""
    cfg = await cog._repo.get_config(guild.id)
    model = body.model or (cfg["model"] if cfg else None)

    async def _run():
        return await cog._service.generate_question(
            guild.id, model=model, deadline_seconds=DASHBOARD_GENERATE_DEADLINE_SECONDS
        )

    return cog._jobs.start(guild.id, model, _run)


@router.get("/guilds/{guild_id}/qotd/generate/{job_id}")
async def get_generate_job(
    job_id: str,
    guild: discord.Guild = Depends(get_guild),
    cog=Depends(get_qotd_cog),
) -> dict:
    job = cog._jobs.get(guild.id, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "job_not_found",
                    "message": "That generation is no longer available (the bot may have restarted). Generate again."},
        )
    return job


class PostQotdRequest(BaseModel):
    question: str
    source: str
    channel_id: int
    use_emojis: bool = False
    ping_role_id: int | None = None
    actor_user_id: int | None = None


@router.post("/guilds/{guild_id}/qotd/post")
async def post_qotd(
    body: PostQotdRequest,
    guild: discord.Guild = Depends(get_guild),
    cog=Depends(get_qotd_cog),
) -> dict:
    channel = guild.get_channel(body.channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "channel_not_found", "message": "That channel no longer exists or is not a text channel."},
        )

    question = body.question.strip()
    if not question:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_request", "message": "The question must not be empty."},
        )

    try:
        msg = await cog._service.post_qotd(
            guild, channel, question, body.source,
            use_emojis=body.use_emojis,
            ping_role_id=body.ping_role_id,
        )
    except HistoryLogError as exc:
        # Posted to Discord, but the history row failed: report success with
        # a warning rather than inviting a duplicate retry.
        msg = exc.message
        warning = "Posted, but saving it to the QOTD history failed."
        if body.actor_user_id is not None:
            await cog._audit.log(
                guild.id, body.actor_user_id, "qotd_posted_dashboard",
                target_type="channel", target_id=channel.id,
                detail={"question": question, "source": body.source, "history_saved": False},
            )
        return stringify_snowflakes({"message_id": msg.id, "channel_id": channel.id, "warning": warning})
    except discord.Forbidden:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "discord_forbidden", "message": f"The bot is not allowed to post in #{channel.name}."},
        )
    except discord.HTTPException as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "discord_error", "message": f"Discord rejected the post: {exc.text or exc}"[:300]},
        )

    if body.actor_user_id is not None:
        await cog._audit.log(
            guild.id, body.actor_user_id, "qotd_posted_dashboard",
            target_type="channel", target_id=channel.id,
            detail={"question": question, "source": body.source},
        )

    return stringify_snowflakes({"message_id": msg.id, "channel_id": channel.id})
