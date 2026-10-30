"""
services/psutil_guard.py — one lock for every psutil call in the bot process.

Between 2026-10-10 and 2026-10-22 the bot died 12 times with exit code
0xC0000005 (access violation), and Windows' "Application Error" event named
`_psutil_windows.pyd` at the same offset (0x26d4) every time. psutil was being
called from several threads at once: the Diagnostics dump runs its sections in
worker threads (`asyncio.to_thread`), the diagnostics sampler calls it on the
event loop every few seconds, the Debug page's health check and the PO-token
server's command-line check use it too. So every psutil call in the bot goes
through PSUTIL_LOCK. It is an RLock because guarded functions call each other
(children_section -> proc_basic -> cpu_pct).

Use `@serialized` on synchronous functions that call psutil (they normally run
in a worker thread, where waiting is harmless). Code on the event loop must not
block on it: use `PSUTIL_LOCK.acquire(blocking=False)` and skip the work when
the lock is busy (see cogs/diagnostics_collector.py).

If the crash comes back anyway, main.py has faulthandler enabled: the Python
stack of every thread is written to stderr (logs/supervisor/bot.log) at the
moment of the crash.
"""
from __future__ import annotations

import functools
import threading
from typing import Callable, TypeVar

T = TypeVar("T")

PSUTIL_LOCK = threading.RLock()


def serialized(fn: Callable[..., T]) -> Callable[..., T]:
    """Run `fn` while holding PSUTIL_LOCK."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with PSUTIL_LOCK:
            return fn(*args, **kwargs)

    return wrapper
