"""Settings, LLM profile, and secrets routes under /api/v1.

Replaces the earlier opaque compatibility shim: app_server now owns user
settings, so these routes read and write the same ``Settings`` model that
conversation start builds its request from.

Ported (and trimmed for a single self-hosted user) from
``openhands/app_server/settings/settings_router.py`` and
``.../secrets/secrets_router.py`` in OpenHands/OpenHands.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Path, status
from fastapi.responses import JSONResponse
from openhands.sdk.llm import LLM
from openhands.sdk.settings import ConversationSettings, export_agent_settings_schema
from pydantic import BaseModel, Field

from .llm_profiles import (
    PROFILE_NAME_PATTERN,
    ProfileAlreadyExistsError,
    ProfileLimitExceededError,
    ProfileNotFoundError,
    StrictLLM,
    has_real_api_key,
)
from .settings import GETSettingsModel, Settings
from .state import AppState
from .user_secrets import (
    CustomSecret,
    CustomSecretCreate,
    CustomSecretWithoutValue,
    ProviderToken,
    ProviderTokenCreate,
    provider_tokens_set,
)

ProfileName = Annotated[str, Path(min_length=1, max_length=64, pattern=PROFILE_NAME_PATTERN)]

# Serializes the read-modify-write cycle behind settings and profile writes.
# app_server runs as a single-user, single-process control plane, so one lock
# closes the lost-update race between concurrent saves.
_write_lock = asyncio.Lock()


class ProfileInfo(BaseModel):
    name: str
    model: str | None = None
    base_url: str | None = None
    api_key_set: bool = False


class ProfileListResponse(BaseModel):
    profiles: list[ProfileInfo]
    active_profile: str | None = None


class ProfileDetailResponse(BaseModel):
    """``config.api_key`` is always None; ``api_key_set`` reports whether one is stored."""

    name: str
    config: dict[str, Any]
    api_key_set: bool = False


class ProfileMutationResponse(BaseModel):
    name: str
    message: str


class ActivateProfileResponse(BaseModel):
    name: str
    message: str
    model: str | None = None


class RenameProfileRequest(BaseModel):
    new_name: str = Field(..., min_length=1, max_length=64, pattern=PROFILE_NAME_PATTERN)


class SaveProfileRequest(BaseModel):
    """Body for saving a profile.

    If ``llm`` is provided it becomes the profile config; otherwise the current
    ``agent_settings.llm`` is snapshotted. ``llm`` is a :class:`StrictLLM`, so a
    typo returns 422 instead of being silently dropped.
    """

    include_secrets: bool = True
    llm: StrictLLM | None = None
    # Set when the caller has no new key (UI key field left blank), so an
    # existing profile's stored key survives instead of the snapshotted one.
    preserve_existing_api_key: bool = False


class SecretsListResponse(BaseModel):
    secrets: list[CustomSecretWithoutValue]


def build_settings_router(state: AppState) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["Settings"])

    # ── Settings ────────────────────────────────────────────────────

    @router.get("/settings", response_model=GETSettingsModel, responses={404: {"description": "Not found"}})
    async def get_settings() -> GETSettingsModel | JSONResponse:
        settings = state.load_settings()
        if settings is None:
            return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"error": "Settings not found"})
        return _settings_response(settings)

    @router.post("/settings", responses={400: {"description": "Invalid settings"}})
    async def save_settings(payload: dict[str, Any] = Body(default_factory=dict)) -> GETSettingsModel:
        async with _write_lock:
            existing = state.load_settings()
            settings = existing.model_copy(deep=True) if existing else Settings()
            try:
                settings.update(payload)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

            # The GET response nulls secret values, so a client round-tripping
            # settings sends `search_api_key: null` back. Absent *or* null both
            # mean "unchanged"; an explicit empty string still clears it.
            if (
                existing is not None
                and payload.get("search_api_key") is None
                and settings.search_api_key is None
            ):
                settings.search_api_key = existing.search_api_key

            state.save_settings(settings)
        return _settings_response(settings)

    @router.get("/settings/agent-schema")
    async def agent_schema() -> dict[str, Any]:
        """The SDK's own agent settings schema, for rendering the settings UI."""
        return export_agent_settings_schema().model_dump(mode="json")

    @router.get("/settings/conversation-schema")
    async def conversation_schema() -> dict[str, Any]:
        return ConversationSettings.export_schema().model_dump(mode="json")

    # ── LLM profiles ────────────────────────────────────────────────

    @router.get("/settings/profiles", response_model=ProfileListResponse)
    async def list_profiles() -> ProfileListResponse:
        settings = state.load_settings()
        if settings is None:
            return ProfileListResponse(profiles=[], active_profile=None)
        return ProfileListResponse(
            profiles=[ProfileInfo(**summary) for summary in settings.llm_profiles.summaries()],
            active_profile=settings.llm_profiles.active,
        )

    @router.get("/settings/profiles/{name}", response_model=ProfileDetailResponse)
    async def get_profile(name: ProfileName) -> ProfileDetailResponse:
        settings = state.load_settings()
        profile = settings.llm_profiles.get(name) if settings else None
        if profile is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Profile '{name}' not found")
        config = profile.model_dump(mode="json")
        config["api_key"] = None  # never echo a mask; use api_key_set instead
        return ProfileDetailResponse(name=name, config=config, api_key_set=has_real_api_key(profile.api_key))

    @router.post(
        "/settings/profiles/{name}",
        response_model=ProfileMutationResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def save_profile(
        name: ProfileName,
        request: Annotated[SaveProfileRequest | None, Body()] = None,
    ) -> ProfileMutationResponse:
        request = request or SaveProfileRequest()
        async with _write_lock:
            settings = state.load_settings()
            if settings is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Settings not found")

            existing = settings.llm_profiles.get(name)
            llm: LLM
            if request.llm is not None:
                llm = request.llm
                # Preserve the stored key when the caller omits it (e.g. a
                # client round-tripping a GET response, where it was nulled).
                if llm.api_key is None and existing is not None and existing.api_key is not None:
                    llm = llm.model_copy(update={"api_key": existing.api_key})
            else:
                llm = settings.agent_settings.llm
            if request.preserve_existing_api_key and existing is not None:
                llm = llm.model_copy(update={"api_key": existing.api_key})

            try:
                settings.llm_profiles.save(name, llm, include_secrets=request.include_secrets)
            except ProfileLimitExceededError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            # Overwriting the active profile would otherwise leave
            # agent_settings.llm stale, so `active` would lie about what runs.
            settings.reconcile_active_profile()
            state.save_settings(settings)

        return ProfileMutationResponse(name=name, message=f"Profile '{name}' saved")

    @router.delete("/settings/profiles/{name}", response_model=ProfileMutationResponse)
    async def delete_profile(name: ProfileName) -> ProfileMutationResponse:
        """Idempotent: succeeds even if the profile didn't exist."""
        async with _write_lock:
            settings = state.load_settings()
            if settings is not None and settings.delete_profile(name):
                state.save_settings(settings)
        return ProfileMutationResponse(name=name, message=f"Profile '{name}' deleted")

    @router.post("/settings/profiles/{name}/activate", response_model=ActivateProfileResponse)
    async def activate_profile(name: ProfileName) -> ActivateProfileResponse:
        async with _write_lock:
            settings = state.load_settings()
            if settings is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Settings not found")
            try:
                settings.switch_to_profile(name)
            except ProfileNotFoundError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
            state.save_settings(settings)
        return ActivateProfileResponse(
            name=name,
            message=f"Switched to profile '{name}'",
            model=settings.agent_settings.llm.model,
        )

    @router.post("/settings/profiles/{name}/rename", response_model=ProfileMutationResponse)
    async def rename_profile(name: ProfileName, request: RenameProfileRequest) -> ProfileMutationResponse:
        async with _write_lock:
            settings = state.load_settings()
            if settings is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Settings not found")
            try:
                settings.llm_profiles.rename(name, request.new_name)
            except ProfileNotFoundError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
            except ProfileAlreadyExistsError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            if settings.title_llm_profile == name:
                settings.title_llm_profile = request.new_name
            state.save_settings(settings)
        return ProfileMutationResponse(
            name=request.new_name, message=f"Profile '{name}' renamed to '{request.new_name}'"
        )

    # ── Secrets ─────────────────────────────────────────────────────

    @router.get("/secrets", response_model=SecretsListResponse)
    async def list_secrets() -> SecretsListResponse:
        secrets = state.load_secrets()
        return SecretsListResponse(
            secrets=[
                CustomSecretWithoutValue(name=name, description=secret.description)
                for name, secret in sorted(secrets.custom_secrets.items())
            ]
        )

    @router.post("/secrets", status_code=status.HTTP_201_CREATED)
    async def create_secret(request: CustomSecretCreate) -> dict[str, bool]:
        async with _write_lock:
            secrets = state.load_secrets()
            secrets.custom_secrets[request.name] = CustomSecret(secret=request.value, description=request.description)
            state.save_secrets(secrets)
        return {"success": True}

    @router.put("/secrets/{name}")
    async def update_secret(name: str, request: CustomSecretCreate) -> dict[str, bool]:
        if name != request.name:
            raise HTTPException(status_code=400, detail="Secret name in path and body must match")
        return await create_secret(request)

    @router.delete("/secrets/{name}")
    async def delete_secret(name: str) -> dict[str, bool]:
        async with _write_lock:
            secrets = state.load_secrets()
            secrets.custom_secrets.pop(name, None)
            state.save_secrets(secrets)
        return {"success": True}

    @router.post("/secrets/provider-tokens/{provider}")
    async def set_provider_token(provider: str, request: ProviderTokenCreate) -> dict[str, bool]:
        async with _write_lock:
            secrets = state.load_secrets()
            secrets.provider_tokens[provider] = ProviderToken(
                token=request.token, host=request.host, user_id=request.user_id
            )
            state.save_secrets(secrets)
        return {"success": True}

    @router.delete("/secrets/provider-tokens/{provider}")
    async def delete_provider_token(provider: str) -> dict[str, bool]:
        async with _write_lock:
            secrets = state.load_secrets()
            secrets.provider_tokens.pop(provider, None)
            state.save_secrets(secrets)
        return {"success": True}

    def _settings_response(settings: Settings) -> GETSettingsModel:
        """Build the GET response, stripping secret values but reporting they exist."""
        response = GETSettingsModel(
            **settings.model_dump(),
            llm_api_key_set=settings.llm_api_key_is_set,
            search_api_key_set=has_real_api_key(settings.search_api_key),
            provider_tokens_set=provider_tokens_set(state.load_secrets()),
        )
        llm = getattr(response.agent_settings, "llm", None)
        if llm is not None:
            llm.api_key = None
        response.search_api_key = None
        return response

    return router
