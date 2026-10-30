"""
services/spam_detector.py — in-memory spam detection for cogs/anti_spam.py.

Pure logic (no Discord objects, no DB) so it is unit-testable with plain
values. The cog feeds every member message through SpamDetector.observe()
and acts on the verdict.

Rules (each one off when its count is 0):
  * flood      — `flood_messages` or more messages within `flood_seconds`,
                 in any channels.
  * duplicate  — the same text `duplicate_messages` or more times within
                 `duplicate_seconds`, across channels (the classic
                 compromised-account pattern). Text is compared casefolded
                 with whitespace collapsed. Messages with no text (only an
                 attachment/sticker) and prefix commands ("!skip" five times
                 is not spam) never count as duplicates, only toward flood.
  * mentions   — one message mentioning `mention_limit` or more distinct
                 users/roles.

A verdict lists the (channel_id, message_id) pairs to delete: the whole
flood window, every copy of the duplicated text, or the one mention message.
After a verdict the user's history is cleared and a COOLDOWN_SECONDS window
starts during which every further message from them is a `cooldown` verdict
(delete only, `first=False`); the cooldown is not extended by those
messages, so a member in delete-only mode is not muted indefinitely.

State is per user id (the bot runs in one guild), bounded per user
(HISTORY_MAX) and swept of idle users every SWEEP_EVERY observations. It is
lost on restart, which is fine for windows of seconds.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

COOLDOWN_SECONDS = 30.0
HISTORY_MAX = 100  # per user; above every configurable count
SWEEP_EVERY = 500  # observations between idle-user sweeps
COMMAND_PREFIX = "!"

_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class SpamRules:
    enabled: bool = True
    flood_messages: int = 6
    flood_seconds: int = 5
    duplicate_messages: int = 5
    duplicate_seconds: int = 30
    mention_limit: int = 6
    timeout_minutes: int = 10  # 0 = delete only, no timeout

    @classmethod
    def from_settings(cls, values: dict[str, Any]) -> "SpamRules":
        """From SettingsService.get_effective() output (antispam_* keys)."""
        return cls(
            enabled=bool(values["antispam_enabled"]),
            flood_messages=int(values["antispam_flood_messages"]),
            flood_seconds=int(values["antispam_flood_seconds"]),
            duplicate_messages=int(values["antispam_duplicate_messages"]),
            duplicate_seconds=int(values["antispam_duplicate_seconds"]),
            mention_limit=int(values["antispam_mention_limit"]),
            timeout_minutes=int(values["antispam_timeout_minutes"]),
        )

    @property
    def window_seconds(self) -> float:
        return float(max(self.flood_seconds, self.duplicate_seconds, 1))


@dataclass(frozen=True)
class SpamVerdict:
    rule: str  # "flood" | "duplicate" | "mentions" | "cooldown"
    detail: str  # one short sentence, safe to show staff
    messages: list[tuple[int, int]]  # (channel_id, message_id) to delete
    first: bool = True  # False = follow-up inside the cooldown: delete only


@dataclass
class _Seen:
    at: float
    channel_id: int
    message_id: int
    fingerprint: str | None


@dataclass
class _UserState:
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAX))
    cooldown_until: float = 0.0
    last_seen: float = 0.0


def fingerprint(content: str | None) -> str | None:
    """Comparable form of a message's text for the duplicate rule, or None
    when the message must not count as a duplicate (no text, a command)."""
    if not content:
        return None
    text = _WS_RE.sub(" ", content).strip().casefold()
    if not text or text.startswith(COMMAND_PREFIX):
        return None
    return text


class SpamDetector:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._users: dict[int, _UserState] = {}
        self._observed = 0

    def forget(self, user_id: int) -> None:
        """Drop everything known about a user (e.g. they turned out exempt)."""
        self._users.pop(user_id, None)

    def in_cooldown(self, user_id: int) -> bool:
        state = self._users.get(user_id)
        return state is not None and state.cooldown_until > self._clock()

    def observe(
        self,
        user_id: int,
        channel_id: int,
        message_id: int,
        content: str | None,
        mention_count: int,
        rules: SpamRules,
    ) -> SpamVerdict | None:
        now = self._clock()
        self._observed += 1
        if self._observed % SWEEP_EVERY == 0:
            self._sweep(now, rules.window_seconds)

        state = self._users.setdefault(user_id, _UserState())
        state.last_seen = now
        if state.cooldown_until > now:
            return SpamVerdict("cooldown", "Message during the anti-spam cooldown.",
                               [(channel_id, message_id)], first=False)

        seen = _Seen(now, channel_id, message_id, fingerprint(content))
        state.history.append(seen)
        cutoff = now - rules.window_seconds
        while state.history and state.history[0].at < cutoff:
            state.history.popleft()

        verdict = self._evaluate(state, seen, mention_count, rules, now)
        if verdict is not None:
            state.history.clear()
            state.cooldown_until = now + COOLDOWN_SECONDS
        return verdict

    @staticmethod
    def _evaluate(state: _UserState, seen: _Seen, mention_count: int, rules: SpamRules, now: float) -> SpamVerdict | None:
        if rules.mention_limit > 0 and mention_count >= rules.mention_limit:
            return SpamVerdict(
                "mentions", f"Mentioned {mention_count} users/roles in one message.",
                [(seen.channel_id, seen.message_id)],
            )

        if rules.duplicate_messages > 0 and seen.fingerprint is not None:
            cutoff = now - rules.duplicate_seconds
            copies = [s for s in state.history if s.at >= cutoff and s.fingerprint == seen.fingerprint]
            if len(copies) >= rules.duplicate_messages:
                channels = len({s.channel_id for s in copies})
                where = f" across {channels} channels" if channels > 1 else ""
                return SpamVerdict(
                    "duplicate",
                    f"Sent the same message {len(copies)} times in {rules.duplicate_seconds}s{where}.",
                    [(s.channel_id, s.message_id) for s in copies],
                )

        if rules.flood_messages > 0:
            cutoff = now - rules.flood_seconds
            recent = [s for s in state.history if s.at >= cutoff]
            if len(recent) >= rules.flood_messages:
                return SpamVerdict(
                    "flood", f"Sent {len(recent)} messages in {rules.flood_seconds}s.",
                    [(s.channel_id, s.message_id) for s in recent],
                )
        return None

    def _sweep(self, now: float, window: float) -> None:
        idle = [
            uid for uid, st in self._users.items()
            if st.cooldown_until <= now and now - st.last_seen > window
        ]
        for uid in idle:
            del self._users[uid]

    def tracked_users(self) -> int:
        return len(self._users)
