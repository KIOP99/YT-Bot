"""
api/dependencies.py
--------------------
FastAPI dependency injection:
  - get_db       — database session
  - get_current_user — JWT/cookie auth
  - require_admin    — admin-only guard
"""

from __future__ import annotations

from typing import Optional

import jwt
from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from core.logging_config import get_logger
from core.security import decode_access_token
from models.user import User

log = get_logger(__name__)


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    access_token: Optional[str] = Cookie(default=None),
) -> User:
    """
    Extract and validate the JWT from the HTTP-only cookie.
    Returns the authenticated User or raises 401.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )

    token = access_token
    # Also accept Authorization: Bearer <token> for API clients
    auth_header = request.headers.get("Authorization", "")
    if not token and auth_header.startswith("Bearer "):
        token = auth_header[7:]

    if not token:
        raise credentials_exception

    try:
        payload = decode_access_token(token)
        user_id: int = int(payload["sub"])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, KeyError, ValueError):
        raise credentials_exception

    result = await db.execute(select(User).where(User.id == user_id))
    user: Optional[User] = result.scalar_one_or_none()

    if user is None or not user.is_active:
        raise credentials_exception

    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """Currently all dashboard users are admins. Reserved for future RBAC."""
    return user
