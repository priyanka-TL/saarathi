"""agent_sessions

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-28 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from sqlalchemy.dialects import postgresql

    session_state_enum = postgresql.ENUM(
        'pending', 'authenticating', 'in_progress', 'awaiting_user',
        'finalizing', 'completed', 'failed', 'abandoned',
        name='session_state_enum'
    )
    session_state_enum.create(op.get_bind())

    op.create_table('agent_sessions',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('conversation_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('agent_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('state', postgresql.ENUM(
            'pending', 'authenticating', 'in_progress', 'awaiting_user',
            'finalizing', 'completed', 'failed', 'abandoned',
            name='session_state_enum', create_type=False), server_default='pending', nullable=False),
        sa.Column('remote_provider', sa.String(), nullable=True),
        sa.Column('remote_session_id', sa.String(), nullable=True),
        sa.Column('remote_profile_id', sa.String(), nullable=True),
        sa.Column('remote_flow', sa.String(), nullable=True),
        sa.Column('remote_bot_route', sa.String(), nullable=True),
        sa.Column('language', sa.String(), server_default='en', nullable=False),
        sa.Column('step', sa.Integer(), server_default='0', nullable=False),
        sa.Column('turn_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('result_ref', sa.String(), nullable=True),
        sa.Column('report_url', sa.String(), nullable=True),
        sa.Column('error', sa.String(), nullable=True),
        sa.Column('error_code', sa.String(), nullable=True),
        sa.Column('state_data', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_activity_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('finalized_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], name='fk_agent_sessions_conversation_id_conversations', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], name='fk_agent_sessions_agent_id_agents', ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name='pk_agent_sessions'),
        sa.UniqueConstraint('remote_session_id', name='uq_sess_remote'),
        # NOTE: short, unqualified suffixes here (not the fully-qualified
        # "ck_agent_sessions_..." form 0001/0002 used) -- Base.metadata's
        # naming convention ("ck": "ck_%(table_name)s_%(constraint_name)s")
        # is re-applied by Alembic at op.create_table() execution time even
        # when an already-qualified name is given, which is why 0001/0002's
        # CHECK constraints ended up double-prefixed in the live DB
        # (verified via pg_constraint). Passing the short suffix here lets
        # the convention apply exactly once.
        sa.CheckConstraint("language IN ('en', 'hi', 'kn', 'te')", name='sess_lang'),
        sa.CheckConstraint('step >= 0 AND turn_count >= 0', name='sess_step'),
        sa.CheckConstraint(
            "(state IN ('completed', 'failed', 'abandoned')) = (ended_at IS NOT NULL)",
            name='sess_terminal'),
        sa.CheckConstraint(
            "state IN ('pending', 'failed', 'abandoned') OR remote_session_id IS NOT NULL",
            name='sess_active_has_remote'),
        sa.CheckConstraint(
            "state <> 'completed' OR result_ref IS NOT NULL",
            name='sess_completed_has_result'),
    )

    # at most ONE open session per conversation -- the pin invariant, in the database
    op.create_index('uq_sess_one_open_per_conv', 'agent_sessions', ['conversation_id'], unique=True,
        postgresql_where=sa.text("state NOT IN ('completed', 'failed', 'abandoned')"))
    op.create_index('ix_sess_sweep', 'agent_sessions', ['state', 'last_activity_at'],
        postgresql_where=sa.text("state NOT IN ('completed', 'failed', 'abandoned')"))
    op.create_index('ix_sess_conv', 'agent_sessions', ['conversation_id', sa.text('started_at DESC')], unique=False)

    # Deferred FKs onto columns that predate their owning tables (0001).
    # Both columns are confirmed all-NULL in the live data, so no safety
    # wipe is needed here (unlike 0002's DELETE FROM conversation_messages,
    # which was needed because THAT column's data predated agents/agent_configurations).
    op.create_foreign_key('fk_conversation_messages_agent_session_id_agent_sessions',
        'conversation_messages', 'agent_sessions', ['agent_session_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key('fk_conversations_pinned_agent_id_agents',
        'conversations', 'agents', ['pinned_agent_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('fk_conversations_pinned_agent_id_agents', 'conversations', type_='foreignkey')
    op.drop_constraint('fk_conversation_messages_agent_session_id_agent_sessions', 'conversation_messages', type_='foreignkey')

    op.drop_index('ix_sess_conv', table_name='agent_sessions')
    op.drop_index('ix_sess_sweep', table_name='agent_sessions', postgresql_where=sa.text("state NOT IN ('completed', 'failed', 'abandoned')"))
    op.drop_index('uq_sess_one_open_per_conv', table_name='agent_sessions', postgresql_where=sa.text("state NOT IN ('completed', 'failed', 'abandoned')"))
    op.drop_table('agent_sessions')

    from sqlalchemy.dialects import postgresql
    postgresql.ENUM(
        'pending', 'authenticating', 'in_progress', 'awaiting_user',
        'finalizing', 'completed', 'failed', 'abandoned',
        name='session_state_enum'
    ).drop(op.get_bind())
