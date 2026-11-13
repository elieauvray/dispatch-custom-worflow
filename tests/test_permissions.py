from __future__ import annotations

from app.db import SessionLocal
from app.models import RunStatus, Workflow, WorkflowRun
from tests.conftest import csrf, login, make_user


def _workflow_for(user_id: int) -> tuple[int, int]:
    with SessionLocal() as db:
        wf = Workflow(user_id=user_id, name="Secret", content="contenu privé")
        db.add(wf)
        db.flush()
        run = WorkflowRun(
            user_id=user_id, workflow_id=wf.id, status=RunStatus.COMPLETED,
            content_snapshot="contenu privé", initial_message="x", pending_event_ids=[],
        )
        db.add(run)
        db.commit()
        return wf.id, run.id


def test_user_cannot_access_admin_pages(client):
    make_user("admin", admin=True)
    make_user("bob")
    login(client, "bob")
    assert client.get("/admin/users").status_code == 403
    assert client.get("/admin/skills").status_code == 403
    token = csrf(client, "/prompt")
    r = client.post("/admin/users", data={"login": "x", "password": "y" * 12, "csrf_token": token})
    assert r.status_code == 403
    r = client.post("/admin/skills", data={"name": "x", "content": "y", "csrf_token": token})
    assert r.status_code == 403
    page = client.get("/prompt").text
    assert "/admin/users" not in page


def test_user_cannot_access_other_users_workflows(client):
    make_user("admin", admin=True)
    alice = make_user("alice")
    make_user("bob")
    wf_id, run_id = _workflow_for(alice)
    login(client, "bob")
    token = csrf(client, "/workflows")
    assert "Secret" not in client.get("/workflows").text
    assert client.get(f"/workflows/{wf_id}/edit").status_code == 404
    assert client.get(f"/workflows/{wf_id}/history").status_code == 404
    assert client.get(f"/runs/{run_id}").status_code == 404
    assert client.get(f"/runs/{run_id}/stream").status_code == 404
    assert client.post(f"/workflows/{wf_id}/run", data={"csrf_token": token}).status_code == 404
    assert client.post(f"/workflows/{wf_id}/delete", data={"csrf_token": token}).status_code == 404
    assert client.post(
        f"/runs/{run_id}/message", data={"text": "hi", "csrf_token": token}
    ).status_code == 404
    with SessionLocal() as db:
        assert db.get(Workflow, wf_id) is not None


def test_admin_pages_and_user_management(client, fake):
    admin_id = make_user("admin", admin=True)
    bob_id = make_user("bob")
    _workflow_for(bob_id)
    login(client, "admin")
    assert client.get("/admin/users").status_code == 200
    token = csrf(client, "/admin/users")

    r = client.post(f"/admin/users/{admin_id}/delete", data={"csrf_token": token})
    assert "your own account" in r.text

    r = client.post("/admin/users", data={"login": "carol", "password": "court", "csrf_token": token})
    assert "at least 10 characters" in r.text
    r = client.post("/admin/users", data={"login": "carol", "password": "mot-de-passe-ok", "csrf_token": token})
    assert "carol" in r.text

    r = client.post(f"/admin/users/{bob_id}/delete", data={"csrf_token": token})
    assert r.status_code == 200
    assert ("archive_vault", "vlt_bob") in fake.calls
    with SessionLocal() as db:
        assert db.query(Workflow).filter_by(user_id=bob_id).count() == 0
        assert db.query(WorkflowRun).filter_by(user_id=bob_id).count() == 0


def test_password_reset_invalidates_sessions(client):
    make_user("admin", admin=True)
    bob_id = make_user("bob")
    from fastapi.testclient import TestClient

    bob_client = TestClient(client.app, base_url="https://testserver")
    login(bob_client, "bob")
    assert bob_client.get("/prompt", follow_redirects=False).status_code == 200
    login(client, "admin")
    token = csrf(client, "/admin/users")
    client.post(f"/admin/users/{bob_id}/password", data={"password": "nouveau-mdp-123", "csrf_token": token})
    assert bob_client.get("/prompt", follow_redirects=False).status_code == 303
    assert login(bob_client, "bob", "nouveau-mdp-123").status_code == 303
