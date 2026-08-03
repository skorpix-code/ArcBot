"""The HTTP and web-socket surface.

ArcBot can run commands, so its local server is treated as privileged: these
tests assert the token gate and input validation, not just the happy path.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from arcbot.web.app import RUNTIME, SESSION_TOKEN, app


@pytest.fixture
def client(workspace):
    RUNTIME.settings.workspace = str(workspace)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth():
    return {"x-arcbot-token": SESSION_TOKEN}


class TestAuthentication:
    def test_api_requires_the_run_token(self, client):
        assert client.get("/api/state").status_code == 401
        assert client.get("/api/sessions").status_code == 401
        assert client.post("/api/settings", json={}).status_code == 401

    def test_a_wrong_token_is_rejected(self, client):
        assert client.get("/api/state", headers={"x-arcbot-token": "nope"}).status_code == 401

    def test_the_token_also_works_as_a_query_parameter(self, client):
        assert client.get(f"/api/state?token={SESSION_TOKEN}").status_code == 200

    def test_health_needs_no_token_and_leaks_nothing(self, client):
        body = client.get("/health").json()
        assert body["ok"] is True
        assert set(body) == {"ok", "uptime"}

    def test_the_websocket_rejects_a_bad_token(self, client):
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws?token=nope"):
            pass


class TestPage:
    def test_the_token_is_injected_into_the_page(self, client):
        html = client.get("/").text
        assert "__ARCBOT_TOKEN__" not in html
        assert SESSION_TOKEN in html

    def test_security_headers_are_set(self, client):
        headers = client.get("/").headers
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert headers["cache-control"] == "no-store"


class TestState:
    def test_state_describes_everything_the_ui_needs(self, client, auth):
        state = client.get("/api/state", headers=auth).json()
        assert state["providers"] and state["toolsets"]
        assert len(state["permissionModes"]) == 4
        assert "settings" in state and "version" in state

    def test_no_secret_ever_appears_in_state(self, client, auth):
        client.post("/api/secret", headers=auth,
                    json={"key": "OPENAI_API_KEY", "value": "sk-supersecret-value"})
        body = json.dumps(client.get("/api/state", headers=auth).json())
        assert "sk-supersecret-value" not in body

    def test_provider_auth_reports_without_revealing_keys(self, client, auth):
        status = client.get("/api/providers/anthropic/auth", headers=auth).json()
        assert set(status) == {"available", "source", "detail", "hint"}

    def test_an_unknown_provider_is_a_404(self, client, auth):
        assert client.get("/api/providers/made-up/auth", headers=auth).status_code == 404


class TestSettingsValidation:
    def test_settings_round_trip(self, client, auth, workspace):
        response = client.post("/api/settings", headers=auth, json={
            "workspace": str(workspace),
            "toolsets": ["files"],
            "permissions": {"mode": "trusted"},
            "onboarded": True,
        })
        assert response.status_code == 200
        settings = response.json()["settings"]
        assert settings["permissions"]["mode"] == "trusted"
        assert settings["onboarded"] is True

    def test_always_on_toolsets_cannot_be_removed(self, client, auth):
        response = client.post("/api/settings", headers=auth, json={"toolsets": ["files"]})
        assert "core" in response.json()["settings"]["toolsets"]

    def test_unknown_toolsets_are_dropped(self, client, auth):
        response = client.post("/api/settings", headers=auth,
                               json={"toolsets": ["files", "definitely-not-real"]})
        assert "definitely-not-real" not in response.json()["settings"]["toolsets"]

    @pytest.mark.parametrize("payload", [
        {"permissions": {"mode": "godmode"}},
        {"model": {"provider": "not-a-provider"}},
    ])
    def test_invalid_values_are_rejected(self, client, auth, payload):
        assert client.post("/api/settings", headers=auth, json=payload).status_code == 400

    @pytest.mark.parametrize("key", ["../../etc/passwd", "has space", "", "a-b"])
    def test_secret_names_must_be_identifiers(self, client, auth, key):
        response = client.post("/api/secret", headers=auth, json={"key": key, "value": "x"})
        assert response.status_code == 400


class TestWebSocket:
    def test_handshake_delivers_state(self, client):
        with client.websocket_connect(f"/ws?token={SESSION_TOKEN}") as ws:
            kinds = {ws.receive_json()["type"] for _ in range(2)}
            assert kinds == {"ready", "toolsets"}

    def test_an_unknown_message_type_is_ignored(self, client):
        with client.websocket_connect(f"/ws?token={SESSION_TOKEN}") as ws:
            for _ in range(2):
                ws.receive_json()
            ws.send_json({"type": "not-a-real-message"})
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"
