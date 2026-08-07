"""The in-process agent catalogue.

Responsible for: holding the enabled agents and their active configs, reloading
them on a TTL, and resolving a tenant's own config on top.
Used by: routing, the agents router and the admin reload endpoint.

The snapshot holds the DEFAULT scope. `resolve_for_scope` layers a tenant's own
config over it -- every path that reads an agent for a caller must go through
it, or one tenant is served another's configuration.
"""
from dataclasses import dataclass
from typing import Optional, List, Dict
import time
from sqlalchemy import text

from pydantic import TypeAdapter

from app.core.logger import get_logger
from app.domain.agent_spec import AgentSpec
from app.domain.scope import DEFAULT_SCOPE, scope_for_user
from app.repositories.scope_sql import (
    scope_candidate_filter,
    scope_precedence_order_by,
)

_agent_spec_adapter = TypeAdapter(AgentSpec)

logger = get_logger("agent_registry")

@dataclass
class RegisteredAgent:
    id: str
    key: str
    name: str
    description: str
    agent_type: str
    is_default: bool
    checksum: str
    spec: AgentSpec

class AgentRegistry:
    def __init__(self, ttl_s: float = 60.0, mitra_enabled: bool = True):
        self._ttl_s = ttl_s
        # MITRA_ENABLED=0 hides every remote_flow agent, or the sidebar offers
        # interviews nothing can serve. A runtime filter, not a DB write: a
        # deployment switch has no business mutating tenant configuration.
        self._mitra_enabled = mitra_enabled
        self._snapshot: Dict[str, RegisteredAgent] = {}
        self._legacy_names: Dict[str, str] = {}
        self._version: int = 0
        self._loaded_at: float = 0.0
        self._max_updated_at = None
        # The registry version is part of the key, so a reload invalidates
        # every scoped entry for free.
        self._scope_cache: Dict[tuple, "RegisteredAgent"] = {}

    def reload(self, session) -> int:
        try:
            # THE SCOPE FILTER IS LOAD-BEARING. An agent may have several
            # active configs, one per (tenant, org); unfiltered, the dict
            # assignment below keeps whichever row arrived last -- letting one
            # tenant's configuration become the snapshot everyone is served
            # from. resolve_for_scope() layers tenant configs on per request.
            query = text("""
                SELECT a.id, a.key, a.name, a.description, a.agent_type, a.is_default, a.updated_at, c.config, c.checksum
                FROM agents a
                JOIN agent_configs c ON a.id = c.agent_id
                WHERE a.status = 'enabled' AND c.is_active = TRUE
                  AND c.tenant_id = 'default' AND c.organization_id = 'default'
            """)
            result = session.execute(query).fetchall()

            new_snapshot = {}
            new_legacy = {}
            max_ts = None

            skipped = []
            for row in result:
                if row.agent_type == "remote_flow" and not self._mitra_enabled:
                    continue
                # PER-ROW: one bad config costs one agent, not all of them.
                # Aborting the loop left the snapshot empty and refused the
                # boot -- one editable row able to take down every agent.
                try:
                    spec = _agent_spec_adapter.validate_python(row.config)
                except Exception as exc:  # noqa: BLE001
                    skipped.append(row.key)
                    logger.error(
                        "AgentRegistry: skipping agent %r -- its active config does "
                        "not validate: %s", row.key, exc,
                    )
                    continue
                agent = RegisteredAgent(
                    id=str(row.id),
                    key=row.key,
                    name=row.name,
                    description=row.description,
                    agent_type=row.agent_type,
                    is_default=row.is_default,
                    checksum=row.checksum,
                    spec=spec
                )
                new_snapshot[agent.key] = agent
                new_legacy[agent.name] = agent.key
                
                if max_ts is None or row.updated_at > max_ts:
                    max_ts = row.updated_at
                    
            self._snapshot = new_snapshot
            self._legacy_names = new_legacy
            self._version += 1
            self._loaded_at = time.monotonic()
            self._max_updated_at = max_ts
            logger.info(
                "AgentRegistry reloaded",
                extra={
                    "registry_version": self._version,
                    "agent_count": len(self._snapshot),
                    "skipped_count": len(skipped),
                },
            )
            if skipped:
                logger.warning(
                    "AgentRegistry: %s agent(s) EXCLUDED for an unparseable config: %s. "
                    "They are invisible to routing and the sidebar until fixed -- "
                    "POST /api/agents/{key}/config, or activate an earlier version.",
                    len(skipped), skipped,
                )
            
        except Exception as e:
            # Failure to reload must LOG and keep the cached snapshot
            logger.error(
                "Failed to reload AgentRegistry: %s. Keeping cached snapshot.", e,
                exc_info=True,
                extra={"registry_version": self._version},
            )
            
        return self._version

    def maybe_reload(self, session):
        """Called from before_request. Cheap: one indexed MAX(), TTL-gated."""
        if time.monotonic() - self._loaded_at < self._ttl_s:
            return
            
        try:
            query = text("SELECT MAX(updated_at) FROM agents WHERE status = 'enabled'")
            db_max = session.execute(query).scalar()
            
            if db_max != self._max_updated_at:
                self.reload(session)
            else:
                self._loaded_at = time.monotonic()
        except Exception as e:
            logger.error(
                "Failed to check for AgentRegistry updates: %s", e,
                extra={"registry_version": self._version},
            )

    @property
    def version(self) -> int:
        return self._version

    def routable(self) -> List[RegisteredAgent]:
        return [a for a in self._snapshot.values() if getattr(a.spec.routing, 'router_selectable', True)]

    def routable_for_user(self, session, user) -> List[RegisteredAgent]:
        """Every routable agent this caller may actually select, scope-resolved.

        Two steps that must stay together:

        1. RESOLVE each agent for the caller's tenant/organization. The snapshot
           holds the default scope, so a tenant that customised an agent would
           otherwise be shown -- and access-checked against -- someone else's
           configuration.
        2. FILTER by the resolved spec's own AccessSpec, using the SAME
           `AccessSpec.matches()` RouterService._visible calls.

        Step 2 calling the same matcher as routing is the point. Listing an
        agent the router would then refuse means the sidebar advertises
        something that silently falls through to the default agent when clicked;
        a second, separate access check here would be free to drift into exactly
        that state.

        Sorted by `sort_order`, which is the order the sidebar renders.
        """
        resolved = (
            self.resolve_for_scope(session, agent, *scope_for_user(user))
            for agent in self.routable()
        )
        return sorted(
            (a for a in resolved if a.spec.access.matches(user)),
            key=lambda a: a.spec.sort_order,
        )


    _KEY_STRIP_CHARS = "'\" .,!?;:"

    def get_by_key_exact(self, key: Optional[str]) -> Optional[RegisteredAgent]:
        """Normalises (strip, lowercase, strip surrounding quotes/trailing
        punctuation) then does an exact dict lookup. NEVER a substring scan --
        this is the fix for the orchestrator.py:62-65 substring-collision bug,
        applied to LLM-returned agent_key values that may carry stray quoting
        or punctuation (design doc §6.3)."""
        if not key:
            return None
        normalized = key.strip().lower().strip(self._KEY_STRIP_CHARS)
        return self._snapshot.get(normalized)

    def get_by_id(self, agent_id: str) -> Optional[RegisteredAgent]:
        return next((a for a in self._snapshot.values() if a.id == agent_id), None)


    def get(self, key_or_legacy_name: str) -> Optional[RegisteredAgent]:
        if key_or_legacy_name in self._snapshot:
            return self._snapshot[key_or_legacy_name]
        
        # fallback to name mapping
        key = self._legacy_names.get(key_or_legacy_name)
        if key:
            return self._snapshot.get(key)
        return None
        
    def default(self) -> Optional[RegisteredAgent]:
        for agent in self._snapshot.values():
            if agent.is_default:
                return agent
        return None

    # ------------------------------------------------------------------
    # Tenant-scoped resolution
    # ------------------------------------------------------------------
    #
    # THE CROSS-TENANT HAZARD THIS EXISTS TO AVOID
    # --------------------------------------------
    # HandlerFactory caches built handlers by `(spec.key, checksum)`. If two
    # tenants' specs differed while sharing a checksum, the first tenant's
    # handler -- including its system prompt -- would be served to the second.
    #
    # That cannot happen here, by construction rather than by care: a
    # tenant-scoped config is a WHOLE row in agent_configs with its OWN
    # checksum, computed from its own content by the writer. Two tenants with
    # byte-identical configs DO share a checksum and therefore a handler, which
    # is correct -- the same spec deserves the same handler.
    #
    # This is why per-tenant config is a scoped row rather than a JSON patch
    # merged over a base: a patch would have to be re-canonicalised and
    # re-checksummed at exactly the right moment, and forgetting once would
    # leak a prompt.

    _SCOPE_CACHE_MAX = 512

    def resolve_for_scope(self, session, agent: "RegisteredAgent",
                          tenant_id: str, organization_id: str) -> "RegisteredAgent":
        """`agent` as this tenant/organization should see it.

        Returns the argument unchanged when the scope has no config of its own,
        which is the overwhelmingly common case -- so the default path costs
        one indexed lookup and nothing else.

        Resolution is most-specific-wins, matching capability_service:
            (tenant, org) > (tenant, 'default') > ('default', 'default')
        """
        if not tenant_id or tenant_id == DEFAULT_SCOPE:
            return agent

        cache_key = (self._version, tenant_id, organization_id, agent.key)
        cached = self._scope_cache.get(cache_key)
        if cached is not None:
            return cached

        resolved = agent
        try:
            row = session.execute(
                text(f"""
                    SELECT c.config, c.checksum
                    FROM agent_configs c
                    WHERE c.agent_id = :agent_id
                      AND c.is_active
                      AND {scope_candidate_filter("c")}
                      AND NOT (c.tenant_id = :default_scope
                               AND c.organization_id = :default_scope)
                    ORDER BY {scope_precedence_order_by("c")}
                    LIMIT 1
                """),
                {
                    "agent_id": agent.id,
                    "tenant_id": tenant_id,
                    "organization_id": organization_id or DEFAULT_SCOPE,
                    "default_scope": DEFAULT_SCOPE,
                },
            ).fetchone()

            if row is not None:
                # Re-validated, not trusted: a scoped config is written through
                # the admin API and could predate a schema change. A spec that
                # will not parse falls back to the default rather than failing
                # the turn -- same posture as reload(), which logs and keeps
                # its cached snapshot.
                spec = _agent_spec_adapter.validate_python(row.config)
                resolved = RegisteredAgent(
                    id=agent.id,
                    key=agent.key,
                    name=agent.name,
                    description=agent.description,
                    agent_type=agent.agent_type,
                    is_default=agent.is_default,
                    # The scoped row's OWN checksum. This is what separates
                    # this tenant's entry in HandlerFactory's cache.
                    checksum=row.checksum,
                    spec=spec,
                )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "Failed to resolve scoped config for agent=%s tenant=%s: %s. Using default.",
                agent.key, tenant_id, e,
            )
            return agent

        if len(self._scope_cache) >= self._SCOPE_CACHE_MAX:
            self._scope_cache.clear()
        self._scope_cache[cache_key] = resolved
        return resolved
