# saarathi-poc-new

Saarthi, migrated from Flask to **React + Vite** (frontend) and **FastAPI /
Python 3.10** (backend). Same product, same UI, same API contract — different
stack.

```
saarathi-poc-new/
├── backend/     FastAPI + SQLAlchemy 2.0 + Alembic   (see backend/README.md)
└── frontend/    React 19 + Vite 6 + React Router 7   (see frontend/README.md)
```

## What Saarthi does

Routes a user's chat turn to one of several YAML-configured "agents". Some
agents call an LLM (LangChain + LiteLLM via OpenRouter); others delegate the
entire turn to **Mitra**, an external interview/story platform, over REST plus
a persistent per-conversation WebSocket. This repo only ever calls Mitra's own
public API.

## Quick start

Needs Postgres 17 on `127.0.0.1:5432` and `uv` + Node 22.

```bash
# backend  ->  http://127.0.0.1:8000  (HOST/PORT from backend/.env)
cd backend
cp .env.example .env          # ONE config file: fill OPENROUTER_API_KEY, SAARTHI_STATIC_TOKEN
createdb saarthi_new
make install && make migrate && make run

# frontend ->  http://localhost:5173  (VITE_DEV_PORT from frontend/.env.development)
cd ../frontend
npm install && npm run dev
```

## Configuration

No URL, port or API path is hardcoded on either side, and each half has exactly
**one** config file.

| | Backend | Frontend |
|---|---|---|
| Config file | `backend/.env` (git-ignored) | `frontend/.env` (git-ignored) |
| Template | `backend/.env.example` | `frontend/.env.example` |
| Overridden by | real environment variables | `dist/config.js` → `window.__APP_CONFIG__` (runtime) |

```bash
API_PREFIX=/saarathi-service PORT=9000 make run   # env vars beat backend/.env
```

Neither side has per-environment files. A backend deployment differs by the
values in its `.env` or by injected environment variables. A frontend
deployment differs by `dist/config.js`, which is read at runtime and outranks
the build-time values — so one bundle can be promoted dev → QA → prod without
a rebuild.

**The service prefix must match on both sides.** `API_PREFIX=/saarathi-service`
in `backend/.env` and `VITE_API_PREFIX=saarathi-service` in `frontend/.env`
send every request to `<host>/saarathi-service/api/...`. Set one without the
other and every request 404s.

**The service prefix.** `API_PREFIX=/saarathi-service` on the backend mounts
the entire surface under that path — `POST /saarathi-service/api/chat`,
`/saarathi-service/healthz`, `/saarathi-service/docs`. The frontend's
`VITE_API_PREFIX` must match; it is joined to `VITE_API_BASE_URL` once, in
`src/config/env.js`, and handed to axios as its `baseURL`. Empty on both sides
(the development default) gives the plain `/api/...` paths.

**Repointing a built frontend.** Vite inlines `VITE_*` at build time, so
`dist/` would otherwise be frozen to one backend. `dist/config.js` (from
`frontend/public/config.js`) is read at runtime and wins over the build-time
values, so one artifact can be promoted dev → QA → prod by editing three lines
next to `index.html`. Blank values or a missing file fall back to the build-time
defaults.

## Verification

| Gate | Command | What it proves |
|---|---|---|
| A — verbatim copy | `cd backend && make verify-copy` | The 52 framework-agnostic files differ from the Flask originals **only** by the `src.*` → `app.*` rename |
| B — layering | `cd backend && make lint` | All four import-linter contracts hold, including the new "core must not import FastAPI/Starlette/Flask" |
| C — API parity | `cd backend && make parity` | 24 request cases produce identical status + body against both apps |
| D — test suite | `cd backend && make test` | 429 tests, with the Flask golden fixtures **unmodified** |
| E — frontend | `cd frontend && npm test` | The `busy` mutex, retry-label branch and transcript anchoring |

CSS parity is a `diff`: `diff ../saarathi-poc/static/css/style.css frontend/src/styles/style.css`
must print nothing.

## How the migration was done

Of ~7,100 lines of Python, **only 8 files touched Flask**. Everything else —
the turn pipeline, router, session state machine, config-sync, Mitra
integration, repositories, models — was copied byte-for-byte and had its import
lines rewritten. The `.importlinter` contracts that the original enforced are
what made that safe, and a fourth contract was added to keep it that way.

The frontend was rebuilt as components, but `style.css` was copied **verbatim**
(1,354 lines, byte-identical) rather than re-expressed, so the visual result is
not a reimplementation. Verified: **0 differing pixels out of 1.6 million**
across desktop (1440×900) and mobile (375×812).

## Deliberate divergences

Everything below is intentional and was verified not to affect any client.

| Change | Why |
|---|---|
| `GET /` and `/static/*` are gone; `GET /healthz` added | React serves the shell now |
| `GET /api/ui/capabilities` added | The sidebar's capability cards were literal markup in `templates/index.html` and are configuration now, served from `app/config/ui/capabilities.yaml`. The frontend keeps no copy, so this route is **required** for the ADVANCED panel to show anything. See `backend/app/routers/ui.py` |
| Malformed UUID path segments return a **JSON** 404 instead of Werkzeug's **HTML** 404 | Same status code; the frontend already treats 404 as "forget this conversation". A JSON API should not emit HTML |
| Unhandled exceptions return the standard JSON envelope instead of an HTML 500 | Same reason |
| Admin `config/{version}` with a non-integer returns JSON 400 instead of HTML 404 | Same reason; route has no frontend |
| A rejected credential returns JSON 401 instead of HTML 401 | Unreachable in practice: the Authenticator resolves identity once at startup, so a bad token fails the boot instead |
| `GET /api/agents/reload` returns 405 instead of 404 | Werkzeug ranked routes by specificity; FastAPI matches in registration order |
| CORS + `X-Request-ID` exposed | Required now that the frontend is a separate origin |
| `X-Request-ID` is sent lowercase | uvicorn normalises header names; HTTP header names are case-insensitive |
| One **bug fix**: the per-request DB connection is now pinned | See below |

### The one bug fixed rather than copied

`OrchestrationService.handle_turn` takes a **session-scoped** Postgres advisory
lock, commits mid-turn, runs a handler for up to 60s, then releases the lock.
A session-scoped lock belongs to the *connection*, and a plain SQLAlchemy
`Session` returns its connection to the pool on `commit()`.

Single-threaded this is invisible — the pool hands the same connection back, so
the unlock lands correctly. Under real concurrency it does not: another worker
can take that connection in the window between the commit and the unlock, so
the unlock silently no-ops and **the original connection holds the advisory
lock forever**, poisoning that conversation with permanent 409s.

This is latent in the Flask original (identical SQLAlchemy setup, identical
orchestration code) but its dev server never ran turns concurrently. Under
uvicorn's 16-thread pool it reproduced on the first run —
`tests/guards/test_turn_concurrency.py` catches it. The fix binds the request's
`Session` to an explicitly checked-out `Connection` for the whole request
(`app/dependencies/db.py`). No business logic changed.

## Architecture note: everything is synchronous

Every endpoint and dependency is a plain `def`, never `async def`, so Starlette
runs it in a worker thread. That is required, not stylistic — the advisory
lock, `threading.Lock`, `queue.Queue`, `requests`, the sync SQLAlchemy engine
and an LLM client whose `_agenerate` *raises* all need a real thread.

The consequence is a hard coupling: **one request = one thread = one DB
connection, held for the whole turn**. `THREADPOOL_SIZE` must stay ≤
`DB_POOL_SIZE`, and with `MITRA_ENABLED=1` the process must run a **single
uvicorn worker** (the WebSocket channel pool lives in process memory). Both are
enforced at startup and by `tests/guards/`.
