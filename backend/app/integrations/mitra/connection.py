"""Resolving which Mitra deployment an agent talks to.

Responsible for: turning a RemoteSpec into a MitraConnection, and caching one
REST client per distinct connection.
Used by: the container, orchestration and the handlers.

Endpoint, timeouts and the Origin credential are all per agent and per tenant,
so a shared client would serve every scope the first scope's endpoint.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.integrations.mitra.rest_client import (
    MitraPaths,
    MitraRestClient,
)

#: Copied straight across from `MitraConnectionSpec` to `MitraConnection`. Kept
#: explicit so a renamed spec field fails loudly here rather than silently
#: resolving to a default.
_SCALARS = (
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

_PATH_FIELDS = (
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
                name: getattr(self.paths, name) for name in _PATH_FIELDS
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
    """Split MITRA_HOST_CEILING's comma-separated env format."""
    if not raw:
        return ()
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


def _as_hosts(value: Any) -> tuple[str, ...]:
    """Accept either an already-split sequence or a comma-separated string.

    The spec stores a list (JSONB round-trips it naturally); the string form is
    still accepted so a hand-written config or test double is not a surprise.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        return _split_hosts(value)
    return tuple(str(h).strip().lower() for h in value if str(h).strip())


def resolve_connection(settings, remote_spec) -> MitraConnection:
    """The connection this agent, at this scope, should use.

    Built entirely from ``remote_spec.connection`` -- see the module docstring
    for why there is no environment floor behind it. ``settings`` supplies only
    the Origin credential and the host ceiling, neither of which may live in a
    config row.

    ``settings`` is duck-typed rather than imported -- app.integrations must not
    depend on app.core for a type it only reads attributes off, and the tests
    pass a small stand-in.
    """
    conn = getattr(remote_spec, "connection", None)
    if conn is None:
        # Unreachable through a validated spec (`RemoteSpec.connection` is
        # required), so this is a stored row that predates migration 0009 or a
        # hand-built test double -- either way, guessing an endpoint would send
        # an interview somewhere nobody configured.
        raise ValueError(
            "remote.connection is missing from this agent config; it is required "
            "since the MITRA_* connection settings moved into agent configuration "
            "(migration 0009)"
        )

    values: Dict[str, Any] = {name: getattr(conn, name) for name in _SCALARS}
    values["paths"] = MitraPaths(
        **{name: getattr(conn.paths, name) for name in _PATH_FIELDS}
    )
    values["allowed_hosts"] = _apply_host_ceiling(
        settings, _as_hosts(getattr(conn, "allowed_hosts", None))
    )
    values["origin_url"] = _resolve_origin(settings, remote_spec)

    return MitraConnection(**values)


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
