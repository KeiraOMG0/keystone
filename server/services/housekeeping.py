"""
Dashboard-DB retention jobs (security audit 2026-10-30).

The Privacy Policy promises a dashboard login session is kept "until you log
out, or 30 days without using the dashboard", and error reports 30 days. An
expired session used to be deleted only when its cookie was presented again,
so abandoned sessions (user id, username, avatar, role ids) stayed in
server/dashboard.db forever (20 of 26 rows were past 30 days when audited), and
error reports were only pruned when a new one arrived.

`run_once()` deletes sessions idle longer than SESSION_MAX_AGE_SECONDS and
prunes error reports; `loop()` runs it at dashboard start and every
HOUSEKEEPING_INTERVAL_SECONDS (started from server/app.py's lifespan).
"""

from __future__ import annotations

import asyncio
import datetime
import logging

from sqlalchemy import delete

from server import config
from server.db import engine as db_engine
from server.db.models import Session

log = logging.getLogger(__name__)

HOUSEKEEPING_INTERVAL_SECONDS = 6 * 60 * 60


async def prune_expired_sessions() -> int:
    """Delete every session idle longer than SESSION_MAX_AGE_SECONDS (the same
    rule load_valid_session enforces when a cookie is presented)."""
    cutoff = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(
        seconds=config.SESSION_MAX_AGE_SECONDS
    )
    async with db_engine.async_session_factory() as db:
        result = await db.execute(delete(Session).where(Session.last_seen_at < cutoff))
        await db.commit()
    return result.rowcount or 0


async def run_once() -> dict:
    from server.services import error_reports

    out = {"sessions": 0, "error_reports": 0}
    try:
        out["sessions"] = await prune_expired_sessions()
    except Exception:
        log.warning("housekeeping: pruning expired sessions failed", exc_info=True)
    try:
        out["error_reports"] = await error_reports.prune()
    except Exception:
        log.warning("housekeeping: pruning error reports failed", exc_info=True)
    if out["sessions"] or out["error_reports"]:
        log.info("housekeeping: removed %d expired session(s), %d old error report(s)",
                 out["sessions"], out["error_reports"])
    return out


async def loop() -> None:
    while True:
        await run_once()
        await asyncio.sleep(HOUSEKEEPING_INTERVAL_SECONDS)
