"""Add 'saathi_flow' to agent_type_enum

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-10 06:00:00.000000

WHY autocommit_block(), AND WHY A SEPARATE MIGRATION IS NOT ENOUGH
------------------------------------------------------------------
Postgres refuses to USE an enum label that the same transaction added --
`UnsafeNewEnumValueUsage`. Splitting the ALTER and the INSERT into two
migrations does NOT fix that on its own: alembic wraps an entire
`upgrade head` in ONE transaction unless `transaction_per_migration` is set, so
0011 and 0012 would still share it. Observed, not theorised: that is exactly
how this failed before the block below was added.

`op.get_context().autocommit_block()` ends the surrounding transaction, runs
the ALTER on its own, and starts a new one -- which is alembic's documented
answer for precisely this, and is safe here because this migration does nothing
else that would need rolling back with it.

`ADD VALUE IF NOT EXISTS` is idempotent, so a database an operator has already
patched by hand is left alone.

THERE IS NO DOWNGRADE. Postgres cannot DROP a value from an enum: the only
route is recreating the type and rewriting every column that uses it, which
would rewrite `agents` for a label that is inert when unused. The downgrade is
a documented no-op rather than a destructive rebuild.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0011"
down_revision: Union[str, Sequence[str], None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # OUTSIDE the surrounding transaction -- see the module docstring. Without
    # this, 0012's INSERT fails with UnsafeNewEnumValueUsage even though it is a
    # separate migration.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE agent_type_enum ADD VALUE IF NOT EXISTS 'saathi_flow'")


def downgrade() -> None:
    """Deliberately a no-op -- see the module docstring.

    An unused enum label costs nothing; removing it would mean recreating the
    type and rewriting `agents`.
    """
