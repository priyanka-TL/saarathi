"""Handler construction and registration.

Responsible for: mapping an AgentSpec to a built handler, cached per
(key, checksum).
Used by: OrchestrationService, once per turn.

Handlers self-register via @register_handler, fired when their module is
imported -- factory walks the package to trigger that.
"""
import pkgutil
import importlib
from dataclasses import dataclass
from typing import Any, Dict, Tuple, Type

from app.domain.agent_spec import AgentSpec
from app.exceptions.domain import UnknownAgentType
from app.agents.protocol import AgentHandler
import app.agents

@dataclass
class HandlerDeps:
    llm_factory: Any
    tool_registry: Any
    settings: Any
    # A ProviderRegistry, not a client and not a per-platform slot: a provider
    # instance carries the base URL, timeouts, endpoint paths and credentials,
    # all of which resolve per agent and per tenant. The handler asks the
    # registry for the one its own resolved spec names.
    #
    # This replaced four named vendor slots. Their cost was not the four lines:
    # it was that HandlerDeps, the container, and every test that built either
    # had to change shape whenever a platform was added.
    providers: Any = None

# UnknownAgentType now lives in app/exceptions/domain.py (imported above).

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
