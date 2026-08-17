"""The chat model wrapper.

Responsible for: normalising message content to a string, and translating
provider exceptions.
Used by: LlmFactory, which returns this rather than a bare ChatLiteLLM.

`_generate` is the single choke point every LLM call passes through, which is
what makes it the one place error translation has to happen.
"""
from typing import Any, List, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult
from langchain_litellm import ChatLiteLLM

from app.llm.exceptions import LlmError, translate

class _NormalizedChatLiteLLM(ChatLiteLLM):
    """
    ChatLiteLLM, with message content normalized to a plain string, and with
    provider exceptions translated at this boundary.

    Reasoning-capable models (e.g. Qwen3) return `AIMessage.content` as a
    list of content blocks (thinking/reasoning + text) instead of a plain
    string. The rest of the app (agents, JSON API, frontend markdown
    rendering) expects `.content` to be a string, exactly as it was with the
    previous provider. Normalizing here -- in the provider layer -- keeps
    that contract intact without touching any agent/application logic.

    `_generate` is the single choke point through which every LLM call in the
    app passes, which makes it the one place error translation has to happen for
    it to happen everywhere. See app/llm/exceptions.py.
    """

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        stream: Optional[bool] = None,
        **kwargs: Any,
    ) -> ChatResult:
        try:
            result = super()._generate(
                messages, stop=stop, run_manager=run_manager, stream=stream, **kwargs
            )
        except LlmError:
            # Already ours (a retry wrapper re-entering, say). Do not re-wrap.
            raise
        except Exception as exc:
            # `from exc` keeps the provider traceback attached for the logs
            # while the client sees only the mapped envelope.
            raise translate(exc, model=getattr(self, "model", None)) from exc

        for generation in result.generations:
            if isinstance(generation.message.content, list):
                generation.message.content = generation.message.text
        return result

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        stream: Optional[bool] = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("Async generation is not supported for _NormalizedChatLiteLLM")
