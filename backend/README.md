# Saarthi backend (FastAPI, Python 3.10)

## Setup

```bash
make install                 # uv venv (py3.10) + uv pip install -r requirements.txt
cp .env.example .env         # ONE config file — fill OPENROUTER_API_KEY, SAARTHI_STATIC_TOKEN, MITRA_*
createdb saarthi_new         # local Postgres 17
make run                     # migrates to head, then binds HOST:PORT from .env
```

On a **fresh** database also set `CONFIG_SYNC_MODE=safe` for that first run, so
the agents are seeded from `app/config/agents/*.yaml`; then put it back to
`off`. Booting an empty database with `off` fails fast rather than coming up
silently empty. See [Configuration](#configuration).

Everything configurable — host, port, `API_PREFIX`, CORS origins, feature
flags, Mitra URLs and paths — lives in that one `.env`. See
[Configuration](#configuration).

Interactive API docs at `<host>:<port>{API_PREFIX}/docs` (OpenAPI 3.1) —
`http://127.0.0.1:8000/docs` with the shipped defaults.

| Command | Does |
|---|---|
| `make run` | **migrates first**, then `python -m app.main`; host/port/workers from settings, **single worker** (mandatory when `MITRA_ENABLED=1`) |
| `make run-no-migrate` | starts without touching the schema — for when applying migrations is not this process's job |
| `make test` | the full test suite |
| `make lint` | import-linter: the four layering contracts |
| `make verify-copy` | Gate A — proves the copied layers were not rewritten |
| `make parity` | Gate C — diffs live responses against the Flask app |
| `make migrate` / `make migrate-down` | alembic up / down one |
| `make db-shell` | psql against the resolved `DATABASE_URL` |

Any setting can be overridden for one invocation, because a real environment
variable outranks the file:

```bash
PORT=9000 make run
API_PREFIX=/saarathi-service make run
APP_ENV=qa MITRA_ENABLED=0 make run
```

## Layout

```
app/
├── main.py            create_app() + lifespan + middleware + routers
├── api/router.py      aggregates the seven routers IN A FIXED ORDER
├── routers/           chat · conversations · agents · sessions · ui
│                      · admin_config · admin
├── dependencies/      db · identity · container · request_context · admin_gate · body
├── middleware/        request_id (pure ASGI, not BaseHTTPMiddleware)
├── exceptions/        envelope (the standard error body) + handlers
├── schemas/           request bodies only -- never responses
├── utils/             responses (json_response, parse_uuid) · serializers
├── core/              settings · logger · context · container · bootstrap
│                      · runtime (startup guards) · concurrency (threadpool)
├── config/agents/     the three agent YAMLs -- a SEED for a fresh database
│                      (CONFIG_SYNC_MODE defaults to `off`); a bad one still
│                      ABORTS STARTUP when sync is on
├── database/engine.py · models/orm.py
└── domain/ repositories/ services/ agents/ integrations/mitra/ llm/ tools/
                        <- copied verbatim from the Flask app
```

`domain`, `agents`, `integrations`, `llm` and `tools` stay top-level rather
than folding into `services/` or `models/` because the `.importlinter`
contracts are `forbidden` contracts over module *trees*, and `app.domain` is
declared pure — nesting it under `app.models` (which holds the ORM classes it
must not import) would make the contract read as a self-exclusion.

## The rules that keep this correct

Each is enforced by a test in `tests/guards/`.

1. **No `async def` endpoints or dependencies.** Starlette runs `def`
   endpoints in a worker thread, which the advisory lock, `threading.Lock`,
   `queue.Queue`, `requests`, the sync SQLAlchemy engine and the LLM client
   (whose `_agenerate` raises `NotImplementedError`) all require. The only
   exceptions are the two body-reading dependencies in `dependencies/body.py`,
   which do no blocking work.

2. **No `response_model`, anywhere.** It silently drops keys and injects nulls.
   Several responses cannot survive that: `GET /api/agents`' first entry has no
   `key`, `/resume` returns three incompatible key sets, `/report`'s 202 has no
   envelope, and `messages[].options` is passed through raw. Every route
   returns a `JSONResponse`, which FastAPI leaves untouched.
   *Corollary:* nothing non-JSON-native may reach a `JSONResponse` — `str()`
   every UUID and `.isoformat()` every datetime, as the routers already do.

3. **Mapped errors `return`, never `raise`.** Flask's teardown committed
   whenever no exception escaped, including on a *returned* error tuple. A
   raise would propagate through the `get_db` yield-dependency and roll back
   instead. Only genuinely unexpected exceptions should raise.

4. **`THREADPOOL_SIZE` ≤ `DB_POOL_SIZE`.** One request = one thread = one
   connection for the whole turn. anyio's default is 40 against a 24-connection
   pool, which turns overload into `QueuePool` timeouts surfaced as 500s.

5. **One uvicorn worker when `MITRA_ENABLED=1`.** `MitraSessionManager` pools
   live WebSockets in process memory; a second worker opens a second Mitra
   channel for the same interview. Scale with `THREADPOOL_SIZE`.

6. **`sync_and_reload` runs in `create_app()`, not the lifespan.** A
   misconfigured agent YAML must abort at import, exactly as under Flask, and
   `TestClient(app)` must get a fully-booted app without a `with` block.

7. **Two error envelopes, kept apart.** Everything except admin uses
   `{status, error, error_code, request_id}`. The admin router answers with a
   bare `{"error": CODE}`. Do not unify them; the tests pin both.

## Tests

```
tests/unit/            242   no DB
tests/integration/     143   real Postgres
tests/characterisation/ 99   TestClient end-to-end, golden JSON fixtures
tests/guards/           20   the rules above, made executable
                       ---
                       504
```

Two of the guards are worth knowing by name:

* `test_tenant_isolation.py` — different tenants' agent configs must not share
  a `HandlerFactory` entry, and identical ones must. This is what stands
  between the schema and one tenant's system prompt answering another tenant's
  user.
* `test_sync_contract.py` — the full route surface, pinned. Adding a route is
  a deliberate edit there, with a reason.

`tests/conftest.py` is **order-critical**: env vars → redirect `DATABASE_URL`
to `<db>_test` (auto-created and migrated) → stub `LlmFactory.get` with a
scripted fake **before any agent module is imported** → then import the app.
The `assert db_name.endswith("_test")` in `reset_globals` is the last line of
defence in front of an unscoped `DELETE FROM conversations`.

`pytest --disable-socket --allow-unix-socket`: the unix-socket allowance is
required, not a loosening — asyncio's event loop builds its self-pipe with
`socket.socketpair()`, so bare `--disable-socket` prevents Starlette's
TestClient from starting at all. INET sockets stay blocked, which is what keeps
the suite off OpenRouter and Mitra.

## Configuration

Nothing is hardcoded — no host, no port, no URL, no path prefix — and there is
exactly **one** config file. A deployment differs by the values in it, or by
injected environment variables; there is no second file to keep in sync.

**Precedence, highest first:**

| | Source | Holds |
|---|---|---|
| 1 | real environment variables | container / CI injection, one-off overrides |
| 2 | `backend/.env` (git-ignored) | everything: host, port, `API_PREFIX`, CORS, flags, Mitra URLs + paths, and the secrets |
| 3 | field defaults in `app/core/settings.py` | the development shape |

`.env` is resolved against the `backend/` directory rather than the CWD, so the
same configuration is found whatever directory you launch from. `.env.example`
is the full template. `APP_ENV` is a **label** only — it appears in the startup
log so a running process can say which deployment it believes it is, and
selects no file.

**`API_PREFIX`** mounts the whole service under a path — `API_PREFIX=/saarathi-service`
makes the chat endpoint `POST /saarathi-service/api/chat`, and moves `/healthz`,
`/docs` and `/openapi.json` with it. The app serves this itself, so it is
directly curl-testable and a proxy must *forward* the prefix, not strip it.
It is applied in exactly one place (`app.include_router(api_router, prefix=...)`
in `main.py`); the route decorators keep their absolute `/api/...` paths. The
one other place that must know about it is `dependencies/db.py`, whose
TTL-gated registry reload keys off the request path.

Every key in `.env` is a real field on `Settings`. `extra="ignore"` means an
unknown key is silently accepted and does nothing, so the ~12 such keys the
Flask `.env.example` had accumulated were removed rather than carried over.
Note also that an *empty* value is not the same as an absent one — it
overrides the field default with `""` (which is why `MITRA_USER_AGENT` is
commented out rather than left blank).

Mitra's own endpoint paths (`MITRA_PROFILE_PATH`, `MITRA_FINALIZE_V2_PATH`, …)
are settings too, but they are a third-party API contract rather than a
preference — change them only when Mitra moves an endpoint. The v1/v2 finalize
pair is load-bearing: the two endpoints read the user token from different
places and select different PDF renderers, so `ConfigSyncService` asserts at
startup that every agent YAML's `finalize_path` matches one of the configured
pair.

### Multi-tenant configuration: capabilities and agents

Everything the sidebar shows, and every agent's configuration, lives in the
**database** and can vary **per tenant**. No file edit, no rebuild, no
restart.

**How a tenant is reached today, and how it is not.** Identity is resolved
once from `.env`, not per request (see [Configuration](#configuration) below)
— the frontend is a bare SPA with no login flow and no way to supply a
caller-specific token, so there is no live path today where an ordinary
end-user request resolves to more than one tenant. What *is* fully real and
exercised: the admin API (`/api/admin/capabilities`) takes `tenant_id` /
`organization_id` as **explicit** parameters, not derived from the caller's
own identity, so scoped rows are completely writable and readable, and the
resolver (`capability_service.resolve_for_user`,
`AgentRegistry.resolve_for_scope`) is generic over *whatever* `UserContext` it
is given — see `tests/guards/test_tenant_isolation.py` and
`tests/integration/test_admin_capabilities.py`, which exercise it directly.
Wiring a real per-caller identity source back in (e.g. a gateway that
terminates a user's own session and forwards their token) is then a change
to `app/dependencies/identity.py` alone — the schema, services and admin API
need nothing further.

**Saarthi does not own tenants or users.** Those are the user service's
records. `tenant_code` and organization id are JWT claims
(`app/services/identity.py`), and the config tables key on those strings
directly — so there is no `tenants` table and no foreign key to one. A row
naming a tenant the user service never issued is simply never read: inert,
not broken. `conversations.tenant_code` has always worked this way.

**The scope rule.** Every scoped row carries `tenant_id` + `organization_id`,
defaulting to the sentinel `'default'`. Resolution is most-specific-wins:

```
(tenant, org)  >  (tenant, 'default')  >  ('default', 'default')
```

so a tenant inherits the default catalogue until it inserts a row of its own.
**Onboarding a tenant costs zero writes**, and shipping a capability to
everyone is one insert at default scope. A sentinel rather than NULL, because
NULL would make every scope unique constraint a partial index (NULLs do not
compare equal in Postgres) and every lookup an `IS NOT DISTINCT FROM`.

| Table | Holds |
|---|---|
| `capabilities` | the sidebar's cards. `metadata` carries `action` |
| `capability_agents` | membership + per-capability label and order. Real FK to `agents`, so a dangling reference is unrepresentable |
| `agents` | the catalogue. `key` stays **globally** unique — `AgentRegistry` and every pinned session look agents up by bare key |
| `agent_configs` | versioned config, **scoped**. Renamed from `agent_configurations` in 0006 |

**A tenant's agent config is a whole `agent_configs` row at that tenant's
scope**, not a patch merged over a base. That is a safety property, not a
style choice: `HandlerFactory` caches handlers by `(spec.key, checksum)`, and a
handler holds its system prompt. A patch would have to be re-canonicalised and
re-checksummed at exactly the right moment, and forgetting once would serve one
tenant's prompt to another. A scoped row carries its own checksum computed from
its own content, so different content implies a different cache key by
construction. `tests/guards/test_tenant_isolation.py` pins both directions —
different configs must not share a handler, identical ones must.

Two indexes make this work and must not be relaxed:
`uq_agent_cfg_scope_version` (version numbering is per scope, so a tenant's v1
does not jump when another tenant edits) and `uq_agent_cfg_one_active` (one
active config per *(agent, scope)*, not per agent).

`GET /api/ui/capabilities` is the **single source** for the panel — the
frontend keeps no bundled copy, so an empty answer means an empty sidebar. It
answers 200 with an empty list rather than 404: "this tenant has no
capabilities" is a real answer, where 404 would claim the route does not exist.

`/api/admin/capabilities` (admin-gated) is the CRUD. Scope is **explicit** on
every route, never inferred from the calling admin's own token — an admin
editing another tenant's configuration is the normal case.

### Where the YAML fits now

`app/config/agents/*.yaml` is a **seed for a fresh database**, not the
authority — which is why `CONFIG_SYNC_MODE` now defaults to `off`. Run once
with `safe` to seed a new environment, then leave it off so a deploy can never
revert a live override or orphan-disable a tenant's agent. Booting an empty
database with `off` fails fast: `sync_and_reload` raises when the registry
loads 0 agents.

`app/config/ui/capabilities.yaml` is **gone**; migration 0006 seeds those rows
instead. Membership is linked at startup by
`app/services/capability_seed.py`, because a migration cannot create it — on a
fresh database the agents do not exist yet and `capability_agents` holds a real
FK. It only ever fills a default-scope capability that has **no** members at
all, so a curated membership is never contradicted.

`MITRA_COMPANY`, `MITRA_STORY_BOT_ROUTE` and `MITRA_DISCUSSION_BOT_ROUTE` are
**not** `Settings` fields — they are resolved by `${VAR}` substitution inside
the agent YAML and read from `os.environ`. `app/core/settings.py` calls
`load_dotenv()` explicitly so that works deterministically; under Flask it
happened to work because `flask run` loads `.env` and so does litellm on
import, neither of which is a guarantee under uvicorn.
