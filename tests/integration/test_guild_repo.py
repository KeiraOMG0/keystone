import pytest

FAKE_GUILD_ID = 123456789012345678


async def test_get_or_create_returns_defaults(guild_repo):
    """get_or_create with a new guild_id returns a dict with guild_id set."""
    result = await guild_repo.get_or_create(FAKE_GUILD_ID)
    assert isinstance(result, dict)
    assert result["guild_id"] == FAKE_GUILD_ID


async def test_get_or_create_is_idempotent(guild_repo):
    """Calling get_or_create twice with the same guild_id returns the same result."""
    first = await guild_repo.get_or_create(FAKE_GUILD_ID)
    second = await guild_repo.get_or_create(FAKE_GUILD_ID)
    assert first["guild_id"] == second["guild_id"]
    assert first["created_at"] == second["created_at"]


async def test_update_guild_config(guild_repo):
    """update() persists changes to log_channel_id."""
    await guild_repo.get_or_create(FAKE_GUILD_ID)

    new_channel_id = 987654321098765432
    await guild_repo.update(FAKE_GUILD_ID, log_channel_id=new_channel_id)

    updated = await guild_repo.get(FAKE_GUILD_ID)
    assert updated is not None
    assert updated["log_channel_id"] == new_channel_id
