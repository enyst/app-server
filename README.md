# Minimal OpenHands app_server

A small FastAPI control plane intended to sit between Agent Canvas and sandbox-hosted `openhands-agent-server` runtimes.

```text
Agent Canvas -> app_server -> sandbox -> agent-server
```

This repository is intentionally smaller than `OpenHands/OpenHands`: it keeps sandbox metadata, conversation metadata, app-server auth, and proxy/gateway routes, while leaving agent execution to `openhands-agent-server`.

## Run locally

Install and test:

```bash
python -m pip install -e '.[dev]'
python -m ruff check .
python -m pytest
```

Run against an existing agent-server runtime:

```bash
SESSION_API_KEY=app-secret \
AGENT_SERVER_URL=http://127.0.0.1:18100 \
AGENT_SERVER_SESSION_API_KEY=runtime-secret \
python -m uvicorn app_server.app:create_app --factory --host 0.0.0.0 --port 8000
```

Use `X-Session-API-Key: app-secret` when calling app_server. app_server uses `AGENT_SERVER_SESSION_API_KEY` when it calls the sandbox-hosted agent-server.

Run with Docker sandbox orchestration instead of a pre-existing runtime:

```bash
SESSION_API_KEY=app-secret \
APP_SERVER_SANDBOX_PROVIDER=docker \
AGENT_SERVER_IMAGE=ghcr.io/openhands/agent-server:1.22.1-python \
SANDBOX_CONTAINER_URL_PATTERN='http://localhost:{port}' \
python -m uvicorn app_server.app:create_app --factory --host 0.0.0.0 --port 8000
```

In Docker mode, `POST /api/v1/app-conversations` creates a new agent-server container, injects `OH_SESSION_API_KEYS_0`, maps the agent-server port, stores the resulting sandbox metadata, waits through the normal app_server proxy/gateway paths, and then starts the runtime conversation.


## Implemented surface

- Health/status: `/alive`, `/health`, `/ready`, `/server_info`
- App conversations:
  - `POST /api/v1/app-conversations`
  - `GET /api/v1/app-conversations/search`
  - `GET /api/v1/app-conversations?ids=...`
  - `GET /api/v1/app-conversations/start-tasks?ids=...`
  - `POST /api/v1/app-conversations/{id}/send-message`
- Sandbox control:
  - `POST /api/v1/sandboxes/{id}/pause`
  - `POST /api/v1/sandboxes/{id}/resume`
- Agent-server proxy routes:
  - `POST /api/conversations/{id}/events`
  - `GET /api/conversations/{id}/events/count`
  - `POST /api/conversations/{id}/events/respond_to_confirmation`
  - `POST /api/conversations/{id}/ask_agent`
  - `POST /api/conversations/{id}/pause`
  - `POST /api/conversations/{id}/run`
  - `GET /api/v1/conversation/{id}/events/search`
  - `GET /api/v1/git/changes?conversation_id=...&path=...`
  - `GET /api/v1/git/diff?conversation_id=...&path=...`
- WebSocket gateways:
  - `WS /ws/events/{id}` -> runtime `/sockets/events/{id}`
  - `WS /ws/bash-events/{id}` -> runtime `/sockets/bash-events`

## Temporary settings/secrets compatibility

`app_server/temporary_settings.py` intentionally contains a `TEMPORARY` comment. These routes exist so current Agent Canvas cloud-style callers can round-trip settings while the proper Agent Canvas-owned per-backend profile/settings store is built:

- `GET/POST /api/v1/settings`
- `GET /api/v1/settings/agent-schema`
- `GET /api/v1/settings/conversation-schema`
- `GET/POST/PUT/DELETE /api/v1/secrets`

MCP servers are stored only as opaque `agent_settings.mcp_config` data in this temporary compatibility store. Long-term MCP config should be owned by Agent Canvas + agent-server profiles/settings, not by app_server.
