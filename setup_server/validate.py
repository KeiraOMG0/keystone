"""
Read-only drift detector: compares the live guild against config.py and
reports what's missing, without changing anything.

Run standalone via `python setup_server.py --check`. Useful after manual
edits in the Discord UI to see what's drifted from the desired state.
"""

from __future__ import annotations

import discord

from setup_server.config import CATEGORIES, CHANNELS, ROLES
from setup_server.state import ServerState


def check_roles(guild: discord.Guild) -> list[str]:
    problems = []
    existing = {r.name for r in guild.roles}
    for spec in ROLES:
        if spec.name not in existing:
            problems.append(f"role '{spec.name}' is missing")
    return problems


def check_categories(guild: discord.Guild) -> list[str]:
    problems = []
    existing = {c.name for c in guild.categories}
    for spec in CATEGORIES:
        if spec.name not in existing:
            problems.append(f"category '{spec.name}' is missing")
    return problems


def check_channels(guild: discord.Guild) -> list[str]:
    problems = []
    for spec in CHANNELS:
        match = None
        for ch in guild.channels:
            if ch.name == spec.name and (ch.category.name if ch.category else None) == spec.category:
                match = ch
                break
        if match is None:
            problems.append(f"channel '{spec.name}' (category: {spec.category}) is missing")
    return problems


def check_state_consistency(guild: discord.Guild, state: ServerState) -> list[str]:
    """Flag entries in state.json that no longer point at a real object."""
    problems = []
    for name, role_id in state.roles.items():
        if guild.get_role(role_id) is None:
            problems.append(f"state.json role '{name}' ({role_id}) no longer exists on the guild")
    for name, cat_id in state.categories.items():
        ch = guild.get_channel(cat_id)
        if ch is None or not isinstance(ch, discord.CategoryChannel):
            problems.append(f"state.json category '{name}' ({cat_id}) no longer exists on the guild")
    for name, ch_id in state.channels.items():
        if guild.get_channel(ch_id) is None:
            problems.append(f"state.json channel '{name}' ({ch_id}) no longer exists on the guild")
    return problems


def run_validation(guild: discord.Guild, state: ServerState) -> dict[str, list[str]]:
    return {
        "roles": check_roles(guild),
        "categories": check_categories(guild),
        "channels": check_channels(guild),
        "state": check_state_consistency(guild, state),
    }


def print_report(report: dict[str, list[str]]) -> bool:
    """Prints the report; returns True if everything is clean."""
    total = sum(len(v) for v in report.values())
    if total == 0:
        print("No drift detected. Server matches desired state.")
        return True

    for section, problems in report.items():
        if not problems:
            continue
        print(f"\n[{section}]")
        for p in problems:
            print(f"  - {p}")
    print(f"\n{total} issue(s) found. Run without --check to repair.")
    return False
