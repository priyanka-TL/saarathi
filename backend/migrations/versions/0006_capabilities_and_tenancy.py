"""capabilities_and_tenancy

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-01 21:00:00.000000

Makes the sidebar's capability catalogue database-driven, and makes agent
configuration per-tenant.

FOUR CHANGES
------------
  1. NEW `capabilities`       -- the sidebar's cards, previously
                                 app/config/ui/capabilities.yaml.
  2. NEW `capability_agents`  -- which agents sit under which capability.
  3. ALTER `agents`           -- gains tenant_id, organization_id, icon, metadata.
  4. RENAME `agent_configurations` -> `agent_configs`, + tenant_id/organization_id.

WHAT THIS DOES NOT DO
---------------------
It does not move agent configuration into the database -- that was already
true. `agents` + `agent_configurations` have been the runtime source since
migration 0002; `AgentRegistry.reload()` reads them, the admin routes write
them, and CONFIG_SYNC_MODE=off makes the YAML unread entirely. The YAML is a
seed. What was missing was the TENANT dimension, which is what this adds.

It does not create `tenants` or `users`. Those are the user service's records;
Saarthi is not their system of record. `tenant_id` and `organization_id` here
are plain strings carrying the values that arrive as JWT claims
(app/services/identity.py) -- so there is NO foreign key to them, and there
cannot be, because the referent lives in another service. A row naming a tenant
the user service never issued is simply never read: inert, not broken.
`conversations.tenant_code` (migration 0001) already works this way.

THE DEFAULT SCOPE
-----------------
Every scoped row carries `tenant_id` and `organization_id`, NOT NULL, defaulting
to the literal `'default'` (DEFAULT_SCOPE below). A `'default'` row is what
every tenant sees until a more specific row exists for it, so:

  * onboarding a tenant needs ZERO rows -- it inherits the default catalogue;
  * shipping a new capability to everyone is ONE insert at default scope;
  * a tenant customises by inserting its own row, which shadows the default.

Resolution is most-specific-wins, three levels:

    (tenant, org)  >  (tenant, 'default')  >  ('default', 'default')

A sentinel string rather than NULL, deliberately: NULL would make every unique
constraint below a partial index (NULLs do not compare equal in Postgres), and
every lookup an IS NOT DISTINCT FROM. The sentinel keeps both ordinary.

WHY TENANT-SCOPED `agent_configs` RATHER THAN A PATCH
-----------------------------------------------------
A tenant's agent configuration is a WHOLE config row at that tenant's scope,
not a diff merged over a base. That matters beyond taste: HandlerFactory caches
handlers by `(spec.key, checksum)`, so if two tenants' specs differed while
sharing a checksum, one tenant's handler -- including its system prompt --
would be served to the other. Because each scoped row carries its own checksum
computed from its own content, that cannot happen here by construction. Two
tenants whose configs are byte-identical DO share a handler, which is correct.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The sentinel that means "applies to every tenant / every organization".
DEFAULT_SCOPE = 'default'


def upgrade() -> None:
    from sqlalchemy.dialects import postgresql

    capability_status_enum = postgresql.ENUM(
        'active', 'disabled', 'coming_soon', name='capability_status_enum'
    )
    capability_status_enum.create(op.get_bind())

    # New audit actions. ALTER TYPE ... ADD VALUE cannot run inside a
    # transaction block on PostgreSQL < 12; alembic runs migrations in one, so
    # COMMIT first. Safe here because the only statement above is idempotent
    # DDL, and a failure after this point leaves unused enum labels behind,
    # which are harmless.
    op.execute("COMMIT")
    for value in (
        'capability_create', 'capability_update', 'capability_delete',
        'capability_agent_set',
    ):
        op.execute(f"ALTER TYPE audit_action_enum ADD VALUE IF NOT EXISTS '{value}'")

    # ------------------------------------------------------------------
    # 1. capabilities
    # ------------------------------------------------------------------
    op.create_table(
        'capabilities',
        sa.Column('id', postgresql.UUID(as_uuid=True),
                  server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('tenant_id', sa.String(), server_default=DEFAULT_SCOPE, nullable=False),
        sa.Column('organization_id', sa.String(), server_default=DEFAULT_SCOPE, nullable=False),
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        # A REGISTRY KEY ('brain', 'globe'), never a path or a URL. The icons
        # are inline SVG in the frontend so they can inherit currentColor; an
        # <img src> could not follow the theme. Storing a URL here would push
        # the frontend towards one.
        sa.Column('icon', sa.String(), nullable=True),
        sa.Column('badge', sa.String(), nullable=True),
        sa.Column('status', postgresql.ENUM('active', 'disabled', 'coming_soon',
                                            name='capability_status_enum', create_type=False),
                  server_default='active', nullable=False),
        sa.Column('display_order', sa.Integer(), server_default='100', nullable=False),
        # Holds `action` -- what clicking the CARD does, e.g.
        # {"action": {"type": "display_card"}}. The set of action types is
        # closed and lives in the frontend's ChatPage registry; an unrecognised
        # one is normalised to inert rather than executed.
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.PrimaryKeyConstraint('id', name='pk_capabilities'),
        # One row per key PER SCOPE -- that is what lets a tenant shadow the
        # default row with its own version of the same capability.
        sa.UniqueConstraint('tenant_id', 'organization_id', 'key',
                            name='uq_capabilities_scope_key'),
        # Same slug rule as agents.key, so a capability key and an agent key
        # are interchangeable-looking identifiers in config and in the API.
        sa.CheckConstraint("key ~ '^[a-z][a-z0-9_]{1,62}$'", name='ck_capabilities_key_slug'),
        sa.CheckConstraint("tenant_id <> '' AND organization_id <> ''",
                           name='ck_capabilities_scope'),
    )
    op.create_index('ix_capabilities_scope', 'capabilities',
                    ['tenant_id', 'organization_id', 'display_order'],
                    postgresql_where=sa.text("status <> 'disabled'"))

    # ------------------------------------------------------------------
    # 2. capability_agents -- the join table
    # ------------------------------------------------------------------
    # A join table rather than a JSONB array on `capabilities`, so membership
    # keeps a real FK to agents: a dangling agent reference would render a
    # button that fails at click time, and the FK makes that unrepresentable.
    #
    # It carries no tenant columns of its own -- it inherits the scope of the
    # capability it belongs to. A tenant that needs different membership
    # creates its own capability row at its own scope, with its own rows here.
    op.create_table(
        'capability_agents',
        sa.Column('id', postgresql.UUID(as_uuid=True),
                  server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('capability_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('agent_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('display_order', sa.Integer(), server_default='100', nullable=False),
        # Overrides agents.name INSIDE this capability only. 'Record Stories'
        # as a button may want different wording from the catalogue name.
        sa.Column('label_override', sa.String(), nullable=True),
        sa.Column('is_visible', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        # Holds `action`, e.g.
        # {"action": {"type": "start_agent", "autostart": "..."}}.
        # autostart is sent with autostart:true so the server does not title
        # the conversation from it.
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.ForeignKeyConstraint(['capability_id'], ['capabilities.id'],
                                name='fk_capability_agents_capability_id_capabilities',
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['agent_id'], ['agents.id'],
                                name='fk_capability_agents_agent_id_agents', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name='pk_capability_agents'),
        sa.UniqueConstraint('capability_id', 'agent_id', name='uq_capability_agents_membership'),
    )
    op.create_index('ix_capability_agents_capability', 'capability_agents',
                    ['capability_id', 'display_order'])

    # ------------------------------------------------------------------
    # 3. agents gains scope and presentation columns
    # ------------------------------------------------------------------
    # `uq_agents_key` stays GLOBAL and is deliberately not touched.
    # AgentRegistry caches its snapshot keyed by bare `key`, and the router,
    # orchestration and every pinned session look agents up that way. Making
    # the key per-tenant would rewrite all of them for no gain here: a tenant
    # differentiates its agents through agent_configs (below) and through
    # capability membership, not by minting a second agent with the same key.
    #
    # So these two columns record which scope OWNS the agent; they do not
    # partition the key space.
    op.add_column('agents', sa.Column('tenant_id', sa.String(),
                                      server_default=DEFAULT_SCOPE, nullable=False))
    op.add_column('agents', sa.Column('organization_id', sa.String(),
                                      server_default=DEFAULT_SCOPE, nullable=False))
    op.add_column('agents', sa.Column('icon', sa.String(), nullable=True))
    op.add_column('agents', sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()),
                                      server_default=sa.text("'{}'::jsonb"), nullable=False))

    # ------------------------------------------------------------------
    # 4. agent_configurations -> agent_configs, scoped
    # ------------------------------------------------------------------
    op.rename_table('agent_configurations', 'agent_configs')

    op.add_column('agent_configs', sa.Column('tenant_id', sa.String(),
                                             server_default=DEFAULT_SCOPE, nullable=False))
    op.add_column('agent_configs', sa.Column('organization_id', sa.String(),
                                             server_default=DEFAULT_SCOPE, nullable=False))
    op.add_column('agent_configs', sa.Column('updated_at', sa.DateTime(timezone=True),
                                             server_default=sa.text('now()'), nullable=False))

    # Version numbering is PER SCOPE: a tenant's v1 is its own, independent of
    # the default scope's version lineage. Sharing one counter would make a
    # tenant's version numbers jump whenever an unrelated tenant edited.
    op.drop_constraint('uq_agent_cfg_version', 'agent_configs', type_='unique')
    op.create_unique_constraint('uq_agent_cfg_scope_version', 'agent_configs',
                                ['agent_id', 'tenant_id', 'organization_id', 'version'])

    # Exactly one ACTIVE config per (agent, scope) -- not per agent. This is
    # the index that lets the default scope and a tenant scope each have a live
    # config for the same agent at the same time.
    #
    # It is a PARTIAL UNIQUE INDEX with no DEFERRABLE option, so it is checked
    # per statement: writers must DEACTIVATE BEFORE INSERTING. See the comment
    # in app/services/config_sync.py, which learned this the hard way.
    op.drop_index('uq_agent_cfg_one_active', table_name='agent_configs')
    op.create_index('uq_agent_cfg_one_active', 'agent_configs',
                    ['agent_id', 'tenant_id', 'organization_id'],
                    unique=True, postgresql_where=sa.text('is_active'))

    op.drop_index('ix_agent_cfg_lookup', table_name='agent_configs')
    op.create_index('ix_agent_cfg_lookup', 'agent_configs',
                    ['agent_id', 'tenant_id', 'organization_id', 'source',
                     sa.text('version DESC')])

    _seed(op.get_bind())


def _seed(bind) -> None:
    """Seed today's shipped catalogue at DEFAULT scope, so upgrading changes
    nothing visible.

    Reproduces app/config/ui/capabilities.yaml exactly -- the two capabilities,
    their order, and the two Mitra interview entry points.

    Membership is inserted only for agents that already exist. A fresh database
    runs this migration before any agent has been seeded from YAML, and
    capability_agents has a real FK, so a blind insert would abort the upgrade.
    The capability still renders; its buttons appear once the agents exist and
    the membership is re-seeded (the admin API, or re-running this statement).
    """
    from sqlalchemy import text

    bind.execute(text("""
        INSERT INTO capabilities (tenant_id, organization_id, key, name, description,
                                  icon, badge, status, display_order, metadata)
        VALUES
          ('default', 'default', 'listening_at_scale', 'Listening at Scale',
           'Synthesize field insights into actionable knowledge',
           'brain', 'SHIKSHALOKAM', 'active', 10,
           '{"action": {"type": "display_card"}}'::jsonb),
          ('default', 'default', 'sg_commons', 'SG Commons Portal',
           'AI search for ecosystem assets',
           'globe', NULL, 'coming_soon', 20,
           '{"action": {"type": "coming_soon",
                        "message": "SG Commons Portal is coming soon."}}'::jsonb)
        ON CONFLICT (tenant_id, organization_id, key) DO NOTHING
    """))

    for agent_key, label, order, autostart in (
        ('record_stories', 'Record Stories', 10, 'I want to record a story'),
        ('capture_discussion', 'Capture Discussions', 20, 'I want to capture a discussion'),
    ):
        # The CASTs are required, not defensive. A bare parameter in a SELECT
        # list has no inferable type -- Postgres answers "could not determine
        # data type of parameter $N" -- because unlike an INSERT ... VALUES
        # there is no target column to infer from.
        bind.execute(
            text("""
                INSERT INTO capability_agents (capability_id, agent_id, display_order,
                                               label_override, is_visible, metadata)
                SELECT c.id, a.id, CAST(:display_order AS integer),
                       CAST(:label AS varchar), true,
                       jsonb_build_object('action',
                           jsonb_build_object('type', 'start_agent',
                                              'autostart', CAST(:autostart AS text)))
                FROM capabilities c, agents a
                WHERE c.key = 'listening_at_scale'
                  AND c.tenant_id = 'default' AND c.organization_id = 'default'
                  AND a.key = CAST(:agent_key AS varchar)
                ON CONFLICT (capability_id, agent_id) DO NOTHING
            """),
            {"label": label, "display_order": order, "autostart": autostart,
             "agent_key": agent_key},
        )


def downgrade() -> None:
    op.drop_index('ix_agent_cfg_lookup', table_name='agent_configs')
    op.create_index('ix_agent_cfg_lookup', 'agent_configs',
                    ['agent_id', 'source', sa.text('version DESC')])

    op.drop_index('uq_agent_cfg_one_active', table_name='agent_configs')
    op.create_index('uq_agent_cfg_one_active', 'agent_configs', ['agent_id'],
                    unique=True, postgresql_where=sa.text('is_active'))

    op.drop_constraint('uq_agent_cfg_scope_version', 'agent_configs', type_='unique')
    op.create_unique_constraint('uq_agent_cfg_version', 'agent_configs',
                                ['agent_id', 'version'])

    op.drop_column('agent_configs', 'updated_at')
    op.drop_column('agent_configs', 'organization_id')
    op.drop_column('agent_configs', 'tenant_id')
    op.rename_table('agent_configs', 'agent_configurations')

    op.drop_column('agents', 'metadata')
    op.drop_column('agents', 'icon')
    op.drop_column('agents', 'organization_id')
    op.drop_column('agents', 'tenant_id')

    op.drop_index('ix_capability_agents_capability', table_name='capability_agents')
    op.drop_table('capability_agents')

    op.drop_index('ix_capabilities_scope', table_name='capabilities',
                  postgresql_where=sa.text("status <> 'disabled'"))
    op.drop_table('capabilities')

    from sqlalchemy.dialects import postgresql
    postgresql.ENUM(name='capability_status_enum').drop(op.get_bind())
    # audit_action_enum's added labels are NOT removed: PostgreSQL has no
    # DROP VALUE, and recreating the type would require rewriting audit_logs.
    # Unused labels are harmless.
