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

PASSWORD = "strong-password"


@dataclass
class FakeManagedAgents:
    """Fake Managed Agents client: records the calls and replays scripted events."""

    calls: list[tuple[str, Any]] = field(default_factory=list)
    agent_configs: list[dict[str, Any]] = field(default_factory=list)
    session_events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Events the stream will deliver after the history
    stream_script: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    counter: int = 0
    bad_key: bool = False
    plan_calls: list[tuple[str, str, list[str]]] = field(default_factory=list)
    plan_error: str | None = None
    plan_response: dict[str, Any] | None = None
    design_calls: list[dict[str, Any]] = field(default_factory=list)
    design_error: str | None = None
    tools_response: dict[str, Any] = field(
        default_factory=lambda: {"connectors_needed": ["github"], "unsupported_tools": []}
    )
    tools_calls: list[str] = field(default_factory=list)
    deployments: dict[str, dict[str, Any]] = field(default_factory=dict)
    deployment_runs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def _id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}_{self.counter:04d}"

    def check_api_key(self) -> None:
        self.calls.append(("check_api_key", None))
        if self.bad_key:
            raise anthropic_client.ManagedAgentsError("Anthropic API key rejected (401).")

    def sync_resources(
        self, state: ResourceState, model: str, connectors: list[ConnectorSpec], login: str,
        force: bool = False,
    ) -> SyncResult:
        result = SyncResult(state=ResourceState(**state.__dict__))
        st = result.state
        if not st.environment_id:
            st.environment_id = self._id("env")
            result.created.append("environment")
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
        return "Credential present in the vault."

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

    def check_tools(self, model, description):  # type: ignore[no-untyped-def]
        self.tools_calls.append(description)
        return self.tools_response

    def design_workflow(self, model, description, services, previous, feedback):  # type: ignore[no-untyped-def]
        self.design_calls.append({"description": description, "services": services,
                                  "previous": previous, "feedback": feedback})
        if self.design_error:
            raise anthropic_client.ManagedAgentsError(self.design_error)
        return {
            "summary": "Migrate each page through Dispatch with human gates.",
            "steps": [
                {"id": "S1", "phase": "Spec", "title": "Write the stage issue", "detail": "Scope and context",
                 "actor": "maestro", "dispatch_workflow": "none", "gate_owner": "", "repeat": "per page", "loop": ""},
                {"id": "S2", "phase": "Spec", "title": "Generate the spec", "detail": "Issue to code run",
                 "actor": "dispatch", "dispatch_workflow": "issue_to_code", "gate_owner": "", "repeat": "",
                 "loop": "", "done_when": "pr_merged"},
                {"id": "S3", "phase": "Spec", "title": "Approve the spec PR", "detail": "",
                 "actor": "human", "dispatch_workflow": "none", "gate_owner": "PO", "repeat": "", "loop": ""},
                {"id": "S4", "phase": "Tests", "title": "Run e2e on <preview>", "detail": "",
                 "actor": "upsun", "dispatch_workflow": "none", "gate_owner": "", "repeat": "", "loop": ""},
            ],
            "config": [{"key": "REPO", "description": "GitHub owner/repo"}],
            "assumptions": ["GitHub is the code host"],
            "not_possible": [{"need": "Custom Dispatch workflow", "alternative": "Maestro chains Issue to code runs"}],
            "tools": ["GitHub connector"],
            "schedule": {"cron": "0 * * * *", "reason": "Hourly is enough for human gates."},
            "orchestration_prompt": "# Mission\nYou are the Executor of the page migration factory.",
        }

    def create_deployment(self, name, agent_id, environment_id, vault_ids, message, cron, timezone, budget_cents):  # type: ignore[no-untyped-def]
        dep_id = self._id("depl")
        self.deployments[dep_id] = {"name": name, "message": message, "cron": cron, "timezone": timezone,
                                    "budget": budget_cents, "paused": False, "archived": False}
        self.calls.append(("create_deployment", dep_id))
        return dep_id

    def update_deployment(self, deployment_id, message, cron, timezone, budget_cents):  # type: ignore[no-untyped-def]
        self.deployments[deployment_id].update(message=message, cron=cron, timezone=timezone, budget=budget_cents)
        self.calls.append(("update_deployment", deployment_id))

    def set_deployment_paused(self, deployment_id, paused):  # type: ignore[no-untyped-def]
        self.deployments[deployment_id]["paused"] = paused

    def run_deployment(self, deployment_id):  # type: ignore[no-untyped-def]
        self.calls.append(("run_deployment", deployment_id))

    def archive_deployment(self, deployment_id):  # type: ignore[no-untyped-def]
        self.deployments[deployment_id]["archived"] = True

    def list_deployment_runs(self, deployment_id):  # type: ignore[no-untyped-def]
        return list(self.deployment_runs.get(deployment_id, []))

    def generate_plan(self, model: str, request_text: str, services: list[str]) -> dict[str, Any]:
        self.plan_calls.append((model, request_text, services))
        if self.plan_error:
            raise anthropic_client.ManagedAgentsError(self.plan_error)
        return self.plan_response or {
            "summary": "Review recent Linear issues and comment on one.",
            "steps": [
                {"title": "List my recent issues", "detail": "5 most recently updated",
                 "kind": "read", "service": "linear"},
                {"title": "Ask which issue", "detail": "", "kind": "question", "service": "none"},
                {"title": "Add the comment", "detail": "Reviewed via Dispatch",
                 "kind": "action", "service": "linear"},
                {"title": "Summarize", "detail": "", "kind": "result", "service": "none"},
            ],
        }


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
    """SQLite by default; PostgreSQL if TEST_DATABASE_URL is set (database emptied for each test)."""
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
    assert m, f"no CSRF token on {path}"
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


def start_run(client: TestClient, text: str, login_name: str = "bob") -> int:
    """Start a run of a ready Maestro workflow whose orchestration prompt is `text`; return the run id."""
    import secrets as _secrets

    from sqlalchemy import select

    from app.models import Workflow

    with SessionLocal() as db:
        uid = db.scalar(select(User.id).where(User.login == login_name))
        wf = Workflow(user_id=uid, name=f"wf-{_secrets.token_hex(3)}", content=text,
                      description="A test workflow described in enough words to be accepted.",
                      design_json={"summary": "", "steps": [], "config": [], "connectors_needed": []},
                      design_status="ready")
        db.add(wf)
        db.commit()
        wf_id = wf.id
    token = csrf(client, "/workflows")
    r = client.post(f"/workflows/{wf_id}/run", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/runs/"), r.headers.get("location")
    return int(r.headers["location"].rsplit("/", 1)[1])
