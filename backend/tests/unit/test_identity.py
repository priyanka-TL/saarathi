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
        saarthi_static_token=SAMPLE_TOKEN
    )

def test_context_from_token_derives_email_and_roles(mock_settings):
    # TRAP 1 & 3 test
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

def test_expired_token_is_accepted():
    # Saarthi has already validated the token. Expiry is NOT re-checked here:
    # rejecting it would refuse a request Saarthi accepted, and (because the
    # Authenticator resolves at construction) would take the process down.
    expired_payload = {
        "data": {"id": 1},
        "exp": int(time.time()) - 3600
    }
    expired_token = jwt.encode(expired_payload, "secret", algorithm="HS256")

    settings = Settings(
        OPENROUTER_API_KEY="test",
        auth_check=True,
        saarthi_static_token=expired_token
    )

    assert Authenticator(settings).authenticate().user_id == "1"

def test_signature_is_not_verified():
    # No secret is configured anywhere; a token signed with an unknown key must
    # still decode. Same reason as above -- Saarthi is the only validator.
    token = jwt.encode({"data": {"id": 7, "tenant_code": "t"}}, "some-other-secret", algorithm="HS256")

    settings = Settings(OPENROUTER_API_KEY="test", auth_check=True, saarthi_static_token=token)

    ctx = Authenticator(settings).authenticate()
    assert ctx.user_id == "7"
    assert ctx.tenant_code == "t"

def test_auth_check_true_uses_env_token(mock_settings):
    # The identity comes from SAARTHI_STATIC_TOKEN, and the raw token is kept
    # on the context (OrchestrationService relays it to Mitra's finalize).
    ctx = Authenticator(mock_settings).authenticate()

    assert ctx.user_id == "1355"
    assert ctx.email == "1355@shikshalokam.org"
    assert ctx.tenant_code == "shikshalokam"
    assert ctx.token == SAMPLE_TOKEN

def test_auth_check_true_requires_token():
    settings = Settings(OPENROUTER_API_KEY="test", auth_check=True, saarthi_static_token=None)

    with pytest.raises(ValueError, match="SAARTHI_STATIC_TOKEN is required"):
        Authenticator(settings)

def test_auth_check_true_rejects_undecodable_token():
    settings = Settings(OPENROUTER_API_KEY="test", auth_check=True, saarthi_static_token="not-a-jwt")

    with pytest.raises(InvalidTokenError):
        Authenticator(settings)

def test_auth_check_false_returns_hardcoded_identity():
    # No token at all, and none required.
    settings = Settings(OPENROUTER_API_KEY="test", auth_check=False, saarthi_static_token=None)

    ctx = Authenticator(settings).authenticate()

    assert ctx.user_id == DEFAULT_USER_ID
    assert ctx.tenant_code == DEFAULT_TENANT_CODE
    assert ctx.roles == DEFAULT_ROLES
    # TRAP 1 holds in this mode too -- the email follows JWT_EMAIL_SUFFIX.
    assert ctx.email == "1355@shikshalokam.org"
    # There is no token to relay when auth is off.
    assert ctx.token is None

def test_auth_check_false_ignores_any_configured_token(mock_settings):
    # A leftover SAARTHI_STATIC_TOKEN must not leak back in once auth is off.
    mock_settings.auth_check = False

    ctx = Authenticator(mock_settings).authenticate()

    assert ctx == default_context(mock_settings)
    assert ctx.token is None

def test_auth_check_false_grants_no_admin_role():
    # Turning auth off must not open the admin surface.
    settings = Settings(OPENROUTER_API_KEY="test", auth_check=False)

    assert "admin" not in Authenticator(settings).authenticate().roles
