"""LLM failures must be distinguishable from each other, and from a real bug.

Before this translation existed, `llm.invoke(...)` was called bare and whatever
LiteLLM raised travelled to the chat router's `except Exception` and became a
generic 500 INTERNAL. A rate limit, a context-length overflow, an expired API
key and a genuinely broken upstream were the same response -- so a client could
not tell "retry in a moment" from "this will never work", and neither could
anyone reading the logs.
"""
from __future__ import annotations

import litellm
import pytest

from app.exceptions.domain import SaarthiError
from app.llm.exceptions import (
    LlmAuthError,
    LlmContentFiltered,
    LlmContextExceeded,
    LlmError,
    LlmRateLimited,
    LlmTimeout,
    translate,
)


def _litellm(cls, **kwargs):
    """Build a litellm exception; they demand differing constructor args."""
    kwargs.setdefault("message", "upstream said no")
    kwargs.setdefault("model", "qwen/qwen3.7-flash")
    kwargs.setdefault("llm_provider", "openrouter")
    return cls(**kwargs)


@pytest.mark.parametrize(
    "litellm_cls, expected, status, code",
    [
        (litellm.ContextWindowExceededError, LlmContextExceeded, 400, "LLM_CONTEXT_EXCEEDED"),
        (litellm.ContentPolicyViolationError, LlmContentFiltered, 422, "LLM_CONTENT_FILTERED"),
        (litellm.AuthenticationError, LlmAuthError, 502, "LLM_AUTH_ERROR"),
        (litellm.RateLimitError, LlmRateLimited, 429, "LLM_RATE_LIMITED"),
    ],
)
def test_each_provider_failure_maps_to_its_own_status_and_code(
    litellm_cls, expected, status, code
):
    translated = translate(_litellm(litellm_cls))

    assert isinstance(translated, expected)
    assert translated.status_code == status
    assert translated.error_code == code


def test_context_window_is_not_swallowed_by_bad_request():
    """ContextWindowExceededError SUBCLASSES BadRequestError in litellm.

    If the isinstance checks in translate() are ever reordered so the general
    case runs first, this collapses to the generic 502 and the one genuinely
    non-retryable LLM failure starts telling clients to try again.
    """
    translated = translate(_litellm(litellm.ContextWindowExceededError))

    assert isinstance(translated, LlmContextExceeded)
    assert translated.status_code == 400, "must not be reported as a retryable upstream error"


def test_an_unrecognised_provider_error_falls_back_to_a_502():
    translated = translate(RuntimeError("something new"))

    assert type(translated) is LlmError
    assert translated.status_code == 502
    assert translated.error_code == "LLM_UPSTREAM_ERROR"


def test_the_provider_message_never_reaches_the_client():
    """A provider error can echo the request payload back -- which on this app
    is a teacher's question or an interview answer. The detail is kept for the
    log; public_message must not carry it."""
    secret = "a user's private interview answer"
    translated = translate(_litellm(litellm.RateLimitError, message=secret))

    assert secret in translated.detail, "the detail is what the logs need"
    assert secret not in translated.public_message


def test_every_llm_error_is_a_domain_error():
    """So the single SaarthiError handler in app/exceptions/handlers.py catches
    them, and the chat router needs no LLM-specific clause."""
    assert issubclass(LlmError, SaarthiError)
    for cls in (LlmRateLimited, LlmContextExceeded, LlmAuthError, LlmTimeout):
        assert issubclass(cls, SaarthiError)


def test_the_model_is_carried_for_the_logs():
    translated = translate(_litellm(litellm.RateLimitError), model="openrouter/qwen")

    assert translated.model == "openrouter/qwen"
