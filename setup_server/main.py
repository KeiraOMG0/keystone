"""
Orchestrator + CLI for the standalone server setup utility.

Usage (from the repo root):
    python setup_server.py                 # apply: create/repair to match config.py
    python setup_server.py --check          # read-only drift report, no changes
    python setup_server.py --dry-run        # same as --check (Discord has no simulated-write API,
                                             # so this is an honest alias, not a true dry run)

Requires DISCORD_TOKEN in the environment / .env file.

The target guild is resolved automatically: if the bot is only in one
server, that server is used. If it's in more than one, set SETUP_GUILD_ID
in .env to disambiguate (this var is specific to this tool — the main bot
does not read it). SETUP_GUILD_ID is optional whenever the single-guild
case applies.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import discord

from setup_server.discord_client import get_token, make_client
from setup_server.state import ServerState
from setup_server.steps.categories import ensure_categories
from setup_server.steps.channels import ensure_channels
from setup_server.steps.permissions import apply_permissions
from setup_server.steps.roles import ensure_roles
from setup_server.validate import print_report, run_validation


def resolve_guild(client: discord.Client) -> discord.Guild:
    raw = os.environ.get("SETUP_GUILD_ID", "")
    if raw:
        try:
            guild_id = int(raw)
        except ValueError:
            raise RuntimeError(f"SETUP_GUILD_ID='{raw}' is not a valid integer guild ID.")
        guild = client.get_guild(guild_id)
        if guild is None:
            raise RuntimeError(
                f"SETUP_GUILD_ID={guild_id} but the bot is not in that server "
                f"(or hasn't finished syncing guilds). Guilds it can see: "
                f"{[(g.name, g.id) for g in client.guilds]}"
            )
        return guild

    if len(client.guilds) == 1:
        guild = client.guilds[0]
        print(f"SETUP_GUILD_ID not set — auto-selected the bot's only server: '{guild.name}' ({guild.id})")
        return guild

    if not client.guilds:
        raise RuntimeError("The bot is not in any servers. Invite it to the target server first.")

    raise RuntimeError(
        "SETUP_GUILD_ID is not set and the bot is in multiple servers, so the "
        "target is ambiguous. Set SETUP_GUILD_ID in .env to one of: "
        f"{[(g.name, g.id) for g in client.guilds]}"
    )


async def run(check: bool, dry_run: bool) -> int:
    token = get_token()
    state = ServerState.load()

    client = make_client()
    result = {"code": 0}

    @client.event
    async def on_ready():
        try:
            guild = resolve_guild(client)

            if state.guild_id is not None and state.guild_id != guild.id:
                print(
                    f"WARNING: server_state.json was built for guild {state.guild_id}, "
                    f"but the resolved target guild is {guild.id}. Refusing to mix "
                    f"state across servers — delete setup_server/server_state.json "
                    f"if this is intentional."
                )
                result["code"] = 1
                return

            state.guild_id = guild.id

            if check:
                report = run_validation(guild, state)
                clean = print_report(report)
                result["code"] = 0 if clean else 1
                return

            if dry_run:
                report = run_validation(guild, state)
                print_report(report)
                print("\n(dry run: no changes were made)")
                result["code"] = 0
                return

            all_actions: list[str] = []
            all_actions += await ensure_roles(guild, state)
            state.save()
            all_actions += await ensure_categories(guild, state)
            state.save()
            all_actions += await ensure_channels(guild, state)
            state.save()
            all_actions += await apply_permissions(guild, state)
            state.save()

            if all_actions:
                print("Actions taken:")
                for a in all_actions:
                    print(f"  - {a}")
            else:
                print("Nothing to do — server already matches desired state.")

            env_lines = state.as_env_lines()
            if env_lines:
                print("\nPaste these into the main bot's .env:")
                for line in env_lines:
                    print(f"  {line}")

            result["code"] = 0
        except Exception as exc:  # noqa: BLE001 - surface any failure and still exit cleanly
            print(f"ERROR: {exc}", file=sys.stderr)
            result["code"] = 1
        finally:
            await client.close()

    await client.start(token)
    return result["code"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision a new Discord server to match setup_server/config.py")
    parser.add_argument("--check", action="store_true", help="Report drift only, make no changes")
    parser.add_argument("--dry-run", action="store_true", help="Alias of --check for this release")
    args = parser.parse_args()

    code = asyncio.run(run(check=args.check, dry_run=args.dry_run))
    sys.exit(code)


if __name__ == "__main__":
    main()
