from __future__ import annotations

from app.db import SessionLocal
from app.models import Workflow, WorkflowRun
from tests.conftest import csrf, login, make_user
from tests.test_maestro import DESCRIPTION, _connect, _design


def _create(client, name: str, shared: bool) -> int:  # type: ignore[no-untyped-def]
    token = csrf(client, "/maestro/new")
    data = {"name": name, "description": DESCRIPTION, "csrf_token": token}
    if shared:
        data["shared"] = "1"
    r = client.post("/maestro/new", data=data, follow_redirects=False)
    assert r.status_code == 303
    return int(r.headers["location"].split("/")[2])


def test_new_workflows_are_private_by_default_and_the_switch_shares_them(client, fake):
    make_user("admin", admin=True)
    alice = make_user("alice")
    _connect(alice, "github")
    login(client, "alice")
    page = client.get("/maestro/new").text
    assert 'role="switch"' in page and "Private" in page and "Shared" in page and "checked" not in page.split('role="switch"')[1][:40]
    private_id = _create(client, "Mine", shared=False)
    _design(private_id)
    shared_id = _create(client, "Team flow", shared=True)
    _design(shared_id)
    with SessionLocal() as db:
        assert db.get(Workflow, private_id).shared is False and db.get(Workflow, shared_id).shared is True
    # The edit page shows the switch and can turn sharing off without redesigning
    edit = client.get(f"/workflows/{shared_id}/edit").text
    assert 'name="shared" value="1" role="switch"' in edit and "checked" in edit.split('role="switch"')[1][:60]
    token = csrf(client, f"/workflows/{shared_id}/edit")
    with SessionLocal() as db:
        wf = db.get(Workflow, shared_id)
        content = wf.content
    client.post(f"/workflows/{shared_id}/edit", data={"name": "Team flow", "description": DESCRIPTION,
                                                       "content": content, "csrf_token": token})
    with SessionLocal() as db:
        wf = db.get(Workflow, shared_id)
        assert wf.shared is False and wf.design_status == "ready"


def test_others_can_view_and_run_a_shared_workflow_but_not_change_it(client, fake):
    make_user("admin", admin=True)
    alice, bob = make_user("alice"), make_user("bob")
    _connect(alice, "github")
    _connect(bob, "github")
    login(client, "alice")
    private_id = _create(client, "Mine", shared=False)
    _design(private_id)
    shared_id = _create(client, "Team flow", shared=True)
    _design(shared_id)
    client.post("/logout", data={"csrf_token": csrf(client, "/workflows")})

    login(client, "bob")
    listing = client.get("/workflows").text
    assert "Team flow" in listing and "Mine" not in listing and "alice" in listing
    assert f'/workflows/{shared_id}/edit' not in listing  # no Edit or Delete for bob
    page = client.get(f"/workflows/{shared_id}/design").text
    assert "Shared by <strong>alice</strong>" in page and f"/workflows/{shared_id}/edit" not in page
    assert f'action="/workflows/{shared_id}/design/config"' not in page
    assert client.get(f"/workflows/{private_id}/design").status_code == 404

    token = csrf(client, f"/workflows/{shared_id}/design")
    for path in ("edit", "delete", "design/config", "design/revise", "schedule"):
        r = client.post(f"/workflows/{shared_id}/{path}", data={"name": "x", "cron": "0 * * * *", "csrf_token": token})
        assert r.status_code == 404, path
    assert client.get(f"/workflows/{shared_id}/edit").status_code == 404

    r = client.post(f"/workflows/{shared_id}/run", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/runs/")
    with SessionLocal() as db:
        run = db.query(WorkflowRun).filter_by(workflow_id=shared_id).one()
        assert run.user_id == bob  # runs with bob's account and connectors
    assert client.get(f"/workflows/{shared_id}/history").status_code == 200


def test_someone_else_s_settings_apply_to_their_run_only(client, fake):
    make_user("admin", admin=True)
    alice, bob = make_user("alice"), make_user("bob")
    _connect(alice, "github")
    _connect(bob, "github")
    login(client, "alice")
    wf_id = _create(client, "Team flow", shared=True)
    _design(wf_id)
    client.post("/logout", data={"csrf_token": csrf(client, "/workflows")})
    login(client, "bob")
    page = client.get(f"/workflows/{wf_id}/run").text
    assert "Prefilled with the values of alice" in page and 'name="remember"' not in page
    token = csrf(client, f"/workflows/{wf_id}/run")
    r = client.post(f"/workflows/{wf_id}/run", data={"cfg:REPO": "bob/repo", "remember": "1", "csrf_token": token},
                    follow_redirects=False)
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    with SessionLocal() as db:
        assert "| REPO | bob/repo |" in db.get(WorkflowRun, run_id).initial_message
        assert db.get(Workflow, wf_id).design_json["config"][0]["value"] == ""  # alice's defaults untouched
