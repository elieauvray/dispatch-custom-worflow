from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from cryptography.fernet import Fernet

os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-" + "x" * 40)
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("COOKIE_SECURE", "true")
os.environ.setdefault("PUBLIC_BASE_URL", "https://testserver")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app import anthropic_client  # noqa: E402
from app.anthropic_client import (  # noqa: E402
    ConnectorSpec,
    ResourceState,
    SyncResult,
    build_agent_config,
    config_hash,
)
from app.db import Base, SessionLocal, configure_engine  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import User, UserSettings  # noqa: E402
from app.security import encrypt, hash_password  # noqa: E402

PASSWORD = "motdepasse-solide"


@dataclass
class FakeManagedAgents:
    """Faux client Managed Agents : enregistre les appels et rejoue des événements scriptés."""

    calls: list[tuple[str, Any]] = field(default_factory=list)
    agent_configs: list[dict[str, Any]] = field(default_factory=list)
    session_events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Événements que le flux délivrera après l'historique
    stream_script: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    counter: int = 0
    bad_key: bool = False

    def _id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}_{self.counter:04d}"

    def check_api_key(self) -> None:
        self.calls.append(("check_api_key", None))
        if self.bad_key:
            raise anthropic_client.ManagedAgentsError("Clé API Anthropic refusée (401).")

    def sync_resources(
        self, state: ResourceState, model: str, connectors: list[ConnectorSpec], login: str,
        force: bool = False,
    ) -> SyncResult:
        result = SyncResult(state=ResourceState(**state.__dict__))
        st = result.state
        if not st.environment_id:
            st.environment_id = self._id("env")
            result.created.append("environnement")
        if not st.vault_id:
            st.vault_id = self._id("vlt")
            result.created.append("vault")
        config = build_agent_config(model, connectors, login)
        digest = config_hash(config)
        if not st.agent_id:
            st.agent_id, st.agent_version = self._id("agent"), 1
            st.agent_config_hash = digest
            result.created.append("agent")
            self.agent_configs.append(config)
            self.calls.append(("agent.create", config))
        elif force or digest != st.agent_config_hash:
            st.agent_version = (st.agent_version or 0) + 1
            st.agent_config_hash = digest
            result.updated.append("agent")
            self.agent_configs.append(config)
            self.calls.append(("agent.update", config))
        return result

    def archive_vault(self, vault_id: str) -> None:
        self.calls.append(("archive_vault", vault_id))

    def put_static_bearer(self, vault_id: str, url: str, token: str, display_name: str) -> str:
        self.calls.append(("put_static_bearer", (vault_id, url)))
        return self._id("cred")

    def put_mcp_oauth(self, vault_id, url, access_token, expires_at, refresh, display_name) -> str:  # type: ignore[no-untyped-def]
        self.calls.append(("put_mcp_oauth", (vault_id, url, refresh)))
        return self._id("cred")

    def archive_credential(self, vault_id: str, credential_id: str) -> None:
        self.calls.append(("archive_credential", credential_id))

    def check_credential(self, vault_id: str, credential_id: str, auth_type: str) -> str:
        return "Credential présent dans le vault."

    def create_session(self, agent_id, agent_version, environment_id, vault_ids, title) -> str:  # type: ignore[no-untyped-def]
        sid = self._id("sesn")
        self.calls.append(("create_session", (agent_id, environment_id, tuple(vault_ids))))
        self.session_events.setdefault(sid, [])
        return sid

    def _append(self, sid: str, ev: dict[str, Any]) -> None:
        ev = {"id": self._id("sevt"), **ev}
        self.session_events.setdefault(sid, []).append(ev)

    def send_user_message(self, session_id: str, text: str) -> None:
        self.calls.append(("send_user_message", (session_id, text)))
        self._append(session_id, {"type": "user.message", "content": [{"type": "text", "text": text}]})

    def send_tool_confirmations(self, session_id, decisions) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(("send_tool_confirmations", (session_id, decisions)))

    def interrupt(self, session_id: str) -> None:
        self.calls.append(("interrupt", session_id))

    def list_events(self, session_id: str) -> list[dict[str, Any]]:
        return list(self.session_events.get(session_id, []))

    @contextmanager
    def open_stream(self, session_id: str) -> Iterator[Iterator[dict[str, Any]]]:
        def gen() -> Iterator[dict[str, Any]]:
            for ev in self.stream_script.pop(session_id, []) or self.stream_script.pop("*", []):
                ev = {"id": self._id("sevt"), **ev}
                self.session_events.setdefault(session_id, []).append(ev)
                yield ev

        yield gen()

    def session_status(self, session_id: str) -> str:
        return "idle"


@pytest.fixture(autouse=True)
def fast_worker(monkeypatch):  # type: ignore[no-untyped-def]
    from app import worker

    monkeypatch.setattr(worker, "STALL_LIMIT", 2)
    monkeypatch.setattr(worker, "STALL_SLEEP", 0)
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)


@pytest.fixture
def fake() -> Iterator[FakeManagedAgents]:
    f = FakeManagedAgents()
    previous = anthropic_client.client_factory
    anthropic_client.client_factory = lambda api_key: f  # type: ignore[assignment]
    yield f
    anthropic_client.client_factory = previous


@pytest.fixture
def db_url(tmp_path: Any) -> Iterator[str]:
    """SQLite par défaut ; PostgreSQL si TEST_DATABASE_URL est défini (base vidée à chaque test)."""
    pg = os.environ.get("TEST_DATABASE_URL")
    if pg:
        url = pg.replace("postgresql://", "postgresql+psycopg://", 1)
        engine = configure_engine(url)
        Base.metadata.drop_all(engine)
    else:
        url = f"sqlite:///{tmp_path}/test.db"
        engine = configure_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    yield url
    engine.dispose()


@pytest.fixture
def app(db_url: str, fake: FakeManagedAgents):  # type: ignore[no-untyped-def]
    return create_app()


@pytest.fixture
def client(app) -> TestClient:  # type: ignore[no-untyped-def]
    return TestClient(app, base_url="https://testserver")


def csrf(client: TestClient, path: str = "/login") -> str:
    html = client.get(path).text
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, f"pas de jeton CSRF sur {path}"
    return m.group(1)


def make_user(login: str, *, admin: bool = False, configured: bool = True) -> int:
    db = SessionLocal()
    try:
        user = User(login=login, password_hash=hash_password(PASSWORD), is_admin=admin)
        db.add(user)
        db.flush()
        settings = UserSettings(user_id=user.id, model="claude-opus-5-5")
        if configured:
            settings.anthropic_api_key_encrypted = encrypt("sk-ant-api03-test-" + login)
            settings.api_key_last4 = "test"
            settings.agent_id = f"agent_{login}"
            settings.agent_version = 1
            settings.environment_id = f"env_{login}"
            settings.vault_id = f"vlt_{login}"
        db.add(settings)
        db.commit()
        return user.id
    finally:
        db.close()


def login(client: TestClient, user: str, password: str = PASSWORD) -> Any:
    token = csrf(client, "/login")
    return client.post(
        "/login", data={"login": user, "password": password, "csrf_token": token},
        follow_redirects=False,
    )
