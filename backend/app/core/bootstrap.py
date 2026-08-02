from pathlib import Path

from app.services.config_sync import ConfigSyncService
from app.core.logger import get_logger

logger = get_logger("bootstrap")

# Anchored to this file's location, not CWD. Two `.parent` hops because this
# module lives in app/core/ while the YAML lives at app/config/agents/.
AGENTS_YAML_DIR = Path(__file__).parent.parent / "config" / "agents"


def sync_and_reload(container) -> None:
    """Runs config sync, then an initial registry load.

    Deliberately has NO try/except around either step. ConfigSyncService.sync()
    raises a pydantic ValidationError (misspelled YAML key -- the schema sets
    extra="forbid") or an UnknownToolError (bad `tools:` entry) uncaught by
    design. Letting those propagate out of create_app() -> import app is
    exactly how "a misspelled YAML key aborts startup with a field path" and
    "an unknown tool reference aborts startup" are satisfied. Do not add a
    blanket except Exception here.
    """
    session = container.session_factory()
    try:
        from app.integrations.mitra.rest_client import paths_from_settings

        service = ConfigSyncService(
            tool_registry=container.tool_registry,
            mitra_enabled=bool(container.settings.mitra_enabled),
            # Validates every remote spec's finalize_path against the
            # CONFIGURED endpoints, not a hardcoded pair.
            mitra_paths=paths_from_settings(container.settings),
        )
        report = service.sync(session, AGENTS_YAML_DIR, mode=container.settings.config_sync_mode)
        logger.info("startup config_sync: %s", report.as_dict())
    finally:
        # sync() commits internally on success; on failure the exception is
        # already propagating and the process is about to die anyway. Close
        # either way to release the connection back to the pool.
        session.close()

    # Membership links capabilities (seeded by migration 0006) to agents
    # (seeded from YAML just above). It has to run HERE, between the two:
    # a migration cannot create it, because on a fresh database the agents do
    # not exist yet and capability_agents holds a real FK. Only ever fills a
    # default-scope capability that has no members at all -- see
    # app/services/capability_seed.py for why that is safe on every boot.
    session_seed = container.session_factory()
    try:
        from app.services.capability_seed import seed_default_membership

        seed_default_membership(session_seed)
    except Exception as exc:  # noqa: BLE001
        # Deliberately NOT fatal, unlike agent config. This is presentation
        # data: a failure here costs the sidebar its buttons, where a bad agent
        # config would route real traffic wrongly. Log and continue.
        logger.warning("capability membership seed failed: %s", exc)
        session_seed.rollback()
    finally:
        session_seed.close()

    session2 = container.session_factory()
    try:
        container.agent_registry.reload(session2)
    finally:
        session2.close()

    if container.agent_registry.version == 0:
        # AgentRegistry.reload() logs and swallows its own exceptions rather
        # than raising (by design, so a transient reload later in the process
        # lifetime never crashes a request). That would otherwise make a
        # broken reload at startup fail SILENTLY. This is the one place that
        # silence is promoted back into a hard failure, specifically for the
        # initial load.
        raise RuntimeError(
            "AgentRegistry loaded 0 agents at startup in config mode; "
            "check the config_sync report above and the agents/agent_configs tables."
        )
