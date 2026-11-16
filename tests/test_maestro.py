from __future__ import annotations
import re

from app.db import SessionLocal
from app.maestro import service
from app.maestro.render import render_design_svg
from app.models import Connector, ConnectorStatus, Skill, Workflow, WorkflowRun
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


def _connect(user_id: int, provider: str) -> None:
    with SessionLocal() as db:
        db.add(Connector(user_id=user_id, provider=provider, mcp_server_url=f"https://{provider}.test/mcp",
                         auth_type="static_bearer", credential_id=f"cred_{provider}", last4="abcd",
                         permission_policy="auto", status=ConnectorStatus.CONFIGURED))
        db.commit()


def _setup(client, connect: tuple[str, ...] = ("github",)):  # type: ignore[no-untyped-def]
    make_user("admin", admin=True)
    uid = make_user("bob")
    for provider in connect:
        _connect(uid, provider)
    login(client, "bob")
    return uid


def test_description_becomes_a_design_with_actors(client, fake):
    _setup(client)
    wf_id = _create(client)
    page = client.get(f"/workflows/{wf_id}/design").text
    assert "Maestro is designing your workflow" in page and f"/workflows/{wf_id}/design/panel" in page

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


def test_design_is_refused_when_a_needed_tool_is_not_connected(client, fake):
    uid = _setup(client)  # only GitHub is connected
    fake.tools_response = {"connectors_needed": ["github", "linear"], "unsupported_tools": []}
    wf_id = _create(client)
    _design(wf_id)
    assert fake.design_calls == []  # no prompt is generated
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert wf.design_status == "blocked" and wf.content == ""
        assert "Linear" in wf.design_error and "not connected" in wf.design_error
    page = client.get(f"/workflows/{wf_id}/design").text
    assert "Maestro cannot create this workflow yet" in page and "/settings#connectors" in page
    assert "Run now" not in page

    _connect(uid, "linear")
    token = csrf(client, f"/workflows/{wf_id}/design")
    client.post(f"/workflows/{wf_id}/design/revise", data={"csrf_token": token})
    _design(wf_id)
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert wf.design_status == "ready" and wf.design_json["connectors_needed"] == ["github", "linear"]


def test_design_is_refused_for_tools_maestro_cannot_connect(client, fake):
    _setup(client)
    fake.tools_response = {"connectors_needed": ["github"],
                           "unsupported_tools": [{"name": "GitLab", "reason": "post merge requests"}]}
    wf_id = _create(client)
    _design(wf_id)
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert wf.design_status == "blocked" and "GitLab" in wf.design_error
        assert "cannot connect to today" in wf.design_error


def test_runs_are_blocked_when_a_connector_is_removed_later(client, fake):
    uid = _setup(client)
    wf_id = _create(client)
    _design(wf_id)
    with SessionLocal() as db:
        db.query(Connector).filter_by(user_id=uid, provider="github").delete()
        db.commit()
    page = client.get(f"/workflows/{wf_id}/design").text
    assert "no longer connected" in page and "GitHub" in page
    token = csrf(client, f"/workflows/{wf_id}/design")
    r = client.post(f"/workflows/{wf_id}/run", data={"csrf_token": token}, follow_redirects=False)
    assert r.headers["location"] == f"/workflows/{wf_id}/design"
    with SessionLocal() as db:
        assert db.query(WorkflowRun).filter_by(workflow_id=wf_id).count() == 0
    client.post(f"/workflows/{wf_id}/schedule", data={"cron": "0 * * * *", "csrf_token": token})
    assert fake.deployments == {}


def test_home_is_describe_your_workflow_and_only_maestro_workflows_exist(client, fake):
    uid = _setup(client)
    r = login(client, "bob")
    assert r.headers["location"] == "/"
    home = client.get("/").text
    assert "Describe your workflow" in home and 'action="/maestro/new"' in home
    assert 'href="/prompt"' not in home and ">Create<" in home
    assert client.get("/prompt").status_code == 404
    assert client.get("/workflows/new").status_code in (404, 405)
    with SessionLocal() as db:
        db.add(Workflow(user_id=uid, name="Legacy saved prompt", content="x"))  # no description
        db.commit()
    page = client.get("/workflows").text
    assert "Legacy saved prompt" not in page and "Describe a new workflow" in page


QUESTIONS = {"questions": [
    {"id": "tracker", "question": "Which tracker holds the page issues?", "why": "Maestro writes the issues there.",
     "options": ["GitHub Issues", "Jira"]},
    {"id": "pr", "question": "How do you want the pull requests?", "why": "Dispatch opens one PR per issue.",
     "options": ["One PR per ticket", "One PR per page"]},
]}


def test_clarify_questions_are_asked_before_the_design(client, fake):
    _setup(client)
    fake.clarify_response = QUESTIONS
    wf_id = _create(client)
    _design(wf_id)
    assert fake.design_calls == []  # nothing is designed before the answers
    with SessionLocal() as db:
        assert db.get(Workflow, wf_id).design_status == "needs_answers"
    page = client.get(f"/workflows/{wf_id}/design").text
    assert "Maestro needs a few decisions" in page and "Which tracker holds the page issues?" in page
    assert 'value="GitHub Issues" checked' in page and "recommended" in page

    token = csrf(client, f"/workflows/{wf_id}/design")
    # An unanswered "Other" is refused
    client.post(f"/workflows/{wf_id}/design/answers",
                data={"q:tracker": "__other__", "other:tracker": "", "q:pr": "One PR per ticket", "csrf_token": token})
    with SessionLocal() as db:
        assert db.get(Workflow, wf_id).design_status == "needs_answers"

    client.post(f"/workflows/{wf_id}/design/answers",
                data={"q:tracker": "__other__", "other:tracker": "Linear", "q:pr": "One PR per ticket",
                      "csrf_token": token})
    _design(wf_id)
    assert len(fake.clarify_calls) == 1  # not asked again for the same request
    sent = fake.design_calls[-1]["description"]
    assert "Decisions made by the user" in sent and "Which tracker holds the page issues? -> Linear" in sent
    assert "-> One PR per ticket" in sent
    page = client.get(f"/workflows/{wf_id}/design").text
    assert "Your decisions" in page and "Linear" in page


def test_clear_descriptions_go_straight_to_the_design(client, fake):
    _setup(client)
    wf_id = _create(client)
    _design(wf_id)
    assert len(fake.clarify_calls) == 1 and len(fake.design_calls) == 1
    with SessionLocal() as db:
        assert db.get(Workflow, wf_id).design_status == "ready"


def test_revision_keeps_previous_decisions_and_may_ask_again(client, fake):
    _setup(client)
    fake.clarify_response = QUESTIONS
    wf_id = _create(client)
    _design(wf_id)
    token = csrf(client, f"/workflows/{wf_id}/design")
    client.post(f"/workflows/{wf_id}/design/answers",
                data={"q:tracker": "GitHub Issues", "q:pr": "One PR per ticket", "csrf_token": token})
    _design(wf_id)
    fake.clarify_response = {"questions": []}
    client.post(f"/workflows/{wf_id}/design/revise", data={"feedback": "Run every 30 minutes", "csrf_token": token})
    _design(wf_id)
    # The previous decisions are given to the clarifier (never re-asked) and to the designer
    assert "Which tracker holds the page issues? -> GitHub Issues" in fake.clarify_calls[-1]
    assert "-> GitHub Issues" in fake.design_calls[-1]["description"]


def test_tool_check_runs_before_clarification(client, fake):
    _setup(client)
    fake.tools_response = {"connectors_needed": ["github", "linear"], "unsupported_tools": []}
    fake.clarify_response = QUESTIONS
    wf_id = _create(client)
    _design(wf_id)
    assert fake.clarify_calls == []
    with SessionLocal() as db:
        assert db.get(Workflow, wf_id).design_status == "blocked"


NEW_DESCRIPTION = (
    "Fix bugs one at a time: Maestro files each bug as a GitHub issue, Dispatch implements it, "
    "the tech lead reviews and merges the PR before the next bug starts."
)


def test_edit_changes_the_description_and_redesigns(client, fake):
    _setup(client)
    wf_id = _create(client)
    _design(wf_id)
    page = client.get(f"/workflows/{wf_id}/edit").text
    assert 'name="description"' in page and DESCRIPTION in page and 'name="content"' in page

    token = csrf(client, f"/workflows/{wf_id}/edit")
    r = client.post(f"/workflows/{wf_id}/edit", data={
        "name": "Bug fixing", "description": NEW_DESCRIPTION, "content": "# Mission\nIgnored.", "csrf_token": token,
    }, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/workflows/{wf_id}/design"
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert wf.name == "Bug fixing" and wf.description == NEW_DESCRIPTION and wf.design_status == "queued"
        assert "Ignored." not in wf.content
    _design(wf_id)
    assert fake.tools_calls[-1] == NEW_DESCRIPTION and fake.design_calls[-1]["description"] == NEW_DESCRIPTION

    # Prompt only: kept as edited, no redesign
    token = csrf(client, f"/workflows/{wf_id}/edit")
    client.post(f"/workflows/{wf_id}/edit", data={
        "name": "Bug fixing", "description": NEW_DESCRIPTION, "content": "# Mission\nBy hand.", "csrf_token": token,
    })
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert wf.content == "# Mission\nBy hand." and wf.design_status == "ready"

    r = client.post(f"/workflows/{wf_id}/edit", data={"name": "Bug fixing", "description": "Too short.",
                                                      "content": "x", "csrf_token": token})
    assert r.status_code == 400 and "more detail" in r.text


def test_edit_when_blocked_offers_the_description_only(client, fake):
    uid = _setup(client)
    fake.tools_response = {"connectors_needed": ["github", "linear"], "unsupported_tools": []}
    wf_id = _create(client)
    _design(wf_id)
    page = client.get(f"/workflows/{wf_id}/edit").text
    assert 'name="description"' in page and 'name="content"' not in page

    _connect(uid, "linear")
    token = csrf(client, f"/workflows/{wf_id}/edit")
    client.post(f"/workflows/{wf_id}/edit", data={"name": "Page migration", "description": NEW_DESCRIPTION,
                                                  "csrf_token": token})
    with SessionLocal() as db:
        assert db.get(Workflow, wf_id).design_status == "queued"
    _design(wf_id)
    assert fake.design_calls[-1]["description"] == NEW_DESCRIPTION


def test_try_again_takes_the_edited_description(client, fake):
    _setup(client)
    fake.design_error = "Claude declined to design this workflow."
    wf_id = _create(client)
    _design(wf_id)
    page = client.get(f"/workflows/{wf_id}/design").text
    assert 'name="description"' in page and DESCRIPTION in page

    fake.design_error = None
    token = csrf(client, f"/workflows/{wf_id}/design")
    client.post(f"/workflows/{wf_id}/design/revise", data={"description": NEW_DESCRIPTION, "csrf_token": token})
    _design(wf_id)
    assert fake.design_calls[-1]["description"] == NEW_DESCRIPTION
    with SessionLocal() as db:
        assert db.get(Workflow, wf_id).design_status == "ready"


def test_final_report_goes_to_the_reporting_step():
    wf = Workflow(name="x", content="", design_json={"steps": [
        {"id": "S1", "actor": "maestro", "title": "Preflight"},
        {"id": "S12", "actor": "human", "title": "Merge the PR"},
        {"id": "S13", "actor": "maestro", "title": "Escalate and report"},
    ]})
    assert "begin that message with `[step: S13]`" in service.run_context(wf, "manual")
    wf.design_json = {"steps": [{"id": "S1", "actor": "maestro", "title": "Preflight"}]}
    assert "marker of the last step you worked on" in service.run_context(wf, "manual")


def test_placeholder_repeat_and_loop_are_hidden():
    from app.maestro.render import step_tags

    assert step_tags({"actor": "maestro", "repeat": "Every run", "loop": "none"}) == ["↻ Every run"]
    assert step_tags({"actor": "maestro", "repeat": "N/A", "loop": "Until merged"}) == ["⟲ Until merged"]


def test_design_progress_is_recorded_and_shown(client, fake):
    from datetime import timedelta

    from app.models import utcnow

    _setup(client)
    wf_id = _create(client)
    page = client.get(f"/workflows/{wf_id}/design/panel").text
    assert "Checking the tools it needs" in page and "Starting" in page

    _design(wf_id)  # the fake designer reports 20 characters written
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        timings = wf.design_progress["timings"]
        assert set(timings) >= {"tools", "design"} and timings["design_chars"] == 20
        assert "stage" not in wf.design_progress  # nothing in progress any more
        estimates = service.design_estimates(db)
        assert estimates["design_chars"] == 20

        # A generation in the middle of writing the design
        now = utcnow()
        wf.design_status = "generating"
        wf.design_progress = {"started": (now - timedelta(seconds=90)).isoformat(),
                              "stage": "design", "stage_started": (now - timedelta(seconds=60)).isoformat(),
                              "done": ["tools", "clarify"], "phase": "writing", "chars": 9000,
                              "writing": "Drawing the steps"}
        view = service.progress_view(wf, {"tools": 10, "clarify": 30, "design": 200, "design_chars": 18000})
    states = [r["state"] for r in view["rows"]]
    assert states == ["done", "done", "active"]
    design_row = view["rows"][2]
    assert 60 <= design_row["pct"] <= 70 and "Drawing the steps · 9,000 characters" == design_row["note"]
    assert 0 < view["pct"] < 97 and view["remaining"] and view["elapsed"] == "1 min 30 s"


def test_writing_label_follows_the_json_being_written():
    assert service.writing_label('{"summary": "x", "steps": [{"id": "S1"') == "Drawing the steps"
    assert service.writing_label('"schedule": {}, "orchestration_prompt": "# Mission') == "Writing the orchestration prompt"
    assert service.writing_label("") is None


def test_branches_render_side_by_side_and_loops_go_back():
    def st(i, title, actor="maestro", branch="", loop_to="", loop=""):
        return {"id": i, "phase": "P", "title": title, "detail": "", "actor": actor, "dispatch_workflow": "none",
                "gate_owner": "", "repeat": "", "loop": loop, "done_when": "none", "branch": branch, "loop_to": loop_to}

    raw = {"steps": [st("S1", "Triage"), st("S2", "Bug spec", branch="Bug"), st("S3", "SFD", branch="Feature"),
                     st("S4", "Build", "dispatch"), st("S5", "Approve", "human", loop_to="S4", loop="PR refused"),
                     st("S6", "Bad loop", loop_to="S9")]}
    design = service._normalize_design(raw, None)
    assert design["steps"][4]["loop_to"] == "S4" and design["steps"][5]["loop_to"] == ""  # unknown target dropped
    svg = str(render_design_svg(design))
    xs = {m.group(1): float(m.group(2)) for m in re.finditer(r'data-step="(S\d)">.*?<rect class="ms-ring" x="([\d.]+)"', svg, re.S)}
    ys = {m.group(1): float(m.group(2)) for m in re.finditer(r'data-step="(S\d)">.*?<rect class="ms-ring" x="[\d.]+" y="([\d.]+)"', svg, re.S)}
    assert ys["S2"] == ys["S3"] and xs["S2"] < xs["S3"]  # the two paths are side by side
    assert ">Bug<" in svg and ">Feature<" in svg
    assert 'stroke-dasharray="5 4"' in svg and "No · PR refused" in svg and ">Yes<" in svg
    from app.maestro.render import step_tags

    assert "⟲ back to S4 when PR refused" in step_tags(design["steps"][4])


def test_answered_questions_show_as_done_while_designing(client, fake, monkeypatch):
    _setup(client)
    fake.clarify_response = QUESTIONS
    wf_id = _create(client)
    _design(wf_id)
    token = csrf(client, f"/workflows/{wf_id}/design")
    client.post(f"/workflows/{wf_id}/design/answers",
                data={"q:tracker": "GitHub Issues", "q:pr": "One PR per ticket", "csrf_token": token})

    seen = {}
    original = service._start_stage

    def spy(db, wf, stage):  # type: ignore[no-untyped-def]
        original(db, wf, stage)
        if stage == "design":
            seen["view"] = service.progress_view(wf, service.DEFAULT_TIMINGS)

    monkeypatch.setattr(service, "_start_stage", spy)
    _design(wf_id)
    rows = {r["key"]: r for r in seen["view"]["rows"]}
    assert rows["tools"]["state"] == "done" and rows["design"]["state"] == "active"
    assert rows["clarify"]["state"] == "done" and rows["clarify"]["note"] == "Already answered"
