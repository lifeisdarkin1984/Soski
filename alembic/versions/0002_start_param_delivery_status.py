"""start_param for processed_messages, status for deliveries

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('processed_messages', sa.Column('start_param', sa.String(255), nullable=True))
    op.add_column('deliveries', sa.Column('status', sa.String(20), nullable=False, server_default='sent'))
    op.add_column('deliveries', sa.Column('destination_msg_id', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column('deliveries', 'destination_msg_id')
    op.drop_column('deliveries', 'status')
    op.drop_column('processed_messages', 'start_param')
