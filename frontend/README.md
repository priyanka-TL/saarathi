# Saarthi frontend (React 19 + Vite 6)

A single-screen chat client. Ported from a 1,344-line vanilla-JS file and one
Jinja template, with the stylesheet copied **byte-for-byte**.

## Setup

```bash
cp .env.example .env      # ONE config file
npm install
npm run dev               # http://localhost:5173 (APPLICATION_DEV_PORT)
npm run build             # -> dist/
npm test                  # vitest
```

The backend's `FRONTEND_ORIGINS` must list this origin, or the browser blocks
every call.

## Configuration

Nothing is hardcoded — no backend host, port, or API path. Two sources, in
priority order, resolved once in `src/config/env.js`:

1. **`window.__APP_CONFIG__`**, set by `config.js` (from `public/config.js`,
   copied to the `dist/` root). Read at **runtime**, so one built bundle can be
   promoted dev → QA → prod by editing three lines next to `index.html` — no
   rebuild. A blank value or a missing file falls through to (2).
2. **`import.meta.env.APPLICATION_*`**, from `.env` at **build** time. Vite
   inlines these into the bundle, which is why they alone cannot repoint a
   deployed artifact — and why one `.env` is enough: environments differ
   through `config.js`, not through a second file.

| Variable | Meaning |
|---|---|
| `APPLICATION_API_BASE_URL` | Complete backend base URL, INCLUDING the service prefix and `/api/`, e.g. `http://127.0.0.1:8000/saarathi-service/api/`. Empty ⇒ same origin as the page |
| `APPLICATION_DEV_PORT` | Vite dev server port (must be in the backend's `FRONTEND_ORIGINS`) |

## Capabilities are configuration

The cards in the ADVANCED panel are **data**, not markup. Adding a capability,
reordering one, hiding one, disabling one or attaching another agent to one is
a **database** change, made through the backend's admin API
(`/api/admin/capabilities`) — no file edit, no rebuild, no restart.

**`GET /api/ui/capabilities` is the only source.** The frontend keeps no
bundled catalogue and reads no override from `config.js` — one place a
capability can be defined, so the sidebar cannot disagree with the backend or
go stale against it.

**The response is resolved by tenant/organization scope on the backend**, but
in this deployment every browser session resolves to the SAME identity —
identity is decided once from the backend's own `.env`, not per request, since
this frontend has no login flow and sends no credential of its own (see
`backend/app/dependencies/identity.py`). So two people opening this app right
now will always see the same cards; per-tenant differences only show up
through the backend admin API acting on an explicit scope, or once this
frontend gains a real per-user identity to send.

The consequence is deliberate and worth knowing before you debug it: **with the
backend unreachable, or the route 404ing, the panel renders no capability
cards.** An empty panel is honest about the backend being down; a bundled
fallback would draw buttons whose `POST /api/reset` is going to fail anyway.
`agents` and `conversations` already degrade to empty the same way.

`src/config/capabilities.js` documents the document shape and holds the two
closed sets that are code rather than config; `src/hooks/useCapabilities.js`
does the fetching.

Only `id` + `title` are required (`id` + `label` for an agent). `status` is
`enabled` | `disabled` | `coming_soon`, `visible: false` removes an entry, and
`order` defaults to declaration position.

**`action.type` is a closed set**, so configuration selects a behaviour and can
never inject one:

| type | Behaviour |
|---|---|
| `start_agent` | full reset → pin the agent → autostart. Needs `agentKey` |
| `display_card` | sets the header banner only; never routes |
| `coming_soon` | raises the sidebar toast. No navigation, no request |
| `none` | inert |

The table lives in `ChatPage`. A new capability reusing an existing type is a
pure data change; a genuinely new *kind* of action is one entry there.

`src/config/capabilitySchema.js` normalises the response before a component
sees it and **never throws**: a single bad entry is dropped, an unknown
`status` becomes `enabled`, an unknown `action.type` becomes `none`, and an
unknown `icon` falls back to a default glyph. This config arrives from a file
an operator edits at deploy time, so a typo must cost one wrong card, not the
whole panel.

The manual agent list underneath is a **separate** catalogue, from
`GET /api/agents`, and always was. The two are not merged — see
`SIDEBAR_HIDDEN_KEYS`.

`API_BASE_URL` (from `APPLICATION_API_BASE_URL`, or `config.js` at runtime) is
the axios `baseURL` directly — no prefix is appended to it. The paths
themselves live in `src/api/endpoints.js` and stay relative. So repointing the
app is a base-URL change and touches no call site.

## Layout

```
src/
├── main.jsx  App.jsx  routes/  pages/  layouts/
├── styles/style.css      VERBATIM copy of the Flask stylesheet -- see below
├── styles/root.css       additive rules only (currently one)
├── api/                  http (axios) + endpoints (every path, in one place)
│                         + agents · conversations · chat · sessions
├── config/env.js         the ONE place a backend URL is resolved
├── config/capabilities.js + capabilitySchema.js
│                         the capability document's contract + validator.
│                         The DATA lives on the backend -- see below
├── constants/            storage keys, poll intervals, breakpoint, ALL user-visible copy
├── context/              ConversationContext -- the per-conversation state
├── hooks/                useChatMessages · useSendMessage · useConversation
│                         useSessionLifecycle · usePollRegistry · useAgents
│                         useRecentConversations
├── services/             resumeFlow (the 202 retry loop)
├── utils/                time · markdown · url · storage
└── components/           icons/ · sidebar/ · chat/ · common/
```

`assets/` is empty on purpose: every graphic is an inline SVG because
`stroke="currentColor"` picks up CSS variables, which an `<img src="*.svg">`
cannot do.

## style.css is a verbatim copy — keep it that way

```bash
diff ../../saarathi-poc/static/css/style.css src/styles/style.css   # must be empty
```

That `diff` is the CSS parity gate. Additive rules go in `root.css`, never in
`style.css`. The file carries a lot of load-bearing detail that would not
survive being re-expressed: a deliberate specificity trick
(`.message-content a.report-link:hover` exists to beat `.message-content
a:hover`; scope it and the download link goes purple-on-purple invisible), two
*different* collapse techniques (`max-height` for the Advanced panel, CSS-grid
`1fr→0fr` for the workflow banner), five `@keyframes`, a 24px graph-paper body
background, and one `@media (max-width: 768px)` block where the sidebar slides
in **from the right**.

### `#root { display: contents }`

The single most important line in `root.css`. `style.css` builds its height
chain as `body { height: 100vh } > .app-container { height: 100% }`. Vite
inserts `<div id="root">` between them, so `height: 100%` would resolve against
an auto-height box and the whole app would collapse to content height.
`display: contents` makes `#root` generate no box at all.

Verified: **0 differing pixels out of 1.6 million** against the Flask UI at
1440×900 and 375×812.

## Refs, not state

Most conversation state lives in refs (`ConversationContext`), because every
value is read inside an async callback mid-turn where a state read would be
stale. The important one:

**`busyRef` is a synchronous mutex.** `if (busy) return; busy = true;` must take
effect immediately. React `setState` is asynchronous and batched, so a
state-based guard lets two rapid clicks both through — and two user messages in
flight is exactly what Mitra merges into one, **destroying an answer with no
error surfaced anywhere**. The paired `isBusy` state exists only to disable the
textarea and show the typing indicator.

The capability buttons additionally hold a per-button `pendingRef`, because
their handler `await`s `resetConversation()` and the global mutex is still free
during that window.

## Behaviours that look like bugs and are not

* **The manual agent list renders empty.** `SIDEBAR_HIDDEN_KEYS` hides the
  three current agents — two are reached via the capability buttons, one is
  the router's default. Driven by live registry data, not dead code.
* **No backend ⇒ no capability cards.** The panel has no bundled fallback by
  design; `GET /api/ui/capabilities` is its only source. If the cards are
  missing, check that request before suspecting the config.
* **The `.highlight-*` rules in `style.css` are now unused.** SG Commons Portal
  renders as a capability card. They stay because `style.css` is byte-for-byte.
* **Dark mode is unreachable.** The CSS is complete, but the toggle is
  commented out in the original markup. `localStorage.theme` is still read at
  boot, so a previously stored preference applies.
* **`Public Sans` is requested but never loaded**, so the wordmark renders in
  the fallback. Adding the font would change its metrics.
* **Enter and Send validate differently.** The textarea is `required`; clicking
  Send triggers native validation, while Enter dispatches a *synthetic* submit
  event which skips it. Use `dispatchEvent`, never `requestSubmit()` — the
  latter would run validation and silently change the Enter path.
* **The textarea does not re-measure on submit**, so it snaps back to one row.
* **Auto-scroll is unconditional** — no "user scrolled up" detection.
* **The sidebar list is never refetched**, only patched locally after a turn.
* **`<StrictMode>` is deliberately absent** (`main.jsx` says why): its dev-only
  double-invocation would double-fire the boot fetches and double-create
  conversations.

## marked + dompurify are pinned exactly

`marked@15.0.12` and `dompurify@3.4.12` — the exact versions jsDelivr was
serving to the Flask app. `marked` changed heading-id, mangling and smart-quote
defaults across majors, so an unpinned upgrade would silently alter every agent
reply. Only agent messages are rendered as HTML; everything else renders as
text, which React escapes.
