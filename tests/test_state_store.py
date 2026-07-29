"""File-backed settings and secrets persistence.

Adapted from ``tests/unit/storage/settings/test_file_settings_store.py`` and
``tests/unit/storage/data_models/test_secret_store.py`` in OpenHands/OpenHands
(at ee9e78b7).

Upstream's ``Secrets`` used frozen models over ``MappingProxyType`` with
``from_value`` coercion so a shared multi-user store stayed immutable; ours is a
single-user file store using plain dicts, so those construction-permutation
tests do not carry over. What matters here is the same: secrets survive the
round trip through disk, and are masked unless explicitly exposed.

The legacy "seed a Default profile from ``agent_settings.llm``" upgrade path is
also not ported — it migrates pre-``llm_profiles`` files that cannot exist here.
"""

from __future__ import annotations

from openhands.sdk.llm import LLM
from openhands.sdk.settings import ConversationSettings, OpenHandsAgentSettings
from pydantic import SecretStr

from app_server.settings import Settings
from app_server.state import AppState
from app_server.user_secrets import CustomSecret, ProviderToken, Secrets

# ── Settings persistence ────────────────────────────────────────────


def test_load_returns_none_when_nothing_persisted(tmp_path):
    assert AppState(tmp_path).load_settings() is None


def test_store_and_load_settings(tmp_path):
    state = AppState(tmp_path)
    original = Settings(
        language="python",
        agent_settings=OpenHandsAgentSettings(
            agent="test-agent",
            llm=LLM(
                model="test-model",
                api_key=SecretStr("test-key"),
                base_url="https://test.com",
            ),
        ),
        conversation_settings=ConversationSettings(
            max_iterations=100,
            security_analyzer="llm",
            confirmation_mode=True,
        ),
    )

    state.save_settings(original)
    loaded = AppState(tmp_path).load_settings()

    assert loaded is not None
    assert loaded.language == original.language
    assert loaded.agent_settings.agent == original.agent_settings.agent
    assert loaded.agent_settings.llm.model == original.agent_settings.llm.model
    assert loaded.agent_settings.llm.base_url == original.agent_settings.llm.base_url
    assert loaded.agent_settings.llm.api_key.get_secret_value() == "test-key"
    assert loaded.conversation_settings.max_iterations == 100
    assert loaded.conversation_settings.security_analyzer == "llm"
    assert loaded.conversation_settings.confirmation_mode is True


def test_persisted_settings_file_contains_real_secrets(tmp_path):
    """The file is the durable store, so it must not hold masked values."""
    state = AppState(tmp_path)
    state.save_settings(
        Settings(agent_settings=OpenHandsAgentSettings(llm=LLM(model="m", api_key=SecretStr("real-key"))))
    )

    raw = (tmp_path / "settings.json").read_text()

    assert "real-key" in raw
    assert "**********" not in raw


def test_profiles_survive_the_store_round_trip(tmp_path):
    state = AppState(tmp_path)
    settings = Settings()
    settings.llm_profiles.save("work", LLM(model="openai/gpt-4o", api_key=SecretStr("sk-work")))
    settings.switch_to_profile("work")
    state.save_settings(settings)

    loaded = AppState(tmp_path).load_settings()

    assert loaded.llm_profiles.active == "work"
    assert loaded.llm_profiles.get("work").api_key.get_secret_value() == "sk-work"


# ── Secrets persistence ─────────────────────────────────────────────


def test_load_secrets_defaults_to_empty(tmp_path):
    secrets = AppState(tmp_path).load_secrets()

    assert secrets.custom_secrets == {}
    assert secrets.provider_tokens == {}


def test_store_and_load_secrets(tmp_path):
    state = AppState(tmp_path)
    state.save_secrets(
        Secrets(
            provider_tokens={"github": ProviderToken(token=SecretStr("github-token-123"), user_id="user1")},
            custom_secrets={
                "API_KEY": CustomSecret(secret=SecretStr("api-key-123"), description="API key")
            },
        )
    )

    loaded = AppState(tmp_path).load_secrets()

    assert loaded.provider_tokens["github"].token.get_secret_value() == "github-token-123"
    assert loaded.provider_tokens["github"].user_id == "user1"
    assert loaded.custom_secrets["API_KEY"].secret.get_secret_value() == "api-key-123"
    assert loaded.custom_secrets["API_KEY"].description == "API key"


def test_secrets_are_masked_unless_exposed():
    secrets = Secrets(
        provider_tokens={"github": ProviderToken(token=SecretStr("github-token-123"), user_id="user1")},
        custom_secrets={"API_KEY": CustomSecret(secret=SecretStr("api-key-123"), description="API key")},
    )

    exposed = secrets.model_dump(mode="json", context={"expose_secrets": True})
    assert exposed["provider_tokens"]["github"]["token"] == "github-token-123"
    assert exposed["custom_secrets"]["API_KEY"]["secret"] == "api-key-123"

    hidden = secrets.model_dump(mode="json")
    assert hidden["provider_tokens"]["github"]["token"] != "github-token-123"
    assert "**" in hidden["provider_tokens"]["github"]["token"]
    assert hidden["custom_secrets"]["API_KEY"]["secret"] != "api-key-123"
    assert "**" in hidden["custom_secrets"]["API_KEY"]["secret"]
    # Non-secret metadata stays readable.
    assert hidden["provider_tokens"]["github"]["user_id"] == "user1"
    assert hidden["custom_secrets"]["API_KEY"]["description"] == "API key"


def test_custom_secrets_become_conversation_env_vars():
    secrets = Secrets(
        custom_secrets={
            "API_KEY": CustomSecret(secret=SecretStr("api-key-123")),
            "OTHER": CustomSecret(secret=SecretStr("other-value")),
        }
    )

    assert secrets.get_env_vars() == {"API_KEY": "api-key-123", "OTHER": "other-value"}
