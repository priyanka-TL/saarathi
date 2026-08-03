# Configuration: what lives in `.env`, what lives in the database

There are two configuration stores, and the split is not arbitrary.

**`.env` holds secrets, infrastructure, and anything read before the app
exists.** **`agent_configs` holds business configuration**, scoped
`(tenant_id, organization_id)`, versioned, audited, and changeable at runtime.

There is deliberately **no third store, and no files**. `agent_configs` already
provides per-scope resolution, version history, one-active-per-scope,
checksums, an audit trail and a management API, so Mitra configuration was
added to it as spec fields rather than given a config table of its own. The
agent YAML and `ConfigSyncService` are gone entirely — the catalogue is seeded
by **migration 0007**, so `make migrate` alone gives a working application.

## The rule

> Env holds what the app needs **before it can read a database**, plus the
> secrets. Everything else is a row.

For the Mitra *connection* settings, env is a **floor** the database overrides:
each `remote.connection.*` field is optional, and unset means "use the `MITRA_*`
setting". That is also the failure mode — if a scoped config cannot be read,
resolution falls back to the default scope and from there to `.env`, field by
field.

For `remote.company` and `remote.bot_route` there is **no env fallback at all**.
They are required fields on the stored config. `MITRA_COMPANY`,
`MITRA_STORY_BOT_ROUTE` and `MITRA_DISCUSSION_BOT_ROUTE` no longer exist.

## Where each key lives

### Stays in `.env`, permanently

| Key(s) | Why |
|---|---|
| `OPENROUTER_API_KEY`, `SAARTHI_STATIC_TOKEN` | secrets |
| `MITRA_ORIGIN_URL` | a **credential** — Mitra gates admission on it. Never in the database, never in a log line. A scope needing its own names a *variable* via `remote.origin_env`; the value is still read from the environment |
| `DATABASE_URL`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `THREADPOOL_SIZE` | needed to reach the database at all |
| `HOST`, `PORT`, `WORKERS`, `API_PREFIX`, `FRONTEND_ORIGINS` | read before the app object exists |
| `APP_ENV`, `LOG_LEVEL` | process identity and logging, read at import |
| `MITRA_ENABLED` | decides whether the database-reading Mitra client is built at all (`core/container.py`) — a boot-order cycle |
| `AUTH_CHECK`, `SAARTHI_ADMIN_ENABLED` | a database write must never be able to disable authentication or open the admin surface |
| `MITRA_MAX_OPEN_CHANNELS`, `MITRA_IDLE_CLOSE_S` | bounds on **one process-global channel pool**; a per-tenant channel ceiling has nothing to apply to |
| `MITRA_HOST_CEILING` | the operator's backstop *on* a config-supplied value — it would be pointless if the thing it constrains could edit it |
| `REGISTRY_TTL_S` | governs how configuration itself is loaded — circular if database-driven |
| `JWT_IDENTIFIER_FIELD`, `JWT_EMAIL_SUFFIX` | identity resolves in `dependencies/identity.py`, upstream of any agent. Safe to keep global: Mitra keys a profile on `(email, company)`, and `company` is per-tenant, so two tenants sharing a suffix still get distinct profiles |

### Database only — no `.env` key at all

Required on every `remote_flow` config. Seeded by migration 0007, changed per
deployment or per tenant through the config API.

| Spec field | Was |
|---|---|
| `remote.company` | `MITRA_COMPANY` (deleted) |
| `remote.bot_route` | `MITRA_STORY_BOT_ROUTE` / `MITRA_DISCUSSION_BOT_ROUTE` (deleted) |

Mitra identifies a profile by `(email, company)`, so `remote.company` is the
single field that decides whether every tenant shares one Mitra company or each
gets its own.

### Overridable per `(agent, tenant, organization)`, with `.env` as the default

| `.env` key | Spec field |
|---|---|
| `MITRA_BASE_URL` | `remote.connection.base_url` |
| `MITRA_WS_URL` | `remote.connection.ws_url` |
| `MITRA_USER_AGENT` | `remote.connection.user_agent` |
| `MITRA_ALLOWED_HOSTS` | `remote.connection.allowed_hosts` |
| `MITRA_CONNECT_TIMEOUT_S` | `remote.connection.connect_timeout_s` |
| `MITRA_READ_TIMEOUT_S` | `remote.connection.read_timeout_s` |
| `MITRA_WS_CONNECT_TIMEOUT_S` | `remote.connection.ws_connect_timeout_s` |
| `MITRA_IP_CITY` / `_STATE` / `_ZIP` | `remote.connection.ip_city` / `ip_state` / `ip_zip` |
| `MITRA_*_PATH` (six) | `remote.connection.paths.*` |
| `OPENROUTER_MODEL` | `model.name` |
| `LLM_TIMEOUT` | `model.timeout_s` |
| `LLM_MAX_RETRIES` | `retry.max_attempts` |

`OPENROUTER_MODEL` and `LLM_TIMEOUT` cannot leave `.env`: `RouterService` also
reads them directly for the **router's own classifier**, which is not an agent
and has no `agent_configs` row.

## Where a fresh database gets its catalogue

Migration 0007. It inserts the three agents and their default-scope
`agent_configs` rows, and it is idempotent in a way that matters:

* an existing agent (`ON CONFLICT (key) DO NOTHING`) is not touched;
* an existing config is superseded **only** if it is still written for the
  YAML-era schema — a `${VAR}` reference, or `bot_route_env` / `company_env`.
  Those no longer validate, so leaving them active would mean zero agents at
  startup. The replacement is a new **version**, so the old config stays one
  `activate` call away;
* anything else is somebody's configuration and is left alone.

## How resolution works

```
Settings (.env)
    │
    ├─ resolve_connection(settings, spec.remote)     app/integrations/mitra/connection.py
    │      applies only the non-None fields of spec.remote.connection
    │      resolves origin_url from spec.remote.origin_env by NAME
    ▼
MitraConnection  (frozen, checksummed)
    │
    ├─ MitraClientRegistry.get(conn)  → one MitraRestClient per checksum
    └─ MitraSessionManager.acquire(spec, sess, conn) → one MitraChannel per conversation,
           stamped with conn.checksum; a mismatch forces a reconnect
```

And the spec itself:

```
AgentRegistry.reload()                  → the ('default','default') snapshot
AgentRegistry.resolve_for_scope(...)    → (tenant, org) > (tenant, 'default') > ('default','default')
```

**Every path that reads an agent off a session must go through
`OrchestrationService.agent_for_session`.** `AgentRegistry.get_by_id` answers
from the default-scope snapshot only; using it directly is how `resume`,
`finalize` and the report route silently ignored a tenant's own
`finalize_path` and `report_media_type`. `tests/guards/test_mitra_scope_isolation.py`
is the regression pin.

## Changing configuration at runtime

```bash
# Point one tenant at its own Mitra company.
curl -X POST "$BASE/api/agents/record_stories/config" \
  -H 'Content-Type: application/json' \
  -d '{ "key": "record_stories", ..., "remote": {
          "provider": "mitra", "flow_name": "guest-mi-story",
          "bot_route": "/guided_guest", "company": "tenant-company",
          "finalize_path": "/api/end-story/" } }'

curl "$BASE/api/agents/record_stories/config/versions"          # history
curl -X POST "$BASE/api/agents/record_stories/config/3/activate" # rollback
```

No restart. `AgentRegistry.maybe_reload` picks the change up within
`REGISTRY_TTL_S`, and a live Mitra socket whose connection checksum changed is
closed and reopened on the next turn rather than left pointing at the old host.

**Writes are validated.** `POST /api/agents/{key}/config` rejects a
`remote_flow` config whose `finalize_path` matches neither resolved endpoint;
`bot_route` and `company` are non-empty by schema. An unrecognised
`finalize_path` otherwise produces a valid HTTP 200 from Mitra and a **blank
PDF** with nothing logged — and this route is now the ONLY gate, since there is
no startup sync left to catch it.

## Caveats

* **`company` changes take effect on new sessions.** `remote_profile_id` is
  persisted on the session row at first turn, so an in-flight interview is not
  re-profiled. **`bot_route` is re-resolved every turn** and sent in the
  authenticate frame, so changing it mid-interview repoints the bot.
* **`WORKERS` must stay 1 while `MITRA_ENABLED=1`.** Unchanged by any of this —
  the channel pool is still in process memory.
* **`allowed_hosts` is an SSRF control.** Overriding it through the admin API
  widens what `_validate_url` will accept. Set `MITRA_HOST_CEILING` in any
  deployment where config writers are not fully trusted.

## Removed

| Gone | Replaced by |
|---|---|
| `app/config/agents/*.yaml` | migration 0007 + `POST /api/agents/{key}/config` |
| `ConfigSyncService` | nothing — with no file to reconcile against there is no drift |
| `CONFIG_SYNC_MODE` | nothing |
| `${VAR}` expansion inside a spec | literal values in the row |
| `remote.bot_route_env` / `remote.company_env` | `remote.bot_route` / `remote.company` |
| `MITRA_COMPANY`, `MITRA_STORY_BOT_ROUTE`, `MITRA_DISCUSSION_BOT_ROUTE` | the two spec fields above |

`BaseAgentSpec` is `extra="forbid"`, so a config still written in the old shape
is **rejected** by the config API rather than silently ignored.

One behaviour moved rather than disappearing: `MITRA_ENABLED=0` used to make
`ConfigSyncService` write `status='disabled'` onto every `remote_flow` agent at
startup, and flip it back when re-enabled. It is now a runtime filter in
`AgentRegistry.reload()` and `capability_service` — a deployment-level switch
has no business mutating rows that a tenant's configuration also lives in.
