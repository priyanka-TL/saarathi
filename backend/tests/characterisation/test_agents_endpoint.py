"""Item 1 -- GET /api/agents.

No LLM is involved, so this response is fully deterministic and is goldened in
its entirety. It is the strongest fixture in the suite: it pins the display
names, the descriptions and the ordering that module 3.8 must reproduce
byte-for-byte when the four hardcoded agents become YAML.

Health & Wellness, Technical Support, and Research were later removed from
the live application; the full registry is now Record Stories, Capture
Discussion, and General Support (in that sort_order). AGENT_NAMES (conftest)
only covers the LLM-type agents used for chat-behavior tests, so the
full-registry order used here is defined locally instead.
"""

from __future__ import annotations

FULL_REGISTRY_NAMES_IN_ORDER = ["Record Stories", "Capture Discussions", "General Support Agent"]

SYNTHETIC_ROUTER_ENTRY = {
    "name": "Saarthi",
    "description": "Automatically routes your request to the best agent",
}


def test_matches_golden_exactly(client, golden):
    """The whole response, locked. Any drift here is a breaking change."""
    response = client.get("/api/agents")
    assert response.status_code == 200
    assert response.json() == golden("api_agents_config")


def test_first_element_is_the_synthetic_router_entry(client):
    """app.py:118-120 hardcodes this entry ahead of the real registry.

    static/js/main.js treats the name "Saarthi" as the sentinel meaning
    "let the router decide", so both the name and its position matter.
    """
    body = client.get("/api/agents").json()
    assert body[0] == SYNTHETIC_ROUTER_ENTRY


def test_agent_order_matches_registration_order(client):
    """Order is user-visible -- it is the sidebar order.

    It comes from `orchestrator.agents` dict-insertion order, which is the
    argument order at app.py:25.
    """
    body = client.get("/api/agents").json()
    assert [entry["name"] for entry in body[1:]] == FULL_REGISTRY_NAMES_IN_ORDER


def test_entries_carry_only_name_and_description(client):
    """The synthetic Saarthi entry has only name and description.
    Real-agent entries carry the additive fields from Module 3.9.
    """
    body = client.get("/api/agents").json()
    assert set(body[0]) == {"name", "description"}
    additive = {
        "name", "description", "key", "agent_type", "capabilities",
        "status", "sort_order", "supports_options", "pin_session",
    }
    for entry in body[1:]:
        assert set(entry) == additive


def test_response_is_a_list_not_an_envelope(client):
    """The payload is a bare JSON array, not {"agents": [...]}.

    main.js iterates the response directly, so wrapping it would break the UI.
    """
    body = client.get("/api/agents").json()
    assert isinstance(body, list)
    assert len(body) == 4


def test_descriptions_are_non_empty(client):
    """Descriptions feed the router's classification prompt (orchestrator.py:33).

    An empty description would silently degrade routing quality, so pin that
    they are all populated.
    """
    body = client.get("/api/agents").json()
    for entry in body:
        assert entry["description"].strip()
