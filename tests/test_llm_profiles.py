"""HTTP surface for LLM profiles.

The secret-handling and cap/self-healing cases are ported from
``tests/unit/app_server/test_profiles_api.py`` in OpenHands/OpenHands (at
ee9e78b7), adapted to this repo's session-key auth and file-backed store.
Pure model behaviour lives in test_llm_profiles_model.py.
"""

from __future__ import annotations

from conftest import TEST_MODEL
from openhands.sdk.llm import LLM
from pydantic import SecretStr

from app_server.llm_profiles import MAX_PROFILES
from app_server.state import AppState


def _save(client, authed_headers, name: str, **llm):
    return client.post(
        f"/api/v1/settings/profiles/{name}",
        json={"llm": {"model": "gpt-4o", "usage_id": "agent", **llm}},
        headers=authed_headers,
    )


def _state(client) -> AppState:
    return AppState(client.app.state.store.state_dir)


def _stored_profile(client, name: str):
    return _state(client).load_settings().llm_profiles.get(name)


def _seed_profile(client, name: str, llm: LLM) -> None:
    """Write a profile straight to disk, bypassing the API's validation."""
    state = _state(client)
    settings = state.load_settings()
    settings.llm_profiles.save(name, llm)
    state.save_settings(settings)


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


# ── Secret handling (ported) ────────────────────────────────────────


def test_save_profile_without_secrets_clears_api_key(client, authed_headers):
    response = client.post(
        "/api/v1/settings/profiles/no-key",
        json={"include_secrets": False, "llm": {"model": "gpt-4o", "api_key": "sk-abc"}},
        headers=authed_headers,
    )

    assert response.status_code == 201
    assert _stored_profile(client, "no-key").api_key is None


def test_edit_profile_round_trip_preserves_api_key(client, authed_headers):
    """The GET -> edit -> POST flow must not corrupt the stored key.

    GET returns ``api_key: null``; echoing that back must preserve the stored
    key rather than overwrite it with None.
    """
    _save(client, authed_headers, "p", api_key="REAL-KEY-42")

    fetched = client.get("/api/v1/settings/profiles/p", headers=authed_headers).json()
    assert fetched["config"]["api_key"] is None  # null, not a mask
    fetched["config"]["model"] = "anthropic/claude-opus-4"  # user edits the model

    response = client.post(
        "/api/v1/settings/profiles/p", json={"llm": fetched["config"]}, headers=authed_headers
    )
    assert response.status_code == 201

    preserved = _stored_profile(client, "p")
    assert preserved.model == "anthropic/claude-opus-4"
    assert preserved.api_key.get_secret_value() == "REAL-KEY-42"


def test_edit_profile_with_new_api_key_replaces_old(client, authed_headers):
    _save(client, authed_headers, "p", api_key="OLD-KEY")
    _save(client, authed_headers, "p", api_key="NEW-KEY")

    assert _stored_profile(client, "p").api_key.get_secret_value() == "NEW-KEY"


def test_save_profile_rejects_client_declared_is_subscription(client, authed_headers):
    """``is_subscription`` is a read-only SDK computed field.

    It is serialized into GET responses so round trips don't 422, but a client
    setting it True must not survive validation — it only becomes true via
    ``LLM.subscription_login()``. See ``StrictLLM._restore_is_subscription``.
    """
    response = client.post(
        "/api/v1/settings/profiles/p",
        json={"llm": {"model": "gpt-4o", "is_subscription": True}},
        headers=authed_headers,
    )

    assert response.status_code == 201
    assert _stored_profile(client, "p").is_subscription is False


def test_snapshot_save_with_preserve_flag_keeps_existing_profile_key(client, authed_headers):
    """The no-key edit-save: the snapshot's model lands, the profile's key stays.

    Without ``preserve_existing_api_key`` the snapshot would replace the
    profile's stored key with the active settings' key.
    """
    _seed_profile(client, "p", LLM(model="anthropic/claude-opus-4", api_key=SecretStr("sk-profile")))

    response = client.post(
        "/api/v1/settings/profiles/p",
        json={"include_secrets": True, "preserve_existing_api_key": True},
        headers=authed_headers,
    )
    assert response.status_code == 201

    saved = _stored_profile(client, "p")
    assert saved.model == TEST_MODEL  # snapshot of the active settings
    assert saved.api_key.get_secret_value() == "sk-profile"  # key preserved


def test_snapshot_save_with_preserve_flag_keeps_profile_keyless(client, authed_headers):
    """A keyless profile must not silently inherit the active settings' key."""
    _seed_profile(client, "p", LLM(model="anthropic/claude-opus-4"))

    response = client.post(
        "/api/v1/settings/profiles/p",
        json={"preserve_existing_api_key": True},
        headers=authed_headers,
    )
    assert response.status_code == 201

    assert _stored_profile(client, "p").api_key is None


def test_snapshot_save_preserve_flag_noop_for_new_profile(client, authed_headers):
    """With no existing profile there is nothing to preserve."""
    response = client.post(
        "/api/v1/settings/profiles/fresh",
        json={"preserve_existing_api_key": True},
        headers=authed_headers,
    )
    assert response.status_code == 201

    from conftest import TEST_LLM_API_KEY

    assert _stored_profile(client, "fresh").api_key.get_secret_value() == TEST_LLM_API_KEY


def test_preserve_flag_wins_over_explicit_llm_key(client, authed_headers):
    """The flag declares "no new key"; a key smuggled in alongside is ignored."""
    _save(client, authed_headers, "p", api_key="OLD-KEY")

    response = client.post(
        "/api/v1/settings/profiles/p",
        json={
            "preserve_existing_api_key": True,
            "llm": {"model": "gpt-4o", "api_key": "NEW-KEY"},
        },
        headers=authed_headers,
    )
    assert response.status_code == 201

    assert _stored_profile(client, "p").api_key.get_secret_value() == "OLD-KEY"


def test_preserve_flag_then_include_secrets_false_still_clears_key(client, authed_headers):
    """``include_secrets: false`` is the explicit "store no secret" switch."""
    _seed_profile(client, "p", LLM(model="gpt-4o", api_key=SecretStr("sk-profile")))

    response = client.post(
        "/api/v1/settings/profiles/p",
        json={"include_secrets": False, "preserve_existing_api_key": True},
        headers=authed_headers,
    )
    assert response.status_code == 201

    assert _stored_profile(client, "p").api_key is None


def test_api_key_set_is_false_for_empty_secret(client, authed_headers):
    """``SecretStr("")`` is not a stored key — the UI must not claim one."""
    _seed_profile(client, "blank", LLM(model="gpt-4o", api_key=SecretStr("")))
    _seed_profile(client, "whitespace", LLM(model="gpt-4o", api_key=SecretStr("   ")))

    rows = {
        p["name"]: p
        for p in client.get("/api/v1/settings/profiles", headers=authed_headers).json()["profiles"]
    }
    assert rows["blank"]["api_key_set"] is False
    assert rows["whitespace"]["api_key_set"] is False

    detail = client.get("/api/v1/settings/profiles/blank", headers=authed_headers).json()
    assert detail["api_key_set"] is False


def test_api_key_never_leaks_across_response_paths(client, authed_headers):
    """No endpoint that echoes a stored LLM may expose the key.

    One regression in any serializer would leak it into logs, the UI, or an
    exported settings payload — so check the whole surface at once.
    """
    secret = "sk-PROBE-MUST-NOT-LEAK"

    save = _save(client, authed_headers, "leak-check", api_key=secret)
    assert save.status_code == 201
    assert secret not in save.text
    assert secret not in client.get("/api/v1/settings/profiles/leak-check", headers=authed_headers).text
    assert (
        secret
        not in client.post("/api/v1/settings/profiles/leak-check/activate", headers=authed_headers).text
    )
    assert secret not in client.get("/api/v1/settings/profiles", headers=authed_headers).text
    assert secret not in client.get("/api/v1/settings", headers=authed_headers).text


# ── Active-pointer and cap behaviour (ported) ───────────────────────


def test_save_overwrite_of_active_profile_clears_active(client, authed_headers):
    """Overwriting the active profile with different config makes the pointer stale."""
    _save(client, authed_headers, "p", api_key="sk-a")
    client.post("/api/v1/settings/profiles/p/activate", headers=authed_headers)

    _save(client, authed_headers, "p", model="anthropic/claude-opus-4", api_key="sk-a")

    listed = client.get("/api/v1/settings/profiles", headers=authed_headers).json()
    assert listed["active_profile"] is None


def test_save_overwrite_of_inactive_profile_preserves_active(client, authed_headers):
    _save(client, authed_headers, "active-one", api_key="sk-a")
    _save(client, authed_headers, "other", api_key="sk-b")
    client.post("/api/v1/settings/profiles/active-one/activate", headers=authed_headers)

    _save(client, authed_headers, "other", model="anthropic/claude-opus-4", api_key="sk-b")

    listed = client.get("/api/v1/settings/profiles", headers=authed_headers).json()
    assert listed["active_profile"] == "active-one"


def test_list_profiles_clears_orphan_active(client, authed_headers):
    """Corrupt persisted state self-heals on the next load."""
    state = _state(client)
    settings = state.load_settings()
    settings.llm_profiles.save("real", LLM(model="gpt-4o"))
    object.__setattr__(settings.llm_profiles, "active", "ghost")  # bypass the validator
    state.save_settings(settings)

    response = client.get("/api/v1/settings/profiles", headers=authed_headers)

    assert response.status_code == 200
    assert response.json()["active_profile"] is None


def test_cap_frees_after_delete(client, authed_headers):
    """A user who hits the cap once must not be stuck forever."""
    for index in range(MAX_PROFILES):
        assert _save(client, authed_headers, f"p{index}", api_key="sk").status_code == 201

    assert _save(client, authed_headers, "over", api_key="sk").status_code == 409

    client.delete("/api/v1/settings/profiles/p0", headers=authed_headers)

    assert _save(client, authed_headers, "over", api_key="sk").status_code == 201
