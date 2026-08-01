"""Shared helpers for the characterisation tests."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

FIXTURES = Path(__file__).parent / "fixtures"

# Health & Wellness, Technical Support, and Research were removed from the
# live application (their YAML configs deleted); General Support is the only
# LLM-type named agent left. Record Stories / Capture Discussion still exist
# but are agent_type=remote_flow (real Mitra network calls, no scripted-LLM
# path), so they're deliberately NOT part of this list -- tests that need a
# second, distinct, chat()-able LLM agent use the `second_llm_agent` fixture
# below instead of a second real named menu agent.
AGENT_NAMES = [
    "General Support Agent",
]

TOOLLESS_AGENTS = AGENT_NAMES
DEFAULT_AGENT = "General Support Agent"  # the router fallback (default: true)


@pytest.fixture()
def golden():
    """Load a golden fixture by stem."""

    def _load(name: str):
        return json.loads((FIXTURES / f"{name}.json").read_text())

    return _load


def chat(
    client,
    message: str,
    agent_name: str | None = None,
    conversation_id: str | None = None,
    autostart: bool = False,
):
    """POST one turn and return (status_code, body).

    `conversation_id` continues an existing conversation instead of resolving
    "most recent active"; `autostart` marks the message as the UI's own canned
    opener rather than something the user typed (it must not title the
    conversation).
    """
    payload: dict = {"message": message}
    if agent_name is not None:
        payload["agent_name"] = agent_name
    if conversation_id is not None:
        payload["conversation_id"] = conversation_id
    if autostart:
        payload["autostart"] = True
    response = client.post("/api/chat", json=payload)
    return response.status_code, response.json()


@pytest.fixture()
def second_llm_agent(flask_app):
    """A throwaway, toolless LLM agent registered directly via SQL (not a
    YAML file, never shown as a real menu agent) -- exists purely so
    multi-agent tests (breadcrumb switching, exact-key routing) still have a
    second distinct chat()-able agent now that only General Support remains
    among the real named agents. Yields (name, key).

    Disabled again in teardown, with a fresh registry reload afterward --
    without this, the row would stay 'enabled' and accumulate across test
    runs, eventually tripping test_agents_endpoint.py's exact-count golden
    assertions exactly the way an earlier, unrelated test-hygiene bug in
    test_admin_routes.py already did once this session.
    Both `key` and `name` carry a random suffix -- `agents.name` is ALSO
    unique (uq_agents_name), so a fixed display name collides across runs
    just as readily as a fixed key would.
    """
    from app.database.engine import SessionLocal

    suffix = uuid.uuid4().hex[:8]
    key = f"second_test_agent_{suffix}"
    name = f"Second Test Agent {suffix}"
    db = SessionLocal()
    agent_id = uuid.uuid4()
    try:
        db.execute(text("""
            INSERT INTO agents (id, key, name, description, agent_type, status)
            VALUES (:id, :key, :name, 'test-only second agent', 'llm', 'enabled')
        """), {"id": agent_id, "key": key, "name": name})
        config = {
            "schema_version": 1, "key": key, "name": name,
            "description": "test-only second agent", "agent_type": "llm",
            "prompt": "You are a test agent.",
            "model": {"provider": "openrouter", "name": "test-model"}, "tools": [],
        }
        db.execute(text("""
            INSERT INTO agent_configurations (agent_id, version, config, checksum, source, is_active, activated_at)
            VALUES (:agent_id, 1, :config, 'test-checksum', 'db', TRUE, now())
        """), {"agent_id": agent_id, "config": json.dumps(config)})
        db.commit()

        container = flask_app.state.container
        container.agent_registry.reload(db)

        yield name, key
    finally:
        # A hard DELETE would violate FK/check constraints the moment a test
        # actually used this agent to answer a real message (conversation_messages
        # references it, by the same "disable, never delete" design used
        # everywhere else in this app -- see config_sync's orphan handling).
        # Disabling is enough to keep it out of routable()/registry counts.
        db.execute(text("UPDATE agents SET status = 'disabled' WHERE id = :id"), {"id": agent_id})
        db.commit()
        flask_app.state.container.agent_registry.reload(db)
        db.close()
        flask_app.state.container.agent_registry.reload(db)
        db.close()
