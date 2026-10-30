"""error_reports: browser crash reports + unhandled backend exceptions

Backs the owner-only Debug page's Errors tab (server/services/error_reports.py).
Purely additive: a new table, nothing else changes.

Revision ID: c7e2a9d4b1f3
Revises: b3c1d2e4f5a6
Create Date: 2026-10-30 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c7e2a9d4b1f3'
down_revision: Union[str, None] = 'b3c1d2e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'error_reports',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('fingerprint', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('stack', sa.Text(), nullable=True),
        sa.Column('component_stack', sa.Text(), nullable=True),
        sa.Column('path', sa.String(length=300), nullable=True),
        sa.Column('user_agent', sa.String(length=400), nullable=True),
        sa.Column('user_id', sa.BigInteger(), nullable=True),
        sa.Column('username', sa.String(length=64), nullable=True),
    )
    op.create_index('ix_error_reports_created_at', 'error_reports', ['created_at'])
    op.create_index('ix_error_reports_fingerprint', 'error_reports', ['fingerprint'])


def downgrade() -> None:
    op.drop_index('ix_error_reports_fingerprint', table_name='error_reports')
    op.drop_index('ix_error_reports_created_at', table_name='error_reports')
    op.drop_table('error_reports')
