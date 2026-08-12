"""Builders for a valid `remote` block, in one place.

Every test that needs a delegated agent used to assemble the whole nested block
by hand, so the shape was written out a dozen times and each copy had to be
found and edited when it changed. These builders exist so the next such change
is one file.

They produce REAL values through the REAL models: `remote_spec()` returns a
validated `RemoteSpec`, so a test cannot accidentally assert against a shape the
application would reject.

CREDENTIALS ARE NAMED, NOT HELD, here as everywhere else -- `auth.credential_env`
names a variable, and `env_for()` gives you the environment that satisfies it.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.domain.agent_spec import RemoteFlowAgentSpec, RemoteSpec

#: The variables the fixtures below name. Tests that build a connection must set
#: these, because a missing credential is a ProviderConfigError by design.
MITRA_ORIGIN_ENV = "TEST_MITRA_ORIGIN_URL"
SAATHI_ORIGIN_ENV = "TEST_SAATHI_ORIGIN_URL"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def env_for(provider: str = "mitra") -> Dict[str, str]:
    """The environment variables the specs below name."""
    if provider == "saathi":
        return {SAATHI_ORIGIN_ENV: "https://saathi.test"}
    return {MITRA_ORIGIN_ENV: "https://mitra.test"}


def mitra_auth(**overrides: Any) -> Dict[str, Any]:
    return {"scheme": "origin_header", "credential_env": MITRA_ORIGIN_ENV, **overrides}


def saathi_auth(**overrides: Any) -> Dict[str, Any]:
    """Saathi's remote.auth is now IDENTICAL in shape to Mitra's: just the
    Origin-header credential. It used to also carry a token-minting scheme
    (elevate_login/static_token) -- removed along with app/providers/saathi/
    auth.py, since the caller's own logged-in UserContext.token is the
    credential now, never connection config.
    """
    return {"scheme": "origin_header", "credential_env": SAATHI_ORIGIN_ENV, **overrides}


def remote_dict(
    provider: str = "mitra",
    *,
    options: Optional[Dict[str, Any]] = None,
    **overrides: Any,
) -> Dict[str, Any]:
    """A valid `remote` block as a plain dict, ready to nest in a config."""
    if provider == "saathi":
        base: Dict[str, Any] = {
            "provider": "saathi",
            "transport": "websocket",
            "base_url": "https://saathi.test",
            "stream_url": "wss://saathi.test/ws/common/",
            "allowed_hosts": [],
            "headers": {"User-Agent": DEFAULT_USER_AGENT},
            "auth": saathi_auth(),
            "flow_name": "saathi",
            "produces_artifact": False,
            "options": {"bot_route": "/saathi-bot"},
        }
    else:
        base = {
            "provider": "mitra",
            "transport": "websocket",
            "base_url": "https://mitra.test",
            "stream_url": "wss://mitra.test/ws/common/",
            "allowed_hosts": ["files.mitra.test"],
            "headers": {"User-Agent": DEFAULT_USER_AGENT},
            "auth": mitra_auth(),
            "flow_name": "guest-mi-story",
            "produces_artifact": True,
            "options": {
                "bot_route": "/guided_guest",
                "company": "testco",
                "finalize_path": "/api/end-story/v2/",
            },
        }

    if options is not None:
        base["options"] = {**base["options"], **options}
    base.update(overrides)
    return base


#: Fields a test names at the top level that actually belong to the provider's
#: own `options` block. Routing them here rather than making every call site
#: nest them keeps the tests readable -- a test that cares about `finalize_path`
#: should say `finalize_path=...`, not build two levels of dict to say it.
_OPTION_FIELDS = frozenset({
    "bot_route", "company", "finalize_path", "finalize_as_guest",
    "completion_poll_every_turn", "ip_city", "ip_state", "ip_zip",
    "handshake", "paths",
})


def remote_spec(provider: str = "mitra", **kwargs: Any) -> RemoteSpec:
    """A validated RemoteSpec. Raises if the fixture itself drifts out of shape.

    Accepts option fields at the top level and files them into `options`, so a
    test reads `remote_spec(finalize_path="/api/end-story/")` rather than
    `remote_spec(options={"finalize_path": "/api/end-story/"})`.
    """
    options = dict(kwargs.pop("options", None) or {})
    for field in list(kwargs):
        if field in _OPTION_FIELDS:
            options[field] = kwargs.pop(field)
    return RemoteSpec(**remote_dict(provider, options=options, **kwargs))


def agent_spec_dict(
    key: str = "test_remote",
    provider: str = "mitra",
    *,
    options: Optional[Dict[str, Any]] = None,
    remote_overrides: Optional[Dict[str, Any]] = None,
    **overrides: Any,
) -> Dict[str, Any]:
    """A whole remote_flow AgentSpec as a dict, ready for POST .../config."""
    spec: Dict[str, Any] = {
        "schema_version": 1,
        "key": key,
        "name": key.replace("_", " ").title(),
        "description": f"A delegated agent backed by {provider}.",
        "agent_type": "remote_flow",
        "status": "enabled",
        "memory": {"strategy": "none"},
        "routing": {"pin_session": True},
        "remote": remote_dict(provider, options=options, **(remote_overrides or {})),
    }
    spec.update(overrides)
    return spec


def agent_spec(key: str = "test_remote", provider: str = "mitra", **kwargs: Any):
    return RemoteFlowAgentSpec(**agent_spec_dict(key, provider, **kwargs))


# ---------------------------------------------------------------------------
# Test doubles for the provider seam
# ---------------------------------------------------------------------------
#
# These stand in for a whole provider, which is what the core now talks to.
# They replaced a per-platform fake REST client plus a per-platform fake channel
# pool -- two doubles that had to be kept in step with two real classes, and
# that could not express "this platform finalises, that one does not" without a
# third.


class FakeProvider:
    """Records what the core asked of it, and answers with scripted values.

    `calls` is shared with the registry that handed this out, so a test can
    assert ORDERING across the whole finalisation sequence -- close before
    finalize, finalize before the artifact fetch -- which is the property the
    claim-then-submit design exists to guarantee.
    """

    name = "fake"
    stateful_transport = True
    supports_recovery = True
    produces_artifacts = True

    def __init__(self, calls: Optional[list] = None, *, name: str = "fake") -> None:
        self.name = name
        self.calls: list = calls if calls is not None else []

        self.open_session_calls: list = []
        self.turn_calls: list = []
        self.finalize_calls: list = []
        self.fetch_artifact_calls: list = []
        self.reconcile_calls: list = []
        self.close_calls: list = []

        #: What finalize() hands back, and what it raises instead if set.
        self.result_ref = "9931"
        self.artifact_url: Optional[str] = None
        self.finalize_error: Optional[Exception] = None

        #: What is_complete() reports, and what turn() replies.
        self.complete = False
        self.reply_text = "bot reply"
        self.reply_options: list = []
        self.reply_step: Optional[int] = None

        #: What reconcile() reports. None means "not applicable".
        self.reconciliation: Any = None

        self.remote_session_id = "remote-session-1"
        self.remote_profile_id = "remote-profile-1"

    # ---- RemoteProvider ------------------------------------------------

    def open_session(self, remote, session_view, user):
        from app.providers.protocol import SessionInit

        self.calls.append("open_session")
        self.open_session_calls.append((remote, session_view, user))
        return SessionInit(
            remote_session_id=session_view.remote_session_id or self.remote_session_id,
            remote_profile_id=session_view.remote_profile_id or self.remote_profile_id,
            remote_bot_route=(remote.options or {}).get("bot_route"),
        )

    def turn(self, remote, session_view, text, user, *, first_turn):
        from app.providers.protocol import ProviderTurn

        self.calls.append("turn")
        self.turn_calls.append((text, first_turn))
        return ProviderTurn(
            text=self.reply_text, options=list(self.reply_options), step=self.reply_step,
        )

    def is_complete(self, remote, session_view, user):
        self.calls.append("is_complete")
        return self.complete

    def finalize(self, remote, session_view, user):
        from app.providers.protocol import FinalizeResult

        self.calls.append("finalize")
        self.finalize_calls.append((
            session_view.remote_session_id,
            session_view.remote_profile_id,
            remote.flow_name,
            session_view.language,
            getattr(user, "token", None),
        ))
        if self.finalize_error is not None:
            raise self.finalize_error
        # The real providers fetch the artifact inside finalize(), because the
        # session may only be transitioned once. Mirrored here so the ordering
        # a test observes is the ordering production produces.
        return FinalizeResult(result_ref=self.result_ref, artifact_url=self.artifact_url)

    def fetch_artifact(self, remote, session_view):
        self.calls.append("fetch_artifact")
        self.fetch_artifact_calls.append(
            (session_view.remote_session_id, remote.report_media_type)
        )
        return self.artifact_url

    def reconcile(self, remote, session_view, sent_text, user):
        self.calls.append("reconcile")
        self.reconcile_calls.append(sent_text)
        return self.reconciliation

    def close_channel(self, conversation_id):
        self.calls.append("close")
        self.close_calls.append(conversation_id)

    def validate_config(self, remote, settings):
        return None


class FakeProviderRegistry:
    """Hands back one provider whatever the spec.

    Matches the real registry's contract where the core uses it: `.get(remote)`,
    `.enabled`, `.close_conversation()`.
    """

    def __init__(self, provider: Optional[FakeProvider] = None, *, calls: Optional[list] = None) -> None:
        self.calls: list = calls if calls is not None else []
        self.provider = provider or FakeProvider(self.calls)
        self.enabled = frozenset({self.provider.name, "mitra", "saathi"})
        self.get_calls: list = []

    def get(self, remote):
        self.get_calls.append(remote)
        return self.provider

    def is_enabled(self, name):
        return name in self.enabled

    def options_for(self, remote):
        return remote.options

    def close_conversation(self, conversation_id):
        self.provider.close_channel(conversation_id)

    def close_all(self):
        pass

    def stop_reapers(self):
        pass
