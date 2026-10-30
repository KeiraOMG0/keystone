"""Both supervisor clients (bot: aiohttp, dashboard: httpx) against a real
in-process control server on an ephemeral port, plus the /restart cog helpers."""

from __future__ import annotations

import json
import socket
import time

import pytest

import config
import supervisor as sup
from cogs import supervisor_control as cog
from server import config as dash_config
from server.services import supervisor_client as dash_client
from services import supervisor_client as bot_client

SECRET = "client-test-secret-abcdef"


class FakeProc:
    pid = 31337

    def poll(self):
        return None


@pytest.fixture(autouse=True)
def _quiet(tmp_path, monkeypatch):
    monkeypatch.setattr(sup, "LOG_DIR", tmp_path)
    monkeypatch.setattr(sup, "alert", lambda *a, **k: None)


@pytest.fixture
def control(monkeypatch):
    services = []
    for name in ("bot", "dashboard", "tunnel"):
        s = sup.Service(name, ["never-run"], sup.ROOT)
        s.proc, s.started_at, s.starts = FakeProc(), time.time() - 5, 1
        s.kill_tree = lambda: None
        services.append(s)
    ctrl = sup.Controller(services, cooldown=30, secret=SECRET)
    server = sup.ControlServer(ctrl, SECRET, port=0)
    assert server.start()
    url = f"http://127.0.0.1:{server.port}"
    monkeypatch.setattr(config, "SUPERVISOR_CONTROL_URL", url)
    monkeypatch.setattr(config, "SUPERVISOR_CONTROL_SECRET", SECRET)
    monkeypatch.setattr(dash_config, "SUPERVISOR_BASE_URL", url)
    monkeypatch.setattr(dash_config, "SUPERVISOR_CONTROL_SECRET", SECRET)
    yield ctrl
    server.stop()


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# Bot client (aiohttp)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bot_client_status_restart_cooldown(control):
    status = await bot_client.get_status()
    assert [s["name"] for s in status["services"]] == ["bot", "dashboard", "tunnel"]
    result = await bot_client.request_restart("tunnel", reason="test", requested_by="Mod (1)", source="discord")
    assert result["accepted"] == ["tunnel"]
    with pytest.raises(bot_client.SupervisorCooldown) as exc:
        await bot_client.request_restart("tunnel")
    assert exc.value.retry_after_s >= 1
    req = control.requests.get_nowait()
    assert req["requested_by"] == "Mod (1)" and req["source"] == "discord" and req["reason"] == "test"


@pytest.mark.asyncio
async def test_bot_client_helper_defaults(control):
    await bot_client.request_restart("dashboard", reason="self-heal")
    req = control.requests.get_nowait()
    assert req["requested_by"] == "bot" and req["source"] == "bot"


@pytest.mark.asyncio
async def test_bot_client_errors(control, monkeypatch):
    with pytest.raises(bot_client.SupervisorError) as exc:
        await bot_client.request_restart("everything")  # type: ignore[arg-type]
    assert exc.value.status == 400 and exc.value.error == "invalid_target"
    monkeypatch.setattr(config, "SUPERVISOR_CONTROL_SECRET", "wrong")
    with pytest.raises(bot_client.SupervisorError) as exc:
        await bot_client.get_status()
    assert exc.value.status == 401


@pytest.mark.asyncio
async def test_bot_client_unavailable(monkeypatch):
    monkeypatch.setattr(config, "SUPERVISOR_CONTROL_URL", f"http://127.0.0.1:{_closed_port()}")
    monkeypatch.setattr(config, "SUPERVISOR_CONTROL_SECRET", SECRET)
    with pytest.raises(bot_client.SupervisorUnavailable):
        await bot_client.get_status()


# ---------------------------------------------------------------------------
# Dashboard client (httpx)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dashboard_client_roundtrip(control):
    status = await dash_client.get_status()
    assert status["supervisor"]["control"] is True
    result = await dash_client.request_restart("bot", requested_by="web (2)", reason=None)
    assert result["accepted"] == ["bot"]
    with pytest.raises(dash_client.SupervisorError) as exc:
        await dash_client.request_restart("bot", requested_by="web (2)")
    assert exc.value.status_code == 429 and exc.value.body["error"] == "cooldown"
    logs = await dash_client.get_logs("bot", 5)
    assert logs["service"] == "bot"


@pytest.mark.asyncio
async def test_dashboard_client_unavailable(monkeypatch):
    monkeypatch.setattr(dash_config, "SUPERVISOR_BASE_URL", f"http://127.0.0.1:{_closed_port()}")
    monkeypatch.setattr(dash_config, "SUPERVISOR_CONTROL_SECRET", SECRET)
    with pytest.raises(dash_client.SupervisorUnavailable):
        await dash_client.get_status()


# ---------------------------------------------------------------------------
# Cog helpers
# ---------------------------------------------------------------------------

def test_marker_fresh_is_returned_and_deleted(tmp_path):
    path = tmp_path / "m.json"
    cog.write_marker(cog.build_marker(42, 7, "bot", now=1000.0), path)
    assert "token" not in path.read_text(encoding="utf-8")
    data = cog.pop_marker(now=1010.0, path=path)
    assert data and data["channel_id"] == "42"
    assert cog.back_online_text(data, now=1012.0) == "Back online after 12s (restart requested by <@7>)."
    assert not path.exists()
    assert cog.pop_marker(now=1010.0, path=path) is None


def test_marker_stale_or_garbage_is_dropped(tmp_path):
    path = tmp_path / "m.json"
    cog.write_marker(cog.build_marker(42, 7, "bot", now=1000.0), path)
    assert cog.pop_marker(now=1000.0 + cog.MARKER_MAX_AGE_S + 1, path=path) is None
    assert not path.exists()
    path.write_text("{nope", encoding="utf-8")
    assert cog.pop_marker(now=1000.0, path=path) is None and not path.exists()
    path.write_text(json.dumps({"requested_at": 1000.0}), encoding="utf-8")
    assert cog.pop_marker(now=1001.0, path=path) is None
    # Old-format marker (interaction token, no channel) is discarded, and deleted.
    path.write_text(json.dumps({"requested_at": 1000.0, "application_id": "1", "token": "t"}), encoding="utf-8")
    assert cog.pop_marker(now=1001.0, path=path) is None and not path.exists()


def test_status_embed_text():
    status = {
        "supervisor": {"pid": 9, "version": "2.0", "started_at": "2026-10-13T10:00:00+00:00", "restart_cooldown_s": 30},
        "services": [
            {"name": "bot", "running": True, "pid": 11, "uptime_s": 3725, "restarts": 2, "health_fails": 0,
             "last_exit": {"reason": "exit code 1", "at": "2026-10-13T10:00:00+00:00"},
             "last_request": {"by": "Mod (1)", "source": "dashboard", "reason": None, "at": "2026-10-13T10:00:00+00:00"},
             "cooldown_remaining_s": 12.2},
            {"name": "dashboard", "running": False, "waiting_on_dependency": True, "restarts": 0},
            {"name": "tunnel", "running": False, "skip_reason": "port in use", "restarts": 0},
        ],
    }
    embed = cog.build_status_embed(status)
    fields = {f.name: f.value for f in embed.fields}
    assert "pid 11, up 1h 2m" in fields["Bot"] and "Restarts: 2" in fields["Bot"]
    assert "Last requested: Mod (1) via dashboard" in fields["Bot"] and "Cooldown: 13s" in fields["Bot"]
    assert "waiting for the bot's internal API" in fields["Dashboard"]
    assert "Not managed" in fields["Tunnel"]
    assert "v2.0" in embed.description
