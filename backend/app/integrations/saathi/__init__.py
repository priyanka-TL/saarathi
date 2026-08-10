"""The Saathi integration.

Responsible for: everything that talks to the Saathi deployment -- token
minting, the REST surface, and the WebSocket channel.
Used by: SaathiFlowAgentHandler and the turn pipeline.

Saathi runs the SAME Django application as Mitra (project `shikshalokam_mohini`:
the same `ws/common/` consumer, the same `/api/generate-session/`,
`/api/companychat/` and `/api/companybot/`). It is a separate package because
two things differ, and both fail hard rather than degrade:

  * AUTH IS PER-USER. The WebSocket carries a real ELEVATE JWT and the server
    derives the profile FROM IT, discarding the `profileid` in the frame. Mitra
    sends `access_token: None`, which Saathi answers with `auth_error` and an
    immediate close.
  * NOTHING IS FINALISED. `/api/flow-connection-info/?flow_route=saathi` reports
    `create_story: "none"`, so there is no story, no PDF and no finalize call.

The frame vocabulary is identical, so `app/integrations/mitra/frame_parser.py`
is reused rather than copied.

Deliberately exports nothing: submodules are imported directly, so a deployment
with SAATHI_ENABLED=0 never builds a token provider or a socket.
"""
