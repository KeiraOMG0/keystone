"""Bot-side live hub (services/live_events.py): fan-out, bounded queues, producers, redaction."""

from __future__ import annotations

import asyncio

import pytest

from services import diagnostics_service
from services.live_events import LiveHub, TokenBucket

pytestmark = pytest.mark.asyncio

BIG_ID = 987654321098765432  # > 2**53: must leave as a string


def _drain(client):
    out = []
    while not client.queue.empty():
        out.append(client.queue.get_nowait())
    return out


async def test_fan_out_only_to_subscribed_clients_with_envelope():
    hub = LiveHub()
    a, b = hub.add_client(), hub.add_client()
    hub.subscribe(a, ["honeypot"])
    hub.subscribe(b, ["tickets"])
    hub.publish("honeypot", {"kind": "catch_created", "user_id": BIG_ID, "ticket_id": 7})
    (msg,) = _drain(a)
    assert _drain(b) == []
    assert msg["v"] == 1 and msg["type"] == "event" and msg["topic"] == "honeypot"
    assert isinstance(msg["ts"], int) and msg["seq"] == 1
    assert msg["data"]["user_id"] == str(BIG_ID)  # snowflake -> string
    assert msg["data"]["ticket_id"] == 7  # db ids stay numbers
    hub.publish("honeypot", {"kind": "x"})
    assert _drain(a)[0]["seq"] == 2  # per-topic sequence


async def test_unknown_topics_rejected_and_unsubscribe():
    hub = LiveHub()
    c = hub.add_client()
    accepted, rejected = hub.subscribe(c, ["stats", "nope", 5])
    assert accepted == ["stats"] and len(rejected) == 2
    hub.unsubscribe(c, ["stats"])
    assert c.topics == set()


async def test_bounded_queue_drops_oldest():
    hub = LiveHub(queue_size=3)
    c = hub.add_client()
    hub.subscribe(c, ["audit"])
    for i in range(10):
        hub.publish("audit", {"kind": "audit", "n": i})
    msgs = _drain(c)
    assert [m["data"]["n"] for m in msgs] == [7, 8, 9]  # newest kept, oldest dropped
    assert c.dropped == 7


async def test_publish_never_raises_and_ignores_no_subscribers():
    hub = LiveHub()
    hub.publish("honeypot", {"kind": "nobody"})  # no clients: no-op
    c = hub.add_client()
    hub.subscribe(c, ["honeypot"])

    class Boom(dict):
        def items(self):
            raise RuntimeError("unserialisable")

    hub.publish("honeypot", Boom())  # must not raise
    hub.publish("honeypot", object())  # type: ignore[arg-type]
    hub.publish("honeypot", {"ok": True})
    assert any(m["data"] == {"ok": True} for m in _drain(c))


async def test_module_level_publish_never_raises(monkeypatch):
    import services.live_events as le

    def boom(*a, **k):
        raise RuntimeError("hub exploded")

    monkeypatch.setattr(le.hub, "publish", boom)
    le.publish("honeypot", {"kind": "x"})  # swallowed


async def test_producers_run_only_while_subscribed():
    hub = LiveHub(intervals={"diagnostics": 0.01, "stats": 0.01})
    calls = {"diagnostics": 0, "stats": 0}

    async def diag():
        calls["diagnostics"] += 1
        return {"n": calls["diagnostics"]}

    async def stats():
        calls["stats"] += 1
        return {"total": 1}

    hub.set_producer("diagnostics", diag)
    hub.set_producer("stats", stats)
    await asyncio.sleep(0.05)
    assert calls == {"diagnostics": 0, "stats": 0}  # nobody watching: collector never runs
    assert hub.running_producers() == set()

    c = hub.add_client()
    hub.subscribe(c, ["diagnostics"])
    await asyncio.sleep(0.08)
    assert calls["diagnostics"] >= 2 and calls["stats"] == 0
    assert hub.running_producers() == {"diagnostics"}
    msgs = [m for m in _drain(c) if m["topic"] == "diagnostics"]
    assert msgs and msgs[0]["type"] == "snapshot"

    hub.unsubscribe(c, ["diagnostics"])  # last subscriber leaves
    await asyncio.sleep(0.03)
    frozen = calls["diagnostics"]
    await asyncio.sleep(0.08)
    assert calls["diagnostics"] == frozen and hub.running_producers() == set()

    hub.subscribe(c, ["diagnostics"])
    await asyncio.sleep(0.05)
    assert calls["diagnostics"] > frozen
    hub.remove_client(c)  # disconnect also stops it
    await asyncio.sleep(0.03)
    frozen = calls["diagnostics"]
    await asyncio.sleep(0.06)
    assert calls["diagnostics"] == frozen
    await hub.shutdown()


async def test_producer_failure_is_contained():
    hub = LiveHub(intervals={"stats": 0.01})
    n = {"i": 0}

    async def flaky():
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("boom")
        return {"total": n["i"]}

    hub.set_producer("stats", flaky)
    c = hub.add_client()
    hub.subscribe(c, ["stats"])
    await asyncio.sleep(0.08)
    assert any(m["data"].get("total") for m in _drain(c))  # kept going after the failure
    await hub.shutdown()


async def test_diagnostics_secrets_scrubbed_on_the_wire(monkeypatch):
    """A dump with planted secrets must not reach the wire."""
    monkeypatch.setenv("DISCORD_TOKEN", "planted-bot-token-VALUE-123456")
    monkeypatch.setenv("SOME_SERVICE_PASSWORD", "planted-password-VALUE-9999")
    hub = LiveHub()
    c = hub.add_client()
    hub.subscribe(c, ["diagnostics", "honeypot"])
    discord_token = ".".join(["MTIzNDU2Nzg5MDEyMzQ1Njc4OTA", "GabcdE", "abcdefghijklmnopqrstuvwxyz0123"])  # fake, built at runtime
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"
    dump = {
        "process": {"cmdline": ["python", "main.py", f"--token={discord_token}"]},
        "logs": {"tail": ["ERROR failed: Authorization: Bearer abcdef0123456789abcdef", f"jwt {jwt}"]},
        "config": {"note": "token is planted-bot-token-VALUE-123456 and planted-password-VALUE-9999"},
    }
    hub.publish("diagnostics", dump, kind="snapshot")
    (msg,) = _drain(c)
    wire = str(msg)
    for leaked in (
        "planted-bot-token-VALUE-123456", "planted-password-VALUE-9999", discord_token, jwt,
        "abcdef0123456789abcdef",
    ):
        assert leaked not in wire
    assert "<redacted>" in wire


async def test_diagnostics_dropped_if_scrubber_fails(monkeypatch):
    hub = LiveHub()
    c = hub.add_client()
    hub.subscribe(c, ["diagnostics"])

    def boom(_):
        raise RuntimeError("scrub failed")

    monkeypatch.setattr(diagnostics_service, "scrub_payload", boom)
    hub.publish("diagnostics", {"secret": "x"}, kind="snapshot")
    assert _drain(c) == []  # never sent raw


async def test_token_bucket_limits_and_refills():
    t = {"now": 0.0}
    b = TokenBucket(rate=1.0, burst=2, clock=lambda: t["now"])
    assert b.allow() and b.allow() and not b.allow()
    t["now"] = 1.5
    assert b.allow() and not b.allow()
