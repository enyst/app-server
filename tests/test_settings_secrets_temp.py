from __future__ import annotations


def test_temporary_settings_store_round_trips_mcp_config(client, authed_headers):
    response = client.get("/api/v1/settings", headers=authed_headers)
    assert response.status_code == 200
    assert response.json()["agent_settings"] == {}

    save = client.post(
        "/api/v1/settings",
        json={
            "agent_settings_diff": {
                "mcp_config": {"mcpServers": {"github": {"command": "github-mcp-server"}}}
            },
            "conversation_settings_diff": {"max_iterations": 12},
        },
        headers=authed_headers,
    )
    assert save.status_code == 200

    settings = client.get("/api/v1/settings", headers=authed_headers).json()
    assert settings["agent_settings"]["mcp_config"]["mcpServers"]["github"]["command"] == "github-mcp-server"
    assert settings["conversation_settings"]["max_iterations"] == 12


def test_temporary_secrets_store(client, authed_headers):
    created = client.post(
        "/api/v1/secrets",
        json={"name": "TOKEN", "value": "secret", "description": "test"},
        headers=authed_headers,
    )
    assert created.status_code == 200

    listed = client.get("/api/v1/secrets", headers=authed_headers).json()
    assert listed["secrets"] == [{"name": "TOKEN", "description": "test"}]

    deleted = client.delete("/api/v1/secrets/TOKEN", headers=authed_headers)
    assert deleted.status_code == 200
    assert client.get("/api/v1/secrets", headers=authed_headers).json() == {"secrets": []}
