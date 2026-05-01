from typing import Callable

from app.core.config import settings
from app.core.llm.base import LLMClient
from app.core.llm.gemini_client import GeminiClient
from app.core.llm.ollama_client import OllamaClient
from app.core.llm.openrouter_client import OpenRouterClient
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

_STRATEGIES: dict[str, Callable[[], LLMClient]] = {
    "openrouter": lambda: OpenRouterClient(
        api_key=settings.llm.openrouter_api_key,
        base_url=settings.llm.openrouter_api_url,
        default_model=settings.llm.openrouter_model,
    ),
    "gemini": lambda: GeminiClient(
        api_key=settings.llm.gemini_api_key,
        default_model=settings.llm.gemini_model,
    ),
    "ollama": lambda: OllamaClient(
        host=settings.llm.ollama_host,
        default_model=settings.llm.ollama_model,
    ),
}

_singleton: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _singleton
    if _singleton is not None:
        return _singleton
    provider = (settings.llm.provider or "").lower().strip()
    if not provider:
        provider = "ollama"

    # Developer-friendly fallback: if a cloud provider is selected but its API key
    # is missing, default to local Ollama in development instead of failing inside
    # a Temporal activity with a cryptic transport error.
    if provider == "openrouter" and not (settings.llm.openrouter_api_key or "").strip():
        if (settings.environment or "").lower() == "development":
            LOGGER.warning(
                "LLM_PROVIDER=openrouter but OPENROUTER_API_KEY is missing; falling back to ollama"
            )
            provider = "ollama"
        else:
            raise ValueError(
                "LLM_PROVIDER=openrouter requires OPENROUTER_API_KEY to be set."
            )
    if provider == "gemini" and not (settings.llm.gemini_api_key or "").strip():
        if (settings.environment or "").lower() == "development":
            LOGGER.warning(
                "LLM_PROVIDER=gemini but GEMINI_API_KEY is missing; falling back to ollama"
            )
            provider = "ollama"
        else:
            raise ValueError("LLM_PROVIDER=gemini requires GEMINI_API_KEY to be set.")
    if provider not in _STRATEGIES:
        raise ValueError(f"Unknown LLM provider '{provider}'. Valid: {list(_STRATEGIES)}")
    _singleton = _STRATEGIES[provider]()
    return _singleton


async def close_llm_client() -> None:
    global _singleton
    if _singleton is not None:
        await _singleton.close()
        _singleton = None
