"""Turn persisted settings into an agent-server ``StartConversationRequest``.

This is the point of the settings store: what the user saved under
``/api/v1/settings`` is what the agent actually runs with. The SDK owns the
wire format, so we populate its models and let ``ConversationSettings.create_request``
assemble the payload rather than hand-rolling JSON.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from openhands.sdk.conversation.request import SendMessageRequest, StartConversationRequest
from openhands.sdk.llm import TextContent
from openhands.sdk.secret import StaticSecret
from openhands.sdk.workspace import LocalWorkspace

from .llm_profiles import has_real_api_key
from .settings import Settings
from .user_secrets import Secrets

DEFAULT_WORKING_DIR = "workspace/project"


def _resolve_initial_message(value: Any) -> SendMessageRequest | None:
    """Accept either a plain string or a SendMessageRequest-shaped dict."""
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        return SendMessageRequest(role="user", content=[TextContent(text=value)], run=True)
    if isinstance(value, SendMessageRequest):
        return value
    if isinstance(value, dict):
        payload = dict(value)
        # `run` defaults to False on the SDK model, but an initial message is
        # always meant to start the agent.
        payload.setdefault("run", True)
        content = payload.get("content")
        if isinstance(content, str):
            payload["content"] = [TextContent(text=content).model_dump()]
        return SendMessageRequest.model_validate(payload)
    raise HTTPException(status_code=400, detail="initial_message must be a string or an object")


def _resolve_workspace(body: dict[str, Any]) -> LocalWorkspace:
    workspace = body.get("workspace")
    if isinstance(workspace, dict):
        working_dir = workspace.get("working_dir") or DEFAULT_WORKING_DIR
    elif isinstance(workspace, str):
        working_dir = workspace
    else:
        working_dir = DEFAULT_WORKING_DIR
    return LocalWorkspace(working_dir=working_dir)


def _apply_profile(settings: Settings, body: dict[str, Any]) -> Settings:
    """Apply a one-off LLM profile override for this conversation.

    The override is not persisted — it only affects the request being built.
    """
    name = body.get("llm_profile") or body.get("profile_name")
    if not name:
        return settings
    if not settings.llm_profiles.has(name):
        raise HTTPException(status_code=404, detail=f"Unknown LLM profile '{name}'")
    launch = settings.model_copy(deep=True)
    launch.switch_to_profile(name)
    return launch


def build_start_request(
    settings: Settings | None,
    secrets: Secrets,
    body: dict[str, Any],
    conversation_id: str,
) -> dict[str, Any]:
    """Build the JSON body for the agent-server ``POST /api/conversations``.

    Raises 400 when no usable LLM is configured — without this the agent-server
    would accept the conversation and only fail once the agent first runs,
    which surfaces to the user as a dead conversation rather than a clear error.
    """
    if settings is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No settings configured. Save an LLM via POST /api/v1/settings first.",
        )

    launch = _apply_profile(settings, body)
    llm = getattr(launch.agent_settings, "llm", None)
    if llm is None or not llm.model:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No LLM model configured. Save one via POST /api/v1/settings first.",
        )
    if not has_real_api_key(llm.api_key):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No API key configured for model '{llm.model}'.",
        )

    conversation_settings = launch.conversation_settings.model_copy(
        update={
            "agent_settings": launch.agent_settings,
            "workspace": _resolve_workspace(body),
            "conversation_id": conversation_id,
            "initial_message": _resolve_initial_message(body.get("initial_message")),
        }
    )

    # Per-conversation overrides of persisted conversation settings.
    for key in ("max_iterations", "confirmation_mode", "security_analyzer"):
        if key in body and body[key] is not None:
            conversation_settings = conversation_settings.model_copy(update={key: body[key]})

    request = conversation_settings.create_request(
        StartConversationRequest,
        secrets={name: StaticSecret(value=value) for name, value in secrets.get_env_vars().items()},
    )
    return request.model_dump(mode="json", context={"expose_secrets": True})
