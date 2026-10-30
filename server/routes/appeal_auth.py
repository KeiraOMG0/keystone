"""
PUBLIC ban-appeal sign-in with Discord (no dashboard session: banned people use it).

GET  /api/public/appeal-auth/login?intent=appeal|rejoin -> 302 to Discord's consent screen
GET  /api/public/appeal-auth/callback?code=&state=       -> 302 to /appeal?signin=... or ?rejoin=...
GET  /api/public/appeal-auth/me                          -> {signed_in: false} |
                                                            {signed_in: true, user: {id, username}, eligible, appeal}
POST /api/public/appeal-auth/submit {name, message}      -> {ok, status} (appeal as the signed-in user)
POST /api/public/appeal-auth/logout                      -> {ok}

Why login: it PROVES which account is appealing (the typed-user-id form cannot), so
`me` may show the person their own appeal and `submit` may answer honestly
(not_eligible / already_submitted) without becoming an oracle for who was banned.

Discord tokens are NEVER stored (deliberate, see docs/ARCHITECTURE.md section 19):
  - intent=appeal asks for `identify` only; the access token is used once to read
    /users/@me and then revoked.
  - intent=rejoin also asks for `guilds.join`; the token goes to the bot for ONE
    add-member call (internal POST .../appeals/rejoin), then it is revoked here.
    So "Rejoin server" needs the person to click and log in at that moment; nothing
    sits in a database waiting for an approval.
  - What remains is a signed, 30-minute identity cookie ({uid, name}, itsdangerous
    with SESSION_COOKIE_SECRET and its own salt), HttpOnly, SameSite=Lax, scoped to
    /api/public/appeal-auth. The OAuth `state` lives in a signed 10-minute cookie.

Redirect URI: config.APPEAL_OAUTH_REDIRECT_URI (must be registered in the Discord
Developer Portal). Rate limited per client IP like the other public appeal routes.
Errors never echo internal detail.
"""

from __future__ import annotations

import logging
import secrets

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, Field, ValidationError, field_validator

from server import config
from server.auth.oauth import DISCORD_API_BASE, DISCORD_AUTHORIZE_URL, DISCORD_TOKEN_URL, cookie_secure, limiter
from server.routes.appeals import client_ip
from server.services import internal_client
from server.services.internal_client import InternalAPIError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/public/appeal-auth", tags=["public-appeal-auth"])

COOKIE_PATH = "/api/public/appeal-auth"
STATE_COOKIE = "appeal_oauth_state"
IDENTITY_COOKIE = "appeal_identity"
STATE_MAX_AGE = 600
IDENTITY_MAX_AGE = 1800
SCOPES = {"appeal": "identify", "rejoin": "identify guilds.join"}
DISCORD_REVOKE_URL = "https://discord.com/api/oauth2/token/revoke"

# Bot answers for the rejoin call -> the ?rejoin= value the appeal page explains.
_REJOIN_CODES = {"not_approved", "still_banned", "too_many_guilds", "login_expired"}

_state_signer = URLSafeTimedSerializer(config.SESSION_COOKIE_SECRET, salt="appeal-oauth-state")
_identity_signer = URLSafeTimedSerializer(config.SESSION_COOKIE_SECRET, salt="appeal-identity")


def _error(code: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code})


def _set_cookie(response: Response, request: Request, key: str, value: str, max_age: int) -> None:
    response.set_cookie(
        key=key, value=value, max_age=max_age, httponly=True, secure=cookie_secure(request),
        samesite="lax", path=COOKIE_PATH,
    )


def _clear_cookie(response: Response, request: Request, key: str) -> None:
    response.delete_cookie(key=key, path=COOKIE_PATH, secure=cookie_secure(request), httponly=True, samesite="lax")


def identity_of(request: Request) -> dict | None:
    """{uid: int, name: str} from a valid identity cookie, else None."""
    raw = request.cookies.get(IDENTITY_COOKIE)
    if not raw:
        return None
    try:
        data = _identity_signer.loads(raw, max_age=IDENTITY_MAX_AGE)
        return {"uid": int(data["uid"]), "name": str(data.get("name") or "")}
    except (BadSignature, KeyError, TypeError, ValueError):
        return None


@router.get("/login")
@limiter.limit("10/minute", key_func=client_ip)
async def login(request: Request, intent: str = "appeal") -> Response:
    if intent not in SCOPES:
        intent = "appeal"
    state = secrets.token_urlsafe(32)
    params = httpx.QueryParams({
        "client_id": config.DISCORD_CLIENT_ID,
        "redirect_uri": config.APPEAL_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES[intent],
        "state": state,
    })
    response = RedirectResponse(url=f"{DISCORD_AUTHORIZE_URL}?{params}", status_code=302)
    _set_cookie(response, request, STATE_COOKIE, _state_signer.dumps({"s": state, "i": intent}), STATE_MAX_AGE)
    return response


async def _revoke(client: httpx.AsyncClient, token: str) -> None:
    """Best effort: a revoked token is useless even if it ever leaked from a log."""
    try:
        await client.post(
            DISCORD_REVOKE_URL,
            data={"token": token, "token_type_hint": "access_token"},
            auth=(config.DISCORD_CLIENT_ID, config.DISCORD_CLIENT_SECRET),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except httpx.RequestError:
        log.warning("appeal sign-in: revoking the Discord token failed (it expires on its own)")


@router.get("/callback")
@limiter.limit("10/minute", key_func=client_ip)
async def callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None,
) -> Response:
    def done(query: str) -> RedirectResponse:
        resp = RedirectResponse(url=f"/appeal?{query}", status_code=302)
        _clear_cookie(resp, request, STATE_COOKIE)
        return resp

    if error is not None:
        return done("signin=cancelled")
    try:
        saved = _state_signer.loads(request.cookies.get(STATE_COOKIE) or "", max_age=STATE_MAX_AGE)
    except BadSignature:
        saved = None
    if not saved or not state or not secrets.compare_digest(state, str(saved.get("s", ""))) or not code:
        return done("signin=failed")
    intent = saved.get("i") if saved.get("i") in SCOPES else "appeal"

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        try:
            token_resp = await client.post(
                DISCORD_TOKEN_URL,
                data={
                    "client_id": config.DISCORD_CLIENT_ID, "client_secret": config.DISCORD_CLIENT_SECRET,
                    "grant_type": "authorization_code", "code": code,
                    "redirect_uri": config.APPEAL_OAUTH_REDIRECT_URI,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.RequestError:
            return done("signin=failed")
        if token_resp.status_code != 200:
            return done("signin=failed")
        access_token = token_resp.json().get("access_token") or ""
        query = "signin=failed"
        try:
            user_resp = await client.get(f"{DISCORD_API_BASE}/users/@me", headers={"Authorization": f"Bearer {access_token}"})
            if user_resp.status_code != 200:
                return done("signin=failed")
            user = user_resp.json()
            uid, name = int(user["id"]), str(user.get("username") or "")
            query = "signin=ok"
            if intent == "rejoin":
                try:
                    result = await internal_client.rejoin_after_appeal(config.DASHBOARD_GUILD_ID, uid, access_token)
                    query = f"rejoin={'joined' if result.get('result') == 'joined' else 'already_member'}"
                except InternalAPIError as exc:
                    detail = exc.detail if isinstance(exc.detail, str) else ""
                    query = f"rejoin={detail if detail in _REJOIN_CODES else 'failed'}"
        finally:
            await _revoke(client, access_token)  # never kept: not in a DB, not in a cookie

    response = done(query)
    _set_cookie(response, request, IDENTITY_COOKIE, _identity_signer.dumps({"uid": str(uid), "name": name}),
                IDENTITY_MAX_AGE)
    return response


@router.get("/me")
@limiter.limit("30/minute", key_func=client_ip)
async def me(request: Request):
    ident = identity_of(request)
    if ident is None:
        return {"signed_in": False}
    try:
        data = await internal_client.get_appeal_status_for_user(config.DASHBOARD_GUILD_ID, ident["uid"])
    except InternalAPIError:
        return _error("unavailable", 503)
    appeal = data.get("appeal")
    return {
        "signed_in": True,
        "user": {"id": str(ident["uid"]), "username": ident["name"]},
        "eligible": bool(data.get("eligible")),
        # Whitelisted: the person's OWN appeal outcome only.
        "appeal": None if not appeal else {
            "status": appeal.get("status"),
            "submitted_at": appeal.get("submitted_at"),
            "reviewed_at": appeal.get("reviewed_at"),
            "rejoin_invite_url": appeal.get("rejoin_invite_url") if appeal.get("status") == "approved" else None,
            "rejoined": bool(appeal.get("rejoined")),
        },
    }


class SubmitBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    message: str = Field(default="", max_length=1500)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("empty")
        return v


@router.post("/submit")
@limiter.limit("5/minute", key_func=client_ip)
async def submit(request: Request):
    ident = identity_of(request)
    if ident is None:
        return _error("not_signed_in", 401)
    try:
        body = SubmitBody.model_validate(await request.json())
    except (ValueError, ValidationError):
        return _error("invalid_request", 422)
    try:
        data = await internal_client.submit_verified_appeal(
            config.DASHBOARD_GUILD_ID, ident["uid"], ident["name"] or None, body.name, body.message.strip(),
        )
    except InternalAPIError as exc:
        if exc.status_code == 404:
            return _error("not_eligible", 404)
        if exc.status_code == 409:
            return _error("already_submitted", 409)
        if exc.status_code == 400:
            return _error("invalid_request", 422)
        return _error("unavailable", 503)
    return {"ok": True, "status": data.get("status", "pending")}


@router.post("/logout")
@limiter.limit("10/minute", key_func=client_ip)
async def logout(request: Request):
    response = JSONResponse({"ok": True})
    _clear_cookie(response, request, IDENTITY_COOKIE)
    return response
