"""Initial tables

Revision ID: 0001
Revises: 
Create Date: 2026-07-28 21:21:35.580026

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from sqlalchemy.dialects import postgresql
    
    # Create ENUMs first
    conversation_status_enum = postgresql.ENUM('active', 'archived', name='conversation_status_enum')
    conversation_status_enum.create(op.get_bind())
    
    message_role_enum = postgresql.ENUM('user', 'assistant', 'system', 'tool', name='message_role_enum')
    message_role_enum.create(op.get_bind())

    # Create tables
    op.create_table('conversations',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('tenant_code', sa.String(), nullable=False),
        sa.Column('organization_id', sa.String(), nullable=True),
        sa.Column('external_user_id', sa.String(), nullable=False),
        sa.Column('title', sa.String(), nullable=True),
        sa.Column('status', postgresql.ENUM('active', 'archived', name='conversation_status_enum', create_type=False), server_default='active', nullable=False),
        sa.Column('pinned_agent_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('locale', sa.String(), server_default='en', nullable=False),
        sa.Column('message_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('last_message_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("locale IN ('en', 'hi', 'kn', 'te')", name='ck_conversations_conv_locale'),
        sa.CheckConstraint('message_count >= 0', name='ck_conversations_conv_msgcount'),
        sa.CheckConstraint("tenant_code <> ''", name='ck_conversations_conv_tenant'),
        sa.PrimaryKeyConstraint('id', name='pk_conversations')
    )
    
    op.create_index('ix_conv_org', 'conversations', ['tenant_code', 'organization_id', sa.text('last_message_at DESC NULLS LAST')], unique=False, postgresql_where=sa.text("status = 'active' AND organization_id IS NOT NULL"))
    op.create_index('ix_conv_pinned', 'conversations', ['pinned_agent_id'], unique=False, postgresql_where=sa.text('pinned_agent_id IS NOT NULL'))
    op.create_index('ix_conv_user_recent', 'conversations', ['tenant_code', 'external_user_id', sa.text('last_message_at DESC NULLS LAST')], unique=False, postgresql_where=sa.text("status = 'active'"))

    op.create_table('conversation_messages',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('conversation_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('role', postgresql.ENUM('user', 'assistant', 'system', 'tool', name='message_role_enum', create_type=False), nullable=False),
        sa.Column('content', sa.String(), nullable=False),
        sa.Column('agent_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('agent_config_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('agent_session_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('route_reason', sa.String(), nullable=True),
        sa.Column('route_confidence', sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column('options', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('selected_option_id', sa.String(), nullable=True),
        sa.Column('model', sa.String(), nullable=True),
        sa.Column('prompt_tokens', sa.Integer(), nullable=True),
        sa.Column('completion_tokens', sa.Integer(), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('error', sa.String(), nullable=True),
        sa.Column('request_id', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("COALESCE(latency_ms, 0) >= 0", name='ck_conversation_messages_msg_latency'),
        sa.CheckConstraint("COALESCE(prompt_tokens, 0) >= 0 AND COALESCE(completion_tokens, 0) >= 0", name='ck_conversation_messages_msg_tokens'),
        sa.CheckConstraint("options IS NULL OR role = 'assistant'", name='ck_conversation_messages_msg_options_only_assistant'),
        sa.CheckConstraint("role <> 'assistant' OR agent_id IS NOT NULL", name='ck_conversation_messages_msg_assistant_attribution'),
        sa.CheckConstraint('route_confidence IS NULL OR (route_confidence >= 0 AND route_confidence <= 1)', name='ck_conversation_messages_msg_confidence'),
        sa.CheckConstraint('seq > 0', name='ck_conversation_messages_msg_seq'),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], name='fk_conversation_messages_conversation_id_conversations', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name='pk_conversation_messages'),
        sa.UniqueConstraint('conversation_id', 'seq', name='uq_msg_seq')
    )
    
    op.create_index('ix_msg_agent_time', 'conversation_messages', ['agent_id', sa.text('created_at DESC')], unique=False)
    op.create_index('ix_msg_conv_seq', 'conversation_messages', ['conversation_id', 'seq'], unique=False)
    op.create_index('ix_msg_request', 'conversation_messages', ['request_id'], unique=False, postgresql_where=sa.text('request_id IS NOT NULL'))
    op.create_index('ix_msg_session', 'conversation_messages', ['agent_session_id'], unique=False, postgresql_where=sa.text('agent_session_id IS NOT NULL'))


def downgrade() -> None:
    op.drop_index('ix_msg_session', table_name='conversation_messages', postgresql_where=sa.text('agent_session_id IS NOT NULL'))
    op.drop_index('ix_msg_request', table_name='conversation_messages', postgresql_where=sa.text('request_id IS NOT NULL'))
    op.drop_index('ix_msg_conv_seq', table_name='conversation_messages')
    op.drop_index('ix_msg_agent_time', table_name='conversation_messages')
    op.drop_table('conversation_messages')
    op.drop_index('ix_conv_user_recent', table_name='conversations', postgresql_where=sa.text("status = 'active'"))
    op.drop_index('ix_conv_pinned', table_name='conversations', postgresql_where=sa.text('pinned_agent_id IS NOT NULL'))
    op.drop_index('ix_conv_org', table_name='conversations', postgresql_where=sa.text("status = 'active' AND organization_id IS NOT NULL"))
    op.drop_table('conversations')
    
    from sqlalchemy.dialects import postgresql
    postgresql.ENUM('user', 'assistant', 'system', 'tool', name='message_role_enum').drop(op.get_bind())
    postgresql.ENUM('active', 'archived', name='conversation_status_enum').drop(op.get_bind())
