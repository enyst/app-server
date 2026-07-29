# Repository Notes

- This repository is a minimal app_server control plane for Agent Canvas + sandbox-hosted OpenHands agent-server runtimes.
- Keep implementation small and test-first. Unit/integration tests live under `tests/`; CI runs `ruff` and `pytest`.
- Settings, profiles, secrets, and their tests were ported from `OpenHands/OpenHands` at commit `ee9e78b7` — the last commit before `cb9138ca` ("clear repository for Agent Canvas migration") deleted that tree. When touching this code, check the upstream original first: each ported module and test file names its source in the docstring. Cloud-only concepts (orgs, marketplaces, Agent Profiles, the LiteLLM proxy) are intentionally not ported.
- app_server owns user settings and secrets: it is the component that calls agent-server's `POST /api/conversations`, so it decides what the agent runs with. Store `agent_settings`/`conversation_settings` as the SDK's own models and build start requests with `ConversationSettings.create_request` — never hand-roll that JSON.
- `openhands-sdk` is pinned in lockstep with the default agent-server image tag in `config.py`. Bump both together.
- Never echo secret values back from a GET. Secrets are stripped from responses and restored from storage on write, so a `GET -> edit -> POST` round trip cannot erase them (`app_server/mcp_secrets.py`).
- Prefer `X-Session-API-Key` auth for self-hosted app_server deployments. OAuth device flow is not required for the minimal self-hosted bridge unless a real durable auth service is added.
- The app_server should orchestrate sandboxes and proxy/tunnel traffic; do not reimplement agent-server internals.
- Validate with `python -m pytest` and `python -m ruff check .`. Integration tests under `tests/integration/` (marked `integration`) boot a real agent-server subprocess; they pin `openhands-agent-server`/`openhands-tools` to the same version as `openhands-sdk` and prove the start-conversation payload is schema-valid against the real runtime. Keep all three versions in lockstep with the image tag in `config.py`.
