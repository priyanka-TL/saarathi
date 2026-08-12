"""The per-request ProfileService.

Built even when ELEVATE_BASE_URL is unset: the service reports
`available == False` and both routes turn that into 503 PROFILE_UNAVAILABLE.
Returning None instead would make each handler check for it before it could ask
-- the same choice app/dependencies/voice.py makes.

NO `get_db` HERE, unlike every other dependency in this package. The profile
lives in ELEVATE and Saarthi keeps no copy, so these routes touch no table.
Taking a session anyway would pin a pooled connection for the length of two
outbound HTTP calls.
"""
from __future__ import annotations

from fastapi import Depends

from app.core.container import Container
from app.dependencies.container import get_container
from app.services.profile_service import ProfileService


def get_profile_service(
    container: Container = Depends(get_container),
) -> ProfileService:
    return ProfileService(client=container.elevate)
