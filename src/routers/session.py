from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response

from ..config import TRAVEL_OIDC_CALLBACK_ROUTE_PATH
from ..schemas import ServiceApplicationRequest, SessionOidcStartRequest
from ..services.session_auth import get_session_service

router = APIRouter(prefix="/api", tags=["session"])


@router.post("/session/oidc/start")
async def session_oidc_start(body: SessionOidcStartRequest | None = None):
    return await get_session_service().start_login(body.returnTo if body else None)


async def session_oidc_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
):
    return await get_session_service().handle_oidc_callback(
        code, state, error, error_description
    )


@router.get("/session/me")
async def session_me(request: Request):
    return await get_session_service().get_user(request)


@router.post("/session/logout")
async def session_logout(request: Request, response: Response):
    await get_session_service().logout(request, response)
    return {"success": True}


@router.post("/session/service-application")
async def create_service_application(request: Request, body: ServiceApplicationRequest):
    return await get_session_service().create_service_application(request, body.message)


router.add_api_route(
    TRAVEL_OIDC_CALLBACK_ROUTE_PATH,
    session_oidc_callback,
    methods=["GET"],
)
