"""The single API router `main.py` mounts.

ORDER IS PART OF THE CONTRACT. Flask/Werkzeug ranked URL rules by specificity
regardless of registration order; FastAPI matches in registration order and
first match wins. The two orderings that matter:

* `admin` goes LAST, because it owns `/api/agents/{key}` which would otherwise
  shadow nothing here but is the more general pattern of the pair.
* Inside admin, `/api/agents/reload` and `/api/agents/{key}/config/versions`
  are declared before `/api/agents/{key}` and `/api/agents/{key}/config` --
  see the comment in app/routers/admin.py.

No `prefix=` on any sub-router: every path here is absolute and must stay so.
The deployment prefix (API_PREFIX, e.g. `/saarathi-service`) is applied exactly
once, by `app.include_router(api_router, prefix=...)` in main.py -- so there is
one place to change it and the route decorators never need to know.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.routers import admin, admin_config, agents, chat, conversations, sessions, ui, voice

api_router = APIRouter()

api_router.include_router(chat.router)
api_router.include_router(conversations.router)
api_router.include_router(agents.router)
api_router.include_router(sessions.router)
api_router.include_router(ui.router)
# All under /api/voice/, so it collides with nothing above and its position is
# for readability rather than to break a tie. It still goes before `admin`,
# like everything else.
api_router.include_router(voice.router)
# Before `admin`, per the ordering rule above. Its paths all live under
# /api/admin/, so they cannot collide with admin's /api/agents/{key} wildcard
# either way -- the position is for consistency, not to break a tie.
api_router.include_router(admin_config.router)
api_router.include_router(admin.router)
