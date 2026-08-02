from datetime import datetime
import enum
import uuid
from typing import Optional, Dict, Any, List

from sqlalchemy import MetaData, String, Integer, DateTime, func, CheckConstraint, ForeignKey, UniqueConstraint, Index, Numeric
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB
import sqlalchemy as sa

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

class Base(DeclarativeBase):
    """
    Base class for SQLAlchemy declarative models.
    Provides standard naming conventions for constraints so Alembic autogenerate
    produces stable, diffable names.
    """
    metadata = MetaData(naming_convention=NAMING)

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
    config_sync = "config_sync"
    config_create = "config_create"
    config_activate = "config_activate"
    agent_enable = "agent_enable"
    agent_disable = "agent_disable"
    session_finalize = "session_finalize"
    session_abandon = "session_abandon"
    # Added with migration 0006, which also ALTERs the Postgres type. Both
    # halves are required: the DB type gates what can be stored, this enum
    # gates what AuditLogRepository.insert() will accept.
    capability_create = "capability_create"
    capability_update = "capability_update"
    capability_delete = "capability_delete"
    capability_agent_set = "capability_agent_set"

class ToolStatusEnum(enum.Enum):
    success = "success"
    error = "error"
    timeout = "timeout"

class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))
    tenant_code: Mapped[str] = mapped_column(String, nullable=False)
    organization_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    external_user_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    
    # We specify create_type=False because Alembic doesn't handle ENUM creation cleanly.
    status: Mapped[ConversationStatusEnum] = mapped_column(
        sa.Enum(ConversationStatusEnum, name="conversation_status_enum", create_type=False),
        nullable=False,
        server_default='active'
    )
    
    pinned_agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True) # FK deferred
    locale: Mapped[str] = mapped_column(String, nullable=False, server_default='en')
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default='0')
    metadata_: Mapped[Dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("locale IN ('en', 'hi', 'kn', 'te')", name="conv_locale"),
        CheckConstraint("message_count >= 0", name="conv_msgcount"),
        CheckConstraint("tenant_code <> ''", name="conv_tenant"),
        Index("ix_conv_user_recent", "tenant_code", "external_user_id", sa.text("last_message_at DESC NULLS LAST"), postgresql_where=sa.text("status = 'active'")),
        Index("ix_conv_org", "tenant_code", "organization_id", sa.text("last_message_at DESC NULLS LAST"), postgresql_where=sa.text("status = 'active' AND organization_id IS NOT NULL")),
        Index("ix_conv_pinned", "pinned_agent_id", postgresql_where=sa.text("pinned_agent_id IS NOT NULL")),
    )

class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    
    role: Mapped[MessageRoleEnum] = mapped_column(
        sa.Enum(MessageRoleEnum, name="message_role_enum", create_type=False),
        nullable=False
    )
    content: Mapped[str] = mapped_column(String, nullable=False)

    # attribution (FKs deferred for agent_*, only adding columns as requested)
    agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    agent_config_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    agent_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    route_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    route_confidence: Mapped[Optional[float]] = mapped_column(Numeric(4, 3), nullable=True)

    # interaction affordances
    options: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(JSONB(none_as_null=True), nullable=True)
    selected_option_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # telemetry
    model: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    request_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_msg_seq"),
        CheckConstraint("seq > 0", name="msg_seq"),
        CheckConstraint("role <> 'assistant' OR agent_id IS NOT NULL", name="msg_assistant_attribution"),
        CheckConstraint("options IS NULL OR role = 'assistant'", name="msg_options_only_assistant"),
        CheckConstraint("COALESCE(prompt_tokens, 0) >= 0 AND COALESCE(completion_tokens, 0) >= 0", name="msg_tokens"),
        CheckConstraint("COALESCE(latency_ms, 0) >= 0", name="msg_latency"),
        CheckConstraint("route_confidence IS NULL OR (route_confidence >= 0 AND route_confidence <= 1)", name="msg_confidence"),
        Index("ix_msg_conv_seq", "conversation_id", "seq"),
        Index("ix_msg_agent_time", "agent_id", sa.text("created_at DESC")),
        Index("ix_msg_request", "request_id", postgresql_where=sa.text("request_id IS NOT NULL")),
        Index("ix_msg_session", "agent_session_id", postgresql_where=sa.text("agent_session_id IS NOT NULL")),
    )

class AgentSession(Base):
    __tablename__ = "agent_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)

    # FK deferred, and STILL deferred deliberately. An Agent ORM model does now
    # exist (added with migration 0006, further down this file), so declaring
    # ForeignKey("agents.id") here would compile -- but adding it would change
    # this table's DDL and the cascade behaviour that migration 0003 already
    # established. The DB-level FK (fk_agent_sessions_agent_id_agents,
    # ON DELETE RESTRICT) exists via 0003's raw ForeignKeyConstraint and is the
    # one in force; leaving the column bare keeps the ORM from disagreeing with
    # it. (Historically this was mandatory rather than a choice: with no Agent
    # class registered in Base.metadata, ForeignKey("agents.id") raised
    # sqlalchemy.exc.NoReferencedTableError at DDL-compile time.)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    state: Mapped[SessionStateEnum] = mapped_column(
        sa.Enum(SessionStateEnum, name="session_state_enum", create_type=False),
        nullable=False,
        server_default="pending"
    )

    remote_provider: Mapped[Optional[str]] = mapped_column(String, nullable=True)
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
    error_code: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    state_data: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("remote_session_id", name="uq_sess_remote"),
        CheckConstraint("language IN ('en', 'hi', 'kn', 'te')", name="sess_lang"),
        CheckConstraint("step >= 0 AND turn_count >= 0", name="sess_step"),
        CheckConstraint("(state IN ('completed', 'failed', 'abandoned')) = (ended_at IS NOT NULL)", name="sess_terminal"),
        CheckConstraint("state IN ('pending', 'failed', 'abandoned') OR remote_session_id IS NOT NULL", name="sess_active_has_remote"),
        CheckConstraint("state <> 'completed' OR result_ref IS NOT NULL", name="sess_completed_has_result"),
        Index("uq_sess_one_open_per_conv", "conversation_id", unique=True,
              postgresql_where=sa.text("state NOT IN ('completed', 'failed', 'abandoned')")),
        Index("ix_sess_sweep", "state", "last_activity_at",
              postgresql_where=sa.text("state NOT IN ('completed', 'failed', 'abandoned')")),
        Index("ix_sess_conv", "conversation_id", sa.text("started_at DESC")),
    )

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    action: Mapped[AuditActionEnum] = mapped_column(
        sa.Enum(AuditActionEnum, name="audit_action_enum", create_type=False),
        nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor: Mapped[str] = mapped_column(String, nullable=False, server_default="system")
    request_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    before: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB(none_as_null=True), nullable=True)
    after: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB(none_as_null=True), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_audit_entity", "entity_type", "entity_id", sa.text("created_at DESC")),
        Index("ix_audit_action_time", "action", sa.text("created_at DESC")),
    )


class ToolExecution(Base):
    """One tool invocation within an LLM agent turn.

    Design rules (§4.8):
      - ON DELETE CASCADE from message_id: retention rides conversation retention,
        no separate policy needed.
      - ck_tool_error enforced at DB level: status != success implies error IS NOT NULL.
        A failure record without an explanation is not a record.
      - result_excerpt is truncated to 4096 chars BY THE REPOSITORY LAYER at write
        time, not by a DB CHECK, so the truncation is observable in application logs.
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
    # agent_id FK: ON DELETE SET NULL — attribution is optional, the record is not.
    # Declared without ForeignKey() in the ORM because the agents table has no
    # matching ORM class (see AgentSession for the same pattern and rationale).
    agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    tool_name: Mapped[str] = mapped_column(String, nullable=False)
    iteration: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, server_default="1")
    arguments: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    # result_excerpt: truncated to 4096 chars at write time (repository layer)
    result_excerpt: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    result_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    status: Mapped[ToolStatusEnum] = mapped_column(
        sa.Enum(ToolStatusEnum, name="tool_status_enum", create_type=False),
        nullable=False,
    )
    # error is REQUIRED when status != success (DB constraint ck_tool_error)
    error: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("iteration BETWEEN 1 AND 10",        name="tool_iteration"),
        CheckConstraint("duration_ms >= 0",                   name="tool_duration"),
        CheckConstraint("status = 'success' OR error IS NOT NULL", name="tool_error"),
        Index("ix_tool_message", "message_id"),
        Index("ix_tool_name_time", "tool_name", sa.text("created_at DESC")),
        Index(
            "ix_tool_failures", "tool_name", sa.text("created_at DESC"),
            postgresql_where=sa.text("status <> 'success'"),
        ),
    )



# ---------------------------------------------------------------------------
# Agent catalogue, capabilities and tenancy (migration 0006)
#
# THE DEFAULT SCOPE
# -----------------
# Every scoped row carries `tenant_id` + `organization_id`, NOT NULL, defaulting
# to the literal 'default'. A 'default' row is what every tenant sees until a
# more specific row exists, so onboarding a tenant needs no writes and shipping
# a capability to everyone is one insert. Resolution is most-specific-wins:
#
#     (tenant, org)  >  (tenant, 'default')  >  ('default', 'default')
#
# A sentinel string rather than NULL, deliberately: NULL would make every scope
# unique constraint a partial index (NULLs do not compare equal in Postgres)
# and every lookup an IS NOT DISTINCT FROM.
#
# tenant_id / organization_id are NOT foreign keys and never will be -- tenants
# and users are the user service's records, and these columns hold the values
# that arrive as JWT claims. conversations.tenant_code already works this way.
#
# WHY `Agent` AND `AgentConfig` APPEAR HERE NOW
# ---------------------------------------------
# Both tables have existed since migration 0002 but had no ORM class, because
# nothing needed one: config_sync.py and agent_registry.py reach them with raw
# SQL, and AgentSession/ToolExecution deliberately declare `agent_id` as a bare
# UUID column with the FK living only in the migration -- see the note on
# AgentSession, which explains that ForeignKey("agents.id") raises
# NoReferencedTableError at DDL-compile time when no Agent class is registered
# in Base.metadata.
#
# `capability_agents` needs a real ForeignKey to agents, so `agents` must now be
# a registered table. These classes exist to satisfy that.
#
# THEY ARE NOT A REWRITE. config_sync.py and agent_registry.py keep their raw
# SQL: those two files carry the versioning, drift and activation logic that
# Gate A and the config-versioning tests pin, and re-expressing them as ORM
# queries would risk both for no benefit.
# ---------------------------------------------------------------------------

#: The sentinel meaning "applies to every tenant / every organization".
DEFAULT_SCOPE = "default"

class AgentTypeEnum(enum.Enum):
    llm = "llm"
    remote_flow = "remote_flow"

class AgentStatusEnum(enum.Enum):
    enabled = "enabled"
    disabled = "disabled"

class ConfigSourceEnum(enum.Enum):
    yaml = "yaml"
    db = "db"

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


class Agent(Base):
    """The agent catalogue. Written by config_sync and the admin routes.

    `key` is GLOBALLY unique and stays that way. AgentRegistry caches its
    snapshot keyed by bare `key`, and the router, orchestration and every
    pinned session look agents up that way; partitioning the key space per
    tenant would rewrite all of them. tenant_id/organization_id record which
    scope OWNS an agent -- a tenant differentiates behaviour through a
    tenant-scoped AgentConfig, not by minting a second agent with the same key.
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class AgentConfig(Base):
    """A versioned agent config, SCOPED to a tenant/organization.

    Renamed from `agent_configurations` in migration 0006, which also added the
    scope columns. `source` distinguishes a YAML seed from a DB override.

    TWO INVARIANTS THE DDL ENFORCES AND WRITERS MUST RESPECT:

      * version numbering is PER SCOPE (uq_agent_cfg_scope_version), so a
        tenant's v1 is its own and does not jump when another tenant edits;
      * exactly one row per (agent, scope) may be `is_active`
        (uq_agent_cfg_one_active). That is a PARTIAL UNIQUE INDEX with no
        DEFERRABLE option, so it is checked per statement -- writers must
        DEACTIVATE BEFORE INSERTING, never the other way round. See the comment
        in app/services/config_sync.py, which learned this the hard way.

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
    source: Mapped[ConfigSourceEnum] = mapped_column(
        sa.Enum(ConfigSourceEnum, name="config_source_enum", create_type=False), nullable=False
    )
    checksum: Mapped[str] = mapped_column(String, nullable=False)
    config: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("false"))
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String, nullable=False, server_default="startup-sync")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("agent_id", "tenant_id", "organization_id", "version",
                         name="uq_agent_cfg_scope_version"),
    )


class Capability(Base):
    """One card in the sidebar's ADVANCED panel.

    Previously app/config/ui/capabilities.yaml, which a tenant could not vary.
    A tenant customises by inserting its own row at its own scope, shadowing
    the 'default' row with the same key.

    `metadata` holds `action` -- what clicking the CARD does, e.g.
    {"action": {"type": "display_card"}}. The set of action types is closed and
    lives in the frontend's ChatPage registry; an unrecognised one is
    normalised to inert rather than executed.
    """
    __tablename__ = "capabilities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, server_default=DEFAULT_SCOPE)
    organization_id: Mapped[str] = mapped_column(String, nullable=False, server_default=DEFAULT_SCOPE)
    key: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    icon: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    badge: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[CapabilityStatusEnum] = mapped_column(
        sa.Enum(CapabilityStatusEnum, name="capability_status_enum", create_type=False),
        nullable=False, server_default="active",
    )
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    metadata_: Mapped[Dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "organization_id", "key", name="uq_capabilities_scope_key"),
        CheckConstraint("key ~ '^[a-z][a-z0-9_]{1,62}$'", name="capabilities_key_slug"),
        CheckConstraint("tenant_id <> '' AND organization_id <> ''", name="capabilities_scope"),
    )


class CapabilityAgent(Base):
    """Membership: which agents sit under which capability.

    A join table rather than a JSONB array on Capability, so membership keeps a
    real FK to agents -- a dangling reference would render a button that fails
    at click time, and the FK makes that unrepresentable.

    It carries NO tenant columns: it inherits the scope of the capability it
    belongs to. A tenant needing different membership creates its own
    capability row at its own scope, with its own rows here.

    `label_override` replaces Agent.name inside THIS capability only, so one
    agent can appear under several capabilities with different wording and
    order in each.
    """
    __tablename__ = "capability_agents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))
    capability_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("capabilities.id", ondelete="CASCADE"), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    label_override: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    is_visible: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("true"))
    metadata_: Mapped[Dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("capability_id", "agent_id", name="uq_capability_agents_membership"),
    )
