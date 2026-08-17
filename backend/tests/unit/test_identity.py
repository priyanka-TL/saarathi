import pytest
import jwt
import time

from app.core.settings import Settings
from app.services.identity import (
    context_from_token,
    default_context,
    Authenticator,
    InvalidTokenError,
    DEFAULT_ROLES,
    DEFAULT_TENANT_CODE,
    DEFAULT_USER_ID,
)

SAMPLE_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJkYXRhIjp7ImlkIjoxMzU1LCJuYW1lIjoiUHJpeWFua2EgUHJhZGVlcCIsInNlc3Npb25faWQiOjEyNTQ3LCJvcmdhbml6YXRpb25faWRzIjpbIjYyIl0sIm9yZ2FuaXphdGlvbl9jb2RlcyI6WyJzb3QiXSwidGVuYW50X2NvZGUiOiJzaGlrc2hhbG9rYW0iLCJvcmdhbml6YXRpb25zIjpbeyJpZCI6NjIsIm5hbWUiOiJTb1QiLCJjb2RlIjoic290IiwiZGVzY3JpcHRpb24iOiJTaGlrc2hhTG9rYW0gaXMgc3RyaXZpbmcgdG8gY3JlYXRlIGEgbmF0aW9uYWwgY29udmVyc2F0aW9uIGFib3V0IGVkdWNhdGlvbiBsZWFkZXJzaGlwLXRoZSB3aGF0LCB3aHkgYW5kIGhvdyBvZiBpdC4iLCJzdGF0dXMiOiJBQ1RJVkUiLCJyZWxhdGVkX29yZ3MiOltdLCJ0ZW5hbnRfY29kZSI6InNoaWtzaGFsb2thbSIsIm1ldGEiOm51bGwsImNyZWF0ZWRfYnkiOjEsInVwZGF0ZWRfYnkiOm51bGwsInJvbGVzIjpbeyJpZCI6MjMsInRpdGxlIjoibWVudGVlIiwibGFiZWwiOiJtZW50ZWUiLCJ1c2VyX3R5cGUiOjAsInN0YXR1cyI6IkFDVElWRSIsIm9yZ2FuaXphdGlvbl9pZCI6MTAsInZpc2liaWxpdHkiOiJQVUJMSUMiLCJ0ZW5hbnRfY29kZSI6InNoaWtzaGFsb2thbSIsInRyYW5zbGF0aW9ucyI6bnVsbH0seyJpZCI6NDIsInRpdGxlIjoiY3JlYXRvciIsImxhYmVsIjoiQ3JlYXRvciIsInVzZXJfdHlwZSI6MCwic3RhdHVzIjoiQUNUSVZFIiwib3JnYW5pemF0aW9uX2lkIjoxMCwidmlzaWJpbGl0eSI6IlBVQkxJQyIsInRlbmFudF9jb2RlIjoic2hpa3NoYWxva2FtIiwidHJhbnNsYXRpb25zIjpudWxsfSx7ImlkIjo0NiwidGl0bGUiOiJwcm9ncmFtX2Rlc2lnbmVyIiwibGFiZWwiOiJQcm9ncmFtIERlc2lnbmVyIiwidXNlcl90eXBlIjowLCJzdGF0dXMiOiJBQ1RJVkUiLCJvcmdhbml6YXRpb25faWQiOjEwLCJ2aXNpYmlsaXR5IjoiUFVCTElDIiwidGVuYW50X2NvZGUiOiJzaGlrc2hhbG9rYW0iLCJ0cmFuc2xhdGlvbnMiOm51bGx9XX1dfSwiaWF0IjoxNzg1MjQyNTY5LCJleHAiOjE3ODU4NDczNjl9.misOFt1cSN-smsRVy8-VzuBvbF9DO-VDVDKyEZhR6g0"

@pytest.fixture
def mock_settings():
    # Construct a Settings object using environment variables disabled so it's pure
    return Settings(
        OPENROUTER_API_KEY="test",
        auth_check=True,
    )

def test_context_from_token_derives_email_and_roles(mock_settings):
    # TRAP 1 & 3 test. verify=False (the default) -- this is testing the
    # decode/mapping logic itself, independent of trust level.
    ctx = context_from_token(SAMPLE_TOKEN, mock_settings)

    # Assert Trap 1: Email derivation
    assert ctx.user_id == "1355"
    assert ctx.email == "1355@shikshalokam.org"

    # Assert other basic fields
    assert ctx.display_name == "Priyanka Pradeep"
    assert ctx.tenant_code == "shikshalokam"
    assert ctx.active_org_id == "62"
    assert len(ctx.orgs) == 1

    org = ctx.orgs[0]
    assert org.org_id == "62"
    assert org.org_code == "sot"

    # Assert Trap 3: Roles from containing org
    assert "mentee" in org.roles
    assert "creator" in org.roles
    assert "program_designer" in org.roles

    # Assert the active roles property
    assert set(ctx.roles) == {"mentee", "creator", "program_designer"}

def test_context_from_token_roles_not_flattened(mock_settings):
    # If the user switches active_org_id to something else, they shouldn't see org 62's roles.
    ctx = context_from_token(SAMPLE_TOKEN, mock_settings)

    # Manually simulate a request where the active org is different or None
    # We use a dataclass replace equivalent (object.__setattr__ since frozen, or just mock)
    object.__setattr__(ctx, 'active_org_id', "999")

    assert len(ctx.roles) == 0


# ---------------------------------------------------------------------------
# AUTH_CHECK=true: a bearer token is REQUIRED, always -- no fallback identity
# for a request with none. One existed briefly (a boot-provisioned
# SAARTHI_STATIC_TOKEN, decoded unverified and served to any caller that sent
# nothing) and was removed: it was an authentication bypass, reachable by
# anyone regardless of whether they had ever logged in.
# ---------------------------------------------------------------------------

def test_auth_check_true_with_no_bearer_token_raises():
    settings = Settings(OPENROUTER_API_KEY="test", auth_check=True)

    with pytest.raises(InvalidTokenError, match="No bearer token"):
        Authenticator(settings).authenticate()

def test_auth_check_true_construction_never_requires_a_token_upfront():
    # Nothing is decoded at BOOT any more -- identity is resolved per request,
    # from whatever that request's own Authorization header carries. A
    # deployment with no bearer token yet configured anywhere must still
    # start; it will 401 real requests until one is actually sent, which is
    # the point.
    settings = Settings(OPENROUTER_API_KEY="test", auth_check=True)

    Authenticator(settings)  # must not raise


# ---------------------------------------------------------------------------
# Per-request bearer tokens: verified against ELEVATE_JWT_SECRET
# ---------------------------------------------------------------------------

def test_authenticate_returns_the_bearer_tokens_own_identity(mock_settings):
    mock_settings.elevate_jwt_secret = "shared-secret"
    bearer = jwt.encode({"data": {"id": "999", "tenant_code": "other"}}, "shared-secret", algorithm="HS256")

    ctx = Authenticator(mock_settings).authenticate(bearer)

    assert ctx.user_id == "999"
    assert ctx.tenant_code == "other"

def test_authenticate_rejects_a_bearer_token_with_the_wrong_signature(mock_settings):
    mock_settings.elevate_jwt_secret = "shared-secret"
    forged = jwt.encode({"data": {"id": "999"}}, "wrong-secret", algorithm="HS256")

    with pytest.raises(InvalidTokenError):
        Authenticator(mock_settings).authenticate(forged)

def test_authenticate_rejects_an_expired_bearer_token(mock_settings):
    # A per-request token's expiry IS enforced -- unlike the removed static
    # token (operator-provisioned, never attacker-reachable), this one comes
    # straight from the request.
    mock_settings.elevate_jwt_secret = "shared-secret"
    expired = jwt.encode(
        {"data": {"id": "999"}, "exp": int(time.time()) - 3600},
        "shared-secret",
        algorithm="HS256",
    )

    with pytest.raises(InvalidTokenError):
        Authenticator(mock_settings).authenticate(expired)

def test_authenticate_refuses_a_bearer_token_when_no_secret_is_configured(mock_settings):
    # The suite configures ELEVATE_JWT_SECRET globally (see conftest.py), so
    # this test explicitly blanks it on its own settings object -- a
    # per-request token cannot be trusted unverified, so it is refused rather
    # than silently accepted. There is no fallback identity to serve instead.
    mock_settings.elevate_jwt_secret = ""
    bearer = jwt.encode({"data": {"id": "999"}}, "anything", algorithm="HS256")

    with pytest.raises(InvalidTokenError, match="ELEVATE_JWT_SECRET"):
        Authenticator(mock_settings).authenticate(bearer)


# ---------------------------------------------------------------------------
# AUTH_CHECK=false: a separate, explicit switch -- not a per-request fallback
# ---------------------------------------------------------------------------

def test_auth_check_false_returns_hardcoded_identity():
    # No token at all, and none required.
    settings = Settings(OPENROUTER_API_KEY="test", auth_check=False)

    ctx = Authenticator(settings).authenticate()

    assert ctx.user_id == DEFAULT_USER_ID
    assert ctx.tenant_code == DEFAULT_TENANT_CODE
    assert ctx.roles == DEFAULT_ROLES
    # TRAP 1 holds in this mode too -- the email follows JWT_EMAIL_SUFFIX.
    assert ctx.email == "1355@shikshalokam.org"
    # There is no token to relay when auth is off.
    assert ctx.token is None

def test_auth_check_false_ignores_any_bearer_token(mock_settings):
    # Flipping auth_check off on an otherwise-normal settings object must
    # still serve the hardcoded identity -- the dev switch is unconditional,
    # not "unless a token happens to be configured."
    mock_settings.auth_check = False

    ctx = Authenticator(mock_settings).authenticate()

    assert ctx == default_context(mock_settings)
    assert ctx.token is None

def test_auth_check_false_grants_no_admin_role():
    # Turning auth off must not open the admin surface.
    settings = Settings(OPENROUTER_API_KEY="test", auth_check=False)

    assert "admin" not in Authenticator(settings).authenticate().roles
