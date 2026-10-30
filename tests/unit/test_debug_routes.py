"""Owner-only Debug page + Fortnite dashboard routes: require_owner answers 404
to everyone but OWNER_ID, the owner id sent upstream comes from the session,
/api/me exposes is_owner, and /api/fortnite/* is permission-gated."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server import config
from server.app import app
from server.auth.dependencies import get_current_session, require_session
from server.db.models import Session
from server.services import internal_client
from server.services.internal_client import InternalAPIError

OWNER = 111222333444555666
OTHER = 999888777666555444
ORIGIN = config.FRONTEND_BASE_URL


def _session(user_id: int, member: bool = True) -> Session:
    s = Session()
    s.session_id = "sid"
    s.discord_user_id = user_id
    s.discord_username = "u"
    s.discord_avatar = None
    s.is_guild_member = member
    s.cached_role_ids = []
    return s


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "OWNER_ID", OWNER)
    calls: dict = {}

    async def run_debug_action(guild_id, *, action, channel_id, owner_id, params):
        calls["run"] = {"action": action, "channel_id": channel_id, "owner_id": owner_id, "params": params}
        return {"ok": True, "action": action, "message_ids": ["1"], "jump_url": "https://discord.com/x"}

    async def get_debug_actions(guild_id):
        return {"actions": [], "skipped": []}

    async def get_debug_health(guild_id):
        raise InternalAPIError("down")

    async def check_permissions_bulk(guild_id, user_id, perms):
        return {p: False for p in perms}

    async def check_is_staff(guild_id, user_id):  # /api/me also asks (is_staff); never the live API
        return False

    async def check_permission(guild_id, user_id, perm):
        calls.setdefault("perm", []).append(perm)
        return user_id == OWNER

    async def get_fortnite_status(guild_id):
        return {"config": {"enabled": False}}

    monkeypatch.setattr(internal_client, "run_debug_action", run_debug_action)
    monkeypatch.setattr(internal_client, "get_debug_actions", get_debug_actions)
    monkeypatch.setattr(internal_client, "get_debug_health", get_debug_health)
    monkeypatch.setattr(internal_client, "check_is_staff", check_is_staff)
    monkeypatch.setattr(internal_client, "check_permissions_bulk", check_permissions_bulk)
    monkeypatch.setattr(internal_client, "check_permission", check_permission)
    monkeypatch.setattr(internal_client, "get_fortnite_status", get_fortnite_status)

    async def get_debug_lab_options(guild_id):
        calls["lab_options"] = True
        return {"roles": [], "templates": []}

    async def debug_lab_permissions(guild_id, *, owner_id, user_id, role_ids):
        calls["lab_permissions"] = {"owner_id": owner_id, "user_id": user_id, "role_ids": role_ids}
        return {"ok": True}

    async def debug_lab_preview(guild_id, *, owner_id, payload):
        calls["lab_preview"] = {"owner_id": owner_id, **payload}
        if payload.get("kind") == "event":
            raise InternalAPIError("x", status_code=400, detail={"error": "lab_error", "message": "No message is bound to boost."})
        return {"messages": []}

    async def debug_lab_antispam(guild_id, *, owner_id, overrides, messages):
        calls["lab_antispam"] = {"owner_id": owner_id, "overrides": overrides, "messages": messages}
        return {"results": []}

    async def probe_get(path, timeout_s=10.0):
        calls.setdefault("lab_probe", []).append(path)
        return {"status": 200, "ms": 1.0, "bytes": 2, "error": None}

    monkeypatch.setattr(internal_client, "get_debug_lab_options", get_debug_lab_options)
    monkeypatch.setattr(internal_client, "debug_lab_permissions", debug_lab_permissions)
    monkeypatch.setattr(internal_client, "debug_lab_preview", debug_lab_preview)
    monkeypatch.setattr(internal_client, "debug_lab_antispam", debug_lab_antispam)
    monkeypatch.setattr(internal_client, "probe_get", probe_get)

    async def get_debug_errors(guild_id):
        calls["errors_get"] = True
        return {"started_at": "2026-10-30T00:00:00Z", "groups": [
            {"id": "b1", "source": "bot", "kind": "error", "name": "ValueError", "message": "bad", "count": 2,
             "first_seen": "2026-10-30T01:00:00Z", "last_seen": "2026-10-30T03:00:00Z", "paths": [],
             "user_agents": [], "users": [], "sample": {}}]}

    async def clear_debug_errors(guild_id, *, fingerprint=None):
        calls["errors_clear"] = fingerprint
        return {"removed": 1}

    async def list_groups(limit_rows=2000):
        return [{"id": "w1", "source": "browser", "kind": "render", "name": "TypeError", "message": "x",
                 "count": 1, "first_seen": "2026-10-30T02:00:00Z", "last_seen": "2026-10-30T02:00:00Z",
                 "paths": ["/dashboard"], "user_agents": [], "users": [], "sample": {}}]

    async def clear_reports(*, source=None, fingerprint_id=None):
        calls["reports_clear"] = {"source": source, "id": fingerprint_id}
        return 3

    from server.services import error_reports
    monkeypatch.setattr(internal_client, "get_debug_errors", get_debug_errors)
    monkeypatch.setattr(internal_client, "clear_debug_errors", clear_debug_errors)
    monkeypatch.setattr(error_reports, "list_groups", list_groups)
    monkeypatch.setattr(error_reports, "clear", clear_reports)
    c = TestClient(app)
    c.calls = calls  # type: ignore[attr-defined]
    yield c
    app.dependency_overrides.clear()


def _as(user_id: int | None, member: bool = True):
    sess = _session(user_id, member) if user_id is not None else None
    app.dependency_overrides[get_current_session] = lambda: sess
    if sess is not None:
        app.dependency_overrides[require_session] = lambda: sess
    else:
        app.dependency_overrides.pop(require_session, None)


@pytest.mark.parametrize("user", [None, OTHER])
@pytest.mark.parametrize(
    "method,path",
    [("GET", "/api/debug/actions"), ("GET", "/api/debug/health"), ("GET", "/api/debug/channels"),
     ("GET", "/api/debug/fortnite/preview"), ("POST", "/api/debug/run"),
     ("GET", "/api/debug/appeal-test"), ("POST", "/api/debug/appeal-test"), ("POST", "/api/debug/appeal-test/clear"),
     ("GET", "/api/debug/lab/options"), ("POST", "/api/debug/lab/permissions"), ("POST", "/api/debug/lab/preview"),
     ("POST", "/api/debug/lab/antispam"), ("GET", "/api/debug/lab/latency"),
     ("GET", "/api/debug/errors"), ("POST", "/api/debug/errors/clear"), ("GET", "/api/debug/errors/clear"),
     # wrong methods on lab routes fall through to the same catch-all 404
     ("POST", "/api/debug/lab/options"), ("GET", "/api/debug/lab/preview")],
)
def test_non_owner_gets_404(client, user, method, path):
    _as(user)
    r = client.request(method, path, json={"action": "plain_message", "channel_id": "1"}, headers={"Origin": ORIGIN})
    assert r.status_code == 404
    # Byte-identical to an unknown /api/* path, so the hidden page can't be probed.
    unknown = client.get("/api/definitely-not-a-route")
    assert unknown.status_code == 404
    assert r.content == unknown.content
    assert "run" not in client.calls
    assert not any(k.startswith(("lab", "errors", "reports")) for k in client.calls)


@pytest.mark.parametrize("user", [None, OTHER])
@pytest.mark.parametrize("path", ["/api/debug/run", "/api/debug/lab/permissions", "/api/debug/lab/preview", "/api/debug/lab/antispam"])
def test_non_owner_invalid_body_still_404(client, user, path):
    """The owner check runs before body validation: no 422 leak."""
    _as(user)
    unknown = client.get("/api/definitely-not-a-route").content
    for body in ({}, {"messages": "x"}, {"kind": 5}):
        r = client.post(path, json=body, headers={"Origin": ORIGIN})
        assert r.status_code == 404
        assert r.content == unknown
    r = client.post(path, content=b"{not json", headers={"Origin": ORIGIN, "Content-Type": "application/json"})
    assert r.status_code == 404 and r.content == unknown


def test_owner_not_member_gets_404(client):
    _as(OWNER, member=False)
    assert client.get("/api/debug/actions").status_code == 404


def test_owner_id_unset_hides_page(client, monkeypatch):
    monkeypatch.setattr(config, "OWNER_ID", 0)
    _as(0)
    assert client.get("/api/debug/actions").status_code == 404


def test_owner_can_run_and_owner_id_comes_from_session(client):
    _as(OWNER)
    assert client.get("/api/debug/actions").json() == {"actions": [], "skipped": []}
    r = client.post(
        "/api/debug/run",
        json={"action": "plain_message", "channel_id": "987654321098765432", "params": {"text": "hi"}, "owner_id": OTHER},
        headers={"Origin": ORIGIN},
    )
    assert r.status_code == 200
    assert client.calls["run"]["owner_id"] == OWNER
    assert client.calls["run"]["channel_id"] == 987654321098765432


def test_owner_bad_channel_id_rejected(client):
    _as(OWNER)
    r = client.post("/api/debug/run", json={"action": "x", "channel_id": "abc"}, headers={"Origin": ORIGIN})
    assert r.status_code == 400


def test_owner_bot_down_is_502(client):
    _as(OWNER)
    r = client.get("/api/debug/health")
    assert r.status_code == 502 and r.json()["detail"] == {"error": "internal_api_unavailable"}


def test_me_reports_is_owner(client):
    _as(OWNER)
    assert client.get("/api/me").json()["is_owner"] is True
    _as(OTHER)
    assert client.get("/api/me").json()["is_owner"] is False


def test_fortnite_routes_permission_gated(client):
    _as(OTHER)
    r = client.get("/api/fortnite/status")
    assert r.status_code == 403 and r.json()["detail"]["required_permission"] == "manage_schedules"
    _as(OWNER)
    assert client.get("/api/fortnite/status").status_code == 200


# ---------------------------------------------------------------------------
# Lab tab
# ---------------------------------------------------------------------------

def test_owner_lab_permissions_owner_from_session(client):
    _as(OWNER)
    r = client.post("/api/debug/lab/permissions", json={"user_id": "400000000000000001", "owner_id": str(OTHER)},
                    headers={"Origin": ORIGIN})
    assert r.status_code == 200
    assert client.calls["lab_permissions"] == {"owner_id": OWNER, "user_id": 400000000000000001, "role_ids": None}
    r = client.post("/api/debug/lab/permissions", json={"role_ids": ["200000000000000002"]}, headers={"Origin": ORIGIN})
    assert r.status_code == 200 and client.calls["lab_permissions"]["role_ids"] == [200000000000000002]
    # bad ID / nothing picked -> 400 for the owner
    assert client.post("/api/debug/lab/permissions", json={"user_id": "abc"}, headers={"Origin": ORIGIN}).status_code == 400
    assert client.post("/api/debug/lab/permissions", json={}, headers={"Origin": ORIGIN}).status_code == 400


def test_owner_lab_preview_and_error_passthrough(client):
    _as(OWNER)
    r = client.post("/api/debug/lab/preview",
                    json={"kind": "welcome", "user_id": "400000000000000001", "send_channel_id": "987654321098765432"},
                    headers={"Origin": ORIGIN})
    assert r.status_code == 200
    assert client.calls["lab_preview"]["owner_id"] == OWNER
    assert client.calls["lab_preview"]["send_channel_id"] == 987654321098765432
    r = client.post("/api/debug/lab/preview", json={"kind": "event", "user_id": "400000000000000001", "event_type": "boost"},
                    headers={"Origin": ORIGIN})
    assert r.status_code == 400
    assert r.json()["detail"] == {"error": "lab_error", "message": "No message is bound to boost."}
    r = client.post("/api/debug/lab/preview", json={"kind": "nuke", "user_id": "400000000000000001"}, headers={"Origin": ORIGIN})
    assert r.status_code == 422  # owner-only validation error


def test_owner_lab_antispam_and_latency(client):
    _as(OWNER)
    r = client.post("/api/debug/lab/antispam",
                    json={"overrides": {"antispam_flood_messages": 3}, "messages": [{"at": 0, "content": "hi"}]},
                    headers={"Origin": ORIGIN})
    assert r.status_code == 200
    assert client.calls["lab_antispam"]["owner_id"] == OWNER
    assert client.calls["lab_antispam"]["messages"][0]["content"] == "hi"
    assert client.post("/api/debug/lab/antispam", json={"messages": []}, headers={"Origin": ORIGIN}).status_code == 422
    r = client.get("/api/debug/lab/latency")
    assert r.status_code == 200
    body = r.json()
    assert len(body["results"]) == len(client.calls["lab_probe"]) and body["results"][0]["status"] == 200
    # Only fixed, read-only GET paths, never a path from the request.
    assert all(p == "/health" or p.startswith(f"/guilds/{config.DASHBOARD_GUILD_ID}/") for p in client.calls["lab_probe"])
    assert client.get("/api/debug/lab/options").status_code == 200


def test_owner_errors_merges_sources(client):
    _as(OWNER)
    r = client.get("/api/debug/errors")
    assert r.status_code == 200
    body = r.json()
    assert [g["id"] for g in body["groups"]] == ["b1", "w1"]  # newest last_seen first, across sources
    assert body["bot"] == {"available": True, "started_at": "2026-10-30T00:00:00Z"}
    assert body["retention_days"] == 30


def test_owner_errors_bot_down_still_lists_stored(client, monkeypatch):
    async def down(guild_id):
        raise InternalAPIError("down")

    monkeypatch.setattr(internal_client, "get_debug_errors", down)
    _as(OWNER)
    body = client.get("/api/debug/errors").json()
    assert [g["id"] for g in body["groups"]] == ["w1"] and body["bot"]["available"] is False


def test_owner_errors_clear(client):
    _as(OWNER)
    r = client.post("/api/debug/errors/clear", json={"source": "browser", "id": "w1"}, headers={"Origin": ORIGIN})
    assert r.json() == {"ok": True, "removed": 3, "bot_error": None}
    assert client.calls["reports_clear"] == {"source": "browser", "id": "w1"} and "errors_clear" not in client.calls
    r = client.post("/api/debug/errors/clear", json={}, headers={"Origin": ORIGIN})
    assert r.json()["removed"] == 4 and client.calls["reports_clear"]["source"] is None
    assert client.calls["errors_clear"] is None
    r = client.post("/api/debug/errors/clear", json={"source": "everything"}, headers={"Origin": ORIGIN})
    assert r.status_code == 400
