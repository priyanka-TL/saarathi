"""The per-request VoiceService.

Built even when voice is disabled: the service reports `enabled == False` and
every route turns that into 503 VOICE_DISABLED. Returning None instead would
make each handler check for it before it could ask.
"""
from __future__ import annotations

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.container import Container
from app.dependencies.container import get_container
from app.dependencies.db import get_db
from app.services.voice_service import VoiceService


def get_voice_service(
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> VoiceService:
    return VoiceService(
        session=db,
        bhashini=container.bhashini,
        object_store=container.object_store,
        settings=container.settings,
    )
