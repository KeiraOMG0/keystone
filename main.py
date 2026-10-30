"""
main.py — new entry point for the Discord bot (Phase 0 redesign).

bot.py remains untouched and fully functional as a fallback.
This file adds structured logging and database initialisation.
"""

import asyncio
import os
import sys

# ---------------------------------------------------------------------------
# Windows consoles default to a legacy codepage (e.g. cp1252) that can't
# encode emoji or many non-Latin scripts. Discord usernames/guild names
# routinely contain these, so any bare print()/log call can raise
# UnicodeEncodeError and crash a task loop. Force UTF-8 on stdout/stderr
# before anything else runs.
# ---------------------------------------------------------------------------
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr is not None and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# A native crash (e.g. Windows exit code 0xC0000005, access violation) kills
# the process with no Python traceback. faulthandler writes the Python stack
# of every thread to stderr at that moment; under supervisor.py stderr goes to
# logs/supervisor/bot.log (and the tail into the crash alert). See
# services/psutil_guard.py for the crash this was added for.
# ---------------------------------------------------------------------------
try:
    import faulthandler

    if sys.stderr is not None and not faulthandler.is_enabled():
        faulthandler.enable(file=sys.stderr, all_threads=True)
except Exception:  # no usable stderr (fileno) - run without it
    pass

# ---------------------------------------------------------------------------
# Logging must be set up before any other bot imports so that every module
# picks up the configured handlers from the start.
# ---------------------------------------------------------------------------
try:
    import config
    from core.logging_setup import setup_logging, get_logger

    setup_logging(config.LOG_LEVEL)
    log = get_logger("main")

except (ImportError, RuntimeError) as exc:
    # config.py raises RuntimeError if DISCORD_TOKEN is missing; core package
    # may also be missing. Write to a file too, since a frozen exe launched
    # by double-click closes its console before this can be read otherwise.
    msg = (
        f"[main] Startup failed before logging was configured: {exc}\n"
        "Make sure you have created a .env file (see README) and that all "
        "source files are present before running main.py.\n"
        "Alternatively, run bot.py as a fallback."
    )
    print(msg)
    try:
        base_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.getcwd()
        with open(os.path.join(base_dir, "bot_startup.log"), "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass
    sys.exit(1)

# ---------------------------------------------------------------------------
# Database imports — also optional-guarded so a missing module gives a clear
# message rather than an opaque traceback.
# ---------------------------------------------------------------------------
try:
    from database.connection import Database
    from database.migrations.runner import run_migrations
    from core.event_bus import EventBus
except ImportError as exc:
    log.error(
        "Could not import database modules: %s. "
        "Ensure the database package exists and dependencies are installed.",
        exc,
    )
    sys.exit(1)

import discord
from discord.ext import commands
import uvicorn

from internal_api.app import create_app
from core.bot_factory import create_bot

# ---------------------------------------------------------------------------
# Migration manifest
# Each entry: (version: int, name: str, sql_path: str)
# Paths are relative to the project root; os.path.dirname(__file__) keeps
# this correct regardless of the working directory the operator uses.
# ---------------------------------------------------------------------------
_ROOT = os.path.dirname(os.path.abspath(__file__))

# The only guild this bot operates in (it leaves any other): GUILD_ID in .env
# (required; main() refuses to start without it). server/config.py reads the
# same GUILD_ID for the dashboard; defined independently here since the bot
# process must not import the dashboard's config module. (.env is already
# loaded: config.py was imported above through internal_api.app.)
ALLOWED_GUILD_ID = int(os.environ.get("GUILD_ID") or "0")

MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "initial", os.path.join(_ROOT, "database", "migrations", "sql", "001_initial.sql")),
    (2, "templates", os.path.join(_ROOT, "database", "migrations", "sql", "002_templates.sql")),
    (3, "events", os.path.join(_ROOT, "database", "migrations", "sql", "003_events.sql")),
    (4, "managed", os.path.join(_ROOT, "database", "migrations", "sql", "004_managed.sql")),
    (5, "tickets", os.path.join(_ROOT, "database", "migrations", "sql", "005_tickets.sql")),
    (6, "advanced", os.path.join(_ROOT, "database", "migrations", "sql", "006_advanced.sql")),
    (7, "reaction_roles", os.path.join(_ROOT, "database", "migrations", "sql", "007_reaction_roles.sql")),
    (8, "seed_events", os.path.join(_ROOT, "database", "migrations", "sql", "008_seed_events.sql")),
    (9, "qotd", os.path.join(_ROOT, "database", "migrations", "sql", "009_qotd.sql")),
    (10, "qotd_role", os.path.join(_ROOT, "database", "migrations", "sql", "010_qotd_role.sql")),
    (11, "qotd_staff_ping", os.path.join(_ROOT, "database", "migrations", "sql", "011_qotd_staff_ping.sql")),
    (12, "birthdays", os.path.join(_ROOT, "database", "migrations", "sql", "012_birthdays.sql")),
    (13, "birthday_settings_v2", os.path.join(_ROOT, "database", "migrations", "sql", "013_birthday_settings_v2.sql")),
    (14, "birthday_ping_user", os.path.join(_ROOT, "database", "migrations", "sql", "014_birthday_ping_user.sql")),
    (15, "birthday_nullable_tz", os.path.join(_ROOT, "database", "migrations", "sql", "015_birthday_nullable_tz.sql")),
    (16, "qotd_approval_channel", os.path.join(_ROOT, "database", "migrations", "sql", "016_qotd_approval_channel.sql")),
    (17, "welcome_boost", os.path.join(_ROOT, "database", "migrations", "sql", "017_welcome_boost.sql")),
    (18, "warnings", os.path.join(_ROOT, "database", "migrations", "sql", "018_warnings.sql")),
    (19, "ticket_panel_purpose", os.path.join(_ROOT, "database", "migrations", "sql", "019_ticket_panel_purpose.sql")),
    (20, "ticket_panel_opened_by_index", os.path.join(_ROOT, "database", "migrations", "sql", "020_ticket_panel_opened_by_index.sql")),
    (21, "event_bindings_audience", os.path.join(_ROOT, "database", "migrations", "sql", "021_event_bindings_audience.sql")),
    (22, "mod_actions", os.path.join(_ROOT, "database", "migrations", "sql", "022_mod_actions.sql")),
    (23, "ticket_claim_chatlog_purge", os.path.join(_ROOT, "database", "migrations", "sql", "023_ticket_claim_chatlog_purge.sql")),
    (24, "guild_settings", os.path.join(_ROOT, "database", "migrations", "sql", "024_guild_settings.sql")),
    (25, "honeypot", os.path.join(_ROOT, "database", "migrations", "sql", "025_honeypot.sql")),
    (26, "honeypot_appeal_verified", os.path.join(_ROOT, "database", "migrations", "sql", "026_honeypot_appeal_verified.sql")),
    (27, "fortnite_shop", os.path.join(_ROOT, "database", "migrations", "sql", "027_fortnite_shop.sql")),
    (28, "honeypot_via_app", os.path.join(_ROOT, "database", "migrations", "sql", "028_honeypot_via_app.sql")),
    (29, "honeypot_scramble", os.path.join(_ROOT, "database", "migrations", "sql", "029_honeypot_scramble.sql")),
    (30, "honeypot_appeal_outcomes", os.path.join(_ROOT, "database", "migrations", "sql", "030_honeypot_appeal_outcomes.sql")),
    (31, "honeypot_test_cases", os.path.join(_ROOT, "database", "migrations", "sql", "031_honeypot_test_cases.sql")),
]

# ---------------------------------------------------------------------------
# Cog list — identical to bot.py so behaviour is consistent.
# ---------------------------------------------------------------------------
COGS = [
    "cogs.kick",
    "cogs.moderation",
    "cogs.info",
    "cogs.verify",
    "cogs.template_commands",
    "cogs.gateway",
    "cogs.logging_member",
    "cogs.logging_moderation",
    "cogs.logging_channel",
    "cogs.logging_role",
    "cogs.logging_server",
    "cogs.logging_voice",
    "cogs.welcome",
    "cogs.boost",
    "cogs.event_commands",
    "cogs.managed_commands",
    "cogs.tickets",
    "cogs.advanced_commands",
    "cogs.reaction_roles",
    "cogs.help_commands",
    "cogs.qotd",
    "cogs.birthdays",
    "cogs.birthday_config",
    "cogs.birthday_override",
    "cogs.birthday_io",
    "cogs.music",
    "cogs.honeypot",
    "cogs.anti_spam",  # after cogs.moderation: reuses its ModerationService
    "cogs.message_log",  # moderation message log in its own file (MESSAGE_LOG_PATH)
    "cogs.user_cache",  # remembered profiles of departed users, own file (USER_CACHE_PATH)
    "cogs.diagnostics_collector",
    "cogs.fortnite_shop",
    "cogs.live_roles",
    "cogs.supervisor_control",  # /restart via supervisor.py's control API
    "cogs.dependency_audit",  # weekly pip-audit + npm audit -> webhook when updates are needed; !depaudit
]


async def main() -> None:
    if not ALLOWED_GUILD_ID:
        raise SystemExit("GUILD_ID is not set. Put your Discord server's ID in .env (see .env.example).")
    # ------------------------------------------------------------------
    # Intents — same as bot.py
    # ------------------------------------------------------------------
    intents = discord.Intents.default()
    intents.members = True
    intents.message_content = True
    intents.presences = True

    # core/bot_factory.py: prefix "!", global allowed_mentions (no
    # @everyone/@here unless a send opts in) and a guild-only command tree.
    bot = create_bot(intents)

    # ------------------------------------------------------------------
    # Database setup
    # ------------------------------------------------------------------
    db = Database(config.DB_PATH)
    await db.connect()
    log.info("Connected to database at %s", config.DB_PATH)

    await run_migrations(db, MIGRATIONS)
    log.info("Database migrations complete")

    # Apply dashboard-saved settings (guild_settings) onto the config module
    # BEFORE any cog is loaded: cogs copy config.X at import, so this is what
    # makes a saved value take effect on restart. Validated values only;
    # owner_id is never applied. See services/settings_service.py.
    try:
        from database.repositories.settings_repo import SettingsRepository
        from services.settings_service import SettingsService

        await SettingsService(SettingsRepository(db)).apply_to_config(ALLOWED_GUILD_ID)
    except Exception:
        log.exception("Could not apply saved dashboard settings; using .env values")

    # Attach db to bot so cogs can reach it via bot.db
    bot.db = db  # type: ignore[attr-defined]

    # Attach event bus so gateway + event_commands can share it
    bot.event_bus = EventBus()  # type: ignore[attr-defined]

    # Construct the DB-backed PermissionService once, up front, and attach it
    # to the bot so every cog's setup() pulls the same instance via
    # bot.permission_service. This replaces the old pattern where each cog
    # built its own no-op PermissionService() and advanced_commands.py
    # monkey-patched a DB-backed one into already-loaded cogs after the fact
    # — that only worked for cogs loaded before advanced_commands and broke
    # entirely for cogs that called the free-function is_staff_interaction()
    # instead of holding a permission_service reference.
    from database.repositories.advanced_repo import PermissionRepository
    from services.permission_service import PermissionService
    from services.pot_server_service import PotServerService

    from database.repositories.audit_repo import AuditRepository
    from services.audit_service import AuditService

    # audit= makes every grant/revoke (slash commands + dashboard) write a
    # permission_grant / permission_revoke audit_log row.
    bot.permission_service = PermissionService(  # type: ignore[attr-defined]
        PermissionRepository(db), audit=AuditService(AuditRepository(db))
    )

    pot_server_service = PotServerService()
    bot.pot_server_service = pot_server_service  # type: ignore[attr-defined]  # used by cogs/music.py !musicstatus

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    @bot.event
    async def on_ready() -> None:
        log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
        log.info("Debug mode: %s", config.DEBUG)

        guilds = [f"{g.name} ({g.id})" for g in bot.guilds]
        log.info("Connected to guild(s): %s", ", ".join(guilds) if guilds else "none")

        # Enforce single-guild membership: leave any guild that isn't the
        # configured one. Runs on every on_ready (including reconnects), but
        # is a no-op after the first leave since bot.guilds won't contain
        # the foreign guild anymore.
        for guild in list(bot.guilds):
            if guild.id != ALLOWED_GUILD_ID:
                try:
                    await guild.leave()
                    log.info(
                        "Left guild %s (ID: %s) because it is not the configured guild (ID: %s)",
                        guild.name, guild.id, ALLOWED_GUILD_ID,
                    )
                except discord.HTTPException as exc:
                    log.warning(
                        "Failed to leave guild %s (ID: %s): %s",
                        guild.name, guild.id, exc,
                    )

        tree_cmds = [c.name for c in bot.tree.get_commands()]
        log.info("Commands in tree before sync (%d): %s", len(tree_cmds), sorted(tree_cmds))
        synced = await bot.tree.sync()
        log.info("Synced %d slash command(s) globally", len(synced))

        if not getattr(bot, "_pot_server_started", False):
            # Only latch the flag on success (or when intentionally disabled)
            # so a later on_ready reconnect can retry a failed start.
            if await pot_server_service.start() or not config.MUSIC_POT_SERVER_ENABLED:
                bot._pot_server_started = True  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Internal API — embedded FastAPI app, served in-process via uvicorn.
    # Not internet-facing (binds to config.INTERNAL_API_HOST, default
    # loopback); protected by a shared-secret bearer token (internal_api.auth).
    # ------------------------------------------------------------------
    internal_app = create_app()
    internal_app.state.bot = bot  # routes reach the live bot via request.app.state.bot

    uvicorn_config = uvicorn.Config(
        internal_app,
        host=config.INTERNAL_API_HOST,
        port=config.INTERNAL_API_PORT,
        log_level=config.LOG_LEVEL.lower(),
    )
    internal_server = uvicorn.Server(uvicorn_config)

    # ------------------------------------------------------------------
    # Run the bot + internal API together; close DB on shutdown regardless
    # of how we exit. uvicorn's Server.serve() is awaited directly (NOT its
    # run() wrapper, which calls asyncio.run() internally and would conflict
    # with the loop already running here).
    # ------------------------------------------------------------------
    try:
        async with bot:
            # Read by the owner-only debug page's health panel (internal_api/routes/debug.py).
            bot.expected_cogs = list(COGS)  # type: ignore[attr-defined]
            bot.cog_load_errors = {}  # type: ignore[attr-defined]
            for cog in COGS:
                try:
                    await bot.load_extension(cog)
                    log.info("Loaded cog: %s", cog)
                except Exception as e:
                    log.error("FAILED to load cog %s: %s", cog, e, exc_info=True)
                    bot.cog_load_errors[cog] = f"{type(e).__name__}: {e}"[:300]  # type: ignore[attr-defined]

            await asyncio.gather(
                bot.start(config.TOKEN),
                internal_server.serve(),
            )
    finally:
        pot_server_service.stop()
        await db.close()
        log.info("Database connection closed")


if __name__ == "__main__":
    asyncio.run(main())
