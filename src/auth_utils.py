from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from jose import JWTError

from .services.session_auth import get_session_service
from .token_verifier import build_user_from_payload, decode_auth_api_token


async def get_current_user(request: Request) -> dict:
    session_service = get_session_service()
    bearer_token = _get_bearer_token(request)

    if request.cookies.get(session_service.cookie_name):
        try:
            session_user = await session_service.get_valid_session_user(request)
        except HTTPException:
            if not bearer_token:
                raise
        else:
            if session_user is not None:
                _require_travel_access(session_user)
                return session_user

    if not bearer_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        user = build_user_from_payload(decode_auth_api_token(bearer_token))
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    _require_travel_access(user)
    return user


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("permission") not in {"admin", "superadmin"}:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _require_travel_access(user: dict) -> None:
    if user.get("permission") == "visitor":
        raise HTTPException(status_code=403, detail="Travel access approval required")


def _get_bearer_token(request: Request) -> str | None:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip() or None
    return None
