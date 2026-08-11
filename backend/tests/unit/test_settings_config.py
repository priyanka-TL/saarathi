"""The configuration surface: API_PREFIX normalisation and the Mitra paths.

Nothing here touches the filesystem's env files -- Settings is constructed with
explicit kwargs so the assertions pin behaviour, not the current .env contents.
"""

from __future__ import annotations

import pytest

from app.core.settings import Settings
from app.providers.mitra.spec import MitraOptions, MitraPaths


def _settings(**overrides) -> Settings:
    return Settings(OPENROUTER_API_KEY="test", **overrides)


# ---------------------------------------------------------------------------
# api_prefix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", ""),
        ("   ", ""),
        ("/", ""),
        ("saarathi-service", "/saarathi-service"),
        ("/saarathi-service", "/saarathi-service"),
        ("saarathi-service/", "/saarathi-service"),
        ("/saarathi-service/", "/saarathi-service"),
    ],
)
def test_api_prefix_is_normalised(raw, expected):
    # Routes are mounted as f"{api_prefix}/api/...", so every spelling must
    # collapse to exactly "" or "/segment" or the mounted paths go wrong.
    assert _settings(api_prefix=raw).api_prefix == expected


def test_api_prefix_defaults_to_empty():
    # The default must keep the unprefixed paths byte-identical.
    assert _settings().api_prefix == ""


# ---------------------------------------------------------------------------
# server binding
# ---------------------------------------------------------------------------

def test_server_binding_defaults():
    s = _settings()
    assert (s.host, s.port, s.workers) == ("127.0.0.1", 8000, 1)


def test_server_binding_is_configurable():
    s = _settings(host="0.0.0.0", port=9000)
    assert (s.host, s.port) == ("0.0.0.0", 9000)


# ---------------------------------------------------------------------------
# Settings carries NO per-platform fields any more
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "field",
    [
        "mitra_base_url", "mitra_ws_url", "mitra_user_agent", "mitra_allowed_hosts",
        "mitra_connect_timeout_s", "mitra_read_timeout_s", "mitra_ws_connect_timeout_s",
        "mitra_ip_city", "mitra_ip_state", "mitra_ip_zip",
        "mitra_profile_path", "mitra_generate_session_path", "mitra_chat_path",
        "mitra_get_story_path", "mitra_finalize_v1_path", "mitra_finalize_v2_path",
        # Retired with the second delegated agent type. Enablement is per
        # provider now (PROVIDERS_ENABLED), and everything else below moved into
        # `remote` on the agent config row where it is per tenant.
        "mitra_enabled", "saathi_enabled",
        "mitra_host_ceiling", "mitra_max_open_channels", "mitra_idle_close_s",
        "saathi_login_mechanism", "saathi_tenant_code", "elevate_base_url",
        # CREDENTIALS. Still in .env -- but named by `remote.auth`, never held as
        # a Settings field, so adding a platform adds no field here.
        "mitra_origin_url", "saathi_origin_url",
        "saathi_access_token", "saathi_email", "saathi_password",
    ],
)
def test_the_moved_fields_are_gone_from_settings(field):
    # extra="ignore" means a leftover key in a .env is accepted silently, so
    # this is the only place that can catch one being quietly reintroduced.
    assert field not in Settings.model_fields


def test_the_structural_provider_fields_remain():
    # Each is here for a reason that rules out a config row: a switch read
    # before any config loads, an SSRF backstop ON config, and the bounds of one
    # pool per provider shared by every agent using it.
    for field in ("providers_enabled", "provider_host_ceiling",
                  "provider_max_open_channels", "provider_idle_close_s"):
        assert field in Settings.model_fields


def test_enablement_is_one_key_however_many_platforms_exist():
    """The point of the rename. Two platforms meant two flags and three gates;
    a third would have meant three flags and nine."""
    # `saarthi_admin_enabled` is THIS APP's own admin surface (SAARTHI, with
    # the R) and `voice_enabled` is a feature of it -- neither is a remote
    # platform. Worth spelling out: SAARTHI_*, SAATHI_* and MITRA_* are three
    # different things one character apart, and conflating the first two is how
    # a reader ends up "generalising" the app's own auth switch.
    not_a_platform = {"voice_enabled", "auth_check", "saarthi_admin_enabled"}
    per_platform = [
        f for f in Settings.model_fields
        if f.endswith("_enabled") and f not in not_a_platform
    ]
    assert per_platform == ["providers_enabled"], (
        f"a per-platform enable flag has reappeared: {per_platform}"
    )


# ---------------------------------------------------------------------------
# Mitra paths
# ---------------------------------------------------------------------------

def test_there_is_now_only_one_copy_of_the_paths():
    """The duplicate this test used to police is GONE.

    The domain layer restated these six paths because it is import-pure and
    could not import the client's copy; nothing but a test kept the two honest,
    and a drift would have repointed an endpoint for every config that did not
    name one explicitly. They live in the provider package now -- one copy, next
    to the client that uses it -- so the drift is not possible rather than
    merely detected.
    """
    from app.domain import agent_spec

    for name in dir(agent_spec):
        assert "PATH" not in name.upper() or name == "DEFAULT_REPORT_MEDIA_TYPE", (
            f"app.domain.agent_spec has grown a provider path constant: {name}"
        )


def test_mitra_paths_default_to_the_shipped_contract():
    assert MitraPaths().finalize_v2 == "/api/end-story/v2/"


def test_mitra_paths_follow_the_agent_config():
    options = MitraOptions(
        bot_route="/guided_guest",
        company="testco",
        paths={"finalize_v2": "/api/end-story/v3/"},
    )
    assert options.paths.finalize_v2 == "/api/end-story/v3/"
    # The five it did not name keep the contract defaults.
    assert options.paths.finalize_v1 == "/api/end-story/"


def test_the_options_block_is_strict():
    """A mistyped option must not be dropped in silence and take a default --
    for a finalize endpoint that is an HTTP 200 and a blank PDF."""
    with pytest.raises(ValueError):
        MitraOptions(bot_route="/r", company="c", finalize_pathh="/api/end-story/")


def test_is_v2_finalize_ignores_trailing_slashes():
    # Configs disagree about trailing slashes constantly; token placement must
    # not depend on that.
    paths = MitraPaths()
    assert paths.is_v2_finalize("/api/end-story/v2")
    assert paths.is_v2_finalize("/api/end-story/v2/")
    assert not paths.is_v2_finalize("/api/end-story/")


def test_is_v2_finalize_tracks_the_configured_path():
    paths = MitraPaths(finalize_v2="/v2/finish/")
    assert paths.is_v2_finalize("/v2/finish/")
    # The old default is no longer v2 -- it would take the v1 body shape.
    assert not paths.is_v2_finalize("/api/end-story/v2/")


def test_is_known_finalize_rejects_an_unrecognised_path():
    # This is the guard POST /api/agents/{key}/config uses: an unknown path
    # silently falls through to the v1 branch and finalises with the wrong body.
    paths = MitraPaths()
    assert paths.is_known_finalize("/api/end-story/")
    assert paths.is_known_finalize("/api/end-story/v2")
    assert not paths.is_known_finalize("/api/end-storyy/v2/")
