"""Section isolation, table allowlist, discord perms, full-dump leak test, collector cog."""

from __future__ import annotations

import json
import types

import discord
import pytest
from discord.ext import commands

import config
from database.connection import Database
from services import diagnostics_service as ds
from tests.unit.test_diagnostics_redaction import ALL_SECRETS, BOT_TOKEN, _set_secret_env


@pytest.fixture(autouse=True)
def _fresh_cache():
    ds.reset_cache()
    yield
    ds.reset_cache()


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    await d.connect()
    await d.executescript(
        """
        CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        INSERT INTO schema_migrations (version, name) VALUES (1, 'init'), (2, 'more');
        CREATE TABLE audit_log (id INTEGER PRIMARY KEY, action TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        INSERT INTO audit_log (action) VALUES ('a'), ('a'), ('b');
        CREATE TABLE mod_actions (id INTEGER PRIMARY KEY, action_type TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE tickets (id INTEGER PRIMARY KEY, status TEXT);
        INSERT INTO tickets (status) VALUES ('open');
        """
    )
    yield d
    await d.close()


def _guild(perms: discord.Permissions):
    role = types.SimpleNamespace(name="Bot", position=5)
    return types.SimpleNamespace(
        id=42, name="Test Guild", owner_id=7, member_count=10, members=[1, 2], channels=[], roles=[role, role],
        emojis=[], premium_tier=0, premium_subscription_count=0, chunked=True,
        me=types.SimpleNamespace(guild_permissions=perms, top_role=role),
    )


def _bot(db=None, guild=None):
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    bot.db = db
    bot.get_guild = lambda gid: guild  # type: ignore[assignment]
    return bot


# --- table allowlist ----------------------------------------------------

async def test_count_rows_allowlist(db):
    assert await ds.count_rows(db, "audit_log") == 3
    assert await ds.list_tables(db) == ["audit_log", "mod_actions", "schema_migrations", "tickets"]
    for bad in (
        "audit_log; DROP TABLE tickets", 'audit_log" --', "sqlite_master", "nope", "", "a b",
        "audit_log UNION SELECT 1", None,
    ):
        with pytest.raises(ValueError):
            await ds.count_rows(db, bad)  # type: ignore[arg-type]
    # explicit allowed list is honoured even for a real table
    with pytest.raises(ValueError):
        await ds.count_rows(db, "tickets", allowed=["audit_log"])
    assert await ds.count_rows(db, "tickets") == 1  # nothing was dropped


async def test_database_section_full(db):
    out = await ds.database_section(_bot(db))
    assert out["migrations"]["latest_version"] == 2 and out["migrations"]["applied_count"] == 2
    assert out["tables"]["audit_log"] == 3 and out["table_count"] == 4
    assert out["quick_check"] == "ok"
    assert out["audit_actions_24h"] == {"a": 2, "b": 1}
    assert out["tickets_by_status"] == {"open": 1}
    assert out["size_bytes"] is not None and out["heavy_as_of"]
    first = out["heavy_as_of"]
    again = await ds.database_section(_bot(db))
    assert again["heavy_as_of"] == first  # served from the ~45s cache


# --- section isolation ---------------------------------------------------

async def test_one_failing_section_does_not_fail_the_dump(db, monkeypatch):
    def boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ds, "cookies_section", boom)
    monkeypatch.setattr(ds, "logs_section", boom)
    dump = await ds.collect_diagnostics(_bot(db, _guild(discord.Permissions.all())), 42)
    assert dump["cookies"] == {"error": "RuntimeError: kaboom"}
    assert dump["logs"]["error"].startswith("RuntimeError")
    for ok in ("process", "children", "system", "discord", "database", "services", "config", "activity"):
        assert "error" not in dump[ok], (ok, dump[ok])
    assert dump["discord"]["guild"]["name"] == "Test Guild"
    assert dump["process"]["pid"] > 0 and dump["process"]["asyncio"]["loop_lag_ms"] >= 0


async def test_psutil_missing_degrades_gracefully(db, monkeypatch):
    monkeypatch.setattr(ds, "psutil", None)
    dump = await ds.collect_diagnostics(_bot(db, _guild(discord.Permissions.all())), 42)
    assert dump["psutil_available"] is False
    for k in ("process", "children", "system"):
        assert "psutil is not installed" in dump[k]["error"]
    assert dump["database"]["tables"]["audit_log"] == 3  # unrelated sections still work


async def test_missing_db_and_guild_are_section_errors():
    dump = await ds.collect_diagnostics(_bot(None, None), 42)
    assert "bot.db" in dump["database"]["error"]
    assert "not in cache" in dump["discord"]["guild"]["error"]


# --- discord permissions -------------------------------------------------

def test_permission_gap_is_listed():
    perms = discord.Permissions(manage_channels=True, kick_members=True, manage_messages=True,
                                manage_roles=True, moderate_members=True)
    out = ds.discord_section(_bot(None, _guild(perms)), 42)
    assert out["permissions"]["missing"] == ["Ban Members"]
    assert out["permissions"]["required"]["Kick Members"] is True
    admin = ds.discord_section(_bot(None, _guild(discord.Permissions(administrator=True))), 42)
    assert admin["permissions"]["missing"] == []


# --- full dump leak test -------------------------------------------------

async def test_full_dump_never_leaks_secrets(db, monkeypatch, tmp_path):
    _set_secret_env(monkeypatch)
    log = tmp_path / "bot_startup.log"
    log.write_text(f"2026 INFO started\n2026 ERROR login failed token={BOT_TOKEN}\nCookie: SID=leakycookie1234\n")
    monkeypatch.setattr(ds, "PROJECT_ROOT", str(tmp_path))
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        ".youtube.com\tTRUE\t/\tTRUE\t4102444800\tSID\tRAWCOOKIEVALUE_should_never_appear\n"
    )
    monkeypatch.setattr(config, "MUSIC_COOKIES_FILE", str(cookie_file), raising=False)
    bot = _bot(db, _guild(discord.Permissions.all()))
    bot.pot_server_service = types.SimpleNamespace(
        snapshot=lambda log_lines=10: {
            "state": "running", "log_path": str(tmp_path / "pot.log"),
            "last_error": f"bad auth Bearer abcdefghijklmnop123 {BOT_TOKEN}",
            "log_tail": [f"line with {ALL_SECRETS[1]} and {ALL_SECRETS[2]}", "ok"],
        }
    )
    dump = await ds.collect_diagnostics(bot, 42)
    blob = json.dumps(dump)
    for s in [*ALL_SECRETS, "RAWCOOKIEVALUE", "leakycookie1234", "abcdefghijklmnop123"]:
        assert s not in blob, f"leaked {s[:14]}"
    assert dump["cookies"]["cookie_count"] == 1 and dump["cookies"]["path"].endswith("cookies.txt")
    startup = next(f for f in dump["logs"]["files"] if f["key"] == "bot_startup")
    assert startup["level_counts"]["ERROR"] == 1
    assert dump["services"]["pot_server"]["state"] == "running"
    assert dump["config"]["env"]["DISCORD_TOKEN"] == "set"


# --- collector cog -------------------------------------------------------

async def test_collector_cog_counts_and_snapshot():
    from cogs.diagnostics_collector import DiagnosticsCollector

    bot = _bot()
    cog = DiagnosticsCollector(bot)
    cmd = types.SimpleNamespace(qualified_name="ping")
    await cog.on_command_completion(types.SimpleNamespace(command=cmd))
    await cog.on_command_error(types.SimpleNamespace(command=cmd), ValueError("secret detail"))
    await cog.on_app_command_completion(None, cmd)
    await cog.on_interaction(types.SimpleNamespace(type=discord.InteractionType.application_command))
    await cog.on_resumed()
    await cog.sample()
    snap = cog.snapshot()
    assert snap["commands"]["prefix_ok_total"] == 1 and snap["commands"]["prefix_error_total"] == 1
    assert snap["commands"]["slash_ok_total"] == 1
    assert snap["commands"]["recent_errors"][0]["error"] == "ValueError"
    assert "secret detail" not in json.dumps(snap)  # exception TYPE only
    assert snap["interactions"] == {"application_command": 1}
    assert snap["gateway"]["counts"]["resumed"] == 1
    assert len(snap["history"]) == 1 and "lag_ms" in snap["history"][0]
    assert bot.diagnostics_collector is cog


# --- stack scoping (fake psutil with lots of unrelated machine data) ------

def _fake_psutil(monkeypatch):
    import os

    me = os.getpid()
    A = lambda ip, port: types.SimpleNamespace(ip=ip, port=port)  # noqa: E731
    procs = {
        me: ("python.exe", ["python", "main.py"], None),
        2001: ("ffmpeg.exe", ["ffmpeg", "-i", "x", "--token", "SHOULDBEMASKED"], me),
        3001: ("node.exe", ["node", "pot.js"], 1),          # adopted external pot server
        4001: ("python.exe", ["uvicorn", "server.app:app"], 1),  # dashboard
        5001: ("cloudflared.exe", ["cloudflared", "tunnel", "run", "idlesys"], 1),
        5002: ("cloudflared.exe", ["cloudflared", "tunnel", "run", "someone-else"], 1),
        9001: ("Spotify.exe", ["spotify"], 1),
        9002: ("svchost.exe", ["svchost"], 1),
        9003: ("AnyDesk.exe", ["anydesk"], 1),
    }
    conns = [
        types.SimpleNamespace(status="LISTEN", laddr=A("127.0.0.1", 8766), raddr=None, pid=me, type=1),
        types.SimpleNamespace(status="LISTEN", laddr=A("127.0.0.1", 4416), raddr=None, pid=3001, type=1),
        types.SimpleNamespace(status="LISTEN", laddr=A("127.0.0.1", 8080), raddr=None, pid=4001, type=1),
        types.SimpleNamespace(status="LISTEN", laddr=A("127.0.0.1", 20241), raddr=None, pid=5001, type=1),
        types.SimpleNamespace(status="ESTABLISHED", laddr=A("10.0.0.5", 5555), raddr=A("1.2.3.4", 443), pid=me, type=1),
        types.SimpleNamespace(status="LISTEN", laddr=A("0.0.0.0", 57621), raddr=None, pid=9001, type=1),
        types.SimpleNamespace(status="LISTEN", laddr=A("0.0.0.0", 135), raddr=None, pid=9002, type=1),
        types.SimpleNamespace(status="LISTEN", laddr=A("0.0.0.0", 7070), raddr=None, pid=9003, type=1),
        types.SimpleNamespace(status="ESTABLISHED", laddr=A("10.0.0.5", 6000), raddr=A("8.8.8.8", 443), pid=9001, type=1),
    ]

    class P:
        def __init__(self, pid):
            if pid not in procs:
                raise KeyError(pid)
            self.pid, self.info = pid, {"pid": pid, "name": procs[pid][0]}

        def name(self): return procs[self.pid][0]
        def cmdline(self): return procs[self.pid][1]
        def ppid(self): return procs[self.pid][2]
        def children(self, recursive=False): return [P(k) for k, v in procs.items() if v[2] == self.pid]
        def is_running(self): return True
        def cpu_percent(self, interval=None): return 4.0
        def create_time(self): return 1_700_000_000.0
        def oneshot(self):
            import contextlib
            return contextlib.nullcontext()
        def memory_info(self): return types.SimpleNamespace(rss=1000, vms=2000)
        def status(self): return "running"
        def num_threads(self): return 3
        def exe(self): return "x.exe"
        def cwd(self): return "C:\bot"
        def username(self): return "u"
        def cpu_times(self): return None
        def io_counters(self): return None
        def num_ctx_switches(self): return None
        def num_handles(self): return 7
        def net_connections(self, kind="inet"): return [c for c in conns if c.pid == self.pid]

    fake = types.SimpleNamespace(
        Process=P, process_iter=lambda attrs=None: [P(k) for k in procs], net_connections=lambda kind="inet": conns,
        cpu_count=lambda logical=True: 4,
    )
    monkeypatch.setattr(ds, "psutil", fake)
    ds._PROCS.clear()
    return me


async def test_stack_scoping_excludes_unrelated_machine_data(monkeypatch):
    me = _fake_psutil(monkeypatch)
    monkeypatch.setenv("MUSIC_POT_SERVER_PORT", "4416")
    monkeypatch.setenv("DASHBOARD_PORT", "8080")
    monkeypatch.setattr(config, "INTERNAL_API_PORT", 8766, raising=False)
    bot = _bot()
    bot.pot_server_service = types.SimpleNamespace(
        pid=None, snapshot=lambda log_lines=10: {"state": "running", "port": 4416, "log_path": "p.log", "log_tail": [], "pid": None, "uptime_seconds": None}
    )
    stack = ds.build_stack(bot)
    assert set(stack["pids"]) == {me, 2001, 3001, 4001, 5001}
    assert stack["pids"][3001] == {"role": "pot server", "origin": "adopted external"}
    assert stack["pids"][2001]["origin"] == "bot child"
    assert stack["pids"][5001]["role"] == "tunnel"
    roles = {l["port"]: l["role"] for l in stack["listeners"]}
    assert roles == {4416: "pot server", 8080: "dashboard", 8766: "internal API", 20241: "tunnel metrics"}
    assert stack["connections_total"] == 5  # 4 listeners + 1 established owned by the bot; none of the others

    rows = ds.children_section(stack)["processes"]
    assert {r["pid"] for r in rows} == set(stack["pids"])
    ffm = next(r for r in rows if r["pid"] == 2001)
    assert "SHOULDBEMASKED" not in json.dumps(ffm) and ffm["role"] == "bot child"

    pot = ds.pot_server_section(bot, stack)
    assert pot["pid"] == 3001 and pot["adopted_external"] is True
    assert pot["uptime_seconds"] > 0 and pot["rss_bytes"] == 1000 and pot["cpu_percent"] is not None

    blob = json.dumps({"stack": stack, "rows": rows, "pot": pot})
    for other in ("Spotify", "svchost", "AnyDesk", "someone-else", "57621", "7070", "8.8.8.8", "9001", "9002"):
        assert other not in blob, other
    ds._PROCS.clear()


def test_build_stack_without_psutil_raises_clean_error(monkeypatch):
    monkeypatch.setattr(ds, "psutil", None)
    with pytest.raises(RuntimeError, match="psutil is not installed"):
        ds.build_stack(_bot())
