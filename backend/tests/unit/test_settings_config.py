"""The configuration surface: API_PREFIX normalisation and the Mitra paths.

Nothing here touches the filesystem's env files -- Settings is constructed with
explicit kwargs so the assertions pin behaviour, not the current .env contents.
"""

from __future__ import annotations

import pytest

from app.core.settings import Settings
from app.domain.agent_spec import MitraConnectionSpec, MitraPathsSpec
from app.integrations.mitra.rest_client import MitraPaths


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
# Settings carries NO Mitra connection fields any more
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "field",
    [
        "mitra_base_url", "mitra_ws_url", "mitra_user_agent", "mitra_allowed_hosts",
        "mitra_connect_timeout_s", "mitra_read_timeout_s", "mitra_ws_connect_timeout_s",
        "mitra_ip_city", "mitra_ip_state", "mitra_ip_zip",
        "mitra_profile_path", "mitra_generate_session_path", "mitra_chat_path",
        "mitra_get_story_path", "mitra_finalize_v1_path", "mitra_finalize_v2_path",
    ],
)
def test_the_moved_fields_are_gone_from_settings(field):
    # They live in `remote.connection` on the agent config now. extra="ignore"
    # means a leftover key in a .env is accepted silently, so this is the only
    # place that can catch one being quietly reintroduced.
    assert field not in Settings.model_fields


def test_the_four_structural_mitra_fields_remain():
    # Each is here for a reason that rules out a config row: a flag read before
    # any config loads, a credential, an SSRF backstop ON config, and the bounds
    # of one process-wide pool shared by every agent.
    for field in ("mitra_enabled", "mitra_origin_url", "mitra_host_ceiling",
                  "mitra_max_open_channels", "mitra_idle_close_s"):
        assert field in Settings.model_fields


# ---------------------------------------------------------------------------
# Mitra paths
# ---------------------------------------------------------------------------

def test_the_spec_defaults_match_the_clients_defaults():
    """MitraPathsSpec restates MitraPaths' defaults because the domain layer is
    import-pure (.importlinter) and cannot import them. Nothing but this test
    keeps the two copies honest, and a drift would repoint an endpoint for every
    config that does not name it explicitly."""
    spec, client = MitraPathsSpec(), MitraPaths()
    for name in ("profile", "generate_session", "chat", "get_story",
                 "finalize_v1", "finalize_v2"):
        assert getattr(spec, name) == getattr(client, name), name


def test_mitra_paths_default_to_the_shipped_contract():
    assert MitraPathsSpec().finalize_v2 == "/api/end-story/v2/"
    assert MitraPaths().finalize_v2 == "/api/end-story/v2/"


def test_mitra_paths_follow_the_agent_config():
    conn = MitraConnectionSpec(
        base_url="https://mitra.example.com",
        ws_url="wss://mitra.example.com/ws/common/",
        paths={"finalize_v2": "/api/end-story/v3/"},
    )
    assert conn.paths.finalize_v2 == "/api/end-story/v3/"
    # The five it did not name keep the contract defaults.
    assert conn.paths.finalize_v1 == "/api/end-story/"


def test_a_connection_spec_requires_an_endpoint():
    # No env floor behind it: a missing base_url has nothing to fall back to.
    with pytest.raises(ValueError):
        MitraConnectionSpec(ws_url="wss://mitra.example.com/ws/common/")


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
