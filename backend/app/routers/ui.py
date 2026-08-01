"""Presentation config for the frontend shell.

New in the FastAPI port -- the Flask app had no equivalent, because the sidebar
cards were literal markup in templates/index.html.

WHAT THIS IS NOT: an agent registry. `GET /api/agents` answers the routable
agent catalogue from the database; this answers what the ADVANCED panel draws
and what clicking it does. The two are separate on purpose -- a capability card
can group several agents, name one that is not registered yet, or contain none
at all (SG Commons Portal), none of which the agent catalogue can express.

THE SINGLE SOURCE. The frontend keeps NO bundled copy of this document, so a
404 here means the ADVANCED panel renders no capability cards at all and the
Mitra interviews have no entry point in the UI.

A missing or malformed file is therefore a LOGGED WARNING and a 404, never a
500 and never a silent empty document -- the log line is the only thing that
distinguishes "the file is broken" from "the panel is meant to be empty".

It is still not a startup abort, unlike app/config/agents/*.yaml. The
distinction is what the failure costs: a broken file here degrades one panel
and is fixed by editing the file, while a bad agent YAML would let a
mis-registered agent route real traffic wrongly and MUST kill the process.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.core.logger import get_logger
from app.dependencies.identity import get_current_user
from app.exceptions.envelope import error_response
from app.utils.responses import json_response

router = APIRouter(tags=["ui"])

logger = get_logger("ui")

# Anchored to this file's location, not CWD -- the pattern app/core/bootstrap.py
# uses for the agent YAML, for the same reason: `make run`, uvicorn and pytest
# all start from different directories.
CAPABILITIES_YAML = Path(__file__).parent.parent / "config" / "ui" / "capabilities.yaml"


def _load_capabilities() -> Optional[dict[str, Any]]:
    """Read and parse the document, or None if it is missing or unusable.

    Read per request rather than cached at import: the file is a few hundred
    bytes, this route is called once per page load, and reading it live means
    an operator can edit the sidebar and reload the browser without restarting
    a process that must run as a single uvicorn worker.

    The blanket `except Exception` is deliberate and is the whole point of the
    module docstring -- see there.
    """
    try:
        with CAPABILITIES_YAML.open("r", encoding="utf-8") as fh:
            document = yaml.safe_load(fh)
    except FileNotFoundError:
        logger.warning("ui capabilities: %s not found; serving 404", CAPABILITIES_YAML)
        return None
    except Exception as exc:  # noqa: BLE001 -- presentation config must not 500
        logger.warning("ui capabilities: %s is unreadable (%s); serving 404", CAPABILITIES_YAML, exc)
        return None

    if not isinstance(document, dict) or not isinstance(document.get("capabilities"), list):
        logger.warning(
            "ui capabilities: %s does not contain a `capabilities` list; serving 404",
            CAPABILITIES_YAML,
        )
        return None

    return document


@router.get("/api/ui/capabilities", response_model=None)
def get_ui_capabilities(user=Depends(get_current_user)) -> JSONResponse:
    """The capability document, verbatim.

    NO `get_db`. This route touches no table, so it takes no connection and
    skips the registry-reload check in get_db -- which matters more than it
    looks: one request holds one thread and one connection for its whole
    lifetime (see app/main.py), and this one has no reason to consume either.

    A plain `def`, like every other endpoint; `tests/guards/
    test_no_async_endpoints.py` enforces that. The read is blocking file I/O
    and belongs in a worker thread.

    `response_model=None` for the house reason: the document is passed through
    unchanged, and a response model would reshape a config whose fields this
    service deliberately does not interpret.
    """
    document = _load_capabilities()
    if document is None:
        # The frontend treats this as "no server opinion" and uses config.js or
        # its bundled default. It is not an error state for the client, but the
        # body still uses the shared envelope so every non-admin 4xx in this
        # service has one shape.
        return error_response("No UI capability configuration", "NOT_FOUND", 404)
    return json_response(document)
