"""Unit tests for MitraConnection, resolve_connection and MitraClientRegistry.

The invariant under test throughout is: **env is the floor**. A spec that
overrides nothing must resolve to exactly what Settings says, so introducing
per-tenant configuration cannot change any existing deployment's behaviour.
"""
from __future__ import annotations

import dataclasses
from typing import List, Optional

from app.integrations.mitra.connection import (
    MitraClientRegistry,
    MitraConnection,
    from_settings,
    resolve_connection,
)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Settings:
    mitra_base_url: str = "https://mitra.example.com"
    mitra_ws_url: str = "wss://mitra.example.com/ws/common/"
    mitra_origin_url: str = "https://origin.example.com"
    mitra_user_agent: str = "test-agent"
    mitra_allowed_hosts: str = ""
    mitra_host_ceiling: str = ""
    mitra_connect_timeout_s: float = 10.0
    mitra_read_timeout_s: float = 30.0
    mitra_ws_connect_timeout_s: float = 10.0
    mitra_ip_city: str = ""
    mitra_ip_state: str = ""
    mitra_ip_zip: str = ""
    mitra_profile_path: str = "/api/profile/"
    mitra_generate_session_path: str = "/api/generate-session/"
    mitra_chat_path: str = "/api/companychat/"
    mitra_get_story_path: str = "/api/get-story/"
    mitra_finalize_v1_path: str = "/api/end-story/"
    mitra_finalize_v2_path: str = "/api/end-story/v2/"


@dataclasses.dataclass
class _PathsOverride:
    profile: Optional[str] = None
    generate_session: Optional[str] = None
    chat: Optional[str] = None
    get_story: Optional[str] = None
    finalize_v1: Optional[str] = None
    finalize_v2: Optional[str] = None


@dataclasses.dataclass
class _ConnectionOverride:
    base_url: Optional[str] = None
    ws_url: Optional[str] = None
    user_agent: Optional[str] = None
    allowed_hosts: Optional[List[str]] = None
    paths: Optional[_PathsOverride] = None
    connect_timeout_s: Optional[float] = None
    read_timeout_s: Optional[float] = None
    ws_connect_timeout_s: Optional[float] = None
    ip_city: Optional[str] = None
    ip_state: Optional[str] = None
    ip_zip: Optional[str] = None


@dataclasses.dataclass
class _RemoteSpec:
    connection: Optional[_ConnectionOverride] = None
    origin_env: Optional[str] = None


# ---------------------------------------------------------------------------
# The env floor
# ---------------------------------------------------------------------------


def test_from_settings_maps_every_field():
    conn = from_settings(_Settings())

    assert conn.base_url == "https://mitra.example.com"
    assert conn.ws_url == "wss://mitra.example.com/ws/common/"
    assert conn.user_agent == "test-agent"
    assert conn.origin_url == "https://origin.example.com"
    assert conn.connect_timeout_s == 10.0
    assert conn.read_timeout_s == 30.0
    assert conn.ws_connect_timeout_s == 10.0
    assert conn.paths.finalize_v2 == "/api/end-story/v2/"


def test_no_spec_resolves_to_the_settings_values():
    settings = _Settings()
    assert resolve_connection(settings, None) == from_settings(settings)


def test_a_spec_with_no_connection_block_changes_nothing():
    """The common case, and the one that must stay free: an agent that has not
    opted into per-tenant endpoints resolves to the env floor."""
    settings = _Settings()
    assert resolve_connection(settings, _RemoteSpec()) == from_settings(settings)


def test_allowed_hosts_split_from_the_comma_separated_env_form():
    conn = from_settings(_Settings(mitra_allowed_hosts="a.example.com, B.example.com ,"))
    assert conn.allowed_hosts == ("a.example.com", "b.example.com")


# ---------------------------------------------------------------------------
# Overrides are partial: an unset field falls through to env
# ---------------------------------------------------------------------------


def test_a_partial_override_touches_only_its_own_fields():
    settings = _Settings()
    spec = _RemoteSpec(connection=_ConnectionOverride(base_url="https://tenant.example.com"))

    conn = resolve_connection(settings, spec)

    assert conn.base_url == "https://tenant.example.com"
    # Everything else is still the floor.
    assert conn.ws_url == settings.mitra_ws_url
    assert conn.user_agent == settings.mitra_user_agent
    assert conn.read_timeout_s == settings.mitra_read_timeout_s
    assert conn.paths.finalize_v2 == settings.mitra_finalize_v2_path


def test_a_partial_path_override_keeps_the_other_five_paths():
    spec = _RemoteSpec(
        connection=_ConnectionOverride(paths=_PathsOverride(finalize_v2="/api/end-story/v3/"))
    )

    conn = resolve_connection(_Settings(), spec)

    assert conn.paths.finalize_v2 == "/api/end-story/v3/"
    assert conn.paths.finalize_v1 == "/api/end-story/"
    assert conn.paths.profile == "/api/profile/"


def test_allowed_hosts_override_accepts_a_list():
    spec = _RemoteSpec(connection=_ConnectionOverride(allowed_hosts=["CDN.example.com"]))
    conn = resolve_connection(_Settings(), spec)
    assert conn.allowed_hosts == ("cdn.example.com",)


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
    conn = from_settings(_Settings(mitra_origin_url="https://secret-origin.example.com"))
    assert "secret-origin" not in repr(conn)


def test_a_different_origin_still_produces_a_different_checksum():
    """The checksum is the cache key for both the REST client and the pooled
    socket. If the origin were simply omitted, two scopes with different
    credentials would share a client carrying the wrong one."""
    a = from_settings(_Settings(mitra_origin_url="https://a.example.com"))
    b = from_settings(_Settings(mitra_origin_url="https://b.example.com"))

    assert a.checksum != b.checksum


def test_the_checksum_does_not_contain_the_origin_in_plaintext():
    conn = from_settings(_Settings(mitra_origin_url="https://secret-origin.example.com"))
    assert "secret-origin" not in conn.checksum


# ---------------------------------------------------------------------------
# Checksum identity
# ---------------------------------------------------------------------------


def test_equal_content_produces_an_equal_checksum():
    assert from_settings(_Settings()).checksum == from_settings(_Settings()).checksum


def test_any_meaningful_change_produces_a_different_checksum():
    base = from_settings(_Settings())
    for changed in (
        _Settings(mitra_base_url="https://other.example.com"),
        _Settings(mitra_ws_url="wss://other.example.com/ws/"),
        _Settings(mitra_user_agent="other-agent"),
        _Settings(mitra_read_timeout_s=31.0),
        _Settings(mitra_finalize_v2_path="/api/end-story/v3/"),
        _Settings(mitra_ip_city="Chennai"),
        _Settings(mitra_allowed_hosts="cdn.example.com"),
    ):
        assert from_settings(changed).checksum != base.checksum


def test_allowed_hosts_order_does_not_change_the_checksum():
    """Otherwise a reordered admin edit would evict every pooled channel for
    no behavioural reason."""
    a = from_settings(_Settings(mitra_allowed_hosts="a.example.com,b.example.com"))
    b = from_settings(_Settings(mitra_allowed_hosts="b.example.com,a.example.com"))
    assert a.checksum == b.checksum


# ---------------------------------------------------------------------------
# The SSRF host ceiling
# ---------------------------------------------------------------------------


def test_an_unset_ceiling_imposes_no_restriction():
    """Behaviour-preserving by default -- every existing deployment keeps
    today's allowlist semantics until it opts in."""
    settings = _Settings(mitra_host_ceiling="")
    spec = _RemoteSpec(connection=_ConnectionOverride(allowed_hosts=["anything.example.com"]))

    conn = resolve_connection(settings, spec)

    assert conn.allowed_hosts == ("anything.example.com",)


def test_the_ceiling_drops_hosts_outside_it():
    settings = _Settings(mitra_host_ceiling="cdn.example.com,static.example.com")
    spec = _RemoteSpec(
        connection=_ConnectionOverride(
            allowed_hosts=["cdn.example.com", "attacker.example.com"]
        )
    )

    conn = resolve_connection(settings, spec)

    assert conn.allowed_hosts == ("cdn.example.com",)


def test_the_ceiling_does_not_constrain_the_env_allowlist():
    """The ceiling exists to bound what a CONFIG WRITE can reach. The operator's
    own env value is not something they need protecting from."""
    settings = _Settings(
        mitra_allowed_hosts="ops-chosen.example.com",
        mitra_host_ceiling="cdn.example.com",
    )

    conn = resolve_connection(settings, _RemoteSpec())

    assert conn.allowed_hosts == ("ops-chosen.example.com",)


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
            _RemoteSpec(connection=_ConnectionOverride(base_url="https://tenant.example.com")),
        )
    )

    assert default_client is not tenant_client
    assert default_client._base_url == "https://mitra.example.com"
    assert tenant_client._base_url == "https://tenant.example.com"


def test_the_client_carries_the_resolved_paths_and_timeouts():
    registry = MitraClientRegistry()
    spec = _RemoteSpec(
        connection=_ConnectionOverride(
            read_timeout_s=99.0,
            paths=_PathsOverride(finalize_v2="/api/end-story/v3/"),
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
                _RemoteSpec(connection=_ConnectionOverride(base_url=f"https://h{i}.example.com")),
            )
        )
    assert len(registry._cache) <= MitraClientRegistry._MAX
