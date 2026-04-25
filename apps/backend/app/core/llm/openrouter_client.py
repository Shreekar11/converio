import httpx

from app.core.llm.base import LLMClient, LLMMessage, LLMResponse, T
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


class OpenRouterClient(LLMClient):
    provider_name = "openrouter"

    def __init__(self, api_key: str, base_url: str, default_model: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/Shreekar11/converio",
            },
            timeout=60.0,
        )
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
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        resp = await self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        body = resp.json()
        return LLMResponse(
            content=body["choices"][0]["message"]["content"],
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
            "temperature": temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                    "strict": True,
                },
            },
        }
        resp = await self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return schema.model_validate_json(content)

    async def close(self) -> None:
        await self._client.aclose()
