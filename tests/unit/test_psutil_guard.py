"""services/psutil_guard.py: psutil is never entered from two threads at once.

The bot crashed natively inside _psutil_windows.pyd (0xC0000005) while
Diagnostics ran psutil in several worker threads at once; every psutil entry
point in the bot is now serialized on one RLock.
"""

from __future__ import annotations

import threading
import time
import types
from collections import namedtuple

from services import diagnostics_service as ds
from services import psutil_guard

_Reading = namedtuple("_Reading", "value")  # psutil readings are namedtuples (_nt() needs _asdict)


def test_entry_points_are_serialized():
    import cogs.diagnostics_collector as collector
    import internal_api.routes.debug as debug

    for fn in (ds._runtime_heavy, ds.build_stack, ds.children_section, ds.system_section, ds._pot_proc_stats,
               collector._sample_process, debug._process_snapshot):
        assert hasattr(fn, "__wrapped__"), fn.__name__


def test_concurrent_calls_never_overlap(monkeypatch):
    active = 0
    peak = 0
    guard = threading.Lock()

    def slow(*_a, **_k):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with guard:
            active -= 1
        return _Reading(1)

    fake = types.SimpleNamespace(
        virtual_memory=slow, swap_memory=slow, cpu_freq=slow, boot_time=lambda: time.time() - 10,
        cpu_count=lambda logical=True: 4, cpu_percent=lambda *a, **k: 1.0, cpu_times=slow, cpu_stats=slow,
        disk_partitions=lambda all=False: [], disk_io_counters=slow, net_io_counters=slow,
        disk_usage=lambda p: types.SimpleNamespace(total=1, free=1, percent=0.0),
    )
    monkeypatch.setattr(ds, "psutil", fake)
    bot = types.SimpleNamespace(latency=0.05, shard_count=1, shard_id=None)
    errors: list[BaseException] = []

    def run():
        try:
            ds.system_section(bot, None)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert peak == 1


def test_system_section_never_calls_cpu_stats(monkeypatch):
    # Both faulthandler dumps of the 0xC0000005 crash ended in psutil.cpu_stats().
    called = []
    fake = types.SimpleNamespace(
        virtual_memory=lambda: _Reading(1), swap_memory=lambda: _Reading(1), cpu_freq=lambda: _Reading(1),
        boot_time=lambda: time.time() - 10, cpu_count=lambda logical=True: 4, cpu_percent=lambda *a, **k: 1.0,
        cpu_times=lambda: _Reading(1), cpu_stats=lambda: called.append(1) or _Reading(1),
        disk_partitions=lambda all=False: [], disk_io_counters=lambda: _Reading(1), net_io_counters=lambda: _Reading(1),
        disk_usage=lambda p: types.SimpleNamespace(total=1, free=1, percent=0.0),
    )
    monkeypatch.setattr(ds, "psutil", fake)
    out = ds.system_section(types.SimpleNamespace(latency=0.05, shard_count=1, shard_id=None), None)
    assert called == []
    assert "stats" not in out["cpu"]


def test_lock_is_reentrant():
    @psutil_guard.serialized
    def inner():
        return "ok"

    @psutil_guard.serialized
    def outer():
        return inner()

    assert outer() == "ok"
