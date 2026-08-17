# Configuration: what lives in `.env`, what lives in the database

> This document is about the **split** between the two stores. For the agent
> spec itself — every field, scoping, and how to onboard a new agent — see
> [agent-configuration.md](agent-configuration.md).

There are two configuration stores, and the split is not arbitrary.

**`.env` holds secrets, infrastructure, and anything read before the app
exists.** **`agent_configs` holds business configuration**, scoped
`(tenant_id, organization_id)`, versioned, audited, and changeable at runtime.

There is deliberately **no third store, and no files**. `agent_configs` already
provides per-scope resolution, version history, one-active-per-scope,
checksums, an audit trail and a management API, so Mitra configuration was
added to it as spec fields rather than given a config table of its own. The
agent YAML and `ConfigSyncService` are gone entirely — the catalogue is seeded
by **migrations 0007 and 0009**, so `make migrate` alone gives a working
application.

## The rule

> Env holds what the app needs **before it can read a database**, plus the
> secrets. Everything else is a row.

**There is no env floor under Mitra any more.** `remote.connection` is the sole
source for which Mitra deployment an agent reaches — there is no second place a
value can come from, and no merge order to reason about. Sixteen `MITRA_*`
settings were deleted from `Settings` in favour of it (migration 0009), joining
`remote.company` / `remote.bot_route`, which lost their env fallback in 0007.

Only **five** Mitra keys remain in `.env`, each for a structural reason listed
below. Everything else is a config row.

> **Upgrading:** run `make migrate` **before** deleting the old `MITRA_*` keys
> from your `.env` — 0009 reads them out of the environment and copies them into
> the config rows, so your deployment keeps its own endpoint rather than
> inheriting the migration's fallbacks. Then delete them: `extra="ignore"` on
> `Settings` means a leftover key is accepted silently and does nothing, so
> nothing will warn you that it stopped having an effect.

## Where each key lives

### Stays in `.env`, permanently

| Key(s) | Why |
|---|---|
| `OPENROUTER_API_KEY` | secret |
| `ELEVATE_JWT_SECRET` | **secret** — ELEVATE's own `ACCESS_TOKEN_SECRET`. Required to trust ANY per-request bearer token the frontend sends after a real login; without it, a request carrying a token is refused rather than trusted unverified. Since AUTH_CHECK=true has no fallback identity for a missing token (removed — it was an authentication bypass), an unset secret means nothing can authenticate at all |
| `MITRA_ORIGIN_URL`, `SAATHI_ORIGIN_URL` | **credentials** — these platforms gate admission on the Origin header. Never in the database, never in a log line. NOT `Settings` fields: a config row names the *variable* via `remote.auth.credential_env` and the value is read from the environment, which is why adding a platform adds no field |
| `DATABASE_URL`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `THREADPOOL_SIZE` | needed to reach the database at all |
| `HOST`, `PORT`, `WORKERS`, `API_PREFIX`, `FRONTEND_ORIGINS` | read before the app object exists |
| `APP_ENV`, `LOG_LEVEL` | process identity and logging, read at import |
| `PROVIDERS_ENABLED` | comma-separated registry keys. Decides whether the database-reading provider clients are built at all (`core/container.py`) — a boot-order cycle. ONE key however many platforms exist; it replaced a flag per platform |
| `AUTH_CHECK`, `SAARTHI_ADMIN_ENABLED` | a database write must never be able to disable authentication or open the admin surface |
| `PROVIDER_MAX_OPEN_CHANNELS`, `PROVIDER_IDLE_CLOSE_S` | bounds on **one channel pool per provider**, shared by every agent using it; a per-tenant channel ceiling has nothing to apply to |
| `PROVIDER_HOST_CEILING` | the operator's backstop *on* a config-supplied value — it would be pointless if the thing it constrains could edit it |
| `REGISTRY_TTL_S` | governs how configuration itself is loaded — circular if database-driven |
| `JWT_IDENTIFIER_FIELD`, `JWT_EMAIL_SUFFIX` | identity resolves in `dependencies/identity.py`, upstream of any agent. Safe to keep global: Mitra keys a profile on `(email, company)`, and `company` is per-tenant, so two tenants sharing a suffix still get distinct profiles |
| `ELEVATE_BASE_URL` | decides whether the ELEVATE user-service client is built at all (`core/container.py`) — the same boot-order cycle as `VOICE_ENABLED`. Base URL of the one first-party user service behind `GET`/`PATCH /api/profile`; not a per-agent choice, and **not a credential** — the profile is read and written with the *caller's own* token. Unset ⇒ both routes answer `503 PROFILE_UNAVAILABLE` and the frontend hides its Profile section; it does **not** fail the boot. <br>**This key once meant something else.** It was retired into `remote.auth.token_endpoint` by migration 0013 when that was Saathi's per-agent login endpoint. This is a different setting wearing the same name — see the note in `.env.example` |
| `ELEVATE_CONNECT_TIMEOUT`, `ELEVATE_READ_TIMEOUT` | split rather than one combined budget, for the reason `THREADPOOL_SIZE ≤ DB_POOL_SIZE` exists: a request holds a worker thread for its whole life, so a dead DNS must not park one for the full read budget. **Tighter than the `BHASHINI_*` equivalents on purpose** — `PATCH /api/profile` makes *three* sequential ELEVATE calls (read the merge baseline, write the complete set, read it back), so a per-call budget sized for speech inference would compound into a two-minute worst case on one thread. A user-service read/write is a small database operation |
| `VOICE_ENABLED` | decides whether the Bhashini client and the object store are built at all (`core/container.py`) — the same boot-order cycle as `PROVIDERS_ENABLED` |
| `BHASHINI_API_KEY`, `BHASHINI_USER_ID`, `BHASHINI_AUTHORIZATION` | secrets |
| `CLOUD_STORAGE_SECRET` | a **secret**, and under `gcp` an entire service-account JSON. Never in the database, never in a log line, never in `__repr__` |
| `CLOUD_STORAGE_PROVIDER`, `CLOUD_STORAGE_ACCOUNTNAME`, `CLOUD_STORAGE_REGION`, `CLOUD_ENDPOINT`, `CLOUD_STORAGE_BUCKETNAME`, `CLOUD_STORAGE_BUCKET_TYPE` | needed to build a storage client at boot, before anything can be read from a database. Names follow the ELEVATE convention shared with the Node services, so one deployment's values drop into another |
| `BHASHINI_BASE_URL`, `BHASHINI_*_TIMEOUT`, `VOICE_MAX_AUDIO_BYTES`, `VOICE_CHUNK_DURATION_S`, `VOICE_TTS_BYTE_LIMIT`, `VOICE_ASR_MAX_WORKERS`, `VOICE_FFMPEG_TIMEOUT_S`, `VOICE_UPLOAD_URL_EXPIRY_S` | one shared upstream, not a per-agent choice. Voice is a property of the deployment, not of which agent happens to be answering |
| `CLOUD_STORAGE_MAX_ATTEMPTS`, `CLOUD_STORAGE_RETRY_MODE` | boto3's retry policy, for the `aws`/`s3`/`oci`/`minio` driver. `standard` mode retries throttling and transient 5xx with exponential backoff; `max_attempts` is the TOTAL, not additional |
| `CONVERSATIONS_PAGE_LIMIT_MAX` | ceiling on `GET /api/conversations?limit=`, applied after the client's own value. Caps how much history one request can pull |
| `LOCAL_STORAGE_DIR` | only read by the `local` provider, which is development-only |

### Database only — no `.env` key at all

Required on every `remote_flow` config, and resolved per
`(agent, tenant, organization)`. Seeded by migrations 0007 and 0009, changed per
deployment or per tenant through the config API.

| Spec field | Was | Deleted in |
|---|---|---|
| `remote.company` | `MITRA_COMPANY` | 0007 |
| `remote.bot_route` | `MITRA_STORY_BOT_ROUTE` / `MITRA_DISCUSSION_BOT_ROUTE` | 0007 |
| `remote.connection.base_url` | `MITRA_BASE_URL` | 0009 |
| `remote.connection.ws_url` | `MITRA_WS_URL` | 0009 |
| `remote.connection.user_agent` | `MITRA_USER_AGENT` | 0009 |
| `remote.connection.allowed_hosts` | `MITRA_ALLOWED_HOSTS` | 0009 |
| `remote.connection.connect_timeout_s` | `MITRA_CONNECT_TIMEOUT_S` | 0009 |
| `remote.connection.read_timeout_s` | `MITRA_READ_TIMEOUT_S` | 0009 |
| `remote.connection.ws_connect_timeout_s` | `MITRA_WS_CONNECT_TIMEOUT_S` | 0009 |
| `remote.connection.ip_city` / `ip_state` / `ip_zip` | `MITRA_IP_CITY` / `_STATE` / `_ZIP` | 0009 |
| `remote.connection.paths.*` (six) | `MITRA_*_PATH` (six) | 0009 |

`base_url` and `ws_url` are **required** — there is no env value left to fall
back to, and an agent pointed at nowhere fails as a blank report rather than an
error. The rest carry defaults (`app/domain/agent_spec.py`); the six paths
default to Mitra's own API contract.

Mitra identifies a profile by `(email, company)`, so `remote.company` is the
single field that decides whether every tenant shares one Mitra profile or each
gets its own.

### Overridable per `(agent, tenant, organization)`, with `.env` as the default

| `.env` key | Spec field |
|---|---|
| `OPENROUTER_MODEL` | `model.name` |
| `LLM_TIMEOUT` | `model.timeout_s` |

`OPENROUTER_MODEL` and `LLM_TIMEOUT` cannot leave `.env`: `RouterService` also
reads them directly for the **router's own classifier**, which is not an agent
and has no `agent_configs` row.

`LLM_MAX_RETRIES` has **no** spec equivalent. It is passed straight to the
LiteLLM client (`app/llm/factory.py`, `app/llm/__init__.py`) and is not
configurable per agent. The `retry` block on `BaseAgentSpec` is unrelated to it,
and is not implemented at all — nothing in `app/` reads it.

## Where a fresh database gets its catalogue

Migration 0007, then 0009.

**0007** inserts the three agents and their default-scope `agent_configs` rows,
and it is idempotent in a way that matters:

* an existing agent (`ON CONFLICT (key) DO NOTHING`) is not touched;
* an existing config is superseded **only** if it is still written for the
  YAML-era schema — a `${VAR}` reference, or `bot_route_env` / `company_env`.
  Those no longer validate, so leaving them active would mean zero agents at
  startup. The replacement is a new **version**, so the old config stays one
  `activate` call away;
* anything else is somebody's configuration and is left alone.

**0009** adds `remote.connection` to every `remote_flow` config row that lacks
one, reading each value from the environment and falling back to a QA default.
Unlike 0007 it edits **in place** rather than inserting a new version: a row
without a connection block no longer validates at all, so it is not a rollback
target — activating it would just make the agent vanish from routing with a log
line. It patches every version, not only the active one, so an older version can
still be activated afterwards. A row that already carries its own `connection`
is left alone.

## How resolution works

```
spec.remote.connection  (the agent config row)
    │
    ├─ resolve_connection(settings, spec.remote)     app/integrations/mitra/connection.py
    │      copies every field across from the spec — there is no env floor
    │      settings supplies ONLY: origin_url (via origin_env, by NAME)
    │                              the MITRA_HOST_CEILING intersection
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
          "finalize_path": "/api/end-story/",
          "connection": {
            "base_url": "https://tenant-mitra.example.org",
            "ws_url": "wss://tenant-mitra.example.org/ws/common/",
            "allowed_hosts": ["tenant-static.example.org"] } } }'

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
* **`WORKERS` must stay 1 while any enabled provider declares
  `stateful_transport`.** The channel pool is still in process memory. Stated in
  terms of the PROVIDERS rather than a named flag, so a deployment running only
  stateless providers is legitimately free of the constraint.
* **`allowed_hosts` is an SSRF control.** It now arrives *only* through a config
  write, so `PROVIDER_HOST_CEILING` is the only bound on it. It applies to every
  allowlist — it used to spare the `.env`-supplied one, but there isn't one any
  more. Set it in any deployment where config writers are not fully trusted.
* **There is no default-scope client.** Building one needs an endpoint, and an
  endpoint needs an agent spec. Every caller goes through `ProviderRegistry`
  with a resolved spec, which returns a provider cached by connection checksum.

## Removed

| Gone | Replaced by |
|---|---|
| `app/config/agents/*.yaml` | migration 0010 + `POST /api/agents/{key}/config` |
| `ConfigSyncService` | nothing — with no file to reconcile against there is no drift |
| `CONFIG_SYNC_MODE` | nothing |
| `${VAR}` expansion inside a spec | literal values in the row |
| `remote.bot_route_env` / `remote.company_env` | `remote.options.bot_route` / `.company` |
| `MITRA_COMPANY`, `MITRA_STORY_BOT_ROUTE`, `MITRA_DISCUSSION_BOT_ROUTE` | `remote.options.company` / `.bot_route` |
| the sixteen `MITRA_*` connection and path settings | the `remote` envelope + `remote.options.paths` (migrations 0009, 0013) |
| `MITRA_ENABLED`, `SAATHI_ENABLED` | `PROVIDERS_ENABLED` (migration 0013) |
| `SAATHI_TENANT_CODE`, `ELEVATE_BASE_URL`, `SAATHI_LOGIN_MECHANISM` | `remote.auth.tenant_code` / `.token_endpoint` / `.scheme` (migration 0013) — **and these three in turn removed by migration 0018**, once Saathi stopped minting a connection-level token at all |
| `SAATHI_EMAIL`, `SAATHI_PASSWORD`, `SAATHI_ACCESS_TOKEN`, `remote.auth.token_endpoint` / `.tenant_code` / `.identifier_env` / `.secret_env` / `.token_env` | the calling `UserContext.token` — Saathi's own per-request, already-verified ELEVATE login (migration 0018; see `app/providers/saathi/provider.py`) |
| `agent_type: saathi_flow` | `agent_type: remote_flow` with `remote.provider: saathi` (migration 0013) |
| `Settings.mitra_*` / `Settings.saathi_*` | nothing — a credential is NAMED by a config row, not declared as a field |
| `Container.mitra_rest`, `.mitra_clients`, `.mitra_sessions`, `.saathi_*` | `Container.providers` (one `ProviderRegistry`) |
| `SAARTHI_STATIC_TOKEN` / `Settings.saarthi_static_token` | nothing — it was the fallback identity `Authenticator.authenticate()` served to any request with **no** bearer token at all, which was an authentication bypass in practice (any caller, logged in or not, got a real identity for free). `AUTH_CHECK=true` now has no fallback: no token is unconditionally `401` |

`BaseAgentSpec` is `extra="forbid"`, so a config still written in the old shape
is **rejected** by the config API rather than silently ignored.

One behaviour moved rather than disappearing: disabling a provider used to make
`ConfigSyncService` write `status='disabled'` onto every `remote_flow` agent at
startup, and flip it back when re-enabled. It is now a runtime filter in
`AgentRegistry.reload()` and `capability_service` — a deployment-level switch
has no business mutating rows that a tenant's configuration also lives in.
