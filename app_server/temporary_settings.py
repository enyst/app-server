from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from .state import AppState, deep_merge

# TEMPORARY: compatibility settings/secrets surface for current Agent Canvas cloud-style
# callers. Long term, Agent Canvas + agent-server should own LLM profiles, MCP config,
# saved secrets, and app preferences; app_server should only receive resolved runtime
# settings at conversation start.


class SecretCreateRequest(BaseModel):
    name: str
    value: str
    description: str | None = None


def build_temporary_router(state: AppState) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get("/settings")
    async def get_settings() -> dict[str, Any]:
        settings = state.settings()
        return {
            **settings,
            "llm_api_key_set": bool(settings.get("agent_settings", {}).get("llm", {}).get("api_key")),
            "search_api_key_set": False,
            "provider_tokens_set": {},
        }

    @router.post("/settings")
    async def save_settings(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        settings = state.settings()
        if agent_diff := body.get("agent_settings_diff"):
            settings["agent_settings"] = deep_merge(settings.get("agent_settings", {}), agent_diff)
        if conversation_diff := body.get("conversation_settings_diff"):
            settings["conversation_settings"] = deep_merge(
                settings.get("conversation_settings", {}), conversation_diff
            )
        app_preferences = {
            key: value
            for key, value in body.items()
            if key
            not in {
                "agent_settings_diff",
                "conversation_settings_diff",
                "misc_settings_diff",
                "app_preferences",
            }
        }
        if body.get("app_preferences"):
            app_preferences.update(body["app_preferences"])
        if app_preferences:
            settings["app_preferences"] = deep_merge(
                settings.get("app_preferences", {}), app_preferences
            )
        state.save_settings(settings)
        return await get_settings()

    @router.get("/settings/agent-schema")
    async def agent_schema() -> dict[str, Any]:
        return {"sections": []}

    @router.get("/settings/conversation-schema")
    async def conversation_schema() -> dict[str, Any]:
        return {"sections": []}

    @router.get("/secrets")
    async def list_secrets() -> dict[str, list[dict[str, str | None]]]:
        return {
            "secrets": [
                {"name": name, "description": item.get("description")}
                for name, item in sorted(state.secrets().items())
            ]
        }

    @router.post("/secrets")
    async def create_secret(request: SecretCreateRequest) -> dict[str, bool]:
        secrets = state.secrets()
        secrets[request.name] = {"value": request.value, "description": request.description}
        state.save_secrets(secrets)
        return {"success": True}

    @router.put("/secrets")
    async def upsert_secret(request: SecretCreateRequest) -> dict[str, bool]:
        return await create_secret(request)

    @router.delete("/secrets/{name}")
    async def delete_secret(name: str) -> dict[str, bool]:
        secrets = state.secrets()
        secrets.pop(name, None)
        state.save_secrets(secrets)
        return {"success": True}

    @router.get("/secrets/{name}")
    async def get_secret(name: str) -> str:
        item = state.secrets().get(name)
        if not item:
            raise HTTPException(status_code=404, detail="unknown secret")
        return item.get("value") or ""

    return router
