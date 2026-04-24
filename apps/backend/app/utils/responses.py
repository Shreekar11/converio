from typing import Any

from fastapi import Request
from pydantic import BaseModel


class ApiResponse(BaseModel):
    status: bool
    message: str
    data: Any | None = None
    correlation_id: str | None = None


def create_api_response(data: Any, message: str, request: Request) -> ApiResponse:
    return ApiResponse(
        status=True,
        message=message,
        data=data,
        correlation_id=getattr(request.state, "correlation_id", None),
    )
