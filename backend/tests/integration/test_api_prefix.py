"""API_PREFIX must move the WHOLE surface, not just the API routes.

Builds a second app with the prefix set (create_app reads settings at call
time), because the session-scoped `api_app` fixture is the unprefixed one.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

PREFIX = "/saarathi-service"


@pytest.fixture()
def prefixed_client(app_module, monkeypatch):
    from app.core.settings import settings

    monkeypatch.setattr(settings, "api_prefix", PREFIX)
    return TestClient(app_module.create_app(), raise_server_exceptions=False)


def test_api_routes_move_under_the_prefix(prefixed_client):
    assert prefixed_client.get(f"{PREFIX}/api/conversations").status_code == 200
    # And are no longer served at the bare path.
    assert prefixed_client.get("/api/conversations").status_code == 404


def test_healthz_moves_with_the_app(prefixed_client):
    # A probe pointed at the prefix must still find it; the orchestrator only
    # routes the prefix to this service.
    assert prefixed_client.get(f"{PREFIX}/healthz").json() == {"status": "ok"}
    assert prefixed_client.get("/healthz").status_code == 404


def test_docs_move_with_the_app(prefixed_client):
    assert prefixed_client.get(f"{PREFIX}/openapi.json").status_code == 200
    assert prefixed_client.get(f"{PREFIX}/docs").status_code == 200


def test_registry_reload_predicate_follows_the_prefix(prefixed_client, monkeypatch):
    """The TTL-gated reload in get_db keys off the request path.

    Hardcoding "/api/" there would silently stop the registry reloading under a
    prefix -- agent config changes would never take effect until a restart, and
    nothing would report an error.
    """
    from app.services.agent_registry import AgentRegistry

    called: list[bool] = []
    original = AgentRegistry.maybe_reload

    def _spy(self, session):
        called.append(True)
        return original(self, session)

    monkeypatch.setattr(AgentRegistry, "maybe_reload", _spy)

    prefixed_client.get(f"{PREFIX}/api/conversations")
    assert called, "maybe_reload was not called for a prefixed /api/ path"

    called.clear()
    prefixed_client.get(f"{PREFIX}/healthz")
    assert not called, "healthz must not touch the DB for a registry reload"


def test_unprefixed_app_is_unchanged(client):
    # The default (API_PREFIX empty) keeps today's paths exactly.
    assert client.get("/api/conversations").status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}
