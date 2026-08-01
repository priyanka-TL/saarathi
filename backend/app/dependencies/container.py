"""Access to the composition root.

Flask kept it in `app.config["CONTAINER"]`; FastAPI's equivalent slot is
`app.state`. Built once in `create_app()`, never per request.
"""
from __future__ import annotations

from fastapi import Request

from app.core.container import Container


def get_container(request: Request) -> Container:
    return request.app.state.container
