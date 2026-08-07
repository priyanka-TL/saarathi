"""LLM client construction.

`LlmFactory.get(spec)` (see `factory.py`) is the ONE way to obtain a chat model:
it is per-spec cached and is what `HandlerFactory` calls at request time.

LiteLLM is the single abstraction layer for all LLM calls in this project --
provider/model selection, retries and timeouts are driven by the AgentSpec and
Settings rather than scattered across call sites.

The old module-level `get_llm()` helper is gone. Its only caller was the legacy
`BaseAgent` in `app/agents/base.py`, which was itself unreachable; keeping it
meant a second, un-cached construction path that hardcoded the model and read
`Settings` at import time.
"""
