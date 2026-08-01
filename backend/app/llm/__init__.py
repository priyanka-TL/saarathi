from typing import Any, List, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult
from langchain_litellm import ChatLiteLLM

from app.core.settings import settings as config
from app.core.logger import get_logger

logger = get_logger("llm_client")


from .normalization import _NormalizedChatLiteLLM

def get_llm(temperature: float = 0.0) -> ChatLiteLLM:
    """
    Returns a LangChain-compatible chat model backed by LiteLLM, routed to
    OpenRouter. LiteLLM is the single abstraction layer for all LLM calls in
    this project -- provider/model selection, retries, and timeouts are all
    driven by configuration here rather than scattered across call sites.
    """
    # LiteLLM routes requests based on the "<provider>/<model>" prefix.
    model = f"openrouter/{config.OPENROUTER_MODEL}"

    logger.info(
        f"Initializing LLM via LiteLLM -> OpenRouter | model='{model}' "
        f"temperature={temperature} timeout={config.LLM_TIMEOUT}s "
        f"max_retries={config.LLM_MAX_RETRIES}"
    )

    return _NormalizedChatLiteLLM(
        model=model,
        api_key=config.OPENROUTER_API_KEY,
        temperature=temperature,
        request_timeout=config.LLM_TIMEOUT,
        max_retries=config.LLM_MAX_RETRIES,
    )
