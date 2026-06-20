# Repository Notes

- This repository is a minimal app_server control plane for Agent Canvas + sandbox-hosted OpenHands agent-server runtimes.
- Keep implementation small and test-first. Unit/integration tests live under `tests/`; CI runs `ruff` and `pytest`.
- Canonical runtime settings should not be owned here long-term. Temporary settings/secrets compatibility modules must include a clear `TEMPORARY` source comment.
- Prefer `X-Session-API-Key` auth for self-hosted app_server deployments. OAuth device flow is not required for the minimal self-hosted bridge unless a real durable auth service is added.
- The app_server should orchestrate sandboxes and proxy/tunnel traffic; do not reimplement agent-server internals.
- Validate with `python -m pytest` and `python -m ruff check .`.
