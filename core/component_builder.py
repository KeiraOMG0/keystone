from __future__ import annotations
import json
import discord


def build_view(components: list[dict]) -> discord.ui.View | None:
    """Build a discord.ui.View from a list of template_components rows."""
    if not components:
        return None

    # Group by row_index
    rows: dict[int, list[dict]] = {}
    for comp in components:
        row_index = comp.get("row_index", 0)
        rows.setdefault(row_index, []).append(comp)

    view = discord.ui.View(timeout=None)

    for row_index in sorted(rows):
        for comp in sorted(rows[row_index], key=lambda c: c.get("position", 0)):
            ctype = comp.get("component_type", "button")
            if ctype == "button":
                _add_button(view, comp, row_index)
            elif ctype == "select":
                _add_select(view, comp, row_index)

    return view if view.children else None


def _add_button(view: discord.ui.View, comp: dict, row: int) -> None:
    style_int = comp.get("style") or 2  # SECONDARY default
    try:
        style = discord.ButtonStyle(style_int)
    except ValueError:
        style = discord.ButtonStyle.secondary

    label = comp.get("label") or "​"
    custom_id = comp.get("custom_id")
    url = comp.get("url")
    emoji_str = comp.get("emoji")
    emoji = discord.PartialEmoji.from_str(emoji_str) if emoji_str else None

    if url:
        btn = discord.ui.Button(
            style=discord.ButtonStyle.link,
            label=label,
            url=url,
            emoji=emoji,
            row=row,
        )
    else:
        btn = discord.ui.Button(
            style=style,
            label=label,
            custom_id=custom_id,
            emoji=emoji,
            row=row,
        )

    view.add_item(btn)


def _add_select(view: discord.ui.View, comp: dict, row: int) -> None:
    options_raw = comp.get("options_json")
    options: list[discord.SelectOption] = []
    if options_raw:
        try:
            items = json.loads(options_raw)
            for item in items:
                options.append(
                    discord.SelectOption(
                        label=item.get("label", "Option"),
                        value=item.get("value", item.get("label", "option")),
                        description=item.get("description"),
                        emoji=item.get("emoji"),
                    )
                )
        except (json.JSONDecodeError, TypeError):
            pass

    if not options:
        return

    custom_id = comp.get("custom_id") or f"select_{row}"
    placeholder = comp.get("placeholder") or "Select an option"

    select = discord.ui.Select(
        custom_id=custom_id,
        placeholder=placeholder,
        options=options,
        row=row,
    )
    view.add_item(select)
