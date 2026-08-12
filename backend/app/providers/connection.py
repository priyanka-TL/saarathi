"""Resolving which deployment an agent talks to, at this scope.

Responsible for: turning a RemoteSpec plus the environment into a frozen,
checksummed RemoteConnection.
Used by: ProviderRegistry, which caches one provider instance per checksum.

ENDPOINT AND CREDENTIALS ARE PER AGENT AND PER TENANT, so a shared connection
would serve every scope the first scope's endpoint. The checksum is what keeps
them apart: it is the cache key, so different content implies a different
instance and identical content shares correctly.

CREDENTIALS ARE RESOLVED FROM NAMES THE CONFIG ROW CARRIES, NEVER FROM VALUES.
A config row is readable through the admin API, so `remote.auth` names an
environment variable (`credential_env`, the Origin header every platform in
this family gates on) and this module reads it with os.getenv. Its VALUE
enters the checksum only as a sha256, and the field holding it is `repr=False`.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from app.providers.errors import ProviderConfigError


@dataclass(frozen=True)
class RemoteConnection:
    """Everything needed to reach one deployment as one configured agent.

    Frozen and hashable-by-checksum: instances are cache keys, so they must not
    change under a cache that already keyed off them.
    """

    provider: str
    base_url: str
    stream_url: str
    user_agent: str
    allowed_hosts: Tuple[str, ...] = ()
    headers: Tuple[Tuple[str, str], ...] = ()
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 30.0
    stream_connect_timeout_s: float = 10.0
    #: Non-secret parts of `remote.auth`: scheme, credential_env's NAME.
    auth: Tuple[Tuple[str, Any], ...] = ()
    #: The provider's own `remote.options`, already validated by its model.
    options: Any = None

    # THE CREDENTIAL. repr=False so no log line that reprs a connection can
    # leak it, and compare=False is deliberately NOT set -- two scopes with
    # different credentials must not share a cached client.
    origin_url: str = field(default="", repr=False)

    # Derived, never passed in -- see __post_init__.
    checksum: str = field(init=False, default="", compare=False)

    def __post_init__(self) -> None:
        payload: Dict[str, Any] = {
            "provider": self.provider,
            "base_url": self.base_url,
            "stream_url": self.stream_url,
            "user_agent": self.user_agent,
            "allowed_hosts": sorted(self.allowed_hosts),
            "headers": sorted(self.headers),
            "connect_timeout_s": self.connect_timeout_s,
            "read_timeout_s": self.read_timeout_s,
            "stream_connect_timeout_s": self.stream_connect_timeout_s,
            "auth": sorted((k, str(v)) for k, v in self.auth),
            "options": _stable(self.options),
            # The credential's HASH, not the credential. Two scopes with
            # different ones must not share a cached client, and a checksum is
            # not a secret store.
            "origin_sha": _sha(self.origin_url),
        }
        digest = hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        object.__setattr__(self, "checksum", digest)

    def header_list(self) -> list[str]:
        """The extra headers in the `"Name: value"` form websocket-client wants."""
        return [f"{name}: {value}" for name, value in self.headers]


def _sha(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _stable(value: Any) -> Any:
    """A JSON-comparable projection of a provider's options model."""
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def split_hosts(raw: Optional[str]) -> Tuple[str, ...]:
    """Split the comma-separated env format used by PROVIDER_HOST_CEILING."""
    if not raw:
        return ()
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


def as_hosts(value: Any) -> Tuple[str, ...]:
    """Accept either an already-split sequence or a comma-separated string.

    The spec stores a list (JSONB round-trips it naturally); the string form is
    still accepted so a hand-written config or test double is not a surprise.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        return split_hosts(value)
    return tuple(str(h).strip().lower() for h in value if str(h).strip())


def resolve_connection(settings, remote_spec, options: Any = None) -> RemoteConnection:
    """The connection this agent, at this scope, should use.

    Built entirely from the spec, except for two things a config row must not
    hold: the credential VALUES (read by name from the environment) and the
    operator's host ceiling.

    ``settings`` is duck-typed rather than imported -- app.providers must not
    depend on app.core for a type it only reads attributes off, and the tests
    pass a small stand-in.
    """
    auth = remote_spec.auth
    origin_url = _require_env(
        auth.credential_env,
        f"remote.auth.credential_env names {auth.credential_env!r}, which is not "
        "set in this environment. It holds the Origin header the provider gates "
        "admission on, so it cannot be defaulted and must never live in a "
        "config row.",
    )

    headers = dict(remote_spec.headers or {})
    user_agent = headers.pop("User-Agent", "") or headers.pop("user-agent", "")

    return RemoteConnection(
        provider=remote_spec.provider,
        base_url=remote_spec.base_url,
        stream_url=remote_spec.stream_url,
        user_agent=user_agent,
        allowed_hosts=_apply_host_ceiling(settings, as_hosts(remote_spec.allowed_hosts)),
        headers=tuple(sorted(headers.items())),
        connect_timeout_s=remote_spec.timeouts.connect_s,
        read_timeout_s=remote_spec.timeouts.read_s,
        stream_connect_timeout_s=remote_spec.timeouts.stream_connect_s,
        # NAMES and non-secret settings only. The variable name is carried so
        # a misconfiguration can name the variable an operator has to go and
        # set, which is the whole reason for the indirection.
        auth=(
            ("scheme", auth.scheme),
            ("credential_env", auth.credential_env),
        ),
        options=options,
        origin_url=origin_url,
    )


def _require_env(name: str, message: str) -> str:
    value = os.getenv(name) if name else None
    if not value:
        raise ProviderConfigError(message)
    return value


def _apply_host_ceiling(settings, hosts: Tuple[str, ...]) -> Tuple[str, ...]:
    """Intersect a spec-supplied allowlist with the deployment's ceiling.

    ``allowed_hosts`` is an SSRF control, and once it is writable through the
    admin API an admin can widen it. The ceiling is the operator's backstop:
    hosts outside it are dropped, not rejected, so a single stale entry cannot
    break every artifact fetch.

    An UNSET ceiling means no ceiling -- deliberate, so this is
    behaviour-preserving for every deployment that does not opt in.
    """
    ceiling = split_hosts(getattr(settings, "provider_host_ceiling", ""))
    if not ceiling:
        return hosts
    return tuple(h for h in hosts if h in ceiling)
