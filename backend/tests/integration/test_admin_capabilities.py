"""The admin CRUD that makes "add a capability without a deployment" true.

The claim under test is end-to-end and specific: a write here changes what
GET /api/ui/capabilities answers on the very next request, with no restart, no
rebuild and no file edit.

READING THE DOCUMENT BACK, WITHOUT A SECOND HTTP IDENTITY. Identity is
resolved once from configuration, not per request (app/dependencies/identity.py
-- there is no login flow upstream of this API that could supply a
caller-specific token), so a second `TestClient` cannot be made to look like a
different tenant's browser the way it once could. `_document_for(tenant_code)`
calls `capability_service.resolve_for_user` directly instead -- the exact
function `GET /api/ui/capabilities` calls -- which is what actually proves
scope resolution differs by tenant. What changed is how a caller's tenant
reaches that function, not whether the function is still correct; see
tests/guards/test_tenant_isolation.py for the same pattern applied to agent
config.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.database.engine import SessionLocal
from app.domain.core import UserContext
from app.services.capability_service import resolve_for_user

TENANT = "admin_test_tenant"


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
def admin_client(flask_app, monkeypatch):
    """A client authenticated as an admin.

    Patches `Authenticator.authenticate()`, the single identity source now --
    see app/services/identity.py. The `Authorization` header sent below is
    inert (nothing reads it) but harmless to include.
    """
    from app.domain.core import OrgMembership

    flask_app.state.container.settings.saarthi_admin_enabled = 1
    admin_user = UserContext(
        user_id="capability_admin", email="admin@example.com", display_name="Admin",
        tenant_code="admin_home", orgs=(OrgMembership(org_id="o", org_code="o", roles=("admin",)),),
        active_org_id="o",
    )
    monkeypatch.setattr(
        "app.services.identity.Authenticator.authenticate", lambda self: admin_user
    )

    from starlette.testclient import TestClient

    return TestClient(
        flask_app, raise_server_exceptions=False,
        headers={"Authorization": "Bearer test-admin-token"},
    )


@pytest.fixture(autouse=True)
def _cleanup():
    """Delete everything this file creates, by tenant tag."""
    yield
    session = SessionLocal()
    session.execute(
        text("DELETE FROM capabilities WHERE tenant_id LIKE 'admin_test%'"),
    )
    session.execute(
        text("DELETE FROM capabilities WHERE key LIKE 'admintest_%'"),
    )
    session.commit()
    session.close()


def _key() -> str:
    return f"admintest_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# The headline claim
# ---------------------------------------------------------------------------


def test_a_created_capability_appears_in_the_public_document_immediately(admin_client):
    """No restart, no rebuild, no file edit -- the point of the whole change."""
    key = _key()
    created = admin_client.post("/api/admin/capabilities", json={
        "tenant_id": TENANT, "key": key, "name": "Brand New",
        "description": "made through the API", "icon": "brain", "display_order": 5,
        "metadata": {"action": {"type": "display_card"}},
    })
    assert created.status_code == 200, created.json()

    document = _document_for(TENANT)
    entry = next(c for c in document["capabilities"] if c["id"] == key)
    assert entry["title"] == "Brand New"
    assert entry["order"] == 5


def test_a_created_capability_is_invisible_to_other_tenants(admin_client):
    key = _key()
    admin_client.post("/api/admin/capabilities", json={
        "tenant_id": TENANT, "key": key, "name": "Private",
    })

    ids = [c["id"] for c in _document_for(f"t_{uuid.uuid4().hex[:8]}")["capabilities"]]
    assert key not in ids


def test_disabling_a_capability_hides_it_from_that_tenant_only(admin_client):
    """Shadowing the default row with a disabled one at tenant scope. This is
    the operation that a naive resolver gets backwards -- filtering disabled
    rows before resolving scope would fall back to the default and SHOW it."""
    admin_client.post("/api/admin/capabilities", json={
        "tenant_id": TENANT, "key": "sg_commons", "name": "SG Commons Portal",
        "status": "disabled",
    })

    mine = [c["id"] for c in _document_for(TENANT)["capabilities"]]
    theirs = [c["id"] for c in _document_for(f"t_{uuid.uuid4().hex[:8]}")["capabilities"]]

    assert "sg_commons" not in mine
    assert "sg_commons" in theirs


def test_deleting_a_tenant_row_reverts_that_tenant_to_the_default(admin_client):
    """The "undo my customisation" operation, and the reason overrides are rows
    rather than a copied catalogue."""
    admin_client.post("/api/admin/capabilities", json={
        "tenant_id": TENANT, "key": "listening_at_scale", "name": "Renamed Locally",
    })
    assert _title(TENANT, "listening_at_scale") == "Renamed Locally"

    deleted = admin_client.delete(
        f"/api/admin/capabilities/listening_at_scale?tenant_id={TENANT}"
    )
    assert deleted.status_code == 200
    assert _title(TENANT, "listening_at_scale") == "Listening at Scale"


def _title(tenant_code, key):
    document = _document_for(tenant_code)
    return next(c["title"] for c in document["capabilities"] if c["id"] == key)


# ---------------------------------------------------------------------------
# Update semantics
# ---------------------------------------------------------------------------


def test_patch_is_partial_and_leaves_other_fields_alone(admin_client):
    key = _key()
    admin_client.post("/api/admin/capabilities", json={
        "tenant_id": TENANT, "key": key, "name": "Original", "badge": "KEEP ME",
        "display_order": 42,
    })

    patched = admin_client.patch(
        f"/api/admin/capabilities/{key}?tenant_id={TENANT}", json={"name": "Renamed"}
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["name"] == "Renamed"
    assert body["badge"] == "KEEP ME"
    assert body["display_order"] == 42


def test_an_unknown_field_is_rejected_not_ignored(admin_client):
    """A silently dropped field looks like a successful edit and is discovered
    much later, in the UI."""
    key = _key()
    admin_client.post("/api/admin/capabilities", json={
        "tenant_id": TENANT, "key": key, "name": "Strict",
    })

    response = admin_client.patch(
        f"/api/admin/capabilities/{key}?tenant_id={TENANT}", json={"titel": "typo"}
    )
    assert response.status_code == 422
    assert response.json()["error"] == "CAPABILITY_INVALID"
    assert response.json()["path"] == ["titel"]


@pytest.mark.parametrize(
    "payload, path",
    [
        ({"name": "No key"}, ["key"]),
        ({"key": "valid_key"}, ["name"]),
        ({"key": "Bad Key!", "name": "n"}, ["key"]),
        ({"key": "valid_key2", "name": "n", "status": "nonsense"}, ["status"]),
    ],
    ids=["missing-key", "missing-name", "bad-slug", "bad-status"],
)
def test_create_validates_before_writing(admin_client, payload, path):
    response = admin_client.post("/api/admin/capabilities",
                                 json={"tenant_id": TENANT, **payload})
    assert response.status_code == 422
    assert response.json()["path"] == path


def test_creating_the_same_key_twice_in_one_scope_is_a_conflict(admin_client):
    key = _key()
    body = {"tenant_id": TENANT, "key": key, "name": "First"}
    assert admin_client.post("/api/admin/capabilities", json=body).status_code == 200
    assert admin_client.post("/api/admin/capabilities", json=body).status_code == 409


def test_the_same_key_is_fine_in_two_different_scopes(admin_client):
    """Which is the whole mechanism: a tenant row shadows the default row for
    the same key."""
    key = _key()
    assert admin_client.post("/api/admin/capabilities", json={
        "tenant_id": TENANT, "key": key, "name": "Tenant"}).status_code == 200
    assert admin_client.post("/api/admin/capabilities", json={
        "tenant_id": "admin_test_other", "key": key, "name": "Other"}).status_code == 200


def test_patching_a_missing_capability_is_404(admin_client):
    assert admin_client.patch(
        f"/api/admin/capabilities/{_key()}?tenant_id={TENANT}", json={"name": "x"}
    ).status_code == 404


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------


def test_membership_is_replaced_wholesale_and_ordered(admin_client):
    key = _key()
    admin_client.post("/api/admin/capabilities", json={
        "tenant_id": TENANT, "key": key, "name": "Grouped"})

    response = admin_client.put(
        f"/api/admin/capabilities/{key}/agents?tenant_id={TENANT}",
        json={"agents": [
            {"agent_key": "capture_discussion", "label_override": "Second", "display_order": 20},
            {"agent_key": "record_stories", "label_override": "First", "display_order": 10},
        ]},
    )
    assert response.status_code == 200, response.json()

    _enable_interview_agents()
    document = _document_for(TENANT)
    entry = next(c for c in document["capabilities"] if c["id"] == key)
    assert [a["label"] for a in entry["agents"]] == ["First", "Second"]


def test_an_unknown_agent_key_is_422_and_writes_nothing(admin_client):
    """Validated BEFORE anything is written -- a partial membership would leave
    the capability in a state the caller never asked for."""
    key = _key()
    admin_client.post("/api/admin/capabilities", json={
        "tenant_id": TENANT, "key": key, "name": "Grouped"})
    admin_client.put(
        f"/api/admin/capabilities/{key}/agents?tenant_id={TENANT}",
        json={"agents": [{"agent_key": "record_stories"}]},
    )

    response = admin_client.put(
        f"/api/admin/capabilities/{key}/agents?tenant_id={TENANT}",
        json={"agents": [
            {"agent_key": "record_stories"},
            {"agent_key": "no_such_agent"},
        ]},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "AGENT_NOT_FOUND"

    # The earlier membership survived: nothing was deleted before validating.
    session = SessionLocal()
    try:
        count = session.execute(
            text("""
                SELECT count(*) FROM capability_agents ca
                JOIN capabilities c ON c.id = ca.capability_id
                WHERE c.key = :key AND c.tenant_id = :tenant
            """),
            {"key": key, "tenant": TENANT},
        ).scalar()
    finally:
        session.close()
    assert count == 1


def test_membership_can_be_emptied(admin_client):
    key = _key()
    admin_client.post("/api/admin/capabilities", json={
        "tenant_id": TENANT, "key": key, "name": "Grouped"})
    admin_client.put(f"/api/admin/capabilities/{key}/agents?tenant_id={TENANT}",
                     json={"agents": [{"agent_key": "record_stories"}]})

    response = admin_client.put(
        f"/api/admin/capabilities/{key}/agents?tenant_id={TENANT}", json={"agents": []}
    )
    assert response.status_code == 200
    assert response.json()["agents"] == []


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_the_admin_surface_is_closed_to_non_admins(flask_app, client):
    """403 when the surface is on but the caller is not an admin. The
    non-admin identity here is the static-token user, which deliberately has no
    `admin` role."""
    flask_app.state.container.settings.saarthi_admin_enabled = 1
    assert client.get("/api/admin/capabilities").status_code == 403


def test_the_admin_surface_is_404_when_the_flag_is_off(flask_app, client):
    """404, not 403 -- a disabled admin surface masquerades as "this route does
    not exist" to everyone, admins included."""
    flask_app.state.container.settings.saarthi_admin_enabled = 0
    try:
        assert client.get("/api/admin/capabilities").status_code == 404
    finally:
        flask_app.state.container.settings.saarthi_admin_enabled = 1


def _enable_interview_agents():
    """record_stories / capture_discussion are remote_flow agents, which
    ConfigSyncService forces `disabled` when MITRA_ENABLED is off. See the
    matching fixture in test_ui_capabilities.py."""
    session = SessionLocal()
    session.execute(text(
        "UPDATE agents SET status = 'enabled' "
        "WHERE key IN ('record_stories', 'capture_discussion')"
    ))
    session.commit()
    session.close()
