import pkgutil
import importlib
from dataclasses import dataclass
from typing import Any, Dict, Tuple, Type

from app.domain.agent_spec import AgentSpec
from app.agents.protocol import AgentHandler
import app.agents

@dataclass
class HandlerDeps:
    llm_factory: Any
    tool_registry: Any
    mitra_rest: Any
    mitra_sessions: Any
    settings: Any

class UnknownAgentType(Exception):
    pass

_HANDLERS: Dict[str, Type[AgentHandler]] = {}

def register_handler(cls: Type[AgentHandler]) -> Type[AgentHandler]:
    if cls.agent_type in _HANDLERS:
        raise RuntimeError(f"duplicate agent_type {cls.agent_type!r}")
    _HANDLERS[cls.agent_type] = cls
    return cls

class HandlerFactory:
    def __init__(self, deps: HandlerDeps):
        self._deps = deps
        self._cache: Dict[Tuple[str, str], AgentHandler] = {}

    def build(self, spec: AgentSpec, checksum: str) -> AgentHandler:
        ck = (spec.key, checksum)
        if ck not in self._cache:
            cls = _HANDLERS.get(spec.agent_type)
            if cls is None:
                raise UnknownAgentType(spec.agent_type)
            if len(self._cache) > 64:
                self._cache.clear()
            self._cache[ck] = cls(spec, self._deps)
        return self._cache[ck]

for _, module_name, _ in pkgutil.iter_modules(app.agents.__path__):
    importlib.import_module(f"app.agents.{module_name}")
