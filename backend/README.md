# Saarthi backend (FastAPI, Python 3.10)

## Setup

```bash
make install                 # uv venv (py3.10) + uv pip install -r requirements.txt
cp .env.example .env         # ONE config file — fill OPENROUTER_API_KEY, SAARTHI_STATIC_TOKEN, MITRA_*
createdb saarthi_new         # local Postgres 17
make run                     # migrates to head, then binds HOST:PORT from .env
```

`make migrate` alone produces a working application: the agent catalogue is
seeded by migration 0007. There is no YAML to sync and no first-run flag.

Infrastructure and secrets — host, port, `API_PREFIX`, CORS origins, feature
flags, credentials, and the Mitra defaults — live in that one `.env`. **Agent
configuration lives in the database**, per tenant and per organization, and is
edited through the config API without a restart. See
[Configuration](#configuration) and [docs/configuration.md](docs/configuration.md).

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
├── integrations/mitra/connection.py  resolves a per-scope MitraConnection
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

6. **`sync_and_reload` runs in `create_app()`, not the lifespan.** An empty
   catalogue must abort at import rather than serving requests that can route
   nowhere, and `TestClient(app)` must get a fully-booted app without a `with`
   block. It no longer syncs anything — the name is kept because
   `create_app()` and the tests call it. One agent whose config will not parse
   is skipped and logged, not fatal; zero agents is fatal.

7. **Two error envelopes, kept apart.** Everything except admin uses
   `{status, error, error_code, request_id}`. The admin router answers with a
   bare `{"error": CODE}`. Do not unify them; the tests pin both.

## Tests

```
tests/unit/            no DB
tests/integration/     real Postgres
tests/characterisation/ TestClient end-to-end, golden JSON fixtures
tests/guards/          the rules above, made executable
```

Two of the guards are worth knowing by name:

* `test_tenant_isolation.py` — different tenants' agent configs must not share
  a `HandlerFactory` entry, and identical ones must. This is what stands
  between the schema and one tenant's system prompt answering another tenant's
  user.
* `test_mitra_scope_isolation.py` — the paths that do NOT go through a handler
  (resume, finalize, report) must resolve the caller's scope too, or a tenant's
  story is submitted to the default scope's Mitra company.
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
| 2 | `backend/.env` (git-ignored) | infrastructure, secrets, feature flags, and the Mitra DEFAULTS |
| — | `agent_configs` (database) | agent configuration, per tenant/org — overrides the Mitra defaults above |
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
places and select different PDF renderers, so `POST /api/agents/{key}/config`
rejects a config whose `finalize_path` matches neither. They are also the
DEFAULT paths — a scope may override them through `remote.connection.paths`,
and the check is made against whatever that scope resolves to.

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

### There is no YAML

`app/config/agents/*.yaml` is **gone**, along with `ConfigSyncService` and
`CONFIG_SYNC_MODE`. The catalogue is seeded by **migration 0007** and edited
through `POST /api/agents/{key}/config`, which versions, audits and validates
every change. `app/config/ui/capabilities.yaml` went the same way in 0006.

That removes the "on a fresh database set `CONFIG_SYNC_MODE=safe` for the first
run, then put it back" step, and with it the possibility of a deploy reverting
a live override — which is why the mode defaulted to `off` in the first place.

Upgrading a database from the YAML era is handled: migration 0007 supersedes
any active default-scope config still written in the old shape (a `${VAR}`
reference, or `bot_route_env` / `company_env`) with a literal one, as a **new
version**, so the previous config stays one activate call away. An operator's
own configuration is left untouched.

`MITRA_COMPANY`, `MITRA_STORY_BOT_ROUTE` and `MITRA_DISCUSSION_BOT_ROUTE` are
no longer read at runtime at all. Their values now live in `remote.company` /
`remote.bot_route` on the stored config, where **a tenant can override them** —
and since Mitra identifies a profile by `(email, company)`, that is the
difference between every tenant sharing one Mitra company and each having its
own.

`MITRA_ORIGIN_URL` is the one exception, and stays env-only: Mitra gates
admission on the Origin header, so it is a credential. A scope needing its own
names a variable through `remote.origin_env` rather than carrying a value.

**Full key-by-key breakdown, resolution order and the runtime-change workflow:
[docs/configuration.md](docs/configuration.md).**
