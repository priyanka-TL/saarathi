"""Saathi's own `remote.options` schema.

Responsible for: the configuration only Saathi has -- its endpoint paths.
Used by: the registry validates `remote.options` against `SaathiOptions`.

DECLARES ITS OWN PATHS EVEN WHERE THEY MATCH ANOTHER PLATFORM'S. Saathi and
Mitra run the same Django application today, so `generate_session` and `chat`
happen to be identical strings. Sharing one model for that reason would make
either platform's endpoint move a change to the other's schema. Two platforms
agreeing on a path today must stay free to diverge tomorrow, and duplicating six
short strings is the cheap side of that trade.

WHAT SAATHI HAS NO OPTION FOR, and why:

  * `company` -- Saathi derives the profile from the access token, so there is
    no (email, company) pair to upsert against.
  * `finalize_path` / `finalize_as_guest` -- Saathi's own
    `/api/flow-connection-info/` reports `create_story: "none"`. There is
    nothing to submit, so naming an endpoint would imply otherwise.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.providers.ws_flow.spec import WsFlowOptions

#: Saathi's REST paths. Its own API contract, not a preference.
DEFAULT_PROFILE_PATH          = "/api/shikshalokam/read-elevate-profile/"
DEFAULT_ACCEPT_TNC_PATH       = "/api/accept-tnc/"
DEFAULT_GENERATE_SESSION_PATH = "/api/generate-session/"
DEFAULT_CHAT_PATH             = "/api/companychat/"


class SaathiPaths(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile:          str = DEFAULT_PROFILE_PATH
    accept_tnc:       str = DEFAULT_ACCEPT_TNC_PATH
    generate_session: str = DEFAULT_GENERATE_SESSION_PATH
    chat:             str = DEFAULT_CHAT_PATH


class SaathiOptions(WsFlowOptions):
    model_config = ConfigDict(extra="forbid")

    paths: SaathiPaths = Field(default_factory=SaathiPaths)
