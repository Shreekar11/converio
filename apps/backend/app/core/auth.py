from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import jwt
from app.core.jwt import jwt_verifier
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)
security = HTTPBearer(auto_error=False)


class CurrentUser(BaseModel):
    id: str
    email: Optional[str] = None
    role: str = "user"
    app_metadata: dict = {}
    user_metadata: dict = {}


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> CurrentUser:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = await jwt_verifier.verify_token(credentials.credentials)
        return CurrentUser(
            id=claims.sub,
            email=claims.email,
            role=claims.role or "user",
            app_metadata=claims.app_metadata or {},
            user_metadata=claims.user_metadata or {},
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e
