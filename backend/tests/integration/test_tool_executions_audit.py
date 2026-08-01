"""Integration tests for tool_executions and audit_logs (§4.8 / §4.9).

Acceptance criteria (design doc §4.8, §4.9):
  1. Deleting a conversation cascades to tool_executions.
  2. An unsuccessful tool record without error is rejected (ck_tool_error).
  3. Audit records survive deletion of their entity (no FKs on audit_logs).

All three tests run against a real PostgreSQL database (not SQLite) because
they exercise native PG features: ON DELETE CASCADE, CHECK constraints, and
the deliberate absence of foreign keys on audit_logs.

The fixture pattern matches test_repositories.py: commit-based isolation,
no rollback, UUIDs make rows unique across runs.
"""
from __future__ import annotations

import uuid
import pytest
from sqlalchemy.exc import IntegrityError

from app.database.engine import SessionLocal
from app.models.orm import (
    AuditLog, AuditActionEnum,
    ToolExecution, ToolStatusEnum,
    ConversationMessage,
)
from app.domain.core import UserContext
from app.repositories.audit import AuditLogRepository
from app.repositories.conversations import ConversationRepository
from app.repositories.messages import MessageRepository
from app.repositories.tool_executions import ToolExecutionRepository
from app.agents.protocol import ToolTrace


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    """Plain session; tests commit their own data and clean up after."""
    session = SessionLocal()
    yield session
    session.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(suffix: str = "") -> UserContext:
    import uuid
    run_id = uuid.uuid4().hex[:6]
    return UserContext(
        tenant_code=f"TOOL_TEST_{run_id}_{suffix}",
        user_id=f"u_{run_id}_{suffix}",
        email=f"u_{run_id}_{suffix}@example.com",
        display_name=f"User {suffix}",
    )


def _make_conv(db, suffix: str = ""):
    repo = ConversationRepository(db)
    conv = repo.get_or_create(None, _make_user(suffix))
    db.commit()
    return conv


def _make_message(db, conv_id: uuid.UUID) -> ConversationMessage:
    """Insert an assistant message and return the flushed ORM row."""
    import uuid as _uuid
    from sqlalchemy import text

    agent_id = _uuid.uuid4()
    # agents has no ORM model; insert a dummy row via raw SQL so the FK is satisfied
    db.execute(
        text("INSERT INTO agents (id, key, name, description, agent_type, status) VALUES (:id, :key, :name, 'Test Desc', 'llm', 'enabled')"),
        {"id": agent_id, "key": f"test_agent_{agent_id.hex}", "name": f"Test Agent {agent_id.hex}"}
    )

    msg = ConversationMessage(
        id=_uuid.uuid4(),
        conversation_id=conv_id,
        seq=1,
        role="assistant",
        content="test response",
        agent_id=agent_id,
    )
    db.add(msg)
    db.flush()
    db.commit()
    db.refresh(msg)
    return msg


def _insert_tool_row(db, message_id: uuid.UUID, *, status: str = "success", error=None) -> ToolExecution:
    """Insert a single tool_executions row directly (bypasses repo guards for error tests)."""
    row = ToolExecution(
        message_id=message_id,
        tool_name="web_search",
        iteration=1,
        arguments={},
        result_excerpt="some result",
        result_bytes=11,
        status=ToolStatusEnum(status),
        error=error,
        duration_ms=120,
    )
    db.add(row)
    db.flush()
    return row


# ---------------------------------------------------------------------------
# Acceptance criterion 1: CASCADE from conversation → messages → tool_executions
# ---------------------------------------------------------------------------

class TestCascadeDeletion:
    """Deleting a conversation must cascade to its tool_executions."""

    def test_deleting_conversation_removes_tool_executions(self, db):
        conv = _make_conv(db, "_casc")
        msg = _make_message(db, conv.id)
        tool_row = _insert_tool_row(db, msg.id)
        tool_id = tool_row.id
        db.commit()

        # Verify the row exists before deletion
        assert db.get(ToolExecution, tool_id) is not None

        # Delete the conversation.  CASCADE path:
        #   conversations → conversation_messages (ON DELETE CASCADE)
        #                 → tool_executions       (ON DELETE CASCADE)
        # Fetch via raw query to get the ORM object
        from app.models.orm import Conversation as ConvModel
        conv_orm = db.get(ConvModel, conv.id)
        db.delete(conv_orm)
        db.commit()

        # Clear identity map so we actually hit the DB, since CASCADE happens in PG
        db.expunge_all()

        # The tool_execution row must no longer exist
        assert db.get(ToolExecution, tool_id) is None, (
            "ON DELETE CASCADE on message_id should have removed tool_executions "
            "when the parent conversation was deleted."
        )

    def test_deleting_message_removes_tool_executions(self, db):
        """The immediate FK is message_id → CASCADE; test that link directly."""
        conv = _make_conv(db, "_msgcasc")
        msg = _make_message(db, conv.id)
        tool_row = _insert_tool_row(db, msg.id)
        tool_id = tool_row.id
        db.commit()

        from app.models.orm import ConversationMessage as MsgModel
        msg_orm = db.get(MsgModel, msg.id)
        db.delete(msg_orm)
        db.commit()

        # Clear identity map so we actually hit the DB
        db.expunge_all()

        assert db.get(ToolExecution, tool_id) is None, (
            "ON DELETE CASCADE on message_id should remove the tool_execution "
            "when its parent message is deleted."
        )


# ---------------------------------------------------------------------------
# Acceptance criterion 2: ck_tool_error rejects non-success rows without error
# ---------------------------------------------------------------------------

class TestToolErrorConstraint:
    """ck_tool_error: status != 'success' implies error IS NOT NULL."""

    @pytest.mark.parametrize("bad_status", ["error", "timeout"])
    def test_non_success_without_error_is_rejected(self, db, bad_status):
        """DB must reject a failure row with error=NULL."""
        conv = _make_conv(db, f"_ckerr_{bad_status}")
        msg = _make_message(db, conv.id)

        with pytest.raises(IntegrityError, match="ck_tool"):
            _insert_tool_row(db, msg.id, status=bad_status, error=None)
            db.commit()

        db.rollback()

    def test_non_success_with_error_is_accepted(self, db):
        """A failure row WITH an error message must be stored without error."""
        conv = _make_conv(db, "_ckerr_ok")
        msg = _make_message(db, conv.id)

        row = _insert_tool_row(
            db, msg.id, status="error",
            error="DDGS rate-limited: 429 Too Many Requests",
        )
        db.commit()
        db.refresh(row)

        assert row.status == ToolStatusEnum.error
        assert row.error is not None

    def test_success_without_error_is_accepted(self, db):
        """A success row with error=NULL is the happy path."""
        conv = _make_conv(db, "_ckerr_succ")
        msg = _make_message(db, conv.id)

        row = _insert_tool_row(db, msg.id, status="success", error=None)
        db.commit()
        db.refresh(row)

        assert row.status == ToolStatusEnum.success
        assert row.error is None

    def test_repository_rejects_non_success_without_error_before_db(self, db):
        """The Python guard in ToolExecutionRepository must raise before hitting PG."""
        conv = _make_conv(db, "_pyguard")
        msg = _make_message(db, conv.id)

        repo = ToolExecutionRepository(db)
        bad_trace = ToolTrace(
            tool_name="web_search",
            iteration=1,
            arguments={},
            result_excerpt="",
            status="error",
            error=None,   # <-- the problem
            duration_ms=50,
        )

        with pytest.raises(ValueError, match="ck_tool_error"):
            repo.bulk_insert(msg.id, None, [bad_trace])

        db.rollback()


# ---------------------------------------------------------------------------
# Acceptance criterion 3: audit records survive entity deletion
# ---------------------------------------------------------------------------

class TestAuditLogSurvival:
    """audit_logs has NO foreign keys (§4.9). Records must outlive their entity."""

    def test_audit_row_survives_conversation_deletion(self, db):
        """Even after the referenced conversation is deleted, the audit row stays."""
        conv = _make_conv(db, "_audit_surv")

        # Create an audit row that references the conversation's id as entity_id
        audit_repo = AuditLogRepository(db)
        audit_dto = audit_repo.insert(
            action="session_finalize",
            entity_type="agent_session",
            entity_id=conv.id,   # reuse conv.id as a stand-in entity UUID
            actor="test",
            note="survival test",
        )
        db.commit()
        audit_id = audit_dto.id

        # Delete the conversation; if audit_logs had a FK this would cascade
        from app.models.orm import Conversation as ConvModel
        conv_orm = db.get(ConvModel, conv.id)
        db.delete(conv_orm)
        db.commit()

        # Audit row must still exist — the design doc says audit records must
        # survive deletion of the entity they describe
        surviving = db.get(AuditLog, audit_id)
        assert surviving is not None, (
            "audit_logs must have NO FK on entity_id. A record that disappears "
            "when its entity is deleted is not an audit log."
        )
        assert surviving.entity_id == conv.id

    def test_multiple_audit_actions_for_same_entity(self, db):
        """Multiple audit records for the same entity all survive."""
        conv = _make_conv(db, "_audit_multi")
        audit_repo = AuditLogRepository(db)

        ids = []
        for action in ["session_finalize", "session_abandon"]:
            dto = audit_repo.insert(
                action=action,
                entity_type="agent_session",
                entity_id=conv.id,
                actor="test",
            )
            db.commit()
            ids.append(dto.id)

        # Delete the entity
        from app.models.orm import Conversation as ConvModel
        db.delete(db.get(ConvModel, conv.id))
        db.commit()

        # All audit rows survive
        for audit_id in ids:
            assert db.get(AuditLog, audit_id) is not None

    def test_audit_log_has_no_fk_to_agents(self, db):
        """An audit row referencing a non-existent agent UUID is valid (no FK)."""
        ghost_id = uuid.uuid4()  # does not exist in agents table

        audit_repo = AuditLogRepository(db)
        dto = audit_repo.insert(
            action="agent_disable",
            entity_type="agent",
            entity_id=ghost_id,
            actor="config_sync",
            note="agent was removed from YAML",
        )
        db.commit()

        surviving = db.get(AuditLog, dto.id)
        assert surviving is not None
        assert surviving.entity_id == ghost_id


# ---------------------------------------------------------------------------
# Result excerpt truncation
# ---------------------------------------------------------------------------

class TestResultExcerptTruncation:
    """result_excerpt must be truncated to 4096 chars at write time."""

    def test_large_result_is_truncated(self, db):
        conv = _make_conv(db, "_trunc")
        msg = _make_message(db, conv.id)

        big_result = "x" * 10_000
        repo = ToolExecutionRepository(db)
        trace = ToolTrace(
            tool_name="web_search",
            iteration=1,
            arguments={},
            result_excerpt=big_result,
            status="success",
            error=None,
            duration_ms=200,
        )
        repo.bulk_insert(msg.id, None, [trace])
        db.commit()

        rows = db.query(ToolExecution).filter_by(message_id=msg.id).all()
        assert len(rows) == 1
        assert len(rows[0].result_excerpt) == 4096, (
            "result_excerpt must be truncated to exactly 4096 chars at write time "
            "so one large result cannot distort the table."
        )

    def test_small_result_is_stored_verbatim(self, db):
        conv = _make_conv(db, "_notrunc")
        msg = _make_message(db, conv.id)

        short_result = "hello world"
        repo = ToolExecutionRepository(db)
        trace = ToolTrace(
            tool_name="web_search",
            iteration=1,
            arguments={},
            result_excerpt=short_result,
            status="success",
            error=None,
            duration_ms=30,
        )
        repo.bulk_insert(msg.id, None, [trace])
        db.commit()

        rows = db.query(ToolExecution).filter_by(message_id=msg.id).all()
        assert rows[0].result_excerpt == short_result
