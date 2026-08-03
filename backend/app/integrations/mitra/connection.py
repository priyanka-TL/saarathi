"""MitraConnection -- one resolved Mitra endpoint configuration, and the
registry of REST clients built from it.

WHY THIS EXISTS
===============
Every Mitra setting used to be read straight off ``Settings``, which made it
process-global: one Saarthi process could talk to exactly one Mitra company,
with one set of bot routes, on one Mitra instance. That contradicts the rest of
the app, where capabilities and agent configs are scoped
``(tenant_id, organization_id)`` and resolved most-specific-wins.

``MitraConnection`` is the seam. It is a plain value object -- no I/O, no
Settings import -- carrying everything the REST client and the WebSocket
channel need. ``resolve_connection`` builds one by layering an agent spec's
optional overrides on top of the ``Settings`` values:

    Settings (env)  <-  spec.remote.connection  (per agent, per tenant/org)

**Env is the floor, not the loser.** Every override field is Optional and an
unset field means "use the env value", so a deployment with no database
overrides at all resolves to exactly what it resolved to before this module
existed. That is also the failure mode: if the scoped config cannot be read,
the caller falls back to the default-scope spec and this function falls back to
env, field by field.

THE ORIGIN URL IS A CREDENTIAL, AND IT IS HANDLED SPECIALLY
==========================================================
Mitra gates admission on the ``Origin`` header (see MitraRestClient's module
docstring). It must never reach the database, a YAML file, a log line or an
error response. So:

  * it is NEVER read from the spec by value. A spec may carry
    ``origin_env: "MITRA_TENANT_X_ORIGIN"`` -- a variable NAME, resolved here
    with ``os.getenv`` -- which is the same indirection the codebase already
    used for ``bot_route_env`` / ``company_env`` before those moved into the
    config as literal values;
  * ``repr=False`` keeps it out of every log line that reprs a connection;
  * it enters the checksum only as its OWN sha256, never as plaintext.

That last point is not decoration. The checksum is the cache key for both the
REST client and the pooled WebSocket channel, so omitting the origin entirely
would let two scopes with different origins share a client carrying the wrong
credential. Hashing it keeps the cache correct without ever storing the value.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Optional

from app.integrations.mitra.rest_client import (
    MitraPaths,
    MitraRestClient,
    paths_from_settings,
)

#: Spec field -> MitraConnection field, for the scalar overrides. The two names
#: differ because the spec drops the `mitra_` prefix Settings carries, and
#: keeping the mapping explicit means a renamed spec field fails loudly here
#: rather than silently resolving to the env value.
_SCALAR_OVERRIDES = (
    "base_url",
    "ws_url",
    "user_agent",
    "connect_timeout_s",
    "read_timeout_s",
    "ws_connect_timeout_s",
    "ip_city",
    "ip_state",
    "ip_zip",
)

_PATH_OVERRIDES = (
    "profile",
    "generate_session",
    "chat",
    "get_story",
    "finalize_v1",
    "finalize_v2",
)


@dataclass(frozen=True)
class MitraConnection:
    """Everything needed to reach one Mitra deployment as one company.

    Frozen and hashable-by-checksum: instances are cache keys, so they must not
    change under a cache that already keyed off them.
    """

    base_url: str
    ws_url: str
    user_agent: str
    allowed_hosts: tuple[str, ...] = ()
    paths: MitraPaths = MitraPaths()
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 30.0
    ws_connect_timeout_s: float = 10.0
    ip_city: str = ""
    ip_state: str = ""
    ip_zip: str = ""
    # CREDENTIAL. repr=False so no log line that reprs a connection can leak it.
    origin_url: str = field(default="", repr=False)
    # Derived, never passed in -- see __post_init__.
    checksum: str = field(init=False, default="", compare=False)

    def __post_init__(self) -> None:
        payload: Dict[str, Any] = {
            "base_url": self.base_url,
            "ws_url": self.ws_url,
            "user_agent": self.user_agent,
            "allowed_hosts": sorted(self.allowed_hosts),
            "paths": {
                name: getattr(self.paths, name) for name in _PATH_OVERRIDES
            },
            "connect_timeout_s": self.connect_timeout_s,
            "read_timeout_s": self.read_timeout_s,
            "ws_connect_timeout_s": self.ws_connect_timeout_s,
            "ip_city": self.ip_city,
            "ip_state": self.ip_state,
            "ip_zip": self.ip_zip,
            # The credential's HASH, not the credential. Two scopes with
            # different origins must not share a cached client.
            "origin_sha": hashlib.sha256(self.origin_url.encode("utf-8")).hexdigest(),
        }
        digest = hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        object.__setattr__(self, "checksum", digest)


def _split_hosts(raw: Optional[str]) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


def _as_hosts(value: Any) -> tuple[str, ...]:
    """Accept either a comma-separated string or an already-split sequence.

    The spec stores a list (JSONB round-trips it naturally); Settings stores the
    comma-separated string the env format requires.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        return _split_hosts(value)
    return tuple(str(h).strip().lower() for h in value if str(h).strip())


def from_settings(settings) -> MitraConnection:
    """The env floor: a connection built purely from ``Settings``.

    ``settings`` is duck-typed rather than imported -- app.integrations must not
    depend on app.core for a type it only reads attributes off, and the tests
    pass a small stand-in.
    """
    return MitraConnection(
        base_url=settings.mitra_base_url,
        ws_url=settings.mitra_ws_url,
        user_agent=settings.mitra_user_agent,
        allowed_hosts=_split_hosts(settings.mitra_allowed_hosts),
        # Reuses the existing mapper rather than restating the six fields; it
        # stays a separate function because the paths are also needed
        # independently of any connection, to validate a spec's finalize_path
        # on the config write path.
        paths=paths_from_settings(settings),
        connect_timeout_s=settings.mitra_connect_timeout_s,
        read_timeout_s=settings.mitra_read_timeout_s,
        ws_connect_timeout_s=settings.mitra_ws_connect_timeout_s,
        ip_city=settings.mitra_ip_city,
        ip_state=settings.mitra_ip_state,
        ip_zip=settings.mitra_ip_zip,
        origin_url=settings.mitra_origin_url,
    )


def resolve_connection(settings, remote_spec=None) -> MitraConnection:
    """The connection this agent, at this scope, should use.

    Starts from the env floor and applies only the fields the spec actually
    sets. Returns the floor unchanged when the spec carries no ``connection``
    block, which is the overwhelmingly common case.
    """
    base = from_settings(settings)
    if remote_spec is None:
        return base

    origin = _resolve_origin(settings, remote_spec)
    override = getattr(remote_spec, "connection", None)
    if override is None:
        return base if origin == base.origin_url else replace(base, origin_url=origin)

    changes: Dict[str, Any] = {}
    for name in _SCALAR_OVERRIDES:
        value = getattr(override, name, None)
        if value is not None:
            changes[name] = value

    hosts = _as_hosts(getattr(override, "allowed_hosts", None))
    if hosts:
        changes["allowed_hosts"] = _apply_host_ceiling(settings, hosts)

    path_override = getattr(override, "paths", None)
    if path_override is not None:
        path_changes = {
            name: getattr(path_override, name)
            for name in _PATH_OVERRIDES
            if getattr(path_override, name, None) is not None
        }
        if path_changes:
            changes["paths"] = replace(base.paths, **path_changes)

    if origin != base.origin_url:
        changes["origin_url"] = origin

    return replace(base, **changes) if changes else base


def _resolve_origin(settings, remote_spec) -> str:
    """The Origin credential for this spec.

    Resolved from the NAME the spec carries, never from a value it carries. An
    ``origin_env`` naming a variable that is not set falls back to the global
    rather than sending an empty Origin, which Mitra would reject with a 403
    that looks like an outage rather than a misconfiguration.
    """
    env_name = getattr(remote_spec, "origin_env", None)
    if env_name:
        value = os.getenv(env_name)
        if value:
            return value
    return settings.mitra_origin_url


def _apply_host_ceiling(settings, hosts: tuple[str, ...]) -> tuple[str, ...]:
    """Intersect a spec-supplied allowlist with the deployment's ceiling.

    ``allowed_hosts`` is an SSRF control (``MitraRestClient._validate_url``), and
    once it is writable through the admin API an admin can widen it. The ceiling
    is the operator's backstop: hosts outside it are dropped, not rejected, so a
    single stale entry cannot break every report fetch.

    An UNSET ceiling means no ceiling -- that is deliberate, so this is
    behaviour-preserving for every deployment that does not opt in.
    """
    ceiling = _split_hosts(settings.mitra_host_ceiling)
    if not ceiling:
        return hosts
    return tuple(h for h in hosts if h in ceiling)


class MitraClientRegistry:
    """One ``MitraRestClient`` per distinct connection, cached by checksum.

    Replaces the single process-wide client. A client owns a ``requests.Session``
    (a connection pool) and its fixed headers -- including the Origin credential
    -- so it must not be shared across scopes that resolved different values.
    Keying on ``conn.checksum`` makes that true by construction: different
    content implies a different key, identical content shares correctly.

    Bounded and cleared wholesale at the limit, matching ``HandlerFactory``.
    """

    _MAX = 64

    def __init__(self) -> None:
        # Every endpoint is a plain `def`, so requests land on worker threads
        # and two can miss the cache concurrently. The lock keeps that from
        # opening two connection pools for one scope.
        self._lock = threading.Lock()
        self._cache: Dict[str, MitraRestClient] = {}

    def get(self, conn: MitraConnection) -> MitraRestClient:
        with self._lock:
            client = self._cache.get(conn.checksum)
            if client is None:
                if len(self._cache) >= self._MAX:
                    self._cache.clear()
                client = MitraRestClient(
                    base_url=conn.base_url,
                    origin_url=conn.origin_url,
                    user_agent=conn.user_agent,
                    allowed_hosts=list(conn.allowed_hosts),
                    connect_timeout=conn.connect_timeout_s,
                    read_timeout=conn.read_timeout_s,
                    paths=conn.paths,
                )
                self._cache[conn.checksum] = client
            return client
