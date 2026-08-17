"""Test doubles for the characterisation suite.

These stand in for the two external dependencies the application reaches for at
runtime: the chat model (via ``app.llm.factory.LlmFactory.get``) and the search
provider (via ``app.tools.DDGS``). Both are replaced so the suite is
deterministic, offline, and free.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ScriptExhausted(AssertionError):
    """Raised when the model is invoked more times than the test scripted for.

    This is deliberately an error rather than a default response. A silent
    fallback would hide a wrong call count, which is exactly the kind of
    behavioural change this suite exists to detect.
    """


class ScriptedChatModel(BaseChatModel):
    """A chat model that returns pre-scripted responses, in order.

    One shared instance is returned by every ``LlmFactory.get()`` call, so the
    script is a single ordered queue across all five call sites (four agents
    plus the router). That makes call *ordering* explicit and assertable: on the
    router path, response 1 is the classification and response 2 is the agent
    reply.

    Satisfies the three things the real client must do for the app to work:

    1. Behave as a Runnable inside an LCEL chain.
    2. Support ``bind_tools`` --- ``LlmAgentHandler`` calls it for any spec that
       declares tools; ``BaseChatModel.bind_tools`` raises
       ``NotImplementedError`` by default.
    3. Record what it was asked, so tests can assert on the router prompt and on
       history growth.
    """

    # BaseChatModel is a pydantic model, so mutable state is declared as fields.
    responses: list[Any] = []
    calls: list[list[BaseMessage]] = []
    bound_tools: list[Any] = []

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    # -- scripting API ----------------------------------------------------

    def reset(self) -> None:
        """Clear the queue and all recorded calls. Run before every test."""
        self.responses.clear()
        self.calls.clear()
        self.bound_tools.clear()

    def queue(self, content: str) -> "ScriptedChatModel":
        """Queue a plain text reply."""
        self.responses.append(AIMessage(content=content))
        return self

    def queue_tool_call(
        self, name: str, args: dict | None = None, call_id: str = "call_1"
    ) -> "ScriptedChatModel":
        """Queue a reply that asks for one tool invocation."""
        self.responses.append(
            AIMessage(
                content="",
                tool_calls=[{"name": name, "args": args or {}, "id": call_id}],
            )
        )
        return self

    def queue_message(self, message: AIMessage) -> "ScriptedChatModel":
        """Queue a fully-formed message, for cases the helpers do not cover."""
        self.responses.append(message)
        return self

    def queue_error(self, exc: Exception) -> "ScriptedChatModel":
        """Queue an exception to be raised on the next invocation."""
        self.responses.append(exc)
        return self

    # -- BaseChatModel ----------------------------------------------------

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(list(messages))

        if not self.responses:
            raise ScriptExhausted(
                f"ScriptedChatModel invoked {len(self.calls)} time(s) but the test "
                f"queued fewer responses. Queue one response per expected LLM call."
            )

        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return ChatResult(generations=[ChatGeneration(message=nxt)])

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> "ScriptedChatModel":
        """Record the bound tools and return self.

        Returning ``self`` (rather than a ``RunnableBinding``) keeps the single
        shared script queue authoritative --- the test, not the binding, decides
        what comes back.
        """
        self.bound_tools.append(list(tools))
        return self

    # -- assertion helpers ------------------------------------------------

    def call_texts(self, index: int) -> list[str]:
        """Message contents of the n-th invocation, as strings."""
        return [
            m.content if isinstance(m.content, str) else str(m.content)
            for m in self.calls[index]
        ]

    def system_prompt(self, index: int = 0) -> str:
        """The system message of the n-th invocation."""
        return self.call_texts(index)[0]


class _FakeDDGSResults:
    """Canned results shaped exactly as ``src/tools.py`` expects to read them."""

    TEXT = [
        {
            "title": "Fake Result One",
            "href": "https://example.test/one",
            "body": "First fake body.",
        },
        {
            "title": "Fake Result Two",
            "href": "https://example.test/two",
            "body": "Second fake body.",
        },
    ]

    VIDEOS = [
        {"title": "Fake Video One", "content": "https://example.test/v1"},
        {"title": "Fake Video Two", "content": "https://example.test/v2"},
    ]


class FakeDDGS:
    """Stands in for ``ddgs.DDGS``.

    Returns canned result dicts so the *real* formatting logic in both tools
    still runs --- that formatting is part of what this suite characterises.
    """

    calls: list[tuple[str, str]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def text(self, query: str, max_results: int = 3, **kwargs: Any) -> Iterable[dict]:
        FakeDDGS.calls.append(("text", query))
        return _FakeDDGSResults.TEXT[:max_results]

    def videos(self, query: str, max_results: int = 3, **kwargs: Any) -> Iterable[dict]:
        FakeDDGS.calls.append(("videos", query))
        return _FakeDDGSResults.VIDEOS[:max_results]

    @classmethod
    def reset(cls) -> None:
        cls.calls.clear()
