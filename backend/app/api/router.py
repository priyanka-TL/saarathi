"""The single API router main.py mounts.

Responsible for: aggregating the nine routers in one place.
Used by: create_app(), which applies API_PREFIX to this router exactly once.

ORDER IS PART OF THE CONTRACT -- FastAPI matches in registration order, first
match wins. `admin` goes LAST because it owns the general `/api/agents/{key}`.

No `prefix=` on any sub-router: paths here are absolute, and the deployment
prefix is applied once in main.py so the route decorators never need to know it.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.routers import (
    admin,
    admin_config,
    agents,
    chat,
    conversations,
    profile,
    sessions,
    ui,
    voice,
)

api_router = APIRouter()

api_router.include_router(chat.router)
api_router.include_router(conversations.router)
api_router.include_router(agents.router)
api_router.include_router(sessions.router)
api_router.include_router(ui.router)
# All three live under their own path prefixes, so none can collide with admin's
# /api/agents/{key} wildcard -- their position is consistency, not a tie-break.
api_router.include_router(voice.router)
api_router.include_router(profile.router)
api_router.include_router(admin_config.router)
api_router.include_router(admin.router)
