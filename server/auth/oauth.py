"""
Discord OAuth2 login flow.

GET /api/auth/login[?next=/hub]
    Redirects the browser to Discord's authorize URL. Generates a random
    `state` value, stores it in a short-lived httpOnly/SameSite=Lax cookie
    (SameSite=Lax specifically because it must survive Discord's top-level
    cross-site redirect back to our callback — Strict would drop it).
    `next` picks where the callback lands; only the fixed paths in
    LOGIN_NEXT_PATHS are honoured (anything else = the dashboard), so it can
    never become an open redirect.

GET /api/auth/callback
    Validates `state` against the cookie (CSRF protection for the OAuth
    flow itself), exchanges the authorization `code` for a token, fetches
    the user's identity and their guild-membership/roles for the dashboard's
    scoped guild, creates a session, sets the session cookie, and redirects
    to the frontend.

POST /api/auth/logout
    Deletes the current session (if any) server-side and clears the session
    cookie. Idempotent — calling it with no/invalid session still succeeds,
    since the end state ("no valid session") is the same either way.

POST /api/auth/logout-all
    "Log out everywhere": deletes EVERY session of the current user (all
    browsers/devices), closes their live sockets and clears this cookie.
    {"ok": true, "sessions_ended": n}. Same idempotency as /logout.

The Discord access token is used only inside the callback (identity + guild
member lookup) and then discarded: it is NOT stored (nothing ever used it;
Alembic revision b3c1d2e4f5a6 nulled the old stored tokens), and since
2026-10-30 it is revoked at Discord as soon as those two reads are done.

Cookie Secure flag: cookie_secure(request), see config.COOKIE_SECURE_MODE
(auto = Secure when the request came in over HTTPS).
"""

from __future__ import annotations

import secrets

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession as DBAsyncSession

from server import config
from server.auth.dependencies import get_current_session, sign_session_id
from server.auth.session import create_session, delete_session, delete_user_sessions
from server.db.engine import async_session_factory
from server.db.models import Session

router = APIRouter(prefix="/api/auth", tags=["auth"])

limiter = Limiter(key_func=get_remote_address)

DISCORD_AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_REVOKE_URL = "https://discord.com/api/oauth2/token/revoke"

# Where a sign-in may return to (the member hub signs in through the same flow):
# path -> the short key kept in the cookie (a "/" would make the value quoted).
LOGIN_NEXT_PATHS = {"/hub": "hub"}
NEXT_COOKIE_NAME = "oauth_next"


def safe_next(value: str | None) -> str | None:
    return value if value in LOGIN_NEXT_PATHS else None


def _next_from_cookie(value: str | None) -> str | None:
    return next((path for path, key in LOGIN_NEXT_PATHS.items() if key == value), None)


async def revoke_token(client: httpx.AsyncClient, token: str) -> None:
    """Best effort: revoke a Discord OAuth access token right after use."""
    try:
        await client.post(
            DISCORD_REVOKE_URL,
            data={"token": token, "token_type_hint": "access_token"},
            auth=(config.DISCORD_CLIENT_ID, config.DISCORD_CLIENT_SECRET),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except httpx.RequestError:
        pass  # it expires on its own


async def _get_db() -> DBAsyncSession:
    async with async_session_factory() as db:
        yield db


_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def cookie_secure(request: Request) -> bool:
    """Whether to set the Secure flag on cookies for this request.

    DASHBOARD_COOKIE_SECURE=true/false forces it; auto (the default) sets it
    when the browser is on HTTPS: the request scheme itself (uvicorn already
    rewrites it from X-Forwarded-Proto for a 127.0.0.1 proxy), or an explicit
    X-Forwarded-Proto: https from a loopback peer (cloudflared runs on this
    machine). A forwarded header from a non-loopback peer is ignored."""
    mode = config.COOKIE_SECURE_MODE
    if mode == "true":
        return True
    if mode == "false":
        return False
    if request.url.scheme in ("https", "wss"):
        return True
    peer = request.client.host if request.client else None
    if peer in _LOOPBACK:
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        return proto == "https"
    return False


def _build_authorize_url(state: str) -> str:
    # No "prompt" param: omitting it lets Discord show the normal consent
    # screen when needed (first-time authorization for this redirect_uri, or
    # after a scope/redirect_uri change) and skip it silently once the user
    # has already granted. "prompt=none" forces Discord to skip UI entirely
    # and silently fail if there's no existing grant to reuse — this broke
    # login on https://dash.idlesys.xyz specifically because no one had ever
    # completed a full consent flow for THAT redirect_uri (only for the
    # earlier http://localhost:8080 one), so Discord had nothing to silently
    # approve and bounced the request instead of showing the consent screen.
    params = {
        "client_id": config.DISCORD_CLIENT_ID,
        "redirect_uri": config.DISCORD_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": config.DISCORD_OAUTH_SCOPES,
        "state": state,
    }
    qp = httpx.QueryParams(params)
    return f"{DISCORD_AUTHORIZE_URL}?{qp}"


@router.get("/login")
@limiter.limit("10/minute")
async def login(request: Request, next: str | None = None) -> RedirectResponse:
    state = secrets.token_urlsafe(32)
    authorize_url = _build_authorize_url(state)

    response = RedirectResponse(url=authorize_url, status_code=status.HTTP_302_FOUND)
    next_path = safe_next(next)
    if next_path:
        response.set_cookie(
            key=NEXT_COOKIE_NAME, value=LOGIN_NEXT_PATHS[next_path], max_age=config.OAUTH_STATE_MAX_AGE_SECONDS,
            httponly=True, secure=cookie_secure(request), samesite="lax", path="/api/auth",
        )
    else:
        response.delete_cookie(key=NEXT_COOKIE_NAME, path="/api/auth", secure=cookie_secure(request),
                               httponly=True, samesite="lax")
    response.set_cookie(
        key=config.OAUTH_STATE_COOKIE_NAME,
        value=state,
        max_age=config.OAUTH_STATE_MAX_AGE_SECONDS,
        httponly=True,
        secure=cookie_secure(request),
        samesite="lax",
        path="/api/auth",
    )
    return response


@router.get("/callback")
@limiter.limit("10/minute")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: DBAsyncSession = Depends(_get_db),
) -> RedirectResponse:
    # ------------------------------------------------------------------
    # 1. Validate state BEFORE doing anything else — no token exchange,
    #    no outbound call to Discord, if this fails.
    # ------------------------------------------------------------------
    cookie_state = request.cookies.get(config.OAUTH_STATE_COOKIE_NAME)

    if error is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "oauth_denied"})

    if not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "invalid_state"})

    if not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "missing_code"})

    # ------------------------------------------------------------------
    # 2. Exchange the authorization code for a token.
    # ------------------------------------------------------------------
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        try:
            token_resp = await client.post(
                DISCORD_TOKEN_URL,
                data={
                    "client_id": config.DISCORD_CLIENT_ID,
                    "client_secret": config.DISCORD_CLIENT_SECRET,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": config.DISCORD_OAUTH_REDIRECT_URI,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.RequestError:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": "discord_unreachable"})

        if token_resp.status_code != 200:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "token_exchange_failed"})

        token_data = token_resp.json()
        # Used for the two lookups below, then dropped (never stored).
        access_token = token_data["access_token"]

        auth_header = {"Authorization": f"Bearer {access_token}"}

        try:
            # ----------------------------------------------------------
            # 3. Fetch identity.
            # ----------------------------------------------------------
            try:
                user_resp = await client.get(f"{DISCORD_API_BASE}/users/@me", headers=auth_header)
            except httpx.RequestError:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": "discord_unreachable"})

            if user_resp.status_code != 200:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": "discord_user_fetch_failed"})

            user_data = user_resp.json()
            discord_user_id = int(user_data["id"])
            discord_username = user_data.get("username", "")
            discord_avatar = user_data.get("avatar")

            # ----------------------------------------------------------
            # 4. Resolve guild membership + roles for the dashboard's guild.
            #    A 404 here means the user is not a member of that guild.
            # ----------------------------------------------------------
            try:
                member_resp = await client.get(
                    f"{DISCORD_API_BASE}/users/@me/guilds/{config.DASHBOARD_GUILD_ID}/member",
                    headers=auth_header,
                )
            except httpx.RequestError:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": "discord_unreachable"})
        finally:
            # The token's job is done (two reads). Revoke it at Discord so a
            # copy of it is useless; it was never stored (security audit
            # 2026-10-30; the appeal sign-in already did this).
            await revoke_token(client, access_token)

    is_guild_member = member_resp.status_code == 200
    cached_role_ids: list[int] = []
    if is_guild_member:
        member_data = member_resp.json()
        cached_role_ids = [int(r) for r in member_data.get("roles", [])]
    elif member_resp.status_code != 404:
        # Anything other than 200 (member) or 404 (not a member) is an
        # unexpected Discord-side failure, not a "not a member" state.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": "discord_member_fetch_failed"})

    # ------------------------------------------------------------------
    # 5. Create the session, set the cookie, redirect to the frontend.
    # ------------------------------------------------------------------
    session = await create_session(
        db,
        discord_user_id=discord_user_id,
        discord_username=discord_username,
        discord_avatar=discord_avatar,
        is_guild_member=is_guild_member,
        cached_role_ids=cached_role_ids,
    )

    next_path = _next_from_cookie(request.cookies.get(NEXT_COOKIE_NAME))
    target = config.FRONTEND_BASE_URL.rstrip("/") + next_path if next_path else config.FRONTEND_BASE_URL
    response = RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        key=config.SESSION_COOKIE_NAME,
        value=sign_session_id(session.session_id),
        max_age=config.SESSION_COOKIE_MAX_AGE_SECONDS,  # sliding expiry is enforced server-side
        httponly=True,
        secure=cookie_secure(request),
        samesite="lax",
        path="/",
    )
    # Clear the now-consumed state (and next) cookies.
    for consumed in (config.OAUTH_STATE_COOKIE_NAME, NEXT_COOKIE_NAME):
        response.delete_cookie(
            key=consumed, path="/api/auth",
            secure=cookie_secure(request), httponly=True, samesite="lax",
        )
    return response


def _clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=config.SESSION_COOKIE_NAME, path="/",
        secure=cookie_secure(request), httponly=True, samesite="lax",
    )


@router.post("/logout")
async def logout(
    request: Request,
    session: Session | None = Depends(get_current_session),
    db: DBAsyncSession = Depends(_get_db),
) -> JSONResponse:
    if session is not None:
        await delete_session(db, session.session_id)
        try:  # close this session's live sockets immediately
            from server.live.hub import hub as live_hub

            await live_hub.close_session(session.session_id)
        except Exception:
            pass

    response = JSONResponse(content={"ok": True})
    _clear_session_cookie(response, request)
    return response


@router.post("/logout-all")
async def logout_all(
    request: Request,
    session: Session | None = Depends(get_current_session),
    db: DBAsyncSession = Depends(_get_db),
) -> JSONResponse:
    """Log out everywhere: every session of this Discord user is deleted and
    every live socket bound to one of them is closed (4401)."""
    ended = 0
    if session is not None:
        session_ids = await delete_user_sessions(db, int(session.discord_user_id))
        ended = len(session_ids)
        try:
            from server.live.hub import hub as live_hub

            for sid in session_ids:
                await live_hub.close_session(sid)
        except Exception:
            pass

    response = JSONResponse(content={"ok": True, "sessions_ended": ended})
    _clear_session_cookie(response, request)
    return response
