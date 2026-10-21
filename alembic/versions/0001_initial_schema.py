"""Initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-08-02

"""
from alembic import op
import sqlalchemy as sa

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'accounts',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('phone', sa.String(20), nullable=False),
        sa.Column('status', sa.Enum('active', 'inactive', 'banned', name='accountstatus'), 
                  server_default='active', nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('phone')
    )

    op.create_table(
        'telegram_sessions',
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('session_string', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint('account_id'),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE')
    )

    op.create_table(
        'monitored_channels',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('channel_id', sa.BigInteger(), nullable=False),
        sa.Column('channel_username', sa.String(100), nullable=True),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('last_processed_msg_id', sa.BigInteger(), nullable=True, server_default='0'),
        sa.Column('added_at', sa.TIMESTAMP(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('channel_id', 'account_id', name='uq_channel_account'),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
        sa.Index('idx_channel_account', 'account_id', 'channel_id')
    )

    op.create_table(
        'processed_messages',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('channel_id', sa.BigInteger(), nullable=False),
        sa.Column('message_id', sa.BigInteger(), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('bot_username', sa.String(100), nullable=True),
        sa.Column('flow_status', 
                  sa.Enum('initiated', 'interacting', 'joined_forced', 'completed', 'failed', 
                          name='flowstatus'),
                  server_default='initiated', nullable=False),
        sa.Column('status', 
                  sa.Enum('pending', 'processing', 'scanned', 'completed', 'failed', 
                          name='processstatus'),
                  server_default='pending', nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('channel_id', 'message_id', 'account_id', name='uq_msg'),
        sa.Index('idx_msg_status_flow', 'status', 'flow_status'),
        sa.Index('idx_msg_account_channel', 'account_id', 'channel_id')
    )

    op.create_table(
        'flow_join_channels',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('processed_message_id', sa.Integer(), nullable=False),
        sa.Column('channel_id', sa.BigInteger(), nullable=False),
        sa.Column('channel_username', sa.String(100), nullable=True),
        sa.Column('joined_at', sa.TIMESTAMP(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['processed_message_id'], ['processed_messages.id'], ondelete='CASCADE'),
        sa.Index('idx_flow_join_msg', 'processed_message_id')
    )

    op.create_table(
        'collector_settings',
        sa.Column('setting_key', sa.String(50), nullable=False),
        sa.Column('setting_value', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('setting_key')
    )

    op.create_table(
        'system_logs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('level', sa.Enum('INFO', 'WARNING', 'ERROR', 'DEBUG', name='loglevel'), 
                  nullable=False),
        sa.Column('module', sa.String(50), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.Index('idx_log_level_time', 'level', 'created_at')
    )

    op.create_table(
        'deliveries',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('source_channel_id', sa.BigInteger(), nullable=True),
        sa.Column('source_msg_id', sa.BigInteger(), nullable=True),
        sa.Column('account_id', sa.Integer(), nullable=True),
        sa.Column('target_bot_username', sa.String(100), nullable=True),
        sa.Column('joined_channels_count', sa.Integer(), nullable=True, server_default='0'),
        sa.Column('report_text', sa.Text(), nullable=True),
        sa.Column('file_id', sa.String(500), nullable=True),
        sa.Column('file_unique_id', sa.String(255), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('file_unique_id', name='uq_delivery_file_unique'),
        sa.Index('idx_delivery_account', 'account_id'),
        sa.Index('idx_delivery_created', 'created_at')
    )


def downgrade() -> None:
    op.drop_table('deliveries')
    op.drop_table('system_logs')
    op.drop_table('collector_settings')
    op.drop_table('flow_join_channels')
    op.drop_table('processed_messages')
    op.drop_table('monitored_channels')
    op.drop_table('telegram_sessions')
    op.drop_table('accounts')
