from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlencode

import httpx
import websockets
from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, status

from .auth import auth_middleware, authorize_websocket
from .config import AppServerConfig
from .models import (
    AGENT_SERVER,
    AppConversation,
    AppConversationPage,
    AppConversationStartTask,
    AppSendMessageResponse,
    Sandbox,
    SandboxStatus,
    StartTaskStatus,
    normalize_uuid,
)
from .runtime import proxy_request, require_running, runtime_conversation_url, runtime_headers
from .sandbox import DockerSandboxService, ExposedPort, SandboxSpec
from .state import AppState
from .temporary_settings import build_temporary_router


def _require_static_agent_server(config: AppServerConfig) -> Sandbox:
    if not config.static_agent_server_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No sandbox provider configured. Set AGENT_SERVER_URL for the static provider.",
        )
    return Sandbox(
        agent_server_url=config.static_agent_server_url.rstrip("/"),
        session_api_key=config.static_agent_server_session_key,
    )


def _get_conversation_and_sandbox(
    state: AppState, conversation_id: str
) -> tuple[AppConversation, Sandbox]:
    normalized = normalize_uuid(conversation_id)
    conversation = state.conversations.get(normalized)
    if not conversation:
        raise HTTPException(status_code=404, detail="unknown conversation")
    sandbox = state.sandboxes.get(conversation.sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail="unknown sandbox")
    return conversation, sandbox


def _conversation_response(conversation: AppConversation, sandbox: Sandbox) -> AppConversation:
    return conversation.model_copy(update={"sandbox_status": sandbox.status})


def _build_start_payload(body: dict[str, Any], conversation_id: str) -> dict[str, Any]:
    payload = dict(body)
    payload["conversation_id"] = conversation_id
    payload.setdefault("workspace", {"kind": "LocalWorkspace", "working_dir": "workspace/project"})
    return payload


async def _start_runtime_conversation(
    config: AppServerConfig, sandbox: Sandbox, payload: dict[str, Any]
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=config.request_timeout_seconds) as client:
        response = await client.post(
            f"{sandbox.agent_server_url.rstrip('/')}/api/conversations",
            json=payload,
            headers=runtime_headers(sandbox),
        )
        response.raise_for_status()
        return response.json()


def _build_sandbox_service(config: AppServerConfig):
    if config.sandbox_provider != "docker":
        return None
    return DockerSandboxService(
        specs=[SandboxSpec(id=config.docker_agent_server_image, command=["--port", "8000"])],
        container_name_prefix=config.docker_container_name_prefix,
        host_port=8000,
        container_url_pattern=config.docker_container_url_pattern,
        mounts=[],
        exposed_ports=[ExposedPort(name=AGENT_SERVER, description="Agent server", container_port=8000)],
        health_check_path="/health",
        httpx_client=httpx.AsyncClient(timeout=config.request_timeout_seconds),
        max_num_sandboxes=5,
    )


def create_app(config: AppServerConfig | None = None, sandbox_service=None) -> FastAPI:
    config = config or AppServerConfig.from_env()
    sandbox_service = sandbox_service if sandbox_service is not None else _build_sandbox_service(config)
    state = AppState(config.state_dir)
    app = FastAPI(title="Minimal OpenHands app_server")
    app.state.config = config
    app.state.store = state

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        return await auth_middleware(request, call_next, config)

    @app.get("/alive")
    @app.get("/health")
    @app.get("/ready")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/server_info")
    async def server_info() -> dict[str, Any]:
        return {
            "app": "minimal-openhands-app-server",
            "websocket_gateway": config.enable_websocket_gateway,
        }

    @app.post("/api/v1/app-conversations")
    async def start_app_conversation(body: dict[str, Any] = Body(default_factory=dict)):
        sandbox = (
            await sandbox_service.start_sandbox(body.get("sandbox_spec_id"))
            if sandbox_service is not None
            else _require_static_agent_server(config)
        )
        state.sandboxes[sandbox.id] = sandbox
        conversation_id = str(uuid.uuid4())
        payload = _build_start_payload(body, conversation_id)
        runtime = await _start_runtime_conversation(config, sandbox, payload)
        status_value = runtime.get("status", "idle")
        conversation = AppConversation(
            id=conversation_id,
            sandbox_id=sandbox.id,
            status=status_value,
            conversation_url=runtime_conversation_url(sandbox, conversation_id),
            session_api_key=sandbox.session_api_key,
            websocket_url=f"/ws/events/{conversation_id}" if config.enable_websocket_gateway else None,
            selected_repository=body.get("selected_repository"),
            selected_branch=body.get("selected_branch"),
            git_provider=body.get("git_provider"),
            title=body.get("title"),
        )
        state.conversations[conversation_id] = conversation
        task = AppConversationStartTask(
            status=StartTaskStatus.READY,
            app_conversation_id=conversation_id,
            sandbox_id=sandbox.id,
            agent_server_url=sandbox.agent_server_url,
            request=body,
        )
        state.tasks[task.id] = task
        return task

    @app.get("/api/v1/app-conversations/search", response_model=AppConversationPage)
    async def search_app_conversations() -> AppConversationPage:
        items = [
            _conversation_response(conv, state.sandboxes[conv.sandbox_id])
            for conv in state.conversations.values()
        ]
        return AppConversationPage(items=items)

    @app.get("/api/v1/app-conversations")
    async def batch_get_app_conversations(ids: list[str] = Query(default_factory=list)):
        result = []
        for conversation_id in ids:
            conv = state.conversations.get(normalize_uuid(conversation_id))
            result.append(_conversation_response(conv, state.sandboxes[conv.sandbox_id]) if conv else None)
        return result

    @app.get("/api/v1/app-conversations/start-tasks")
    async def batch_get_start_tasks(ids: list[str] = Query(default_factory=list)):
        return [state.tasks.get(task_id) for task_id in ids]

    @app.post("/api/v1/app-conversations/{conversation_id}/send-message")
    async def send_message(conversation_id: str, body: dict[str, Any] = Body(default_factory=dict)):
        conversation, sandbox = _get_conversation_and_sandbox(state, conversation_id)
        require_running(conversation, sandbox)
        async with httpx.AsyncClient(timeout=config.request_timeout_seconds) as client:
            response = await client.post(
                f"{sandbox.agent_server_url.rstrip('/')}/api/conversations/{conversation.id}/events",
                json=body,
                headers=runtime_headers(sandbox),
            )
            response.raise_for_status()
        return AppSendMessageResponse(success=True, sandbox_status=sandbox.status)

    @app.post("/api/v1/sandboxes/{sandbox_id}/pause")
    async def pause_sandbox(sandbox_id: str):
        sandbox = state.sandboxes.get(sandbox_id)
        if not sandbox:
            raise HTTPException(status_code=404, detail="unknown sandbox")
        if sandbox_service is not None:
            exists = await sandbox_service.pause_sandbox(sandbox_id)
            if not exists:
                raise HTTPException(status_code=404, detail="unknown sandbox")
        sandbox.status = SandboxStatus.PAUSED
        return {"success": True, **sandbox.model_dump()}

    @app.post("/api/v1/sandboxes/{sandbox_id}/resume")
    async def resume_sandbox(sandbox_id: str):
        sandbox = state.sandboxes.get(sandbox_id)
        if not sandbox:
            raise HTTPException(status_code=404, detail="unknown sandbox")
        if sandbox_service is not None:
            exists = await sandbox_service.resume_sandbox(sandbox_id)
            if not exists:
                raise HTTPException(status_code=404, detail="unknown sandbox")
        sandbox.status = SandboxStatus.RUNNING
        return {"success": True, **sandbox.model_dump()}

    @app.post("/api/conversations/{conversation_id}/pause")
    async def pause_runtime(request: Request, conversation_id: str):
        _, sandbox = _get_conversation_and_sandbox(state, conversation_id)
        return await proxy_request(request, sandbox, f"/api/conversations/{normalize_uuid(conversation_id)}/pause")

    @app.post("/api/conversations/{conversation_id}/run")
    async def run_runtime(request: Request, conversation_id: str):
        _, sandbox = _get_conversation_and_sandbox(state, conversation_id)
        return await proxy_request(request, sandbox, f"/api/conversations/{normalize_uuid(conversation_id)}/run")

    @app.get("/api/conversations/{conversation_id}/events/count")
    async def count_events(request: Request, conversation_id: str):
        _, sandbox = _get_conversation_and_sandbox(state, conversation_id)
        return await proxy_request(
            request, sandbox, f"/api/conversations/{normalize_uuid(conversation_id)}/events/count"
        )

    @app.post("/api/conversations/{conversation_id}/events/respond_to_confirmation")
    async def respond_to_confirmation(request: Request, conversation_id: str):
        _, sandbox = _get_conversation_and_sandbox(state, conversation_id)
        return await proxy_request(
            request,
            sandbox,
            f"/api/conversations/{normalize_uuid(conversation_id)}/events/respond_to_confirmation",
        )

    @app.post("/api/conversations/{conversation_id}/events")
    async def send_event_proxy(request: Request, conversation_id: str):
        _, sandbox = _get_conversation_and_sandbox(state, conversation_id)
        return await proxy_request(
            request, sandbox, f"/api/conversations/{normalize_uuid(conversation_id)}/events"
        )

    @app.post("/api/conversations/{conversation_id}/ask_agent")
    async def ask_agent(request: Request, conversation_id: str):
        _, sandbox = _get_conversation_and_sandbox(state, conversation_id)
        return await proxy_request(
            request,
            sandbox,
            f"/api/conversations/{normalize_uuid(conversation_id)}/ask_agent",
            timeout=600,
        )

    @app.get("/api/v1/conversation/{conversation_id}/events/search")
    async def search_events(request: Request, conversation_id: str):
        _, sandbox = _get_conversation_and_sandbox(state, conversation_id)
        return await proxy_request(
            request, sandbox, f"/api/conversations/{normalize_uuid(conversation_id)}/events/search"
        )

    @app.get("/api/v1/git/changes")
    async def git_changes(request: Request, conversation_id: str, path: str):
        _, sandbox = _get_conversation_and_sandbox(state, conversation_id)
        return await proxy_request(request, sandbox, "/api/git/changes", query_params={"path": path})

    @app.get("/api/v1/git/diff")
    async def git_diff(request: Request, conversation_id: str, path: str):
        _, sandbox = _get_conversation_and_sandbox(state, conversation_id)
        return await proxy_request(request, sandbox, "/api/git/diff", query_params={"path": path})

    @app.websocket("/ws/events/{conversation_id}")
    async def events_gateway(websocket: WebSocket, conversation_id: str):
        if not await authorize_websocket(websocket, config):
            return
        _, sandbox = _get_conversation_and_sandbox(state, conversation_id)
        query = {key: value for key, value in websocket.query_params.items() if key != "session_api_key"}
        if sandbox.session_api_key:
            query["session_api_key"] = sandbox.session_api_key
        url = _runtime_ws_url(sandbox, f"/sockets/events/{normalize_uuid(conversation_id)}", query)
        await _tunnel_websocket(websocket, url)

    @app.websocket("/ws/bash-events/{conversation_id}")
    async def bash_gateway(websocket: WebSocket, conversation_id: str):
        if not await authorize_websocket(websocket, config):
            return
        _, sandbox = _get_conversation_and_sandbox(state, conversation_id)
        query = {}
        if sandbox.session_api_key:
            query["session_api_key"] = sandbox.session_api_key
        url = _runtime_ws_url(sandbox, "/sockets/bash-events", query)
        await _tunnel_websocket(websocket, url)

    app.include_router(build_temporary_router(state))
    return app


def _runtime_ws_url(sandbox: Sandbox, path: str, query: dict[str, str]) -> str:
    parsed = httpx.URL(sandbox.agent_server_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    query_string = urlencode(query)
    return f"{scheme}://{parsed.host}:{parsed.port}{path}" + (f"?{query_string}" if query_string else "")


async def _tunnel_websocket(client_ws: WebSocket, runtime_url: str) -> None:
    await client_ws.accept()
    async with websockets.connect(runtime_url) as runtime_ws:
        async for message in runtime_ws:
            if isinstance(message, bytes):
                await client_ws.send_bytes(message)
            else:
                await client_ws.send_text(message)
