from __future__ import annotations

from urllib.parse import urlencode, urlparse, urlunparse

import httpx
from fastapi import HTTPException, Request, Response, status

from .models import AppConversation, Sandbox

HOP_BY_HOP_HEADERS = {
    "host",
    "connection",
    "transfer-encoding",
    "content-length",
    "x-session-api-key",
    "authorization",
}


def runtime_headers(sandbox: Sandbox) -> dict[str, str]:
    return {"X-Session-API-Key": sandbox.session_api_key} if sandbox.session_api_key else {}


def runtime_conversation_url(sandbox: Sandbox, conversation_id: str) -> str:
    return f"{sandbox.agent_server_url.rstrip('/')}/api/conversations/{conversation_id}"


def websocket_url_from_http(base_url: str, path: str, query: dict[str, str]) -> str:
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, path, "", urlencode(query), ""))


async def proxy_request(
    request: Request,
    sandbox: Sandbox,
    path: str,
    timeout: float = 30.0,
    query_params: dict[str, str] | None = None,
) -> Response:
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }
    headers.update(runtime_headers(sandbox))
    body = await request.body()
    async with httpx.AsyncClient(timeout=timeout) as client:
        upstream = await client.request(
            request.method,
            f"{sandbox.agent_server_url.rstrip('/')}{path}",
            headers=headers,
            params=query_params if query_params is not None else request.query_params,
            content=body,
        )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


def require_running(conversation: AppConversation, sandbox: Sandbox) -> None:
    if sandbox.status != "RUNNING":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Sandbox is {sandbox.status}",
        )
    if conversation.sandbox_id != sandbox.id:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="sandbox mismatch")
