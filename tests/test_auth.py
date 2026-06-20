from __future__ import annotations


def test_session_key_required_for_api_routes(client):
    response = client.get("/api/v1/app-conversations/search")
    assert response.status_code == 401


def test_session_key_allows_api_routes(client, authed_headers):
    response = client.get("/api/v1/app-conversations/search", headers=authed_headers)
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_page_id": None}


def test_health_routes_are_public(client):
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200
    assert client.get("/server_info").json()["app"] == "minimal-openhands-app-server"
