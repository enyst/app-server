from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


class AppServerConfig(BaseModel):
    session_api_keys: list[str] = Field(default_factory=list)
    state_dir: Path = Field(default=Path(".app-server-state"))
    static_agent_server_url: str | None = None
    static_agent_server_session_key: str | None = None
    public_base_url: str | None = None
    enable_websocket_gateway: bool = True
    request_timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> AppServerConfig:
        keys = []
        for name in ("SESSION_API_KEY", "OH_SESSION_API_KEYS_0", "LOCAL_BACKEND_API_KEY"):
            value = os.environ.get(name)
            if value and value not in keys:
                keys.append(value)
        return cls(
            session_api_keys=keys,
            state_dir=Path(os.environ.get("APP_SERVER_STATE_DIR", ".app-server-state")),
            static_agent_server_url=os.environ.get("AGENT_SERVER_URL"),
            static_agent_server_session_key=os.environ.get("AGENT_SERVER_SESSION_API_KEY")
            or os.environ.get("RUNTIME_SESSION_API_KEY"),
            public_base_url=os.environ.get("APP_SERVER_PUBLIC_BASE_URL"),
            enable_websocket_gateway=os.environ.get("ENABLE_WEBSOCKET_GATEWAY", "true").lower()
            in {"1", "true", "yes"},
        )
