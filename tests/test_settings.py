from __future__ import annotations

from conftest import TEST_LLM_API_KEY, TEST_MODEL


def test_get_settings_is_404_before_anything_is_saved(unconfigured_client, authed_headers):
    response = unconfigured_client.get("/api/v1/settings", headers=authed_headers)
    assert response.status_code == 404


def test_get_settings_redacts_secrets_but_reports_them_as_set(client, authed_headers):
    response = client.get("/api/v1/settings", headers=authed_headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["agent_settings"]["llm"]["model"] == TEST_MODEL
    assert body["agent_settings"]["llm"]["api_key"] is None
    assert body["llm_api_key_set"] is True
    assert body["search_api_key_set"] is False


def test_agent_settings_diff_merges_without_clobbering_siblings(client, authed_headers):
    saved = client.post(
        "/api/v1/settings",
        json={"agent_settings_diff": {"llm": {"temperature": 0.5}}},
        headers=authed_headers,
    )
    assert saved.status_code == 200, saved.text

    llm = saved.json()["agent_settings"]["llm"]
    assert llm["temperature"] == 0.5
    # The model came from the seeded settings and must survive a diff that
    # only mentions temperature.
    assert llm["model"] == TEST_MODEL
    assert saved.json()["llm_api_key_set"] is True


def test_conversation_settings_diff_persists(client, authed_headers):
    response = client.post(
        "/api/v1/settings",
        json={"conversation_settings_diff": {"max_iterations": 42, "confirmation_mode": True}},
        headers=authed_headers,
    )
    assert response.status_code == 200, response.text

    reloaded = client.get("/api/v1/settings", headers=authed_headers).json()
    assert reloaded["conversation_settings"]["max_iterations"] == 42
    assert reloaded["conversation_settings"]["confirmation_mode"] is True


def test_legacy_nested_settings_keys_are_rejected(client, authed_headers):
    response = client.post(
        "/api/v1/settings",
        json={"agent_settings": {"llm": {"model": "x"}}},
        headers=authed_headers,
    )
    assert response.status_code == 400
    assert "diff" in response.json()["detail"]


def test_product_settings_round_trip(client, authed_headers):
    response = client.post(
        "/api/v1/settings",
        json={"language": "en", "git_user_name": "Engel", "max_budget_per_task": 1.5},
        headers=authed_headers,
    )
    assert response.status_code == 200, response.text

    reloaded = client.get("/api/v1/settings", headers=authed_headers).json()
    assert reloaded["language"] == "en"
    assert reloaded["git_user_name"] == "Engel"
    assert reloaded["max_budget_per_task"] == 1.5


def test_saving_unrelated_settings_preserves_the_llm_api_key(client, authed_headers):
    client.post("/api/v1/settings", json={"language": "fr"}, headers=authed_headers)

    reloaded = client.get("/api/v1/settings", headers=authed_headers).json()
    assert reloaded["llm_api_key_set"] is True


def test_echoing_the_whole_get_response_back_preserves_secrets(client, authed_headers):
    """The naive client round trip: GET, then POST the response back verbatim.

    The GET nulls ``llm.api_key`` and ``search_api_key``, so without treating
    null as "unchanged" this would silently wipe both credentials.
    """
    client.post("/api/v1/settings", json={"search_api_key": "real-search-key"}, headers=authed_headers)

    got = client.get("/api/v1/settings", headers=authed_headers).json()
    assert got["agent_settings"]["llm"]["api_key"] is None
    assert got["search_api_key"] is None

    echoed = {
        key: value
        for key, value in got.items()
        if key
        not in (
            "agent_settings",
            "conversation_settings",
            "llm_profiles",
            "llm_api_key_set",
            "search_api_key_set",
            "provider_tokens_set",
        )
    }
    echoed["agent_settings_diff"] = got["agent_settings"]
    assert client.post("/api/v1/settings", json=echoed, headers=authed_headers).status_code == 200

    stored = _stored_settings(client)
    assert stored.agent_settings.llm.api_key.get_secret_value() == TEST_LLM_API_KEY
    assert stored.search_api_key.get_secret_value() == "real-search-key"


def test_secrets_can_still_be_rotated_and_cleared(client, authed_headers):
    """Null means "unchanged", but a real value rotates and "" clears."""
    client.post(
        "/api/v1/settings",
        json={"agent_settings_diff": {"llm": {"api_key": "rotated"}}, "search_api_key": "new-search"},
        headers=authed_headers,
    )
    stored = _stored_settings(client)
    assert stored.agent_settings.llm.api_key.get_secret_value() == "rotated"
    assert stored.search_api_key.get_secret_value() == "new-search"

    client.post(
        "/api/v1/settings",
        json={"agent_settings_diff": {"llm": {"api_key": ""}}, "search_api_key": ""},
        headers=authed_headers,
    )
    stored = _stored_settings(client)
    assert stored.agent_settings.llm.api_key is None
    assert stored.search_api_key is None


def test_mcp_secrets_survive_a_redacted_round_trip(client, authed_headers):
    """The core reason mcp_secrets.py exists.

    Save an MCP server with a real credential, read settings back (the GET
    strips the credential entirely — the redaction marker validates to None and
    is dropped), then POST that stripped payload back as a client would. The
    stored credential must survive rather than be erased.
    """
    saved = client.post(
        "/api/v1/settings",
        json={
            "agent_settings_diff": {
                "mcp_config": {
                    "mcpServers": {
                        "fetch": {
                            "url": "https://mcp.example.dev",
                            "headers": {"Authorization": "Bearer real-token"},
                        }
                    }
                }
            }
        },
        headers=authed_headers,
    )
    assert saved.status_code == 200, saved.text

    # What a client sees: the strategy, but no credential value at all.
    echoed = client.get("/api/v1/settings", headers=authed_headers).json()
    mcp = echoed["agent_settings"]["mcp_config"]
    assert mcp["fetch"]["auth"] == {"strategy": "bearer"}
    assert "real-token" not in str(echoed)

    # Echo it straight back, unchanged.
    round_tripped = client.post(
        "/api/v1/settings",
        json={"agent_settings_diff": {"mcp_config": {"mcpServers": mcp}}},
        headers=authed_headers,
    )
    assert round_tripped.status_code == 200, round_tripped.text

    stored = _stored_settings(client)
    assert stored.agent_settings.mcp_config["fetch"].auth.value.get_secret_value() == "real-token"


def test_changing_an_mcp_secret_is_honored(client, authed_headers):
    client.post(
        "/api/v1/settings",
        json={
            "agent_settings_diff": {
                "mcp_config": {
                    "mcpServers": {
                        "fetch": {
                            "url": "https://mcp.example.dev",
                            "headers": {"Authorization": "Bearer real-token"},
                        }
                    }
                }
            }
        },
        headers=authed_headers,
    )
    client.post(
        "/api/v1/settings",
        json={
            "agent_settings_diff": {
                "mcp_config": {
                    "mcpServers": {
                        "fetch": {
                            "url": "https://mcp.example.dev",
                            "headers": {"Authorization": "Bearer rotated-token"},
                        }
                    }
                }
            }
        },
        headers=authed_headers,
    )

    stored = _stored_settings(client)
    assert stored.agent_settings.mcp_config["fetch"].auth.value.get_secret_value() == "rotated-token"


def test_schemas_are_served_from_the_sdk(client, authed_headers):
    """These used to be hardcoded empty stubs; they must now be real."""
    agent_schema = client.get("/api/v1/settings/agent-schema", headers=authed_headers).json()
    conversation_schema = client.get("/api/v1/settings/conversation-schema", headers=authed_headers).json()

    assert agent_schema["sections"], "agent schema should not be empty"
    assert conversation_schema["sections"], "conversation schema should not be empty"


def _stored_settings(client):
    """Read settings straight off disk, bypassing the redacting GET."""
    from app_server.state import AppState

    return AppState(client.app.state.store.state_dir).load_settings()


def test_settings_are_persisted_with_real_secrets(client, authed_headers):
    stored = _stored_settings(client)
    assert stored is not None
    assert stored.agent_settings.llm.api_key.get_secret_value() == TEST_LLM_API_KEY
