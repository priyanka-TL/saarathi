"""Unit tests for MitraConnection, resolve_connection and MitraClientRegistry.

The invariant under test throughout is: **the spec is the only source**. Which
Mitra deployment an agent reaches comes entirely from `remote.connection`, with
exactly two exceptions that must keep coming from Settings -- the Origin
credential and the SSRF host ceiling, neither of which may live in a config row.
"""
from __future__ import annotations

import dataclasses
from typing import List, Optional

import pytest

from app.integrations.mitra.connection import (
    MitraClientRegistry,
    MitraConnection,
    resolve_connection,
)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Settings:
    """The FOUR Mitra keys left on Settings. That this double is this small is
    the point of the change these tests cover."""

    mitra_origin_url: str = "https://origin.example.com"
    mitra_host_ceiling: str = ""
    mitra_max_open_channels: int = 200
    mitra_idle_close_s: float = 1200.0


@dataclasses.dataclass
class _Paths:
    profile: str = "/api/profile/"
    generate_session: str = "/api/generate-session/"
    chat: str = "/api/companychat/"
    get_story: str = "/api/get-story/"
    finalize_v1: str = "/api/end-story/"
    finalize_v2: str = "/api/end-story/v2/"


@dataclasses.dataclass
class _Connection:
    base_url: str = "https://mitra.example.com"
    ws_url: str = "wss://mitra.example.com/ws/common/"
    user_agent: str = "test-agent"
    allowed_hosts: List[str] = dataclasses.field(default_factory=list)
    paths: _Paths = dataclasses.field(default_factory=_Paths)
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 30.0
    ws_connect_timeout_s: float = 10.0
    ip_city: str = ""
    ip_state: str = ""
    ip_zip: str = ""


@dataclasses.dataclass
class _RemoteSpec:
    connection: Optional[_Connection] = dataclasses.field(default_factory=_Connection)
    origin_env: Optional[str] = None


# ---------------------------------------------------------------------------
# The spec is the source
# ---------------------------------------------------------------------------


def test_every_field_comes_across_from_the_spec():
    conn = resolve_connection(_Settings(), _RemoteSpec())

    assert conn.base_url == "https://mitra.example.com"
    assert conn.ws_url == "wss://mitra.example.com/ws/common/"
    assert conn.user_agent == "test-agent"
    assert conn.connect_timeout_s == 10.0
    assert conn.read_timeout_s == 30.0
    assert conn.ws_connect_timeout_s == 10.0
    assert conn.paths.finalize_v2 == "/api/end-story/v2/"
    # ...and the credential still comes from Settings.
    assert conn.origin_url == "https://origin.example.com"


def test_a_scoped_spec_resolves_to_its_own_endpoint():
    conn = resolve_connection(
        _Settings(),
        _RemoteSpec(connection=_Connection(base_url="https://tenant.example.com")),
    )

    assert conn.base_url == "https://tenant.example.com"


def test_each_path_is_carried_independently():
    spec = _RemoteSpec(
        connection=_Connection(paths=_Paths(finalize_v2="/api/end-story/v3/"))
    )

    conn = resolve_connection(_Settings(), spec)

    assert conn.paths.finalize_v2 == "/api/end-story/v3/"
    assert conn.paths.finalize_v1 == "/api/end-story/"
    assert conn.paths.profile == "/api/profile/"


def test_allowed_hosts_are_normalised_to_lowercase():
    spec = _RemoteSpec(connection=_Connection(allowed_hosts=["CDN.example.com"]))
    conn = resolve_connection(_Settings(), spec)
    assert conn.allowed_hosts == ("cdn.example.com",)


def test_a_missing_connection_block_is_refused_rather_than_guessed():
    """There is no environment floor to fall back to any more. Inventing an
    endpoint would send an interview somewhere nobody configured -- and that
    failure would surface as a blank PDF, not an exception."""
    with pytest.raises(ValueError, match="remote.connection is missing"):
        resolve_connection(_Settings(), _RemoteSpec(connection=None))


# ---------------------------------------------------------------------------
# The Origin credential
# ---------------------------------------------------------------------------


def test_origin_comes_from_the_named_variable_not_from_the_spec(monkeypatch):
    monkeypatch.setenv("MITRA_TENANT_X_ORIGIN", "https://tenant-x.example.com")
    spec = _RemoteSpec(origin_env="MITRA_TENANT_X_ORIGIN")

    conn = resolve_connection(_Settings(), spec)

    assert conn.origin_url == "https://tenant-x.example.com"


def test_an_unset_origin_variable_falls_back_rather_than_sending_an_empty_origin(monkeypatch):
    monkeypatch.delenv("MITRA_MISSING_ORIGIN", raising=False)
    spec = _RemoteSpec(origin_env="MITRA_MISSING_ORIGIN")

    conn = resolve_connection(_Settings(), spec)

    assert conn.origin_url == "https://origin.example.com"


def test_the_origin_never_appears_in_repr():
    """It is a credential (see MitraRestClient's module docstring). Any log
    line that reprs a connection must not leak it."""
    conn = resolve_connection(
        _Settings(mitra_origin_url="https://secret-origin.example.com"), _RemoteSpec()
    )
    assert "secret-origin" not in repr(conn)


def test_a_different_origin_still_produces_a_different_checksum():
    """The checksum is the cache key for both the REST client and the pooled
    socket. If the origin were simply omitted, two scopes with different
    credentials would share a client carrying the wrong one."""
    a = resolve_connection(_Settings(mitra_origin_url="https://a.example.com"), _RemoteSpec())
    b = resolve_connection(_Settings(mitra_origin_url="https://b.example.com"), _RemoteSpec())

    assert a.checksum != b.checksum


def test_the_checksum_does_not_contain_the_origin_in_plaintext():
    conn = resolve_connection(
        _Settings(mitra_origin_url="https://secret-origin.example.com"), _RemoteSpec()
    )
    assert "secret-origin" not in conn.checksum


# ---------------------------------------------------------------------------
# Checksum identity
# ---------------------------------------------------------------------------


def _conn(**overrides) -> MitraConnection:
    return resolve_connection(_Settings(), _RemoteSpec(connection=_Connection(**overrides)))


def test_equal_content_produces_an_equal_checksum():
    assert _conn().checksum == _conn().checksum


def test_any_meaningful_change_produces_a_different_checksum():
    base = _conn()
    for changed in (
        _conn(base_url="https://other.example.com"),
        _conn(ws_url="wss://other.example.com/ws/"),
        _conn(user_agent="other-agent"),
        _conn(read_timeout_s=31.0),
        _conn(paths=_Paths(finalize_v2="/api/end-story/v3/")),
        _conn(ip_city="Chennai"),
        _conn(allowed_hosts=["cdn.example.com"]),
    ):
        assert changed.checksum != base.checksum


def test_allowed_hosts_order_does_not_change_the_checksum():
    """Otherwise a reordered admin edit would evict every pooled channel for
    no behavioural reason."""
    a = _conn(allowed_hosts=["a.example.com", "b.example.com"])
    b = _conn(allowed_hosts=["b.example.com", "a.example.com"])
    assert a.checksum == b.checksum


# ---------------------------------------------------------------------------
# The SSRF host ceiling
# ---------------------------------------------------------------------------


def test_an_unset_ceiling_imposes_no_restriction():
    """Behaviour-preserving by default -- every existing deployment keeps
    today's allowlist semantics until it opts in."""
    settings = _Settings(mitra_host_ceiling="")
    spec = _RemoteSpec(connection=_Connection(allowed_hosts=["anything.example.com"]))

    conn = resolve_connection(settings, spec)

    assert conn.allowed_hosts == ("anything.example.com",)


def test_the_ceiling_drops_hosts_outside_it():
    settings = _Settings(mitra_host_ceiling="cdn.example.com,static.example.com")
    spec = _RemoteSpec(
        connection=_Connection(allowed_hosts=["cdn.example.com", "attacker.example.com"])
    )

    conn = resolve_connection(settings, spec)

    assert conn.allowed_hosts == ("cdn.example.com",)


def test_the_ceiling_now_bounds_every_allowlist():
    """It used to spare the env-supplied value, because that one was the
    operator's own. There is no env-supplied allowlist any more -- every
    allowlist arrives through a config write, which is exactly what the ceiling
    exists to bound."""
    settings = _Settings(mitra_host_ceiling="cdn.example.com")
    spec = _RemoteSpec(connection=_Connection(allowed_hosts=["ops-chosen.example.com"]))

    conn = resolve_connection(settings, spec)

    assert conn.allowed_hosts == ()


# ---------------------------------------------------------------------------
# MitraClientRegistry
# ---------------------------------------------------------------------------


def test_the_same_connection_yields_the_same_client():
    registry = MitraClientRegistry()
    settings = _Settings()

    first = registry.get(resolve_connection(settings, _RemoteSpec()))
    second = registry.get(resolve_connection(settings, _RemoteSpec()))

    assert first is second, "one connection pool per distinct configuration, not per call"


def test_different_connections_yield_different_clients():
    """The cross-tenant hazard this exists to prevent: one client carries one
    base URL and one Origin, so sharing it across scopes would send a tenant's
    traffic to another tenant's Mitra."""
    registry = MitraClientRegistry()
    settings = _Settings()

    default_client = registry.get(resolve_connection(settings, _RemoteSpec()))
    tenant_client = registry.get(
        resolve_connection(
            settings,
            _RemoteSpec(connection=_Connection(base_url="https://tenant.example.com")),
        )
    )

    assert default_client is not tenant_client
    assert default_client._base_url == "https://mitra.example.com"
    assert tenant_client._base_url == "https://tenant.example.com"


def test_the_client_carries_the_resolved_paths_and_timeouts():
    registry = MitraClientRegistry()
    spec = _RemoteSpec(
        connection=_Connection(
            read_timeout_s=99.0,
            paths=_Paths(finalize_v2="/api/end-story/v3/"),
        )
    )

    client = registry.get(resolve_connection(_Settings(), spec))

    assert client._timeout == (10.0, 99.0)
    assert client.paths.finalize_v2 == "/api/end-story/v3/"


def test_the_base_url_host_is_trusted_without_being_listed():
    """Preserved from MitraRestClient -- worth pinning here because the
    allowlist now arrives through a different path."""
    registry = MitraClientRegistry()
    client = registry.get(resolve_connection(_Settings(), _RemoteSpec()))
    assert "mitra.example.com" in client._allowed_hosts


def test_the_cache_is_bounded():
    registry = MitraClientRegistry()
    for i in range(MitraClientRegistry._MAX + 5):
        registry.get(
            resolve_connection(
                _Settings(),
                _RemoteSpec(connection=_Connection(base_url=f"https://h{i}.example.com")),
            )
        )
    assert len(registry._cache) <= MitraClientRegistry._MAX
