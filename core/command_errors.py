"""
core/command_errors.py - the bot's prefix-command error handler.

discord.py's built-in Bot.on_command_error (which logs the traceback) does
NOTHING as soon as any `on_command_error` listener exists, and
cogs/diagnostics_collector.py registers one to count errors. So that listener
calls handle_command_error() to do what the default did, plus a short reply:

* CommandNotFound: ignored (no reply, no log; "!" is a common chat prefix).
* A command or cog with its own error handler (e.g. !shop's cooldown
  handler in cogs/fortnite_shop.py): left to it, same as discord.py's default.
* User errors (missing / bad argument, failed check, cooldown, DM-only
  refusals): a brief reply, logged at INFO without a traceback.
* Anything else: the full traceback is logged (logger "commands.errors") and
  the user gets a generic "that command failed" reply.

Replies never ping anyone (AllowedMentions.none(), no reply ping).
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

log = logging.getLogger("commands.errors")

_NO_PINGS = discord.AllowedMentions.none()

GENERIC_FAILURE = "Something went wrong running that command. It has been logged."


def user_message(ctx, error: Exception) -> str | None:
    """Short reply for an expected user error, or None if it isn't one."""
    prefix = getattr(ctx, "clean_prefix", None) or getattr(ctx, "prefix", None) or "!"
    command = getattr(ctx, "command", None)
    usage = ""
    if command is not None:
        signature = getattr(command, "signature", "") or ""
        usage = f" Usage: `{prefix}{command.qualified_name}{(' ' + signature) if signature else ''}`"
    if isinstance(error, commands.MissingRequiredArgument):
        return f"Missing `{error.param.name}`.{usage}"
    if isinstance(error, commands.TooManyArguments):
        return f"Too many arguments.{usage}"
    if isinstance(error, commands.BadArgument):  # also MemberNotFound, BadUnionArgument's parts, ...
        return f"{discord.utils.escape_mentions(str(error))}{usage}"
    if isinstance(error, (commands.BadUnionArgument, commands.BadLiteralArgument, commands.ArgumentParsingError)):
        return f"Couldn't read that.{usage}"
    if isinstance(error, commands.CommandOnCooldown):
        return f"Slow down, try again in {error.retry_after:.0f}s."
    if isinstance(error, commands.NoPrivateMessage):
        return "This command only works in the server."
    if isinstance(error, commands.DisabledCommand):
        return "That command is disabled."
    if isinstance(error, commands.MaxConcurrencyReached):
        return "That command is already running; try again in a moment."
    if isinstance(error, commands.CheckFailure):
        return "You can't use that command."
    return None


async def _reply(ctx, text: str) -> None:
    try:
        await ctx.send(text[:1900], allowed_mentions=_NO_PINGS)
    except discord.HTTPException as exc:
        log.debug("could not send command error reply: %s", exc)


async def handle_command_error(ctx, error: Exception) -> str:
    """Handle a prefix-command error. Returns what was done
    ("ignored" | "delegated" | "user_error" | "logged") for tests/diagnostics."""
    if isinstance(error, commands.CommandNotFound):
        return "ignored"
    command = getattr(ctx, "command", None)
    if command is not None and command.has_error_handler():
        return "delegated"
    cog = getattr(ctx, "cog", None)
    if cog is not None and cog.has_error_handler():
        return "delegated"

    text = user_message(ctx, error)
    name = command.qualified_name if command is not None else "(unknown)"
    if text is not None:
        log.info("command %s: %s: %s", name, type(error).__name__, error)
        await _reply(ctx, text)
        return "user_error"

    original = getattr(error, "original", error)
    log.error("Ignoring exception in command %s", name, exc_info=(type(original), original, original.__traceback__))
    await _reply(ctx, GENERIC_FAILURE)
    return "logged"
