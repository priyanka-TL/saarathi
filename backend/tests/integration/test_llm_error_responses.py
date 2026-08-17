"""An LLM failure must reach the client as itself, not as a generic 500.

These assert the HTTP end of the translation added in app/llm/exceptions.py.
The mapping from a provider exception to one of our types is unit-tested in
tests/unit/test_llm_exceptions.py; what is checked here is that the chat
router's `except SaarthiError` clause actually surfaces the status, the code and
the client-safe message -- the part that used to be a blanket 500.

NOT in tests/characterisation/: those pin the ORIGINAL Flask app's behaviour
against golden fixtures, and this is deliberately new behaviour.
"""
from __future__ import annotations

import pytest

from app.llm.exceptions import (
    LlmAuthError,
    LlmContextExceeded,
    LlmError,
    LlmRateLimited,
    LlmTimeout,
)
from tests.characterisation.conftest import DEFAULT_AGENT, chat


@pytest.mark.parametrize(
    "exc, expected_status, expected_code",
    [
        (LlmRateLimited("provider throttled us"), 429, "LLM_RATE_LIMITED"),
        (LlmContextExceeded("too many tokens"), 400, "LLM_CONTEXT_EXCEEDED"),
        (LlmAuthError("bad api key"), 502, "LLM_AUTH_ERROR"),
        (LlmTimeout("provider was slow"), 504, "LLM_TIMEOUT"),
        (LlmError("something else"), 502, "LLM_UPSTREAM_ERROR"),
    ],
)
def test_each_llm_failure_gets_its_own_status_and_code(
    client, script, exc, expected_status, expected_code
):
    script.queue_error(exc)

    status, body = chat(client, "hello", DEFAULT_AGENT)

    assert status == expected_status, f"{type(exc).__name__} should not be a {status}"
    assert body["error_code"] == expected_code
    assert body["status"] == "error"


def test_all_five_used_to_be_indistinguishable(client, script):
    """The regression this phase exists to prevent.

    Every one of these was a 500 INTERNAL before translation, so a client had
    no way to know that the rate limit was worth retrying and the context
    overflow was not.
    """
    seen = set()
    for exc in (
        LlmRateLimited("x"), LlmContextExceeded("x"),
        LlmAuthError("x"), LlmTimeout("x"),
    ):
        script.reset()
        script.queue_error(exc)
        status, _ = chat(client, "hello", DEFAULT_AGENT)
        seen.add(status)

    assert len(seen) == 4, f"expected four distinct statuses, got {sorted(seen)}"
    assert 500 not in seen


def test_the_provider_detail_is_not_echoed_to_the_client(client, script):
    """A provider message can carry the request payload back, which here is the
    user's own prompt."""
    script.queue_error(LlmRateLimited("upstream quoted: a private interview answer"))

    _, body = chat(client, "hello", DEFAULT_AGENT)

    assert "private interview answer" not in body["error"]


def test_the_error_envelope_shape_is_unchanged(client, script):
    """A new status code must not come with a new body shape -- the standard
    envelope is pinned independently of which failure produced it."""
    script.queue_error(LlmRateLimited("x"))

    _, body = chat(client, "hello", DEFAULT_AGENT)

    assert set(body) == {"status", "error", "error_code", "request_id"}
    assert body["request_id"]
