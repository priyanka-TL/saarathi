"""MITRA_ENABLED gates remote_flow agents at config-sync time (design doc
§10.2): 'when 0, config sync marks remote_flow agents disabled and the UI
simply does not list them.' Real Postgres and the real agents yaml directory
-- this is exactly what src/bootstrap.py:sync_and_reload runs on every boot.
"""
from __future__ import annotations

from sqlalchemy import text

from app.core.bootstrap import AGENTS_YAML_DIR
from app.database.engine import SessionLocal
from app.services.agent_registry import AgentRegistry
from app.services.config_sync import ConfigSyncService


def _status_of(session, key: str) -> str:
    row = session.execute(
        text("SELECT status FROM agents WHERE key = :key"), {"key": key}
    ).fetchone()
    assert row is not None, f"agent {key!r} was not synced"
    return row[0]


def test_mitra_disabled_forces_remote_flow_disabled_and_skips_env_check(monkeypatch):
    """The concrete regression this whole change exists to prevent: without
    the mitra_enabled gate, sync() unconditionally asserts
    os.getenv(bot_route_env), which would crash startup the instant
    record_stories.yaml exists in any environment that hasn't set
    MITRA_STORY_BOT_ROUTE."""
    monkeypatch.delenv("MITRA_STORY_BOT_ROUTE", raising=False)

    session = SessionLocal()
    try:
        service = ConfigSyncService(mitra_enabled=False)
        service.sync(session, AGENTS_YAML_DIR, mode="safe")  # must not raise

        assert _status_of(session, "record_stories") == "disabled"
        assert _status_of(session, "general_support") == "enabled"
    finally:
        session.close()


def test_mitra_enabled_respects_yaml_status(monkeypatch):
    monkeypatch.setenv("MITRA_STORY_BOT_ROUTE", "/test/bot-route")

    session = SessionLocal()
    try:
        service = ConfigSyncService(mitra_enabled=True)
        service.sync(session, AGENTS_YAML_DIR, mode="safe")

        # record_stories.yaml declares status: enabled
        assert _status_of(session, "record_stories") == "enabled"
        assert _status_of(session, "general_support") == "enabled"
    finally:
        session.close()


def test_flipping_mitra_enabled_off_then_on_toggles_status(monkeypatch):
    """A revert (MITRA_ENABLED=0) must not permanently strand the agent
    disabled once the flag is flipped back on."""
    monkeypatch.setenv("MITRA_STORY_BOT_ROUTE", "/test/bot-route")
    session = SessionLocal()
    try:
        ConfigSyncService(mitra_enabled=True).sync(session, AGENTS_YAML_DIR, mode="safe")
        assert _status_of(session, "record_stories") == "enabled"

        ConfigSyncService(mitra_enabled=False).sync(session, AGENTS_YAML_DIR, mode="safe")
        assert _status_of(session, "record_stories") == "disabled"

        ConfigSyncService(mitra_enabled=True).sync(session, AGENTS_YAML_DIR, mode="safe")
        assert _status_of(session, "record_stories") == "enabled"
    finally:
        session.close()


def test_registry_hides_disabled_remote_flow_but_shows_llm_agents(monkeypatch):
    monkeypatch.delenv("MITRA_STORY_BOT_ROUTE", raising=False)
    session = SessionLocal()
    try:
        ConfigSyncService(mitra_enabled=False).sync(session, AGENTS_YAML_DIR, mode="safe")

        registry = AgentRegistry()
        registry.reload(session)

        assert registry.get_by_key_exact("record_stories") is None
        assert registry.get_by_key_exact("general_support") is not None
    finally:
        session.close()
