"""GET /api/ui/capabilities -- the sidebar's capability document.

This is the single source for those cards: the frontend keeps no bundled copy,
so what this route answers IS the sidebar. Two things are pinned here.

1. THE DOCUMENT'S CONTENT, because it is now product rather than a hint.

2. THE SCOPE RULE the resolver applies for whatever tenant a caller resolves
   to:

       (tenant, org)  >  (tenant, 'default')  >  ('default', 'default')

   A tenant with no rows of its own inherits the default catalogue -- so
   onboarding costs zero writes -- and a tenant's own row shadows the default
   for that tenant ONLY.

READING THE DOCUMENT FOR A SPECIFIC TENANT. Identity is resolved once from
configuration, not per request (app/dependencies/identity.py -- there is no
login flow upstream of this API that could supply a caller-specific token), so
an HTTP client cannot be made to look like a different tenant's browser. The
scope-resolution tests below call `resolve_for_user` directly instead -- the
exact function this route calls -- which is what actually proves the
precedence rule, independent of how a caller's tenant reaches it.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from app.database.engine import SessionLocal
from app.domain.core import UserContext
from app.services.capability_service import resolve_for_user


def _document_for(tenant_code: str, *, org_id: str = "default") -> dict:
    """What a caller with this tenant_code (and, optionally, org) sees."""
    session = SessionLocal()
    try:
        user = UserContext(
            user_id="fixture", email="fixture@example.com", display_name="Fixture",
            tenant_code=tenant_code, active_org_id=org_id,
        )
        return resolve_for_user(session, user)
    finally:
        session.close()


@pytest.fixture()
def scoped():
    """Insert scoped capability rows, and remove exactly them afterwards.

    Every row created here is tagged with a unique tenant code, so this file
    can never disturb the 'default'-scope rows the migration seeded (which are
    what every other test in the suite sees).
    """
    created_tenants: list[str] = []
    session = SessionLocal()

    def _capability(*, tenant_id, key, name="Scoped", status="active",
                    display_order=50, organization_id="default", metadata=None):
        created_tenants.append(tenant_id)
        session.execute(
            text("""
                INSERT INTO capabilities (tenant_id, organization_id, key, name,
                                          description, icon, status, display_order, metadata)
                VALUES (:tenant_id, :organization_id, :key, :name, 'desc', 'brain',
                        CAST(:status AS capability_status_enum), :display_order,
                        CAST(:metadata AS jsonb))
                RETURNING id
            """),
            {"tenant_id": tenant_id, "organization_id": organization_id, "key": key,
             "name": name, "status": status, "display_order": display_order,
             "metadata": json.dumps(metadata or {"action": {"type": "display_card"}})},
        )
        session.commit()

    yield _capability

    for tenant_id in set(created_tenants):
        session.execute(text("DELETE FROM capabilities WHERE tenant_id = :t"), {"t": tenant_id})
    session.commit()
    session.close()


@pytest.fixture()
def interview_agents_enabled():
    """Force the two Mitra interview agents `enabled`, and restore after.

    NOT redundant. `record_stories` and `capture_discussion` are `remote_flow`
    agents, and ConfigSyncService forces every remote_flow agent to `disabled`
    when MITRA_ENABLED is off -- deliberately, so the UI never lists an agent
    nothing can serve. tests/integration/test_config_sync_mitra_gating.py
    exercises exactly that and leaves them disabled, and it sorts before this
    file, so without this fixture the membership assertions below fail on a
    full-suite run and pass in isolation.

    Mirrors the snapshot/restore pattern
    test_config_versioning_lifecycle.py::_protect_real_agent_statuses already
    uses for the same class of cross-file leakage.
    """
    keys = ("record_stories", "capture_discussion")
    session = SessionLocal()
    before = {
        row[0]: row[1]
        for row in session.execute(
            text("SELECT key, status FROM agents WHERE key = ANY(:keys)"), {"keys": list(keys)}
        ).fetchall()
    }
    session.execute(
        text("UPDATE agents SET status = 'enabled' WHERE key = ANY(:keys)"),
        {"keys": list(keys)},
    )
    session.commit()

    yield

    for key, status in before.items():
        session.execute(
            text("UPDATE agents SET status = CAST(:status AS agent_status_enum) WHERE key = :key"),
            {"status": status, "key": key},
        )
    session.commit()
    session.close()


def _doc(client):
    response = client.get("/api/ui/capabilities")
    assert response.status_code == 200
    return response.json()


def _ids(document):
    return [c["id"] for c in document["capabilities"]]


def _by_id(document, key):
    """Look a capability up by key, never by position.

    Ordering is itself configurable -- a tenant that reorders its catalogue
    moves the index of everything -- so indexing into the list would make
    unrelated tests fail for the wrong reason.
    """
    return next(c for c in document["capabilities"] if c["id"] == key)


# ---------------------------------------------------------------------------
# The default catalogue
# ---------------------------------------------------------------------------


def test_serves_the_seeded_catalogue(client):
    """The default-scope catalogue, in display order.

    A PIN, not a golden fixture: seeding a new capability is expected to fail
    this once, and the list is then updated deliberately.

    `saathi_assistant` is ABSENT here because the suite runs with
    SAATHI_ENABLED unset: its only agent is filtered out by the provider gate,
    and an active card with no agents is hidden rather than rendered as a dead
    end. test_an_active_card_with_no_agents_is_hidden covers that directly.
    """
    body = _doc(client)
    assert body["version"] == 1
    assert _ids(body) == ["listening_at_scale", "sg_commons"]


def test_listening_at_scale_groups_both_interview_agents(client, interview_agents_enabled):
    """The capability -> many agents shape the sidebar renders as one card."""
    listening = _by_id(_doc(client), "listening_at_scale")

    assert [a["action"]["agentKey"] for a in listening["agents"]] == [
        "record_stories",
        "capture_discussion",
    ]
    assert listening["badge"] == "SHIKSHALOKAM"
    # `agentKey` is injected from the join, never read out of stored JSON, so a
    # renamed agent key cannot leave a stale copy behind.
    for agent in listening["agents"]:
        assert agent["action"]["type"] == "start_agent"
        assert agent["action"]["autostart"]


def test_sg_commons_is_coming_soon_and_routes_nowhere(client):
    sg_commons = _by_id(_doc(client), "sg_commons")

    assert sg_commons["status"] == "coming_soon"
    assert sg_commons["agents"] == []
    # NOT display_card: that would set the header banner, which counts as
    # navigating somewhere.
    assert sg_commons["action"]["type"] == "coming_soon"


def test_membership_can_only_name_real_agents(client, interview_agents_enabled):
    """capability_agents holds a real FK to agents, so a dangling reference is
    unrepresentable. This asserts the resulting guarantee end to end."""
    session = SessionLocal()
    try:
        defined = {
            row[0] for row in session.execute(text("SELECT key FROM agents")).fetchall()
        }
    finally:
        session.close()

    for capability in _doc(client)["capabilities"]:
        for agent in capability["agents"]:
            assert agent["action"]["agentKey"] in defined


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------


def test_a_tenant_with_no_rows_inherits_the_default_catalogue():
    """Onboarding a tenant costs zero writes -- the headline property of the
    default-scope model.

    Includes `saathi_assistant`, unlike the client-based test above:
    `_document_for` calls resolve_for_user directly, whose provider gates
    DEFAULT to enabled, where the HTTP route passes the real settings (and the
    suite runs with SAATHI_ENABLED unset).
    """
    newcomer = _document_for(f"t_{uuid.uuid4().hex[:8]}")
    assert _ids(newcomer) == ["listening_at_scale", "saathi_assistant", "sg_commons"]


def test_a_tenant_row_shadows_the_default_for_that_tenant_only(scoped):
    tenant = f"t_{uuid.uuid4().hex[:8]}"
    scoped(tenant_id=tenant, key="listening_at_scale", name="Listening, Rebranded")

    mine = _document_for(tenant)
    theirs = _document_for(f"t_{uuid.uuid4().hex[:8]}")

    assert _by_id(mine, "listening_at_scale")["title"] == "Listening, Rebranded"
    # The other tenant is untouched -- this is the assertion that would catch a
    # missing scope filter, which is the failure mode that leaks config across
    # tenants.
    assert _by_id(theirs, "listening_at_scale")["title"] == "Listening at Scale"


def test_a_tenant_can_disable_a_capability_for_itself(scoped):
    tenant = f"t_{uuid.uuid4().hex[:8]}"
    scoped(tenant_id=tenant, key="sg_commons", status="disabled")

    mine = _document_for(tenant)
    theirs = _document_for(f"t_{uuid.uuid4().hex[:8]}")

    assert "sg_commons" not in _ids(mine)
    assert "sg_commons" in _ids(theirs)


def test_a_tenant_can_add_a_capability_nobody_else_sees(scoped):
    tenant = f"t_{uuid.uuid4().hex[:8]}"
    scoped(tenant_id=tenant, key="tenant_only", name="Tenant Only")

    mine = _document_for(tenant)
    theirs = _document_for(f"t_{uuid.uuid4().hex[:8]}")

    assert "tenant_only" in _ids(mine)
    assert "tenant_only" not in _ids(theirs)


def test_a_tenant_can_reorder_its_own_catalogue(scoped):
    tenant = f"t_{uuid.uuid4().hex[:8]}"
    # sg_commons seeds at display_order 20, behind listening_at_scale's 10.
    scoped(tenant_id=tenant, key="sg_commons", name="SG Commons Portal",
           status="coming_soon", display_order=1)

    mine = _document_for(tenant)
    assert _ids(mine)[0] == "sg_commons"


def test_an_organization_row_beats_a_tenant_row(scoped):
    """The third level of the precedence rule -- the one an ORDER BY with the
    CASE expressions in the wrong order would silently get backwards."""
    tenant = f"t_{uuid.uuid4().hex[:8]}"
    scoped(tenant_id=tenant, key="listening_at_scale", name="Tenant Wide")
    scoped(tenant_id=tenant, organization_id="62", key="listening_at_scale",
           name="Org Specific")

    in_org = _document_for(tenant, org_id="62")
    other_org = _document_for(tenant, org_id="77")

    assert _by_id(in_org, "listening_at_scale")["title"] == "Org Specific"
    # A different org in the same tenant falls back to the tenant-wide row,
    # not to the global default.
    assert _by_id(other_org, "listening_at_scale")["title"] == "Tenant Wide"


# ---------------------------------------------------------------------------
# Failure shapes
# ---------------------------------------------------------------------------


def test_an_empty_catalogue_is_200_with_an_empty_list_not_404(client, monkeypatch):
    """"This tenant has no capabilities" is a real answer. A 404 would claim
    the route does not exist, making a misconfigured deployment
    indistinguishable from a deliberately empty one."""
    from app.services import capability_service

    monkeypatch.setattr(
        capability_service, "resolve_for_user",
        lambda session, user, **kw: {"version": 1, "capabilities": []},
    )
    monkeypatch.setattr(
        "app.routers.ui.resolve_for_user",
        lambda session, user, **kw: {"version": 1, "capabilities": []},
    )

    response = client.get("/api/ui/capabilities")
    assert response.status_code == 200
    assert response.json()["capabilities"] == []


def test_the_route_needs_no_credential(anonymous_client):
    """Identity comes from configuration, not the request (see
    app/dependencies/identity.py) -- a caller that sends no `Authorization`
    header at all still gets 200, because there is nothing about the request
    the resolver ever reads. This is the frontend's actual traffic pattern: it
    has no login flow and sends no such header, ever."""
    response = anonymous_client.get("/api/ui/capabilities")
    assert response.status_code == 200
    assert response.json()["capabilities"]


# ---------------------------------------------------------------------------
# Empty cards
# ---------------------------------------------------------------------------


def test_an_active_card_with_no_agents_is_hidden():
    """A card whose agents were filtered out advertises a dead end.

    The provider gate drops an agent when its provider is disabled, for a
    reason the user cannot see. Leaving the card behind renders a heading and a
    description with nothing to click -- the same mistake as listing an agent
    the router would refuse.

    Reproduced with SAATHI_ENABLED off, which is the case that surfaced it.
    """
    session = SessionLocal()
    try:
        shown = resolve_for_user(session, None, enabled_providers=frozenset({"mitra", "saathi"}))
        hidden = resolve_for_user(session, None, enabled_providers=frozenset({"mitra"}))
    finally:
        session.close()

    assert "saathi_assistant" in [c["id"] for c in shown["capabilities"]]
    assert "saathi_assistant" not in [c["id"] for c in hidden["capabilities"]]


def test_a_coming_soon_card_with_no_agents_still_shows():
    """SG Commons Portal has no agents BY DESIGN and says so with a badge, so
    it explains itself rather than dead-ending. The exemption is what keeps the
    rule above from hiding it."""
    session = SessionLocal()
    try:
        doc = resolve_for_user(session, None, enabled_providers=frozenset({"mitra", "saathi"}))
    finally:
        session.close()

    sg = next(c for c in doc["capabilities"] if c["id"] == "sg_commons")
    assert sg["agents"] == []
    assert sg["status"] == "coming_soon"


def test_disabling_mitra_hides_the_interview_card_too():
    """The rule is not Saathi-specific: listening_at_scale had the same latent
    dead end whenever MITRA_ENABLED was off."""
    session = SessionLocal()
    try:
        doc = resolve_for_user(session, None, enabled_providers=frozenset({"saathi"}))
    finally:
        session.close()

    assert "listening_at_scale" not in [c["id"] for c in doc["capabilities"]]


# ---------------------------------------------------------------------------
# Self-launching cards
# ---------------------------------------------------------------------------


def test_a_self_launching_card_carries_an_agent_key_and_no_nested_agents():
    """The Saathi card IS the control, so it needs no button beside it.

    `agentKey` on the CARD is what makes it routable: the frontend normalises
    `start_agent` without one to an inert `none`, which is why the card was
    dead and its nested button did the routing. With the key present, a nested
    button would be a second control showing the same name twice.
    """
    session = SessionLocal()
    try:
        doc = resolve_for_user(session, None, enabled_providers=frozenset({"mitra", "saathi"}))
    finally:
        session.close()

    card = next(c for c in doc["capabilities"] if c["id"] == "saathi_assistant")

    assert card["action"] == {"type": "start_agent", "agentKey": "saathi"}
    assert card["agents"] == []


def test_suppressing_the_nested_agent_does_not_disable_the_provider_gate():
    """THE SUBTLE ONE.

    The membership row is never rendered, but it is NOT dead data: the gate
    reads it to tell "no agents configured" (legitimate, shown) from "every
    agent filtered out" (a dead end, hidden). Deleting the row as unused would
    make the card render, and fail on click, whenever SAATHI_ENABLED=0.
    """
    session = SessionLocal()
    try:
        on = resolve_for_user(session, None, enabled_providers=frozenset({"mitra", "saathi"}))
        off = resolve_for_user(session, None, enabled_providers=frozenset({"mitra"}))
    finally:
        session.close()

    assert "saathi_assistant" in [c["id"] for c in on["capabilities"]]
    assert "saathi_assistant" not in [c["id"] for c in off["capabilities"]]


def test_a_grouping_card_keeps_its_nested_agents():
    """Listening at Scale is display-only, so its buttons ARE the controls.
    The suppression must apply only to cards that route themselves."""
    session = SessionLocal()
    try:
        doc = resolve_for_user(session, None, enabled_providers=frozenset({"mitra", "saathi"}))
    finally:
        session.close()

    card = next(c for c in doc["capabilities"] if c["id"] == "listening_at_scale")

    assert card["action"]["type"] == "display_card"
    assert [a["id"] for a in card["agents"]] == ["record_stories", "capture_discussion"]
