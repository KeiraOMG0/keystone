"""
Lockfile for the setup utility: maps human-readable names to Discord
snowflake IDs, scoped to a single guild.

This file is generated and safe to delete — if it's missing, every step
falls back to scanning the live guild by name to rebuild it (see
steps/*.py). That's the "repair" story: no state.json, no problem, it just
costs one extra API pass to rediscover everything.

Do not hand-edit the ID values. Do not commit this file if the repo is ever
made public alongside a token — it's not secret, but it is server-specific.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_STATE_PATH = Path(__file__).parent / "server_state.json"


@dataclass
class ServerState:
    guild_id: int | None = None
    roles: dict[str, int] = field(default_factory=dict)
    categories: dict[str, int] = field(default_factory=dict)
    channels: dict[str, int] = field(default_factory=dict)

    path: Path = field(default=DEFAULT_STATE_PATH, compare=False)

    @classmethod
    def load(cls, path: Path = DEFAULT_STATE_PATH) -> "ServerState":
        if not path.exists():
            return cls(path=path)
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            guild_id=data.get("guild_id"),
            roles={k: int(v) for k, v in data.get("roles", {}).items()},
            categories={k: int(v) for k, v in data.get("categories", {}).items()},
            channels={k: int(v) for k, v in data.get("channels", {}).items()},
            path=path,
        )

    def save(self) -> None:
        payload = {
            "guild_id": self.guild_id,
            "roles": self.roles,
            "categories": self.categories,
            "channels": self.channels,
        }
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def as_env_lines(self) -> list[str]:
        """Render the IDs the main bot cares about as .env-ready lines."""
        lines = []
        if "Unverified" in self.roles:
            lines.append(f"UNVERIFIED_ROLE_ID={self.roles['Unverified']}")
        if "Verified" in self.roles:
            lines.append(f"VERIFIED_ROLE_ID={self.roles['Verified']}")
        if "moderation-log" in self.channels:
            lines.append(f"LOG_CHANNEL_ID={self.channels['moderation-log']}")
        return lines
