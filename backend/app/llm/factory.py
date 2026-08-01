from app.core.settings import settings as config
from app.core.logger import get_logger
from app.domain.agent_spec import ModelSpec
from .normalization import _NormalizedChatLiteLLM

logger = get_logger("llm_factory")

class LlmFactory:
    def __init__(self):
        self._cache = {}

    def get(self, spec: ModelSpec) -> _NormalizedChatLiteLLM:
        """
        Returns a cached client per distinct spec. 
        Replaces per-agent client construction.
        """
        cache_key = (spec.provider, spec.name, spec.temperature, spec.max_tokens, spec.timeout_s)
        
        if cache_key not in self._cache:
            model_id = f"{spec.provider}/{spec.name}"
            
            logger.info(
                f"Initializing LLM via LiteLLM -> {spec.provider} | model='{model_id}' "
                f"temperature={spec.temperature} max_tokens={spec.max_tokens} "
                f"timeout={spec.timeout_s}s"
            )

            kwargs = {
                "model": model_id,
                "api_key": config.OPENROUTER_API_KEY if spec.provider == "openrouter" else "",
                "temperature": spec.temperature,
                "request_timeout": spec.timeout_s,
                "max_retries": config.LLM_MAX_RETRIES,
            }
            if spec.max_tokens is not None:
                kwargs["max_tokens"] = spec.max_tokens

            self._cache[cache_key] = _NormalizedChatLiteLLM(**kwargs)
            
        return self._cache[cache_key]
