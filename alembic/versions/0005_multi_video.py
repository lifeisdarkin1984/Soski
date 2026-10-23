"""media_posts.storage_msg_ids: چند ویدیو در یک بسته (فقط یک ستون اضافه می‌شود)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-24
"""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('media_posts', sa.Column('storage_msg_ids', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('media_posts', 'storage_msg_ids')
