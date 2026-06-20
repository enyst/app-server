# Minimal OpenHands app_server

A small FastAPI control plane intended to sit between Agent Canvas and sandbox-hosted `openhands-agent-server` runtimes.

The target shape is:

```text
Agent Canvas -> app_server -> sandbox -> agent-server
```

This repository is intentionally smaller than `OpenHands/OpenHands`: it keeps sandbox metadata, conversation metadata, app-server auth, and proxy/gateway routes, while leaving agent execution to `openhands-agent-server`.
