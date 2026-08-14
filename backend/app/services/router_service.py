"""Agent routing: five gates, in order.

Responsible for: choosing which agent serves a turn.
Used by: OrchestrationService, once per turn.

    1 explicit selection from the UI
    2 session pin        -- zero LLM calls
    3 keyword pre-route  -- zero LLM calls
    4 LLM classifier     -- the only gate that can fail
    5 default            -- routing NEVER fails

Gate 4 falling through to Gate 5 is why a router outage degrades rather than
errors.
"""
from dataclasses import dataclass
from operator import itemgetter
from typing import List, Literal, Optional

import json_repair
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agents.protocol import HistoryTurn, TurnContext
from app.domain.agent_spec import ModelSpec
from app.exceptions.domain import AgentNotFound
from app.core import timing
from app.core.logger import get_logger
from app.repositories.conversations import ConversationRepository
from app.repositories.sessions import AgentSessionRepository
from app.services.agent_registry import AgentRegistry, RegisteredAgent
from app.services.session_service import SessionService

logger = get_logger("router_service")


@dataclass(frozen=True)
class RouteDecision:
    agent: RegisteredAgent
    reason: Literal[
        "explicit", "pinned", "keyword", "llm", "default", "exit_to_default",
        # A pinned session that GAVE THE TURN UP. Distinct from "keyword"/"llm"
        # so the turn log shows a displacement rather than an ordinary route.
        "keyword_yield", "llm_yield",
    ]
    confidence: float
    router_latency_ms: int
    unpinned: bool = False


# Declared in app/exceptions/domain.py; re-exported here for existing callers.


# Stripped from both ends of a candidate command before comparing it to an exit
# keyword, so "Stop." and "/exit!" still match while the comparison itself stays
# an exact one (see _is_exit for why precision matters here). Referenced by
# _normalise_command; its absence raised NameError on every Gate 2 turn.
_COMMAND_PUNCTUATION = " \t\r\n'\"`.,!?;:…-–—()[]{}"


def _as_text(msg: AIMessage) -> str:
    content = msg.content
    if isinstance(content, list):
        return " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content) if content else ""


#: Local alias kept so the five gates below stay one line each.
_ms = timing.elapsed_ms


class RouterService:
    def __init__(self, session, registry: AgentRegistry, llm_factory):
        self._session = session
        self._registry = registry
        self._llm_factory = llm_factory
        self._sessions_repo = AgentSessionRepository(session)
        self._conversations = ConversationRepository(session)
        self._session_service = SessionService(session)

        self._cache_key = None
        self._cached_prompt: Optional[str] = None

    # ------------------------------------------------------------------
    # The five gates
    # ------------------------------------------------------------------

    def select(self, conv, ctx: TurnContext, explicit_key: Optional[str]) -> RouteDecision:
        t0 = timing.start()

        # GATE 1 -- explicit selection from the UI
        if explicit_key:
            a = self._registry.get(explicit_key)  # accepts key OR legacy display name
            if not (a and a.spec.routing.direct_selectable):
                raise AgentNotFound(explicit_key)  # -> HTTP 404
            # An exit command must work even when the client names an agent.
            # The UI sends agent_key on EVERY message while a capability panel
            # is open, so a Gate-2-only exit check meant "/exit" was dead on the
            # one path users actually take -- it was only reachable after a
            # reload had cleared the client's agent key.
            if self._is_exit(ctx.text, a.spec.routing.exit_keywords):
                return self._exit_to_default(conv)
            return RouteDecision(a, "explicit", 1.0, 0)

        # GATE 2 -- session pin. THE FIX.
        pin = self._pin_for(conv)
        if pin:
            if self._is_exit(ctx.text, pin.spec.routing.exit_keywords):
                return self._exit_to_default(conv)
            if pin.spec.routing.yields_to_keyword:
                yielded = self._yield_from_pin(ctx, pin, t0)
                if yielded is not None:
                    return yielded
            return RouteDecision(pin, "pinned", 1.0, 0)  # <- ZERO LLM calls

        # GATE 3 -- deterministic keyword pre-route
        visible = self._visible(ctx.user)
        hits = [(a.spec.routing.priority, a) for a in visible
                if self._kw_match(ctx.text, a.spec.routing.keywords)]
        if hits:
            return RouteDecision(max(hits, key=itemgetter(0))[1], "keyword", 1.0, 0)

        # GATE 4 -- LLM router: history-aware, JSON output, exact key
        try:
            raw = self._invoke_router(ctx, visible)
            parsed = json_repair.loads(raw)
            key = parsed.get("agent_key") if isinstance(parsed, dict) else None
            conf = float(parsed.get("confidence", 0)) if isinstance(parsed, dict) else 0.0
            a = self._registry.get_by_key_exact(key)
            if (a and a.spec.routing.router_selectable
                    and conf >= a.spec.routing.confidence_threshold):
                return RouteDecision(a, "llm", conf, _ms(t0))
        except Exception as e:
            # Gate 4 is the only gate that can fail, and falling through to the
            # default agent hides that it did. The fields are what distinguish
            # "the router model is down" from "it answered but below threshold".
            logger.warning(
                "router failed, falling back: %s", e,
                extra={
                    "conversation_id": str(getattr(conv, "id", "")) or None,
                    "candidate_count": len(visible),
                    "latency_ms": _ms(t0),
                },
            )

        # GATE 5 -- default. Routing NEVER fails.
        return RouteDecision(self._registry.default(), "default", 0.0, _ms(t0))

    # ------------------------------------------------------------------
    # Gate 2 helpers
    # ------------------------------------------------------------------

    def _exit_to_default(self, conv) -> RouteDecision:
        """Abandon the open session and hand the turn to the default agent.
        Shared by Gate 1 and Gate 2 so an exit command behaves identically
        whether or not the client named an agent.

        Abandoning the session IS the unpin: the session moving to a terminal
        state is what makes _pin_for stop finding it. There used to be a second
        `conversations.unpin()` call here, which had to be kept in step with
        this one by hand.
        """
        self._session_service.abandon(conv.id, reason="user_exit")
        return RouteDecision(self._registry.default(), "exit_to_default", 1.0, 0, unpinned=True)

    def _yield_from_pin(
        self, ctx: TurnContext, pin: RegisteredAgent, t0,
    ) -> Optional[RouteDecision]:
        """Give the turn up when the user plainly wants a DIFFERENT agent.

        Only reached for an agent whose config sets `routing.yields_to_keyword`
        -- see RoutingSpec for why that defaults to False and must stay so. An
        interview never gets here, and a user answering it is never re-routed.

        WHY THIS EXISTS. An open-ended assistant has no completion signal, so
        its session never becomes terminal and Gate 2 would pin the conversation
        to it for good. Observed live: one `saathi` session sat `awaiting_user`
        for twelve turns and swallowed every later request, including an
        explicit ask for a different agent.

        THE FLOOR IS THE PINNED AGENT, NOT THE DEFAULT, and that is the whole
        care in this method. Gates 3-5 fall through to `registry.default()` when
        nothing is confident; reusing them verbatim here would drop an ordinary,
        slightly-ambiguous assistant turn onto General Support -- a worse bug
        than the one being fixed. Returning None means "stay pinned".

        TWO STEPS, cheap first:

          1. keyword, deterministic and free -- the same substring match Gate 3
             uses, over the same visibility-filtered candidates;
          2. the classifier, only when no keyword matched. Needed because the
             match is a plain substring test: the sentence that prompted this
             work, "I wanted to capture a story", matches NONE of
             record_stories' keywords ("capture story" is not in "capture a
             story"), so a keyword-only rule would not have fixed it.

        The pinned agent is excluded from the candidates throughout, so its own
        keywords cannot "switch" the conversation to itself and log a spurious
        yield.

        The session is deliberately NOT abandoned here. Returning a different
        agent is enough: `SessionService.open_for` already abandons the old
        session with reason="agent_switch" AND fires `on_displace`, which closes
        the orphaned WebSocket. Abandoning here would skip that and leak the
        socket until the idle reaper noticed.
        """
        # THE DEFAULT AGENT IS NOT A YIELD TARGET, and excluding it matters.
        # Observed while testing this: an ordinary follow-up mid-assistant --
        # "tell me more about that plan" -- classified to general_support ABOVE
        # its confidence threshold and would have been bounced out of the
        # conversation it belonged to. The floor below only guards against an
        # UNSURE classifier, not a confidently wrong one.
        #
        # A yield means "the user wants THAT agent". Wanting out of this one is
        # a different intent, and `_exit_to_default` already serves it. Staying
        # put costs nothing when the assistant could have answered anyway.
        candidates = [
            a for a in self._visible(ctx.user)
            if a.key != pin.key and not a.is_default
        ]
        if not candidates:
            return None

        # 1. Deterministic, zero LLM calls.
        hits = [(a.spec.routing.priority, a) for a in candidates
                if self._kw_match(ctx.text, a.spec.routing.keywords)]
        if hits:
            winner = max(hits, key=itemgetter(0))[1]
            logger.info(
                "pin yielded on a keyword",
                extra={"from_agent": pin.key, "to_agent": winner.key},
            )
            return RouteDecision(winner, "keyword_yield", 1.0, 0, unpinned=True)

        # 2. The classifier. Same underlying call Gate 4 makes, but told it is
        #    deciding whether to interrupt an ALREADY-PINNED conversation, and
        #    a miss means STAY PINNED rather than fall through to the default
        #    agent.
        try:
            raw = self._invoke_router(ctx, candidates, pin=pin)
            parsed = json_repair.loads(raw)
            key = parsed.get("agent_key") if isinstance(parsed, dict) else None
            conf = float(parsed.get("confidence", 0)) if isinstance(parsed, dict) else 0.0
            a = self._registry.get_by_key_exact(key)
            # THE PIN'S OWN BAR, NOT THE CANDIDATE'S. `confidence_threshold` is
            # tuned for cheap first-message routing (Gate 4); interrupting a
            # conversation already in progress is a higher-stakes call and, if
            # the pinned agent sets `yield_confidence_threshold`, must clear
            # that stricter bar instead.
            threshold = (pin.spec.routing.yield_confidence_threshold
                         if pin.spec.routing.yield_confidence_threshold is not None
                         else (a.spec.routing.confidence_threshold if a else 1.0))
            # MEMBERSHIP IN `candidates`, not merely a registry hit. The
            # registry is global; `candidates` is what THIS caller may see. A
            # key the classifier returned for an agent outside that set --
            # hallucinated, stale, or belonging to another tenant -- must not
            # become a route, and `get_by_key_exact` alone would let it.
            if (a is not None
                    and any(c.key == a.key for c in candidates)
                    and a.spec.routing.router_selectable
                    and conf >= threshold):
                logger.info(
                    "pin yielded on the classifier",
                    extra={"from_agent": pin.key, "to_agent": a.key, "confidence": conf},
                )
                return RouteDecision(a, "llm_yield", conf, _ms(t0), unpinned=True)
        except Exception as e:  # noqa: BLE001
            # Staying pinned is the safe outcome, so a router failure here is
            # strictly less serious than at Gate 4 -- the turn still gets the
            # agent the user was already talking to.
            logger.warning(
                "pin yield check failed, staying pinned: %s", e,
                extra={"agent_key": pin.key, "latency_ms": _ms(t0)},
            )

        return None

    def _pin_for(self, conv) -> Optional[RegisteredAgent]:
        """The agent currently driving this conversation, or None.

        ONE SOURCE: the conversation's open `agent_sessions` row.
        `uq_agent_sessions_one_open_per_conversation` guarantees there is at
        most one, so this cannot be ambiguous.

        This used to consult `conversations.pinned_agent_id` first and fall back
        to here. The two were written under identical conditions
        (`routing.pin_session` gates both `SessionService.open_for` and the pin)
        and cleared in the same four places, so the column could only ever agree
        with this row or be a bug -- and nothing at the DB level decided which.
        Reading the row that carries the invariant removes the second case.

        Returns None when the agent is no longer in the enabled registry
        snapshot (disabled since, or a remote_flow agent with MITRA_ENABLED=0),
        which correctly falls the turn through to Gate 3.
        """
        open_session = self._sessions_repo.get_open_for_conversation(conv.id)
        if open_session:
            return self._registry.get_by_id(str(open_session.agent_id))

        return None

    @staticmethod
    def _normalise_command(text: str) -> str:
        """Lowercase, collapse whitespace, drop surrounding punctuation."""
        stripped = (text or "").strip().strip(_COMMAND_PUNCTUATION).lower()
        return " ".join(stripped.split())

    @classmethod
    def _is_exit(cls, text: str, exit_keywords: List[str]) -> bool:
        """Exit only when the message IS the command -- never when it merely
        contains it.

        This was a substring test, and the effect was severe. Exit keywords
        default to ["/exit", "cancel", "stop"] (RoutingSpec), and
        capture_discussion inherits those bare defaults, so an ordinary
        interview answer silently aborted the interview. Verified live against
        the running app:

            "Children stopped coming to school"        -> session abandoned
            "The meeting was cancelled last week"      -> session abandoned
            "We discussed the bus stop near the school"-> session abandoned

        Each one abandoned the remote session, unpinned the conversation and
        rerouted to the default agent, which then answered plausibly -- so the
        user had no idea their interview had been destroyed and no report would
        ever be produced. record_stories was hit too, via "start over" inside
        "we had to start over".

        Word-boundary matching is not enough either: "we had to stop the
        dropouts" is a legitimate answer that contains the standalone word
        "stop". The failure is irreversible (the session is abandoned) while a
        miss is trivially recoverable (type the command alone, or press New
        chat), so this deliberately biases all the way to precision.
        """
        normalised = cls._normalise_command(text)
        if not normalised:
            return False
        return any(
            normalised == cls._normalise_command(kw)
            for kw in exit_keywords if kw and kw.strip()
        )

    # ------------------------------------------------------------------
    # Gate 3 helper
    # ------------------------------------------------------------------

    @staticmethod
    def _kw_match(text: str, keywords: List[str]) -> bool:
        lowered = text.lower()
        return any(kw.lower() in lowered for kw in keywords)

    # ------------------------------------------------------------------
    # Gate 4: visibility, prompt cache, LLM invocation
    # ------------------------------------------------------------------

    def _visible(self, user) -> List[RegisteredAgent]:
        """The registry snapshot is global; VISIBILITY is per-caller."""
        return [a for a in self._registry.routable() if a.spec.access.matches(user)]

    #: Ceiling on the classifier's reply. It emits one small JSON verdict
    #: (`{"agent_key": ..., "confidence": ...}`), so this is generous by an
    #: order of magnitude and exists to bound the REQUEST, not the answer.
    #:
    #: WITHOUT IT THE ROUTER 402s ON A FUNDED ACCOUNT. Omitting max_tokens does
    #: not mean "no limit" to OpenRouter -- it means "reserve credit for the
    #: model's whole context window", so a request that will really emit ~30
    #: tokens is priced at 65,536 and refused with
    #: `code: 402, limit_source: openrouter_credits` while the balance is
    #: healthy. Observed live: the same key, at the same moment, could afford
    #: 60,029 tokens of the cheap chat model and only 3,121 of the pricier
    #: router model -- remaining credit divided by each model's rate. The
    #: router failing takes the whole turn down with it, because the fallback
    #: agent is an LLM agent that 402s for the same reason, and /api/chat
    #: answers 502.
    ROUTER_MAX_TOKENS = 512

    def _router_model_spec(self) -> ModelSpec:
        # There is no YAML for "the router" -- it classifies, it doesn't answer.
        # Mirrors get_llm()'s own hardcoding (src/llm/__init__.py) and
        # config_mode_router.py's existing _router_model_spec().
        from app.core.settings import settings as config
        return ModelSpec(
            provider="openrouter",
            name=config.OPENROUTER_MODEL,
            temperature=0.0,
            max_tokens=self.ROUTER_MAX_TOKENS,
            timeout_s=config.LLM_TIMEOUT,
        )

    def _history_messages(self, history: List[HistoryTurn]):
        return [
            HumanMessage(content=h.content) if h.role == "user" else AIMessage(content=h.content)
            for h in history
        ]

    def _build_router_prompt(
        self, visible: List[RegisteredAgent], pin: Optional[RegisteredAgent] = None,
    ) -> str:
        default_agent = self._registry.default()
        lines = [
            "You are the router for the Saarthi assistant.",
            "Choose exactly one agent for the LATEST user message.",
            "Use the prior conversation for context -- a short reply such as a name",
            "or a number is usually a continuation of the previous agent's topic.",
        ]
        if pin is not None:
            # YIELD MODE. This is not a fresh routing decision -- the user is
            # already mid-conversation with `pin`, and the agents below are
            # NOT told about `pin` because it was deliberately excluded from
            # `candidates` (see _yield_from_pin). Naming it here is what lets
            # the classifier tell "answering the current question" apart from
            # "asking for something else" instead of always picking whichever
            # visible agent is the closest topical match.
            lines += [
                "",
                f'The user is currently mid-conversation with "{pin.name}": '
                f"{pin.description}",
                "Decide only whether the LATEST message is an explicit, unambiguous",
                "request for one of the agents below, unrelated to that ongoing",
                "conversation. A short or ambiguous reply -- a name, a number, an",
                "acknowledgement, an answer to a quick-reply question -- is normally a",
                "continuation of that conversation, not a request to leave it: return",
                "low confidence for every agent below in that case.",
            ]
        lines += [
            "",
            "Agents:",
        ]
        for a in visible:
            lines.append(f'- agent_key: "{a.key}"')
            lines.append(f"  name: {a.name}")
            lines.append(f"  handles: {a.description}")
            if a.spec.capabilities:
                lines.append(f"  capabilities: {', '.join(a.spec.capabilities)}")
        lines += [
            "",
            'Reply with ONLY a JSON object: '
            '{"agent_key": "<one of the keys above>", "confidence": <0.0-1.0>}',
        ]
        if default_agent:
            lines.append(f'If nothing fits, use "{default_agent.key}" with low confidence.')
        return "\n".join(lines)

    def _router_messages(
        self, ctx: TurnContext, visible: List[RegisteredAgent],
        pin: Optional[RegisteredAgent] = None,
    ):
        # Cache key is (registry version, visibility signature, pin) -- NOT
        # version alone. Two callers in different orgs must not share a
        # cached prompt, or one will be offered an agent they cannot reach;
        # the pin key keeps Gate 4's cached prompt from being reused for a
        # yield decision (a different prompt shape) and vice versa.
        cache_key = (self._registry.version, tuple(sorted(a.key for a in visible)),
                     pin.key if pin else None)
        if self._cache_key != cache_key:
            self._cached_prompt = self._build_router_prompt(visible, pin=pin)
            self._cache_key = cache_key

        return [
            SystemMessage(content=self._cached_prompt),
            *self._history_messages(ctx.history[-6:]),  # HISTORY-AWARE
            HumanMessage(content=ctx.text),
        ]

    def _invoke_router(
        self, ctx: TurnContext, visible: List[RegisteredAgent],
        pin: Optional[RegisteredAgent] = None,
    ) -> str:
        llm = self._llm_factory.get(self._router_model_spec())
        messages = self._router_messages(ctx, visible, pin=pin)
        # SEPARATE FROM THE SERVING AGENT'S "llm" STAGE. This call is routing
        # overhead, not the answer the user is waiting for, and a pinned turn
        # skips it entirely -- folding the two together would hide both facts.
        # `pin` is set only on the yield path, which is the case where a pinned
        # conversation pays for a classifier on every single turn.
        timing.count("router_llm_calls")
        with timing.stage("router_llm"):
            response = llm.invoke(messages)
        return _as_text(response)
