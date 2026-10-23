"""distribution tables: receiver_bots, media_posts, forced_channels, video_deliveries
(فقط جدول‌های جدید اضافه می‌شوند؛ به داده‌ی موجود دست نمی‌زند)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-23
"""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'receiver_bots',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('bot_id', sa.BigInteger(), nullable=False, unique=True),
        sa.Column('username', sa.String(64), nullable=False),
        sa.Column('token_enc', sa.Text(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('last_assigned_seq', sa.BigInteger(), nullable=False, server_default=sa.text('0')),
        sa.Column('assigned_count', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('request_count', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('error_count', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('last_health_at', sa.DateTime(), nullable=True),
        sa.Column('last_health_ok', sa.Boolean(), nullable=True),
        sa.Column('last_error', sa.String(255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_table(
        'media_posts',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('delivery_id', sa.Integer(), sa.ForeignKey('deliveries.id', ondelete='RESTRICT'),
                  nullable=False, unique=True),
        sa.Column('token', sa.String(32), nullable=False, unique=True),
        sa.Column('storage_chat_id', sa.BigInteger(), nullable=False),
        sa.Column('storage_msg_id', sa.BigInteger(), nullable=False),
        sa.Column('caption', sa.Text(), nullable=True),
        sa.Column('final_caption', sa.Text(), nullable=True),
        sa.Column('caption_trimmed', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('assigned_bot_id', sa.Integer(), sa.ForeignKey('receiver_bots.id', ondelete='RESTRICT'),
                  nullable=True),
        sa.Column('final_chat_id', sa.BigInteger(), nullable=True),
        sa.Column('final_msg_id', sa.BigInteger(), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('error_message', sa.String(500), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('published_at', sa.DateTime(), nullable=True),
    )
    op.create_index('idx_media_posts_status', 'media_posts', ['status', 'next_attempt_at'])
    op.create_table(
        'forced_channels',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('chat_id', sa.BigInteger(), nullable=False, unique=True),
        sa.Column('title', sa.String(255), nullable=True),
        sa.Column('link', sa.String(255), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_table(
        'video_deliveries',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('media_post_id', sa.Integer(), sa.ForeignKey('media_posts.id', ondelete='RESTRICT'),
                  nullable=False),
        sa.Column('bot_id', sa.Integer(), sa.ForeignKey('receiver_bots.id', ondelete='RESTRICT'),
                  nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('video_msg_id', sa.BigInteger(), nullable=True),
        sa.Column('guide_msg_id', sa.BigInteger(), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='sending'),
        sa.Column('delete_at', sa.DateTime(), nullable=True),
        sa.Column('delete_attempts', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('error', sa.String(255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
    )
    op.create_index('idx_vd_due', 'video_deliveries', ['status', 'delete_at'])
    op.create_index('idx_vd_user_media', 'video_deliveries', ['user_id', 'media_post_id'])


def downgrade() -> None:
    op.drop_table('video_deliveries')
    op.drop_table('forced_channels')
    op.drop_table('media_posts')
    op.drop_table('receiver_bots')
