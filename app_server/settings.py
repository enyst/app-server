"""Persisted app_server settings.

``agent_settings`` and ``conversation_settings`` are SDK models — the same
types the agent-server consumes — so a saved setting can be turned into a
``StartConversationRequest`` without a translation layer. Everything else is
product state this control plane owns.

Ported (and trimmed for self-hosted use) from
``openhands/app_server/settings/settings_models.py`` in OpenHands/OpenHands.
Cloud-only concepts — org marketplaces, Agent Profiles, analytics consent
plumbing, the legacy ``secrets_store`` migration field — are deliberately
absent; see docs/implementation-plan.md.
"""

from __future__ import annotations

from typing import Any

from openhands.sdk.settings import (
    ACPAgentSettings,
    AgentSettingsConfig,
    ConversationSettings,
    OpenHandsAgentSettings,
    apply_agent_settings_diff,
    default_agent_settings,
    validate_agent_settings,
)
from openhands.sdk.utils.pydantic_secrets import REDACTED_SECRET_VALUE
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    SerializationInfo,
    field_serializer,
    model_validator,
)

from .llm_profiles import LLMProfiles
from .mcp_secrets import normalize_mcp_config, preserve_redacted_mcp_secrets

# Fields the batch ``update()`` refuses to touch. Profile mutations go through
# /api/v1/settings/profiles/... which validate input and enforce the count cap;
# accepting a raw dict here would bypass both.
_UPDATE_IGNORED_FIELDS = frozenset(["llm_profiles"])


def _coerce_value(value: Any) -> Any:
    """Unwrap SecretStr to a plain value."""
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def _coerce_dict_secrets(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively unwrap SecretStr leaves."""
    return {
        key: _coerce_dict_secrets(value) if isinstance(value, dict) else _coerce_value(value)
        for key, value in data.items()
    }


def _drop_unchanged_secrets(diff: dict[str, Any]) -> dict[str, Any]:
    """Strip secret leaves that mean "unchanged" rather than "clear this".

    ``GET /api/v1/settings`` nulls ``llm.api_key`` and masks nested secrets, so
    a client that echoes the response back would otherwise wipe the stored
    credential. ``None`` and the redaction marker both mean "I have nothing new
    for you"; an explicit empty string still clears the secret.
    """
    cleaned: dict[str, Any] = {}
    for key, value in diff.items():
        if isinstance(value, dict):
            cleaned[key] = _drop_unchanged_secrets(value)
        elif key.endswith("api_key") and (value is None or value == REDACTED_SECRET_VALUE):
            continue
        else:
            cleaned[key] = value
    return cleaned


def deep_merge(base: dict[str, Any], diff: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``diff`` onto ``base``, returning a new dict."""
    merged = dict(base)
    for key, value in diff.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class Settings(BaseModel):
    """Settings persisted by app_server for the single self-hosted user."""

    model_config = ConfigDict(populate_by_name=True)

    # Agent/conversation configuration, owned by the SDK models.
    agent_settings: AgentSettingsConfig = Field(default_factory=default_agent_settings)
    conversation_settings: ConversationSettings = Field(default_factory=ConversationSettings)
    llm_profiles: LLMProfiles = Field(
        default_factory=LLMProfiles,
        description="Saved LLM profiles and the currently active profile name.",
    )

    # Product settings owned by app_server.
    language: str | None = None
    enable_sound_notifications: bool = False
    enable_proactive_conversation_starters: bool = True
    search_api_key: SecretStr | None = None
    max_budget_per_task: float | None = None
    git_user_name: str | None = None
    git_user_email: str | None = None
    git_full_clone: bool = False
    title_llm_profile: str | None = None
    default_sandbox_spec_id: str | None = None

    @property
    def llm_api_key_is_set(self) -> bool:
        raw = getattr(self.agent_settings, "llm", None)
        raw = getattr(raw, "api_key", None)
        if raw is None:
            return False
        secret_value = raw.get_secret_value() if isinstance(raw, SecretStr) else str(raw)
        return bool(secret_value and secret_value.strip())

    # ── Batch update ────────────────────────────────────────────────

    def reconcile_active_profile(self) -> None:
        """Clear ``llm_profiles.active`` when the live LLM diverges from it.

        The active profile is a pointer into ``llm_profiles.profiles``. If the
        user edits ``agent_settings.llm`` directly, the pointer becomes a lie —
        drop the marker rather than mutating the saved profile.
        """
        active = self.llm_profiles.active
        if active is None:
            return
        saved = self.llm_profiles.get(active)
        if saved is None or saved != getattr(self.agent_settings, "llm", None):
            self.llm_profiles.active = None

    def update(self, payload: dict[str, Any]) -> None:
        """Apply a batch of changes from a partial payload.

        ``agent_settings_diff`` and ``conversation_settings_diff`` deep-merge
        onto the persisted values, so saving one settings page never clobbers
        fields owned by another. Other keys are set directly.
        """
        legacy_nested_keys = [key for key in ("agent_settings", "conversation_settings") if key in payload]
        if legacy_nested_keys:
            raise ValueError(
                "Use *_diff nested settings payloads instead of legacy " + ", ".join(sorted(legacy_nested_keys))
            )

        agent_update = payload.get("agent_settings_diff")
        if isinstance(agent_update, dict):
            # mcp_config carries its own redaction handling below, so keep it
            # out of the generic secret-stripping pass.
            mcp_config_value = agent_update.get("mcp_config")
            agent_update = _drop_unchanged_secrets(
                {key: value for key, value in agent_update.items() if key != "mcp_config"}
            )
            if "mcp_config" in payload["agent_settings_diff"]:
                agent_update["mcp_config"] = mcp_config_value

            coerced: dict[str, Any] = {
                key: value if isinstance(value, dict) else _coerce_value(value) for key, value in agent_update.items()
            }

            # mcp_config replaces wholesale rather than deep-merging (a merge
            # could never remove a server), so hold it back from the
            # variant-aware merge and apply it afterwards.
            replace_mcp_config = "mcp_config" in agent_update
            mcp_config = (
                normalize_mcp_config(
                    preserve_redacted_mcp_secrets(
                        coerced.pop("mcp_config", None),
                        getattr(self.agent_settings, "mcp_config", None),
                    )
                )
                if replace_mcp_config
                else None
            )

            # The SDK owns the discriminated-union merge: replace on
            # agent_kind change, deep-merge within a variant.
            new_settings = apply_agent_settings_diff(self.agent_settings, coerced)
            if replace_mcp_config:
                dumped = new_settings.model_dump(mode="json", context={"expose_secrets": True})
                dumped["mcp_config"] = mcp_config
                new_settings = validate_agent_settings(dumped)

            # object.__setattr__ avoids validate_assignment side-effects.
            object.__setattr__(self, "agent_settings", new_settings)

        conv_update = payload.get("conversation_settings_diff")
        if isinstance(conv_update, dict):
            merged = deep_merge(self.conversation_settings.model_dump(mode="json"), conv_update)
            object.__setattr__(self, "conversation_settings", ConversationSettings.model_validate(merged))

        for key, value in payload.items():
            if key in ("agent_settings_diff", "conversation_settings_diff"):
                continue
            if key in Settings.model_fields and key not in _UPDATE_IGNORED_FIELDS:
                annotation = Settings.model_fields[key].annotation
                # Coerce plain strings where the field expects a SecretStr.
                if isinstance(value, str) and (
                    annotation is SecretStr or SecretStr in getattr(annotation, "__args__", ())
                ):
                    value = SecretStr(value) if value else None
                setattr(self, key, value)

        self.reconcile_active_profile()

    # ── Profile activation ──────────────────────────────────────────

    def switch_to_profile(self, name: str) -> None:
        """Point ``agent_settings.llm`` at a saved profile."""
        # Copy the LLM so post-activation fixups don't bleed back into the
        # saved profile: model_copy(update=...) is shallow, so the update value
        # would otherwise be shared with llm_profiles.profiles[name].
        llm = self.llm_profiles.require(name)
        self.agent_settings = self.agent_settings.model_copy(update={"llm": llm.model_copy()})
        self.llm_profiles.active = name

    def delete_profile(self, name: str) -> bool:
        """Delete a saved profile, promoting a fallback when it was active.

        Returns False if the profile didn't exist. When the deleted profile was
        active and others remain, switches to the first remaining one so the
        user isn't left without an active LLM.
        """
        was_active = self.llm_profiles.active == name
        if not self.llm_profiles.delete(name):
            return False
        if self.title_llm_profile == name:
            self.title_llm_profile = None
        if was_active and self.llm_profiles.profiles:
            self.switch_to_profile(next(iter(self.llm_profiles.profiles)))
        return True

    # ── Validation / serialization ──────────────────────────────────

    @model_validator(mode="before")
    @classmethod
    def _normalize_inputs(cls, data: dict | object) -> dict | object:
        """Route persisted agent/conversation settings through SDK loaders.

        The SDK loaders apply registered schema migrations, so a settings.json
        written by an older SDK still loads.
        """
        if not isinstance(data, dict):
            return data

        agent_settings = data.get("agent_settings")
        if isinstance(agent_settings, dict):
            data["agent_settings"] = validate_agent_settings(_coerce_dict_secrets(agent_settings)).model_dump(
                mode="json", context={"expose_secrets": True}
            )
        elif isinstance(agent_settings, (OpenHandsAgentSettings, ACPAgentSettings)):
            data["agent_settings"] = agent_settings.model_dump(mode="json", context={"expose_secrets": True})

        conversation_settings = data.get("conversation_settings")
        if isinstance(conversation_settings, dict):
            data["conversation_settings"] = ConversationSettings.from_persisted(conversation_settings).model_dump(
                mode="json"
            )
        elif isinstance(conversation_settings, ConversationSettings):
            data["conversation_settings"] = conversation_settings.model_dump(mode="json")

        return data

    @field_serializer("search_api_key")
    def _search_api_key_serializer(self, api_key: SecretStr | None, info: SerializationInfo):
        if api_key is None:
            return None
        secret_value = api_key.get_secret_value()
        if not secret_value or not secret_value.strip():
            return None
        context = info.context
        if context and context.get("expose_secrets", False):
            return secret_value
        return str(api_key)

    @field_serializer("agent_settings")
    def _agent_settings_serializer(
        self, agent_settings: OpenHandsAgentSettings | ACPAgentSettings, info: SerializationInfo
    ) -> dict[str, Any]:
        context = info.context or {}
        if context.get("expose_secrets", False):
            return agent_settings.model_dump(mode="json", context={"expose_secrets": True})
        return agent_settings.model_dump(mode="json")


class GETSettingsModel(Settings):
    """Settings response with the extra "is it set?" flags the frontend needs.

    Secret values themselves are nulled out by the router before returning.
    """

    model_config = ConfigDict(use_enum_values=True)

    llm_api_key_set: bool = False
    search_api_key_set: bool = False
    provider_tokens_set: dict[str, str | None] = Field(default_factory=dict)
