"""Startup: load the agent catalogue out of the database.

Responsible for: seeding capability membership and populating the agent registry.
Used by: create_app(), once at import time.

THERE IS NO CONFIG SYNC. `agents` + `agent_configs` are the only source of agent
configuration -- no YAML, no CONFIG_SYNC_MODE, and so no drift. The catalogue is
seeded by migration 0007 and edited through POST /api/agents/{key}/config.
"""
from app.core.logger import get_logger

logger = get_logger("bootstrap")


def sync_and_reload(container) -> None:
    """Link capability membership, then load the registry.

    The name is kept because create_app() and the tests call it, but nothing is
    synced any more -- this is a read of the database into the in-process
    snapshot.
    """
    # Repairs a database where migrations 0006 (capabilities) and 0007 (agents,
    # membership) got out of step. Only ever fills a default-scope capability
    # with NO members at all, so a curated membership is never contradicted.
    session_seed = container.session_factory()
    try:
        from app.services.capability_seed import seed_default_membership

        seed_default_membership(session_seed)
    except Exception as exc:  # noqa: BLE001
        # Not fatal, unlike the catalogue below: this is presentation data. A
        # failure costs the sidebar its buttons; a missing agent config would
        # route real traffic wrongly.
        logger.warning("capability membership seed failed: %s", exc)
        session_seed.rollback()
    finally:
        session_seed.close()

    session = container.session_factory()
    try:
        container.agent_registry.reload(session)
    finally:
        session.close()

    if container.agent_registry.version == 0:
        # reload() logs and swallows its own exceptions so a transient failure
        # mid-lifetime never crashes a request. That would make a broken load at
        # STARTUP fail silently, so promote it back to fatal here.
        raise RuntimeError(
            "AgentRegistry loaded 0 agents at startup. The catalogue is seeded by "
            "migration 0007 -- check that migrations are applied (`make migrate`) "
            "and that agents/agent_configs hold an active default-scope config "
            "per enabled agent."
        )
