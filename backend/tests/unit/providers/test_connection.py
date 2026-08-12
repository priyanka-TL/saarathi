"""Unit tests for RemoteConnection, resolve_connection and ProviderRegistry.

The connection is the CACHE KEY that keeps one tenant's endpoint and credentials
out of another tenant's client, so most of these are checksum properties: equal
content shares, different content does not, and no credential appears in
plaintext anywhere.
"""
from __future__ import annotations

import dataclasses

import pytest

from app.providers.connection import RemoteConnection, resolve_connection
from app.providers.errors import ProviderConfigError, ProviderNotEnabled, UnknownProvider
from app.providers.registry import ProviderRegistry
from tests.provider_factories import MITRA_ORIGIN_ENV, remote_spec

ORIGIN = "https://origin.test.invalid"


class _Settings:
    """Duck-types the two attributes resolve_connection reads off Settings."""

    def __init__(self, ceiling: str = "") -> None:
        self.provider_host_ceiling = ceiling
        self.providers_enabled = "mitra,saathi"
        self.provider_max_open_channels = 8
        self.provider_idle_close_s = 60.0


@pytest.fixture(autouse=True)
def _origin(monkeypatch):
    monkeypatch.setenv(MITRA_ORIGIN_ENV, ORIGIN)


# ---------------------------------------------------------------------------
# resolve_connection
# ---------------------------------------------------------------------------

def test_every_field_comes_across_from_the_spec():
    spec = remote_spec()
    conn = resolve_connection(_Settings(), spec)

    assert conn.provider == "mitra"
    assert conn.base_url == spec.base_url
    assert conn.stream_url == spec.stream_url
    assert conn.connect_timeout_s == spec.timeouts.connect_s
    assert conn.read_timeout_s == spec.timeouts.read_s
    assert conn.stream_connect_timeout_s == spec.timeouts.stream_connect_s


def test_a_scoped_spec_resolves_to_its_own_endpoint():
    """The property the whole per-tenant design rests on: two scopes with
    different endpoints must not collapse onto one connection."""
    a = resolve_connection(_Settings(), remote_spec(base_url="https://a.test"))
    b = resolve_connection(_Settings(), remote_spec(base_url="https://b.test"))

    assert a.base_url != b.base_url
    assert a.checksum != b.checksum


def test_the_user_agent_is_lifted_out_of_the_headers():
    """It is sent on the socket upgrade as well as on REST calls, so it has its
    own slot on the connection while staying one of the configured headers."""
    conn = resolve_connection(
        _Settings(), remote_spec(headers={"User-Agent": "test-agent/1.0"}),
    )
    assert conn.user_agent == "test-agent/1.0"
    assert dict(conn.headers) == {}


def test_other_headers_are_carried_through():
    conn = resolve_connection(
        _Settings(),
        remote_spec(headers={"User-Agent": "ua", "X-Extra": "value"}),
    )
    assert dict(conn.headers) == {"X-Extra": "value"}
    assert conn.header_list() == ["X-Extra: value"]


def test_allowed_hosts_are_normalised_to_lowercase():
    conn = resolve_connection(
        _Settings(), remote_spec(allowed_hosts=["Files.EXAMPLE.com", " other.test "]),
    )
    assert conn.allowed_hosts == ("files.example.com", "other.test")


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def test_the_origin_comes_from_the_named_variable_not_from_the_spec(monkeypatch):
    monkeypatch.setenv(MITRA_ORIGIN_ENV, "https://from-the-environment.test")
    conn = resolve_connection(_Settings(), remote_spec())
    assert conn.origin_url == "https://from-the-environment.test"


def test_an_unset_credential_variable_is_refused_rather_than_defaulted(monkeypatch):
    """A CHANGE IN BEHAVIOUR, and a deliberate one.

    This used to fall back to a deployment-wide setting when the named variable
    was unset. That fallback silently sent one platform's Origin to another's
    endpoint, which is a 403 that reads like an outage rather than like the
    misconfiguration it is. There is no global to fall back to any more, so an
    unset variable fails loudly and names itself.
    """
    monkeypatch.delenv(MITRA_ORIGIN_ENV, raising=False)
    with pytest.raises(ProviderConfigError, match=MITRA_ORIGIN_ENV):
        resolve_connection(_Settings(), remote_spec())


def test_the_origin_never_appears_in_repr():
    conn = resolve_connection(_Settings(), remote_spec())
    assert ORIGIN not in repr(conn)


def test_the_checksum_does_not_contain_the_credential_in_plaintext():
    conn = resolve_connection(_Settings(), remote_spec())
    assert ORIGIN not in conn.checksum


def test_a_different_origin_still_produces_a_different_checksum(monkeypatch):
    """Hashing the credential rather than embedding it must not cost the
    property that made it part of the key."""
    a = resolve_connection(_Settings(), remote_spec())
    monkeypatch.setenv(MITRA_ORIGIN_ENV, "https://a-different-origin.test")
    b = resolve_connection(_Settings(), remote_spec())
    assert a.checksum != b.checksum


def test_saathi_credentials_are_resolved_by_name(monkeypatch):
    """Saathi's remote.auth is Origin-only now (same shape as Mitra's) --
    the per-user ELEVATE token it used to mint from identifier_env/secret_env
    comes from the caller's own UserContext.token instead, never from
    connection config. See app/providers/saathi/provider.py."""
    from tests.provider_factories import SAATHI_ORIGIN_ENV

    monkeypatch.setenv(SAATHI_ORIGIN_ENV, "https://saathi-origin.test")

    conn = resolve_connection(_Settings(), remote_spec("saathi"))
    assert conn.origin_url == "https://saathi-origin.test"
    # The name is on the connection; the value is not in its repr.
    assert "https://saathi-origin.test" not in repr(conn)
    assert dict(conn.auth)["credential_env"] == SAATHI_ORIGIN_ENV


def test_a_different_origin_produces_a_different_checksum_for_saathi_too(monkeypatch):
    from tests.provider_factories import SAATHI_ORIGIN_ENV

    a = resolve_connection(_Settings(), remote_spec("saathi"))
    monkeypatch.setenv(SAATHI_ORIGIN_ENV, "https://a-different-saathi-origin.test")
    b = resolve_connection(_Settings(), remote_spec("saathi"))
    assert a.checksum != b.checksum


# ---------------------------------------------------------------------------
# Checksum properties
# ---------------------------------------------------------------------------

def test_equal_content_produces_an_equal_checksum():
    a = resolve_connection(_Settings(), remote_spec())
    b = resolve_connection(_Settings(), remote_spec())
    assert a.checksum == b.checksum


@pytest.mark.parametrize("field,value", [
    ("base_url", "https://elsewhere.test"),
    ("stream_url", "wss://elsewhere.test/ws/"),
])
def test_any_meaningful_change_produces_a_different_checksum(field, value):
    base = resolve_connection(_Settings(), remote_spec())
    changed = resolve_connection(_Settings(), remote_spec(**{field: value}))
    assert base.checksum != changed.checksum


def test_the_provider_name_is_part_of_the_checksum():
    """Two providers reaching the same host must still get their own client:
    they authenticate differently and speak different REST surfaces."""
    a = resolve_connection(_Settings(), remote_spec())
    b = dataclasses.replace(a, provider="something_else")
    assert a.checksum != b.checksum


def test_the_options_are_part_of_the_checksum():
    """`options` carries the endpoint paths and the bot route, so a change to it
    must not be served from a cached client built before the change."""
    a = RemoteConnection(
        provider="p", base_url="u", stream_url="w", user_agent="ua", options={"a": 1},
    )
    b = RemoteConnection(
        provider="p", base_url="u", stream_url="w", user_agent="ua", options={"a": 2},
    )
    assert a.checksum != b.checksum


def test_allowed_hosts_order_does_not_change_the_checksum():
    a = resolve_connection(_Settings(), remote_spec(allowed_hosts=["a.test", "b.test"]))
    b = resolve_connection(_Settings(), remote_spec(allowed_hosts=["b.test", "a.test"]))
    assert a.checksum == b.checksum


# ---------------------------------------------------------------------------
# The operator's SSRF ceiling
# ---------------------------------------------------------------------------

def test_an_unset_ceiling_imposes_no_restriction():
    conn = resolve_connection(
        _Settings(ceiling=""), remote_spec(allowed_hosts=["anything.test"]),
    )
    assert conn.allowed_hosts == ("anything.test",)


def test_the_ceiling_drops_hosts_outside_it():
    """Dropped, not rejected: a single stale entry must not break every
    artifact fetch."""
    conn = resolve_connection(
        _Settings(ceiling="allowed.test"),
        remote_spec(allowed_hosts=["allowed.test", "sneaky.test"]),
    )
    assert conn.allowed_hosts == ("allowed.test",)


def test_the_ceiling_bounds_a_config_an_admin_can_widen():
    """allowed_hosts is writable through the admin API, so the ceiling is the
    operator's backstop on it."""
    conn = resolve_connection(
        _Settings(ceiling="only-this.test"),
        remote_spec(allowed_hosts=["internal.metadata.test"]),
    )
    assert conn.allowed_hosts == ()


# ---------------------------------------------------------------------------
# ProviderRegistry
# ---------------------------------------------------------------------------

def test_the_same_spec_yields_the_same_provider_instance():
    registry = ProviderRegistry(_Settings())
    spec = remote_spec()
    assert registry.get(spec) is registry.get(spec)


def test_different_endpoints_yield_different_provider_instances():
    """A provider owns a requests.Session and its fixed headers -- including the
    Origin credential -- so it must not be shared across scopes that resolved
    different values."""
    registry = ProviderRegistry(_Settings())
    a = registry.get(remote_spec(base_url="https://a.test"))
    b = registry.get(remote_spec(base_url="https://b.test"))
    assert a is not b


def test_a_provider_carries_its_resolved_options():
    registry = ProviderRegistry(_Settings())
    provider = registry.get(remote_spec(company="acme", bot_route="/route"))
    assert provider.options.company == "acme"
    assert provider.options.bot_route == "/route"


def test_a_disabled_provider_is_refused_by_name():
    class _Off(_Settings):
        def __init__(self):
            super().__init__()
            self.providers_enabled = "saathi"

    registry = ProviderRegistry(_Off())
    with pytest.raises(ProviderNotEnabled, match="mitra"):
        registry.get(remote_spec())


def test_an_unregistered_provider_is_refused_distinctly():
    """A typo in a config row and a deliberate deployment choice are different
    problems, fixed in different places."""
    registry = ProviderRegistry(_Settings())
    with pytest.raises(UnknownProvider):
        registry.descriptor("no_such_platform")


def test_a_bad_options_block_is_refused_with_a_readable_message():
    registry = ProviderRegistry(_Settings())
    with pytest.raises(ProviderConfigError, match="options"):
        # `company` is required for this provider and cannot be defaulted.
        registry.get(remote_spec(options={"company": None}))


def test_the_instance_cache_is_bounded():
    registry = ProviderRegistry(_Settings())
    for i in range(ProviderRegistry._MAX + 5):
        registry.get(remote_spec(base_url=f"https://host-{i}.test"))
    assert len(registry._cache) <= ProviderRegistry._MAX + 1
