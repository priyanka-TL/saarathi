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

    # FK deferred: no Agent ORM model exists yet (agents/agent_configurations are
    # accessed via raw SQL only -- see src/services/config_sync.py, agent_registry.py).
    # Declaring ForeignKey("agents.id") here without an Agent class registered in
    # Base.metadata raises sqlalchemy.exc.NoReferencedTableError at DDL-compile time
    # (verified directly). The DB-level FK (fk_agent_sessions_agent_id_agents,
    # ON DELETE RESTRICT) already exists via migration 0003's raw ForeignKeyConstraint.
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
