"""audit_logs

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from sqlalchemy.dialects import postgresql

    audit_action_enum = postgresql.ENUM(
        'config_sync', 'config_create', 'config_activate',
        'agent_enable', 'agent_disable', 'session_finalize', 'session_abandon',
        name='audit_action_enum'
    )
    audit_action_enum.create(op.get_bind())

    op.create_table('audit_logs',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('action', postgresql.ENUM(
            'config_sync', 'config_create', 'config_activate',
            'agent_enable', 'agent_disable', 'session_finalize', 'session_abandon',
            name='audit_action_enum', create_type=False), nullable=False),
        sa.Column('entity_type', sa.String(), nullable=False),
        sa.Column('entity_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('actor', sa.String(), server_default='system', nullable=False),
        sa.Column('request_id', sa.String(), nullable=True),
        sa.Column('before', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('after', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('note', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id', name='pk_audit_logs'),
    )

    op.create_index('ix_audit_entity', 'audit_logs', ['entity_type', 'entity_id', sa.text('created_at DESC')], unique=False)
    op.create_index('ix_audit_action_time', 'audit_logs', ['action', sa.text('created_at DESC')], unique=False)


def downgrade() -> None:
    op.drop_index('ix_audit_action_time', table_name='audit_logs')
    op.drop_index('ix_audit_entity', table_name='audit_logs')
    op.drop_table('audit_logs')

    from sqlalchemy.dialects import postgresql
    postgresql.ENUM(
        'config_sync', 'config_create', 'config_activate',
        'agent_enable', 'agent_disable', 'session_finalize', 'session_abandon',
        name='audit_action_enum'
    ).drop(op.get_bind())
