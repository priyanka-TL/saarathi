"""Downloadable documents on a message

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-11 09:00:00.000000

WHY A COLUMN AND NOT A REUSE OF `options`
-----------------------------------------
`conversation_messages.options` is the only per-message extras blob today, and
it is the wrong one: it is semantically claimed as choice buttons, the SPA
renders it as click-to-reply controls, and a click posts the value back as the
next user turn. A download URL in that list would be sent to the agent as user
input.

WHY A COLUMN AND NOT NOTHING
----------------------------
The reply that carries a document is an ordinary assistant message. Reopening
the conversation from the sidebar, or reloading the page, replays it from
`GET /api/conversations/{id}/messages` -- and anything not stored on the row is
simply gone. A download that survives only the turn that produced it is a
document the user can no longer reach at all, because nothing else in the UI
links to it.

The other artifact channel, `agent_sessions.report_url`, is per SESSION and set
once at finalisation. These arrive mid-conversation, more than one at a time,
and repeatedly. Per-message is the right grain; this column is that grain.

SHAPE: a JSON array of objects, each
    {"file_name": str, "format": str, "media_type": str, "url": str}
one entry per FILE, so a document offered as PDF and DOCX is two entries. NULL
and `[]` both mean "no documents"; the writer stores NULL for the empty case,
matching how `options` behaves.

Mirrors `options` in every structural respect, including the assistant-only
CHECK -- a user message cannot carry one, because nothing ever produces one.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0014"
down_revision: Union[str, Sequence[str], None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversation_messages",
        sa.Column("attachments", JSONB(none_as_null=True), nullable=True),
    )
    # BARE SUFFIX, like every other CHECK in this schema: Base.metadata's naming
    # convention interpolates %(constraint_name)s, so a qualified name here gets
    # double-prefixed. See CLAUDE.md.
    op.create_check_constraint(
        "attachments_only_assistant",
        "conversation_messages",
        "attachments IS NULL OR role = 'assistant'",
    )


def downgrade() -> None:
    # RAW SQL, and deliberately. `op.drop_constraint` runs the name through
    # Base.metadata's naming convention, which re-applies the
    # `ck_%(table_name)s_` prefix to a name that already has it -- producing
    # `ck_conversation_messages_ck_conversation_messages_attac_ec6c` and a
    # "constraint does not exist" failure. The same double-prefix trap the
    # BARE-suffix rule in CLAUDE.md exists to avoid, in its drop-side form.
    op.execute(
        "ALTER TABLE conversation_messages "
        "DROP CONSTRAINT IF EXISTS ck_conversation_messages_attachments_only_assistant"
    )
    op.drop_column("conversation_messages", "attachments")
