"""``Settings.update`` agent-kind switch behaviour.

The discriminated ``OpenHandsAgentSettings | ACPAgentSettings`` union means a
naive deep-merge of the incoming kind's fields onto the outgoing kind's dump
produces a mongrel (e.g. ``llm`` plus ``acp_command``) that fails validation and
500s the settings endpoint. The SDK's ``apply_agent_settings_diff`` avoids that
by starting from a fresh base for the new kind.

Ported from ``tests/unit/app_server/test_settings_agent_kind_switch.py`` in
OpenHands/OpenHands (at ee9e78b7). The two loader cases are routed through
``Settings(...)`` rather than the upstream ``_load_persisted_agent_settings``
helper, since that normalization happens inline in our ``_normalize_inputs``.
"""

from __future__ import annotations

from openhands.sdk.settings.model import AGENT_SETTINGS_SCHEMA_VERSION

from app_server.settings import Settings


def _set_acp(command: list[str] | None = None) -> dict:
    return {
        "agent_settings_diff": {
            "agent_kind": "acp",
            "acp_command": command or ["npx", "-y", "@agentclientprotocol/claude-agent-acp"],
            "acp_args": [],
        }
    }


def _set_openhands(*, llm_model: str | None = None, mcp_config: dict | None = None) -> dict:
    diff: dict = {"agent_kind": "openhands"}
    if llm_model is not None:
        diff["llm"] = {"model": llm_model}
    if mcp_config is not None:
        diff["mcp_config"] = mcp_config
    return {"agent_settings_diff": diff}


def test_kind_switch_does_not_raise():
    """OpenHands -> ACP -> OpenHands must not blow up.

    Regression guard for the discriminated-union mongrel: deep-merging the
    OpenHands dump onto an ``acp_command`` payload would produce a dict carrying
    both ``llm`` and ``acp_command``, which neither branch accepts.
    """
    settings = Settings()
    settings.update(_set_openhands(llm_model="anthropic/claude-sonnet-4-5"))

    settings.update(_set_acp())
    assert settings.agent_settings.agent_kind == "acp"

    settings.update(_set_openhands())
    assert settings.agent_settings.agent_kind == "openhands"


def test_kind_switch_resets_new_kind_to_defaults():
    """Switching to a new kind starts from a fresh base."""
    settings = Settings()
    settings.update(_set_openhands(llm_model="anthropic/claude-sonnet-4-5"))

    settings.update(_set_acp())

    assert settings.agent_settings.agent_kind == "acp"
    assert settings.agent_settings.llm.model != "anthropic/claude-sonnet-4-5"


def test_kind_switch_with_inline_field_override():
    """Fields sent alongside an ``agent_kind`` switch land on the fresh base."""
    settings = Settings()
    settings.update(_set_acp())

    settings.update(_set_openhands(llm_model="model-c"))

    assert settings.agent_settings.agent_kind == "openhands"
    assert settings.agent_settings.llm.model == "model-c"


def test_replace_mcp_config_in_kind_switch():
    """mcp_config replace-wholesale also works alongside a kind switch."""
    settings = Settings()
    settings.update(_set_acp())

    settings.update(_set_openhands(mcp_config={"mcpServers": {"foo": {"command": "foo-bin"}}}))

    assert settings.agent_settings.mcp_config is not None
    assert "foo" in settings.agent_settings.mcp_config


def test_loader_normalizes_legacy_llm_tag_at_current_schema_version():
    """A persisted ``agent_kind: 'llm'`` row must read back as ``openhands``.

    The SDK's ``llm -> openhands`` rename only fires while advancing the schema
    version, so a payload already at the current version is not migrated and
    would otherwise validate as the deprecated ``LLMAgentSettings``.
    """
    settings = Settings(
        agent_settings={
            "agent_kind": "llm",
            "schema_version": AGENT_SETTINGS_SCHEMA_VERSION,
            "llm": {"model": "anthropic/claude-sonnet-4-5"},
        }
    )

    assert settings.agent_settings.agent_kind == "openhands"
    assert settings.agent_settings.llm.model == "anthropic/claude-sonnet-4-5"


def test_loader_preserves_acp_variant_without_coercion():
    """``agent_kind: 'acp'`` must be left alone.

    The ``llm`` normalization must not regress into cross-variant coercion,
    which 500'd ACP settings (``ACPAgentSettings.agent_context`` is nullable;
    the OpenHands shape rejects ``None``).
    """
    settings = Settings(
        agent_settings={
            "agent_kind": "acp",
            "acp_server": "claude-code",
            "llm": {"model": "litellm_proxy/anthropic/claude-sonnet-4"},
        }
    )

    assert settings.agent_settings.agent_kind == "acp"
    assert settings.agent_settings.agent_context is None
