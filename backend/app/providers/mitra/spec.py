"""Mitra's own `remote.options` schema.

Responsible for: the configuration only Mitra has -- its endpoint paths, the
company a profile belongs to, and the two finalisation switches.
Used by: the registry validates `remote.options` against `MitraOptions`; the
provider reads it.

THIS FILE IS WHY THE DOMAIN LAYER IS CLEAN. These models used to sit in
`app/domain/agent_spec.py`, which meant the import-pure domain layer carried a
third party's URL paths and a browser User-Agent string, duplicated from the
REST client because it could not import it. Both copies are gone: the paths live
here, next to the client that uses them.

WHICH FINALIZE ENDPOINT (v1 vs v2) IS CONFIGURATION, NOT A VERSION PREFERENCE,
and the choice has two independent consequences:

  * BOT RESOLUTION. v2 requires a row in Mitra's Flow table keyed on the flow
    route; a flow without one is a deterministic HTTP 500.
  * PDF RENDERER. Only v1 knows the chaupal (discussion) flow exists. A v2
    finalisation of a chaupal flow returns a story, a StoryMedia row, a 200 from
    get-story and a downloadable file that is COMPLETELY BLANK, with nothing
    logged anywhere.

`finalize_as_guest` is the same class of silent failure: Mitra derives
`auth = access_token is not None` and picks the PDF template's user_type from it,
so finalising with a token on a guest flow renders a blank PDF. It must match
what the socket authenticated as.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.providers.ws_flow.spec import WsFlowOptions

# A third-party API contract, not a preference: change these when Mitra moves an
# endpoint. Declared once, HERE, and read by both the client and the schema.
DEFAULT_PROFILE_PATH          = "/api/profile/"
DEFAULT_GENERATE_SESSION_PATH = "/api/generate-session/"
DEFAULT_CHAT_PATH             = "/api/companychat/"
DEFAULT_GET_STORY_PATH        = "/api/get-story/"
DEFAULT_FINALIZE_V1_PATH      = "/api/end-story/"
DEFAULT_FINALIZE_V2_PATH      = "/api/end-story/v2/"


class MitraPaths(BaseModel):
    """Mitra's REST surface, in one place, so no request method carries a literal."""

    model_config = ConfigDict(extra="forbid")

    profile:          str = DEFAULT_PROFILE_PATH
    generate_session: str = DEFAULT_GENERATE_SESSION_PATH
    chat:             str = DEFAULT_CHAT_PATH
    get_story:        str = DEFAULT_GET_STORY_PATH
    # The v1/v2 pair is load-bearing -- see the module docstring.
    finalize_v1:      str = DEFAULT_FINALIZE_V1_PATH
    finalize_v2:      str = DEFAULT_FINALIZE_V2_PATH

    def is_v2_finalize(self, path: str) -> bool:
        """Compared on the normalised path, so a trailing slash cannot change
        where the token goes."""
        return path.strip("/") == self.finalize_v2.strip("/")

    def is_known_finalize(self, path: str) -> bool:
        """Whether `path` is one of the configured finalize endpoints.

        A path matching neither would fall through to the v1 branch and finalise
        with the wrong body shape, which Mitra ACCEPTS.
        """
        return path.strip("/") in {
            self.finalize_v1.strip("/"), self.finalize_v2.strip("/"),
        }


class MitraOptions(WsFlowOptions):
    """Everything about a Mitra binding that only Mitra has.

    `company` is per-agent and per-tenant, not a global. Mitra identifies a
    profile by (email, company), so a process-wide value is the difference
    between every tenant sharing one Mitra profile and each having its own.
    """

    model_config = ConfigDict(extra="forbid")

    company: str = Field(min_length=1)
    paths:   MitraPaths = Field(default_factory=MitraPaths)

    # Optional because an agent configured with produces_artifact=false never
    # finalises and so has no endpoint to name. The cross-check between the two
    # lives in MitraProvider.validate_config.
    finalize_path:     Optional[str] = DEFAULT_FINALIZE_V2_PATH
    finalize_as_guest: bool = False

    # Whether the profile upsert carries the caller's ELEVATE profile alongside
    # the (email, company) pair that identifies it.
    #
    # DEFAULTS TO FALSE, AND THAT IS THE ISOLATION. Two agents share this class
    # -- `record_stories` and `capture_discussion` -- and a flag that defaulted
    # to on would change the story flow's wire body without anyone editing its
    # config. Only an agent that opts in sends anything extra.
    #
    # The opt-in is per-agent rather than global for the same reason `company`
    # is: a guest interview on one bot may want the user named in its report
    # while another deliberately stays anonymous, and that is a property of the
    # bot the agent is bound to, not of the deployment.
    send_user_profile: bool = False
