"""channel_title for monitored_channels

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-22
"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('monitored_channels', sa.Column('channel_title', sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column('monitored_channels', 'channel_title')
