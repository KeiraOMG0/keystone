"""Smoke test the internal route with an ASGI client, a fake bot and a temp DB
(never the real bot.db / bot token / Discord)."""

from __future__ import annotations

import discord
import httpx
import pytest
from discord.ext import commands

import config
from database.connection import Database
from internal_api.app import create_app
from services import diagnostics_service as ds

SECRET = "test-internal-secret-value"


@pytest.fixture
async def client(tmp_path, monkeypatch):
    ds.reset_cache()
    monkeypatch.setattr(config, "INTERNAL_API_SECRET", SECRET)
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    await db.executescript(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP);"
        "INSERT INTO schema_migrations (version, name) VALUES (1, 'init');"
    )
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    bot.db = db
    app = create_app()
    app.state.bot = bot
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c
    await db.close()
    ds.reset_cache()


async def test_requires_secret(client):
    assert (await client.get("/guilds/1/diagnostics")).status_code == 401
    r = await client.get("/guilds/1/diagnostics", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 403


async def test_dump_shape_and_no_secret(client):
    r = await client.get("/guilds/1/diagnostics", headers={"Authorization": f"Bearer {SECRET}"})
    assert r.status_code == 200
    body = r.json()
    for section in ("process", "children", "system", "discord", "database", "services", "cookies", "config", "logs", "activity"):
        assert section in body
    assert body["database"]["migrations"]["applied_count"] == 1
    assert body["discord"]["guild"]["error"]  # guild 1 not cached in the fake bot: isolated, not a 500
    paths = [x["path"] for x in body["services"]["internal_api"]["routes"]]
    assert "/guilds/{guild_id}/diagnostics" in paths
    assert SECRET not in r.text
    assert body["activity"]["available"] is False  # collector cog not loaded in the fake bot
