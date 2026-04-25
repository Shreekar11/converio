from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMMessage(BaseModel):
    role: str  # "system" | "user" | "assistant"
    content: str


class LLMResponse(BaseModel):
    content: str
    model: str
    provider: str
    raw: dict[str, Any] | None = None


class LLMClient(ABC):
    provider_name: str

    @abstractmethod
    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...

    @abstractmethod
    async def structured_complete(
        self,
        messages: list[LLMMessage],
        schema: type[T],
        *,
        model: str | None = None,
        temperature: float = 0.0,
    ) -> T: ...

    @abstractmethod
    async def close(self) -> None: ...
