from typing import Any, List, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult
from langchain_litellm import ChatLiteLLM

class _NormalizedChatLiteLLM(ChatLiteLLM):
    """
    ChatLiteLLM, with message content normalized to a plain string.

    Reasoning-capable models (e.g. Qwen3) return `AIMessage.content` as a
    list of content blocks (thinking/reasoning + text) instead of a plain
    string. The rest of the app (agents, JSON API, frontend markdown
    rendering) expects `.content` to be a string, exactly as it was with the
    previous provider. Normalizing here -- in the provider layer -- keeps
    that contract intact without touching any agent/application logic.
    """

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        stream: Optional[bool] = None,
        **kwargs: Any,
    ) -> ChatResult:
        result = super()._generate(messages, stop=stop, run_manager=run_manager, stream=stream, **kwargs)
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
