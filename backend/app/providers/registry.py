"""Provider discovery, enablement and instance caching.

Responsible for: name -> class, which names this deployment allows, and one
provider instance per distinct resolved connection.
Used by: the container builds one; handlers, the finaliser and the admin route
resolve through it.

THERE IS NO PROVIDER LIST IN THIS FILE. Discovery is a pkgutil walk over
`app/providers/`, so a `@register_provider` decorator in a dropped-in package is
the whole registration -- the same mechanism `HandlerFactory` already uses for
handlers and `app/tools` uses for tools. Adding a platform therefore edits no
existing module.

INSTANCES ARE CACHED BY (name, connection.checksum), NOT BY NAME. A provider
instance owns a `requests.Session` and its fixed headers -- including the Origin
credential -- so it must not be shared across scopes that resolved different
values. Keying on the checksum makes that true by construction: different content
implies a different key, identical content shares correctly.

ENABLEMENT IS A DEPLOYMENT CONCERN, read from PROVIDERS_ENABLED before any agent
config is loadable. That is why it is env and not a config row: it decides
whether the database-reading client is built at all, and a database write must
not be able to switch a provider on.
"""
from __future__ import annotations

import importlib
import pkgutil
import threading
from typing import Any, Dict, FrozenSet, List, Optional, Tuple, Type

from app.core.logger import get_logger
from app.providers.connection import resolve_connection
from app.providers.errors import (
    ProviderConfigError,
    ProviderNotEnabled,
    UnknownProvider,
)
from app.providers.transport.pool import ChannelPool

logger = get_logger("provider_registry")

_PROVIDERS: Dict[str, Type[Any]] = {}
_DISCOVERED = False
_DISCOVERY_LOCK = threading.Lock()


def register_provider(cls: Type[Any]) -> Type[Any]:
    """Register a provider under its own `name`.

    A duplicate name raises at import, exactly as `register_handler` does: two
    providers answering to one key is not a configuration a deployment could
    debug from its symptoms.

    A provider with no `options_model` is refused HERE rather than at the first
    turn, because an untyped options block is precisely the hole this
    architecture exists to close.
    """
    name = getattr(cls, "name", "")
    if not name:
        raise RuntimeError(f"{cls.__name__} has no `name` and cannot be registered")
    if name in _PROVIDERS:
        raise RuntimeError(f"duplicate remote provider {name!r}")

    model = getattr(cls, "options_model", None)
    if model is None:
        raise RuntimeError(
            f"provider {name!r} declares no `options_model`. Every provider must "
            "type its own `remote.options` block -- an untyped one accepts a typo "
            "silently and takes a default."
        )
    if getattr(model, "model_config", {}).get("extra") != "forbid":
        raise RuntimeError(
            f"provider {name!r}'s options_model {model.__name__} must set "
            'model_config = ConfigDict(extra="forbid"). Without it a mistyped '
            "option is dropped in silence and takes its default."
        )

    _PROVIDERS[name] = cls
    return cls


def discover() -> None:
    """Import every submodule of `app.providers` to fire the decorators.

    Idempotent and thread-safe: several worker threads can reach a lazy
    resolution at once.
    """
    global _DISCOVERED
    if _DISCOVERED:
        return
    with _DISCOVERY_LOCK:
        if _DISCOVERED:
            return
        import app.providers  # noqa: WPS433 - imported here to avoid a cycle

        for _, module_name, _ in pkgutil.iter_modules(app.providers.__path__):
            # `transport` and `ws_flow` register nothing; importing them is
            # harmless and keeps the walk free of an exclusion list.
            importlib.import_module(f"app.providers.{module_name}")
        _DISCOVERED = True


def registered_names() -> FrozenSet[str]:
    discover()
    return frozenset(_PROVIDERS)


def parse_enabled(raw: Optional[str]) -> FrozenSet[str]:
    """PROVIDERS_ENABLED's comma-separated format, normalised."""
    if not raw:
        return frozenset()
    return frozenset(name.strip().lower() for name in raw.split(",") if name.strip())


def enabled_names(settings) -> FrozenSet[str]:
    """The providers this deployment allows, intersected with what registered.

    A name that is enabled but not registered is logged and ignored rather than
    fatal: a typo in PROVIDERS_ENABLED should hide the affected agents, not
    refuse the boot for every other one.
    """
    discover()
    requested = parse_enabled(getattr(settings, "providers_enabled", ""))
    unknown = requested - frozenset(_PROVIDERS)
    if unknown:
        logger.warning(
            "PROVIDERS_ENABLED names %s, which no provider package registers. "
            "Registered providers: %s.",
            ", ".join(sorted(unknown)) or "-",
            ", ".join(sorted(_PROVIDERS)) or "none",
        )
    return requested & frozenset(_PROVIDERS)


def stateful_enabled_names(settings) -> List[str]:
    """Enabled providers that pool transports in process memory.

    Read by the startup guards: those apply to ANY such provider, which is what
    the two named `*_ENABLED` flags could never express.
    """
    return sorted(
        name for name in enabled_names(settings)
        if getattr(_PROVIDERS[name], "stateful_transport", False)
    )


class ProviderRegistry:
    """Hands out provider instances bound to a resolved connection."""

    _MAX = 64

    def __init__(self, settings, profile_reader=None) -> None:
        discover()
        self._settings = settings
        self._enabled = enabled_names(settings)
        self._lock = threading.Lock()
        self._cache: Dict[Tuple[str, str], Any] = {}
        self._pools: Dict[str, ChannelPool] = {}
        # Handed to every provider it builds. Duck-typed and optional, so a
        # deployment with no identity provider configured -- and every test that
        # builds a registry with one argument -- is unchanged. See
        # BaseWsFlowProvider.__init__ for why it is a constructor argument
        # rather than a per-call one.
        self._profile_reader = profile_reader

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> FrozenSet[str]:
        return self._enabled

    def is_enabled(self, name: Optional[str]) -> bool:
        return bool(name) and name in self._enabled

    def descriptor(self, name: str) -> Type[Any]:
        """The provider CLASS, for its ClassVar declarations. No instance, so no
        connection and no credentials are needed to ask about capabilities."""
        cls = _PROVIDERS.get(name)
        if cls is None:
            raise UnknownProvider(name, _PROVIDERS)
        return cls

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def options_for(self, remote) -> Any:
        """`remote.options` as the provider's own typed model.

        Stage 2 of the two-stage validation: the domain validated the envelope
        and left this block opaque, because `app.domain` is import-pure and
        cannot reach a provider registry.
        """
        cls = self.descriptor(remote.provider)
        try:
            return cls.options_model.model_validate(remote.options or {})
        except Exception as exc:  # noqa: BLE001
            raise ProviderConfigError(
                f"remote.options is not valid for provider {remote.provider!r}: {exc}"
            ) from exc

    def get(self, remote) -> Any:
        """The provider instance for this (already scope-resolved) spec.

        :raises ProviderNotEnabled: the name is registered but switched off here.
        :raises UnknownProvider: nothing registers the name.
        :raises ProviderConfigError: the options block or credentials are wrong.
        """
        cls = self.descriptor(remote.provider)
        if remote.provider not in self._enabled:
            raise ProviderNotEnabled(remote.provider)

        options = self.options_for(remote)
        conn = resolve_connection(self._settings, remote, options)

        key = (remote.provider, conn.checksum)
        with self._lock:
            instance = self._cache.get(key)
            if instance is None:
                if len(self._cache) >= self._MAX:
                    # Bounded and cleared wholesale at the limit, matching
                    # HandlerFactory.
                    self._cache.clear()
                instance = cls(
                    conn, self._pool_for(cls), profile_reader=self._profile_reader,
                )
                self._cache[key] = instance
            return instance

    def _pool_for(self, cls: Type[Any]) -> Optional[ChannelPool]:
        """One pool per provider NAME, built lazily.

        Not shared across providers: every pool keys on conversation_id and a
        conversation can move between agents, so a shared pool would hand one
        provider a socket another provider authenticated.
        """
        if not getattr(cls, "stateful_transport", False):
            return None
        pool = self._pools.get(cls.name)
        if pool is None:
            pool = ChannelPool(
                max_open=int(getattr(self._settings, "provider_max_open_channels", 200)),
                idle_close_s=float(getattr(self._settings, "provider_idle_close_s", 1200.0)),
                name=cls.name,
            )
            self._pools[cls.name] = pool
        return pool

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close_conversation(self, conversation_id) -> None:
        """Drop this conversation's transport in EVERY provider's pool.

        Provider-agnostic on purpose. The old code closed only the Mitra pool,
        so a Saathi socket outlived the session that owned it and was cleaned up
        only when the idle reaper eventually noticed.
        """
        for pool in list(self._pools.values()):
            try:
                pool.close(conversation_id)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "failed to close a provider channel: %s", e,
                    extra={"conversation_id": str(conversation_id)},
                )

    def close_all(self) -> None:
        for pool in list(self._pools.values()):
            pool.close_all()

    def stop_reapers(self) -> None:
        """Test/shutdown helper."""
        for pool in list(self._pools.values()):
            pool.stop_reaper()
