import json
import os
import yaml
from pathlib import Path
from typing import Literal, Dict, List, Any
from sqlalchemy import text
from app.core.logger import get_logger
from app.domain.agent_spec import AgentSpec, canonical_json
from app.repositories.audit import AuditLogRepository

logger = get_logger("config_sync")

class SyncReport:
    def __init__(self):
        self.created: List[str] = []
        self.updated: List[str] = []
        self.unchanged: List[str] = []
        self.drifted: List[str] = []
        self.orphaned: List[str] = []
        
    def as_dict(self) -> Dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "drifted": self.drifted,
            "orphaned": self.orphaned
        }

def assert_unique(items):
    seen = set()
    for item in items:
        if item in seen:
            raise ValueError(f"Duplicate item found: {item}")
        seen.add(item)

def exactly_one(items):
    items_list = list(items)
    count = len(items_list)
    if count != 1:
        raise ValueError(f"Expected exactly 1 item, got {count}")

class ConfigSyncService:
    def __init__(self, tool_registry=None, mitra_enabled: bool = False, mitra_paths=None):
        self.tool_registry = tool_registry
        self.mitra_enabled = mitra_enabled
        # MitraPaths. Defaults to the shipped contract so the many test call
        # sites that construct this service bare keep working.
        if mitra_paths is None:
            from app.integrations.mitra.rest_client import MitraPaths
            mitra_paths = MitraPaths()
        self.mitra_paths = mitra_paths

    def sync(self, session, yaml_dir: Path, mode: Literal["safe", "force", "off"] = "safe") -> SyncReport:
        if mode == "off":
            # Suppresses reconciliation ENTIRELY -- no lock, no YAML read, no
            # DB write at all. The right setting for production with live
            # overrides: whatever's already `is_active` stays active, and
            # AgentRegistry.reload() (called right after this by bootstrap.py)
            # picks it up unchanged.
            logger.info("config_sync: mode=off, skipping reconciliation entirely")
            return SyncReport()

        audit_repo = AuditLogRepository(session)

        # Acquire advisory lock
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext('saarthi:agent_sync'))"))

        specs = []
        if yaml_dir.exists():
            for path in sorted(yaml_dir.glob("*.yaml")):
                with open(path, "r") as f:
                    raw = yaml.safe_load(f)
                
                # agent_spec.py resolve_and_store does the env expansion internally,
                # extra=forbid is defined on the model, so typos will crash here.
                from pydantic import TypeAdapter
                spec = TypeAdapter(AgentSpec).validate_python(raw)
                specs.append((path, spec))
                
        # 1-3. Global Invariants checked BEFORE DB writes
        assert_unique(s.key for _, s in specs)
        assert_unique(s.name for _, s in specs)
        
        default_agents = [s for _, s in specs if getattr(s, "default", False)]
        if default_agents:
            exactly_one(default_agents)
            
        for _, s in specs:
            if s.agent_type == "llm":
                if self.tool_registry:
                    self.tool_registry.assert_all_known(s.tools)
            elif self.mitra_enabled:
                # Only required when Mitra is actually in use -- a remote_flow
                # agent gets forced to 'disabled' below when it's not, so its
                # bot_route_env need not exist in that environment at all.
                bot_route_env = s.remote.bot_route_env
                assert os.getenv(bot_route_env), f"Missing environment variable: {bot_route_env}"

                # finalize_path is a plain string on RemoteSpec (the domain
                # layer cannot import Settings to build a Literal), so this is
                # what stops a typo -- or a YAML that disagrees with the
                # configured MITRA_FINALIZE_*_PATH -- from reaching Mitra.
                # Anything not recognised as v2 falls through to the v1 branch
                # and finalises with the wrong body shape, which Mitra accepts
                # and silently renders as a blank PDF.
                assert self.mitra_paths.is_known_finalize(s.remote.finalize_path), (
                    f"Agent '{s.key}': finalize_path {s.remote.finalize_path!r} matches neither "
                    f"MITRA_FINALIZE_V1_PATH ({self.mitra_paths.finalize_v1!r}) nor "
                    f"MITRA_FINALIZE_V2_PATH ({self.mitra_paths.finalize_v2!r})"
                )
                
        report = SyncReport()
        
        for path, spec in specs:
            canonical, checksum = canonical_json(spec)
            
            # Get agent
            agent_row = session.execute(
                text("SELECT id FROM agents WHERE key = :key"),
                {"key": spec.key}
            ).fetchone()
            
            # Case 1: Brand new
            if not agent_row:
                # remote_flow agents cannot function without Mitra configured --
                # MITRA_ENABLED=0 forces them disabled regardless of what the
                # YAML says, so the UI simply never lists them (§10.2).
                effective_status = (
                    spec.status
                    if (spec.agent_type == "llm" or self.mitra_enabled)
                    else "disabled"
                )
                res = session.execute(
                    text("""
                        INSERT INTO agents (key, name, description, agent_type, status, is_default, sort_order)
                        VALUES (:key, :name, :description, :agent_type, :status, :is_default, :sort_order)
                        RETURNING id
                    """),
                    {
                        "key": spec.key, "name": spec.name, "description": spec.description,
                        "agent_type": spec.agent_type, "status": effective_status,
                        "is_default": getattr(spec, "default", False), "sort_order": spec.sort_order
                    }
                )
                agent_id = res.scalar()
                
                session.execute(
                    text("""
                        INSERT INTO agent_configs (agent_id, version, source, checksum, config, is_active, activated_at)
                        VALUES (:agent_id, 1, 'yaml', :checksum, :config, true, now())
                    """),
                    {"agent_id": agent_id, "checksum": checksum, "config": canonical}
                )
                audit_repo.insert(
                    action="config_sync", entity_type="agent_configuration", entity_id=agent_id,
                    note=f"created from {path.name}", after=json.loads(canonical),
                )
                report.created.append(spec.key)
                continue
                
            agent_id = agent_row[0]
            
            # Identity columns follow YAML
            # status and is_default are deliberately NOT touched (§3.3) --
            # this preserves an admin's manual PATCH /api/agents/{key} disable
            # across a routine sync.
            session.execute(
                text("""
                    UPDATE agents
                    SET name = :name, description = :description, agent_type = :agent_type, sort_order = :sort_order, updated_at = now()
                    WHERE id = :agent_id
                """),
                {
                    "name": spec.name, "description": spec.description, "agent_type": spec.agent_type,
                    "sort_order": spec.sort_order, "agent_id": agent_id
                }
            )

            # Exception to the "never touch status on update" rule above:
            # a remote_flow agent's status must always track MITRA_ENABLED,
            # in BOTH directions -- flipping it to 0 must retroactively
            # disable an agent created while it was on (or it sits enabled
            # with nothing able to serve it), and flipping it back to 1 must
            # restore whatever the YAML says, or a revert would leave the
            # agent permanently disabled. This is a routine, not an admin,
            # override, so it isn't subject to the "don't touch status"
            # carve-out that protects a real PATCH /api/agents/{key} disable.
            if spec.agent_type == "remote_flow":
                effective_status = spec.status if self.mitra_enabled else "disabled"
                session.execute(
                    text("UPDATE agents SET status = :status, updated_at = now() WHERE id = :agent_id"),
                    {"status": effective_status, "agent_id": agent_id}
                )
            
            latest_yaml = session.execute(
                text("""
                    SELECT version, checksum FROM agent_configs
                    WHERE agent_id = :agent_id AND source = 'yaml'
                    ORDER BY version DESC LIMIT 1
                """),
                {"agent_id": agent_id}
            ).fetchone()

            active_cfg = session.execute(
                text("""
                    SELECT version, source, config FROM agent_configs
                    WHERE agent_id = :agent_id AND is_active = true
                """),
                {"agent_id": agent_id}
            ).fetchone()

            if latest_yaml and latest_yaml[1] == checksum:
                # The YAML itself hasn't changed since it was last synced --
                # but if a db override is still the active version, that's
                # STILL drift and must be reported on THIS boot too, not just
                # the boot the divergence was first introduced on.
                if active_cfg and active_cfg[1] == "db":
                    if mode == "force":
                        # Reclaim control even though the YAML content didn't
                        # change -- force means "the YAML is truth," full stop.
                        session.execute(
                            text("UPDATE agent_configs SET is_active = false WHERE agent_id = :agent_id AND version != :keep_v"),
                            {"agent_id": agent_id, "keep_v": latest_yaml[0]}
                        )
                        session.execute(
                            text("UPDATE agent_configs SET is_active = true, activated_at = now() WHERE agent_id = :agent_id AND version = :version"),
                            {"agent_id": agent_id, "version": latest_yaml[0]}
                        )
                        audit_repo.insert(
                            action="config_activate", entity_type="agent_configuration", entity_id=agent_id,
                            before=active_cfg[2], after=json.loads(canonical),
                            note=f"force mode: discarded db override v{active_cfg[0]}; reactivated yaml v{latest_yaml[0]}",
                        )
                        report.updated.append(spec.key)
                    else:
                        audit_repo.insert(
                            action="config_sync", entity_type="agent_configuration", entity_id=agent_id,
                            after=active_cfg[2],
                            note=f"drift: db override v{active_cfg[0]} retained (yaml v{latest_yaml[0]} unchanged)",
                        )
                        report.drifted.append(spec.key)
                else:
                    report.unchanged.append(spec.key)
                continue

            # Case 2: YAML changed
            adopt = (mode == "force") or (not active_cfg) or (active_cfg[1] == "yaml")
            
            new_v_row = session.execute(
                text("SELECT COALESCE(MAX(version), 0) + 1 FROM agent_configs WHERE agent_id = :agent_id"),
                {"agent_id": agent_id}
            ).scalar()
            
            if adopt:
                # DEACTIVATE BEFORE INSERTING, not after. uq_agent_cfg_one_active
                # is a PARTIAL UNIQUE INDEX (migration 0002) -- `unique(agent_id)
                # WHERE is_active` -- and an index is checked per statement, with
                # no DEFERRABLE option. Inserting the new active row first meant
                # two active rows existed for the duration of one statement, so
                # the INSERT itself raised UniqueViolation and the old
                # deactivate-afterwards UPDATE never ran. Effect: the FIRST edit
                # to any already-synced agent YAML crashed startup
                # (sync_and_reload has no try/except, by design), which is why
                # this only ever showed up when a shipped agent config changed.
                session.execute(
                    text("UPDATE agent_configs SET is_active = false WHERE agent_id = :agent_id AND is_active = true"),
                    {"agent_id": agent_id}
                )

            session.execute(
                text("""
                    INSERT INTO agent_configs (agent_id, version, source, checksum, config, is_active, activated_at)
                    VALUES (:agent_id, :version, 'yaml', :checksum, :config, :is_active, CASE WHEN :is_active THEN now() ELSE NULL END)
                """),
                {"agent_id": agent_id, "version": new_v_row, "checksum": checksum, "config": canonical, "is_active": adopt}
            )

            if adopt:
                audit_repo.insert(
                    action="config_activate", entity_type="agent_configuration", entity_id=agent_id,
                    before=active_cfg[2] if active_cfg else None, after=json.loads(canonical),
                )
                report.updated.append(spec.key)
            else:
                report.drifted.append(spec.key)
                audit_repo.insert(
                    action="config_sync", entity_type="agent_configuration", entity_id=agent_id,
                    after=json.loads(canonical),
                    note=f"drift: yaml v{new_v_row} stored INACTIVE; db override v{active_cfg[0]} retained",
                )

        # Case 3: orphans (agents sourced from YAML originally but no longer in the yaml dir)
        yaml_keys = {s.key for _, s in specs}
        
        # We find agents whose ACTIVE configuration is YAML-sourced, but who are not in the current yaml_keys.
        orphans = session.execute(
            text("""
                SELECT a.id, a.key FROM agents a
                WHERE a.id IN (
                    SELECT agent_id FROM agent_configs WHERE source = 'yaml' AND is_active = true
                )
            """)
        ).fetchall()
        
        for orphan_id, orphan_key in orphans:
            if orphan_key not in yaml_keys:
                # Disabling the agent. Never delete.
                session.execute(
                    text("UPDATE agents SET status = 'disabled', updated_at = now() WHERE id = :agent_id"),
                    {"agent_id": orphan_id}
                )
                audit_repo.insert(
                    action="agent_disable", entity_type="agent", entity_id=orphan_id,
                    note="yaml file removed",
                )
                report.orphaned.append(orphan_key)

        session.commit()
        logger.info(f"agent config sync: {report.as_dict()}")
        if report.drifted:
            logger.warning(
                "CONFIG DRIFT at startup: %s agent(s) have a live DB override diverging from YAML: %s",
                len(report.drifted), report.drifted,
            )
        return report
