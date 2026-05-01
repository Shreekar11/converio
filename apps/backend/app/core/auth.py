
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.jwt import jwt_verifier
from app.database.models import Operator
from app.repositories.operators import OperatorRepository
from app.schemas.enums import OperatorStatus
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)
security = HTTPBearer(auto_error=False)


class CurrentUser(BaseModel):
    id: str
    email: str | None = None
    role: str = "user"
    app_metadata: dict = {}
    user_metadata: dict = {}


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
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


async def get_current_operator(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> Operator:
    """Resolve authenticated user to an active Operator row.

    Raises:
        HTTPException 403 if no operator row is linked to this user, or if
            the operator's status is not 'active'.
    """
    operator = await OperatorRepository(session).get_by_supabase_id(
        current_user.id
    )

    # Collapse "no row" and "inactive" into a single 403 to avoid leaking the
    # distinction (operator presence is itself sensitive: an attacker probing
    # which Supabase users are Converio operators must not be able to tell
    # "no operator row" from "operator suspended").
    if operator is None:
        LOGGER.warning(
            "Operator auth check failed",
            extra={"user_id": current_user.id, "reason": "no_operator_row"},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operator privileges required",
        )

    if operator.status != OperatorStatus.ACTIVE.value:
        LOGGER.warning(
            "Operator auth check failed",
            extra={
                "user_id": current_user.id,
                "operator_id": str(operator.id),
                "reason": "inactive_operator",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operator privileges required",
        )

    return operator
