"""tool_executions

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-29 12:00:00.000000

§4.8 — tool_executions table and its two supporting ENUMs.

Design decisions baked into the DDL:

  1. ON DELETE CASCADE from message_id.
     Retention rides conversation retention automatically. Deleting a
     conversation cascades to its messages, which cascade to their tool
     executions. No separate retention policy needed.

  2. ck_tool_error: status != 'success' implies error IS NOT NULL.
     A failure record must always explain itself. An error row with a NULL
     error column is unintelligible and should be rejected at write time, not
     discovered during a post-incident investigation.

  3. result_excerpt is TEXT (not VARCHAR). Truncation to 4096 chars is done
     at WRITE TIME by the repository layer (not enforced by DB length) so
     one large tool result cannot distort the table. The constraint is in the
     application, not in a CHECK — we log that truncation happened so it is
     observable, unlike a silent DB-side TRUNCATE function.

  4. agent_id FK is ON DELETE SET NULL (not CASCADE) because the execution
     record is evidence of what happened; losing agent attribution when an
     agent is decommissioned is acceptable, but losing the record itself is not.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from sqlalchemy.dialects import postgresql

    # Create the tool_status_enum type BEFORE the table that uses it.
    # Alembic autogenerate does not manage PG ENUM types; create explicitly
    # with create_type=False on the column to avoid double-create (§4.10).
    tool_status_enum = postgresql.ENUM(
        'success', 'error', 'timeout',
        name='tool_status_enum',
    )
    tool_status_enum.create(op.get_bind())

    op.create_table(
        'tool_executions',
        sa.Column(
            'id',
            postgresql.UUID(as_uuid=True),
            server_default=sa.text('gen_random_uuid()'),
            nullable=False,
        ),
        # message_id → ON DELETE CASCADE: retention rides conversation retention
        sa.Column('message_id', postgresql.UUID(as_uuid=True), nullable=False),
        # agent_id → ON DELETE SET NULL: attribution optional, record mandatory
        sa.Column('agent_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('tool_name', sa.String(), nullable=False),
        sa.Column('iteration', sa.SmallInteger(), server_default='1', nullable=False),
        sa.Column(
            'arguments',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        # result_excerpt: truncated to 4096 chars at write time by the repo layer
        sa.Column('result_excerpt', sa.Text(), nullable=True),
        sa.Column('result_bytes', sa.Integer(), nullable=True),
        sa.Column(
            'status',
            postgresql.ENUM('success', 'error', 'timeout',
                            name='tool_status_enum', create_type=False),
            nullable=False,
        ),
        # error: required when status != 'success' (enforced by ck_tool_error below)
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=False),
        sa.Column('request_id', sa.String(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        # Primary key
        sa.PrimaryKeyConstraint('id', name='pk_tool_executions'),
        # Foreign keys
        sa.ForeignKeyConstraint(
            ['message_id'], ['conversation_messages.id'],
            name='fk_tool_executions_message_id_conversation_messages',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['agent_id'], ['agents.id'],
            name='fk_tool_executions_agent_id_agents',
            ondelete='SET NULL',
        ),
        # Constraints — pass short suffix, naming convention adds the table prefix
        # (see 0003 comment on this pattern)
        sa.CheckConstraint('iteration BETWEEN 1 AND 10', name='tool_iteration'),
        sa.CheckConstraint('duration_ms >= 0',           name='tool_duration'),
        # ck_tool_error: a non-success status with no error message is unintelligible
        sa.CheckConstraint(
            "status = 'success' OR error IS NOT NULL",
            name='tool_error',
        ),
    )

    # ix_tool_message: the primary lookup path — all executions for a message
    op.create_index('ix_tool_message', 'tool_executions', ['message_id'])

    # ix_tool_name_time: per-tool latency and volume queries (§13.3 runbook)
    op.create_index(
        'ix_tool_name_time', 'tool_executions',
        ['tool_name', sa.text('created_at DESC')],
    )

    # ix_tool_failures: partial index — error rate by tool (§13.3 runbook query 1)
    op.create_index(
        'ix_tool_failures', 'tool_executions',
        ['tool_name', sa.text('created_at DESC')],
        postgresql_where=sa.text("status <> 'success'"),
    )


def downgrade() -> None:
    op.drop_index(
        'ix_tool_failures', table_name='tool_executions',
        postgresql_where=sa.text("status <> 'success'"),
    )
    op.drop_index('ix_tool_name_time', table_name='tool_executions')
    op.drop_index('ix_tool_message', table_name='tool_executions')
    op.drop_table('tool_executions')

    from sqlalchemy.dialects import postgresql
    postgresql.ENUM('success', 'error', 'timeout', name='tool_status_enum').drop(
        op.get_bind()
    )
