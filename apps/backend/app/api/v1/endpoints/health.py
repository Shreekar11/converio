from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


@router.get("/health", operation_id="health_check", tags=["Health"])
async def health_check() -> dict:
    return {"status": "ok"}


class EchoResponse(BaseModel):
    message: str
    server_time: datetime


@router.get("/health/echo", operation_id="echo", tags=["Health"])
async def echo(message: str = "hello") -> EchoResponse:
    return EchoResponse(message=message, server_time=datetime.now(UTC))
