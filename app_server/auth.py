from __future__ import annotations

from fastapi import Request, WebSocket, status
from fastapi.responses import JSONResponse

from .config import AppServerConfig

PUBLIC_PREFIXES = ("/health", "/ready", "/alive", "/server_info", "/docs", "/openapi.json")


def extract_key(headers: dict[str, str], query_key: str | None = None) -> str | None:
    if query_key:
        return query_key
    header_key = headers.get("x-session-api-key")
    if header_key:
        return header_key
    auth = headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1]
    return None


def is_authorized(config: AppServerConfig, key: str | None) -> bool:
    if not config.session_api_keys:
        return True
    return key in config.session_api_keys


async def auth_middleware(request: Request, call_next, config: AppServerConfig):
    if request.url.path.startswith(PUBLIC_PREFIXES) or not request.url.path.startswith("/api/"):
        return await call_next(request)
    if not is_authorized(config, extract_key(dict(request.headers))):
        return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": "invalid session key"})
    return await call_next(request)


async def authorize_websocket(websocket: WebSocket, config: AppServerConfig) -> bool:
    key = extract_key(dict(websocket.headers), websocket.query_params.get("session_api_key"))
    if is_authorized(config, key):
        return True
    await websocket.close(code=4401, reason="invalid session key")
    return False
