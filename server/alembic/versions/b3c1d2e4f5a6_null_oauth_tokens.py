"""stop storing Discord OAuth tokens: make the token columns nullable and null them

The dashboard never used the stored access/refresh tokens (the access token is
only needed inside the OAuth callback), so they were pure liability. The
columns stay (SQLite-friendly, no model churn) but are nullable and always
NULL from now on.

Revision ID: b3c1d2e4f5a6
Revises: 77e8789ecc98
Create Date: 2026-10-14 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b3c1d2e4f5a6'
down_revision: Union[str, None] = '77e8789ecc98'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite cannot ALTER COLUMN in place: batch mode copies the table.
    with op.batch_alter_table('sessions') as batch:
        batch.alter_column('discord_access_token', existing_type=sa.String(length=255), nullable=True)
        batch.alter_column('discord_refresh_token', existing_type=sa.String(length=255), nullable=True)
        batch.alter_column('token_expires_at', existing_type=sa.DateTime(timezone=True), nullable=True)
    op.execute(
        "UPDATE sessions SET discord_access_token = NULL, discord_refresh_token = NULL, token_expires_at = NULL"
    )


def downgrade() -> None:
    # The tokens are gone for good; refill with placeholders so NOT NULL holds.
    op.execute(
        "UPDATE sessions SET discord_access_token = COALESCE(discord_access_token, ''), "
        "discord_refresh_token = COALESCE(discord_refresh_token, ''), "
        "token_expires_at = COALESCE(token_expires_at, last_seen_at)"
    )
    with op.batch_alter_table('sessions') as batch:
        batch.alter_column('discord_access_token', existing_type=sa.String(length=255), nullable=False)
        batch.alter_column('discord_refresh_token', existing_type=sa.String(length=255), nullable=False)
        batch.alter_column('token_expires_at', existing_type=sa.DateTime(timezone=True), nullable=False)
