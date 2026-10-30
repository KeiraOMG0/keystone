"""
Shared-secret bearer-token authentication for the internal API.

This is intentionally simple: the internal API is only ever meant to be
called by a trusted backend process running alongside the bot (later step),
not exposed directly to the internet. A single static secret compared in
constant time is sufficient for this trust boundary.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

import config


async def require_internal_secret(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency: validates the `Authorization: Bearer <secret>` header.

    Raises 401 if the header is missing/malformed, 403 if the secret doesn't match.
    Uses hmac.compare_digest for a constant-time comparison to avoid leaking
    timing information about the secret.
    """
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header. Expected 'Bearer <secret>'.",
        )

    provided = authorization.removeprefix("Bearer ").strip()

    if not hmac.compare_digest(provided, config.INTERNAL_API_SECRET):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid internal API secret.",
        )
