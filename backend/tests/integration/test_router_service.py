import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.database.engine import SessionLocal
from app.domain.core import UserContext
from app.domain.agent_spec import LlmAgentSpec, ModelSpec, RoutingSpec, AccessSpec
from app.agents.protocol import TurnContext
from app.repositories.conversations import ConversationRepository
from app.repositories.sessions import AgentSessionRepository
from app.services.agent_registry import AgentRegistry, RegisteredAgent
from app.services.router_service import RouterService, AgentNotFound


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeAIMessage:
    def __init__(self, content):
        self.content = content


class _FakeLlm:
    def __init__(self):
        self.calls = []
        self._responses = []
        self._error = None

    def queue(self, text):
        self._responses.append(text)

    def queue_error(self, exc):
        self._error = exc

    def invoke(self, messages):
        self.calls.append(messages)
        if self._error is not None:
            err, self._error = self._error, None
            raise err
        if not self._responses:
            return _FakeAIMessage("{}")
        return _FakeAIMessage(self._responses.pop(0))


class _FakeLlmFactory:
    def __init__(self, llm: _FakeLlm):
        self._llm = llm

    def get(self, spec):
        return self._llm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_agent_row(session, db_key: str) -> uuid.UUID:
    """Inserts a real row purely to satisfy live FK constraints
    (agent_sessions.agent_id, conversation_messages.agent_id). The DB row's
    `key` column is a unique-per-call synthetic value, independent of the
    logical RegisteredAgent.key the test actually exercises RouterService
    logic against -- since RouterService never re-reads agents from the DB
    in these tests (registries are built directly in-memory), only the FK
    target needs to exist."""
    row = session.execute(text("""
        INSERT INTO agents (key, name, description, agent_type)
        VALUES (:key, :key, 'test agent', 'llm') RETURNING id
    """), {"key": db_key}).fetchone()
    return row[0]


def _make_agent(
    session,
    key: str,
    name: str = None,
    description: str = "test agent",
    is_default: bool = False,
    routing: RoutingSpec = None,
    access: AccessSpec = None,
) -> RegisteredAgent:
    agent_id = _insert_agent_row(session, f"{key}_{uuid.uuid4().hex[:8]}")
    spec = LlmAgentSpec(
        key=key,
        name=name or key,
        description=description,
        agent_type="llm",
        routing=routing or RoutingSpec(),
        access=access or AccessSpec(),
        model=ModelSpec(name="test/model"),
        prompt="test prompt",
    )
    return RegisteredAgent(
        id=str(agent_id), key=key, name=spec.name, description=description,
        agent_type="llm", is_default=is_default, checksum="test", spec=spec,
    )


def _build_registry(agents) -> AgentRegistry:
    reg = AgentRegistry()
    reg._snapshot = {a.key: a for a in agents}
    reg._legacy_names = {a.name: a.key for a in agents}
    reg._version = 1
    return reg


def _new_user(tenant_code: str = None) -> UserContext:
    return UserContext(
        user_id=f"user_{uuid.uuid4().hex[:8]}",
        email="test@example.com",
        display_name="Test User",
        tenant_code=tenant_code or f"TENANT_{uuid.uuid4().hex[:8]}",
    )


def _new_conversation(session, user=None):
    repo = ConversationRepository(session)
    return repo.get_or_create(None, user or _new_user())


def _ctx(text: str, user: UserContext = None, conversation_id=None, history=None) -> TurnContext:
    return TurnContext(
        request_id=f"req_{uuid.uuid4().hex[:8]}",
        conversation_id=conversation_id or uuid.uuid4(),
        user=user or _new_user(),
        text=text,
        option_id=None,
        history=history or [],
        session=None,
        locale="en",
    )


# ---------------------------------------------------------------------------
# Gate 1: explicit
# ---------------------------------------------------------------------------


def test_gate1_explicit_resolves_by_key():
    session = SessionLocal()
    try:
        agent = _make_agent(session, "billing", is_default=True)
        session.commit()
        registry = _build_registry([agent])
        svc = RouterService(session, registry, _FakeLlmFactory(_FakeLlm()))

        decision = svc.select(_new_conversation(session), _ctx("anything"), explicit_key="billing")
        assert decision.reason == "explicit"
        assert decision.agent.key == "billing"
        assert decision.confidence == 1.0
    finally:
        session.close()


def test_gate1_rejects_not_direct_selectable():
    session = SessionLocal()
    try:
        agent = _make_agent(session, "internal_only", is_default=True,
                             routing=RoutingSpec(direct_selectable=False))
        session.commit()
        registry = _build_registry([agent])
        svc = RouterService(session, registry, _FakeLlmFactory(_FakeLlm()))

        with pytest.raises(AgentNotFound):
            svc.select(_new_conversation(session), _ctx("anything"), explicit_key="internal_only")
    finally:
        session.close()


def test_gate1_unknown_key_raises_agent_not_found():
    session = SessionLocal()
    try:
        agent = _make_agent(session, "billing", is_default=True)
        session.commit()
        registry = _build_registry([agent])
        svc = RouterService(session, registry, _FakeLlmFactory(_FakeLlm()))

        with pytest.raises(AgentNotFound):
            svc.select(_new_conversation(session), _ctx("anything"), explicit_key="does_not_exist")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Gate 2: pin -- acceptance criteria 1 and 2
# ---------------------------------------------------------------------------


def test_pinned_turn_issues_zero_llm_calls():
    session = SessionLocal()
    try:
        agent = _make_agent(session, "record_stories", is_default=False,
                             routing=RoutingSpec(pin_session=True, exit_keywords=["/exit"]))
        default_agent = _make_agent(session, "general_support", is_default=True)
        conv = _new_conversation(session)
        session.commit()

        fake_llm = _FakeLlm()
        registry = _build_registry([agent, default_agent])
        svc = RouterService(session, registry, _FakeLlmFactory(fake_llm))

        # THE PIN IS THE OPEN SESSION. There is no conversations.pinned_agent_id
        # any more, so Gate 2 has exactly one source: the conversation's
        # non-terminal agent_sessions row.
        AgentSessionRepository(session).create_pending(conv.id, uuid.UUID(agent.id))
        session.commit()

        conv_stub = SimpleNamespace(id=conv.id)
        decision = svc.select(conv_stub, _ctx("Priya"), explicit_key=None)

        assert decision.reason == "pinned"
        assert decision.agent.key == "record_stories"
        assert len(fake_llm.calls) == 0, "a pinned turn must never call the LLM"
    finally:
        session.close()


def test_exit_keyword_releases_pin_and_abandons_session():
    session = SessionLocal()
    try:
        agent = _make_agent(session, "record_stories", is_default=False,
                             routing=RoutingSpec(pin_session=True, exit_keywords=["/exit"]))
        default_agent = _make_agent(session, "general_support", is_default=True)
        conv = _new_conversation(session)

        sessions_repo = AgentSessionRepository(session)
        open_session = sessions_repo.create_pending(conv.id, uuid.UUID(agent.id))
        session.commit()

        fake_llm = _FakeLlm()
        registry = _build_registry([agent, default_agent])
        svc = RouterService(session, registry, _FakeLlmFactory(fake_llm))

        conv_stub = SimpleNamespace(id=conv.id)
        decision = svc.select(conv_stub, _ctx("/exit"), explicit_key=None)
        session.commit()

        assert decision.reason == "exit_to_default"
        assert decision.unpinned is True
        assert decision.agent.key == "general_support"
        assert len(fake_llm.calls) == 0
    finally:
        session.close()

    verify = SessionLocal()
    try:
        # Abandoning the session IS the release: it is terminal, so Gate 2 no
        # longer finds it and the conversation routes freely again. That single
        # assertion replaces the old pair (pinned_agent_id IS NULL *and* the
        # session abandoned), which could disagree with each other.
        fresh_session = AgentSessionRepository(verify).get(open_session.id)
        assert fresh_session.state == "abandoned"
        assert AgentSessionRepository(verify).get_open_for_conversation(conv.id) is None
    finally:
        verify.close()


# ---------------------------------------------------------------------------
# Gate 3: keyword priority tie-break
# ---------------------------------------------------------------------------


def test_gate3_keyword_highest_priority_wins():
    session = SessionLocal()
    try:
        low = _make_agent(session, "low_priority", routing=RoutingSpec(keywords=["invoice"], priority=10))
        high = _make_agent(session, "high_priority", routing=RoutingSpec(keywords=["invoice"], priority=90))
        default_agent = _make_agent(session, "general_support", is_default=True)
        session.commit()

        registry = _build_registry([low, high, default_agent])
        svc = RouterService(session, registry, _FakeLlmFactory(_FakeLlm()))

        decision = svc.select(_new_conversation(session), _ctx("I have a question about my invoice"), explicit_key=None)
        assert decision.reason == "keyword"
        assert decision.agent.key == "high_priority"
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Gate 4: LLM classify -- acceptance criteria 3 and 4
# ---------------------------------------------------------------------------


def test_substring_of_another_agent_name_resolves_correctly():
    """'help' is a substring of 'help_desk' -- the old orchestrator.py:62-65
    bug would scan `if name.lower() in category.lower()` and could match the
    wrong one depending on dict order. Exact-key JSON resolution must not."""
    session = SessionLocal()
    try:
        help_agent = _make_agent(session, "help", name="Help")
        help_desk_agent = _make_agent(session, "help_desk", name="Help Desk")
        default_agent = _make_agent(session, "general_support", is_default=True)
        session.commit()

        fake_llm = _FakeLlm()
        fake_llm.queue('{"agent_key": "help", "confidence": 0.9}')
        registry = _build_registry([help_agent, help_desk_agent, default_agent])
        svc = RouterService(session, registry, _FakeLlmFactory(fake_llm))

        decision = svc.select(_new_conversation(session), _ctx("something ambiguous"), explicit_key=None)
        assert decision.reason == "llm"
        assert decision.agent.key == "help", "must resolve to the exact key, not the first substring match"
    finally:
        session.close()


def test_sentence_phrased_classification_reply_resolves_correctly():
    session = SessionLocal()
    try:
        research = _make_agent(session, "research")
        default_agent = _make_agent(session, "general_support", is_default=True)
        session.commit()

        fake_llm = _FakeLlm()
        fake_llm.queue(
            'Sure! Based on the message, I would go with: '
            '{"agent_key": "research", "confidence": 0.87} Hope that helps!'
        )
        registry = _build_registry([research, default_agent])
        svc = RouterService(session, registry, _FakeLlmFactory(fake_llm))

        decision = svc.select(_new_conversation(session), _ctx("find me a video on this"), explicit_key=None)
        assert decision.reason == "llm"
        assert decision.agent.key == "research"
        assert decision.confidence == 0.87
    finally:
        session.close()


def test_low_confidence_falls_through_to_default():
    session = SessionLocal()
    try:
        research = _make_agent(session, "research", routing=RoutingSpec(confidence_threshold=0.8))
        default_agent = _make_agent(session, "general_support", is_default=True)
        session.commit()

        fake_llm = _FakeLlm()
        fake_llm.queue('{"agent_key": "research", "confidence": 0.3}')
        registry = _build_registry([research, default_agent])
        svc = RouterService(session, registry, _FakeLlmFactory(fake_llm))

        decision = svc.select(_new_conversation(session), _ctx("hmm"), explicit_key=None)
        assert decision.reason == "default"
        assert decision.agent.key == "general_support"
    finally:
        session.close()


def test_unparseable_output_falls_through_to_default():
    session = SessionLocal()
    try:
        default_agent = _make_agent(session, "general_support", is_default=True)
        session.commit()

        fake_llm = _FakeLlm()
        fake_llm.queue("I have no idea what you mean.")
        registry = _build_registry([default_agent])
        svc = RouterService(session, registry, _FakeLlmFactory(fake_llm))

        decision = svc.select(_new_conversation(session), _ctx("???"), explicit_key=None)
        assert decision.reason == "default"
    finally:
        session.close()


def test_llm_exception_falls_through_to_default():
    session = SessionLocal()
    try:
        default_agent = _make_agent(session, "general_support", is_default=True)
        session.commit()

        fake_llm = _FakeLlm()
        fake_llm.queue_error(RuntimeError("upstream exploded"))
        registry = _build_registry([default_agent])
        svc = RouterService(session, registry, _FakeLlmFactory(fake_llm))

        decision = svc.select(_new_conversation(session), _ctx("hello"), explicit_key=None)
        assert decision.reason == "default"
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Gate 4: per-caller visibility -- acceptance criterion 5
# ---------------------------------------------------------------------------


def test_callers_in_different_orgs_get_different_prompts():
    session = SessionLocal()
    try:
        org_a_only = _make_agent(
            session, "org_a_special", description="handles org A special requests",
            access=AccessSpec(organization_codes=["ORG_A"]),
        )
        general = _make_agent(session, "general_support", is_default=True)
        session.commit()

        registry = _build_registry([org_a_only, general])
        fake_llm = _FakeLlm()
        svc = RouterService(session, registry, _FakeLlmFactory(fake_llm))

        from app.domain.core import OrgMembership
        user_a = UserContext(
            user_id="u_a", email="a@example.com", display_name="A", tenant_code="T",
            orgs=(OrgMembership(org_id="1", org_code="ORG_A", roles=()),), active_org_id="1",
        )
        user_b = UserContext(
            user_id="u_b", email="b@example.com", display_name="B", tenant_code="T",
            orgs=(OrgMembership(org_id="2", org_code="ORG_B", roles=()),), active_org_id="2",
        )

        fake_llm.queue('{"agent_key": "general_support", "confidence": 0.9}')
        svc.select(_new_conversation(session, user_a), _ctx("hi", user=user_a), explicit_key=None)
        prompt_a = svc._cached_prompt
        cache_key_a = svc._cache_key

        fake_llm.queue('{"agent_key": "general_support", "confidence": 0.9}')
        svc.select(_new_conversation(session, user_b), _ctx("hi", user=user_b), explicit_key=None)
        prompt_b = svc._cached_prompt
        cache_key_b = svc._cache_key

        assert cache_key_a != cache_key_b
        assert "org_a_special" in prompt_a
        assert "org_a_special" not in prompt_b
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Gate 5: default always available
# ---------------------------------------------------------------------------


def test_no_pin_no_keyword_no_llm_response_lands_on_default():
    session = SessionLocal()
    try:
        default_agent = _make_agent(session, "general_support", is_default=True)
        session.commit()

        registry = _build_registry([default_agent])
        svc = RouterService(session, registry, _FakeLlmFactory(_FakeLlm()))

        decision = svc.select(_new_conversation(session), _ctx("random unrouted text"), explicit_key=None)
        assert decision.reason == "default"
        assert decision.agent.key == "general_support"
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Gate 2: a pin that YIELDS
#
# THE BUG THESE EXIST FOR. An open-ended assistant never reports completion, so
# its session never becomes terminal and Gate 2 pinned the conversation to it
# for good. Observed live: one `saathi` session sat `awaiting_user` for twelve
# turns and swallowed every later request, including explicit asks for another
# agent -- of six such sessions in the database, ZERO had ever completed, while
# the two interview agents completed routinely.
#
# The release must NOT apply to an interview: a user answering "I want to tell
# my story about attendance" is talking TO it, not asking to leave.
# ---------------------------------------------------------------------------


def _yield_setup(session, *, yields: bool, target_keywords=None, yield_confidence_threshold=None):
    """A pinned assistant plus a keyword-bearing target and a default."""
    pinned = _make_agent(
        session, "saathi", is_default=False,
        description="Open-ended per-user assistant with no fixed end.",
        routing=RoutingSpec(
            pin_session=True, exit_keywords=["/exit", "exit saathi"],
            keywords=["improvement plan", "action plan"], priority=80,
            yields_to_keyword=yields,
            yield_confidence_threshold=yield_confidence_threshold,
        ),
    )
    target = _make_agent(
        session, "record_stories", is_default=False,
        routing=RoutingSpec(
            pin_session=True, priority=90,
            keywords=target_keywords if target_keywords is not None else ["capture story", "my story"],
        ),
    )
    default_agent = _make_agent(session, "general_support", is_default=True)
    conv = _new_conversation(session)
    AgentSessionRepository(session).create_pending(conv.id, uuid.UUID(pinned.id))
    session.commit()
    return pinned, target, default_agent, conv


def test_a_yielding_pin_releases_on_a_keyword_with_no_llm_call():
    session = SessionLocal()
    try:
        _pin, _target, default_agent, conv = _yield_setup(session, yields=True)
        fake_llm = _FakeLlm()
        svc = RouterService(session, _build_registry([_pin, _target, default_agent]),
                            _FakeLlmFactory(fake_llm))

        decision = svc.select(SimpleNamespace(id=conv.id),
                              _ctx("I want to capture story about attendance"),
                              explicit_key=None)

        assert decision.reason == "keyword_yield"
        assert decision.agent.key == "record_stories"
        assert decision.unpinned is True
        assert len(fake_llm.calls) == 0, "the keyword path must stay deterministic"
    finally:
        session.close()


def test_a_yielding_pin_releases_on_the_classifier_when_no_keyword_matches():
    """THE REPORTED SENTENCE. `_kw_match` is a plain substring test, so
    "I wanted to capture a story" matches NONE of the target's keywords --
    "capture story" is not in "capture a story". A keyword-only rule would have
    left this pinned, which is why the classifier is the second step."""
    session = SessionLocal()
    try:
        _pin, target, default_agent, conv = _yield_setup(session, yields=True)
        assert not any(k in "i wanted to capture a story"
                       for k in target.spec.routing.keywords), "premise of this test"

        fake_llm = _FakeLlm()
        fake_llm.queue('{"agent_key": "record_stories", "confidence": 0.9}')
        svc = RouterService(session, _build_registry([_pin, target, default_agent]),
                            _FakeLlmFactory(fake_llm))

        decision = svc.select(SimpleNamespace(id=conv.id),
                              _ctx("I wanted to capture a story"), explicit_key=None)

        assert decision.reason == "llm_yield"
        assert decision.agent.key == "record_stories"
        assert decision.unpinned is True
    finally:
        session.close()


def test_an_unsure_classifier_STAYS_PINNED_rather_than_falling_to_the_default():
    """THE FLOOR, and the most important test here.

    Gates 3-5 fall through to the default agent when nothing is confident.
    Reusing that verbatim would drop an ordinary, slightly-ambiguous assistant
    turn onto General Support -- a worse bug than the one being fixed."""
    session = SessionLocal()
    try:
        pinned, target, default_agent, conv = _yield_setup(session, yields=True)
        fake_llm = _FakeLlm()
        fake_llm.queue('{"agent_key": "record_stories", "confidence": 0.2}')  # below 0.5
        svc = RouterService(session, _build_registry([pinned, target, default_agent]),
                            _FakeLlmFactory(fake_llm))

        decision = svc.select(SimpleNamespace(id=conv.id),
                              _ctx("hmm, what do you think?"), explicit_key=None)

        assert decision.reason == "pinned"
        assert decision.agent.key == "saathi"
        assert decision.unpinned is False
    finally:
        session.close()


# ---------------------------------------------------------------------------
# yield_confidence_threshold -- THE REPORTED SCENARIO.
#
# A quick-reply answer with no story content at all ("Yes, we've tried some
# things") cleared record_stories' own confidence_threshold (0.5, tuned for
# cheap first-message routing) and yielded the pin away unasked. These pin
# their own, stricter bar on the PINNED agent instead.
# ---------------------------------------------------------------------------


def test_a_stricter_yield_threshold_blocks_a_moderate_confidence_yield():
    """THE REPORTED BUG. 0.6 clears record_stories' own 0.5 threshold but not
    saathi's stricter 0.85 yield threshold, so the turn must stay pinned."""
    session = SessionLocal()
    try:
        pinned, target, default_agent, conv = _yield_setup(
            session, yields=True, yield_confidence_threshold=0.85)
        fake_llm = _FakeLlm()
        fake_llm.queue('{"agent_key": "record_stories", "confidence": 0.6}')
        svc = RouterService(session, _build_registry([pinned, target, default_agent]),
                            _FakeLlmFactory(fake_llm))

        decision = svc.select(SimpleNamespace(id=conv.id),
                              _ctx("Yes, we've tried some things"), explicit_key=None)

        assert decision.reason == "pinned"
        assert decision.agent.key == "saathi"
        assert decision.unpinned is False
    finally:
        session.close()


def test_a_stricter_yield_threshold_still_yields_on_high_confidence():
    """The stricter bar isn't a block -- an explicit, unambiguous request
    still clears it and yields normally."""
    session = SessionLocal()
    try:
        pinned, target, default_agent, conv = _yield_setup(
            session, yields=True, yield_confidence_threshold=0.85)
        fake_llm = _FakeLlm()
        fake_llm.queue('{"agent_key": "record_stories", "confidence": 0.9}')
        svc = RouterService(session, _build_registry([pinned, target, default_agent]),
                            _FakeLlmFactory(fake_llm))

        decision = svc.select(SimpleNamespace(id=conv.id),
                              _ctx("I want to record a story now"), explicit_key=None)

        assert decision.reason == "llm_yield"
        assert decision.agent.key == "record_stories"
        assert decision.unpinned is True
    finally:
        session.close()


def test_an_unset_yield_threshold_falls_back_to_the_candidates_own_threshold():
    """No `yield_confidence_threshold` on the pin (the default, `None`) must
    reproduce today's behaviour exactly: the candidate's own
    `confidence_threshold` (0.5) is still the bar."""
    session = SessionLocal()
    try:
        pinned, target, default_agent, conv = _yield_setup(session, yields=True)
        fake_llm = _FakeLlm()
        fake_llm.queue('{"agent_key": "record_stories", "confidence": 0.6}')
        svc = RouterService(session, _build_registry([pinned, target, default_agent]),
                            _FakeLlmFactory(fake_llm))

        decision = svc.select(SimpleNamespace(id=conv.id),
                              _ctx("Yes, we've tried some things"), explicit_key=None)

        assert decision.reason == "llm_yield"
        assert decision.agent.key == "record_stories"
    finally:
        session.close()


def test_the_yield_prompt_names_the_pinned_agent():
    """The classifier is told which agent it might be interrupting, and given
    the "short/ambiguous reply is a continuation" instruction -- the context
    that lets it tell "answering the current question" apart from "asking for
    something else"."""
    session = SessionLocal()
    try:
        pinned, target, default_agent, conv = _yield_setup(session, yields=True)
        fake_llm = _FakeLlm()
        fake_llm.queue('{"agent_key": "record_stories", "confidence": 0.2}')
        svc = RouterService(session, _build_registry([pinned, target, default_agent]),
                            _FakeLlmFactory(fake_llm))

        svc.select(SimpleNamespace(id=conv.id),
                  _ctx("Yes, we've tried some things"), explicit_key=None)

        assert len(fake_llm.calls) == 1
        system_prompt = fake_llm.calls[0][0].content
        assert pinned.name in system_prompt
        assert pinned.description in system_prompt
        assert "quick-reply question" in system_prompt
    finally:
        session.close()


def test_a_classifier_failure_stays_pinned_rather_than_surfacing():
    """Safer than Gate 4's fallback: the turn still reaches the agent the user
    was already talking to."""
    session = SessionLocal()
    try:
        pinned, target, default_agent, conv = _yield_setup(session, yields=True)
        fake_llm = _FakeLlm()
        fake_llm.queue_error(RuntimeError("router is down"))
        svc = RouterService(session, _build_registry([pinned, target, default_agent]),
                            _FakeLlmFactory(fake_llm))

        decision = svc.select(SimpleNamespace(id=conv.id),
                              _ctx("something unrelated"), explicit_key=None)

        assert decision.reason == "pinned"
        assert decision.agent.key == "saathi"
    finally:
        session.close()


def test_AN_INTERVIEW_IS_NEVER_HIJACKED():
    """The regression that matters most.

    `yields_to_keyword` defaults to False, so an interview keeps the absolute
    pin. A user answering it with a sentence that happens to contain another
    agent's keyword must stay exactly where they are -- re-routing them would
    destroy the run."""
    session = SessionLocal()
    try:
        pinned, target, default_agent, conv = _yield_setup(session, yields=False)
        fake_llm = _FakeLlm()
        svc = RouterService(session, _build_registry([pinned, target, default_agent]),
                            _FakeLlmFactory(fake_llm))

        for message in ("I want to capture story about attendance",
                        "I wanted to capture a story",
                        "my story is about the school"):
            decision = svc.select(SimpleNamespace(id=conv.id), _ctx(message),
                                  explicit_key=None)
            assert decision.reason == "pinned", message
            assert decision.agent.key == "saathi", message

        assert len(fake_llm.calls) == 0, "a non-yielding pin must never call the LLM"
    finally:
        session.close()


def test_a_yielding_pin_does_not_switch_to_ITSELF():
    """The pinned agent is excluded from its own candidates, so its own
    keywords cannot log a spurious yield to where the turn already was."""
    session = SessionLocal()
    try:
        pinned, target, default_agent, conv = _yield_setup(session, yields=True)
        fake_llm = _FakeLlm()
        fake_llm.queue('{"agent_key": "saathi", "confidence": 0.99}')
        svc = RouterService(session, _build_registry([pinned, target, default_agent]),
                            _FakeLlmFactory(fake_llm))

        # "improvement plan" is the PINNED agent's own keyword.
        decision = svc.select(SimpleNamespace(id=conv.id),
                              _ctx("let's revisit the improvement plan"), explicit_key=None)

        assert decision.reason == "pinned"
        assert decision.agent.key == "saathi"
        assert decision.unpinned is False
    finally:
        session.close()


def test_an_exit_keyword_still_wins_over_a_yield():
    session = SessionLocal()
    try:
        pinned, target, default_agent, conv = _yield_setup(session, yields=True)
        fake_llm = _FakeLlm()
        svc = RouterService(session, _build_registry([pinned, target, default_agent]),
                            _FakeLlmFactory(fake_llm))

        decision = svc.select(SimpleNamespace(id=conv.id), _ctx("exit saathi"),
                              explicit_key=None)

        assert decision.reason == "exit_to_default"
        assert decision.agent.key == "general_support"
        assert len(fake_llm.calls) == 0
    finally:
        session.close()


def test_a_yield_cannot_route_to_an_agent_the_caller_cannot_see():
    """The yield reuses `_visible`, so access control travels with it."""
    session = SessionLocal()
    try:
        pinned = _make_agent(
            session, "saathi", is_default=False,
            routing=RoutingSpec(pin_session=True, yields_to_keyword=True),
        )
        hidden = _make_agent(
            session, "record_stories", is_default=False,
            routing=RoutingSpec(keywords=["capture story"], priority=90),
            access=AccessSpec(tenant_codes=["SOME_OTHER_TENANT"], allow_anonymous=False),
        )
        default_agent = _make_agent(session, "general_support", is_default=True)
        conv = _new_conversation(session)
        AgentSessionRepository(session).create_pending(conv.id, uuid.UUID(pinned.id))
        session.commit()

        fake_llm = _FakeLlm()
        fake_llm.queue('{"agent_key": "record_stories", "confidence": 0.99}')
        svc = RouterService(session, _build_registry([pinned, hidden, default_agent]),
                            _FakeLlmFactory(fake_llm))

        decision = svc.select(SimpleNamespace(id=conv.id), _ctx("capture story"),
                              explicit_key=None)

        assert decision.agent.key == "saathi", "an invisible agent must not be yielded to"
        assert decision.reason == "pinned"
    finally:
        session.close()


def test_a_yield_never_lands_on_the_DEFAULT_agent():
    """Found by running the real thing: "tell me more about that plan" mid-
    assistant classified to general_support ABOVE threshold and would have been
    bounced out of the conversation it belonged to.

    A yield means "the user wants THAT agent". Wanting out of this one is a
    different intent that `_exit_to_default` already serves, so the default is
    not a yield target and an ambiguous turn stays where it is."""
    session = SessionLocal()
    try:
        pinned, target, default_agent, conv = _yield_setup(session, yields=True)
        fake_llm = _FakeLlm()
        fake_llm.queue('{"agent_key": "general_support", "confidence": 0.95}')
        svc = RouterService(session, _build_registry([pinned, target, default_agent]),
                            _FakeLlmFactory(fake_llm))

        decision = svc.select(SimpleNamespace(id=conv.id),
                              _ctx("tell me more about that plan"), explicit_key=None)

        assert decision.reason == "pinned"
        assert decision.agent.key == "saathi"
    finally:
        session.close()
