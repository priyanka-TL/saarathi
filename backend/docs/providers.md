# Remote providers

How Saarthi talks to an external conversation platform, and what it costs to add
one.

Companion docs: [configuration.md](configuration.md) ·
[agent-configuration.md](agent-configuration.md) ·
[agent-generalization-plan.md](agent-generalization-plan.md) (the RFC this
implements).

---

## The shape

```
routers / services / orchestration      knows only: AgentSpec, RemoteProvider, ProviderError
        |
agents/remote_flow_handler.py           ONE handler, agent_type "remote_flow"
        |
app/providers/registry.py               name -> class, pkgutil-discovered,
        |                                PROVIDERS_ENABLED-filtered, instances
app/providers/protocol.py                cached by (name, connection.checksum)
        |
   +----+------+-----------+
mitra/     saathi/     <future>/         one package per platform, peers
```

```
app/providers/
├── protocol.py       RemoteProvider · ProviderTurn · SessionInit · FinalizeResult
├── registry.py       @register_provider · ProviderRegistry · discovery
├── errors.py         ProviderError + the eight it covers
├── recovery.py       reconcile-don't-resend, vendor-free
├── connection.py     RemoteSpec + environment -> frozen, checksummed connection
│
│   # NAMED FOR THE PROTOCOL FAMILY, NOT FOR A PLATFORM
├── transport/        pool.py (LRU + reaper) · http.py (REST shell) · ws.py · frames.py
├── ws_flow/          BaseWsFlowProvider — the shape any JSON-framed WebSocket
│                     conversational API follows, plus its frame parser
│
│   # ONE PACKAGE PER PLATFORM. Peers. Neither imports the other.
├── mitra/            @register_provider("mitra")
└── saathi/           @register_provider("saathi")
```

**Why the split.** Mitra and Saathi run the same Django application today, so
`ws_flow/` and `transport/` carry most of the code. That is a fact about the
**protocol they both speak**, not about either platform — so it lives below them
rather than in a package named after one. Saathi's channel and session manager
used to be *subclasses of Mitra's*; two `.importlinter` contracts now make that
impossible, and a third stops the shared layers reaching back into either.

---

## The contract

`app/providers/protocol.py`. Four `ClassVar`s replace every `if provider == ...`
the core used to carry:

| Declaration | What reads it |
|---|---|
| `options_model` | the registry, to type `remote.options` (must be `extra="forbid"`) |
| `stateful_transport` | `app/core/runtime.py` — the single-worker startup guard |
| `supports_recovery` | `TurnFinalizer` — whether a timed-out turn is reconciled |
| `produces_artifacts` | whether finalisation can yield a downloadable artifact |

Eight methods: `open_session`, `turn`, `is_complete`, `finalize`,
`fetch_artifact`, `reconcile`, `close_channel`, `validate_config`.

---

## Configuration

`remote` is an **envelope the core reads** plus an **`options` block only the
provider reads**. The rule that decides the split, and the only one:

> A field belongs in the envelope if and only if Saarathi's own core reads it.

```jsonc
"remote": {
  "provider": "saathi",              // registry key; a plain str, not a Literal
  "transport": "websocket",
  "base_url": "https://…",
  "stream_url": "wss://…/ws/common/",
  "allowed_hosts": [],               // SSRF list, ∩ PROVIDER_HOST_CEILING
  "timeouts":  { "connect_s": 10.0, "read_s": 30.0, "stream_connect_s": 10.0 },
  "headers":   { "User-Agent": "…" },
  "auth":      { "scheme": "origin_header", "credential_env": "SAATHI_ORIGIN_URL" },
  "flow_name": "saathi",             // persisted to agent_sessions.remote_flow
  "default_language": "en",
  "supported_languages": ["en","hi","kn","te"],
  "turn": { "first_turn_timeout_ms": 60000, "turn_timeout_ms": 45000, "idle_gap_ms": 8000 },
  "produces_artifact": false,
  "report_media_type": "application/pdf",

  "options": { "bot_route": "/saathi-bot", … }   // typed by the provider
}
```

### Two-stage validation

`app.domain` is import-pure by contract, so it cannot import a provider registry
to validate `options`. Split who validates what:

| Stage | Where | Validates |
|---|---|---|
| 1 | `RemoteSpec` (domain) | the envelope, `extra="forbid"` |
| 2 | `POST /api/agents/{key}/config` | `provider.options_model` + `provider.validate_config()` |
| 2 | `AgentRegistry.reload` / `resolve_for_scope` | the same; a failure skips **that one agent** and logs |
| 2 | `ProviderRegistry.get` | again, when the provider is built |

Both stages are strict. Every model in the chain sets `extra="forbid"` — a guard
test walks the registry and fails a provider whose model does not. This closes a
hole that was live for a long time: nested spec models inherited nothing from
their parent's config, so a mistyped key inside `remote` was dropped in silence
and took its default. For a finalize endpoint that meant HTTP 200 and a
downloadable, completely blank PDF, reachable by one doubled letter.

### Credentials

**A config row may NAME a credential; it must never HOLD one** — a row is
readable through the admin API. `remote.auth`'s `*_env` fields name environment
variables and `app/providers/connection.py` resolves them with `os.getenv`.

An unset variable is **refused**, not defaulted. There used to be a fallback to a
deployment-wide setting, which silently sent one platform's Origin to another's
endpoint — a 403 that reads like an outage rather than like the misconfiguration
it is.

This is also why adding a platform adds no `Settings` field: the variables go in
`.env`, and the row names them.

**Saathi is the one exception, and deliberately so.** It used to mint its own
connection-level ELEVATE credential from `remote.auth` (`scheme:
"elevate_login"` reading `SAATHI_EMAIL`/`SAATHI_PASSWORD`, or `scheme:
"static_token"` reading `SAATHI_ACCESS_TOKEN`) — one identity shared by every
user of a tenant's Saathi agent, which contradicted "per-user assistant."
Now that Saarthi's frontend logs users in directly against ELEVATE's user
service, `app/providers/saathi/provider.py` authenticates every REST/WS call
with **the calling `UserContext`'s own `token`** instead — nothing is minted,
cached, or named in `remote.auth`. Saathi's `remote.auth` is therefore just
the Origin credential now, identical in shape to a guest platform's. A caller
with no token (not logged in) gets a clear `ProviderAuthError`, not a
fallback identity.

---

## Downloadable documents

A turn can carry generated files. The platform sends them on an ordinary bot
frame:

```jsonc
"extra_content": {
  "download": {
    "pdf_url":   "https://<static-host>/…/1786427418-MIP_student-focus.pdf",
    "docx_url":  "https://<static-host>/…/1786427418-MIP_student-focus.docx",
    "file_name": "MIP_student-focus"
  }
}
```

`ws_flow/frames.py::_normalise_attachments` turns that into one `Attachment`
**per file**, which is what keeps "only one format available" from being a
special case anywhere downstream — it is simply a shorter list. The format comes
off the KEY (`<fmt>_url`), so a platform that starts sending `pptx_url` needs no
code change.

It travels the same chain options do, one additive field at each hop:

```
extra_content.download
  → Frame.attachments → BotTurn → ProviderTurn → AgentTurn
  → POST /api/chat  "attachments": [...]
  → conversation_messages.attachments   (so it survives a reload)
```

### Two rules that are easy to get wrong

**They are not options.** An option is click-to-reply: the SPA echoes its label
as a user message and posts its value as the next turn. A URL there would be
sent to the agent as user input. Attachments are `<a href>` and nothing else.

**They are allowlisted.** A URL handed to a user's browser gets the same check
as one we would fetch — https plus an exact host match against
`remote.allowed_hosts` — but the POLICY differs: a failure DROPS that one link
with a warning (host only, never the URL) rather than failing a turn whose reply
text is still worth showing. `transport/http.py::url_is_permitted` is the single
rule; `validate_url` is its raising half.

This matters operationally: these platforms serve documents from a **different
host than their API**, so the agent's `remote.allowed_hosts` must name it or
every download is silently dropped. Migration 0015 does that for `saathi`.

### Chat history

Documents are stored on the message and returned raw by
`GET /api/conversations/{id}/messages`, so reopening a conversation from the
sidebar shows them again, still clickable. Two details make that real rather
than apparent:

* `MessageAttachments` **ignores `readOnly`**, unlike `MessageOptions`. A
  replayed option must be inert because clicking it would fire a turn; a
  replayed download is a link to a document that still exists, and nothing else
  in the UI points at it.
* Messages written **before** this shipped have no stored URLs and cannot get
  them back — the data was discarded at the time. Only turns from here on
  replay their files.

The URL is stored verbatim. If a deployment's static host ever issues signed or
expiring links, an old conversation's buttons will go dead; the fix would be a
redirect endpoint that re-resolves them. Checked against QA on 2026-08-11: the
URLs are plain static paths with no signature or expiry parameter, so this is
theoretical there today.

---

## Enablement

One key: `PROVIDERS_ENABLED=mitra,saathi`. It replaced a flag per platform.

That mattered structurally, not cosmetically. The gate existed in three places
keyed on `agent_type`, which is why a second platform had to invent a second
delegated agent type (`saathi_flow`) just to be switchable independently —
dragging a Postgres enum value, a second handler and a second set of container
slots behind it. Keyed on `remote.provider` instead, one set scales, and
`agent_type` went back to describing *how a turn is executed* rather than *who
executes it*.

It stays in the environment because it is read at container-build time, before
any agent config is loadable, and because a database write must not be able to
switch a provider on.

---

## Adding a platform

1. Drop in `app/providers/<name>/` with a `@register_provider` class and an
   `options_model` carrying `extra="forbid"`. If it speaks a JSON-framed
   WebSocket, inherit `BaseWsFlowProvider` and supply `_build_rest()`,
   `open_session()` and (if it authenticates per user) `_access_token()`.
2. Put its credentials in `.env`.
3. Add `<name>` to `PROVIDERS_ENABLED`.
4. Write its agent's config row, naming those variables in `remote.auth`.

**There is no registry file to edit and no domain file to touch.** Discovery is a
pkgutil walk; the domain holds only the envelope. Verified end to end: a third
provider dropped into `app/providers/` registered itself, was picked up
automatically by the parametrised contract guards, and broke none of the eight
import contracts — with zero edits outside its own package.

A new *agent* on an existing platform is a config row and no code at all.

---

## What is enforced, and where

| Rule | Enforced by |
|---|---|
| Nothing above `app.providers` imports a platform package | `.importlinter` |
| The two platform packages do not import each other | `.importlinter` (×2) |
| `transport/` and `ws_flow/` import no platform | `.importlinter` |
| No platform NAME appears in code above the seam | `tests/guards/test_no_platform_names_above_providers.py` (AST-based, derived from the registry, so it needs no edit per platform) |
| Every provider declares the contract, strictly | `tests/guards/test_provider_contract.py` (parametrised over the registry) |
| A tenant's scoped config reaches its own endpoint | `tests/guards/test_provider_scope_isolation.py` |
| `.env.example` documents every setting | `tests/guards/test_env_example_is_current.py` |

The grep guard deliberately parses rather than greps: comments and docstrings
may name a platform — "Mitra returns `{'status': 200, …}`" is the sentence that
explains why a workaround exists — but nothing the program can branch on may.

---

## Bugs this closed

Each was live before the refactor and is fixed by the seam rather than patched:

* **`SaathiError` was caught nowhere.** It subclassed plain `Exception`, so a
  Saathi REST failure answered `500 INTERNAL` where the identical Mitra failure
  answered `502 UPSTREAM_UNAVAILABLE`. One shared `ProviderError` base makes that
  class of omission impossible.
* **Saathi channels were never closed.** Every close path reached one named
  platform's pool, so a Saathi socket outlived the session that owned it until
  the idle reaper noticed. `providers.close_conversation()` sweeps every pool.
* **`rest_for()` handed a Saathi agent a Mitra client.** Safe only by accident —
  both callers happened to be gated before they could reach it.
* **Saathi got no turn-timeout recovery.** The check read
  `agent_type != "remote_flow"`, which excluded it by construction; it is
  `provider.supports_recovery` now.
* **Nested spec models accepted typos in silence** (see two-stage validation).
* **One Saathi identity per process.** The token provider was a container-built
  singleton fed from `SAATHI_*` settings; it is per-connection now, so a
  tenant-scoped row naming its own variables gets its own token.
* **Two dead SQL bind parameters** in `capability_service`.
