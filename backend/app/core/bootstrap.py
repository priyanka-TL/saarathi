"""Startup: load the agent catalogue out of the database.

THERE IS NO CONFIG SYNC ANY MORE. `agents` + `agent_configs` are the only
source of agent configuration -- there is no YAML to reconcile against, no
`CONFIG_SYNC_MODE`, and no drift, because there is nothing for the database to
drift FROM. The catalogue is seeded by migration 0007 and edited through
`POST /api/agents/{key}/config`, which versions and audits every change.

What that removes, deliberately:

  * the "on a fresh database set CONFIG_SYNC_MODE=safe for the first run, then
    put it back" instruction -- `make migrate` now produces a working app;
  * the possibility of a deploy reverting a live override, which is why the
    mode defaulted to `off` in the first place;
  * a second place a tenant's configuration could come from.
"""
from app.core.logger import get_logger

logger = get_logger("bootstrap")


def sync_and_reload(container) -> None:
    """Link capability membership, then load the registry.

    The name is kept because `create_app()` and the tests call it, but there is
    no sync left -- this is a read of the database into the in-process snapshot.
    """
    # Membership links capabilities to agents. Migration 0006 seeds the
    # capabilities and 0007 the agents, and 0007 fills in the membership 0006
    # had to skip on a fresh database (capability_agents holds a real FK, so
    # 0006 could not insert rows for agents that did not exist yet). This still
    # runs, to repair a database where the two got out of step -- it only ever
    # fills a default-scope capability that has NO members at all, so a curated
    # membership is never contradicted.
    session_seed = container.session_factory()
    try:
        from app.services.capability_seed import seed_default_membership

        seed_default_membership(session_seed)
    except Exception as exc:  # noqa: BLE001
        # Deliberately NOT fatal, unlike the agent catalogue below. This is
        # presentation data: a failure here costs the sidebar its buttons,
        # where a missing agent config would route real traffic wrongly.
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
        # AgentRegistry.reload() logs and swallows its own exceptions rather
        # than raising (by design, so a transient reload later in the process
        # lifetime never crashes a request). That would otherwise make a broken
        # load at startup fail SILENTLY. This is the one place that silence is
        # promoted back into a hard failure, specifically for the initial load.
        raise RuntimeError(
            "AgentRegistry loaded 0 agents at startup. The catalogue is seeded by "
            "migration 0007 -- check that migrations are applied (`make migrate`) "
            "and that agents/agent_configs hold an active default-scope config "
            "per enabled agent."
        )
