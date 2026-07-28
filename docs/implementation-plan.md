# Minimal app_server implementation plan

## Target architecture

Agent Canvas registers an app_server backend. app_server authenticates with a session key, creates or selects a sandbox, discovers the sandbox's agent-server URL/session key, starts a runtime conversation, and exposes app-level metadata plus HTTP/WebSocket gateway routes.

## Tests first

1. Add CI for lint + tests.
2. Add integration fixtures for a fake agent-server runtime.
3. Add tests for:
   - app_server session-key auth;
   - app-conversation start/search/batch-get;
   - sandbox pause/resume metadata;
   - HTTP proxy routes for events, ask_agent, git, send-message, confirmation;
   - WebSocket event and bash gateways;
   - temporary settings/secrets compatibility, including opaque MCP config storage.

## Implementation order

1. Config and auth dependencies.
2. In-memory/file-backed stores for sandboxes, conversations, start tasks, temporary settings/secrets.
3. Static sandbox provider for a pre-existing agent-server URL/session key.
4. Docker sandbox provider adapted from OpenHands/OpenHands non-enterprise Docker sandbox service: container metadata translation, session-key injection, port mapping, search/pagination, get-by-session-key, pause/resume/delete.
5. App-conversation router that forwards `StartConversationRequest` payloads to agent-server.
6. Proxy routers and WebSocket tunnels.
7. CI/doc polish and final validation.

## Settings ownership

Settings started out as a deliberately opaque compatibility shim, on the
assumption that Agent Canvas + agent-server profiles would own them. That plan
is superseded: **app_server owns user settings and secrets.**

The reason is that nothing else can. app_server is what calls the agent-server's
`POST /api/conversations`, so it is the only component positioned to decide
which LLM, MCP servers, condenser, and conversation limits a run uses. Storing
settings as opaque blobs meant the agent only ran with whatever configuration
the client happened to put in the start request — a backend that could not work
on its own.

Concretely:

- `Settings.agent_settings` and `Settings.conversation_settings` are the SDK's
  own models, so persisted settings are already in the shape the agent-server
  consumes. `ConversationSettings.create_request(StartConversationRequest)`
  builds the start payload; app_server does not hand-roll that JSON.
- This makes `openhands-sdk` a hard dependency, pinned in lockstep with the
  default agent-server image tag. The SDK is the single source of truth for the
  wire format and for the settings schema served to the UI.
- Secrets are owned here too: custom secrets become conversation environment
  variables, and git provider tokens are stored for repository access.
- MCP credentials survive the `GET -> edit -> POST` round trip
  (`app_server/mcp_secrets.py`). The GET response strips secret values, so
  without that merge a settings save would erase every configured MCP
  credential.

## Explicit non-goals

- No old OpenHands frontend.
- No SaaS org/billing/account flows.
- No org- or instance-scoped settings, marketplace composition, or Agent
  Profiles — those are cloud concepts that assume multiple users and orgs.
  app_server serves a single self-hosted user.
- No reimplementation of agent execution.
