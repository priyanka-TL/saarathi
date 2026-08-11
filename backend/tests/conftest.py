"""Root test configuration.

ORDER IS LOAD-BEARING IN THIS FILE. Read the note below before editing.

``app.main`` bootstraps the container and the FastAPI app at module scope
(build_container + sync_and_reload run inside ``create_app()``, NOT in the
lifespan, precisely so that importing the module is enough to get a fully
booted app -- see app/main.py). Tests use a scripted LLM stub that must be
patched in **before any agent code is first imported**.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# 1. Environment, before anything reads it.
#
#    Settings is instantiated at import time (app/core/settings.py) and exits
#    the process when OPENROUTER_API_KEY is absent. app/core/settings.py calls
#    load_dotenv(override=False), so values set here win over the real .env
#    sitting next to this repo --- which is what keeps the suite from depending
#    on (or spending) a real credential.
# ---------------------------------------------------------------------------
os.environ["OPENROUTER_API_KEY"] = "test-key-never-used"
os.environ["OPENROUTER_MODEL"] = "test/scripted-model"
os.environ["LLM_TIMEOUT"] = "1"
os.environ["LLM_MAX_RETRIES"] = "1"
os.environ["LOG_LEVEL"] = "ERROR"
# The whole suite is written against the unprefixed default (`client.post
# ("/api/reset")`, not "/saarathi-service/api/reset"). A developer's local
# `.env` sets API_PREFIX for THEIR OWN dev server -- e.g. to match a frontend
# deployed under a path -- and without this override that value leaks into
# the test run and 404s nearly every characterisation test. Prefix behaviour
# itself is exercised by tests/integration/test_api_prefix.py, which builds
# its own prefixed app rather than relying on the ambient one.
os.environ["API_PREFIX"] = ""

# Same leak, higher stakes. A developer's `.env` sets VOICE_ENABLED=1 with REAL
# Bhashini credentials, and without this override build_container would create a
# live client for every test run -- so a bug in a fake could send a user's test
# audio to a government API. Voice-off is also the correct default state for the
# suite: tests/characterisation/test_voice_router.py pins the 503, and the ones
# that need it on swap fakes onto the container themselves.
os.environ["VOICE_ENABLED"] = "0"

# The same leak a third time, and now in ONE key rather than one per platform.
#
# `mitra` alone, deliberately, because that is exactly what the pinned
# characterisation fixtures were captured against: api_agents.json lists Record
# Stories and Capture Discussions but not the Saathi assistant. Enabling a
# second provider here would change `GET /api/agents` and fail a fixture that is
# NEVER edited to make a test pass. Observed, not hypothetical: turning the
# second platform on in .env once turned three characterisation tests red
# without touching a line of application code.
#
# A test that needs a different set builds its own registry or passes
# `enabled_providers` explicitly, so nothing depends on the ambient value -- and
# with the rest off, no test can open a socket to a live deployment.
os.environ["PROVIDERS_ENABLED"] = "mitra"

# CREDENTIALS THE SEEDED CONFIGS NAME. `remote.auth.credential_env` holds the
# NAME of a variable, not a value, so a config row is useless without the
# environment behind it -- and resolving a provider raises ProviderConfigError
# when the named variable is unset. These are junk values: the suite runs with
# `--disable-socket`, so nothing can be reached with them, and setting them here
# rather than relying on a developer's .env is what keeps the suite runnable on
# a clean checkout.
os.environ.setdefault("MITRA_ORIGIN_URL", "https://mitra.test.invalid")
os.environ.setdefault("SAATHI_ORIGIN_URL", "https://saathi.test.invalid")

# The variables tests/provider_factories.py names. Same reasoning; kept distinct
# from the two above so a factory-built spec can never accidentally resolve the
# same credential a seeded config does.
os.environ.setdefault("TEST_MITRA_ORIGIN_URL", "https://mitra.factory.invalid")
os.environ.setdefault("TEST_SAATHI_ORIGIN_URL", "https://saathi.factory.invalid")
os.environ.setdefault("TEST_SAATHI_EMAIL", "tester@example.invalid")
os.environ.setdefault("TEST_SAATHI_PASSWORD", "not-a-real-password")
os.environ.setdefault("TEST_SAATHI_ACCESS_TOKEN", "not-a-real-token")

# CLOUD_STORAGE_PROVIDER is not overridden: with voice off, build_container
# never constructs a store, so the developer's provider is never reached.

# ---------------------------------------------------------------------------
# 1b. DATABASE_URL --> A DEDICATED TEST DATABASE. NON-NEGOTIABLE.
#
#     The reset_state fixture below runs an UNSCOPED `DELETE FROM conversations`
#     before every test that touches the app. It has to: those tests act as the
#     static-token user, so leftover conversations for that identity would leak
#     between them. But `.env`'s DATABASE_URL points at the DEVELOPMENT database
#     -- the one the running app uses -- so `pytest` silently destroyed real
#     chat history, every run. That is how a user's conversations disappeared.
#
#     Redirecting here (rather than asking developers to remember an env var)
#     is what makes the destructive fixture safe by construction. Same override
#     trick as the keys above: os.environ beats settings' .env file, and this
#     runs before app.core.settings is first imported.
#
#     MUST stay above the `from app.llm.factory import LlmFactory` below --
#     that import instantiates Settings(), which snapshots the environment as
#     it is at that moment.
# ---------------------------------------------------------------------------
TEST_DB_SUFFIX = "_test"


def _derive_test_database_url() -> str:
    """Return the dev DATABASE_URL with `_test` appended to the database name."""
    from urllib.parse import urlparse, urlunparse

    from dotenv import dotenv_values

    dev_url = os.environ.get("DATABASE_URL") or dotenv_values(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    ).get("DATABASE_URL")

    if not dev_url:
        return ""

    parsed = urlparse(dev_url)
    name = parsed.path.lstrip("/")
    if name.endswith(TEST_DB_SUFFIX):
        return dev_url
    return urlunparse(parsed._replace(path=f"/{name}{TEST_DB_SUFFIX}"))


def _ensure_test_database(url: str) -> None:
    """CREATE DATABASE (if absent) and migrate it to head.

    Runs once per pytest process. Without this, redirecting the URL would just
    trade a data-loss bug for a "database does not exist" failure.
    """
    from urllib.parse import urlparse, urlunparse

    import sqlalchemy as sa

    parsed = urlparse(url)
    db_name = parsed.path.lstrip("/")
    admin_url = urlunparse(parsed._replace(path="/postgres"))

    admin_engine = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        exists = conn.execute(
            sa.text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": db_name}
        ).scalar()
        if not exists:
            # Identifier cannot be bound as a parameter; db_name is derived from
            # our own config, not from user input.
            conn.execute(sa.text(f'CREATE DATABASE "{db_name}"'))
    admin_engine.dispose()

    from alembic import command
    from alembic.config import Config

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(repo_root, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(repo_root, "migrations"))
    command.upgrade(cfg, "head")


_TEST_DATABASE_URL = _derive_test_database_url()
if _TEST_DATABASE_URL:
    os.environ["DATABASE_URL"] = _TEST_DATABASE_URL
    _ensure_test_database(_TEST_DATABASE_URL)

# ---------------------------------------------------------------------------
# 2. Install the LLM stub.
#
#    There is exactly ONE call site to patch: LlmFactory.get(spec)
#    (app/llm/factory.py), which HandlerFactory.build() resolves lazily
#    per-request. Patching the class method here -- at import time, well before
#    any request -- is therefore safe and order-independent.
#
#    This used to also patch a free function `app.llm.get_llm`, which had to be
#    imported at exactly the right moment (after the DATABASE_URL override
#    above, before any app.agents import) because importing it instantiated
#    Settings(). That function and its only caller (the legacy BaseAgent) are
#    both gone, and the ordering hazard went with them.
# ---------------------------------------------------------------------------
from tests.fakes import FakeDDGS, ScriptedChatModel  # noqa: E402

SHARED_MODEL = ScriptedChatModel()

from app.llm.factory import LlmFactory  # noqa: E402


def _fake_llm_factory_get(self, spec) -> ScriptedChatModel:
    return SHARED_MODEL


LlmFactory.get = _fake_llm_factory_get  # type: ignore[assignment]

import pytest  # noqa: E402


# ---------------------------------------------------------------------------
# 3. Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def app_module():
    """Import ``app.main`` and return the module.

    Session-scoped: importing it boots the container, runs config sync and
    loads the registry exactly once for the whole run, which is what the Flask
    suite got from ``import app``.
    """
    import app.main as app_mod

    return app_mod


@pytest.fixture()
def api_app(app_module):
    """The FastAPI application instance (as opposed to the module)."""
    return app_module.app


# Kept so the ~30 existing call sites don't have to churn. `api_app` is the
# name to use in new tests.
@pytest.fixture()
def flask_app(api_app):
    return api_app


@pytest.fixture()
def auth_headers():
    """An `Authorization` header, for tests that construct their own TestClient
    (because they need a differently-configured app) and want their intent to
    read clearly even though nothing in the app examines this header.

    Identity is resolved once from configuration, not from the request (see
    app/dependencies/identity.py) -- there is no login flow upstream of this
    API that could supply a caller-specific token. Sending this header is
    therefore inert, not required; it costs nothing to include and documents
    "this client represents an authenticated caller" at the call site.
    """
    from app.core.settings import settings

    if not settings.saarthi_static_token:
        return {}
    return {"Authorization": f"Bearer {settings.saarthi_static_token}"}


@pytest.fixture()
def client(api_app):
    """A test client for the API.

    ``raise_server_exceptions=False`` is required for parity: Starlette's
    TestClient re-raises unhandled exceptions by default, where Flask's test
    client returned the 500 response. Our routers map every expected failure
    themselves, so anything reaching this is a genuine regression -- and this
    flag keeps it visible as a failing assertion on a 500 body rather than as a
    traceback from inside the client.

    Sends the static token's `Authorization` header for documentation value
    only -- see `auth_headers`. `anonymous_client` below sends none at all and
    resolves identically, which is itself the thing worth testing (see
    tests/integration/test_identity_per_request.py).
    """
    from starlette.testclient import TestClient

    from app.core.settings import settings

    headers = {}
    if settings.saarthi_static_token:
        headers["Authorization"] = f"Bearer {settings.saarthi_static_token}"

    return TestClient(api_app, raise_server_exceptions=False, headers=headers)


@pytest.fixture()
def anonymous_client(api_app):
    """A client that sends no credential.

    Resolves to the SAME identity `client` does -- there is no per-caller
    identity for it to be missing. Exists to make that equivalence an explicit
    test fixture rather than something asserted ad hoc.
    """
    from starlette.testclient import TestClient

    return TestClient(api_app, raise_server_exceptions=False)


@pytest.fixture()
def script():
    """The shared scripted model, cleared before and after each test."""
    SHARED_MODEL.reset()
    yield SHARED_MODEL
    SHARED_MODEL.reset()


@pytest.fixture(autouse=True)
def reset_globals(request):
    """
    Process globals are gone. This fixture ensures the DB is wiped and the
    handler cache is cleared between tests so test order does not affect results.
    """
    if (
        "app_module" in request.fixturenames
        or "api_app" in request.fixturenames
        or "flask_app" in request.fixturenames
        or "client" in request.fixturenames
    ):
        # HandlerFactory caches LlmAgentHandler instances per (key, checksum)
        # across requests -- correct production behaviour (tool bindings
        # shouldn't be rebuilt every turn), but it means bind_tools() is only
        # ever called once per agent for the life of the (session-scoped)
        # container, not once per test. Clear it so tests that inspect
        # script.bound_tools see a fresh call every time, same as the old
        # per-turn bind_tools() call in src/agents/base.py did.
        import app.main as app_mod
        container = getattr(app_mod.app.state, "container", None)
        if container is not None:
            container.handler_factory._cache.clear()

        # In postgres mode, we must completely wipe the DB state to ensure test isolation
        # because the static user would otherwise pick up stale conversations from prior tests.
        from app.core.settings import settings
        from app.database.engine import SessionLocal
        from sqlalchemy import text

        # LAST LINE OF DEFENCE. This DELETE is unscoped, so pointing it at a
        # real database wipes real chat history -- which is exactly what
        # happened before section 1b redirected DATABASE_URL. Assert the target
        # instead of trusting it: a misconfigured URL must fail the suite, never
        # quietly destroy data.
        db_name = (settings.database_url or "").rsplit("/", 1)[-1].split("?")[0]
        assert db_name.endswith(TEST_DB_SUFFIX), (
            f"refusing to DELETE FROM conversations in database {db_name!r} -- "
            f"the test suite must run against a *{TEST_DB_SUFFIX} database. "
            "Check tests/conftest.py section 1b."
        )

        with SessionLocal() as db_session:
            db_session.execute(text("DELETE FROM conversation_messages;"))
            db_session.execute(text("DELETE FROM conversations;"))
            db_session.commit()
            
        # Also call /api/reset to clear any memory state just in case, though
        # we mainly rely on the DB delete.
        client = request.getfixturevalue("client")
        client.post("/api/reset")
        
        yield
    else:
        yield


@pytest.fixture()
def fake_ddgs(monkeypatch):
    """Replace the search provider inside ``app.tools.search_tools``.

    ``src/tools/search_tools.py:2`` does ``from ddgs import DDGS`` into ITS OWN
    module namespace, so the name to patch is ``app.tools.search_tools.DDGS``
    --- patching ``app.tools.DDGS`` (the package, not the submodule) or
    ``ddgs.DDGS`` would be too late/wrong target.
    """
    import app.tools.search_tools

    FakeDDGS.reset()
    monkeypatch.setattr(app.tools.search_tools, "DDGS", FakeDDGS)
    yield FakeDDGS
    FakeDDGS.reset()
