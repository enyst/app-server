"""Saved LLM configurations ("profiles") and the currently active one.

Ported from ``openhands/app_server/settings/llm_profiles.py`` in
OpenHands/OpenHands, minus the managed-proxy/base-url resolution which is a
cloud concern — a self-hosted app_server has no LiteLLM proxy to infer.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from openhands.sdk.llm import LLM
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    SerializationInfo,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

logger = logging.getLogger(__name__)

# Soft cap keeping the persisted Settings payload bounded.
MAX_PROFILES: Final[int] = 10

# Alphanumerics plus . _ - only, 1-64 chars. Blocks empty names,
# path-traversal fragments, and slash-in-name routing ambiguity.
PROFILE_NAME_PATTERN: Final[str] = r"^[A-Za-z0-9._-]{1,64}$"


def has_real_api_key(api_key: Any) -> bool:
    """True iff ``api_key`` carries a non-empty value.

    A ``SecretStr("")`` reports as *not set* — otherwise the UI claims a key is
    stored when it isn't.
    """
    if api_key is None:
        return False
    secret_value = api_key.get_secret_value() if isinstance(api_key, SecretStr) else str(api_key)
    return bool(secret_value and secret_value.strip())


class ProfileNotFoundError(LookupError):
    """Raised when a lookup or activation references an unknown profile."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Profile '{name}' not found")


class ProfileLimitExceededError(ValueError):
    """Raised when saving a new profile would exceed :data:`MAX_PROFILES`."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"Profile limit reached ({limit}). Delete a profile before saving a new one.")


class ProfileAlreadyExistsError(ValueError):
    """Raised when a rename target collides with an existing profile."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Profile '{name}' already exists")


class StrictLLM(LLM):
    """LLM variant that rejects unknown fields.

    The base ``LLM`` has ``extra="ignore"``, so typos silently disappear. For
    API input we want to fail loud rather than 201 a dropped field.
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="wrap")
    @classmethod
    def _restore_is_subscription(cls, data: Any, handler: Any) -> Any:
        """Drop ``is_subscription`` from input instead of restoring it.

        ``LLM`` defines a ``mode="wrap"`` validator of this same name that
        reads its own raw ``data``, so shadowing the method is the only way to
        override it. We strip the computed field so ``extra="forbid"`` doesn't
        reject a GET-response echo on the GET -> edit -> POST round trip; it is
        only ever meant to be set via ``LLM.subscription_login()``.

        See OpenHands/software-agent-sdk#3942.
        """
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if k != "is_subscription"}
        return handler(data)


class LLMProfiles(BaseModel):
    """Named ``LLM`` configurations plus the active one, if any.

    Invariants (enforced on validate and on assignment):
    - ``active`` is either ``None`` or a key of ``profiles``.
    - Individual profiles that fail to parse (schema drift) are dropped with a
      warning rather than failing the whole ``Settings`` load.
    """

    model_config = ConfigDict(validate_assignment=True)

    profiles: dict[str, LLM] = Field(default_factory=dict)
    active: str | None = None

    @field_validator("profiles", mode="before")
    @classmethod
    def _skip_invalid_profiles(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        valid: dict[str, Any] = {}
        for name, raw in value.items():
            if isinstance(raw, LLM):
                valid[name] = raw
                continue
            try:
                valid[name] = LLM.model_validate(raw)
            except ValidationError as exc:
                logger.warning("Skipping invalid LLM profile %r: %s", name, exc)
        return valid

    @model_validator(mode="after")
    def _reconcile_active(self) -> LLMProfiles:
        if self.active is not None and self.active not in self.profiles:
            # Bypass validate_assignment to avoid re-entering this validator.
            object.__setattr__(self, "active", None)
        return self

    def get(self, name: str) -> LLM | None:
        return self.profiles.get(name)

    def require(self, name: str) -> LLM:
        llm = self.profiles.get(name)
        if llm is None:
            raise ProfileNotFoundError(name)
        return llm

    def has(self, name: str) -> bool:
        return name in self.profiles

    def summaries(self) -> list[dict[str, Any]]:
        """A ``{name, model, base_url, api_key_set}`` dict per profile.

        ``api_key_set`` mirrors the ``llm_api_key_set`` convention used by the
        main settings response, so the frontend can render "key stored" vs
        "needs key" without fetching each profile.
        """
        return [
            {
                "name": name,
                "model": llm.model,
                "base_url": llm.base_url,
                "api_key_set": has_real_api_key(llm.api_key),
            }
            for name, llm in self.profiles.items()
        ]

    def save(self, name: str, llm: LLM, include_secrets: bool = True) -> None:
        """Save ``llm`` under ``name``, overwriting an existing profile.

        Stores a copy so later caller-side mutation doesn't bleed into the
        stored profile.
        """
        if name not in self.profiles and len(self.profiles) >= MAX_PROFILES:
            raise ProfileLimitExceededError(MAX_PROFILES)

        update = {} if include_secrets else {"api_key": None}
        self.profiles[name] = llm.model_copy(update=update)

    def rename(self, old_name: str, new_name: str) -> None:
        """Rename a profile, preserving config, insertion order, and active flag."""
        if old_name not in self.profiles:
            raise ProfileNotFoundError(old_name)
        if new_name == old_name:
            return
        if new_name in self.profiles:
            raise ProfileAlreadyExistsError(new_name)

        # Capture `active` before reassigning `profiles`: the model validator
        # runs on assignment and would null it out (old_name is gone from the
        # rebuilt dict), losing the signal.
        was_active = self.active == old_name

        # Rebuild to preserve insertion order, so the renamed profile keeps its
        # slot rather than moving to the end.
        renamed: dict[str, LLM] = {(new_name if key == old_name else key): llm for key, llm in self.profiles.items()}
        self.profiles = renamed
        if was_active:
            object.__setattr__(self, "active", new_name)

    def delete(self, name: str) -> bool:
        """Delete a profile. Returns True if it existed."""
        if name not in self.profiles:
            return False
        del self.profiles[name]
        if self.active == name:
            object.__setattr__(self, "active", None)
        return True

    @field_serializer("profiles")
    def _profiles_serializer(self, profiles: dict[str, LLM], info: SerializationInfo) -> dict[str, Any]:
        return {name: llm.model_dump(mode="json", context=info.context) for name, llm in profiles.items()}
