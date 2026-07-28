from __future__ import annotations

from conftest import TEST_MODEL


def _save(client, authed_headers, name: str, **llm):
    return client.post(
        f"/api/v1/settings/profiles/{name}",
        json={"llm": {"model": "gpt-4o", "usage_id": "agent", **llm}},
        headers=authed_headers,
    )


def test_save_and_list_profiles(client, authed_headers):
    assert _save(client, authed_headers, "work", api_key="sk-work").status_code == 201

    listed = client.get("/api/v1/settings/profiles", headers=authed_headers).json()
    assert [p["name"] for p in listed["profiles"]] == ["work"]
    assert listed["profiles"][0]["model"] == "gpt-4o"
    assert listed["profiles"][0]["api_key_set"] is True


def test_get_profile_never_echoes_the_api_key(client, authed_headers):
    _save(client, authed_headers, "work", api_key="sk-work")

    detail = client.get("/api/v1/settings/profiles/work", headers=authed_headers).json()
    assert detail["config"]["api_key"] is None
    assert detail["api_key_set"] is True
    assert "sk-work" not in str(detail)


def test_activate_profile_switches_the_live_llm(client, authed_headers):
    _save(client, authed_headers, "work", api_key="sk-work")

    activated = client.post("/api/v1/settings/profiles/work/activate", headers=authed_headers)
    assert activated.status_code == 200, activated.text
    assert activated.json()["model"] == "gpt-4o"

    settings = client.get("/api/v1/settings", headers=authed_headers).json()
    assert settings["agent_settings"]["llm"]["model"] == "gpt-4o"
    assert settings["llm_profiles"]["active"] == "work"


def test_editing_the_llm_directly_clears_the_active_pointer(client, authed_headers):
    """The active profile is a pointer; it must not lie about what's running."""
    _save(client, authed_headers, "work", api_key="sk-work")
    client.post("/api/v1/settings/profiles/work/activate", headers=authed_headers)

    client.post(
        "/api/v1/settings",
        json={"agent_settings_diff": {"llm": {"model": "something-else"}}},
        headers=authed_headers,
    )

    settings = client.get("/api/v1/settings", headers=authed_headers).json()
    assert settings["llm_profiles"]["active"] is None


def test_rename_preserves_config_and_active_flag(client, authed_headers):
    _save(client, authed_headers, "work", api_key="sk-work")
    client.post("/api/v1/settings/profiles/work/activate", headers=authed_headers)

    renamed = client.post(
        "/api/v1/settings/profiles/work/rename",
        json={"new_name": "day-job"},
        headers=authed_headers,
    )
    assert renamed.status_code == 200, renamed.text

    listed = client.get("/api/v1/settings/profiles", headers=authed_headers).json()
    assert [p["name"] for p in listed["profiles"]] == ["day-job"]
    assert listed["active_profile"] == "day-job"


def test_rename_onto_an_existing_name_is_a_conflict(client, authed_headers):
    _save(client, authed_headers, "work", api_key="sk-work")
    _save(client, authed_headers, "home", api_key="sk-home")

    response = client.post(
        "/api/v1/settings/profiles/work/rename",
        json={"new_name": "home"},
        headers=authed_headers,
    )
    assert response.status_code == 409


def test_deleting_the_active_profile_promotes_a_fallback(client, authed_headers):
    _save(client, authed_headers, "work", api_key="sk-work")
    _save(client, authed_headers, "home", api_key="sk-home")
    client.post("/api/v1/settings/profiles/work/activate", headers=authed_headers)

    assert client.delete("/api/v1/settings/profiles/work", headers=authed_headers).status_code == 200

    listed = client.get("/api/v1/settings/profiles", headers=authed_headers).json()
    assert [p["name"] for p in listed["profiles"]] == ["home"]
    assert listed["active_profile"] == "home"


def test_delete_is_idempotent(client, authed_headers):
    response = client.delete("/api/v1/settings/profiles/never-existed", headers=authed_headers)
    assert response.status_code == 200


def test_profile_limit_is_enforced(client, authed_headers):
    for index in range(10):
        assert _save(client, authed_headers, f"p{index}", api_key="sk").status_code == 201

    overflow = _save(client, authed_headers, "one-too-many", api_key="sk")
    assert overflow.status_code == 409


def test_unknown_fields_in_a_profile_are_rejected(client, authed_headers):
    """StrictLLM should fail loud rather than silently dropping a typo."""
    response = client.post(
        "/api/v1/settings/profiles/work",
        json={"llm": {"model": "gpt-4o", "usage_id": "agent", "custom_header": "x"}},
        headers=authed_headers,
    )
    assert response.status_code == 422


def test_invalid_profile_names_are_rejected(client, authed_headers):
    response = client.post("/api/v1/settings/profiles/bad%2Fname", headers=authed_headers)
    assert response.status_code in (404, 422)


def test_saving_without_a_body_snapshots_the_current_llm(client, authed_headers):
    response = client.post("/api/v1/settings/profiles/current", headers=authed_headers)
    assert response.status_code == 201, response.text

    detail = client.get("/api/v1/settings/profiles/current", headers=authed_headers).json()
    assert detail["config"]["model"] == TEST_MODEL
    assert detail["api_key_set"] is True
