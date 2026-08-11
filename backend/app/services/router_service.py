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
    reason: Literal["explicit", "pinned", "keyword", "llm", "default", "exit_to_default"]
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

    def _router_model_spec(self) -> ModelSpec:
        # There is no YAML for "the router" -- it classifies, it doesn't answer.
        # Mirrors get_llm()'s own hardcoding (src/llm/__init__.py) and
        # config_mode_router.py's existing _router_model_spec().
        from app.core.settings import settings as config
        return ModelSpec(
            provider="openrouter",
            name=config.OPENROUTER_MODEL,
            temperature=0.0,
            max_tokens=None,
            timeout_s=config.LLM_TIMEOUT,
        )

    def _history_messages(self, history: List[HistoryTurn]):
        return [
            HumanMessage(content=h.content) if h.role == "user" else AIMessage(content=h.content)
            for h in history
        ]

    def _build_router_prompt(self, visible: List[RegisteredAgent]) -> str:
        default_agent = self._registry.default()
        lines = [
            "You are the router for the Saarthi assistant.",
            "Choose exactly one agent for the LATEST user message.",
            "Use the prior conversation for context -- a short reply such as a name",
            "or a number is usually a continuation of the previous agent's topic.",
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

    def _router_messages(self, ctx: TurnContext, visible: List[RegisteredAgent]):
        # Cache key is (registry version, visibility signature) -- NOT version
        # alone. Two callers in different orgs must not share a cached prompt,
        # or one will be offered an agent they cannot reach.
        cache_key = (self._registry.version, tuple(sorted(a.key for a in visible)))
        if self._cache_key != cache_key:
            self._cached_prompt = self._build_router_prompt(visible)
            self._cache_key = cache_key

        return [
            SystemMessage(content=self._cached_prompt),
            *self._history_messages(ctx.history[-6:]),  # HISTORY-AWARE
            HumanMessage(content=ctx.text),
        ]

    def _invoke_router(self, ctx: TurnContext, visible: List[RegisteredAgent]) -> str:
        llm = self._llm_factory.get(self._router_model_spec())
        messages = self._router_messages(ctx, visible)
        response = llm.invoke(messages)
        return _as_text(response)
