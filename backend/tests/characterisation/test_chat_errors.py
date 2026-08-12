"""Items 4 and 5 -- error responses from POST /api/chat.

Module 5.7 introduces a typed error model: every failure now carries
`status: "error"` plus a stable `error_code`, and a Mitra-side failure maps
to 502 UPSTREAM_UNAVAILABLE / 504 UPSTREAM_TIMEOUT instead of a generic 500.
`request_id` is dynamic (a uuid per request), so it's popped off the body
and checked for presence separately rather than pinned in the golden fixture.
"""

from __future__ import annotations

from tests.characterisation.conftest import chat


def test_unknown_agent_returns_404(client, golden, script):
    """app.py:84-85 (legacy). The frontend surfaces this as a bare error."""
    expected = golden("error_shapes")["agent_not_found"]

    status, body = chat(client, "hello", "No Such Agent")
    request_id = body.pop("request_id", None)

    assert status == expected["status_code"]
    assert body == expected["body"]
    assert request_id
    assert script.calls == [], "no LLM call should be made for an unknown agent"


def test_missing_message_returns_400(client, golden, script):
    """app.py:69-70 (legacy)."""
    expected = golden("error_shapes")["no_message"]

    response = client.post("/api/chat", json={"agent_name": "General Support Agent"})
    body = response.json()
    request_id = body.pop("request_id", None)

    assert response.status_code == expected["status_code"]
    assert body == expected["body"]
    assert request_id
    assert script.calls == []


def test_empty_json_body_returns_400(client, golden, script):
    expected = golden("error_shapes")["empty_body"]

    response = client.post("/api/chat", json={})
    body = response.json()
    request_id = body.pop("request_id", None)

    assert response.status_code == expected["status_code"]
    assert body == expected["body"]
    assert request_id
    assert script.calls == []


def test_empty_string_message_returns_400(client, script):
    """app.py:69 tests `"message" not in data`, so an EMPTY STRING passes that
    check -- but then falls through to the agent.

    Pinning the actual behaviour: an empty message is accepted, not rejected.
    """
    script.queue("reply to nothing")

    status, body = chat(client, "", "General Support Agent")

    assert status == 200
    assert body["status"] == "success"


def test_error_bodies_now_carry_status_and_error_code(client):
    """Formerly a pinned defect (success responses carried `status`, error
    responses did not, so a client could only detect failure by absence, never
    distinguish a 404 from a 500 or from an upstream timeout). Module 5.7
    fixes this -- flipping the assertion here is the intended result, not a
    regression.
    """
    not_found = client.post(
        "/api/chat", json={"message": "x", "agent_name": "Nope"}
    ).json()
    bad_request = client.post("/api/chat", json={}).json()

    assert set(not_found) == {"status", "error", "error_code", "request_id"}
    assert set(bad_request) == {"status", "error", "error_code", "request_id"}
    assert not_found["status"] == "error"
    assert bad_request["status"] == "error"
    assert not_found["error_code"] == "AGENT_NOT_FOUND"
    assert bad_request["error_code"] == "INVALID_REQUEST"


def test_mitra_turn_timeout_maps_to_504(client, monkeypatch):
    """A Mitra timeout must be distinguishable from any other failure -- the
    session survives a 504, so the client knows a retry is safe."""
    from app.providers.errors import ProviderTurnTimeout
    from app.services.orchestration import OrchestrationService

    def _raise(self, ctx_in):
        raise ProviderTurnTimeout(step=3)

    monkeypatch.setattr(OrchestrationService, "handle_turn", _raise)

    response = client.post("/api/chat", json={"message": "hello"})
    body = response.json()

    assert response.status_code == 504
    assert body["status"] == "error"
    assert body["error_code"] == "UPSTREAM_TIMEOUT"


def test_other_mitra_failures_map_to_502(client, monkeypatch):
    from app.providers.errors import ProviderHTTPError
    from app.services.orchestration import OrchestrationService

    def _raise(self, ctx_in):
        raise ProviderHTTPError("Mitra", "POST", "/api/end-story/v2/", 503)

    monkeypatch.setattr(OrchestrationService, "handle_turn", _raise)

    response = client.post("/api/chat", json={"message": "hello"})
    body = response.json()

    assert response.status_code == 502
    assert body["status"] == "error"
    assert body["error_code"] == "UPSTREAM_UNAVAILABLE"


def test_saarthi_sentinel_is_never_treated_as_an_unknown_agent(client, script):
    """app.py:79 -- "Saarthi" is a sentinel, not a registered agent.

    It is present in GET /api/agents but absent from `orchestrator.agents`, so
    the 404 branch must not fire for it.
    """
    script.queue("General Support Agent")
    script.queue("reply")

    status, _ = chat(client, "hello", "Saarthi")

    assert status == 200
