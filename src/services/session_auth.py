from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from threading import RLock
from time import time
from typing import Any
from urllib import error, parse, request

from fastapi import HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from ..config import (
    AUTH_API_BASE_URL,
    AUTH_SERVICE_KEY_ID,
    AUTH_SERVICE_SECRET,
    TRAVEL_OIDC_CALLBACK_ROUTE_PATH,
    TRAVEL_OIDC_CLIENT_ID,
    TRAVEL_OIDC_CLIENT_SECRET,
    TRAVEL_OIDC_REDIRECT_URI,
    TRAVEL_SESSION_COOKIE_DOMAIN,
    TRAVEL_SESSION_COOKIE_NAME,
    TRAVEL_SESSION_COOKIE_SAMESITE,
    TRAVEL_SESSION_COOKIE_SECURE,
    TRAVEL_SESSION_MAX_AGE_SECONDS,
    TRAVEL_WEB_BASE_URL,
)
from ..token_verifier import (
    build_user_from_payload,
    decode_auth_api_token,
    serialize_user,
)


class _NoRedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


LOGIN_SCOPE = "openid profile email service.permission"
LOGIN_TRANSACTION_TTL_SECONDS = 10 * 60
TRAVEL_WEB_DEFAULT_RETURN_PATH = "/"
TRAVEL_WEB_LOGIN_PATH = "/login"


@dataclass
class TravelSession:
    id: str
    access_token: str
    refresh_token: str
    access_token_expires_at: float
    session_expires_at: float
    user: dict
    refresh_lock: Any = field(default_factory=RLock)


@dataclass
class OidcLoginTransaction:
    state: str
    code_verifier: str
    return_to_path: str
    created_at: float


@dataclass
class _HttpResponse:
    status: int
    headers: Any
    data: Any


@dataclass
class _CallbackResult:
    redirect_url: str
    session_id: str | None = None


class OidcCallbackError(Exception):
    def __init__(self, error_code: str, description: str):
        super().__init__(description)
        self.error_code = error_code
        self.description = description


class TravelSessionService:
    def __init__(self) -> None:
        self.auth_api_base_url = AUTH_API_BASE_URL
        self.client_id = TRAVEL_OIDC_CLIENT_ID
        self.client_secret = TRAVEL_OIDC_CLIENT_SECRET
        self.redirect_uri = TRAVEL_OIDC_REDIRECT_URI
        self.callback_route_path = TRAVEL_OIDC_CALLBACK_ROUTE_PATH
        self.cookie_name = TRAVEL_SESSION_COOKIE_NAME
        self.travel_web_base_url = TRAVEL_WEB_BASE_URL
        self._sessions: dict[str, TravelSession] = {}
        self._login_transactions: dict[str, OidcLoginTransaction] = {}
        self._lock = RLock()

    async def start_login(self, return_to: str | None) -> dict:
        return await asyncio.to_thread(self._start_login_sync, return_to)

    async def handle_oidc_callback(
        self,
        code: str | None,
        state: str | None,
        error_code: str | None,
        error_description: str | None,
    ) -> RedirectResponse:
        result = await asyncio.to_thread(
            self._handle_oidc_callback_sync,
            code,
            state,
            error_code,
            error_description,
        )
        response = RedirectResponse(result.redirect_url, status_code=302)
        if result.session_id:
            self._set_session_cookie(response, result.session_id)
        return response

    async def logout(self, request_obj: Request, response: Response) -> None:
        session = self._get_request_session(request_obj)
        if session is not None:
            await asyncio.to_thread(
                self._revoke_refresh_token_safe, session.refresh_token
            )
            self._delete_session(session.id)
        self._clear_session_cookie(response)

    async def get_user(self, request_obj: Request) -> dict:
        session = await self.require_valid_session(request_obj)
        result = serialize_user(session.user)
        result["serviceApplication"] = None
        if AUTH_SERVICE_KEY_ID and AUTH_SERVICE_SECRET:
            result["serviceApplication"] = await self.get_application_status(
                session.user["account_id"]
            )
        return result

    async def require_valid_session(self, request_obj: Request) -> TravelSession:
        session = self._get_request_session(request_obj)
        if session is None:
            raise HTTPException(status_code=401, detail="Travel session is required")
        return await asyncio.to_thread(self._refresh_if_needed_sync, session.id)

    async def get_valid_session_user(self, request_obj: Request) -> dict | None:
        session = self._get_request_session(request_obj)
        if session is None:
            return None
        valid_session = await asyncio.to_thread(
            self._refresh_if_needed_sync, session.id
        )
        return valid_session.user

    async def create_service_application(
        self, request_obj: Request, message: str | None
    ) -> Any:
        session = await self.require_valid_session(request_obj)
        request_message = (
            message.strip()
            if isinstance(message, str) and message.strip()
            else "travel 서비스를 사용하기 위해 user 권한 상승을 요청합니다."
        )
        response = await asyncio.to_thread(
            self._request_json,
            "POST",
            f"{self.auth_api_base_url}/api/service-applications",
            {
                "serviceKey": "travel",
                "message": request_message,
                "requestedPermissionKey": "user",
            },
            {"Authorization": f"Bearer {session.access_token}"},
        )
        return _require_success(response, "Service application failed")

    async def search_accounts(self, query: str) -> list[dict]:
        return await asyncio.to_thread(self.search_accounts_sync, query)

    def search_accounts_sync(self, query: str) -> list[dict]:
        self._ensure_service_credential_configured("account search")
        url = (
            f"{self.auth_api_base_url}/api/internal/service-accounts/search"
            f"?serviceKey=travel&q={parse.quote(query)}"
        )
        response = self._request_json(
            "GET", url, None, self._service_credential_headers()
        )
        accounts = _require_success(response, "Account search failed")
        if not isinstance(accounts, list):
            return []
        return [_normalize_account(account) for account in accounts]

    async def get_application_status(self, account_id: str) -> Any:
        self._ensure_service_credential_configured("permission status")
        url = (
            f"{self.auth_api_base_url}/api/internal/service-applications/status"
            f"?serviceKey=travel&accountId={parse.quote(account_id)}"
        )
        response = await asyncio.to_thread(
            self._request_json,
            "GET",
            url,
            None,
            self._service_credential_headers(),
        )
        return _require_success(response, "Permission status lookup failed")

    def find_exact_account_by_login_id(self, login_id: str) -> dict:
        matches = [
            account
            for account in self.search_accounts_sync(login_id)
            if account.get("loginId") == login_id
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "Legacy travel owner lookup requires exactly one auth account "
                f"with loginId={login_id!r}; found {len(matches)}"
            )
        return matches[0]

    def _start_login_sync(self, return_to: str | None) -> dict:
        self._prune_expired_login_transactions()
        self._ensure_oidc_configured()
        state = _random_token(24)
        code_verifier = _random_token(48)
        transaction = OidcLoginTransaction(
            state=state,
            code_verifier=code_verifier,
            return_to_path=_normalize_return_to_path(return_to),
            created_at=time(),
        )
        with self._lock:
            self._login_transactions[state] = transaction
        return {
            "authorizeUrl": self._build_authorize_url(
                state, _code_challenge(code_verifier)
            )
        }

    def _handle_oidc_callback_sync(
        self,
        code: str | None,
        state: str | None,
        error_code: str | None,
        error_description: str | None,
    ) -> _CallbackResult:
        self._prune_expired_login_transactions()
        transaction = self._pop_login_transaction(state)
        if error_code:
            return _CallbackResult(
                self._build_error_redirect_url(
                    error_code, error_description or "Authorization was denied"
                )
            )
        if transaction is None:
            return _CallbackResult(
                self._build_error_redirect_url(
                    "invalid_state", "Login transaction expired or was not found"
                )
            )
        if not code:
            return _CallbackResult(
                self._build_error_redirect_url(
                    "invalid_request", "Authorization code is missing"
                )
            )
        try:
            token = self._exchange_code_for_token(code, transaction.code_verifier)
            session = self._create_session(token)
            self._store_session(session)
        except OidcCallbackError as exc:
            return _CallbackResult(
                self._build_error_redirect_url(exc.error_code, exc.description)
            )
        except HTTPException as exc:
            return _CallbackResult(
                self._build_error_redirect_url(
                    "callback_failed",
                    _stringify_error_detail(exc.detail, "OIDC callback failed"),
                )
            )
        return _CallbackResult(
            self._build_success_redirect_url(transaction.return_to_path), session.id
        )

    def _build_authorize_url(self, state: str, code_challenge: str) -> str:
        params = parse.urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": LOGIN_SCOPE,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self.auth_api_base_url}/oauth/authorize?{params}"

    def _exchange_code_for_token(self, code: str, code_verifier: str) -> dict:
        self._ensure_oidc_configured()
        response = self._request_json(
            "POST",
            f"{self.auth_api_base_url}/oauth/token",
            {
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": self.redirect_uri,
                "code": code,
                "code_verifier": code_verifier,
            },
            None,
        )
        if not 200 <= response.status < 300:
            error_name, description = _extract_oidc_error(
                response.data, "token_exchange_failed", "Token exchange failed"
            )
            raise OidcCallbackError(error_name, description)
        if not isinstance(response.data, dict):
            raise OidcCallbackError("token_exchange_failed", "Invalid token response")
        return response.data

    def _request_token(self, body: dict[str, Any]) -> dict:
        response = self._request_json(
            "POST",
            f"{self.auth_api_base_url}/oauth/token",
            {key: value for key, value in body.items() if value is not None},
            None,
        )
        data = _require_success(response, "Token exchange failed")
        if not isinstance(data, dict):
            raise HTTPException(status_code=503, detail="Invalid token response")
        return data

    def _refresh_if_needed_sync(self, session_id: str) -> TravelSession:
        session = self._get_session_by_id(session_id)
        if session is None:
            raise HTTPException(status_code=401, detail="Travel session is required")
        if session.access_token_expires_at - time() <= 30:
            with session.refresh_lock:
                session = self._get_session_by_id(session_id)
                if session is None:
                    raise HTTPException(
                        status_code=401, detail="Travel session is required"
                    )
                if session.access_token_expires_at - time() > 30:
                    return session
                refresh_token = session.refresh_token
                refresh_lock = session.refresh_lock
                try:
                    token = self._request_token(
                        {
                            "grant_type": "refresh_token",
                            "client_id": self.client_id,
                            "client_secret": self.client_secret,
                            "refresh_token": refresh_token,
                        }
                    )
                except HTTPException as exc:
                    with self._lock:
                        current = self._sessions.get(session_id)
                        if current and current.refresh_token == refresh_token:
                            self._sessions.pop(session_id, None)
                    raise HTTPException(status_code=401, detail=exc.detail) from exc
                session = self._create_session(token, session_id, refresh_lock)
                with self._lock:
                    self._sessions[session_id] = session
        return session

    def _revoke_refresh_token_safe(self, refresh_token: str) -> None:
        try:
            self._request_json(
                "POST",
                f"{self.auth_api_base_url}/oauth/revoke",
                {"token": refresh_token},
                None,
            )
        except HTTPException:
            return

    def _create_session(
        self,
        token: dict,
        session_id: str | None = None,
        refresh_lock: Any | None = None,
    ) -> TravelSession:
        access_token = token.get("access_token")
        refresh_token = token.get("refresh_token")
        expires_in = token.get("expires_in")
        if not access_token or not refresh_token or not expires_in:
            raise HTTPException(status_code=503, detail="Invalid token response")
        user = build_user_from_payload(decode_auth_api_token(access_token))
        existing_session = self._get_session_by_id(session_id) if session_id else None
        return TravelSession(
            id=session_id or _random_token(32),
            access_token=access_token,
            refresh_token=refresh_token,
            access_token_expires_at=time() + int(expires_in),
            session_expires_at=(
                existing_session.session_expires_at
                if existing_session
                else time() + TRAVEL_SESSION_MAX_AGE_SECONDS
            ),
            user=user,
            refresh_lock=refresh_lock or RLock(),
        )

    def _get_request_session(self, request_obj: Request) -> TravelSession | None:
        return self._get_session_by_id(request_obj.cookies.get(self.cookie_name))

    def _get_session_by_id(self, session_id: str | None) -> TravelSession | None:
        if not session_id:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session and session.session_expires_at <= time():
                self._sessions.pop(session_id, None)
                return None
            return session

    def _store_session(self, session: TravelSession) -> None:
        with self._lock:
            self._sessions[session.id] = session

    def _delete_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def _pop_login_transaction(self, state: str | None) -> OidcLoginTransaction | None:
        if not state:
            return None
        with self._lock:
            return self._login_transactions.pop(state, None)

    def _prune_expired_login_transactions(self) -> None:
        cutoff = time() - LOGIN_TRANSACTION_TTL_SECONDS
        with self._lock:
            for state in [
                key
                for key, transaction in self._login_transactions.items()
                if transaction.created_at < cutoff
            ]:
                self._login_transactions.pop(state, None)

    def _set_session_cookie(self, response: Response, session_id: str) -> None:
        response.set_cookie(
            key=self.cookie_name,
            value=session_id,
            max_age=TRAVEL_SESSION_MAX_AGE_SECONDS,
            httponly=True,
            secure=TRAVEL_SESSION_COOKIE_SECURE,
            samesite=TRAVEL_SESSION_COOKIE_SAMESITE,
            path="/",
            domain=TRAVEL_SESSION_COOKIE_DOMAIN,
        )

    def _clear_session_cookie(self, response: Response) -> None:
        response.delete_cookie(
            key=self.cookie_name,
            path="/",
            domain=TRAVEL_SESSION_COOKIE_DOMAIN,
            secure=TRAVEL_SESSION_COOKIE_SECURE,
            samesite=TRAVEL_SESSION_COOKIE_SAMESITE,
        )

    def _build_success_redirect_url(self, return_to_path: str) -> str:
        return _join_base_and_path(self.travel_web_base_url, return_to_path)

    def _build_error_redirect_url(self, error_code: str, description: str) -> str:
        query = parse.urlencode({"error": error_code, "error_description": description})
        return f"{_join_base_and_path(self.travel_web_base_url, TRAVEL_WEB_LOGIN_PATH)}?{query}"

    def _ensure_oidc_configured(self) -> None:
        if not self.client_id:
            raise HTTPException(
                status_code=503, detail="TRAVEL_OIDC_CLIENT_ID is required"
            )
        if not self.client_secret:
            raise HTTPException(
                status_code=503, detail="TRAVEL_OIDC_CLIENT_SECRET is required"
            )
        if not self.redirect_uri:
            raise HTTPException(
                status_code=503, detail="TRAVEL_OIDC_REDIRECT_URI is required"
            )

    def _ensure_service_credential_configured(self, purpose: str) -> None:
        if not AUTH_SERVICE_KEY_ID or not AUTH_SERVICE_SECRET:
            raise HTTPException(
                status_code=503,
                detail=f"Auth service credential is required for {purpose}",
            )

    def _service_credential_headers(self) -> dict[str, str]:
        return {
            "x-auth-service-key-id": AUTH_SERVICE_KEY_ID,
            "x-auth-service-secret": AUTH_SERVICE_SECRET,
        }

    def _request_json(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> _HttpResponse:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        req = request.Request(url, data=payload, method=method.upper())
        req.add_header("Accept", "application/json")
        if payload is not None:
            req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        opener = request.build_opener(_NoRedirectHandler())
        try:
            with opener.open(req, timeout=10) as response_obj:
                return _HttpResponse(
                    response_obj.status,
                    response_obj.headers,
                    _decode_json(response_obj.read().decode("utf-8")),
                )
        except error.HTTPError as exc:
            return _HttpResponse(
                exc.code, exc.headers, _decode_json(exc.read().decode("utf-8"))
            )
        except error.URLError as exc:
            raise HTTPException(
                status_code=502, detail=f"Upstream connection failed: {exc.reason}"
            ) from exc


_session_service = TravelSessionService()


def get_session_service() -> TravelSessionService:
    return _session_service


def _normalize_account(account: Any) -> dict:
    if not isinstance(account, dict):
        return {}
    return {
        "accountId": str(account.get("id", "")),
        "loginId": str(account.get("loginId", "")),
        "name": str(account.get("name", "")),
        "email": str(account.get("email", "")),
        "status": str(account.get("status", "")),
        "permission": str(account.get("permissionKey", "visitor")),
        "isSuperAdmin": bool(account.get("isSuperAdmin")),
    }


def _require_success(response: _HttpResponse, fallback: str) -> Any:
    if 200 <= response.status < 300:
        return response.data
    raise HTTPException(
        status_code=response.status,
        detail=_extract_error_detail(response.data, fallback),
    )


def _decode_json(raw: str) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _extract_error_detail(data: Any, fallback: str) -> Any:
    if isinstance(data, dict):
        for key in ("detail", "message", "error_description", "error"):
            if data.get(key):
                return data[key]
    if isinstance(data, (list, str)) and data:
        return data
    return fallback


def _random_token(byte_length: int) -> str:
    import secrets

    return secrets.token_urlsafe(byte_length)


def _code_challenge(code_verifier: str) -> str:
    import base64
    import hashlib

    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _extract_oidc_error(
    data: Any, fallback_error: str, fallback_description: str
) -> tuple[str, str]:
    if isinstance(data, dict):
        return (
            str(data.get("error") or fallback_error),
            _stringify_error_detail(
                data.get("error_description")
                or data.get("detail")
                or data.get("message"),
                fallback_description,
            ),
        )
    return fallback_error, _stringify_error_detail(data, fallback_description)


def _stringify_error_detail(detail: Any, fallback: str) -> str:
    if isinstance(detail, str) and detail.strip():
        return detail
    if isinstance(detail, list) and detail:
        rendered = ", ".join(str(item) for item in detail if item)
        if rendered:
            return rendered
    return fallback


def _normalize_return_to_path(return_to: str | None) -> str:
    if not return_to:
        return TRAVEL_WEB_DEFAULT_RETURN_PATH
    parsed = parse.urlsplit(return_to)
    if parsed.scheme or parsed.netloc:
        return TRAVEL_WEB_DEFAULT_RETURN_PATH
    path = parsed.path or TRAVEL_WEB_DEFAULT_RETURN_PATH
    if not path.startswith("/"):
        path = f"/{path}"
    return parse.urlunsplit(("", "", path, parsed.query, parsed.fragment))


def _join_base_and_path(base_url: str, path: str) -> str:
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{base_url.rstrip('/')}{normalized_path}"
