from typing import Any, Optional
from pydantic import BaseModel
from fastapi import Request


class ApiResponse(BaseModel):
    status: bool
    message: str
    data: Optional[Any] = None
    correlation_id: Optional[str] = None


def create_api_response(data: Any, message: str, request: Request) -> ApiResponse:
    return ApiResponse(
        status=True,
        message=message,
        data=data,
        correlation_id=getattr(request.state, "correlation_id", None),
    )
