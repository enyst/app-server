from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import UUID

import pytest
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, WebSocket
from fastapi.testclient import TestClient

from app_server.app import create_app
from app_server.config import AppServerConfig

RUNTIME_KEY = "runtime-secret"
APP_KEY = "app-secret"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class RuntimeState:
    conversations: dict[str, dict] = field(default_factory=dict)
    received: list[dict] = field(default_factory=list)


def _check_runtime_key(x_session_api_key: str | None) -> None:
    if x_session_api_key != RUNTIME_KEY:
        raise HTTPException(status_code=401, detail="bad runtime key")


def build_fake_agent_server(state: RuntimeState) -> FastAPI:
    app = FastAPI()

    @app.get("/alive")
    async def alive():
        return {"status": "ok"}

    @app.get("/server_info")
    async def server_info(x_session_api_key: str | None = Header(default=None)):
        _check_runtime_key(x_session_api_key)
        return {"version": "fake-agent-server", "usable_tools": ["terminal"]}

    @app.post("/api/conversations")
    async def create_conversation(body: dict, x_session_api_key: str | None = Header(default=None)):
        _check_runtime_key(x_session_api_key)
        conversation_id = body.get("conversation_id") or "00000000-0000-0000-0000-000000000001"
        state.conversations[conversation_id] = body
        return {
            "id": conversation_id,
            "status": "idle",
            "workspace": {"working_dir": body.get("workspace", {}).get("working_dir", "/workspace/project")},
        }

    @app.get("/api/conversations/{conversation_id}")
    async def get_conversation(conversation_id: str, x_session_api_key: str | None = Header(default=None)):
        _check_runtime_key(x_session_api_key)
        return {"id": conversation_id, "status": "idle"}

    @app.post("/api/conversations/{conversation_id}/events")
    async def send_event(conversation_id: str, body: dict, x_session_api_key: str | None = Header(default=None)):
        _check_runtime_key(x_session_api_key)
        state.received.append({"path": "events", "conversation_id": conversation_id, "body": body})
        return {"success": True}

    @app.post("/api/conversations/{conversation_id}/events/respond_to_confirmation")
    async def confirm(conversation_id: str, body: dict, x_session_api_key: str | None = Header(default=None)):
        _check_runtime_key(x_session_api_key)
        state.received.append({"path": "confirm", "conversation_id": conversation_id, "body": body})
        return {"success": True}

    @app.get("/api/conversations/{conversation_id}/events/search")
    async def search_events(conversation_id: str, x_session_api_key: str | None = Header(default=None)):
        _check_runtime_key(x_session_api_key)
        return {"items": [{"id": 1, "source": "agent", "message": "hello"}], "next_page_id": None}

    @app.get("/api/conversations/{conversation_id}/events/count")
    async def count_events(conversation_id: str, x_session_api_key: str | None = Header(default=None)):
        _check_runtime_key(x_session_api_key)
        return 3

    @app.post("/api/conversations/{conversation_id}/ask_agent")
    async def ask_agent(conversation_id: str, body: dict, x_session_api_key: str | None = Header(default=None)):
        _check_runtime_key(x_session_api_key)
        return {"response": f"answer: {body.get('question')}"}

    @app.post("/api/conversations/{conversation_id}/pause")
    async def pause(conversation_id: str, x_session_api_key: str | None = Header(default=None)):
        _check_runtime_key(x_session_api_key)
        return {"success": True}

    @app.post("/api/conversations/{conversation_id}/run")
    async def run(conversation_id: str, x_session_api_key: str | None = Header(default=None)):
        _check_runtime_key(x_session_api_key)
        return {"success": True}

    @app.get("/api/git/changes")
    async def git_changes(path: str = Query(...), x_session_api_key: str | None = Header(default=None)):
        _check_runtime_key(x_session_api_key)
        return [{"status": "MODIFIED", "path": path.rstrip("/") + "/README.md"}]

    @app.get("/api/git/diff")
    async def git_diff(path: str = Query(...), x_session_api_key: str | None = Header(default=None)):
        _check_runtime_key(x_session_api_key)
        return {"path": path, "diff": "diff --git a/README.md b/README.md"}

    @app.websocket("/sockets/events/{conversation_id}")
    async def events_ws(websocket: WebSocket, conversation_id: str, session_api_key: str | None = Query(default=None)):
        if session_api_key != RUNTIME_KEY:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        await websocket.send_json({"type": "runtime_event", "conversation_id": conversation_id})
        await websocket.close()

    @app.websocket("/sockets/bash-events")
    async def bash_ws(websocket: WebSocket, session_api_key: str | None = Query(default=None)):
        if session_api_key != RUNTIME_KEY:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        await websocket.send_json({"type": "bash_event", "message": "ready"})
        await websocket.close()

    return app


@dataclass
class RunningServer:
    base_url: str
    state: RuntimeState


@pytest.fixture
def fake_agent_server() -> Iterator[RunningServer]:
    state = RuntimeState()
    app = build_fake_agent_server(state)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        pass
    yield RunningServer(base_url=f"http://127.0.0.1:{port}", state=state)
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def client(fake_agent_server: RunningServer, tmp_path) -> Iterator[TestClient]:
    app = create_app(
        AppServerConfig(
            session_api_keys=[APP_KEY],
            state_dir=tmp_path,
            static_agent_server_url=fake_agent_server.base_url,
            static_agent_server_session_key=RUNTIME_KEY,
            public_base_url="http://app-server.test",
            enable_websocket_gateway=True,
        )
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def authed_headers() -> dict[str, str]:
    return {"X-Session-API-Key": APP_KEY}


def assert_uuid(value: str) -> None:
    UUID(value)
