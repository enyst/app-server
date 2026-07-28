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

## Auth model

Self-hosted Agent Canvas can use a normal app-server session key. Send it as `X-Session-API-Key`; `Authorization: Bearer <same-key>` is also accepted as a compatibility bridge for current Agent Canvas cloud-style backend calls. The intended Agent Canvas shape is app_server protocol routes with session-key auth; current Agent Canvas may still need a backend kind/auth-mode split to express that cleanly.


Run with Docker sandbox orchestration instead of a pre-existing runtime:

```bash
SESSION_API_KEY=app-secret \
APP_SERVER_SANDBOX_PROVIDER=docker \
AGENT_SERVER_IMAGE=ghcr.io/openhands/agent-server:1.38.0-python \
SANDBOX_CONTAINER_URL_PATTERN='http://localhost:{port}' \
python -m uvicorn app_server.app:create_app --factory --host 0.0.0.0 --port 8000
```

In Docker mode, `POST /api/v1/app-conversations` creates a new agent-server container, injects `OH_SESSION_API_KEYS_0`, maps the agent-server port, waits for the sandbox to report `RUNNING`, stores the resulting sandbox metadata, and then starts the runtime conversation.

Runtime state is file-backed under `APP_SERVER_STATE_DIR` (default `.app-server-state`): app-conversation records, start tasks, and known sandbox metadata are restored when the process restarts. Docker mode can also rediscover live containers through the sandbox provider's Docker search APIs.



## Implemented surface

- Health/status: `/alive`, `/health`, `/ready`, `/server_info`
- App conversations:
  - `POST /api/v1/app-conversations`
  - `GET /api/v1/app-conversations/search`
  - `GET /api/v1/app-conversations?ids=...`
  - `GET /api/v1/app-conversations/start-tasks?ids=...`
  - `POST /api/v1/app-conversations/{id}/send-message`
- Sandbox specs/control:
  - `GET /api/v1/sandbox-specs/search`
  - `GET /api/v1/sandbox-specs?id=...`
  - `GET /api/v1/sandboxes/search`
  - `GET /api/v1/sandboxes?id=...`
  - `POST /api/v1/sandboxes`
  - `POST /api/v1/sandboxes/{id}/pause`
  - `POST /api/v1/sandboxes/{id}/resume`
  - `DELETE /api/v1/sandboxes/{id}`
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

## Settings

app_server owns user settings and secrets, and builds every conversation from them. `agent_settings` and `conversation_settings` are stored as the SDK's own models, so a saved setting is already in the shape agent-server consumes: conversation start turns them into a `StartConversationRequest` via `ConversationSettings.create_request` rather than hand-rolled JSON.

That makes `openhands-sdk` a hard dependency, pinned in lockstep with the default agent-server image tag. Bump both together.

```bash
# Configure an LLM before starting conversations
curl -X POST localhost:8000/api/v1/settings \
  -H 'X-Session-API-Key: app-secret' -H 'Content-Type: application/json' \
  -d '{"agent_settings_diff": {"llm": {"model": "anthropic/claude-sonnet-4-5", "api_key": "sk-..."}}}'
```

Notes:

- Writes are partial: `agent_settings_diff` and `conversation_settings_diff` deep-merge onto what is stored, so saving one settings page never clobbers another's fields. Sending the legacy nested `agent_settings` / `conversation_settings` keys is rejected with a 400.
- Secret values are never echoed. `GET /api/v1/settings` nulls `llm.api_key` and strips MCP credentials, reporting `llm_api_key_set` instead. A `GET -> edit -> POST` round trip preserves the stored secrets.
- `POST /api/v1/app-conversations` returns 400 if no LLM is configured, before any sandbox is started.
- Custom secrets become environment variables in the conversation.

### Settings surface

- `GET/POST /api/v1/settings`
- `GET /api/v1/settings/agent-schema`, `GET /api/v1/settings/conversation-schema` (served from the SDK, for rendering a settings UI)
- LLM profiles:
  - `GET /api/v1/settings/profiles`
  - `GET/POST/DELETE /api/v1/settings/profiles/{name}`
  - `POST /api/v1/settings/profiles/{name}/activate`
  - `POST /api/v1/settings/profiles/{name}/rename`
- Secrets:
  - `GET/POST /api/v1/secrets`, `PUT/DELETE /api/v1/secrets/{name}`
  - `POST/DELETE /api/v1/secrets/provider-tokens/{provider}`

Start a conversation with a one-off LLM override by passing `{"llm_profile": "<name>"}`; the override is not persisted.
