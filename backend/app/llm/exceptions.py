"""LLM failures, translated at the LiteLLM boundary.

WHY THIS EXISTS
---------------
There was no exception handling around LLM calls anywhere in the app.
`LlmAgentHandler` called `llm.invoke(...)` bare, and whatever LiteLLM raised
travelled all the way to the chat router's `except Exception` and became a
generic 500 INTERNAL. A rate limit, a context-length overflow, an expired API
key and a genuinely broken upstream were indistinguishable -- to the client,
which could not tell "retry in a moment" from "this will never work", and to
whoever was on call, who got the same 500 for all four.

These are the same shape as the hierarchies the other integrations already have
(`ProviderError`, `BhashiniError`, `StorageError`): raised at the boundary, so the
provider's own exception types never travel past `app/llm/`.

WHAT IS DELIBERATELY *NOT* HERE
-------------------------------
There is no `LLMProvider` interface, and this module is not a step toward one.
LiteLLM is already an abstraction over providers, and OpenRouter -- the only
provider configured -- is itself a multi-provider gateway. A third abstraction
over those two would buy nothing today. What was actually missing was the
ability to tell these four failures apart, which is what this module supplies.
"""
from __future__ import annotations

from typing import Optional

from app.exceptions.domain import SaarthiError


class LlmError(SaarthiError):
    """Base for every LLM-side failure. 502 unless a subclass says otherwise."""

    status_code = 502
    error_code = "LLM_UPSTREAM_ERROR"

    def __init__(self, detail: str = "", *, model: Optional[str] = None) -> None:
        super().__init__(detail or self.__class__.__name__)
        self.detail = detail
        self.model = model

    @property
    def public_message(self) -> str:
        # NEVER `str(self)`. A provider message can carry the request payload
        # back -- which is the user's prompt, and on this app that is a
        # teacher's question or an interview answer.
        return "The assistant is temporarily unavailable. Please try again."


class LlmRateLimited(LlmError):
    """The provider is throttling us. Retryable, and the client is told so."""

    status_code = 429
    error_code = "LLM_RATE_LIMITED"

    @property
    def public_message(self) -> str:
        return "The assistant is busy right now. Please try again in a moment."


class LlmContextExceeded(LlmError):
    """The prompt plus history exceeded the model's context window.

    NOT retryable as-is, and 400 rather than 502 for that reason: sending the
    identical request again produces the identical failure. This is the one
    LLM failure that is genuinely about the request rather than the upstream,
    and it is usually a `memory.history_turns` that is too generous for the
    configured model.
    """

    status_code = 400
    error_code = "LLM_CONTEXT_EXCEEDED"

    @property
    def public_message(self) -> str:
        return "This conversation has grown too long for the assistant to process. Start a new one to continue."


class LlmAuthError(LlmError):
    """The provider rejected our credentials. An operator problem, not a user one."""

    status_code = 502
    error_code = "LLM_AUTH_ERROR"

    @property
    def public_message(self) -> str:
        # 502 and a generic message on purpose: a 401 here would suggest to the
        # CLIENT that ITS credentials were bad, when it is ours that are.
        return "The assistant is not correctly configured. Please contact support."


class LlmTimeout(LlmError):
    """The provider did not answer within the spec's `timeout_s`."""

    status_code = 504
    error_code = "LLM_TIMEOUT"

    @property
    def public_message(self) -> str:
        return "The assistant took too long to respond. Please try again."


class LlmContentFiltered(LlmError):
    """The provider's content policy refused the prompt or the completion."""

    status_code = 422
    error_code = "LLM_CONTENT_FILTERED"

    @property
    def public_message(self) -> str:
        return "The assistant could not respond to that request."


def translate(exc: Exception, *, model: Optional[str] = None) -> LlmError:
    """Map a LiteLLM/provider exception onto this hierarchy.

    ORDER MATTERS. litellm's exception classes inherit from each other and from
    openai's -- `ContextWindowExceededError` is a `BadRequestError`, and several
    are `APIError` subclasses -- so the most specific checks have to come first
    or everything collapses into the generic case.

    Imports litellm INSIDE the function: this module is imported by
    `normalization.py` at class-definition time, and a module-level import here
    would make the exception hierarchy a hard import-time dependency of a file
    that only needs it when something has already gone wrong.

    Returns (rather than raises) so the caller writes `raise translate(exc)
    from exc` and keeps the original traceback attached.
    """
    try:
        import litellm
    except ImportError:  # pragma: no cover - litellm is a hard dependency
        return LlmError(str(exc), model=model)

    detail = f"{type(exc).__name__}: {exc}"

    # Most specific first -- see the docstring.
    if isinstance(exc, litellm.ContextWindowExceededError):
        return LlmContextExceeded(detail, model=model)
    if isinstance(exc, litellm.ContentPolicyViolationError):
        return LlmContentFiltered(detail, model=model)
    if isinstance(exc, (litellm.AuthenticationError, litellm.PermissionDeniedError)):
        return LlmAuthError(detail, model=model)
    if isinstance(exc, litellm.RateLimitError):
        return LlmRateLimited(detail, model=model)
    if isinstance(exc, (litellm.Timeout, litellm.APIConnectionError)):
        return LlmTimeout(detail, model=model)

    return LlmError(detail, model=model)
