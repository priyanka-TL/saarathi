"""GET /api/ui/capabilities -- the sidebar's presentation config.

This file is the SINGLE source for those cards -- the frontend keeps no bundled
copy -- so the document's content is now part of the product, not a hint. Hence
the assertions on what it actually contains.

The degradation path still matters, for a different reason than before: a
broken file must answer 404 with a logged warning rather than raising, because
a 500 tells an operator nothing and a silent empty document is
indistinguishable from a panel that is meant to be empty.
"""
from __future__ import annotations

import pytest


def test_serves_the_capability_document(client):
    response = client.get("/api/ui/capabilities")
    assert response.status_code == 200

    body = response.json()
    assert isinstance(body["capabilities"], list)

    ids = [c["id"] for c in body["capabilities"]]
    assert ids == ["listening_at_scale", "sg_commons"]


def test_listening_at_scale_groups_both_interview_agents(client):
    """The capability -> many agents shape the sidebar renders as one card."""
    body = client.get("/api/ui/capabilities").json()
    listening = body["capabilities"][0]

    assert [a["action"]["agentKey"] for a in listening["agents"]] == [
        "record_stories",
        "capture_discussion",
    ]
    # Every referenced key must name a real agent, or the button fails at click
    # time with no warning anywhere. This file is the only thing that connects
    # the two catalogues, so it is the only place to check.
    #
    # Checked against the agent YAML rather than against
    # `agent_registry.routable()`: the registry is Mitra-gated, so with
    # MITRA_ENABLED=0 both of these agents are legitimately absent from it and
    # only general_support remains. "Is this key defined?" is the invariant
    # here; "is it enabled right now?" is a deployment question.
    import yaml

    from app.core.bootstrap import AGENTS_YAML_DIR

    defined = {
        yaml.safe_load(path.read_text(encoding="utf-8"))["key"]
        for path in AGENTS_YAML_DIR.glob("*.yaml")
    }
    for agent in listening["agents"]:
        assert agent["action"]["agentKey"] in defined


def test_sg_commons_is_coming_soon_and_routes_nowhere(client):
    body = client.get("/api/ui/capabilities").json()
    sg_commons = body["capabilities"][1]

    assert sg_commons["status"] == "coming_soon"
    assert sg_commons["agents"] == []
    # NOT display_card: that would set the header banner, which counts as
    # navigating somewhere.
    assert sg_commons["action"]["type"] == "coming_soon"


@pytest.mark.parametrize(
    "content",
    [
        None,                       # file absent
        "",                         # empty
        "just a string",            # valid YAML, wrong shape
        "capabilities: not-a-list",
        "{{{ not yaml",             # unparseable
    ],
    ids=["absent", "empty", "scalar", "wrong-type", "unparseable"],
)
def test_a_broken_document_answers_404_instead_of_500(client, tmp_path, monkeypatch, content):
    from app.routers import ui

    if content is None:
        target = tmp_path / "does_not_exist.yaml"
    else:
        target = tmp_path / "capabilities.yaml"
        target.write_text(content, encoding="utf-8")

    monkeypatch.setattr(ui, "CAPABILITIES_YAML", target)

    response = client.get("/api/ui/capabilities")
    assert response.status_code == 404
    # The shared envelope, so every non-admin 4xx in this service looks alike.
    assert response.json()["error_code"] == "NOT_FOUND"


def test_the_route_takes_no_database_connection(client, monkeypatch):
    """It declares no `get_db`, and must keep declaring none.

    One request holds one thread and one DB connection for its whole lifetime
    (app/main.py), so a route that reads a small file has no business consuming
    either. Asserting on the dependency list rather than on behaviour, because
    adding `get_db` back would not change any response.
    """
    from app.dependencies.db import get_db

    route = next(
        r for r in client.app.routes if getattr(r, "path", None) == "/api/ui/capabilities"
    )
    assert get_db not in [d.call for d in route.dependant.dependencies]
