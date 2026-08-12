"""The SQLAlchemy schema -- all nine tables.

Responsible for: the ORM mapping. The MIGRATIONS are the source of truth for
what is actually in the database; this mirrors them.
Used by: repositories, and Alembic's autogenerate comparison.

CHECK CONSTRAINTS ARE DECLARED WITH A BARE SUFFIX (`name="locale"`), here and in
the migrations. The naming convention interpolates `%(constraint_name)s`, so a
qualified name gets double-prefixed -- that is what produced
`ck_conversations_ck_conversations_conv_locale`. uq/fk/pk/ix names are written
out in full and used verbatim.

SOME `agent_id` COLUMNS HAVE NO ForeignKey() here on purpose: their migrations
set per-table `ondelete` policies, and letting the ORM emit its own would mean
two places to keep in step. The migration is the one in force.
"""
from datetime import datetime
import enum
import uuid
from typing import Optional, Dict, Any, List

from sqlalchemy import MetaData, String, Integer, DateTime, func, CheckConstraint, ForeignKey, UniqueConstraint, Index, Numeric
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID, JSONB
import sqlalchemy as sa

#: The sentinel meaning "applies to every tenant / every organization".
#: Declared once in app/domain/scope.py; re-exported from this module because
#: the column defaults below read it and `from app.models.orm import
#: DEFAULT_SCOPE` is an established import path.
from app.domain.scope import DEFAULT_SCOPE  # noqa: F401  (re-exported)

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: The `created_by` / `updated_by` value for a write with no user behind it.
SYSTEM_ACTOR = "system"


class Base(DeclarativeBase):
    """Declarative base carrying the constraint naming convention.

    Alembic's `env.py` sets `target_metadata = Base.metadata`, which is how the
    convention reaches `op.create_table()` in the migrations too -- see the
    module docstring on why CHECK constraints are named differently from
    everything else because of it.
    """
    metadata = MetaData(naming_convention=NAMING)


class AuditMixin:
    """The four audit columns, identical on every table.

    Declared once here and inherited, so "every table has the same audit block"
    is a fact about the code rather than a convention nine classes have to keep
    agreeing on. SQLAlchemy copies these onto each mapped subclass.

    `updated_at` carries `onupdate` so an ORM-mediated UPDATE refreshes it.
    Several hot paths write through SQLAlchemy Core (`update()` statements in
    the repositories) where `onupdate` also applies, but raw `text()` SQL --
    which the admin routes and the registry use -- does not get it for free and
    must set both `updated_at` and `updated_by` itself.
    """
    created_by: Mapped[str] = mapped_column(String, nullable=False, server_default=SYSTEM_ACTOR)
    updated_by: Mapped[str] = mapped_column(String, nullable=False, server_default=SYSTEM_ACTOR)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# Enums. Each is created by the migration that creates the table using it, with
# create_type=False on the column so the type is not created twice.
# ---------------------------------------------------------------------------

class ConversationStatusEnum(enum.Enum):
    active = "active"
    archived = "archived"


class MessageRoleEnum(enum.Enum):
    user = "user"
    assistant = "assistant"
    system = "system"
    tool = "tool"


class SessionStateEnum(enum.Enum):
    pending = "pending"
    authenticating = "authenticating"
    in_progress = "in_progress"
    awaiting_user = "awaiting_user"
    finalizing = "finalizing"
    completed = "completed"
    failed = "failed"
    abandoned = "abandoned"


class AuditActionEnum(enum.Enum):
    """What an audit row records.

    There is no `config_sync` member. It belonged to the YAML-era startup
    reconciliation, which no longer exists -- nothing could write it, and a
    label nothing can write is an invitation to reintroduce the thing it named.
    """
    config_create = "config_create"
    config_activate = "config_activate"
    agent_enable = "agent_enable"
    agent_disable = "agent_disable"
    session_finalize = "session_finalize"
    session_abandon = "session_abandon"
    capability_create = "capability_create"
    capability_update = "capability_update"
    capability_delete = "capability_delete"
    capability_agent_set = "capability_agent_set"


class ToolStatusEnum(enum.Enum):
    success = "success"
    error = "error"
    timeout = "timeout"


class AgentTypeEnum(enum.Enum):
    llm = "llm"
    remote_flow = "remote_flow"


class AgentStatusEnum(enum.Enum):
    enabled = "enabled"
    disabled = "disabled"


class CapabilityStatusEnum(enum.Enum):
    """A superset of AgentStatusEnum, and deliberately a SEPARATE type.

    `coming_soon` is a capability-only state: a card advertised to users before
    the thing behind it exists (SG Commons Portal). An agent has no such state
    -- no handler knows how to serve one -- so folding these into one enum
    would put an unservable value within reach of the routing code.
    """
    active = "active"
    disabled = "disabled"
    coming_soon = "coming_soon"


# ---------------------------------------------------------------------------
# The agent catalogue
# ---------------------------------------------------------------------------

class Agent(AuditMixin, Base):
    """The agent catalogue. Written by migration 0010 and the admin routes.

    `key` is GLOBALLY unique and stays that way. AgentRegistry caches its
    snapshot keyed by bare `key`, and the router, orchestration and every open
    session look agents up that way; partitioning the key space per tenant would
    rewrite all of them. tenant_id/organization_id record which scope OWNS an
    agent -- a tenant differentiates behaviour through a tenant-scoped
    AgentConfig, not by minting a second agent with the same key.
    """
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, server_default=DEFAULT_SCOPE)
    organization_id: Mapped[str] = mapped_column(String, nullable=False, server_default=DEFAULT_SCOPE)
    key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    description: Mapped[str] = mapped_column(String, nullable=False)
    agent_type: Mapped[AgentTypeEnum] = mapped_column(
        sa.Enum(AgentTypeEnum, name="agent_type_enum", create_type=False), nullable=False
    )
    status: Mapped[AgentStatusEnum] = mapped_column(
        sa.Enum(AgentStatusEnum, name="agent_status_enum", create_type=False),
        nullable=False, server_default="enabled",
    )
    is_default: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("false"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    # Presentation only -- a key into the frontend's icon registry, never a
    # path or URL (the icons are inline SVG so they can inherit currentColor).
    icon: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    metadata_: Mapped[Dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))

    __table_args__ = (
        CheckConstraint("key ~ '^[a-z][a-z0-9_]{1,62}$'", name="key_slug"),
        CheckConstraint("tenant_id <> '' AND organization_id <> ''", name="scope"),
        # At most one default agent globally -- RouterService's Gate 5 fallback
        # returns the first is_default row it finds, and two would make that
        # non-deterministic.
        Index("uq_agents_single_default", "is_default", unique=True,
              postgresql_where=sa.text("is_default")),
        Index("ix_agents_enabled", "sort_order", "key",
              postgresql_where=sa.text("status = 'enabled'")),
        # AgentRegistry.maybe_reload polls MAX(updated_at) on this table.
        Index("ix_agents_updated", sa.text("updated_at DESC")),
    )


class AgentConfig(AuditMixin, Base):
    """A versioned agent config, SCOPED to a tenant/organization.

    There is no provenance column: this table is the ONLY source of agent
    configuration, so every row has the same origin and a `source` marker would
    only imply that some second store still existed.

    TWO INVARIANTS THE DDL ENFORCES AND WRITERS MUST RESPECT:

      * version numbering is PER SCOPE (uq_agent_configs_scope_version), so a
        tenant's v1 is its own and does not jump when another tenant edits;
      * exactly one row per (agent, scope) may be `is_active`
        (uq_agent_configs_one_active). That is a PARTIAL UNIQUE INDEX with no
        DEFERRABLE option, so it is checked per statement -- writers must
        DEACTIVATE BEFORE INSERTING, never the other way round.

    Each row carries its OWN checksum, computed from its own content. That is
    what keeps HandlerFactory's `(spec.key, checksum)` cache from serving one
    tenant's handler -- and its system prompt -- to another.
    """
    __tablename__ = "agent_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, server_default=DEFAULT_SCOPE)
    organization_id: Mapped[str] = mapped_column(String, nullable=False, server_default=DEFAULT_SCOPE)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String, nullable=False)
    config: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("false"))
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("agent_id", "tenant_id", "organization_id", "version",
                         name="uq_agent_configs_scope_version"),
        CheckConstraint("version > 0", name="version"),
        CheckConstraint("is_active = FALSE OR activated_at IS NOT NULL", name="activated"),
        # A config that cannot name its own agent is not a config. Cheap
        # structural guard against an INSERT of the wrong JSON entirely.
        CheckConstraint("config ? 'agent_type' AND config ? 'key' AND config ? 'name'",
                        name="shape"),
        CheckConstraint("tenant_id <> '' AND organization_id <> ''", name="scope"),
        Index("uq_agent_configs_one_active", "agent_id", "tenant_id", "organization_id",
              unique=True, postgresql_where=sa.text("is_active")),
        Index("ix_agent_configs_lookup", "agent_id", "tenant_id", "organization_id",
              sa.text("version DESC")),
    )


# ---------------------------------------------------------------------------
# Capabilities -- the sidebar catalogue
# ---------------------------------------------------------------------------

class Capability(AuditMixin, Base):
    """One card in the sidebar's ADVANCED panel.

    A tenant customises by inserting its own row at its own scope, shadowing the
    'default' row with the same key.

    `metadata` holds `action` -- what clicking the CARD does, e.g.
    {"action": {"type": "display_card"}}. The set of action types is closed and
    lives in the frontend's ChatPage registry; an unrecognised one is normalised
    to inert rather than executed.
    """
    __tablename__ = "capabilities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, server_default=DEFAULT_SCOPE)
    organization_id: Mapped[str] = mapped_column(String, nullable=False, server_default=DEFAULT_SCOPE)
    key: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # A REGISTRY KEY ('brain', 'globe'), never a path or a URL.
    icon: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    badge: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[CapabilityStatusEnum] = mapped_column(
        sa.Enum(CapabilityStatusEnum, name="capability_status_enum", create_type=False),
        nullable=False, server_default="active",
    )
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    metadata_: Mapped[Dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))

    __table_args__ = (
        UniqueConstraint("tenant_id", "organization_id", "key", name="uq_capabilities_scope_key"),
        # Same slug rule as agents.key, so a capability key and an agent key are
        # interchangeable-looking identifiers in config and in the API.
        CheckConstraint("key ~ '^[a-z][a-z0-9_]{1,62}$'", name="key_slug"),
        CheckConstraint("tenant_id <> '' AND organization_id <> ''", name="scope"),
        Index("ix_capabilities_scope", "tenant_id", "organization_id", "display_order",
              postgresql_where=sa.text("status <> 'disabled'")),
    )


class CapabilityAgent(AuditMixin, Base):
    """Membership: which agents sit under which capability.

    A join table rather than a JSONB array on Capability, so membership keeps a
    real FK to agents -- a dangling reference would render a button that fails
    at click time, and the FK makes that unrepresentable.

    It carries NO tenant columns: it inherits the scope of the capability it
    belongs to. A tenant needing different membership creates its own capability
    row at its own scope, with its own rows here.

    `label_override` replaces Agent.name inside THIS capability only, so one
    agent can appear under several capabilities with different wording and order
    in each.
    """
    __tablename__ = "capability_agents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))
    capability_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("capabilities.id", ondelete="CASCADE"), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    label_override: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    is_visible: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("true"))
    # Holds `action`, e.g. {"action": {"type": "start_agent", "autostart": "..."}}.
    metadata_: Mapped[Dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))

    __table_args__ = (
        UniqueConstraint("capability_id", "agent_id", name="uq_capability_agents_membership"),
        Index("ix_capability_agents_capability", "capability_id", "display_order"),
    )


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

class Conversation(AuditMixin, Base):
    """One chat thread.

    IT OWNS NOTHING ABOUT AGENTS, AND THAT IS THE POINT. There is no
    `pinned_agent_id`: a conversation is a container that many agents pass
    through, so the three agent questions are answered elsewhere --

      which agent is driving right now?  the `agent_sessions` row for this
                                         conversation whose state is not
                                         terminal (at most one, enforced by
                                         uq_agent_sessions_one_open_per_conversation)
      which agent said this?             `conversation_messages.agent_id`
      which agents took part?            every `agent_sessions` row for this
                                         conversation, and the distinct
                                         `agent_id` sequence over its messages

    `pinned_agent_id` used to duplicate the first of those. It was written under
    exactly the condition that opened a session (`routing.pin_session`) and
    cleared exactly when that session was abandoned, so it could only ever agree
    with `agent_sessions` or be a bug -- and nothing at the DB level enforced
    which. Deriving it removes the second possibility.

    `user_id` holds the identifier the user service issued, as a string, and is
    scoped by `tenant_code` on every read: the pair is the ownership check.
    """
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))
    tenant_code: Mapped[str] = mapped_column(String, nullable=False)
    organization_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[ConversationStatusEnum] = mapped_column(
        sa.Enum(ConversationStatusEnum, name="conversation_status_enum", create_type=False),
        nullable=False,
        server_default="active",
    )
    locale: Mapped[str] = mapped_column(String, nullable=False, server_default="en")
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    metadata_: Mapped[Dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("locale IN ('en', 'hi', 'kn', 'te')", name="locale"),
        CheckConstraint("message_count >= 0", name="message_count"),
        CheckConstraint("tenant_code <> '' AND user_id <> ''", name="owner"),
        Index("ix_conversations_user_recent", "tenant_code", "user_id",
              sa.text("last_message_at DESC NULLS LAST"),
              postgresql_where=sa.text("status = 'active'")),
        Index("ix_conversations_org", "tenant_code", "organization_id",
              sa.text("last_message_at DESC NULLS LAST"),
              postgresql_where=sa.text("status = 'active' AND organization_id IS NOT NULL")),
    )


class AgentSession(AuditMixin, Base):
    """One agent's episode within a conversation.

    THE MULTI-AGENT UNIT. A conversation accumulates as many of these as it has
    agent hand-offs, and `uq_agent_sessions_one_open_per_conversation` allows at
    most one of them to be non-terminal at a time. That single partial unique
    index is what makes "which agent is driving this conversation" a question
    with exactly one answer, without the conversation row knowing anything.

    Only agents that declare `routing.pin_session` get a session at all --
    stateless LLM agents answer a turn and leave no episode behind.

    There is no `remote_provider` column: the provider is a property of the
    agent's spec (`remote.provider`), not of one session, and the column was
    never written. There is no `error_code` either -- `error` is the record.
    """
    __tablename__ = "agent_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    # FK to agents.id lives in migration 0006 with ON DELETE RESTRICT -- see the
    # module docstring. An agent with a live session must not be deletable.
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    state: Mapped[SessionStateEnum] = mapped_column(
        sa.Enum(SessionStateEnum, name="session_state_enum", create_type=False),
        nullable=False,
        server_default="pending",
    )

    remote_session_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    remote_profile_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    remote_flow: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    remote_bot_route: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    language: Mapped[str] = mapped_column(String, nullable=False, server_default="en")
    step: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    result_ref: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    report_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    state_data: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    # No `started_at`: it was always exactly `created_at`. The serializer still
    # emits a "started_at" key from this column, so the API contract is unchanged.
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("remote_session_id", name="uq_agent_sessions_remote_session"),
        CheckConstraint("language IN ('en', 'hi', 'kn', 'te')", name="language"),
        CheckConstraint("step >= 0 AND turn_count >= 0", name="counters"),
        CheckConstraint("(state IN ('completed', 'failed', 'abandoned')) = (ended_at IS NOT NULL)",
                        name="terminal"),
        CheckConstraint("state IN ('pending', 'failed', 'abandoned') OR remote_session_id IS NOT NULL",
                        name="active_has_remote"),
        CheckConstraint("state <> 'completed' OR result_ref IS NOT NULL",
                        name="completed_has_result"),
        # THE multi-agent invariant: one open episode per conversation, any
        # number of closed ones.
        Index("uq_agent_sessions_one_open_per_conversation", "conversation_id", unique=True,
              postgresql_where=sa.text("state NOT IN ('completed', 'failed', 'abandoned')")),
        Index("ix_agent_sessions_sweep", "state", "last_activity_at",
              postgresql_where=sa.text("state NOT IN ('completed', 'failed', 'abandoned')")),
        Index("ix_agent_sessions_conversation", "conversation_id", sa.text("created_at DESC")),
    )


class ConversationMessage(AuditMixin, Base):
    """One message. Append-only.

    `agent_id` is the per-message attribution that makes a multi-agent
    conversation legible: `ck_conversation_messages_assistant_attribution`
    makes it mandatory on every assistant row, so no reply is ever anonymous and
    `MessageRepository.distinct_agent_sequence` can reconstruct the hand-off
    order from the messages alone.

    There is no `agent_config_id`. It was declared with the rest of the
    attribution block but never had a writer -- the value passed was
    `getattr(agent, "config_id", None)` against a dataclass with no such field,
    so every row held NULL. Config provenance is available through
    `agent_configs.checksum`, which the registry already carries per agent.
    """
    __tablename__ = "conversation_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    role: Mapped[MessageRoleEnum] = mapped_column(
        sa.Enum(MessageRoleEnum, name="message_role_enum", create_type=False),
        nullable=False
    )
    content: Mapped[str] = mapped_column(String, nullable=False)

    # Attribution. FKs (agents SET NULL, agent_sessions SET NULL) live in
    # migration 0007 -- see the module docstring.
    agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    agent_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    route_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    route_confidence: Mapped[Optional[float]] = mapped_column(Numeric(4, 3), nullable=True)

    # interaction affordances
    options: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(JSONB(none_as_null=True), nullable=True)
    selected_option_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Downloadable documents this reply produced. NOT options: an option is
    # click-to-reply, so a URL there would be posted back as user input. One
    # entry per file, so a document offered as PDF and DOCX is two entries.
    attachments: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(JSONB(none_as_null=True), nullable=True)

    # telemetry
    model: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    request_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_conversation_messages_seq"),
        CheckConstraint("seq > 0", name="seq"),
        CheckConstraint("role <> 'assistant' OR agent_id IS NOT NULL", name="assistant_attribution"),
        CheckConstraint("options IS NULL OR role = 'assistant'", name="options_only_assistant"),
        CheckConstraint("attachments IS NULL OR role = 'assistant'", name="attachments_only_assistant"),
        CheckConstraint("COALESCE(prompt_tokens, 0) >= 0 AND COALESCE(completion_tokens, 0) >= 0",
                        name="tokens"),
        CheckConstraint("COALESCE(latency_ms, 0) >= 0", name="latency"),
        CheckConstraint("route_confidence IS NULL OR (route_confidence >= 0 AND route_confidence <= 1)",
                        name="confidence"),
        Index("ix_conversation_messages_conversation_seq", "conversation_id", "seq"),
        Index("ix_conversation_messages_agent_time", "agent_id", sa.text("created_at DESC")),
        Index("ix_conversation_messages_request", "request_id",
              postgresql_where=sa.text("request_id IS NOT NULL")),
        Index("ix_conversation_messages_session", "agent_session_id",
              postgresql_where=sa.text("agent_session_id IS NOT NULL")),
    )


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

class ToolExecution(AuditMixin, Base):
    """One tool invocation within an LLM agent turn.

      - ON DELETE CASCADE from message_id: retention rides conversation
        retention, no separate policy needed.
      - ck_tool_executions_error is enforced at DB level: status != success
        implies error IS NOT NULL. A failure record without an explanation is
        not a record.
      - result_excerpt is truncated to 4096 chars BY THE REPOSITORY LAYER at
        write time, not by a DB CHECK, so the truncation is observable in
        application logs.
    """
    __tablename__ = "tool_executions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversation_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    # agent_id FK: ON DELETE SET NULL, declared in migration 0008 -- attribution
    # is optional, the record is not.
    agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    tool_name: Mapped[str] = mapped_column(String, nullable=False)
    iteration: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, server_default="1")
    arguments: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    result_excerpt: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    result_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    status: Mapped[ToolStatusEnum] = mapped_column(
        sa.Enum(ToolStatusEnum, name="tool_status_enum", create_type=False),
        nullable=False,
    )
    # error is REQUIRED when status != success (ck_tool_executions_error)
    error: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint("iteration BETWEEN 1 AND 10", name="iteration"),
        CheckConstraint("duration_ms >= 0", name="duration"),
        CheckConstraint("status = 'success' OR error IS NOT NULL", name="error"),
        Index("ix_tool_executions_message", "message_id"),
        Index("ix_tool_executions_name_time", "tool_name", sa.text("created_at DESC")),
        Index("ix_tool_executions_failures", "tool_name", sa.text("created_at DESC"),
              postgresql_where=sa.text("status <> 'success'")),
    )


class AuditLog(AuditMixin, Base):
    """Append-only record of configuration and session lifecycle changes.

    NO FOREIGN KEYS, deliberately: an audit row must outlive its subject. A
    deleted agent's config_activate rows are exactly the rows an investigation
    needs, and a FK would cascade or block.

    The actor is `created_by`, like everywhere else -- this table used to spell
    the same concept `actor`, which meant the one table whose entire purpose is
    provenance was the one that named it differently.
    """
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    action: Mapped[AuditActionEnum] = mapped_column(
        sa.Enum(AuditActionEnum, name="audit_action_enum", create_type=False),
        nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    request_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    before: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB(none_as_null=True), nullable=True)
    after: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB(none_as_null=True), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("ix_audit_logs_entity", "entity_type", "entity_id", sa.text("created_at DESC")),
        Index("ix_audit_logs_action_time", "action", sa.text("created_at DESC")),
    )
