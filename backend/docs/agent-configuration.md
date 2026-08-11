# Agent configuration

How to configure **any** agent. The schema of record is
`app/domain/agent_spec.py`; this document explains it, and everything
provider-specific is confined to the appendix.

For the wider question of *which* settings belong in `.env` versus the
database, see [configuration.md](configuration.md). This document is the spec
reference and the onboarding guide.

---

## 1. What an agent is

Two rows. No files, no YAML.

| | |
|---|---|
| **`agents`** | Identity and catalogue placement — `key`, `name`, `description`, `agent_type`, `status`, `is_default`, `sort_order`, `icon`, and the owning `(tenant_id, organization_id)`. |
| **`agent_configs`** | Behaviour — one versioned JSONB column holding an entire `AgentSpec`, scoped `(tenant_id, organization_id)`, with its own checksum. |

Definitions: `app/models/orm.py`. A fresh database gets its catalogue from
migration `0010_seed_default_data`, so `make migrate` alone yields a working
application.

`key` is **globally unique** and stays that way — the registry, the router,
orchestration and every open session look agents up by bare `key`. A tenant
differentiates behaviour by owning a tenant-scoped `agent_configs` row, never by
minting a second agent with the same key.

---

## 2. Choosing an agent type

`agent_type` is fixed at creation and cannot be changed by a config write.

### `llm` — configuration alone, no code

A prompt, a model, and a list of tools that are already registered in the
process. Everything the agent does is expressible in the config row, so
onboarding one is a database write and nothing else.

### `remote_flow` — delegated to an external system

The conversation is handed to a remote system that owns the interview state.
This **requires a handler in `app/agents/`**, because the protocol is not
expressible as configuration.

**Where the line is.** *If correct behaviour depends on knowledge of the remote
system that is not visible in a request/response pair, it needs code.* The
existing `remote_flow` integration is the worked example of all three ways that
happens:

- **The transport is not request/response.** A persistent WebSocket with an
  authenticate handshake, a settle window, a reader thread, multi-frame turn
  accumulation with an idle-gap heuristic, and a pooled per-conversation
  lifecycle with an LRU and a reaper.
- **The state machine lives in the client.** A turn the app stopped listening
  for must be reconciled *without re-sending it*, because the remote system
  merges consecutive same-sender messages and would silently destroy an answer.
- **Failure is not signalled by the status code.** Picking the wrong finalize
  endpoint returns HTTP 200 and a valid, downloadable, **blank** PDF, with
  nothing logged anywhere.

None of that can be inferred from the wire. A new integration that *is* plain
request/response does not need a new agent type — expose it as a tool and use an
`llm` agent.

---

## 3. Where a setting belongs

The governing rule, from [configuration.md](configuration.md):

> Env holds what the app needs **before it can read a database**, plus the
> secrets. Everything else is a row.

Three categories, stated without reference to any particular provider:

**Environment only.** Secrets and credentials; anything read before the app
object exists (host, port, workers, prefix, database URL); process identity and
log level; feature flags that a database write must never be able to flip
(authentication, the admin surface); bounds on a process-global resource pool;
and **any operator backstop on a value the config can set** — a ceiling is
pointless if the thing it constrains can edit it.

**Database only.** Business configuration with no sensible global default. If a
wrong value produces a plausible-looking but incorrect result rather than an
outage, it belongs here and should be required by the schema rather than given a
fallback.

**Database over an environment floor.** Optional spec fields where unset means
"use the deployment default". This is the additive pattern: a deployment with no
overrides resolves to exactly what it resolved to before the field existed, and
if a scoped config cannot be read, resolution falls back field by field.

---

## 4. Spec reference

### Every agent — `BaseAgentSpec`

| Field | Type | Default | Notes |
|---|---|---|---|
| `schema_version` | `1` | `1` | Only value accepted. |
| `key` | str | — | `^[a-z][a-z0-9_]{1,62}$`. Must match the `agents` row. |
| `name` | str | — | 1–120 chars. Unique across agents. |
| `description` | str | — | 1–1000 chars. **The router reads this** to classify intent, so write it for that audience. |
| `status` | `enabled`\|`disabled` | `enabled` | |
| `default` | bool | `false` | The fallback agent when routing is inconclusive. |
| `sort_order` | int | `100` | Sidebar ordering. |
| `capabilities` | list[str] | `[]` | Free-text tags. Surfaced in `GET /api/agents` and given to the router prompt. **Not** a wiring mechanism — declaring one grants nothing. |
| `agent_type` | `llm`\|`remote_flow` | — | The discriminator. |

Plus these sub-objects, each with its own defaults:

**`routing`** — `keywords` (`[]`), `priority` (`50`, 0–100), `pin_session`
(`false`), `exit_keywords` (`["/exit","cancel","stop"]`),
`confidence_threshold` (`0.5`), `router_selectable` (`true`),
`direct_selectable` (`true`).

`pin_session` is the significant one: it means a session row is opened for this
agent and the conversation stays with it until finalised or abandoned. Agents
that own multi-turn state need it; stateless agents get no session at all.

**`memory`** — `strategy` (`recent`|`none`, default `recent`), `history_turns`
(`10`), `include_other_agents` (`true`).

Set `strategy: none` for any agent whose remote system reconstructs its own
conversation state. Supplying history to a system that is already tracking it
corrupts that state.

**`limits`** — `max_tool_iterations` (`3`, 0–10), `max_turns` (`null`),
`rate_limit_per_conversation_per_min` (`20`),
`rate_limit_per_user_per_min` (`60`).

**`access`** — `tenant_codes` (`[]`), `organization_codes` (`[]`),
`required_roles` (`[]`), `allow_anonymous` (`true`).

An empty list means *no restriction on that dimension*, and the three dimensions
are ANDed. `required_roles` is satisfied by holding **at least one** of the
listed roles, and it matches against the user's **active organization only** —
roles are never flattened across organizations.

**`features`** — `emit_options` (`false`), `streaming` (`false`),
`record_tool_executions` (`true`).

**`retry`** — accepted by the schema and **not implemented**. Nothing in `app/`
reads it; two seeded configs carry the key and it has no effect. Do not tune it
expecting a change in behaviour. It is documented here only so that finding it
in a stored config is not mistaken for working configuration.

### `agent_type: llm`

| Field | Type | Notes |
|---|---|---|
| `prompt` | str | The system prompt. Non-empty. |
| `tools` | list[str] | Names from the process-wide tool registry. **Validated at write time** — an unknown name is a 422, not a runtime surprise. `GET /api/tools` lists what is available. |
| `model` | object | `provider` (`openrouter`), `name`, `temperature` (`0.0`, 0–2), `max_tokens` (`null`, ≤32768), `timeout_s` (`30.0`, ≤300). |

### `agent_type: remote_flow`

One field, `remote`, whose shape is provider-specific. See the appendix.

### `extra="forbid"`

An unrecognised key is rejected with a 422 from the config API. A config written
for an older schema is refused outright rather than half-applied — which is the
intended behaviour, but it has a consequence worth knowing: if a stored config
somehow becomes invalid, `AgentRegistry.reload` **skips that agent** and logs an
error (`app/services/agent_registry.py:95-103`). The agent disappears from
routing and the sidebar while everything else keeps working. One bad row costs
one agent, deliberately, rather than taking down the catalogue — but it does so
quietly, so check the logs if an agent goes missing.

---

## 5. Scoping and resolution

Every scoped row carries `tenant_id` and `organization_id`: plain strings with a
`'default'` sentinel, **not** foreign keys. There are no tenant or organization
tables — the values arrive as JWT claims and these columns hold them.

Resolution is most-specific-wins:

```
(tenant, org)  >  (tenant, 'default')  >  ('default', 'default')
```

Implemented once, in `app/services/agent_registry.py:219-291`, and mirrored by
`capability_service`. A scope with no config of its own gets the default
snapshot back unchanged, so the common path costs one indexed lookup.

Two properties that matter:

**A scoped config is a whole row with its own checksum** — not a patch merged
over a base that someone must remember to re-checksum. Handlers are cached by
`(key, checksum)`, so this is what structurally prevents one tenant's prompt
from answering another tenant's user. Any future per-scope configuration should
preserve that property.

**Resolution degrades rather than failing.** A scoped config that will not
validate falls back to the default and logs; it never fails the turn.

The registry snapshot itself is **default-scope only**
(`agent_registry.py:68-74`); tenant overrides are applied per request. A code
path that reads an agent straight off the snapshot silently ignores every tenant
override — `tests/guards/test_mitra_scope_isolation.py` is the regression pin
for exactly that class of bug.

---

## 6. Secrets

**A config row never holds a secret value.**

Where a scope needs its own credential, the spec names an **environment
variable** and the value is read from the environment when the client is built.
`remote.origin_env` is the existing instance of this and the pattern any future
integration should follow.

The supporting mechanics are part of the pattern, not incidental:

- the resolved value is held in a `repr=False` field, so it cannot leak through
  a log line that reprs the connection;
- it enters the client-cache checksum **only as its own sha256**
  (`app/integrations/mitra/connection.py:110-132`), so two scopes with different
  credentials can never share a cached client while the plaintext is never
  stored anywhere;
- error detail extracted from an upstream response is dropped entirely if the
  credential appears in it, rather than being pattern-scrubbed.

Consequence for review: a spec field whose name ends `_env` holds a **variable
name** and is safe to read in `GET /api/agents/{key}`. A field holding a
credential value would not be, and should not exist.

---

## 7. Adding a new agent

No restart. `AgentRegistry.maybe_reload` picks up changes within
`REGISTRY_TTL_S`, and every config write reloads the registry explicitly.

**1 — Insert the catalogue row.** There is no create-agent API; `agents` is
written by migration or by hand.

```sql
INSERT INTO agents (key, name, description, agent_type, status, sort_order)
VALUES ('policy_helper', 'Policy Helper',
        'Answers questions about leave, reimbursement and HR policy.',
        'llm', 'enabled', 40);
```

**2 — Post the config.** Admin-gated (`SAARTHI_ADMIN_ENABLED=1` and the `admin`
role).

```bash
curl -X POST "$BASE/api/agents/policy_helper/config" \
  -H 'Content-Type: application/json' \
  -d '{
    "schema_version": 1,
    "key": "policy_helper",
    "name": "Policy Helper",
    "description": "Answers questions about leave, reimbursement and HR policy.",
    "agent_type": "llm",
    "status": "enabled",
    "sort_order": 40,
    "capabilities": ["general_qa"],
    "prompt": "You are a policy assistant. Answer from the material provided. If you do not know, say so.",
    "tools": [],
    "model": { "provider": "openrouter", "name": "qwen/qwen3.7-flash", "temperature": 0.0, "timeout_s": 30 },
    "routing": { "keywords": ["leave", "policy", "reimbursement"], "priority": 40, "pin_session": false },
    "memory": { "strategy": "recent", "history_turns": 10 }
  }'
```

Response carries `version`, `checksum`, `activated_at` and the new
`registry_version`.

**3 — Confirm.** `GET /api/agents` should list it. If it does not, the config
failed to validate on reload — check the logs for an `AgentRegistry: skipping
agent` line.

---

## 8. Versions, activation, rollback

| | |
|---|---|
| `POST /api/agents/{key}/config` | Insert a new version and activate it. |
| `GET /api/agents/{key}/config/versions` | History, newest first. |
| `POST /api/agents/{key}/config/{n}/activate` | Roll back to version `n`. |
| `GET /api/agents/{key}` | The active config, secrets redacted. |
| `PATCH /api/agents/{key}` | Change `status`. |
| `POST /api/agents/reload` | Force a registry reload. |

Nothing is edited in place: a change is always a new version, so the previous
one stays a single `activate` call away. Exactly one config per
`(agent, scope)` may be active, enforced by a partial unique index — which is
also why writers must deactivate *before* inserting. Version numbering is **per
scope**, so one tenant's `v1` does not jump when another tenant edits.

> **Known limitation — scoped writes.** These routes take no `tenant_id` /
> `organization_id`, and their queries are unscoped
> (`app/routers/admin.py:98, 140, 149, 258, 262, 267`). Every write therefore
> lands at `('default','default')`, and the deactivate statement would clear
> *every* scope's active config, not just the target's. **Per-tenant configs are
> readable and resolvable, but writable only by direct SQL.** Fix this before
> relying on tenant overrides in production.

---

## 9. Validation at write time

`POST /api/agents/{key}/config` is the **only** gate. There is no YAML and no
startup sync left, so a config reaches its integration exactly as it was
written here.

Applied to every agent:

- the body must validate against the discriminated `AgentSpec` union — first
  error only, reported as `{"error": "CONFIG_INVALID", "path": [...], "msg": ...}`;
- `extra="forbid"`, so an unrecognised key is rejected;
- `agent_type` must match the `agents` row — it cannot be changed;
- for `llm`, every name in `tools` must be registered
  (`tool_registry.assert_all_known`).

Per-type rules go beyond schema validation where a wrong value would fail
*silently*. The `remote_flow` check is the model to copy: it rejects a
`finalize_path` that matches neither resolved endpoint, because anything
unrecognised otherwise falls through to a default branch that the remote system
**accepts** — returning HTTP 200 and a blank PDF with nothing logged. Rejecting
it at write time is the only cheap place to catch it.

When adding a new integration, ask what a plausible typo would do. If the answer
is "an outage", the schema is enough. If it is "a wrong-looking success", add a
write-time check.

---

## Appendix — worked example: the Mitra `remote_flow` integration

Everything below is specific to one provider and is included as an illustration
of the sections above.

### `remote` fields

| Field | Notes |
|---|---|
| `provider` | `mitra` |
| `flow_name` | `guest-mi-story` \| `guest-discussion` |
| `company` | **Required, no env fallback.** Mitra identifies a profile by `(email, company)`, so this single field decides whether every tenant shares one Mitra company or each gets its own. |
| `bot_route` | **Required, no env fallback.** Selects the CompanyBot. |
| `origin_env` | Names the variable holding this scope's `Origin` credential — never the value. Unset means the deployment-wide `MITRA_ORIGIN_URL`. |
| `connection` | **Required.** Which Mitra deployment this scope reaches. See below. |
| `default_language` / `supported_languages` | `en`, `hi`, `kn`, `te` |
| `handshake` | `settle_ms` (1500), `ack_types`, `timeout_ms` (5000) |
| `turn` | `first_turn_timeout_ms` (60000), `turn_timeout_ms` (45000), `idle_gap_ms` (8000) |
| `completion_poll_every_turn` | Poll for terminal state after each turn. |
| `finalize_path` | v1 or v2. Not a version preference — see below. |
| `finalize_as_guest` | Finalize without a user token. |
| `report_media_type` | `application/pdf` |

`company` and `bot_route` are `min_length=1` rather than optional because an
empty value does not fail loudly at Mitra: it silently resolves the wrong bot or
splits a user's profile history.

### `connection` — the endpoint, and the whole of it

```jsonc
"connection": {
  "base_url": "https://mitra.example.org",          // required
  "ws_url":   "wss://mitra.example.org/ws/common/", // required
  "user_agent": "...",                              // default: a browser UA
  "allowed_hosts": ["static.example.org"],          // default: []
  "connect_timeout_s": 10, "read_timeout_s": 30, "ws_connect_timeout_s": 10,
  "ip_city": "", "ip_state": "", "ip_zip": "",
  "paths": {                                        // Mitra's API contract
    "profile": "/api/profile/", "generate_session": "/api/generate-session/",
    "chat": "/api/companychat/", "get_story": "/api/get-story/",
    "finalize_v1": "/api/end-story/", "finalize_v2": "/api/end-story/v2/"
  }
}
```

**There is no environment floor behind this.** `MITRA_BASE_URL`, `MITRA_WS_URL`,
`MITRA_USER_AGENT`, `MITRA_ALLOWED_HOSTS`, the three timeouts, the three IP
fields and the six `MITRA_*_PATH` settings were deleted from `Settings` and
moved here by migration 0009. `resolve_connection`
(`app/integrations/mitra/connection.py`) copies every field straight across.

That is why `base_url` and `ws_url` have no default: there is nothing left to
fall back to, and an agent pointed at nowhere fails as a blank report rather
than an error. `MitraPathsSpec` restates `MitraPaths`' defaults rather than
importing them, because the domain layer is import-pure by contract
(`.importlinter`); `tests/unit/test_settings_config.py` pins the two together.

Only **four** provider keys are env-only, each for a structural reason — and
note that none of them names a platform, so onboarding one adds no key:

| Key | Why it cannot be a config row |
|---|---|
| `PROVIDERS_ENABLED` | Read at container build, before any config is loaded — a boot-order cycle. |
| `PROVIDER_HOST_CEILING` | The operator's backstop *on* config. A control config could widen is not a control. |
| `PROVIDER_MAX_OPEN_CHANNELS`, `PROVIDER_IDLE_CLOSE_S` | Bounds on one pool per provider, shared by every agent using it. |

Credentials are the fifth case and work differently: they stay in `.env` but are
**named** by the config row (`remote.auth.credential_env` and the other `*_env`
fields) rather than declared as `Settings` fields. That is what lets endpoint,
company and tenant be per-tenant configuration while the secrets stay
deployment-level — and why a new platform adds variables to `.env` but no field
to `app/core/settings.py`.

There are likewise no `MITRA_COMPANY` / `MITRA_STORY_BOT_ROUTE` /
`MITRA_DISCUSSION_BOT_ROUTE` variables (removed in 0007), and no `SAATHI_*`
endpoint or tenant settings (removed in 0013). The reasoning is the same
throughout: as environment values these were process-global, which meant one
deployment could serve exactly one company, on one instance, with one set of bot
routes — and, for a per-user platform, exactly one identity.

### v1 vs v2 finalize

A behavioural choice, not a version preference — the two endpoints resolve the
story bot from different Mitra tables and read the user token from different
places (v2 from the `Authorization` header, v1 from the request body). Both
seeded agents use v1, for different documented reasons. `finalize_as_guest` must
agree with what the WebSocket handshake sent: interviewing as a guest and
finalising as an authenticated user is the mismatch, not either half.

### Caveats

- **`company` takes effect on new sessions only.** The remote profile id is
  persisted on the session row at the first turn, so an in-flight interview is
  not re-profiled.
- **`bot_route` is re-resolved every turn** and sent in the authenticate frame,
  so changing it mid-interview repoints the bot.
- **`WORKERS` must stay 1 while any enabled provider is stateful.** The channel
  pool is in process memory.
- **`remote.allowed_hosts` is an SSRF control.** Widening it through the admin
  API widens what the client will fetch. Set `PROVIDER_HOST_CEILING` in any
  deployment where config writers are not fully trusted.
