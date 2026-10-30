"""
services/fortnite_shop_service.py — Fortnite item shop: fetch, parse, group,
build Discord embeds, and the restart-safe daily auto-post.

Source: fortnite-api.com, `GET https://fortnite-api.com/v2/shop?language=en`
(unofficial community API, free, no key needed for this endpoint; an optional
key from FORTNITE_API_KEY is sent as the `Authorization` header). Response:
`{"status": 200, "data": {"hash", "date": "YYYY-MM-DDT00:00:00Z", "vbuckIcon",
"entries": [...]}}`. Each entry has regularPrice/finalPrice, offerId,
inDate/outDate, sortPriority, layoutId, optional `layout` {id, name, index,
rank, category} (the shop section), optional `bundle` {name, info, image},
`banner` {value}, `newDisplayAsset.renderImages[].image`, and one or more of
`brItems` (cosmetics: name, type.displayValue, rarity.value, optional
series {value, colors}, images.featured/icon/smallIcon), `tracks` (Jam Tracks:
title, artist, albumArt), `cars`, `instruments`, `legoKits` (name, rarity,
images.large/small). The shop rotates daily at 00:00 UTC.

Layering:
  - Pure functions (parse_shop, group_sections, build_post_embeds,
    batch_embeds, build_item_pages, match_wishlist, build_wishlist_messages,
    next_post_time, is_post_due, compute_next_post) hold all logic and are unit-tested against a recorded
    fixture (tests/unit/fixtures/fortnite_shop_sample.json).
  - FortniteShopService adds HTTP (aiohttp, timeout + retries with backoff),
    an in-memory cache of the latest shop for `!shop`, and the daily post.

Dedupe: fortnite_shop_config.last_posted_date (the shop's YYYY-MM-DD) is the
restart-safe key; run_daily() never posts the same shop date twice. It is
written BEFORE the first message is sent and released again only if nothing
reached Discord, so a failure partway through a multi-message post is
recorded in last_error ("partial post") instead of being re-posted. Debug
posts (owner-only dashboard page) call post_shop() directly and never touch
the dedupe columns.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import aiohttp
import discord

log = logging.getLogger(__name__)

SHOP_URL = "https://fortnite-api.com/v2/shop"
VBUCK_ICON_FALLBACK = "https://fortnite-api.com/images/vbuck.png"
CACHE_TTL_SECONDS = 600            # !shop reuses a fetch for up to 10 minutes (same shop date)
FETCH_ATTEMPTS = 3                 # daily auto-post + !shop (worst case ~97s: 3 x 30s + 2s + 5s backoff)
MANUAL_FETCH_ATTEMPTS = 1          # dashboard/debug-triggered posts + preview: worst case ~30s, so the
                                   # dashboard's 150s internal-API timeout never fires while the bot still posts
FETCH_BACKOFF_SECONDS = (2.0, 5.0)  # sleep between attempts 1->2, 2->3
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)
OTHER_SECTION = "Other"

# Discord limits.
MAX_EMBEDS_PER_MESSAGE = 10
MAX_CHARS_PER_MESSAGE = 6000
MAX_DESCRIPTION = 4096
DESCRIPTION_BUDGET = 3900          # leave headroom under 4096 for the "(cont.)" marker etc.

RARITY_COLOURS: dict[str, int] = {
    "common": 0xB1B1B1,
    "uncommon": 0x60AA3A,
    "rare": 0x49ACF2,
    "epic": 0xB15BE2,
    "legendary": 0xD37841,
    "mythic": 0xFFD13E,
    "exotic": 0x76D7E3,
    "transcendent": 0x5CDCE4,
    "marvel": 0xC53334,
    "dc": 0x5475C7,
    "dark": 0xFF42E7,
    "frozen": 0x94DFFF,
    "lava": 0xD19635,
    "shadow": 0x5F5F5F,
    "slurp": 0x29F1A3,
    "icon": 0x3FB9CE,
    "starwars": 0x2A4F7A,
    "gaminglegends": 0x7D58DB,
}
DEFAULT_COLOUR = 0x2B8CFF
# Higher = "rarer" when choosing a section's colour.
_RARITY_RANK = {
    "common": 0, "uncommon": 1, "rare": 2, "epic": 3, "legendary": 4, "mythic": 5,
    "exotic": 5, "transcendent": 5,
}


class FortniteShopError(Exception):
    """Fetching or parsing the shop failed (API outage, bad JSON, timeout)."""


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass
class ShopEntry:
    offer_id: str
    name: str
    type_label: str
    kind: str                     # "bundle" | "br" | "track" | "car" | "instrument" | "lego" | "other"
    rarity: str | None            # rarity.value (or series-derived), lower-case
    rarity_label: str | None
    colour: int
    final_price: int
    regular_price: int
    image_url: str | None
    section: str
    section_index: int
    sort_priority: int
    banner: str | None = None
    out_date: str | None = None
    item_names: list[str] = field(default_factory=list)  # every contained item name (wishlist matching)

    @property
    def discounted(self) -> bool:
        return self.final_price < self.regular_price


@dataclass
class Shop:
    hash: str
    date: str                     # raw ISO from the API
    shop_date: str                # YYYY-MM-DD
    vbuck_icon: str
    entries: list[ShopEntry]

    def to_dict(self) -> dict:
        return {
            "hash": self.hash,
            "date": self.date,
            "shop_date": self.shop_date,
            "vbuck_icon": self.vbuck_icon,
            "entry_count": len(self.entries),
            "sections": [
                {"name": s.name, "count": len(s.entries)} for s in group_sections(self.entries)
            ],
            "entries": [asdict(e) | {"discounted": e.discounted} for e in self.entries],
        }


@dataclass
class ShopSection:
    name: str
    index: int
    entries: list[ShopEntry]

    @property
    def is_jam_tracks(self) -> bool:
        return bool(self.entries) and all(e.kind == "track" for e in self.entries)


# ---------------------------------------------------------------------------
# Parsing (pure)
# ---------------------------------------------------------------------------


def _hex_colour(value: Any) -> int | None:
    """'8078ffff' / '#8078ff' -> 0x8078FF. None on junk."""
    if not isinstance(value, str):
        return None
    v = value.strip().lstrip("#")
    if len(v) == 8:
        v = v[:6]
    if len(v) != 6:
        return None
    try:
        return int(v, 16)
    except ValueError:
        return None


def _first(seq: Any) -> dict | None:
    if isinstance(seq, list) and seq and isinstance(seq[0], dict):
        return seq[0]
    return None


def _image_of(obj: dict | None, *keys: str) -> str | None:
    if not obj:
        return None
    images = obj.get("images") or {}
    for key in keys:
        url = images.get(key)
        if isinstance(url, str) and url:
            return url
    return None


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_entry(raw: dict) -> ShopEntry | None:
    """One API entry -> ShopEntry, or None when it has nothing displayable."""
    if not isinstance(raw, dict):
        return None
    br = raw.get("brItems") if isinstance(raw.get("brItems"), list) else []
    tracks = raw.get("tracks") if isinstance(raw.get("tracks"), list) else []
    cars = raw.get("cars") if isinstance(raw.get("cars"), list) else []
    instruments = raw.get("instruments") if isinstance(raw.get("instruments"), list) else []
    legos = raw.get("legoKits") if isinstance(raw.get("legoKits"), list) else []
    bundle = raw.get("bundle") if isinstance(raw.get("bundle"), dict) else None

    item_names: list[str] = []
    for coll in (br, cars, instruments, legos):
        item_names += [str(i["name"]) for i in coll if isinstance(i, dict) and i.get("name")]
    for t in tracks:
        if isinstance(t, dict) and t.get("title"):
            item_names.append(str(t["title"]))

    first_br, first_track = _first(br), _first(tracks)
    first_car, first_inst, first_lego = _first(cars), _first(instruments), _first(legos)
    primary = first_br or first_car or first_inst or first_lego

    if bundle and bundle.get("name"):
        kind, name = "bundle", str(bundle["name"])
        type_label = str(bundle.get("info") or "Bundle")
    elif first_br:
        kind, name = "br", str(first_br.get("name") or "")
        type_label = str((first_br.get("type") or {}).get("displayValue") or "Item")
    elif first_track:
        kind = "track"
        title = str(first_track.get("title") or "")
        artist = first_track.get("artist")
        name = f"{title} - {artist}" if artist else title
        type_label = "Jam Track"
    elif first_car:
        kind, name = "car", str(first_car.get("name") or "")
        type_label = str((first_car.get("type") or {}).get("displayValue") or "Car")
    elif first_inst:
        kind, name = "instrument", str(first_inst.get("name") or "")
        type_label = str((first_inst.get("type") or {}).get("displayValue") or "Instrument")
    elif first_lego:
        kind, name = "lego", str(first_lego.get("name") or "")
        type_label = "LEGO Kit"
    else:
        return None
    if not name.strip():
        return None
    if name not in item_names:
        item_names.insert(0, name)

    # Rarity / colour: a series (Icon, Marvel, Gaming Legends, ...) wins over rarity.
    rarity = rarity_label = None
    colour: int | None = None
    if primary:
        series = primary.get("series") if isinstance(primary.get("series"), dict) else None
        rar = primary.get("rarity") if isinstance(primary.get("rarity"), dict) else {}
        rarity = (str(rar.get("value")).lower() if rar.get("value") else None)
        rarity_label = rar.get("displayValue") or None
        if series:
            rarity_label = series.get("value") or rarity_label
            colours = series.get("colors") if isinstance(series.get("colors"), list) else []
            colour = _hex_colour(colours[1] if len(colours) > 1 else (colours[0] if colours else None))
    if colour is None:
        colour = RARITY_COLOURS.get(rarity or "", DEFAULT_COLOUR)

    render = None
    nda = raw.get("newDisplayAsset") if isinstance(raw.get("newDisplayAsset"), dict) else None
    if nda:
        ri = _first(nda.get("renderImages"))
        render = ri.get("image") if ri else None
    image = (
        (bundle.get("image") if bundle else None)
        or render
        or _image_of(first_br, "featured", "icon", "smallIcon")
        or (first_track.get("albumArt") if first_track else None)
        or _image_of(first_car, "large", "small")
        or _image_of(first_inst, "large", "small")
        or _image_of(first_lego, "large", "small")
    )

    layout = raw.get("layout") if isinstance(raw.get("layout"), dict) else None
    section = (str(layout.get("name") or "").strip() if layout else "") or OTHER_SECTION
    section_index = _int(layout.get("index"), 9999) if layout else 10000
    banner = raw.get("banner") if isinstance(raw.get("banner"), dict) else None

    return ShopEntry(
        offer_id=str(raw.get("offerId") or ""),
        name=name.strip(),
        type_label=type_label,
        kind=kind,
        rarity=rarity,
        rarity_label=rarity_label,
        colour=colour,
        final_price=_int(raw.get("finalPrice")),
        regular_price=_int(raw.get("regularPrice")),
        image_url=image if isinstance(image, str) and image else None,
        section=section,
        section_index=section_index,
        sort_priority=_int(raw.get("sortPriority")),
        banner=(banner.get("value") or None) if banner else None,
        out_date=raw.get("outDate"),
        item_names=item_names,
    )


def parse_shop(payload: dict) -> Shop:
    """API JSON -> Shop. Raises FortniteShopError on a malformed payload."""
    if not isinstance(payload, dict):
        raise FortniteShopError("shop payload is not an object")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise FortniteShopError(f"shop payload has no data (status={payload.get('status')})")
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        raise FortniteShopError("shop payload has no entries list")
    date = str(data.get("date") or "")
    shop_date = date[:10]
    try:
        datetime.strptime(shop_date, "%Y-%m-%d")
    except ValueError:
        raise FortniteShopError(f"shop payload has an invalid date: {date!r}")
    entries = [e for e in (parse_entry(r) for r in raw_entries) if e is not None]
    return Shop(
        hash=str(data.get("hash") or ""),
        date=date,
        shop_date=shop_date,
        vbuck_icon=str(data.get("vbuckIcon") or VBUCK_ICON_FALLBACK),
        entries=entries,
    )


def group_sections(entries: list[ShopEntry]) -> list[ShopSection]:
    """Group by section name, sections ordered by the API's layout index
    ("Other" last), entries by sortPriority (desc) then name."""
    groups: dict[str, ShopSection] = {}
    for e in entries:
        sec = groups.get(e.section)
        if sec is None:
            sec = groups[e.section] = ShopSection(e.section, e.section_index, [])
        sec.index = min(sec.index, e.section_index)
        sec.entries.append(e)
    for sec in groups.values():
        sec.entries.sort(key=lambda x: (-x.sort_priority, x.name.lower()))
    return sorted(groups.values(), key=lambda s: (s.index, s.name.lower()))


# ---------------------------------------------------------------------------
# Embeds (pure)
# ---------------------------------------------------------------------------


def format_price(entry: ShopEntry) -> str:
    price = f"{entry.final_price:,} V-Bucks"
    if entry.discounted:
        price += f" ~~{entry.regular_price:,}~~"
    return price


def _entry_line(e: ShopEntry) -> str:
    extra = f" · {e.type_label}" if e.kind in ("bundle", "car", "instrument", "lego") else ""
    if e.rarity_label and e.kind != "bundle":
        extra += f" · {e.rarity_label}"
    banner = f" · *{e.banner}*" if e.banner else ""
    return f"**{e.name}** · {format_price(e)}{extra}{banner}"


def _section_colour(sec: ShopSection) -> int:
    best, best_rank = None, -1
    for e in sec.entries:
        rank = _RARITY_RANK.get(e.rarity or "", 6 if e.rarity else -1)
        if rank > best_rank:
            best, best_rank = e, rank
    return best.colour if best else DEFAULT_COLOUR


def _pretty_date(shop_date: str) -> str:
    try:
        d = datetime.strptime(shop_date, "%Y-%m-%d")
        return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}"
    except ValueError:
        return shop_date


def build_post_embeds(shop: Shop, *, include_jam_tracks: bool = False, test: bool = False) -> list[discord.Embed]:
    """Header embed + one embed per section (long sections split into
    "(cont.)" embeds so no description exceeds Discord's 4096-char limit).
    Jam-Track-only sections collapse to a single line unless include_jam_tracks."""
    sections = group_sections(shop.entries)
    header = discord.Embed(
        title=("[TEST] " if test else "") + f"Fortnite Item Shop - {_pretty_date(shop.shop_date)}",
        description=(
            f"{len(shop.entries)} offers across {len(sections)} sections. "
            "Use `!shop` to browse every item with pictures."
        ),
        colour=DEFAULT_COLOUR,
    )
    header.set_thumbnail(url=shop.vbuck_icon)
    header.set_footer(text="Data: fortnite-api.com · resets daily at 00:00 UTC")

    embeds = [header]
    collapsed: list[str] = []
    for sec in sections:
        if sec.is_jam_tracks and not include_jam_tracks:
            collapsed.append(f"**{sec.name}**: {len(sec.entries)} Jam Tracks")
            continue
        chunks: list[list[str]] = [[]]
        size = 0
        for e in sec.entries:
            line = _entry_line(e)[:500]
            if size + len(line) + 1 > DESCRIPTION_BUDGET and chunks[-1]:
                chunks.append([])
                size = 0
            chunks[-1].append(line)
            size += len(line) + 1
        thumb = next((e.image_url for e in sec.entries if e.image_url), None)
        for i, lines in enumerate(chunks):
            title = sec.name if i == 0 else f"{sec.name} (cont.)"
            emb = discord.Embed(title=title[:256], description="\n".join(lines), colour=_section_colour(sec))
            if i == 0 and thumb:
                emb.set_thumbnail(url=thumb)
            embeds.append(emb)
    if collapsed:
        embeds.append(
            discord.Embed(
                title="Also in the shop",
                description="\n".join(collapsed) + "\nBrowse them with `!shop`.",
                colour=DEFAULT_COLOUR,
            )
        )
    return embeds


def batch_embeds(
    embeds: list[discord.Embed],
    *,
    max_embeds: int = MAX_EMBEDS_PER_MESSAGE,
    max_chars: int = MAX_CHARS_PER_MESSAGE,
) -> list[list[discord.Embed]]:
    """Split embeds into per-message batches honouring Discord's 10-embeds and
    6000-total-characters-per-message limits (len(embed) is discord.py's count)."""
    batches: list[list[discord.Embed]] = []
    current: list[discord.Embed] = []
    chars = 0
    for emb in embeds:
        n = len(emb)
        if current and (len(current) >= max_embeds or chars + n > max_chars):
            batches.append(current)
            current, chars = [], 0
        current.append(emb)
        chars += n
    if current:
        batches.append(current)
    return batches


def build_item_page(shop: Shop, entry: ShopEntry, position: int, total: int) -> discord.Embed:
    emb = discord.Embed(title=entry.name[:256], colour=entry.colour)
    bits = [entry.type_label]
    if entry.rarity_label:
        bits.append(entry.rarity_label)
    emb.description = " · ".join(bits)
    emb.add_field(name="Price", value=format_price(entry), inline=True)
    emb.add_field(name="Section", value=entry.section[:1024], inline=True)
    if entry.banner:
        emb.add_field(name="Tag", value=entry.banner[:1024], inline=True)
    if entry.kind == "bundle" and len(entry.item_names) > 1:
        names = ", ".join(n for n in entry.item_names if n != entry.name)
        emb.add_field(name="Includes", value=names[:1024], inline=False)
    if entry.image_url:
        emb.set_image(url=entry.image_url)
    emb.set_thumbnail(url=shop.vbuck_icon)
    emb.set_footer(text=f"{position}/{total} · Shop for {shop.shop_date} · fortnite-api.com")
    return emb


def ordered_entries(shop: Shop) -> list[ShopEntry]:
    return [e for sec in group_sections(shop.entries) for e in sec.entries]


def section_starts(shop: Shop) -> list[tuple[str, int, int]]:
    """(section name, index of its first entry in ordered_entries, entry count)."""
    out: list[tuple[str, int, int]] = []
    i = 0
    for sec in group_sections(shop.entries):
        out.append((sec.name, i, len(sec.entries)))
        i += len(sec.entries)
    return out


# ---------------------------------------------------------------------------
# Wishlist + timing (pure)
# ---------------------------------------------------------------------------


def match_wishlist(shop: Shop, wishes: list[dict]) -> dict[int, list[dict]]:
    """wishes: rows {id, user_id, query, last_notified_date}. Returns
    {user_id: [{"wish_id", "query", "items": [entry names]}]} for wishes whose
    (lower-case) query is a substring of any item name in the shop, skipping
    wishes already notified for this shop date."""
    index = [(e, [n.lower() for n in e.item_names]) for e in shop.entries]
    out: dict[int, list[dict]] = {}
    for w in wishes:
        q = str(w.get("query") or "").strip().lower()
        if not q or w.get("last_notified_date") == shop.shop_date:
            continue
        hits = sorted({e.name for e, names in index if any(q in n for n in names)})
        if hits:
            out.setdefault(int(w["user_id"]), []).append({"wish_id": w["id"], "query": q, "items": hits})
    return out


WISHLIST_HEADER = "**Wishlist alert!** These are in today's shop:"
WISHLIST_HEADER_CONT = "**Wishlist alert (cont.)**"


def build_wishlist_messages(matches: dict[int, list[dict]], limit: int = 1900) -> list[tuple[str, list[int]]]:
    """Wishlist pings split into as many messages as needed, each at most
    `limit` chars. Returns [(text, wish ids rendered in that text)], so the
    caller marks exactly the wishes whose `<@uid>` mention was actually sent.
    A single member's line never loses its mention: only the item list is
    shortened if that one line would be too long on its own."""
    out: list[tuple[str, list[int]]] = []
    lines: list[str] = [WISHLIST_HEADER]
    ids: list[int] = []
    size = len(WISHLIST_HEADER)
    for uid, hits in matches.items():
        items = ", ".join(sorted({name for h in hits for name in h["items"]}))
        prefix = f"<@{uid}>: "
        room = limit - max(len(WISHLIST_HEADER), len(WISHLIST_HEADER_CONT)) - 1 - len(prefix)
        if len(items) > room:
            items = items[: max(room - 3, 0)] + "..."
        line = prefix + items
        if ids and size + 1 + len(line) > limit:
            out.append(("\n".join(lines), ids))
            lines, ids, size = [WISHLIST_HEADER_CONT], [], len(WISHLIST_HEADER_CONT)
        lines.append(line)
        ids.extend(int(h["wish_id"]) for h in hits)
        size += 1 + len(line)
    if ids:
        out.append(("\n".join(lines), ids))
    return out


def build_wishlist_message(matches: dict[int, list[dict]], limit: int = 1900) -> str | None:
    """All wishlist pings as ONE string (debug/preview only; the daily post uses
    build_wishlist_messages so nothing is cut off)."""
    chunks = build_wishlist_messages(matches, limit)
    return "\n".join(text for text, _ids in chunks) if chunks else None


def next_post_time(now: datetime, delay_minutes: int) -> datetime:
    """The next daily post moment strictly after `now` (00:00 UTC + delay)."""
    now = now.astimezone(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=delay_minutes)
    return today if now < today else today + timedelta(days=1)


def is_post_due(cfg: dict | None, now: datetime, delay_minutes: int) -> bool:
    """Enabled, has a channel, past today's post moment, and today's UTC date
    has not been posted yet (last_posted_date is the restart-safe dedupe key)."""
    if not cfg or not cfg.get("enabled") or not cfg.get("channel_id"):
        return False
    now = now.astimezone(timezone.utc)
    post_at = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=delay_minutes)
    if now < post_at:
        return False
    return cfg.get("last_posted_date") != now.strftime("%Y-%m-%d")


def compute_next_post(
    cfg: dict | None, now: datetime, delay_minutes: int, retry_at: datetime | None = None
) -> tuple[datetime | None, bool]:
    """(when the next daily post should happen, whether today's is still pending).
    None while disabled or without a channel. If today's post is due but not
    done yet, that is "now" (or the loop's retry gate after a failed tick),
    not tomorrow's slot."""
    if not cfg or not cfg.get("enabled") or not cfg.get("channel_id"):
        return None, False
    now = now.astimezone(timezone.utc)
    if is_post_due(cfg, now, delay_minutes):
        if retry_at is not None and retry_at > now:
            return retry_at, True
        return now, True
    return next_post_time(now, delay_minutes), False


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

Fetcher = Callable[..., Awaitable[dict]]  # called as fetcher(attempts=N)


class FortniteShopService:
    def __init__(
        self,
        repo,
        *,
        api_key: str = "",
        language: str = "en",
        post_delay_minutes: int = 5,
        fetcher: Fetcher | None = None,
    ) -> None:
        self._repo = repo
        self._api_key = api_key
        self._language = language
        self.post_delay_minutes = post_delay_minutes
        self._fetcher = fetcher or self._http_fetch
        self._cache: Shop | None = None
        self._cache_at: float = 0.0
        self._lock = asyncio.Lock()
        self.last_fetch_error: str | None = None
        # guild id -> earliest next auto-post attempt after a failed tick. Written
        # by cogs/fortnite_shop.py's loop, read by status() for "next post".
        self.retry_at: dict[int, datetime] = {}

    @property
    def repo(self):
        return self._repo

    # -------------------------------------------------------------- HTTP
    async def _http_fetch(self, attempts: int = FETCH_ATTEMPTS) -> dict:
        attempts = max(1, attempts)
        headers = {"User-Agent": "kick-bot (Discord bot; fortnite shop)"}
        if self._api_key:
            headers["Authorization"] = self._api_key
        last_exc: Exception | None = None
        async with aiohttp.ClientSession(timeout=FETCH_TIMEOUT, headers=headers) as session:
            for attempt in range(attempts):
                try:
                    async with session.get(SHOP_URL, params={"language": self._language}) as resp:
                        if resp.status != 200:
                            raise FortniteShopError(f"HTTP {resp.status}")
                        return await resp.json(content_type=None)
                except (aiohttp.ClientError, asyncio.TimeoutError, FortniteShopError, ValueError) as exc:
                    last_exc = exc
                    log.warning("fortnite: shop fetch attempt %d/%d failed: %s", attempt + 1, attempts, exc)
                    if attempt < attempts - 1:
                        await asyncio.sleep(FETCH_BACKOFF_SECONDS[min(attempt, len(FETCH_BACKOFF_SECONDS) - 1)])
        raise FortniteShopError(f"shop fetch failed after {attempts} attempt(s): {last_exc}")

    async def get_shop(self, *, force: bool = False, attempts: int = FETCH_ATTEMPTS) -> Shop:
        """Latest shop; reuses the in-memory copy for CACHE_TTL_SECONDS when
        it is still today's (UTC) shop. Raises FortniteShopError. Manual
        (dashboard/debug) callers pass attempts=MANUAL_FETCH_ATTEMPTS to keep
        their worst case well under the dashboard's request timeout."""
        async with self._lock:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            fresh = (
                self._cache is not None
                and time.monotonic() - self._cache_at < CACHE_TTL_SECONDS
                and self._cache.shop_date == today
            )
            if fresh and not force:
                return self._cache  # type: ignore[return-value]
            try:
                shop = parse_shop(await self._fetcher(attempts=attempts))
            except FortniteShopError as exc:
                self.last_fetch_error = str(exc)
                if self._cache is not None and not force:
                    log.warning("fortnite: using stale cached shop (%s): %s", self._cache.shop_date, exc)
                    return self._cache
                raise
            except Exception as exc:  # defensive: never let a parser bug escape untyped
                self.last_fetch_error = f"{type(exc).__name__}: {exc}"
                raise FortniteShopError(self.last_fetch_error) from exc
            self._cache, self._cache_at = shop, time.monotonic()
            self.last_fetch_error = None
            return shop

    @property
    def cached_shop(self) -> Shop | None:
        return self._cache

    # -------------------------------------------------------------- Posting
    async def post_shop(
        self,
        channel: discord.abc.Messageable,
        shop: Shop,
        *,
        include_jam_tracks: bool = False,
        ping_role_id: int | None = None,
        wishlist_text: str | None = None,
        test: bool = False,
        sent: list[discord.Message] | None = None,
    ) -> list[discord.Message]:
        """Send the shop embeds (batched to Discord's limits). Does NOT touch
        any DB state; callers that want dedupe use run_daily(). Pass `sent`
        to learn which messages went out if a later send raises."""
        batches = batch_embeds(build_post_embeds(shop, include_jam_tracks=include_jam_tracks, test=test))
        messages: list[discord.Message] = sent if sent is not None else []
        no_pings = discord.AllowedMentions.none()
        for i, batch in enumerate(batches):
            content = None
            allowed = no_pings
            if i == 0 and ping_role_id and not test:
                content = f"<@&{ping_role_id}>"
                allowed = discord.AllowedMentions(everyone=False, users=False, roles=[discord.Object(ping_role_id)])
            messages.append(await channel.send(content=content, embeds=batch, allowed_mentions=allowed))
        if wishlist_text:
            messages.append(
                await channel.send(
                    wishlist_text,
                    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=not test),
                )
            )
        return messages

    async def run_daily(self, guild: discord.Guild, *, now: datetime | None = None) -> str:
        """One auto-post check for `guild`. Returns a short status string. Never
        raises: API outages/Discord errors are logged and stored in last_error."""
        now = now or datetime.now(timezone.utc)
        cfg = await self._repo.get_config(guild.id)
        if not is_post_due(cfg, now, self.post_delay_minutes):
            return "not_due"
        assert cfg is not None
        today = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        try:
            shop = await self.get_shop(force=True)
        except FortniteShopError as exc:
            await self._record_error(guild.id, f"fetch failed: {exc}")
            return "fetch_failed"
        if shop.shop_date != today:
            # The API has not rotated yet; try again on the next tick.
            return "stale_shop"
        channel = guild.get_channel(cfg["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            await self._record_error(guild.id, f"channel {cfg['channel_id']} not found")
            return "no_channel"

        wishes = await self._repo.list_wishes(guild.id)
        matches = match_wishlist(shop, wishes)

        # Claim the shop date BEFORE sending anything. A failure partway
        # through a multi-message post (or a DB error after it) must never
        # make the next tick re-post the batches that already went out. If
        # this write fails nothing has been sent, so the retry is safe.
        previous = {"last_posted_date": cfg.get("last_posted_date"), "last_posted_hash": cfg.get("last_posted_hash")}
        await self._repo.update_config(guild.id, last_posted_date=shop.shop_date, last_posted_hash=shop.hash)

        sent: list[discord.Message] = []
        try:
            await self.post_shop(
                channel,
                shop,
                include_jam_tracks=cfg.get("include_jam_tracks", False),
                ping_role_id=cfg.get("ping_role_id"),
                sent=sent,
            )
        except Exception as exc:  # discord.HTTPException in practice; never leave the claim on an empty post
            if not sent:
                # Nothing reached Discord: release the claim so the loop retries.
                try:
                    await self._repo.update_config(guild.id, **previous)
                except Exception:
                    log.exception("fortnite: could not release the dedupe claim; today's post will not be retried")
                await self._record_error(guild.id, f"post failed: {exc}")
                return "post_failed"
            # Partly posted: keep the claim (no duplicate re-post), say so.
            await self._record_error(
                guild.id, f"partial post ({len(sent)} message(s) sent, not retried to avoid duplicates): {exc}"
            )
            await self._finish_post(guild.id, sent, clear_error=False)
            return "post_partial"

        await self._finish_post(guild.id, sent, clear_error=True)
        await self._send_wishlist(guild.id, channel, matches, shop.shop_date)
        log.info("fortnite: posted shop %s (%s) to #%s", shop.shop_date, shop.hash, channel.name)
        return "posted"

    async def _finish_post(self, guild_id: int, sent: list[discord.Message], *, clear_error: bool) -> None:
        values: dict = {
            "last_posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "last_message_id": sent[0].id if sent else None,
        }
        if clear_error:
            values.update(last_error=None, last_error_at=None)
        try:
            await self._repo.update_config(guild_id, **values)
        except Exception:  # the dedupe claim is already stored; this is bookkeeping only
            log.exception("fortnite: could not store last_posted_at/last_message_id")

    async def _send_wishlist(
        self, guild_id: int, channel: discord.abc.Messageable, matches: dict[int, list[dict]], shop_date: str
    ) -> None:
        """One message per chunk; only wishes whose mention was actually sent
        are marked notified, so a failed or cut-off chunk is not lost silently
        (those members are simply not pinged; they stay un-notified)."""
        allowed = discord.AllowedMentions(everyone=False, roles=False, users=True)
        for text, wish_ids in build_wishlist_messages(matches):
            try:
                await channel.send(text, allowed_mentions=allowed)
            except Exception as exc:
                await self._record_error(guild_id, f"wishlist ping failed: {exc}")
                return
            try:
                await self._repo.mark_notified(wish_ids, shop_date)
            except Exception:
                log.exception("fortnite: could not mark wishlist entries notified")

    async def _record_error(self, guild_id: int, message: str) -> None:
        log.warning("fortnite: daily post for guild %s: %s", guild_id, message)
        try:
            await self._repo.update_config(
                guild_id,
                last_error=message[:500],
                last_error_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        except Exception:
            log.exception("fortnite: could not store last_error")

    # -------------------------------------------------------------- Status
    async def status(self, guild_id: int) -> dict:
        cfg = await self._repo.get_config(guild_id) or {
            "guild_id": guild_id, "channel_id": None, "enabled": False, "include_jam_tracks": False,
            "ping_role_id": None, "last_posted_date": None, "last_posted_hash": None, "last_posted_at": None,
            "last_message_id": None, "last_error": None, "last_error_at": None,
        }
        now = datetime.now(timezone.utc)
        cache = self._cache
        next_at, pending = compute_next_post(cfg, now, self.post_delay_minutes, self.retry_at.get(guild_id))
        return {
            "config": cfg,
            "next_post_at": next_at.isoformat(timespec="seconds") if next_at else None,
            # True while today's post is due but not done yet (just enabled, or
            # the loop is waiting for the API to rotate / retrying after an error).
            "post_pending": pending,
            "post_delay_minutes": self.post_delay_minutes,
            "cache": {
                "shop_date": cache.shop_date if cache else None,
                "hash": cache.hash if cache else None,
                "entry_count": len(cache.entries) if cache else 0,
            },
            "last_fetch_error": self.last_fetch_error,
            "wishlist": await self._repo.count_wishes(guild_id),
            "source": SHOP_URL,
        }
