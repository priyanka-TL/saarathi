"""agents_and_configs

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-28 22:24:42.131325

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from sqlalchemy.dialects import postgresql
    
    agent_type_enum = postgresql.ENUM('llm', 'remote_flow', name='agent_type_enum')
    agent_type_enum.create(op.get_bind())
    
    agent_status_enum = postgresql.ENUM('enabled', 'disabled', name='agent_status_enum')
    agent_status_enum.create(op.get_bind())
    
    config_source_enum = postgresql.ENUM('yaml', 'db', name='config_source_enum')
    config_source_enum.create(op.get_bind())

    op.create_table('agents',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=False),
        sa.Column('agent_type', postgresql.ENUM('llm', 'remote_flow', name='agent_type_enum', create_type=False), nullable=False),
        sa.Column('status', postgresql.ENUM('enabled', 'disabled', name='agent_status_enum', create_type=False), server_default='enabled', nullable=False),
        sa.Column('is_default', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('sort_order', sa.Integer(), server_default='100', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id', name='pk_agents'),
        sa.UniqueConstraint('key', name='uq_agents_key'),
        sa.UniqueConstraint('name', name='uq_agents_name'),
        sa.CheckConstraint("key ~ '^[a-z][a-z0-9_]{1,62}$'", name='ck_agents_key_slug')
    )
    
    op.create_index('uq_agents_single_default', 'agents', ['is_default'], unique=True, postgresql_where=sa.text('is_default'))
    op.create_index('ix_agents_enabled', 'agents', ['sort_order', 'key'], postgresql_where=sa.text("status = 'enabled'"))
    op.create_index('ix_agents_updated', 'agents', [sa.text('updated_at DESC')], unique=False)

    op.create_table('agent_configurations',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('agent_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('source', postgresql.ENUM('yaml', 'db', name='config_source_enum', create_type=False), nullable=False),
        sa.Column('checksum', sa.String(), nullable=False),
        sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('activated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by', sa.String(), server_default='startup-sync', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], name='fk_agent_configurations_agent_id_agents', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name='pk_agent_configurations'),
        sa.UniqueConstraint('agent_id', 'version', name='uq_agent_cfg_version'),
        sa.CheckConstraint('version > 0', name='ck_agent_cfg_version'),
        sa.CheckConstraint('is_active = FALSE OR activated_at IS NOT NULL', name='ck_agent_cfg_activated'),
        sa.CheckConstraint("config ? 'agent_type' AND config ? 'key' AND config ? 'name'", name='ck_agent_cfg_type')
    )
    
    op.create_index('uq_agent_cfg_one_active', 'agent_configurations', ['agent_id'], unique=True, postgresql_where=sa.text('is_active'))
    op.create_index('ix_agent_cfg_lookup', 'agent_configurations', ['agent_id', 'source', sa.text('version DESC')])

    # Adding deferred foreign keys from conversation_messages to the new tables
    op.execute('DELETE FROM conversation_messages')
    op.create_foreign_key('fk_conversation_messages_agent_id_agents', 'conversation_messages', 'agents', ['agent_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key('fk_conversation_messages_agent_config_id_agent_configurations', 'conversation_messages', 'agent_configurations', ['agent_config_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('fk_conversation_messages_agent_config_id_agent_configurations', 'conversation_messages', type_='foreignkey')
    op.drop_constraint('fk_conversation_messages_agent_id_agents', 'conversation_messages', type_='foreignkey')

    op.drop_index('ix_agent_cfg_lookup', table_name='agent_configurations')
    op.drop_index('uq_agent_cfg_one_active', table_name='agent_configurations', postgresql_where=sa.text('is_active'))
    op.drop_table('agent_configurations')

    op.drop_index('ix_agents_updated', table_name='agents')
    op.drop_index('ix_agents_enabled', table_name='agents', postgresql_where=sa.text("status = 'enabled'"))
    op.drop_index('uq_agents_single_default', table_name='agents', postgresql_where=sa.text('is_default'))
    op.drop_table('agents')

    from sqlalchemy.dialects import postgresql
    postgresql.ENUM('yaml', 'db', name='config_source_enum').drop(op.get_bind())
    postgresql.ENUM('enabled', 'disabled', name='agent_status_enum').drop(op.get_bind())
    postgresql.ENUM('llm', 'remote_flow', name='agent_type_enum').drop(op.get_bind())
