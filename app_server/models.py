from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class SandboxStatus(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    ERROR = "ERROR"


class Sandbox(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    status: SandboxStatus = SandboxStatus.RUNNING
    agent_server_url: str
    session_api_key: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AppConversation(BaseModel):
    id: str
    sandbox_id: str
    status: str = "idle"
    conversation_url: str
    session_api_key: str | None = None
    websocket_url: str | None = None
    sandbox_status: SandboxStatus = SandboxStatus.RUNNING
    selected_repository: str | None = None
    selected_branch: str | None = None
    git_provider: str | None = None
    title: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AppConversationPage(BaseModel):
    items: list[AppConversation]
    next_page_id: str | None = None


class StartTaskStatus(StrEnum):
    WORKING = "WORKING"
    READY = "READY"
    ERROR = "ERROR"


class AppConversationStartTask(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    status: StartTaskStatus
    detail: str | None = None
    app_conversation_id: str | None = None
    sandbox_id: str | None = None
    agent_server_url: str | None = None
    request: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AppSendMessageResponse(BaseModel):
    success: bool
    sandbox_status: SandboxStatus
    message: str | None = None


def normalize_uuid(value: str | UUID) -> str:
    return str(UUID(str(value).replace("-", "")))
