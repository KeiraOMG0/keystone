"""Pure helpers behind the dashboard Roles page (services/role_admin_service.py)."""

import base64

import discord
import pytest

from services.role_admin_service import (
    RoleAdminError,
    all_catalog_flags,
    audit_reason,
    clamp_position,
    compute_reorder,
    copy_name,
    decode_icon,
    format_color,
    paginate,
    parse_color,
    permission_catalog,
    resolve_permission_change,
    unmanageable_reason,
    validate_role_name,
    validate_unicode_emoji,
)

KNOWN = all_catalog_flags()


def test_catalog_flags_are_canonical_discord_flags():
    valid = set(discord.Permissions.VALID_FLAGS)
    assert set(KNOWN) <= valid, set(KNOWN) - valid
    assert len(KNOWN) == len(set(KNOWN))
    for alias in ("read_messages", "manage_emojis", "manage_permissions", "use_external_emojis"):
        assert alias not in KNOWN


def test_catalog_filters_unknown_flags_and_groups():
    cat = permission_catalog({"administrator", "send_messages"})
    assert [g["group"] for g in cat] == ["Text", "Advanced"]
    assert cat[1]["permissions"][0] == {"key": "administrator", "label": "Administrator", "dangerous": True}
    groups = [g["group"] for g in permission_catalog(discord.Permissions.VALID_FLAGS)]
    assert groups == ["General", "Membership", "Text", "Voice", "Advanced"]


@pytest.mark.parametrize("raw,expected", [("#5865F2", 0x5865F2), ("5865f2", 0x5865F2), ("", 0), (None, 0), (" #000000 ", 0)])
def test_parse_color(raw, expected):
    assert parse_color(raw) == expected


@pytest.mark.parametrize("raw", ["#12345", "red", "#GGGGGG", "#1234567"])
def test_parse_color_rejects(raw):
    with pytest.raises(RoleAdminError) as e:
        parse_color(raw)
    assert e.value.status == 400 and e.value.code == "invalid_color"


def test_format_color():
    assert format_color(0) is None and format_color(0xABCDEF) == "#abcdef" and format_color(1) == "#000001"


def test_validate_role_name():
    assert validate_role_name("  Mods ") == "Mods"
    for bad in ("", "   ", None, "x" * 101, "@everyone", "@HERE"):
        with pytest.raises(RoleAdminError):
            validate_role_name(bad)


def _reason(**kw):
    base = dict(is_default=False, managed=False, position=3, bot_top_position=10, is_bot_top=False, bot_can_manage_roles=True)
    base.update(kw)
    return unmanageable_reason(**base)


def test_unmanageable_reason_matrix():
    assert _reason() is None
    assert "everyone" in _reason(is_default=True, position=0)
    assert "Managed" in _reason(managed=True)
    assert "bot's own" in _reason(is_bot_top=True, position=10)
    assert "Manage Roles" in _reason(bot_can_manage_roles=False)
    assert "above" in _reason(position=10)
    assert "above" in _reason(position=11)
    assert _reason(position=9) is None


def test_permission_change_only_bot_held_flags_can_change():
    held = {"send_messages", "embed_links", "kick_members"}
    # add and remove held flags: fine; an unheld flag already on the role stays.
    out = resolve_permission_change(
        ["send_messages", "kick_members", "ban_members"], {"embed_links", "ban_members"}, held, known_flags=KNOWN
    )
    assert out == {"send_messages", "kick_members", "ban_members"}
    with pytest.raises(RoleAdminError) as e:
        resolve_permission_change(["ban_members"], set(), held, known_flags=KNOWN)
    assert e.value.status == 403 and e.value.extra["permissions"] == ["ban_members"]
    with pytest.raises(RoleAdminError) as e:  # removing an unheld flag is refused too
        resolve_permission_change([], {"ban_members"}, held, known_flags=KNOWN)
    assert e.value.code == "permission_not_held"


def test_permission_change_unknown_and_none():
    assert resolve_permission_change(None, {"a"}, set(), known_flags=KNOWN) == {"a"}
    with pytest.raises(RoleAdminError) as e:
        resolve_permission_change(["read_messages"], set(), {"view_channel"}, known_flags=KNOWN)
    assert e.value.code == "unknown_permission"


def test_administrator_needs_bot_admin_and_confirm():
    with pytest.raises(RoleAdminError) as e:
        resolve_permission_change(["administrator"], set(), {"send_messages"}, known_flags=KNOWN)
    assert e.value.code == "permission_not_held"
    held = set(KNOWN)
    with pytest.raises(RoleAdminError) as e:
        resolve_permission_change(["administrator"], set(), held, known_flags=KNOWN)
    assert e.value.code == "administrator_needs_confirm"
    assert resolve_permission_change(["administrator"], set(), held, known_flags=KNOWN, confirm_administrator=True) == {"administrator"}
    # already admin: no confirm needed to keep it
    assert resolve_permission_change(["administrator"], {"administrator"}, held, known_flags=KNOWN) == {"administrator"}


def test_clamp_position():
    assert clamp_position(0, 10) == 1
    assert clamp_position(5, 10) == 5
    assert clamp_position(50, 10) == 9
    assert clamp_position(3, 1) == 1  # bot at the bottom: nothing manageable, still >= 1


def test_compute_reorder_moves_and_returns_only_changes():
    roles = [(100, 0), (1, 1), (2, 2), (3, 3), (4, 4), (99, 5), (50, 6)]  # bot top = 99 at 5
    assert compute_reorder(roles, 1, 4, 5) == {2: 1, 3: 2, 4: 3, 1: 4}
    assert compute_reorder(roles, 4, 1, 5) == {4: 1, 1: 2, 2: 3, 3: 4}
    assert compute_reorder(roles, 2, 2, 5) == {}
    # clamped: asking for the bot's slot or above lands directly under it
    assert compute_reorder(roles, 1, 99, 5) == {2: 1, 3: 2, 4: 3, 1: 4}


def test_compute_reorder_normalises_ties_and_refuses_unmanageable():
    roles = [(10, 1), (11, 1), (12, 2), (90, 4)]
    # ties ordered by id: 10, 11, 12 -> moving 12 to the bottom renumbers everyone
    assert compute_reorder(roles, 12, 1, 4) == {12: 1, 10: 2, 11: 3}
    with pytest.raises(RoleAdminError) as e:
        compute_reorder(roles, 90, 1, 4)
    assert e.value.status == 409 and e.value.code == "role_not_manageable"
    # three roles squeezed under a bot role at 3: renumbering would hit the bot's slot
    with pytest.raises(RoleAdminError) as e:
        compute_reorder([(10, 1), (11, 1), (12, 2), (90, 3)], 12, 1, 3)
    assert e.value.code == "positions_out_of_sync"


def test_audit_reason():
    assert audit_reason("keira") == "Dashboard: keira"
    assert audit_reason(None, "create role") == "Dashboard: unknown (create role)"
    assert len(audit_reason("x" * 1000)) == 512


def test_decode_icon():
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 10
    assert decode_icon(base64.b64encode(png).decode()) == png
    assert decode_icon("data:image/png;base64," + base64.b64encode(png).decode()) == png
    assert decode_icon(None) is None
    for bad in ("!!notb64", base64.b64encode(b"GIF89a....").decode(), base64.b64encode(b"\x89PNG" + b"0" * (256 * 1024)).decode()):
        with pytest.raises(RoleAdminError):
            decode_icon(bad)


def test_validate_unicode_emoji():
    assert validate_unicode_emoji(" \U0001F525 ") == "\U0001F525"
    assert validate_unicode_emoji("") is None and validate_unicode_emoji(None) is None
    for bad in ("<:x:123>", "abc", "\U0001F525" * 20):
        with pytest.raises(RoleAdminError):
            validate_unicode_emoji(bad)


def test_paginate_and_copy_name():
    items = list(range(30))
    assert paginate(items, 0, 10) == (list(range(10)), 30)
    assert paginate(items, 25, 10) == ([25, 26, 27, 28, 29], 30)
    assert paginate(items, -5, 1000)[0] == items  # clamped to the page max (100)
    assert copy_name("Mods") == "Mods (copy)"
    assert len(copy_name("x" * 100)) == 100 and copy_name("x" * 100).endswith(" (copy)")


def test_position_above():
    from services.role_admin_service import position_above

    roles = [(7, 0), (1, 1), (2, 2), (3, 3), (99, 4), (50, 5)]  # bot top = 99 at 4, @everyone id 7
    assert position_above(roles, 3, 7, 4, 7) == 1  # above @everyone = bottom
    assert position_above(roles, 3, 1, 4, 7) == 2  # directly above role 1
    # the moved role itself is ignored: new role 200 sitting at 1 (tie) goes above role 2
    assert position_above(roles + [(200, 1)], 200, 2, 4, 7) == 3
    with pytest.raises(RoleAdminError):
        position_above(roles, 3, 50, 4, 7)


# --- acting-user hierarchy and member counts ---------------------------------

from types import SimpleNamespace  # noqa: E402

from services.role_admin_service import (  # noqa: E402
    actor_grantable_permissions,
    actor_unmanageable_reason,
    role_member_counts,
)


def test_actor_unmanageable_reason():
    assert actor_unmanageable_reason(position=3, actor_top_position=4, is_owner=False) is None
    assert "your highest role" in actor_unmanageable_reason(position=4, actor_top_position=4, is_owner=False)
    assert actor_unmanageable_reason(position=9, actor_top_position=4, is_owner=False)
    assert actor_unmanageable_reason(position=9, actor_top_position=0, is_owner=True) is None


def test_actor_grantable_permissions():
    known = ["send_messages", "kick_members", "administrator"]
    assert actor_grantable_permissions(["send_messages"], is_owner=False, known_flags=known) == {"send_messages"}
    # Administrator holders may grant everything except Administrator itself.
    assert actor_grantable_permissions(["administrator"], is_owner=False, known_flags=known) == {"send_messages", "kick_members"}
    assert actor_grantable_permissions([], is_owner=True, known_flags=known) == set(known)
    assert actor_grantable_permissions(["bogus"], is_owner=False, known_flags=known) == set()


def test_permission_change_respects_actor_grantable():
    bot = {"send_messages", "kick_members", "administrator"}
    with pytest.raises(RoleAdminError) as e:
        resolve_permission_change(["kick_members"], set(), bot, known_flags=KNOWN, actor_grantable={"send_messages"})
    assert e.value.status == 403 and e.value.code == "actor_permission_not_held" and e.value.extra["permissions"] == ["kick_members"]
    # Removing a flag the actor lacks is refused too.
    with pytest.raises(RoleAdminError):
        resolve_permission_change([], {"kick_members"}, bot, known_flags=KNOWN, actor_grantable={"send_messages"})
    with pytest.raises(RoleAdminError) as e:
        resolve_permission_change(
            ["administrator"], set(), bot, known_flags=KNOWN, confirm_administrator=True, actor_grantable={"send_messages", "kick_members"}
        )
    assert "owner" in e.value.message
    # Untouched flags the actor lacks are fine; None = no actor limit.
    assert resolve_permission_change(
        ["kick_members", "send_messages"], {"kick_members"}, bot, known_flags=KNOWN, actor_grantable={"send_messages"}
    ) == {"kick_members", "send_messages"}
    assert resolve_permission_change(["kick_members"], set(), bot, known_flags=KNOWN, actor_grantable=None) == {"kick_members"}


def test_compute_reorder_ceiling_clamps_below_actor_top():
    roles = [(1, 1), (2, 2), (3, 3), (4, 4)]  # bot top at 5; actor top = role 3
    assert compute_reorder(roles, 1, 99, 5, ceiling_role_id=3) == {2: 1, 1: 2}
    assert compute_reorder(roles, 2, 4, 5, ceiling_role_id=3) == {}
    # A ceiling outside the bot's band (at/above the bot) adds no limit.
    assert compute_reorder(roles, 1, 99, 5, ceiling_role_id=77) == {2: 1, 3: 2, 4: 3, 1: 4}
    assert compute_reorder(roles, 4, 1, 5, ceiling_role_id=3) == {4: 1, 1: 2, 2: 3, 3: 4}


def test_role_member_counts_single_pass():
    members = [
        SimpleNamespace(_roles=[10, 11]),
        SimpleNamespace(_roles=[11]),
        SimpleNamespace(roles=[SimpleNamespace(id=99), SimpleNamespace(id=10)]),  # no _roles: fall back
    ]
    counts = role_member_counts(members, 99, None)
    assert counts[10] == 2 and counts[11] == 2 and counts[99] == 3
    assert role_member_counts(members, 99, 500)[99] == 500
    assert role_member_counts([], 99, None) == {99: 0}
