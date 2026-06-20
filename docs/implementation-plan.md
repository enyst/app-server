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
4. App-conversation router that forwards `StartConversationRequest` payloads to agent-server.
5. Proxy routers and WebSocket tunnels.
6. CI/doc polish and final validation.

## Explicit non-goals

- No old OpenHands frontend.
- No SaaS org/billing/account flows.
- No canonical user settings/secrets ownership in app_server beyond temporary compatibility routes.
- No reimplementation of agent execution.
