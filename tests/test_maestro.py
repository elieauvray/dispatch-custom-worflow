from __future__ import annotations

from app.db import SessionLocal
from app.maestro import service
from app.maestro.render import render_design_svg
from app.models import Skill, Workflow, WorkflowRun
from tests.conftest import csrf, login, make_user

DESCRIPTION = (
    "Migrate a legacy app page by page. For each page: write a spec through Dispatch, the PO approves the PR, "
    "then split into tickets, implement each ticket with Dispatch and run e2e tests on the Upsun preview."
)


def _create(client) -> int:
    token = csrf(client, "/maestro/new")
    r = client.post("/maestro/new", data={"name": "Page migration", "description": DESCRIPTION, "csrf_token": token},
                    follow_redirects=False)
    assert r.status_code == 303
    return int(r.headers["location"].split("/")[2])


def _design(wf_id: int) -> None:
    with SessionLocal() as db:
        assert service.claim_design_jobs(db) == [wf_id]
        service.generate_design(db, wf_id)


def _setup(client):  # type: ignore[no-untyped-def]
    make_user("admin", admin=True)
    uid = make_user("bob")
    login(client, "bob")
    return uid


def test_description_becomes_a_design_with_actors(client, fake):
    _setup(client)
    wf_id = _create(client)
    page = client.get(f"/workflows/{wf_id}/design").text
    assert "Maestro is designing the workflow" in page and f"/workflows/{wf_id}/design/panel" in page

    _design(wf_id)
    assert fake.design_calls[0]["description"] == DESCRIPTION
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert wf.design_status == "ready" and wf.content.startswith("# Mission")
        assert wf.schedule_cron == "0 * * * *"
        assert [s["actor"] for s in wf.design_json["steps"]] == ["maestro", "dispatch", "human", "upsun"]

    page = client.get(f"/workflows/{wf_id}/design").text
    for text in ("Upsun Dispatch", "People (gates)", "Upsun Cloud", "GATE · PO", "Issue to code",
                 "Custom Dispatch workflow", "Save config", "Schedule Maestro", "Run now", "done when PR merged"):
        assert text in page, text
    # The panel endpoint tells HTMX to reload the page once the design is ready
    r = client.get(f"/workflows/{wf_id}/design/panel", headers={"HX-Request": "true"})
    assert r.headers.get("HX-Redirect") == f"/workflows/{wf_id}/design"


def test_run_now_sends_orchestration_prompt_config_and_skills(client, fake):
    _setup(client)
    with SessionLocal() as db:
        db.add(Skill(name="Tone", content="Be concise.", position=1, active=True))
        db.commit()
    wf_id = _create(client)
    _design(wf_id)
    token = csrf(client, f"/workflows/{wf_id}/design")
    client.post(f"/workflows/{wf_id}/design/config", data={"cfg:REPO": "acme/shop", "csrf_token": token})
    r = client.post(f"/workflows/{wf_id}/run", data={"csrf_token": token}, follow_redirects=False)
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    with SessionLocal() as db:
        msg = db.get(WorkflowRun, run_id).initial_message
    assert msg.index("Be concise.") < msg.index("# Mission") < msg.index("Trigger: manual")
    assert "| REPO | acme/shop |" in msg and "never interact with it" in msg


def test_schedule_lifecycle_and_scheduled_runs_import(client, fake):
    _setup(client)
    wf_id = _create(client)
    _design(wf_id)
    token = csrf(client, f"/workflows/{wf_id}/design")
    client.post(f"/workflows/{wf_id}/schedule",
                data={"cron": "*/30 * * * *", "timezone": "Europe/Paris", "budget": "5", "csrf_token": token})
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        dep_id = wf.deployment_id
        assert dep_id and wf.deployment_status == "active" and wf.budget_cents == 500
    dep = fake.deployments[dep_id]
    assert dep["cron"] == "*/30 * * * *" and "Trigger: schedule" in dep["message"] and dep["budget"] == 500

    r = client.post(f"/workflows/{wf_id}/schedule", data={"cron": "bad", "csrf_token": token})
    assert "Invalid cron" in r.text

    client.post(f"/workflows/{wf_id}/schedule/pause", data={"csrf_token": token})
    assert fake.deployments[dep_id]["paused"] is True

    # Editing the prompt by hand updates the schedule
    client.post(f"/workflows/{wf_id}/edit", data={"name": "Page migration", "content": "# Mission\nEdited.",
                                                  "csrf_token": token})
    assert "Edited." in fake.deployments[dep_id]["message"]

    fake.deployment_runs[dep_id] = [
        {"id": "drun_1", "session_id": "sesn_sched_1"},
        {"id": "drun_2", "session_id": None, "error": {"type": "vault_not_found", "message": "gone"}},
    ]
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert service.import_scheduled_runs(db, wf) == 2
        assert service.import_scheduled_runs(db, wf) == 0  # no duplicates
        runs = {r.deployment_run_id: r for r in db.query(WorkflowRun).filter_by(workflow_id=wf_id)}
    assert runs["drun_1"].trigger == "schedule" and runs["drun_1"].session_id == "sesn_sched_1"
    assert runs["drun_1"].status == "running"
    assert runs["drun_2"].status == "failed" and "vault_not_found" in runs["drun_2"].error

    client.post(f"/workflows/{wf_id}/delete", data={"csrf_token": token})
    assert fake.deployments[dep_id]["archived"] is True


def test_revision_keeps_config_values_and_sends_feedback(client, fake):
    _setup(client)
    wf_id = _create(client)
    _design(wf_id)
    token = csrf(client, f"/workflows/{wf_id}/design")
    client.post(f"/workflows/{wf_id}/design/config", data={"cfg:REPO": "acme/shop", "csrf_token": token})
    client.post(f"/workflows/{wf_id}/design/revise", data={"feedback": "Use Jira", "csrf_token": token})
    _design(wf_id)
    assert fake.design_calls[-1]["feedback"] == "Use Jira" and fake.design_calls[-1]["previous"]
    with SessionLocal() as db:
        assert db.get(Workflow, wf_id).design_json["config"][0]["value"] == "acme/shop"


def test_design_failure_is_shown_with_retry(client, fake):
    _setup(client)
    fake.design_error = "Claude declined to design this workflow."
    wf_id = _create(client)
    _design(wf_id)
    page = client.get(f"/workflows/{wf_id}/design").text
    assert "could not be generated" in page and "Try again" in page


def test_render_escapes_and_access_control(client, fake):
    svg = str(render_design_svg({"summary": "<x>", "steps": [
        {"id": "S1", "phase": "<b>P</b>", "title": "<script>", "detail": "", "actor": "dispatch",
         "dispatch_workflow": "issue_to_code", "gate_owner": "", "repeat": "", "loop": ""}]}))
    assert "<script>" not in svg and "&lt;script&gt;" in svg and "Issue to code" in svg

    make_user("admin", admin=True)
    alice = make_user("alice")
    make_user("bob")
    with SessionLocal() as db:
        wf = Workflow(user_id=alice, name="Private", content="", description="secret process " * 5)
        db.add(wf)
        db.commit()
        wf_id = wf.id
    login(client, "bob")
    assert client.get(f"/workflows/{wf_id}/design").status_code == 404
    assert client.get(f"/workflows/{wf_id}/design.svg").status_code == 404
