from __future__ import annotations

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Connector, UserSettings
from app.settings import routes as settings_routes
from tests.conftest import csrf, login, make_user


def _settings(user_id: int) -> UserSettings:
    with SessionLocal() as db:
        s = db.get(UserSettings, user_id)
        assert s is not None
        return s


def test_api_key_is_encrypted_and_resources_created_idempotently(client, fake):
    make_user("admin", admin=True)
    uid = make_user("bob", configured=False)
    login(client, "bob")
    token = csrf(client, "/settings")
    key = "sk-ant-api03-SECRET-abcd"
    r = client.post("/settings/anthropic", data={"api_key": key, "model": "claude-opus-5-5", "csrf_token": token})
    assert r.status_code == 200
    s = _settings(uid)
    assert s.anthropic_api_key_encrypted and key not in s.anthropic_api_key_encrypted
    assert s.api_key_last4 == "abcd"
    assert s.agent_id and s.environment_id and s.vault_id
    assert key not in r.text  # jamais réaffichée
    assert [c for c, _ in fake.calls].count("agent.create") == 1

    # Ré-enregistrer sans changement : aucune mise à jour de l'agent
    client.post("/settings/anthropic", data={"api_key": "", "model": "claude-opus-5-5", "csrf_token": token})
    assert [c for c, _ in fake.calls].count("agent.update") == 0
    # Changement de modèle : mise à jour (nouvelle version)
    client.post("/settings/anthropic", data={"api_key": "", "model": "claude-sonnet-5-5", "csrf_token": token})
    assert [c for c, _ in fake.calls].count("agent.update") == 1
    assert fake.agent_configs[-1]["model"] == "claude-sonnet-5-5"


def test_bad_api_key_is_rejected(client, fake):
    make_user("admin", admin=True)
    uid = make_user("bob", configured=False)
    fake.bad_key = True
    login(client, "bob")
    token = csrf(client, "/settings")
    r = client.post("/settings/anthropic", data={"api_key": "sk-bad", "model": "claude-opus-5-5", "csrf_token": token})
    assert "Clé API non enregistrée" in r.text
    assert _settings(uid).anthropic_api_key_encrypted is None


def test_connector_declared_in_agent_with_permission_policy(client, fake, monkeypatch):
    monkeypatch.setattr(settings_routes.mcp_oauth, "probe_mcp", lambda url, token: (True, "Handshake MCP réussi."))
    make_user("admin", admin=True)
    uid = make_user("bob")
    login(client, "bob")
    token = csrf(client, "/settings")
    secret = "lin_api_TOKEN1234"
    r = client.post(
        "/settings/connectors/linear",
        data={"mcp_server_url": "https://mcp.linear.app/mcp", "token": secret, "csrf_token": token},
    )
    assert secret not in r.text
    with SessionLocal() as db:
        c = db.scalar(select(Connector).where(Connector.user_id == uid))
        assert c and c.credential_id and c.last4 == "1234" and c.auth_type == "static_bearer"
        assert c.permission_policy == "always_ask"
    cfg = fake.agent_configs[-1]
    assert cfg["mcp_servers"] == [{"type": "url", "name": "linear", "url": "https://mcp.linear.app/mcp"}]
    toolset = [t for t in cfg["tools"] if t["type"] == "mcp_toolset"][0]
    assert toolset["default_config"]["permission_policy"] == {"type": "always_ask"}
    # Aucun secret dans l'agent
    assert secret not in str(cfg)

    client.post("/settings/connectors/linear/permission", data={"permission_policy": "always_allow", "csrf_token": token})
    toolset = [t for t in fake.agent_configs[-1]["tools"] if t["type"] == "mcp_toolset"][0]
    assert toolset["default_config"]["permission_policy"] == {"type": "always_allow"}

    client.post("/settings/connectors/linear/delete", data={"csrf_token": token})
    assert any(c == "archive_credential" for c, _ in fake.calls)
    assert fake.agent_configs[-1]["mcp_servers"] == []


def test_connector_with_rejected_token_is_not_saved(client, fake, monkeypatch):
    monkeypatch.setattr(settings_routes.mcp_oauth, "probe_mcp", lambda url, token: (False, "Jeton refusé (401)."))
    make_user("admin", admin=True)
    uid = make_user("bob")
    login(client, "bob")
    token = csrf(client, "/settings")
    r = client.post(
        "/settings/connectors/github",
        data={"mcp_server_url": "https://api.githubcopilot.com/mcp/", "token": "ghp_x", "csrf_token": token},
    )
    assert "pas été enregistré" in r.text
    with SessionLocal() as db:
        assert db.scalar(select(Connector).where(Connector.user_id == uid)) is None
