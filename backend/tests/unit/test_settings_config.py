"""The configuration surface: API_PREFIX normalisation and the Mitra paths.

Nothing here touches the filesystem's env files -- Settings is constructed with
explicit kwargs so the assertions pin behaviour, not the current .env contents.
"""

from __future__ import annotations

import pytest

from app.core.settings import Settings
from app.integrations.mitra.rest_client import MitraPaths, paths_from_settings


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
# Mitra paths
# ---------------------------------------------------------------------------

def test_mitra_paths_default_to_the_shipped_contract():
    paths = paths_from_settings(_settings())
    assert paths == MitraPaths()
    assert paths.finalize_v2 == "/api/end-story/v2/"


def test_mitra_paths_follow_settings():
    paths = paths_from_settings(_settings(mitra_finalize_v2_path="/api/end-story/v3/"))
    assert paths.finalize_v2 == "/api/end-story/v3/"


def test_is_v2_finalize_ignores_trailing_slashes():
    # The YAML and the setting disagree about trailing slashes constantly;
    # token placement must not depend on that.
    paths = MitraPaths()
    assert paths.is_v2_finalize("/api/end-story/v2")
    assert paths.is_v2_finalize("/api/end-story/v2/")
    assert not paths.is_v2_finalize("/api/end-story/")


def test_is_v2_finalize_tracks_the_configured_path():
    paths = paths_from_settings(_settings(mitra_finalize_v2_path="/v2/finish/"))
    assert paths.is_v2_finalize("/v2/finish/")
    # The old default is no longer v2 -- it would take the v1 body shape.
    assert not paths.is_v2_finalize("/api/end-story/v2/")


def test_is_known_finalize_rejects_an_unrecognised_path():
    # This is the guard ConfigSyncService uses: an unknown path silently falls
    # through to the v1 branch and finalises with the wrong body shape.
    paths = MitraPaths()
    assert paths.is_known_finalize("/api/end-story/")
    assert paths.is_known_finalize("/api/end-story/v2")
    assert not paths.is_known_finalize("/api/end-storyy/v2/")
