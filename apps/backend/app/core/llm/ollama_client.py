import httpx

from app.core.llm.base import LLMClient, LLMMessage, LLMResponse, T
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


class OllamaClient(LLMClient):
    provider_name = "ollama"

    def __init__(self, host: str, default_model: str) -> None:
        self._client = httpx.AsyncClient(base_url=host, timeout=120.0)
        self._default_model = default_model

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        payload: dict = {
            "model": model or self._default_model,
            "messages": [m.model_dump() for m in messages],
            "stream": False,
            "options": {"temperature": temperature},
        }
        if max_tokens:
            payload["options"]["num_predict"] = max_tokens
        resp = await self._client.post("/api/chat", json=payload)
        resp.raise_for_status()
        body = resp.json()
        return LLMResponse(
            content=body["message"]["content"],
            model=body.get("model", payload["model"]),
            provider=self.provider_name,
            raw=body,
        )

    async def structured_complete(
        self,
        messages: list[LLMMessage],
        schema: type[T],
        *,
        model: str | None = None,
        temperature: float = 0.0,
    ) -> T:
        payload = {
            "model": model or self._default_model,
            "messages": [m.model_dump() for m in messages],
            "stream": False,
            "format": schema.model_json_schema(),
            "options": {"temperature": temperature},
        }
        resp = await self._client.post("/api/chat", json=payload)
        resp.raise_for_status()
        content = resp.json()["message"]["content"]
        return schema.model_validate_json(content)

    async def close(self) -> None:
        await self._client.aclose()
