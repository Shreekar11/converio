"""JWT Authentication Middleware for FastAPI.

This middleware verifies the JWT access token in the Authorization header
using Supabase JWT keys and attaches the user information to the request state.
"""

import jwt
from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.auth import CurrentUser
from app.core.jwt import jwt_verifier
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Paths that don't require authentication
# Note: API routes are mounted under settings.api_v1_prefix (default: /api/v1),
# so we must also exclude versioned health endpoints.
EXCLUDED_PATHS = {"/health", "/api/v1/health", "/docs", "/openapi.json", "/", "/redoc"}


class JWTAuthenticationMiddleware(BaseHTTPMiddleware):
    """Middleware for JWT authentication.

    Verifies Bearer token in 'Authorization' header and populates request.state.user.
    """

    async def dispatch(self, request: Request, call_next):
        # Allow OPTIONS requests
        if request.method == "OPTIONS":
            return await call_next(request)

        # Skip authentication for excluded paths
        if (
            request.url.path in EXCLUDED_PATHS
            or request.url.path.startswith("/health")
            or request.url.path.startswith("/api/v1/health")
        ):
            return await call_next(request)

        auth_header = request.headers.get("Authorization")
        token = None

        if auth_header:
            if not auth_header.startswith("Bearer "):
                LOGGER.warning(f"Invalid Authorization header format for {request.url.path}")
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"detail": "Invalid authentication scheme. Use Bearer token."},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            token = auth_header.split(" ")[1]
        elif request.url.path.startswith("/api/v1/") and request.query_params.get("token"):
            # SSE fallback — EventSource cannot send headers
            token = request.query_params.get("token")

        if not token:
            LOGGER.warning(f"Missing authentication for {request.url.path}")
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Authentication required"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        try:
            claims = await jwt_verifier.verify_token(token)

            request.state.user = CurrentUser(
                id=claims.sub,
                email=claims.email,
                role=claims.role or "user",
                app_metadata=claims.app_metadata or {},
                user_metadata=claims.user_metadata or {},
            )

            LOGGER.debug(f"Authenticated user {claims.sub} via middleware")

        except jwt.InvalidTokenError as e:
            LOGGER.warning(f"Invalid token for {request.url.path}: {e}")
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Invalid authentication token"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        response = await call_next(request)
        return response
