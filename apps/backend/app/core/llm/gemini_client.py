from google import genai
from google.genai import types

from app.core.llm.base import LLMClient, LLMMessage, LLMResponse, T
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


class GeminiClient(LLMClient):
    provider_name = "gemini"

    def __init__(self, api_key: str, default_model: str) -> None:
        self._client = genai.Client(api_key=api_key)
        self._default_model = default_model

    @staticmethod
    def _split_messages(messages: list[LLMMessage]) -> tuple[str | None, list[str]]:
        system = next((m.content for m in messages if m.role == "system"), None)
        user_parts = [m.content for m in messages if m.role != "system"]
        return system, user_parts

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        system, parts = self._split_messages(messages)
        cfg = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            max_output_tokens=max_tokens,
        )
        resp = await self._client.aio.models.generate_content(
            model=model or self._default_model,
            contents=parts,
            config=cfg,
        )
        return LLMResponse(
            content=resp.text or "",
            model=model or self._default_model,
            provider=self.provider_name,
        )

    async def structured_complete(
        self,
        messages: list[LLMMessage],
        schema: type[T],
        *,
        model: str | None = None,
        temperature: float = 0.0,
    ) -> T:
        system, parts = self._split_messages(messages)
        cfg = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            response_mime_type="application/json",
            response_schema=schema,
        )
        resp = await self._client.aio.models.generate_content(
            model=model or self._default_model,
            contents=parts,
            config=cfg,
        )
        return schema.model_validate_json(resp.text)

    async def close(self) -> None:
        return None
