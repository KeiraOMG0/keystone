"""
cogs/help_commands.py — /help command explaining how the bot works in plain English.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from cogs.music import command_summary, render_music_help
from core.text_utils import EMBED_DESCRIPTION_LIMIT, split_message
from ui.paginator import Paginator
from services.permission_service import PermissionService


_PAGES = [
    {
        "title": "How the bot works — Overview",
        "color": 0xFFB3C1,
        "description": (
            "This bot is built around **templates** and **events**.\n\n"
            "A **template** is a message you design — it can have text, an embed, a colour, images, "
            "a footer, fields, anything. You make it once and reuse it anywhere.\n\n"
            "An **event** is something that happens on the server — someone joins, someone boosts, "
            "someone leaves. You tell the bot which template to send when that event fires.\n\n"
            "Everything is set up through slash commands. Nothing needs code changes."
        ),
    },
    {
        "title": "Step 1 — Make a template",
        "color": 0xFFB3C1,
        "description": (
            "**`/template create name:my-welcome`**\n"
            "Give it a name (letters, numbers, hyphens only).\n\n"
            "**`/template edit name:my-welcome`**\n"
            "Opens an editor with buttons. Click **Edit Content** to add text and a title, "
            "**Color** to set the colour, **Footer**, **Author**, **Image/Thumb**, or **Add Field**.\n\n"
            "**`/template preview name:my-welcome`**\n"
            "Shows you what it looks like. Note: placeholders like `{user}` show blank in preview "
            "because there's no real member — they fill in correctly when a real event fires.\n\n"
            "**`/template list`** — see all your templates\n"
            "**`/template delete name:my-welcome`** — delete one\n"
            "**`/template history name:my-welcome`** — see old versions\n"
            "**`/template rollback name:my-welcome version:3`** — go back to an old version"
        ),
    },
    {
        "title": "Step 2 — Connect a template to an event",
        "color": 0xFFB3C1,
        "description": (
            "**`/event set type:member_join channel:#welcome template:my-welcome`**\n"
            "Now whenever someone joins, the bot sends `my-welcome` to `#welcome`.\n\n"
            "Available event types:\n"
            "• `member_join` — someone joins the server\n"
            "• `member_leave` — someone leaves\n"
            "• `boost` — someone boosts\n\n"
            "**`/event test type:member_join`**\n"
            "Fires the event using *you* as the test member — great for checking it looks right.\n\n"
            "**`/event list`** — see what's configured\n"
            "**`/event clear type:member_join`** — remove a binding"
        ),
    },
    {
        "title": "Placeholders — make messages personal",
        "color": 0xFFB3C1,
        "description": (
            "Put these inside any template field and they fill in automatically when the message is sent. "
            "There are 100+ of them; these are the favourites:\n\n"
            "**Member**: `{mention}` `{user}` `{username}` `{avatar}` `{joindate:R}` `{accountage:R}` "
            "`{joinpositionordinal}` `{roles}` `{toprole}`\n"
            "**Server**: `{server}` `{membercount}` `{membercountordinal}` `{boostcount}` `{boostsneeded}` "
            "`{servericon}` `{ruleschannel}`\n"
            "**Moderation logs**: `{reason}` `{action}` `{duration}` `{expires}` `{caseid}` `{warningcount}` "
            "`{moderatormention}` `{source}`\n"
            "**Tickets**: `{ticketnumber}` `{ticketchannel}` `{staffmention}`\n"
            "**Time**: `{time:F}` `{timestamp_relative}` `{date:%A}`\n\n"
            "**Lookups**: `{channel:general}` links a channel, `{role:Moderator}` mentions a role, "
            "`{emoji:heart}` shows a server emoji, `{random:Hi;Hello;Hey}` picks one at random.\n"
            "**Backups**: `{nickname|user}` or `{reason|No reason given}` use the part after `|` when the "
            "first one is empty.\n\n"
            "Run **`/template placeholders`** for the full list by category, or open a template on the "
            "dashboard and click **Placeholders** to see every one with an example and insert it. "
            "`/template preview` fills them in using you as the member."
        ),
    },
    {
        "title": "Posted panels — managed messages",
        "color": 0xFFB3C1,
        "description": (
            "A **managed message** is a template posted as a permanent message in a channel. "
            "Whenever you edit the template, the posted message updates automatically.\n\n"
            "**`/template post name:my-info channel:#info`**\n"
            "Posts the template. From now on, editing the template updates that message.\n\n"
            "**`/managed list`** — see all posted panels\n"
            "**`/managed sync name:my-info`** — force an update right now\n"
            "**`/managed untrack message_id:123`** — stop tracking (message stays, just won't auto-update)"
        ),
    },
    {
        "title": "Ticket panels",
        "color": 0xFFB3C1,
        "description": (
            "Ticket panels let members open private support channels with a button click.\n\n"
            "**Setup:**\n"
            "`/ticket setup channel:#support open_category:Tickets`\n"
            "Then: `/ticket post panel_id:1`\n\n"
            "**Options:**\n"
            "• `transcript_channel` — where to send a log when a ticket closes\n"
            "• `template` — embed shown above the Open button\n"
            "• `max_open` — how many open tickets a member can have at once (default 1)\n"
            "• `naming` — channel name pattern, use `{number}` (default: `ticket-{number}`)\n"
            "• `button_label` / `button_emoji` — customise the button\n\n"
            "Inside a ticket: **Close** (staff only) locks the member out and deletes the channel 30 seconds later; "
            "the record and chat log stay on the dashboard. Open tickets with no messages for 7 days are closed automatically (weekly)."
        ),
    },
    {
        "title": "Reaction role panels",
        "color": 0xFFB3C1,
        "description": (
            "Let members self-assign roles by clicking buttons.\n\n"
            "**Setup:**\n"
            "`/reactionrole setup channel:#roles title:Pick your colour`\n"
            "`/reactionrole add panel_id:1 emoji:🎨 role:@Artist label:Artist`\n"
            "`/reactionrole add panel_id:1 emoji:🎵 role:@Musician label:Musician`\n"
            "`/reactionrole post panel_id:1`\n\n"
            "Use `exclusive:True` if members should only be able to hold **one** role from the panel at a time.\n\n"
            "**`/reactionrole list`** — see all panels\n"
            "**`/reactionrole delete panel_id:1`** — delete a panel\n"
            "**`/reactionrole remove panel_id:1 emoji:🎨`** — remove one button"
        ),
    },
    {
        "title": "Schedules — post on a timer",
        "color": 0xFFB3C1,
        "description": (
            "Send a template to a channel automatically on a schedule.\n\n"
            "**`/schedule add template:announcements channel:#general cron:0 9 * * 1`**\n"
            "Sends every Monday at 9am UTC.\n\n"
            "Cron format: `minute hour day month weekday`\n"
            "Examples:\n"
            "• `0 9 * * 1` — every Monday 9am\n"
            "• `0 18 * * 5` — every Friday 6pm\n"
            "• `0 12 1 * *` — 1st of every month at noon\n\n"
            "**`/schedule list`** — see all schedules\n"
            "**`/schedule toggle schedule_id:1 enabled:False`** — pause\n"
            "**`/schedule remove schedule_id:1`** — delete"
        ),
    },
    {
        "title": "Autorole — assign roles automatically",
        "color": 0xFFB3C1,
        "description": (
            "Automatically give members a role when something happens.\n\n"
            "**`/autorole add trigger:join role:@Member`**\n"
            "Gives @Member to everyone who joins.\n\n"
            "**`/autorole add trigger:boost role:@Booster`**\n"
            "Gives @Booster when someone boosts.\n\n"
            "**`/autorole add trigger:role_add trigger_role:@Unverified role:@Member delay:0`**\n"
            "When @Unverified is added (e.g. by another bot), also add @Member.\n\n"
            "Use `delay:` (seconds) to wait before assigning — e.g. `delay:300` waits 5 minutes.\n\n"
            "**`/autorole list`** — see all rules\n"
            "**`/autorole remove rule_id:1`** — delete a rule"
        ),
    },
    {
        "title": "Permissions — let other staff use commands",
        "color": 0xFFB3C1,
        "description": (
            "By default only server staff (mod/admin/owner roles) can use bot commands. "
            "You can grant specific permissions to other roles.\n\n"
            "**`/bot permissions grant role:@Helper permission:manage_templates`**\n\n"
            "Available permissions:\n"
            "• `manage_templates` — create/edit/delete templates\n"
            "• `manage_events` — configure event bindings\n"
            "• `manage_tickets` — manage ticket panels\n"
            "• `manage_schedules` — manage scheduled posts\n"
            "• `manage_autorole` — manage autorole rules\n"
            "• `view_audit` — view the audit log\n\n"
            "**`/bot permissions revoke role:@Helper permission:manage_templates`**\n"
            "**`/bot permissions list`** — see all grants\n"
            "**`/bot audit`** — paginated log of everything staff has done with the bot"
        ),
    },
    {
        "title": "Template import & export",
        "color": 0xFFB3C1,
        "description": (
            "You can share templates between servers or keep a backup.\n\n"
            "**`/template export name:my-welcome`**\n"
            "Sends the template as a JSON blob (or file if it's large).\n\n"
            "**`/template import data:{paste json here}`**\n"
            "Recreates the template from the JSON. Use `name_override:` to give it a different name.\n\n"
            "The exported format includes all embed data, fields, colours, and content — "
            "everything except which events or channels it's bound to."
        ),
    },
    {
        "title": "Birthdays — User commands",
        "color": 0xFFB3C1,
        "description": (
            "The birthday system announces members on their birthday at exactly midnight in their timezone.\n\n"
            "**`/birthday set date day:14 month:June`**\n"
            "Set your birthday. Add `timezone:Europe/London` to set your timezone at the same time.\n\n"
            "**`/birthday set timezone timezone:America/New_York`**\n"
            "Update your timezone after your birthday is already set.\n\n"
            "**`/birthday get`** — show your own birthday\n"
            "**`/birthday get member:@someone`** — show someone else's birthday\n\n"
            "**`/birthday show-nearest`**\n"
            "Lists birthdays within 7 days in the past and 14 days ahead.\n\n"
            "**`/birthday remove`**\n"
            "Removes your birthday from this server.\n\n"
            "Note: if the server has **add-only mode** enabled, you can set your birthday once "
            "but cannot edit it afterwards — ask a mod to update it for you."
        ),
    },
    {
        "title": "Birthdays — Staff setup",
        "color": 0xFFB3C1,
        "description": (
            "All config commands need the `manage_birthdays` bot permission (staff always have it). Also on the dashboard: Birthdays page.\n\n"
            "**`/config birthday-role role:@Birthday`**\n"
            "Role assigned to a member for the duration of their birthday.\n\n"
            "**`/config announce set-channel channel:#birthdays`**\n"
            "Where announcements are posted. Leave blank to clear.\n\n"
            "**`/config announce set-ping enabled:True`**\n"
            "Whether `{mention}` in the message pings the member or uses their display name.\n\n"
            "**`/config announce ping-user enabled:True`**\n"
            "Send a separate @mention ping before the announcement so the birthday person gets notified.\n\n"
            "**`/config announce set-message`**\n"
            "Opens an editor for the announcement message. Use `{mention}`, `{user}`, `{server}` as placeholders. Supports newlines and bullets.\n\n"
            "**`/config announce test member:@someone`** — sends a test announcement\n"
            "**`/config announce timers-reset`** — re-trigger today's birthday announcements\n"
            "**`/config set-timezone timezone:Europe/London`** — server fallback timezone\n"
            "**`/config add-only enabled:True`** — lock birthdays after first set\n"
            "**`/config private-confirms enabled:True`** — make user confirmations ephemeral\n"
            "**`/config check`** — show full config, permission checks, and scheduled count\n\n"
            "**`/birthdays export format:plaintext`** — download birthdays as .txt, .csv, or .ics\n"
            "**`/birthdays import file:`** — upload a BirthdayBot export .txt to bulk import"
        ),
    },
    {
        "title": "Birthdays — Override commands",
        "color": 0xFFB3C1,
        "description": (
            "Members with `manage_birthdays` can manage birthdays on behalf of other members.\n\n"
            "**`/override set-birthday member:@user day:3 month:March`**\n"
            "Sets or updates another member's birthday. Bypasses add-only mode.\n"
            "Optionally add `timezone:` — skipping it **removes** any previously set timezone.\n\n"
            "**`/override set-timezone member:@user timezone:Asia/Tokyo`**\n"
            "Updates another member's timezone without changing their birthday date.\n\n"
            "**`/override remove-birthday member:@user`**\n"
            "Removes a member's birthday entirely.\n\n"
            "**How the scheduler works:**\n"
            "On startup and weekly, the bot calculates the exact UTC moment when midnight arrives "
            "in each member's timezone on their birthday, then sleeps until that instant. "
            "Announcements fire within a few seconds of their local midnight — no polling delay."
        ),
    },
    {
        "title": "QOTD — Setup & channel config",
        "color": 0xFFB3C1,
        "description": (
            "The bot posts one AI-generated icebreaker question per day. Before it goes public, "
            "staff review and approve it in the Staff Lounge.\n\n"
            "**First-time setup (run once):**\n"
            "`/qotd setchannel channel:#qotd`\n"
            "Sets where the question posts publicly. Default time is 9:00 UTC.\n\n"
            "**Set a specific time:**\n"
            "`/qotd setchannel channel:#qotd hour:14 minute:30`\n"
            "Posts at 14:30 UTC every day. Hour is 0–23, minute is 0–59.\n\n"
            "**Check what's configured:**\n"
            "`/qotd status`\n"
            "Shows the channel, post time, AI model in use, last post time, "
            "and whether there's a question waiting for approval right now.\n\n"
            "Note: there is no command to customise the embed appearance yet — "
            "it uses a fixed blurple embed with the question as the description."
        ),
    },
    {
        "title": "QOTD — Approval flow",
        "color": 0xFFB3C1,
        "description": (
            "Every day at the configured time, the bot generates a question and posts it "
            "to the **Staff Lounge** with four buttons. Staff choose what happens next.\n\n"
            "**✅ Approve**\n"
            "Posts the question to the public channel immediately. "
            "Recorded in history so it won't be repeated.\n\n"
            "**❌ Deny**\n"
            "Discards the question. Nothing gets posted today. "
            "The bot won't try again until tomorrow.\n\n"
            "**🔄 Regenerate**\n"
            "Asks the AI for a completely new question and updates the approval message in place. "
            "You can regenerate as many times as you like before approving.\n\n"
            "**✏️ Custom Question**\n"
            "Opens a text box where you type your own question. "
            "Whatever you type gets posted to the public channel instead of the AI's version.\n\n"
            "The approval message expires after 23 hours if nobody acts on it."
        ),
    },
    {
        "title": "QOTD — Manual commands",
        "color": 0xFFB3C1,
        "description": (
            "These commands let you post or preview questions outside the daily schedule.\n\n"
            "**`/qotd preview`**\n"
            "Generates a question right now and shows it to you privately. "
            "Nothing is posted. The question is cached — use `/qotd now` to post it.\n\n"
            "**`/qotd regenerate`**\n"
            "Throws away your cached preview and generates a fresh one. "
            "Use this if you don't like what the AI came up with.\n\n"
            "**`/qotd now`**\n"
            "Posts a question to the public channel immediately, bypassing the approval flow. "
            "If you ran `/qotd preview` first it posts that cached question; "
            "otherwise it generates a new one on the spot.\n\n"
            "**`/qotd test`**\n"
            "Shows you two things privately: the raw text the AI actually returned before any "
            "cleanup, and a preview of the final public embed. Useful for checking if the AI "
            "is behaving or if the validation is being too aggressive."
        ),
    },
    {
        "title": "QOTD — Choosing an AI model",
        "color": 0xFFB3C1,
        "description": (
            "**`/qotd setmodel`**\n"
            "Opens a drop-down menu listing every supported model with a description. "
            "Pick one and it saves immediately.\n\n"
            "**Available models:**\n"
            "• **Llama 3.2 3B** — fastest, most reliable, default. ~2 GB VRAM.\n"
            "• **Llama 3.1 8B** — better variety and creativity. ~5 GB VRAM.\n"
            "• **Phi-4 Mini** — Microsoft's compact model, very clean output. ~2.5 GB VRAM.\n"
            "• **Gemma 2 9B** — Google's model, excellent topic variety. ~5.4 GB VRAM.\n"
            "• **Phi-4 14B** — highest quality but slowest. ~9 GB VRAM.\n\n"
            "If the AI fails after 3 attempts (model offline, bad output, etc.) the bot "
            "automatically falls back to a built-in list of 50 hand-written icebreaker questions "
            "and picks one that hasn't been posted recently.\n\n"
            "Image and coding models (llava, bakllava, codegemma, etc.) are excluded — "
            "they don't generate good conversation questions."
        ),
    },
    {
        "title": "Fortnite item shop",
        "color": 0x2B8CFF,
        "description": (
            "The bot posts the Fortnite item shop once a day, a few minutes after the "
            "00:00 UTC rotation, and never posts the same shop twice (even across restarts).\n\n"
            "**Anyone**\n"
            "**`!shop`** — browse today's shop, one item per page with its picture, price and rarity. "
            "Use the buttons or the section menu to jump around.\n"
            "**`/fortnite wishlist add item:renegade raider`** — get pinged in the daily post when a "
            "matching item is in the shop (part of a name works).\n"
            "**`/fortnite wishlist remove`** / **`/fortnite wishlist list`** — manage your wishlist.\n"
            "**`/fortnite status`** — current settings, last post and next post.\n\n"
            "**Needs the `manage_schedules` permission** (staff always can)\n"
            "**`/fortnite channel set channel:#shop`** / **`/fortnite channel clear`** — where it posts.\n"
            "**`/fortnite enable`** / **`/fortnite disable`** — turn the daily post on or off.\n"
            "**`/fortnite jamtracks enabled:True`** — list every Jam Track instead of one summary line.\n"
            "**`/fortnite pingrole role:@Shop`** — ping a role with the daily post (leave empty to clear; "
            "@everyone and bot roles are refused).\n"
            "**`/fortnite postnow`** — post today's shop right away. It does not count as the daily post.\n\n"
            "The same settings are on the dashboard under **Fortnite shop**."
        ),
    },
]


_DEV_PAGES = [
    {
        "title": "Architecture overview",
        "color": 0x5865F2,
        "description": (
            "```\n"
            "cogs/        — slash command handlers only, no business logic\n"
            "services/    — all business logic, no raw SQL\n"
            "core/        — rendering, placeholders, event bus, sync queue\n"
            "database/\n"
            "  connection.py      — aiosqlite wrapper\n"
            "  migrations/        — versioned SQL runner\n"
            "  repositories/      — one file per table group, all SQL lives here\n"
            "ui/          — discord.ui Views and Modals\n"
            "tests/       — pytest + pytest-asyncio\n"
            "```\n\n"
            "**Layer rules (never break):**\n"
            "• Cogs → Services only\n"
            "• Services → Repositories only\n"
            "• Core → no database imports\n"
            "• Repositories → Database class only"
        ),
    },
    {
        "title": "Permission system internals",
        "color": 0x5865F2,
        "description": (
            "**File:** `services/permission_service.py`\n\n"
            "Staff come from `.env`: `OWNER_ID` (one user) and `STAFF_ROLE_IDS` (comma-separated role IDs), "
            "read by `config.py` into `DEFAULT_STAFF_ROLE_IDS`. Staff always pass every check, regardless "
            "of DB grants:\n"
            "```ini\n"
            "OWNER_ID=<your user id>\n"
            "STAFF_ROLE_IDS=<owner role id>,<admin role id>\n"
            "```\n"
            "DB grants (from `/bot permissions grant`) layer on top via `permission_grants` table. "
            "`has_permission()` checks hard-coded staff first, then DB grants.\n\n"
            "When `advanced_commands` loads, it replaces the `permission_service` on every already-loaded cog "
            "with the DB-backed version via `bot.get_cog()`."
        ),
    },
    {
        "title": "Placeholder engine",
        "color": 0x5865F2,
        "description": (
            "**File:** `core/placeholder_engine.py`\n\n"
            "Replaces `{key}`, `{key:argument}` and `{key|fallback}` from a registry of callables "
            "that carry metadata (category, description, where it works) for `/template placeholders` "
            "and the dashboard's Placeholders panel.\n\n"
            "`RenderContext` carries `guild`, `member`, `channel`, `actor`, `ticket_number` and an `extra` "
            "dict (reason, action, case_id, duration_minutes, purge_count, ... filled by the mod logs).\n\n"
            "To add a new placeholder:\n"
            "```python\n"
            "register_placeholder(\n"
            "    \"mykey\",\n"
            "    lambda ctx: ctx.member.something if ctx.member else \"\",\n"
            "    category=\"Member\", where=MEMBER, description=\"What it shows.\",\n"
            ")\n"
            "```\n"
            "Unknown keys are left as-is (not stripped). Previews use `sample_context()` "
            "(the viewer as member, example moderation data)."
        ),
    },
    {
        "title": "Template renderer & validator",
        "color": 0x5865F2,
        "description": (
            "**Files:** `core/renderer.py`, `core/validator.py`\n\n"
            "`TemplateRenderer.render_message(template_data, ctx)` returns:\n"
            "```python\n"
            "{\"content\": str | None, \"embeds\": list[discord.Embed]}\n"
            "```\n"
            "`template_data` is the full dict from `TemplateRepository.get_full()` — "
            "includes `embeds` list, each with a `fields` list.\n\n"
            "`TemplateValidator` enforces Discord limits (title ≤256, description ≤4096, etc.) "
            "and validates URLs before setting them on embeds.\n\n"
            "The renderer skips embeds with no title, description, or fields — they render to `None` "
            "and are filtered out of the output list."
        ),
    },
    {
        "title": "Event bus",
        "color": 0x5865F2,
        "description": (
            "**File:** `core/event_bus.py`\n\n"
            "Simple pub/sub. `bus.subscribe(event_type, async_handler)` registers a coroutine. "
            "`bus.publish(event_type, **kwargs)` calls all handlers with those kwargs, "
            "catching and logging any exceptions so one bad handler can't break others.\n\n"
            "`gateway.py` publishes to the bus on Discord gateway events:\n"
            "```\n"
            "on_member_join   → bus.publish(\"member_join\", member=member)\n"
            "on_member_remove → bus.publish(\"member_leave\", member=member)\n"
            "on_message (boost type) → bus.publish(\"boost\", member=author)\n"
            "```\n"
            "`EventService` subscribes during `__init__` and handles rendering + sending.\n\n"
            "`bot.event_bus` is attached in `main.py` before cogs load."
        ),
    },
    {
        "title": "Sync queue (managed messages)",
        "color": 0x5865F2,
        "description": (
            "**File:** `core/sync_queue.py`\n\n"
            "After any template edit, the template ID is enqueued via `bot.sync_queue.enqueue(id)`. "
            "The queue worker debounces for **2 seconds** — if more edits arrive within that window "
            "they're deduplicated, then the handler fires once per unique template ID.\n\n"
            "The handler is `ManagedMessageService.sync_template(template_id)` which:\n"
            "1. Fetches all `managed_messages` rows for that template\n"
            "2. Re-renders the template for each message's guild/channel\n"
            "3. Calls `message.edit()` on each Discord message\n"
            "4. Marks `last_synced_at` or `sync_failed` in the DB\n\n"
            "If a message is deleted from Discord, the `NotFound` exception removes the DB record automatically."
        ),
    },
    {
        "title": "Persistent views (tickets & reaction roles)",
        "color": 0x5865F2,
        "description": (
            "Discord persistent views survive bot restarts by using **stable `custom_id` strings**.\n\n"
            "Format used:\n"
            "• Ticket panel open button: `ticket_open:{panel_id}`\n"
            "• Ticket close: `ticket_close:{ticket_id}`\n"
            "• Ticket claim: `ticket_claim:{ticket_id}`\n"
            "• Ticket verify & close: `ticket_verify_close:{ticket_id}`\n"
            "• Reaction role: `rr:{panel_id}:{role_id}`\n\n"
            "On startup, each cog's `setup()` queries the DB for all posted panels/messages "
            "and calls `bot.add_view(view, message_id=...)` to re-register them. "
            "Without this, buttons would show 'This interaction failed' after a restart."
        ),
    },
    {
        "title": "Scheduler internals",
        "color": 0x5865F2,
        "description": (
            "**File:** `services/scheduler_service.py`\n\n"
            "Runs an asyncio task that ticks every **30 seconds**. Each tick queries:\n"
            "```sql\n"
            "SELECT * FROM scheduled_sends\n"
            "WHERE enabled = 1 AND next_run_at <= now\n"
            "```\n"
            "For each due row it renders the template and sends it, then calls `croniter` "
            "to compute the next run time and writes it back to `next_run_at`.\n\n"
            "**Cron format (UTC):** `minute hour day month weekday`\n"
            "```\n"
            "0 9 * * 1     every Monday 9am\n"
            "0 18 * * 5    every Friday 6pm\n"
            "30 12 1 * *   1st of month at 12:30pm\n"
            "0 */6 * * *   every 6 hours\n"
            "```\n"
            "Croniter is installed in the venv: `pip install croniter`"
        ),
    },
    {
        "title": "Database — migrations & repositories",
        "color": 0x5865F2,
        "description": (
            "**Runner:** `database/migrations/runner.py`\n"
            "Tracks applied versions in `schema_migrations` table. "
            "Add a new migration by adding a tuple to `MIGRATIONS` in `main.py`.\n\n"
            "**Migrations so far:**\n"
            "1 — initial (guilds)\n"
            "2 — templates (message_templates, template_embeds, embed_fields, template_versions, audit_log)\n"
            "3 — events (event_bindings, event_binding_templates)\n"
            "4 — managed (managed_messages, template_components)\n"
            "5 — tickets (ticket_panels, ticket_panel_roles, tickets)\n"
            "6 — advanced (permission_grants, scheduled_sends, autorole_rules)\n"
            "7 — reaction_roles (reaction_role_panels, reaction_role_entries)\n"
            "8–11 — seed_events, qotd, qotd_role, qotd_staff_ping\n"
            "12 — birthdays (birthday_settings, birthdays)\n"
            "13 — birthday_settings_v2 (add_only, private_confirms columns)\n\n"
            "**Repo pattern:** each repo takes a `Database` instance, exposes typed async methods, "
            "no SQL outside repo files. `BaseRepository` just holds `self._db`."
        ),
    },
    {
        "title": "Birthday system internals",
        "color": 0x5865F2,
        "description": (
            "**Files:** `cogs/birthdays.py` · `services/birthday_service.py` · `database/repositories/birthday_repo.py`\n\n"
            "**Scheduler — no polling:**\n"
            "`_schedule_guild(guild_id)` is called on startup and after any data change. "
            "For each upcoming birthday it calls `next_celebration(...)` (services/birthday_service.py) "
            "which returns the UTC datetime when midnight arrives in that member's timezone on "
            "their birthday (Feb 29 birthdays fire on Feb 28 in non-leap years). An `asyncio.Task` is created that "
            "`await asyncio.sleep(delay)` until that instant, then fires `_fire_birthday()`. "
            "Precision: within a few seconds of actual local midnight.\n\n"
            "**Task registry:** `self._scheduled: dict[tuple[guild_id, user_id], asyncio.Task]`\n"
            "Any write op (set/remove/import) cancels and rebuilds the guild's tasks immediately.\n\n"
            "**Role cleanup:** a separate `@tasks.loop(hours=1)` checks members currently holding "
            "the birthday role — if it's no longer their birthday in their timezone, the role is removed.\n\n"
            "**`add_only` enforcement:** checked in `BirthdayService.set_birthday()` when `override=False`. "
            "Staff commands pass `override=True` to bypass it.\n\n"
            "**`private_confirms`:** `_ephemeral(settings)` returns `True` when enabled, "
            "making all user-facing birthday confirmations ephemeral."
        ),
    },
    {
        "title": "Birthday DB schema",
        "color": 0x5865F2,
        "description": (
            "**Migrations:** 012 (birthday_settings + birthdays), 013 (add_only + private_confirms columns)\n\n"
            "**`birthday_settings`** (one row per guild):\n"
            "```\n"
            "guild_id            INTEGER PRIMARY KEY\n"
            "birthday_role_id    INTEGER\n"
            "announce_channel_id INTEGER\n"
            "guild_timezone      TEXT DEFAULT 'UTC'\n"
            "announce_message    TEXT      -- single birthday\n"
            "announce_message_pl TEXT      -- plural (falls back to single)\n"
            "announce_ping       INTEGER   -- 0/1\n"
            "add_only            INTEGER   -- 0/1\n"
            "private_confirms    INTEGER   -- 0/1\n"
            "```\n\n"
            "**`birthdays`** (one row per user per guild):\n"
            "```\n"
            "guild_id        INTEGER  ─┐ composite PK\n"
            "user_id         INTEGER  ─┘\n"
            "birth_month     INTEGER  CHECK 1-12\n"
            "birth_day       INTEGER  CHECK 1-31\n"
            "user_timezone   TEXT     -- nullable, falls back to guild_timezone\n"
            "last_celebrated INTEGER  -- member's LOCAL year last announced (prevents double-fire)\n"
            "```\n"
            "`idx_birthdays_month_day` index on `(birth_month, birth_day)` speeds up the upcoming query."
        ),
    },
    {
        "title": "QOTD — scheduler & approval flow",
        "color": 0x5865F2,
        "description": (
            "**Files:** `cogs/qotd.py` · `services/qotd_service.py` · `database/repositories/qotd_repo.py`\n\n"
            "**Scheduler (`cogs/qotd.py → QotdCog._scheduler`):**\n"
            "A `@tasks.loop(seconds=60)` wakes every minute and calls `_maybe_queue_approval()` "
            "for each guild. That method:\n"
            "1. Loads `qotd_config` for the guild — bails if disabled or no channel set\n"
            "2. Checks `_pending` dict — bails if an approval is already waiting\n"
            "3. Checks `qotd_history` — bails if a question was already posted today (UTC date match)\n"
            "4. Checks `now.hour == post_hour and now.minute == post_minute` — bails if not time yet\n"
            "5. Calls `QotdService.generate_question()` then posts `ApprovalView` to Staff Lounge\n\n"
            "**`_pending` dict:** maps `guild_id → ApprovalView`. One pending approval per guild at a time. "
            "Cleared on approve, deny, custom, or 23-hour timeout.\n\n"
            "**`ApprovalView` buttons:** Approve / Deny / Regenerate / Custom Question (modal). "
            "All buttons check `self._resolved` first to prevent double-fires."
        ),
    },
    {
        "title": "QOTD — generation & validation pipeline",
        "color": 0x5865F2,
        "description": (
            "**`QotdService.generate_question(guild_id, model)`** — up to 3 attempts:\n"
            "1. `_call_ollama(model)` — POST to `http://localhost:11434/api/generate` with the "
            "system prompt and `stream: false`. Returns raw string.\n"
            "2. `_validate(raw, recent)` — runs the full pipeline:\n"
            "   • Strip known preamble phrases (\"Here's a question:\", \"Sure!\", etc.)\n"
            "   • Take only the first line\n"
            "   • Normalise to exactly one trailing `?`\n"
            "   • Length: 15–280 chars\n"
            "   • Exactly one `?`, must be at the end\n"
            "   • No multiple sentence-ending punctuation\n"
            "   • Banned keyword scan — ~70 words covering NSFW, politics, religion, self-harm, drugs\n"
            "   • SequenceMatcher ratio < 0.75 vs last 60 posted questions\n"
            "   • Significant word overlap < 0.65 vs last 20 posted questions\n"
            "3. Any `ValidationError` → retry. After 3 failures → `_pick_fallback(recent)`\n\n"
            "**Fallback:** 50 hand-written questions in `FALLBACK_QUESTIONS` list at top of "
            "`qotd_service.py`. Picks the first one not already in recent history."
        ),
    },
    {
        "title": "QOTD — Ollama system prompt engineering",
        "color": 0x5865F2,
        "description": (
            "The system prompt lives in `services/qotd_service.py → _SYSTEM_PROMPT`. "
            "It is engineered specifically for small 3B–8B models that tend to:\n"
            "• Add preamble ('Here is a great question!')\n"
            "• Output numbered lists instead of one question\n"
            "• Add explanations after the question\n"
            "• Ask multiple questions\n\n"
            "Key constraints enforced in the prompt:\n"
            "• 'Output EXACTLY one question. Nothing else.'\n"
            "• 'Do NOT include numbering, bullet points, or labels.'\n"
            "• 'Do NOT add explanations, alternatives, or commentary.'\n"
            "• 'End the question with a single question mark.'\n\n"
            "**Generation params** (`options` in the API payload):\n"
            "`temperature: 0.85` — high enough for variety, not so high it goes off-topic\n"
            "`top_p: 0.9` · `top_k: 40` · `num_predict: 80` — caps output length hard\n"
            "`stop: ['\\n\\n', '\\n1.', '\\nA:', '\\n-']` — kills multi-line responses early\n\n"
            "To tune: edit `_SYSTEM_PROMPT` or the `options` dict in `_call_ollama()`."
        ),
    },
    {
        "title": "QOTD — database schema",
        "color": 0x5865F2,
        "description": (
            "**Migration:** `database/migrations/sql/009_qotd.sql`\n\n"
            "**`qotd_config`** (one row per guild):\n"
            "```\n"
            "guild_id    INTEGER PRIMARY KEY\n"
            "channel_id  INTEGER          -- public post channel\n"
            "post_hour   INTEGER          -- UTC hour (0-23)\n"
            "post_minute INTEGER          -- minute (0-59)\n"
            "enabled     INTEGER          -- 0/1\n"
            "model       TEXT             -- ollama model id\n"
            "use_emojis  INTEGER          -- 0/1\n"
            "updated_at  TEXT\n"
            "```\n\n"
            "**`qotd_history`** (one row per posted question):\n"
            "```\n"
            "id          INTEGER PK AUTOINCREMENT\n"
            "guild_id    INTEGER\n"
            "question    TEXT             -- the final posted text\n"
            "source      TEXT             -- 'ai' | 'fallback' | 'custom'\n"
            "posted_at   TEXT             -- ISO datetime UTC\n"
            "message_id  INTEGER          -- Discord message snowflake\n"
            "channel_id  INTEGER\n"
            "```\n"
            "History is used for duplicate detection (last 60 rows) and the "
            "'already posted today' check (last row's date)."
        ),
    },
    {
        "title": "Startup sequence & bot object",
        "color": 0x5865F2,
        "description": (
            "**`main.py` startup order:**\n"
            "1. `setup_logging()` — structured logging via `core/logging_setup.py`\n"
            "2. `Database.connect()` — opens aiosqlite connection to `bot.db` file\n"
            "3. `run_migrations()` — applies any unapplied SQL files in version order\n"
            "4. `bot.db = db` — attached to bot so all cogs reach it via `bot.db`\n"
            "5. `bot.event_bus = EventBus()` — attached before cogs load\n"
            "6. Each cog's `setup(bot)` is awaited — cog guards on `bot.db` before adding itself\n"
            "7. `bot.start(token)` — connects to Discord gateway\n"
            "8. `on_ready` fires — `bot.tree.sync()` pushes slash commands globally\n\n"
            "**Objects attached to `bot`:**\n"
            "`bot.db` — `Database` instance (aiosqlite wrapper)\n"
            "`bot.event_bus` — `EventBus` pub/sub instance\n"
            "`bot.sync_queue` — `SyncQueue` debounced worker (attached by managed_commands)\n\n"
            "**Entry point:** always run `main.py`, not `bot.py`. "
            "`bot.py` is the legacy file kept as a fallback."
        ),
    },
    {
        "title": "Database layer — connection & repositories",
        "color": 0x5865F2,
        "description": (
            "**`database/connection.py` — `Database` class:**\n"
            "Thin async wrapper around a single `aiosqlite.Connection`. "
            "All methods commit immediately after writes. Key methods:\n"
            "`fetchone(sql, params)` → `aiosqlite.Row | None`\n"
            "`fetchall(sql, params)` → `list[aiosqlite.Row]`\n"
            "`execute(sql, params)` → `rowcount: int`\n"
            "`execute_returning_id(sql, params)` → `lastrowid: int`\n"
            "`executescript(sql)` — for multi-statement migration files\n"
            "`transaction()` — async context manager, rolls back on exception\n\n"
            "**Repository pattern:**\n"
            "Every table group has one repo file in `database/repositories/`. "
            "All SQL lives there — no raw SQL in services or cogs. "
            "`BaseRepository.__init__` just stores `self._db`. "
            "Rows come back as `aiosqlite.Row` objects; repos convert to `dict` before returning.\n\n"
            "**Migration runner (`database/migrations/runner.py`):**\n"
            "Reads `schema_migrations` table to find applied versions, "
            "runs missing ones in order, records each. Safe to run on every startup."
        ),
    },
    {
        "title": "Service layer rules",
        "color": 0x5865F2,
        "description": (
            "Services live in `services/` and contain all business logic. Rules:\n\n"
            "• **No raw SQL** — call repo methods only\n"
            "• **Minimal discord.py** — only import discord objects where the service genuinely "
            "needs to build or send them (e.g. `QotdService` builds embeds, `TicketService` "
            "creates channels). Pure logic services have no discord imports at all.\n"
            "• **No bot reference** unless the service needs to look up guild/channel objects\n"
            "• **Async throughout** — all methods that touch DB or network are `async def`\n\n"
            "**Current services:**\n"
            "`template_service.py` — CRUD + versioning\n"
            "`event_service.py` — subscribes to bus, renders + sends on events\n"
            "`managed_message_service.py` — post/sync/untrack managed messages\n"
            "`ticket_service.py` — full ticket lifecycle\n"
            "`scheduler_service.py` — cron-based scheduled sends\n"
            "`autorole_service.py` — delayed role assignment\n"
            "`permission_service.py` — staff check + DB grants\n"
            "`audit_service.py` — write + paginate audit log\n"
            "`qotd_service.py` — Ollama generation + validation + embed build\n"
            "`birthday_service.py` — date/tz parsing, add_only logic, import/export, scheduler helpers"
        ),
    },
    {
        "title": "Cog layer rules & slash command patterns",
        "color": 0x5865F2,
        "description": (
            "Cogs are thin — they handle the Discord interaction and delegate to a service. "
            "No SQL, no business logic, no raw HTTP in a cog.\n\n"
            "**Standard cog structure:**\n"
            "```python\n"
            "class MyCog(commands.Cog, name=\"MyFeature\"):\n"
            "    def __init__(self, bot):\n"
            "        self.bot = bot\n"
            "        self._repo = MyRepo(bot.db)\n"
            "        self._service = MyService(self._repo)\n\n"
            "    group = app_commands.Group(name=\"x\", description=\"...\")\n\n"
            "    @group.command(name=\"do\", description=\"...\")\n"
            "    async def do_cmd(self, interaction):\n"
            "        await interaction.response.defer(ephemeral=True)\n"
            "        result = await self._service.do_thing()\n"
            "        await interaction.followup.send(result)\n\n"
            "async def setup(bot):\n"
            "    if not hasattr(bot, 'db') or bot.db is None:\n"
            "        return\n"
            "    await bot.add_cog(MyCog(bot))\n"
            "```\n"
            "Always `defer()` before any async work — interactions expire in 3 seconds. "
            "Use `ephemeral=True` for staff-only responses. "
            "The `bot.db` guard in `setup()` prevents the cog loading if DB isn't ready."
        ),
    },
    {
        "title": "Adding a new feature — full checklist",
        "color": 0x5865F2,
        "description": (
            "**1. Migration**\n"
            "Write `database/migrations/sql/0XX_name.sql`. "
            "Add `(version, 'name', path)` to `MIGRATIONS` in `main.py`.\n\n"
            "**2. Repository**\n"
            "New file in `database/repositories/name_repo.py`. "
            "Extend `BaseRepository`. All SQL here, return `dict` not raw rows.\n\n"
            "**3. Service**\n"
            "New file in `services/name_service.py`. "
            "Takes repo in `__init__`. No SQL. Returns plain Python objects.\n\n"
            "**4. Cog**\n"
            "New file in `cogs/name.py`. "
            "`class XCog(commands.Cog, name='X')`. "
            "Group: `x = app_commands.Group(name='x', description='...')`. "
            "Guard `setup()` on `bot.db`.\n\n"
            "**5. Wire up**\n"
            "Add cog string to `COGS` list in `main.py`.\n\n"
            "**6. Help**\n"
            "Add pages to `_PAGES` (user-facing) and `_DEV_PAGES` (technical) in `help_commands.py`.\n\n"
            "**Run tests:** `venv\\Scripts\\pytest.exe tests/`\n"
            "**Syntax check:** `venv\\Scripts\\python.exe -m py_compile cogs/name.py`"
        ),
    },
]


def _prefix(bot: commands.Bot) -> str:
    p = getattr(bot, "command_prefix", "!")
    return p if isinstance(p, str) else "!"


_one_liner = command_summary


def music_commands(bot: commands.Bot) -> list[commands.Command]:
    """Visible commands registered by the Music cog (definition order)."""
    cog = bot.get_cog("Music")
    if cog is None:
        return []
    return cog._help_commands()


def build_music_pages(bot: commands.Bot) -> list[dict]:
    """Help pages for the music prefix commands, rendered by the Music cog's own
    help generator (registered commands + docstrings; `extras={"staff_only": True}`
    marks staff-only ones) so this never drifts from the code. The text points at
    `!musichelp <command>` and `/music help` for details."""
    cog = bot.get_cog("Music")
    if cog is None:
        return []
    cmds = cog._help_commands()
    if not cmds:
        return []
    prefix = _prefix(bot)
    intro = (
        f"Music uses **prefix** commands (`{prefix}`); `/music help` is the slash entry point. "
        f"Join a voice channel and use `{prefix}play <song or URL>`.\n\n"
    )
    chunks = split_message(intro + render_music_help(cmds, prefix, cog._help_notes()), EMBED_DESCRIPTION_LIMIT)
    return [
        {
            "title": "Music commands" + (f" ({i + 1}/{len(chunks)})" if len(chunks) > 1 else ""),
            "color": 0xFFB3C1,
            "description": chunk,
        }
        for i, chunk in enumerate(chunks)
    ]


def build_dev_extra_pages(bot: commands.Bot) -> list[dict]:
    """Extra /devhelp pages: diagnostics/debug commands, honeypot admin commands
    and internal tooling. Command lists are derived from what is registered."""
    prefix = _prefix(bot)
    color = 0x5865F2

    staff_music = [
        f"`{prefix}{c.name}` — {_one_liner(c)}" for c in music_commands(bot) if c.extras.get("staff_only")
    ]
    diag = [
        "**Music / PO-token diagnostics**",
        f"`{prefix}musicstatus` / `{prefix}status` — public health summary; staff also see pot-server "
        "pid/URL, watchdog, last error and a sanitised log tail.",
        *staff_music,
        "",
        "`!debugurl` output masks signed-URL params (`sig`, `lsig`, `pot`, `ip`, `key`, ...). `--full` "
        "(bot owner only) DMs the unmasked URL and never posts it in a channel. "
        "Extra masks: `MUSIC_DEBUGURL_MASK_PARAMS`; refused inputs: `MUSIC_DEBUGURL_BLOCKED`.",
        "",
        "**Other**",
        "`/bot audit` — everything staff did with the bot.",
        "`/honeypot status` / `/honeypot stats` — trap config and counters.",
        "",
        "**Restarts (supervisor.py)**",
        "`/restart bot|dashboard|tunnel|all [reason]` — ask `supervisor.py` to restart part of the stack "
        "(staff, 30 s cooldown per service, audit-logged); `/restart status` — what it is running. "
        "Only works when the stack runs under `supervisor.py`. The same controls are on the dashboard "
        "under Diagnostics -> Services. A restarted child re-reads `.env`, but `SUPERVISOR_*` keys "
        "and `supervisor.py` itself need a supervisor restart.",
    ]

    hp_lines = []
    for cmd in bot.tree.walk_commands():
        if cmd.qualified_name.startswith("honeypot ") and isinstance(cmd, app_commands.Command):
            hp_lines.append(f"`/{cmd.qualified_name}` — {cmd.description}")
    honeypot = (
        "\n".join(sorted(hp_lines)) if hp_lines else "Honeypot commands are not loaded."
    ) + "\n\nAppeals arrive via the dashboard (`APPEAL_BASE_URL`); see `docs/honeypot_contract.md`."

    tooling = (
        "**Processes:** the bot (embeds the internal API on loopback, bearer `INTERNAL_API_SECRET`), "
        "the dashboard backend (`server/`), and the PO-token Node server (spawned by "
        "`services/pot_server_service.py`, log in `logs/pot_server.log`). Under `supervisor.py` "
        "(logs in `logs/supervisor/`) the bot, dashboard and tunnel are its children, and its control "
        "API listens on `127.0.0.1:8767`.\n\n"
        "**Cookie listener:** optional loopback endpoint the browser extension pushes YouTube cookies to "
        "(`services/cookie_listener_service.py`, off unless `MUSIC_COOKIE_LISTENER_ENABLED` and a secret are "
        "set; setup in `docs/COOKIE_EXTENSION.md`).\n\n"
        "**Config:** `.env` (see `.env.example`); music knobs are `MUSIC_*` in `config.py`, read at start-up, "
        "so changes need a restart.\n\n"
        "**Tests:** `venv\\Scripts\\python.exe -m pytest tests -q`"
    )

    pages = []
    for title, body in (
        ("Diagnostics & debug commands", "\n".join(diag)),
        ("Honeypot admin commands", honeypot),
        ("Internal tooling", tooling),
    ):
        chunks = split_message(body, EMBED_DESCRIPTION_LIMIT)
        for i, chunk in enumerate(chunks):
            pages.append({
                "title": title + (f" ({i + 1}/{len(chunks)})" if len(chunks) > 1 else ""),
                "color": color,
                "description": chunk,
            })
    return pages


class HelpCog(commands.Cog, name="Help"):
    def __init__(self, bot: commands.Bot, permission_service: PermissionService):
        self.bot = bot
        self.permission_service = permission_service

    @app_commands.command(name="help", description="Learn how the bot works. (Staff only)")
    async def help_cmd(self, interaction: discord.Interaction) -> None:
        if not await self.permission_service.is_any_staff_interaction(interaction):
            await interaction.response.send_message("This command is staff-only.", ephemeral=True)
            return

        def formatter(slice_: list, page: int, max_page: int) -> discord.Embed:
            item = slice_[0]
            embed = discord.Embed(
                title=item["title"],
                description=item["description"],
                color=item["color"],
            )
            embed.set_footer(text=f"Page {page + 1} of {max_page + 1}")
            return embed

        paginator = Paginator(items=[*_PAGES, *build_music_pages(self.bot)], page_size=1, formatter=formatter)
        await interaction.response.send_message(
            embed=paginator.render_embed(), view=paginator, ephemeral=True
        )

    @app_commands.command(name="bdayhelp", description="How the birthday system works.")
    async def bdayhelp_cmd(self, interaction: discord.Interaction) -> None:
        bday_pages = [p for p in _PAGES if p["title"].startswith("Birthday")]

        def formatter(slice_: list, page: int, max_page: int) -> discord.Embed:
            item = slice_[0]
            embed = discord.Embed(
                title=item["title"],
                description=item["description"],
                color=item["color"],
            )
            embed.set_footer(text=f"Page {page + 1} of {max_page + 1} — /bdayhelp")
            return embed

        paginator = Paginator(items=bday_pages, page_size=1, formatter=formatter)
        await interaction.response.send_message(
            embed=paginator.render_embed(), view=paginator, ephemeral=True
        )

    @app_commands.command(name="devhelp", description="Technical reference for bot internals. (Staff only)")
    async def devhelp_cmd(self, interaction: discord.Interaction) -> None:
        if not self.permission_service.is_staff_interaction(interaction):
            await interaction.response.send_message("This command is staff-only.", ephemeral=True)
            return

        def formatter(slice_: list, page: int, max_page: int) -> discord.Embed:
            item = slice_[0]
            embed = discord.Embed(
                title=item["title"],
                description=item["description"],
                color=item["color"],
            )
            embed.set_footer(text=f"Page {page + 1} of {max_page + 1} — /devhelp")
            return embed

        paginator = Paginator(
            items=[*_DEV_PAGES, *build_dev_extra_pages(self.bot)], page_size=1, formatter=formatter
        )
        await interaction.response.send_message(
            embed=paginator.render_embed(), view=paginator, ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    perm = bot.permission_service  # type: ignore[attr-defined]
    await bot.add_cog(HelpCog(bot, perm))
