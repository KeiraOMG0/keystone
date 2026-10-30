"""
internal_api — FastAPI skeleton embedded in the bot process.

This package exposes a small internal HTTP API (stats + permission checks)
that a future dashboard backend will call. It is NOT internet-facing; it
binds to a loopback address by default (see config.INTERNAL_API_HOST) and is
protected by a shared-secret bearer token (see internal_api.auth).

Nothing in this package should import cogs/ — routes talk to the live bot
instance via request.app.state.bot (see internal_api.deps).
"""
