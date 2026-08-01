"""Executable versions of the three rules that keep this port correct.

Each of these encodes a decision that is invisible at the call site and whose
violation fails only under load, or only in production, or only silently.
"""
from __future__ import annotations

import inspect

import anyio.to_thread
import pytest
from fastapi.routing import APIRoute


def _api_routes(api_app):
    return [r for r in api_app.routes if isinstance(r, APIRoute)]


def test_no_endpoint_is_a_coroutine(api_app):
    """Every endpoint must be a plain `def`, never `async def`.

    Starlette runs a non-coroutine endpoint in the anyio worker threadpool.
    That is what lets the turn pipeline hold a SESSION-scoped
    `pg_try_advisory_lock` on one connection across a commit and across a
    handler call of up to 60s, and what lets MitraChannel use `threading.Lock`
    and a blocking `queue.Queue`.

    An `async def` endpoint here would block the event loop for the length of a
    `requests` call, and would hard-crash on the LLM path --
    `_NormalizedChatLiteLLM._agenerate` raises NotImplementedError.

    The two body-reading dependencies in app/dependencies/body.py ARE async and
    correctly so; they do no blocking work. They are not endpoints, so they are
    not covered here.
    """
    offenders = [
        f"{sorted(r.methods - {'HEAD'})} {r.path}"
        for r in _api_routes(api_app)
        if inspect.iscoroutinefunction(r.endpoint)
    ]
    assert not offenders, (
        "These endpoints are `async def` and would block the event loop:\n  "
        + "\n  ".join(offenders)
    )


def test_no_route_declares_a_response_model(api_app):
    """No route may declare a response_model.

    FastAPI's response_model silently reshapes: it drops keys the model does
    not declare and injects nulls for ones it does. Several responses here
    cannot survive that --

      * GET /api/agents' first entry deliberately has NO `key` field
      * /api/sessions/{id}/resume returns three mutually incompatible key sets
      * /api/sessions/{id}/report's 202 body carries no envelope at all
      * messages[].options is the raw stored JSONB list, not a projection

    Every route returns a JSONResponse instead, which FastAPI passes through
    untouched.
    """
    offenders = [
        f"{sorted(r.methods - {'HEAD'})} {r.path} -> {r.response_model}"
        for r in _api_routes(api_app)
        if r.response_model is not None
    ]
    assert not offenders, (
        "These routes declare a response_model and may reshape their body:\n  "
        + "\n  ".join(offenders)
    )


def test_threadpool_sizing_clamps_to_the_db_pool(api_app):
    """The anyio threadpool must not be able to outnumber DB connections.

    One request == one worker thread == one Session == one pooled connection,
    held for the whole turn. anyio's default limiter is 40; the engine grants
    db_pool_size (16) + db_max_overflow (8). If the threadpool exceeds
    db_pool_size, surplus requests block in QueuePool.connect() until
    pool_timeout and then surface to the client as a 500 INTERNAL rather than
    as backpressure -- a failure mode that only appears under load.

    Asserted two ways: the clamp itself, and the value actually applied once
    the lifespan has run.
    """
    from types import SimpleNamespace

    from app.core.concurrency import resolve_threadpool_size

    settings = api_app.state.container.settings
    assert resolve_threadpool_size(settings) <= settings.db_pool_size

    # An over-large request must be clamped, not honoured...
    assert resolve_threadpool_size(SimpleNamespace(threadpool_size=999, db_pool_size=16)) == 16
    # ...an unset one defaults to the pool size...
    assert resolve_threadpool_size(SimpleNamespace(threadpool_size=None, db_pool_size=16)) == 16
    # ...and a smaller one is honoured as-is.
    assert resolve_threadpool_size(SimpleNamespace(threadpool_size=4, db_pool_size=16)) == 4


def test_size_threadpool_actually_mutates_the_limiter(api_app):
    """`size_threadpool` must really move the limiter, not just compute a number.

    anyio's default thread limiter lives in a RunVar scoped to the running
    event loop, so it can only be read from inside one -- which is why this
    runs the call under `anyio.run` rather than reading it at module scope.
    In production the equivalent loop is uvicorn's, and the call site is the
    lifespan in app/main.py.
    """
    import anyio

    settings = api_app.state.container.settings
    expected = min(settings.threadpool_size or settings.db_pool_size, settings.db_pool_size)

    async def apply_and_read():
        applied = size_threadpool_fn(settings)
        return applied, anyio.to_thread.current_default_thread_limiter().total_tokens

    from app.core.concurrency import size_threadpool as size_threadpool_fn

    applied, observed = anyio.run(apply_and_read)
    assert applied == expected
    assert observed == expected


def test_every_api_path_from_the_flask_app_still_exists(api_app):
    """The full route surface, pinned.

    GET / is deliberately absent -- the React app serves the shell now.

    GET /api/ui/capabilities is the one ADDITION to the Flask surface. It
    serves the sidebar's capability document, which under Flask was literal
    markup in templates/index.html and so had no route. It is optional by
    contract: the frontend ships an identical bundled copy and treats a 404 as
    "no server opinion" (app/routers/ui.py). Every other entry below is a Flask
    path that must keep existing.
    """
    expected = {
        ("POST", "/api/chat"),
        ("POST", "/api/reset"),
        ("GET", "/api/conversations"),
        ("GET", "/api/conversations/{conversation_id}/messages"),
        ("GET", "/api/agents"),
        ("GET", "/api/sessions/{session_id}"),
        ("POST", "/api/sessions/{session_id}/finalize"),
        ("POST", "/api/sessions/{session_id}/resume"),
        ("POST", "/api/sessions/{session_id}/abandon"),
        ("GET", "/api/sessions/{session_id}/report"),
        ("GET", "/api/agents/{key}"),
        ("PATCH", "/api/agents/{key}"),
        ("POST", "/api/agents/{key}/config"),
        ("GET", "/api/agents/{key}/config/versions"),
        ("POST", "/api/agents/{key}/config/{version}/activate"),
        ("POST", "/api/agents/reload"),
        ("GET", "/api/tools"),
        ("GET", "/api/ui/capabilities"),
    }
    actual = {
        (m, r.path)
        for r in _api_routes(api_app)
        for m in (r.methods - {"HEAD", "OPTIONS"})
        if r.path.startswith("/api/")
    }
    assert actual == expected, (
        f"missing: {sorted(expected - actual)}\nunexpected: {sorted(actual - expected)}"
    )


def test_reload_is_declared_before_the_agent_key_wildcard(api_app):
    """`/api/agents/reload` must win over `/api/agents/{key}`.

    Flask ranked URL rules by specificity regardless of registration order.
    FastAPI matches in registration order and first match wins, so declaring
    the wildcard first would make POST /api/agents/reload resolve as an agent
    whose key is the literal string "reload".
    """
    paths = [r.path for r in _api_routes(api_app)]
    assert paths.index("/api/agents/reload") < paths.index("/api/agents/{key}")


@pytest.mark.parametrize("bad_uuid", ["not-a-uuid", "123", "xyz"])
def test_malformed_uuid_path_params_404_and_never_422(client, bad_uuid):
    """Flask's <uuid:...> converter failed to match, so the URL did not exist
    and the answer was a 404. FastAPI typing these as uuid.UUID would answer
    422 with its own {"detail": [...]} body instead.

    This matters beyond aesthetics: the frontend treats 404 on a conversation
    as "forget this conversation" and would not recognise a 422.
    """
    for path in (f"/api/sessions/{bad_uuid}", f"/api/conversations/{bad_uuid}/messages"):
        response = client.get(path)
        assert response.status_code == 404, f"{path} -> {response.status_code}"
        body = response.json()
        assert body["status"] == "error"
        assert body["error_code"] in ("SESSION_NOT_FOUND", "CONVERSATION_NOT_FOUND")


def test_request_id_is_generated_and_echoed(client):
    """Port of Flask's before_request/after_request pair."""
    generated = client.get("/api/agents")
    assert generated.headers.get("x-request-id")

    supplied = client.get("/api/agents", headers={"X-Request-ID": "caller-supplied-42"})
    assert supplied.headers.get("x-request-id") == "caller-supplied-42"


def test_error_envelope_carries_the_request_id(client):
    """The envelope reads request_id off the ContextVar. If the middleware were
    BaseHTTPMiddleware instead of pure ASGI, this would be null."""
    response = client.post(
        "/api/chat",
        json={},
        headers={"X-Request-ID": "envelope-check"},
    )
    assert response.status_code == 400
    assert response.json()["request_id"] == "envelope-check"
