"""services/hierarchy.py: shared member / self-role rank checks."""

from types import SimpleNamespace as NS

from services.hierarchy import dangerous_flags, member_action_block_reason, role_grant_block_reason


def role(pos, rid=None, managed=False, default=False, **perms):
    return NS(id=rid or 1000 + pos, position=pos, managed=managed, is_default=lambda: default, permissions=NS(**perms))


def member(mid, top_pos):
    return NS(id=mid, top_role=role(top_pos))


BOT = member(1, 50)


def test_member_rules():
    mod = member(2, 20)
    assert member_action_block_reason(mod, member(3, 10), BOT) is None
    assert "yours" in member_action_block_reason(mod, member(3, 20), BOT)
    assert "yours" in member_action_block_reason(mod, member(3, 30), BOT)
    assert "mine" in member_action_block_reason(mod, member(3, 50), BOT, actor_is_owner=True)
    assert member_action_block_reason(mod, member(3, 30), BOT, actor_is_owner=True) is None
    assert "yourself" in member_action_block_reason(mod, mod, BOT)
    assert "myself" in member_action_block_reason(mod, BOT, BOT)
    assert "owner" in member_action_block_reason(mod, member(9, 1), BOT, guild_owner_id=9)


def test_non_member_target_only_identity_rules():
    assert member_action_block_reason(member(2, 1), NS(id=77), BOT) is None


def test_role_rules():
    assert role_grant_block_reason(role(10), BOT) is None
    assert "@everyone" in role_grant_block_reason(role(0, default=True), BOT)
    assert "managed" in role_grant_block_reason(role(10, managed=True), BOT)
    assert "my top role" in role_grant_block_reason(role(50), BOT)
    assert "administrator" in role_grant_block_reason(role(10, administrator=True), BOT)
    assert "staff" in role_grant_block_reason(role(10, rid=555), BOT, privileged_role_ids=[555])
    assert "your top role" in role_grant_block_reason(role(10), BOT, actor=member(2, 10))
    assert role_grant_block_reason(role(10), BOT, actor=member(2, 5), actor_is_owner=True) is None
    assert dangerous_flags(role(1, kick_members=True, ban_members=True)) == ["ban_members", "kick_members"]
