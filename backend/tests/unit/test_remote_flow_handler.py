"""Unit tests for RemoteFlowAgentHandler, through a REAL provider.

No DB, no network: only plain dataclasses and hand-rolled fakes. Mirrors
tests/unit/test_llm_handler.py's style (real TurnContext/UserContext/
AgentSessionView, only the outermost dependencies faked).

THE PROVIDER IS REAL, and only its REST surface and channel pool are faked.
That is deliberate: the handler got smaller in this refactor -- profile
creation, the one re-establishment attempt and the completion poll all moved
behind the provider -- so a test that faked the whole provider would assert
almost nothing. Driving the real MitraProvider keeps every behaviour below
pinned where a user would actually experience it.
"""
from __future__ import annotations

import dataclasses
import uuid
from typing import List, Optional

import pytest

from app.agents.factory import HandlerDeps
from app.agents.protocol import AgentSessionView, SessionState, TurnContext, HistoryTurn
from app.agents.remote_flow_handler import RemoteFlowAgentHandler
from app.domain.core import UserContext
from app.domain.agent_spec import (
    RemoteFlowAgentSpec,
    RemoteSpec,
    RemoteTurnSpec,
    RoutingSpec,
)
from app.providers.connection import resolve_connection
from app.providers.errors import ProviderNotEnabled
from app.providers.mitra.provider import MitraProvider
from app.providers.mitra.spec import MitraOptions
from tests.provider_factories import MITRA_ORIGIN_ENV, remote_dict
from app.providers.errors import ProviderChannelClosed, ProviderTurnTimeout
from app.providers.ws_flow.frames import ParsedOption
from app.providers.transport.ws import BotTurn


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRestClient:
    def __init__(self):
        self.upsert_profile_calls = []
        self.generate_session_calls = 0
        self.is_session_completed_calls = []
        self._profile_id = "profile-1"
        self._session_id = "remote-sess-1"
        self._completed = False

    def upsert_profile(self, email, latest_flow_used, company):
        self.upsert_profile_calls.append((email, latest_flow_used, company))
        return self._profile_id

    def generate_session(self):
        self.generate_session_calls += 1
        return self._session_id

    def is_session_completed(self, session_id):
        self.is_session_completed_calls.append(session_id)
        return self._completed

    def finalize(self, *args, **kwargs):
        raise AssertionError("RemoteFlowAgentHandler must never call finalize()")

    def get_report(self, *args, **kwargs):
        raise AssertionError("RemoteFlowAgentHandler must never call get_report()")


class _FakeChannel:
    def __init__(self, responses):
        self._responses = list(responses)
        self.sent: List[tuple] = []

    def send_and_await_turn(self, text, timeout_s, idle_gap_s):
        self.sent.append((text, timeout_s, idle_gap_s))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeSessionManager:
    def __init__(self, channels):
        """`channels` is a list of _FakeChannel -- consumed by successive
        acquire()/reacquire() calls in order."""
        self._channels = list(channels)
        self.acquire_calls = []
        self.reacquire_calls = []

    def acquire(self, sess, conn, factory):
        self.acquire_calls.append((sess, conn))
        return self._channels.pop(0)

    def reacquire(self, sess, conn, factory):
        self.reacquire_calls.append((sess, conn))
        return self._channels.pop(0)


class _FakeClientRegistry:
    """Stands in for MitraClientRegistry: hands back one client whatever the
    connection, and records the connections it was asked for so a test can
    assert which endpoint the handler resolved."""

    def __init__(self, client):
        self._client = client
        self.requested = []

    def get(self, conn):
        self.requested.append(conn)
        return self._client


#: The endpoint the fixture spec below points at. `resolve_connection` reads it
#: off the spec, not off Settings -- there is no MITRA_* environment floor.
_BASE_URL = "https://mitra.example.com"


@dataclasses.dataclass
class _Settings:
    """The ONE thing `resolve_connection` still reads from Settings: the
    operator's SSRF ceiling. The credentials come from the variables the config
    row NAMES, not from a field here -- which is why adding a platform no
    longer adds a Settings field."""

    provider_host_ceiling: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _remote_spec(**overrides) -> RemoteFlowAgentSpec:
    fields = remote_dict(
        "mitra",
        options={"bot_route": "/test-bot-route", "company": "test-company",
                 "handshake": {"settle_ms": 10}},
    )
    fields["turn"] = RemoteTurnSpec(
        first_turn_timeout_ms=60000, turn_timeout_ms=45000, idle_gap_ms=8000,
    )
    fields["base_url"] = _BASE_URL
    # Merged rather than splatted alongside, so a test can override any of the
    # defaults above.
    fields.update(overrides)
    remote = RemoteSpec(**fields)
    return RemoteFlowAgentSpec(
        key="record_stories",
        name="Record Stories",
        description="test remote flow agent",
        agent_type="remote_flow",
        routing=RoutingSpec(pin_session=True, exit_keywords=["/exit"]),
        remote=remote,
    )


def _session(remote_session_id: Optional[str] = None, step: int = 0) -> AgentSessionView:
    return AgentSessionView(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        state=SessionState.pending if remote_session_id is None else SessionState.awaiting_user,
        remote_session_id=remote_session_id,
        remote_profile_id=None,
        remote_flow=None,
        remote_bot_route=None,
        language="en",
        step=step,
        turn_count=0,
        result_ref=None,
        report_url=None,
        error=None,
        state_data={},
    )


def _ctx(text: str, session: AgentSessionView, history=None) -> TurnContext:
    return TurnContext(
        request_id="req-1",
        conversation_id=session.conversation_id,
        user=UserContext(user_id="u1", email="user@example.com", display_name="U", tenant_code="t"),
        text=text,
        option_id=None,
        history=history or [],
        session=session,
        locale="en",
    )


def _provider(rest, pool, spec, settings=None):
    """A REAL MitraProvider with its REST surface and pool substituted.

    `__new__` rather than `__init__` because the real constructor would build a
    live RestTransport; every method under test below is the real one.
    """
    provider = MitraProvider.__new__(MitraProvider)
    options = MitraOptions(**spec.remote.options)
    provider._conn = resolve_connection(settings or _Settings(), spec.remote, options)
    provider._pool = pool
    provider.options = options
    provider._rest = rest
    return provider


class _Registry:
    """Stands in for ProviderRegistry, recording the specs it was asked for so
    a test can assert which endpoint the handler resolved."""

    def __init__(self, provider):
        self._provider = provider
        self.requested = []

    def get(self, remote):
        self.requested.append(remote)
        if self._provider is None:
            raise ProviderNotEnabled(remote.provider)
        return self._provider


def _deps(rest, sessions, settings=None, spec=None) -> HandlerDeps:
    spec = spec or _remote_spec()
    provider = (
        _provider(rest, sessions, spec, settings)
        if rest is not None and sessions is not None else None
    )
    return HandlerDeps(
        llm_factory=None,
        tool_registry=None,
        providers=_Registry(provider),
        settings=settings or _Settings(),
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # The credential the config row NAMES. Unset, resolving a connection is a
    # ProviderConfigError by design -- there is no global to fall back to.
    monkeypatch.setenv(MITRA_ORIGIN_ENV, "https://origin.example.com")


# ---------------------------------------------------------------------------
# Construction guards
# ---------------------------------------------------------------------------


def test_constructor_raises_if_the_provider_is_not_enabled():
    """ONE guard, not one per platform. This used to be two hand-written
    constructor checks -- one per platform, each naming its own settings --
    and a third platform would have added a third."""
    with pytest.raises(ProviderNotEnabled):
        RemoteFlowAgentHandler(_remote_spec(), _deps(rest=None, sessions=None))


def test_constructor_raises_without_a_provider_registry():
    deps = HandlerDeps(
        llm_factory=None, tool_registry=None, providers=None, settings=_Settings(),
    )
    with pytest.raises(RuntimeError):
        RemoteFlowAgentHandler(_remote_spec(), deps)


def test_handle_raises_if_ctx_session_is_none():
    rest, sessions = _FakeRestClient(), _FakeSessionManager([])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))
    ctx = _ctx("hi", session=_session())
    ctx = dataclasses.replace(ctx, session=None)
    with pytest.raises(RuntimeError):
        handler.handle(ctx)


# ---------------------------------------------------------------------------
# First turn
# ---------------------------------------------------------------------------


def test_first_turn_creates_profile_and_session():
    rest = _FakeRestClient()
    channel = _FakeChannel([BotTurn(text="Welcome!", options=[], step=1)])
    sessions = _FakeSessionManager([channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    ctx = _ctx("I want to record a story", session=_session(remote_session_id=None))
    turn = handler.handle(ctx)

    assert rest.upsert_profile_calls == [("user@example.com", "guest-mi-story", "test-company")]
    assert rest.generate_session_calls == 1
    assert turn.session_delta.remote_session_id == "remote-sess-1"
    assert turn.session_delta.remote_profile_id == "profile-1"
    assert turn.text == "Welcome!"


def test_first_turn_uses_first_turn_timeout():
    rest = _FakeRestClient()
    channel = _FakeChannel([BotTurn(text="hi", options=[], step=1)])
    sessions = _FakeSessionManager([channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    handler.handle(_ctx("go", session=_session(remote_session_id=None)))

    assert channel.sent[0][1] == 60.0  # first_turn_timeout_ms / 1000


def test_subsequent_turn_does_not_recreate_profile_or_session():
    rest = _FakeRestClient()
    channel = _FakeChannel([BotTurn(text="continuing", options=[], step=2)])
    sessions = _FakeSessionManager([channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    ctx = _ctx("Priya", session=_session(remote_session_id="already-set", step=1))
    turn = handler.handle(ctx)

    assert rest.upsert_profile_calls == []
    assert rest.generate_session_calls == 0
    assert channel.sent[0][1] == 45.0  # turn_timeout_ms / 1000
    assert turn.session_delta.remote_session_id == "already-set"


# ---------------------------------------------------------------------------
# Acceptance: exactly one re-establishment attempt on connection loss
# ---------------------------------------------------------------------------


def test_exactly_one_reestablishment_attempt_on_connection_loss():
    rest = _FakeRestClient()
    failing_channel = _FakeChannel([ProviderChannelClosed("dropped")])
    retry_channel = _FakeChannel([BotTurn(text="recovered", options=[], step=3)])
    sessions = _FakeSessionManager([failing_channel, retry_channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    ctx = _ctx("Priya", session=_session(remote_session_id="already-set", step=2))
    turn = handler.handle(ctx)

    assert len(sessions.acquire_calls) == 1
    assert len(sessions.reacquire_calls) == 1
    assert turn.text == "recovered"


def test_second_connection_loss_is_not_retried_again():
    rest = _FakeRestClient()
    failing_channel = _FakeChannel([ProviderChannelClosed("dropped")])
    also_failing_channel = _FakeChannel([ProviderChannelClosed("dropped again")])
    sessions = _FakeSessionManager([failing_channel, also_failing_channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    ctx = _ctx("Priya", session=_session(remote_session_id="already-set", step=2))
    with pytest.raises(ProviderChannelClosed):
        handler.handle(ctx)

    assert len(sessions.acquire_calls) == 1
    assert len(sessions.reacquire_calls) == 1  # not called a second time


def test_other_exceptions_are_not_retried():
    rest = _FakeRestClient()
    channel = _FakeChannel([ProviderTurnTimeout(step=1)])
    sessions = _FakeSessionManager([channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    ctx = _ctx("Priya", session=_session(remote_session_id="already-set"))
    with pytest.raises(ProviderTurnTimeout):
        handler.handle(ctx)
    assert len(sessions.reacquire_calls) == 0


# ---------------------------------------------------------------------------
# Acceptance: no history is ever sent
# ---------------------------------------------------------------------------


def test_no_history_is_ever_sent():
    rest = _FakeRestClient()
    channel = _FakeChannel([BotTurn(text="ok", options=[], step=1)])
    sessions = _FakeSessionManager([channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    poisoned_history = [
        HistoryTurn(role="user", content="SECRET_PRIOR_MESSAGE_MARKER", agent_key=None),
        HistoryTurn(role="assistant", content="ANOTHER_SECRET_MARKER", agent_key=None),
    ]
    ctx = _ctx("current message", session=_session(remote_session_id="already-set"), history=poisoned_history)
    handler.handle(ctx)

    assert channel.sent == [("current message", 45.0, 8.0)]
    for call_args in rest.upsert_profile_calls:
        assert "SECRET_PRIOR_MESSAGE_MARKER" not in str(call_args)
        assert "ANOTHER_SECRET_MARKER" not in str(call_args)


# ---------------------------------------------------------------------------
# Completion polling / terminal signaling
# ---------------------------------------------------------------------------


def test_completed_session_returns_terminal_true_and_awaiting_user_state():
    """The handler must NEVER put state=finalizing in its own delta -- that
    transition belongs exclusively to OrchestrationService._finalize's own
    claim_finalizing() call (design doc §4.7's idempotency guard, whose
    claimable set is only {in_progress, awaiting_user}). If the delta here
    set finalizing itself, orchestration.py's step 11 would apply that
    BEFORE calling _finalize(), so the claim would find the session already
    in 'finalizing' and fail -- wedging it there forever, since nothing
    ever revisits it. terminal=True alone is the correct, sufficient signal
    for OrchestrationService to call _finalize() next."""
    rest = _FakeRestClient()
    rest._completed = True
    channel = _FakeChannel([BotTurn(text="Thank you!", options=[], step=14)])
    sessions = _FakeSessionManager([channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    ctx = _ctx("last answer", session=_session(remote_session_id="already-set"))
    turn = handler.handle(ctx)

    assert turn.terminal is True
    assert turn.session_delta.state == SessionState.awaiting_user


def test_incomplete_session_returns_not_terminal_and_awaiting_user():
    rest = _FakeRestClient()
    rest._completed = False
    channel = _FakeChannel([BotTurn(text="tell me more", options=[], step=5)])
    sessions = _FakeSessionManager([channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    turn = handler.handle(_ctx("go on", session=_session(remote_session_id="already-set")))

    assert turn.terminal is False
    assert turn.session_delta.state == SessionState.awaiting_user


def test_completion_poll_every_turn_false_never_polls():
    rest = _FakeRestClient()
    channel = _FakeChannel([BotTurn(text="ok", options=[], step=1)])
    sessions = _FakeSessionManager([channel])
    spec = _remote_spec(options={"bot_route": "/test-bot-route", "company": "test-company",
                          "completion_poll_every_turn": False})
    handler = RemoteFlowAgentHandler(spec, _deps(rest, sessions, spec=spec))

    turn = handler.handle(_ctx("go", session=_session(remote_session_id="already-set")))

    assert rest.is_session_completed_calls == []
    assert turn.terminal is False


def test_options_are_normalised_from_bot_turn():
    rest = _FakeRestClient()
    parsed = [ParsedOption(id="en", label="English", value="en")]
    channel = _FakeChannel([BotTurn(text="pick one", options=parsed, step=1)])
    sessions = _FakeSessionManager([channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    turn = handler.handle(_ctx("go", session=_session(remote_session_id="already-set")))

    assert len(turn.options) == 1
    assert turn.options[0].id == "en"
    assert turn.options[0].label == "English"


# ---------------------------------------------------------------------------
# bot_route / company come straight off the spec
#
# This pair is what makes Mitra config per-tenant. Mitra identifies a profile by
# (email, company), so a tenant-scoped `company` is the difference between every
# tenant sharing one Mitra company and each having its own. They used to be read
# from os.environ via `bot_route_env` / `company_env`, which made them
# process-global; there is no environment lookup left on this path.
# ---------------------------------------------------------------------------


def test_bot_route_and_company_come_from_the_spec_not_the_environment(monkeypatch):
    # Deliberately set variables with the OLD names. Nothing may read them.
    monkeypatch.setenv("MITRA_COMPANY", "env-company")
    monkeypatch.setenv("MITRA_STORY_BOT_ROUTE", "/env-bot-route")
    rest = _FakeRestClient()
    sessions = _FakeSessionManager([_FakeChannel([BotTurn(text="hi", options=[], step=1)])])
    spec = _remote_spec(options={"bot_route": "/tenant_bot", "company": "tenant-company",
                                 "handshake": {"settle_ms": 10}})

    handler = RemoteFlowAgentHandler(spec, _deps(rest, sessions, spec=spec))
    turn = handler.handle(_ctx("go", session=_session(remote_session_id=None)))

    assert rest.upsert_profile_calls[0][2] == "tenant-company"
    assert turn.session_delta.remote_bot_route == "/tenant_bot"


def test_two_scopes_of_the_same_agent_use_their_own_companies():
    """The point of the whole change, at handler level: two configs of one
    agent key produce two handlers that talk to two Mitra companies."""
    profiles = []
    for company, route in (("tenant-a", "/bot_a"), ("tenant-b", "/bot_b")):
        rest = _FakeRestClient()
        sessions = _FakeSessionManager([_FakeChannel([BotTurn(text="hi", options=[], step=1)])])
        spec = _remote_spec(options={"company": company, "bot_route": route,
                                     "handshake": {"settle_ms": 10}})
        handler = RemoteFlowAgentHandler(spec, _deps(rest, sessions, spec=spec))
        turn = handler.handle(_ctx("go", session=_session(remote_session_id=None)))
        profiles.append((rest.upsert_profile_calls[0][2], turn.session_delta.remote_bot_route))

    assert profiles == [("tenant-a", "/bot_a"), ("tenant-b", "/bot_b")]


def test_an_empty_company_or_bot_route_is_rejected_at_validation():
    """Not deferred to Mitra: an empty company does not error there, it
    silently resolves the wrong CompanyBot or splits a user's profile."""
    import pydantic

    # Enforced by the PROVIDER's own options model now, which is where the
    # knowledge that these two exist belongs.
    from app.providers.mitra.spec import MitraOptions

    for field in ("company", "bot_route"):
        base = {"company": "c", "bot_route": "/r"}
        with pytest.raises(pydantic.ValidationError):
            MitraOptions(**{**base, field: ""})


# ---------------------------------------------------------------------------
# The connection the handler resolves is the one its scope configured
# ---------------------------------------------------------------------------


def test_a_connection_override_reaches_both_the_client_and_the_channel_pool():
    rest = _FakeRestClient()
    sessions = _FakeSessionManager([_FakeChannel([BotTurn(text="hi", options=[], step=1)])])
    spec = _remote_spec(
        base_url="https://tenant-remote.example.com",
        stream_url="wss://tenant-remote.example.com/ws/common/",
    )

    deps = _deps(rest, sessions, spec=spec)
    handler = RemoteFlowAgentHandler(spec, deps)
    handler.handle(_ctx("go", session=_session(remote_session_id="already-set")))

    # The provider was resolved for the tenant's endpoint...
    assert deps.providers.requested[0].base_url == "https://tenant-remote.example.com"
    # ...and the SAME connection reached the channel pool, so the socket and the
    # REST calls cannot disagree about which deployment this is.
    assert sessions.acquire_calls[0][1] is handler._provider._conn


def test_the_endpoint_comes_from_the_spec_not_from_settings():
    """There is no env floor left to fall through to: whatever the spec says is
    what the handler reaches, full stop."""
    rest = _FakeRestClient()
    sessions = _FakeSessionManager([_FakeChannel([BotTurn(text="hi", options=[], step=1)])])

    deps = _deps(rest, sessions)
    RemoteFlowAgentHandler(_remote_spec(), deps)

    assert deps.providers.requested[0].base_url == _BASE_URL


# ---------------------------------------------------------------------------
# Downloadable documents reach the turn
# ---------------------------------------------------------------------------


def test_documents_are_carried_from_the_provider_to_the_agent_turn():
    from app.providers.transport.frames import Attachment as ProviderAttachment

    rest = _FakeRestClient()
    channel = _FakeChannel([BotTurn(
        text="Your plan is ready.",
        options=[],
        attachments=[
            ProviderAttachment("plan", "pdf", "application/pdf", "https://files.mitra.test/p.pdf"),
            ProviderAttachment("plan", "docx", "application/msword", "https://files.mitra.test/p.docx"),
        ],
        step=1,
    )])
    sessions = _FakeSessionManager([channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    turn = handler.handle(_ctx("go", session=_session(remote_session_id="already-set")))

    assert [(a.format, a.url) for a in turn.attachments] == [
        ("pdf", "https://files.mitra.test/p.pdf"),
        ("docx", "https://files.mitra.test/p.docx"),
    ]
    assert turn.attachments[0].media_type == "application/pdf"
    # They are NOT options -- an option is click-to-reply and would post the
    # URL back to the agent as user input.
    assert turn.options == []


def test_a_turn_with_no_documents_carries_an_empty_list():
    rest = _FakeRestClient()
    channel = _FakeChannel([BotTurn(text="just talking", options=[], step=1)])
    sessions = _FakeSessionManager([channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    turn = handler.handle(_ctx("hi", session=_session(remote_session_id="already-set")))

    assert turn.attachments == []


def test_a_document_on_a_host_the_agent_does_not_allow_is_dropped():
    """End to end through the handler: `remote.allowed_hosts` is what decides
    whether a link is surfaced at all, and a rejected one costs the button
    rather than the turn."""
    from app.providers.transport.frames import Attachment as ProviderAttachment

    rest = _FakeRestClient()
    channel = _FakeChannel([BotTurn(
        text="Your plan is ready.",
        options=[],
        attachments=[
            ProviderAttachment("plan", "pdf", "application/pdf", "https://elsewhere.test/p.pdf"),
            ProviderAttachment("plan", "docx", "application/msword", "https://files.mitra.test/p.docx"),
        ],
        step=1,
    )])
    sessions = _FakeSessionManager([channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    turn = handler.handle(_ctx("go", session=_session(remote_session_id="already-set")))

    assert [a.format for a in turn.attachments] == ["docx"]
    assert turn.text == "Your plan is ready.", "the reply survives a dropped link"
