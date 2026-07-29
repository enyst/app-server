"""Model-level Settings tests.

Ported from ``tests/unit/storage/data_models/test_settings.py`` in
OpenHands/OpenHands (at ee9e78b7, before the Agent Canvas migration cleared the
repo). Cloud-only cases — marketplaces, the LiteLLM proxy display rules, and the
legacy ``secrets_store`` frozen-field regression — are dropped along with the
features they cover.

The MCP cases are the important ones: they exercise ``app_server/mcp_secrets.py``,
where a wrong answer silently destroys a user's stored credentials.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from openhands.sdk.llm import LLM
from openhands.sdk.mcp.config import dump_mcp_config
from openhands.sdk.settings import (
    AGENT_SETTINGS_SCHEMA_VERSION,
    ConversationSettings,
    OpenHandsAgentSettings,
)
from openhands.sdk.settings.model import CondenserSettings, VerificationSettings
from pydantic import SecretStr

import app_server.settings as settings_module
from app_server.llm_profiles import ProfileNotFoundError
from app_server.settings import GETSettingsModel, Settings

# ── Core model behaviour ────────────────────────────────────────────


def test_settings_handles_sensitive_data():
    settings = Settings(
        language="en",
        agent_settings=OpenHandsAgentSettings(
            agent="test-agent",
            llm=LLM(
                model="test-model",
                api_key=SecretStr("test-key"),
                base_url="https://test.example.com",
            ),
        ),
        conversation_settings=ConversationSettings(
            max_iterations=100,
            security_analyzer="llm",
            confirmation_mode=True,
        ),
    )

    llm_api_key = settings.agent_settings.llm.api_key
    assert str(llm_api_key) == "**********"
    assert llm_api_key.get_secret_value() == "test-key"


def test_settings_loads_persisted_settings_via_sdk_loaders():
    """Persisted payloads go through the SDK loaders, so migrations apply."""
    loaded_agent_settings = OpenHandsAgentSettings(agent="migrated-agent")
    loaded_conversation_settings = ConversationSettings(max_iterations=77)

    with (
        patch.object(
            settings_module,
            "validate_agent_settings",
            return_value=loaded_agent_settings,
        ) as agent_loader,
        patch.object(
            ConversationSettings,
            "from_persisted",
            return_value=loaded_conversation_settings,
        ) as conversation_loader,
    ):
        settings = Settings(
            agent_settings={"legacy": True},
            conversation_settings={"legacy": True},
        )

    agent_loader.assert_called_once_with({"legacy": True})
    conversation_loader.assert_called_once_with({"legacy": True})
    assert settings.agent_settings.agent == "migrated-agent"
    assert settings.conversation_settings.max_iterations == 77


def test_settings_update_deep_merges_agent_settings():
    """A partial agent_settings diff must not overwrite sibling sub-fields."""
    settings = Settings(
        agent_settings=OpenHandsAgentSettings(
            llm=LLM(model="existing-model", api_key=SecretStr("existing-key")),
            condenser=CondenserSettings(enabled=True, max_size=200),
        ),
    )

    settings.update({"agent_settings_diff": {"condenser": {"max_size": 300}}})

    assert settings.agent_settings.llm.model == "existing-model"
    assert settings.agent_settings.llm.api_key.get_secret_value() == "existing-key"
    assert settings.agent_settings.condenser.max_size == 300
    assert settings.agent_settings.condenser.enabled is True


def test_settings_preserve_agent_settings():
    settings = Settings(
        agent_settings=OpenHandsAgentSettings(
            llm=LLM(
                model="test-model",
                api_key=SecretStr("test-key"),
                litellm_extra_body={"metadata": {"tier": "pro"}},
            ),
            verification=VerificationSettings(critic_enabled=True, critic_mode="all_actions"),
        ),
    )

    assert settings.agent_settings.llm.api_key.get_secret_value() == "test-key"
    dump = settings.agent_settings.model_dump(mode="json", context={"expose_secrets": True})

    assert dump["schema_version"] == AGENT_SETTINGS_SCHEMA_VERSION
    assert dump["llm"]["model"] == "test-model"
    assert dump["llm"]["api_key"] == "test-key"
    assert dump["verification"]["critic_enabled"] is True
    assert dump["verification"]["critic_mode"] == "all_actions"
    assert dump["llm"]["litellm_extra_body"] == {"metadata": {"tier": "pro"}}


def test_settings_agent_settings_uses_agent_vals():
    settings = Settings(
        agent_settings=OpenHandsAgentSettings(
            llm=LLM(
                model="sdk-model",
                base_url="https://sdk.example.com",
                litellm_extra_body={"metadata": {"tier": "enterprise"}},
            ),
            condenser=CondenserSettings(enabled=False, max_size=88),
            verification=VerificationSettings(critic_enabled=True, critic_mode="all_actions"),
        ),
    )

    agent_settings = settings.agent_settings

    assert agent_settings.llm.model == "sdk-model"
    assert agent_settings.llm.base_url == "https://sdk.example.com"
    assert agent_settings.llm.litellm_extra_body == {"metadata": {"tier": "enterprise"}}
    assert agent_settings.condenser.enabled is False
    assert agent_settings.condenser.max_size == 88
    assert agent_settings.verification.critic_enabled is True
    assert agent_settings.verification.critic_mode == "all_actions"


def test_settings_agent_settings_keeps_sdk_mcp_shape_canonical():
    settings = Settings(
        agent_settings=OpenHandsAgentSettings(
            llm=LLM(model="sdk-model"),
            mcp_config={"sse_server": {"url": "https://example.com/sse", "transport": "sse"}},
        ),
    )

    mcp_config = settings.agent_settings.mcp_config
    assert mcp_config is not None
    assert "sse_server" in mcp_config
    assert mcp_config["sse_server"].transport == "sse"
    assert mcp_config["sse_server"].url == "https://example.com/sse"

    api_values = settings.agent_settings.model_dump(mode="json")
    assert "sse_server" in api_values["mcp_config"]


def test_settings_update_batch():
    settings = Settings()
    settings.update(
        {
            "language": "fr",
            "title_llm_profile": "Titles",
            "agent_settings_diff": {
                "agent": "TestAgent",
                "llm": {"model": "new-model", "api_key": "new-key"},
            },
            "conversation_settings_diff": {"max_iterations": 200},
        }
    )

    assert settings.language == "fr"
    assert settings.title_llm_profile == "Titles"
    assert settings.agent_settings.agent == "TestAgent"
    assert settings.agent_settings.llm.model == "new-model"
    assert settings.agent_settings.llm.api_key.get_secret_value() == "new-key"
    assert settings.conversation_settings.max_iterations == 200


def test_settings_update_batch_accepts_diff_keys():
    settings = Settings()
    settings.update(
        {
            "agent_settings_diff": {
                "agent": "DiffAgent",
                "llm": {"model": "diff-model", "api_key": "diff-key"},
            },
            "conversation_settings_diff": {"max_iterations": 123},
        }
    )

    assert settings.agent_settings.agent == "DiffAgent"
    assert settings.agent_settings.llm.model == "diff-model"
    assert settings.agent_settings.llm.api_key.get_secret_value() == "diff-key"
    assert settings.conversation_settings.max_iterations == 123


def test_settings_update_rejects_legacy_nested_keys():
    settings = Settings()

    with pytest.raises(ValueError, match=r"Use \*_diff nested settings payloads"):
        settings.update({"agent_settings": {"agent": "LegacyAgent"}})


def test_git_full_clone_defaults_to_false_and_updates():
    settings = Settings()

    assert settings.git_full_clone is False

    settings.update({"git_full_clone": True})

    assert settings.git_full_clone is True
    assert settings.model_dump(mode="json")["git_full_clone"] is True


# ── MCP config replacement ──────────────────────────────────────────


def test_settings_update_mcp_config():
    settings = Settings(agent_settings=OpenHandsAgentSettings(llm=LLM(model="sdk-model")))

    settings.update(
        {
            "agent_settings_diff": {
                "mcp_config": {
                    "mcpServers": {"custom": {"transport": "http", "url": "https://example.com/mcp"}}
                }
            }
        }
    )

    mcp = settings.agent_settings.mcp_config
    assert mcp is not None
    assert "custom" in mcp
    assert mcp["custom"].transport == "http"
    assert mcp["custom"].url == "https://example.com/mcp"


def test_settings_update_replaces_existing_mcp_servers():
    """mcp_config replaces wholesale — a deep merge could never remove a server."""
    settings = Settings(
        agent_settings=OpenHandsAgentSettings(
            llm=LLM(model="sdk-model"),
            mcp_config={"stale": {"transport": "sse", "url": "https://example.com/stale"}},
        )
    )

    settings.update(
        {
            "agent_settings_diff": {
                "mcp_config": {
                    "mcpServers": {
                        "fresh": {
                            "transport": "http",
                            "url": "https://example.com/fresh",
                            "tools": ["search"],
                        }
                    }
                }
            }
        }
    )

    mcp = settings.agent_settings.mcp_config
    assert mcp is not None
    assert set(mcp) == {"fresh"}
    assert mcp["fresh"].url == "https://example.com/fresh"


def test_settings_update_can_clear_mcp_config():
    settings = Settings(
        agent_settings=OpenHandsAgentSettings(
            llm=LLM(model="sdk-model"),
            mcp_config={"custom": {"transport": "http", "url": "https://example.com/mcp"}},
        )
    )

    settings.update({"agent_settings_diff": {"mcp_config": None}})

    # The SDK normalizes None to {}, so a cleared config round-trips as an
    # empty server map rather than None.
    assert settings.agent_settings.mcp_config == {}


# ── MCP secret preservation ─────────────────────────────────────────


def _settings_with_mcp_auth(url: str = "https://integration.example.com/mcp") -> Settings:
    return Settings(
        agent_settings=OpenHandsAgentSettings(
            mcp_config={"integration-hub": {"url": url, "headers": {"Authorization": "Bearer real-key"}}}
        )
    )


def test_settings_update_preserves_redacted_mcp_auth_for_same_endpoint():
    settings = _settings_with_mcp_auth()

    settings.update(
        {
            "agent_settings_diff": {
                "mcp_config": {
                    "integration-hub": {
                        "url": "https://integration.example.com/mcp",
                        "auth": {"strategy": "bearer", "value": "**********"},
                    }
                }
            }
        }
    )

    server = settings.agent_settings.mcp_config["integration-hub"]
    assert server.auth is not None
    assert server.auth.to_http_headers() == {"Authorization": "Bearer real-key"}


def test_settings_update_preserves_redacted_mcp_header_for_same_endpoint():
    settings = _settings_with_mcp_auth()

    settings.update(
        {
            "agent_settings_diff": {
                "mcp_config": {
                    "integration-hub": {
                        "url": "https://integration.example.com/mcp",
                        "headers": {"Authorization": "Bearer **********"},
                    }
                }
            }
        }
    )

    server = settings.agent_settings.mcp_config["integration-hub"]
    assert server.auth is not None
    assert server.auth.to_http_headers() == {"Authorization": "Bearer real-key"}


@pytest.mark.parametrize(
    "auth",
    (
        {"strategy": "api_key", "value": "real-key", "header_name": "X-API-Key"},
        {"strategy": "basic", "username": "user", "password": "real-key"},
        {"strategy": "header", "headers": {"X-API-Key": "real-key"}},
        {"strategy": "oauth2", "state": {"tokens": {"access_token": "real-key"}}},
    ),
)
def test_settings_update_preserves_typed_auth_from_redacted_bearer(auth: dict[str, object]):
    settings = Settings(
        agent_settings=OpenHandsAgentSettings(
            mcp_config={"server": {"url": "https://integration.example.com/mcp", "auth": auth}}
        )
    )

    settings.update(
        {
            "agent_settings_diff": {
                "mcp_config": {
                    "server": {
                        "url": "https://integration.example.com/mcp",
                        "auth": {"strategy": "bearer", "value": "**********"},
                    }
                }
            }
        }
    )

    dumped = dump_mcp_config(
        settings.agent_settings.mcp_config or {}, context={"expose_secrets": "plaintext"}
    )
    assert dumped["server"]["auth"] == auth


def test_settings_update_preserves_custom_headers_with_typed_auth():
    settings = Settings(
        agent_settings=OpenHandsAgentSettings(
            mcp_config={
                "server": {
                    "url": "https://integration.example.com/mcp",
                    "headers": {"X-Tenant": "tenant-secret"},
                    "auth": {"strategy": "bearer", "value": "real-key"},
                }
            }
        )
    )

    settings.update(
        {
            "agent_settings_diff": {
                "mcp_config": {
                    "server": {
                        "url": "https://integration.example.com/mcp",
                        "auth": {"strategy": "bearer", "value": "**********"},
                    }
                }
            }
        }
    )

    dumped = dump_mcp_config(
        settings.agent_settings.mcp_config or {}, context={"expose_secrets": "plaintext"}
    )
    assert dumped["server"]["headers"] == {"X-Tenant": "tenant-secret"}
    assert dumped["server"]["auth"] == {"strategy": "bearer", "value": "real-key"}


@pytest.mark.parametrize(
    "credential_update",
    (
        {},
        {"auth": {"strategy": "bearer", "value": "**********"}},
        {"headers": {"Authorization": "Bearer **********"}},
    ),
)
def test_settings_update_drops_mcp_auth_for_changed_endpoint(credential_update: dict[str, object]):
    """Secrets are bound to the endpoint they authenticate against."""
    settings = _settings_with_mcp_auth("https://old.example.com/mcp")

    settings.update(
        {
            "agent_settings_diff": {
                "mcp_config": {
                    "integration-hub": {"url": "https://new.example.com/mcp", **credential_update}
                }
            }
        }
    )

    server = settings.agent_settings.mcp_config["integration-hub"]
    assert not server.headers
    assert server.auth is None or server.auth.to_http_headers() == {}


def test_settings_update_preserves_omitted_mcp_auth_for_same_endpoint():
    settings = _settings_with_mcp_auth()

    settings.update(
        {
            "agent_settings_diff": {
                "mcp_config": {"integration-hub": {"url": "https://integration.example.com/mcp"}}
            }
        }
    )

    server = settings.agent_settings.mcp_config["integration-hub"]
    assert server.auth is not None
    assert server.auth.to_http_headers() == {"Authorization": "Bearer real-key"}


def test_settings_update_clears_explicit_mcp_auth():
    settings = _settings_with_mcp_auth()

    settings.update(
        {
            "agent_settings_diff": {
                "mcp_config": {
                    "integration-hub": {"url": "https://integration.example.com/mcp", "auth": None}
                }
            }
        }
    )

    server = settings.agent_settings.mcp_config["integration-hub"]
    assert server.auth is None


def test_settings_update_clears_auth_without_clearing_custom_headers():
    settings = Settings(
        agent_settings=OpenHandsAgentSettings(
            mcp_config={
                "server": {
                    "url": "https://integration.example.com/mcp",
                    "headers": {"X-Tenant": "tenant-secret"},
                    "auth": {"strategy": "bearer", "value": "real-key"},
                }
            }
        )
    )

    settings.update(
        {
            "agent_settings_diff": {
                "mcp_config": {"server": {"url": "https://integration.example.com/mcp", "auth": None}}
            }
        }
    )

    dumped = dump_mcp_config(
        settings.agent_settings.mcp_config or {}, context={"expose_secrets": "plaintext"}
    )
    assert dumped["server"]["headers"] == {"X-Tenant": "tenant-secret"}
    assert "auth" not in dumped["server"]


def test_settings_update_preserves_redacted_mcp_env_for_same_command():
    settings = Settings(
        agent_settings=OpenHandsAgentSettings(
            mcp_config={
                "local": {"command": "mcp-server", "args": ["--stdio"], "env": {"API_KEY": "real-key"}}
            }
        )
    )

    settings.update(
        {
            "agent_settings_diff": {
                "mcp_config": {
                    "local": {
                        "command": "mcp-server",
                        "args": ["--stdio"],
                        "env": {"API_KEY": "**********"},
                    }
                }
            }
        }
    )

    server = settings.agent_settings.mcp_config["local"]
    assert server.env is not None
    assert server.env["API_KEY"].get_secret_value() == "real-key"

    # An explicitly empty env still clears.
    settings.update(
        {
            "agent_settings_diff": {
                "mcp_config": {"local": {"command": "mcp-server", "args": ["--stdio"], "env": {}}}
            }
        }
    )

    assert not settings.agent_settings.mcp_config["local"].env


def test_settings_update_preserves_redacted_mcp_env_across_rename():
    """A renamed server keeps its secrets when the endpoint is unchanged."""
    settings = Settings(
        agent_settings=OpenHandsAgentSettings(
            mcp_config={
                "old-name": {"command": "mcp-server", "args": ["--stdio"], "env": {"API_KEY": "real-key"}}
            }
        )
    )

    settings.update(
        {
            "agent_settings_diff": {
                "mcp_config": {
                    "new-name": {
                        "command": "mcp-server",
                        "args": ["--stdio"],
                        "env": {"API_KEY": "**********"},
                    }
                }
            }
        }
    )

    server = settings.agent_settings.mcp_config["new-name"]
    assert server.env is not None
    assert server.env["API_KEY"].get_secret_value() == "real-key"


def test_settings_update_does_not_copy_mcp_env_to_duplicate_server():
    """An ambiguous match must not leak one server's secret into another."""
    settings = Settings(
        agent_settings=OpenHandsAgentSettings(
            mcp_config={
                "original": {"command": "mcp-server", "args": ["--stdio"], "env": {"API_KEY": "real-key"}}
            }
        )
    )

    settings.update(
        {
            "agent_settings_diff": {
                "mcp_config": {
                    "original": {"command": "mcp-server", "args": ["--stdio"]},
                    "duplicate": {
                        "command": "mcp-server",
                        "args": ["--stdio"],
                        "env": {"API_KEY": "**********"},
                    },
                }
            }
        }
    )

    original = settings.agent_settings.mcp_config["original"]
    duplicate = settings.agent_settings.mcp_config["duplicate"]
    assert original.env is not None
    assert original.env["API_KEY"].get_secret_value() == "real-key"
    assert not duplicate.env


def test_settings_update_drops_redacted_mcp_env_when_args_change():
    """Env secrets are bound to the full process invocation, not just the command."""
    settings = Settings(
        agent_settings=OpenHandsAgentSettings(
            mcp_config={
                "local": {"command": "npx", "args": ["first-package"], "env": {"API_KEY": "real-key"}}
            }
        )
    )

    settings.update(
        {
            "agent_settings_diff": {
                "mcp_config": {
                    "local": {
                        "command": "npx",
                        "args": ["different-package"],
                        "env": {"API_KEY": "**********"},
                    }
                }
            }
        }
    )

    assert not settings.agent_settings.mcp_config["local"].env


# ── MCP secrets across the real GET round trip ──────────────────────


def _mcp_config_as_seen_by_frontend(settings: Settings) -> dict:
    """The ``mcp_config`` a client receives from ``GET /api/v1/settings``.

    Mirrors the router: the response is built as
    ``GETSettingsModel(**settings.model_dump(...))`` and then serialized. That
    dump-then-revalidate strips every MCP secret to *absent* — redaction emits
    ``"**********"``, which validates back to ``None`` on the way in and is
    dropped on the way out. So an unchanged credential reaches the client as
    ``{"strategy": "bearer"}`` with no value, and that is what gets echoed back
    on the next save.
    """
    response = GETSettingsModel(
        **settings.model_dump(),
        llm_api_key_set=settings.llm_api_key_is_set,
    )
    return response.model_dump(mode="json")["agent_settings"]["mcp_config"]


def test_get_settings_roundtrip_strips_mcp_auth_secret_to_absent():
    """Document the root cause: the GET response omits the secret entirely."""
    settings = Settings(
        agent_settings=OpenHandsAgentSettings(
            mcp_config={
                "server": {
                    "url": "https://mcp.example.com",
                    "transport": "http",
                    "auth": {"strategy": "bearer", "value": "real-key"},
                }
            }
        )
    )

    seen = _mcp_config_as_seen_by_frontend(settings)

    # The secret is gone, not redacted to "**********".
    assert seen["server"]["auth"] == {"strategy": "bearer"}


def test_settings_update_preserves_mcp_auth_across_get_roundtrip_on_add():
    """Adding a server must not wipe existing servers' keys.

    Replays the real flow: read the values as the client receives them (secret
    stripped), append a brand-new server with its own key, and save the whole
    map back — exactly the payload a browser sends.
    """
    settings = Settings(
        agent_settings=OpenHandsAgentSettings(
            mcp_config={
                "shttp": {
                    "url": "https://a.example.com",
                    "transport": "http",
                    "auth": {"strategy": "bearer", "value": "key-a"},
                },
                "shttp_1": {
                    "url": "https://b.example.com",
                    "transport": "http",
                    "auth": {"strategy": "bearer", "value": "key-b"},
                },
            }
        )
    )

    submitted = _mcp_config_as_seen_by_frontend(settings)
    submitted["shttp_2"] = {
        "url": "https://c.example.com",
        "auth": {"strategy": "bearer", "value": "key-c"},
    }

    settings.update({"agent_settings_diff": {"mcp_config": submitted}})

    dumped = dump_mcp_config(
        settings.agent_settings.mcp_config or {}, context={"expose_secrets": "plaintext"}
    )
    assert dumped["shttp"]["auth"] == {"strategy": "bearer", "value": "key-a"}
    assert dumped["shttp_1"]["auth"] == {"strategy": "bearer", "value": "key-b"}
    assert dumped["shttp_2"]["auth"] == {"strategy": "bearer", "value": "key-c"}


@pytest.mark.parametrize(
    "auth,secret_path",
    (
        ({"strategy": "api_key", "value": "real-key", "header_name": "X-API-Key"}, ("value",)),
        ({"strategy": "basic", "username": "user", "password": "real-key"}, ("password",)),
        ({"strategy": "header", "headers": {"X-API-Key": "real-key"}}, ("headers", "X-API-Key")),
        (
            {"strategy": "oauth2", "state": {"tokens": {"access_token": "real-key"}}},
            ("state", "tokens", "access_token"),
        ),
    ),
)
def test_settings_update_preserves_typed_mcp_auth_across_get_roundtrip(
    auth: dict, secret_path: tuple[str, ...]
):
    """Every typed strategy survives the stripped-secret round trip on an edit."""
    settings = Settings(
        agent_settings=OpenHandsAgentSettings(
            mcp_config={
                "server": {"url": "https://mcp.example.com", "transport": "http", "auth": auth}
            }
        )
    )

    submitted = _mcp_config_as_seen_by_frontend(settings)
    # An unrelated edit (a new server) triggers a full-map save.
    submitted["added"] = {
        "url": "https://added.example.com",
        "auth": {"strategy": "bearer", "value": "new-key"},
    }

    settings.update({"agent_settings_diff": {"mcp_config": submitted}})

    dumped = dump_mcp_config(
        settings.agent_settings.mcp_config or {}, context={"expose_secrets": "plaintext"}
    )
    restored = dumped["server"]["auth"]
    for key in secret_path:
        restored = restored[key]
    assert restored == "real-key"


def test_settings_update_preserves_stdio_env_across_get_roundtrip():
    """A stdio server's env secrets survive when another server is added."""
    settings = Settings(
        agent_settings=OpenHandsAgentSettings(
            mcp_config={
                "local": {"command": "mcp-server", "args": ["--stdio"], "env": {"API_KEY": "real-key"}}
            }
        )
    )

    submitted = _mcp_config_as_seen_by_frontend(settings)
    submitted["added"] = {
        "url": "https://added.example.com",
        "auth": {"strategy": "bearer", "value": "new-key"},
    }

    settings.update({"agent_settings_diff": {"mcp_config": submitted}})

    server = settings.agent_settings.mcp_config["local"]
    assert server.env is not None
    assert server.env["API_KEY"].get_secret_value() == "real-key"


# ── LLM profiles: Settings integration ──────────────────────────────
# Pure LLMProfiles behaviour lives in test_llm_profiles_model.py.


def test_switch_to_profile_updates_agent_settings_llm():
    settings = Settings()
    settings.llm_profiles.save("my-profile", LLM(model="openai/gpt-4o"))

    settings.switch_to_profile("my-profile")

    assert settings.agent_settings.llm.model == "openai/gpt-4o"
    assert settings.llm_profiles.active == "my-profile"


def test_switch_to_nonexistent_profile_raises():
    settings = Settings()

    with pytest.raises(ProfileNotFoundError) as exc_info:
        settings.switch_to_profile("nonexistent")

    assert exc_info.value.name == "nonexistent"
    assert settings.llm_profiles.active is None


def test_llm_profiles_masking_and_roundtrip():
    """Masked by default, exposed with context, reconstructible via model_validate."""
    settings = Settings()
    settings.llm_profiles.save("p", LLM(model="openai/gpt-4o", api_key=SecretStr("secret")))

    masked = settings.model_dump(mode="json")
    exposed = settings.model_dump(mode="json", context={"expose_secrets": True})
    assert masked["llm_profiles"]["profiles"]["p"]["api_key"] != "secret"
    assert exposed["llm_profiles"]["profiles"]["p"]["api_key"] == "secret"

    rehydrated = Settings.model_validate(exposed)
    assert rehydrated.llm_profiles.get("p").api_key.get_secret_value() == "secret"


def test_switch_to_profile_preserves_other_agent_settings():
    """Switching the LLM must not wipe condenser/verification/mcp_config."""
    settings = Settings(
        agent_settings=OpenHandsAgentSettings(
            llm=LLM(model="openai/gpt-4o"),
            condenser=CondenserSettings(enabled=True, max_size=321),
            verification=VerificationSettings(critic_enabled=True, critic_mode="all_actions"),
            mcp_config={"s": {"transport": "http", "url": "https://example.com/mcp"}},
        ),
    )
    settings.llm_profiles.save("p", LLM(model="anthropic/claude-opus-4"))

    settings.switch_to_profile("p")

    assert settings.agent_settings.llm.model == "anthropic/claude-opus-4"
    assert settings.agent_settings.condenser.max_size == 321
    assert settings.agent_settings.verification.critic_mode == "all_actions"
    assert settings.agent_settings.mcp_config is not None
    assert "s" in settings.agent_settings.mcp_config


def test_delete_active_profile_promotes_remaining_one():
    settings = Settings()
    settings.llm_profiles.save("a", LLM(model="openai/gpt-4o"))
    settings.llm_profiles.save("b", LLM(model="anthropic/claude-opus-4"))
    settings.switch_to_profile("a")

    assert settings.delete_profile("a") is True

    assert "a" not in settings.llm_profiles.profiles
    assert settings.llm_profiles.active == "b"
    assert settings.agent_settings.llm.model == "anthropic/claude-opus-4"


def test_delete_inactive_profile_does_not_touch_active():
    settings = Settings()
    settings.llm_profiles.save("a", LLM(model="openai/gpt-4o"))
    settings.llm_profiles.save("b", LLM(model="anthropic/claude-opus-4"))
    settings.switch_to_profile("a")

    assert settings.delete_profile("b") is True

    assert settings.llm_profiles.active == "a"
    assert settings.agent_settings.llm.model == "openai/gpt-4o"


def test_delete_only_profile_clears_active():
    settings = Settings(title_llm_profile="only")
    settings.llm_profiles.save("only", LLM(model="openai/gpt-4o"))
    settings.switch_to_profile("only")

    assert settings.delete_profile("only") is True

    assert settings.llm_profiles.profiles == {}
    assert settings.llm_profiles.active is None
    assert settings.title_llm_profile is None


def test_delete_missing_profile_returns_false():
    settings = Settings()
    assert settings.delete_profile("nope") is False


def test_update_ignores_llm_profiles_payload():
    """Profile changes must go through the dedicated endpoints, which enforce
    name rules, the count cap, and the write lock."""
    settings = Settings()

    settings.update({"llm_profiles": {"profiles": {"X": {"model": "openai/gpt-4o"}}, "active": "X"}})

    assert settings.llm_profiles.profiles == {}
    assert settings.llm_profiles.active is None


def test_update_clears_active_when_llm_diverges():
    settings = Settings(
        agent_settings=OpenHandsAgentSettings(llm=LLM(model="openai/gpt-4o", api_key=SecretStr("sk-a")))
    )
    settings.llm_profiles.save("p", LLM(model="openai/gpt-4o", api_key=SecretStr("sk-a")))
    settings.switch_to_profile("p")
    assert settings.llm_profiles.active == "p"

    settings.update({"agent_settings_diff": {"llm": {"model": "anthropic/claude-opus-4"}}})

    assert settings.llm_profiles.active is None


def test_update_keeps_active_when_llm_unchanged():
    """A no-op update must not spuriously clear ``active``."""
    settings = Settings(
        agent_settings=OpenHandsAgentSettings(llm=LLM(model="openai/gpt-4o", api_key=SecretStr("sk-a")))
    )
    settings.llm_profiles.save("p", LLM(model="openai/gpt-4o", api_key=SecretStr("sk-a")))
    settings.switch_to_profile("p")

    settings.update({"language": "fr"})

    assert settings.llm_profiles.active == "p"
