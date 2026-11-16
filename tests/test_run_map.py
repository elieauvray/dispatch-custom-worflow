from __future__ import annotations

import re
import threading

from app.db import SessionLocal
from app.models import Connector, ConnectorStatus, Workflow, WorkflowRun
from app.runs import routes as runs_routes
from app.runs.events import step_marker, strip_step_markers
from app.worker import RunDriver
from tests.conftest import csrf, login, make_user

STEPS = [
    {"id": "S1", "phase": "Setup", "title": "Write the issue", "detail": "", "actor": "maestro",
     "dispatch_workflow": "none", "gate_owner": "", "repeat": "", "loop": "", "done_when": "none"},
    {"id": "S2", "phase": "Build", "title": "Implement", "detail": "", "actor": "dispatch",
     "dispatch_workflow": "issue_to_code", "gate_owner": "", "repeat": "", "loop": "", "done_when": "pr_merged"},
    {"id": "S3", "phase": "Build", "title": "Merge the PR", "detail": "", "actor": "human",
     "dispatch_workflow": "none", "gate_owner": "Dev", "repeat": "", "loop": "", "done_when": "none"},
]


def test_step_marker_parsing():
    assert step_marker("[step: S2]\nCreating the issue") == "S2"
    assert step_marker("[STEP:S1] a [step: S3] b") == "S3"
    assert step_marker("no marker") is None
    assert strip_step_markers("[step: S2]\nCreating the issue") == "Creating the issue"


def _workflow(uid: int) -> int:
    with SessionLocal() as db:
        db.add(Connector(user_id=uid, provider="github", mcp_server_url="https://github.test/mcp",
                         auth_type="static_bearer", credential_id="cred", last4="abcd",
                         permission_policy="auto", status=ConnectorStatus.CONFIGURED))
        wf = Workflow(user_id=uid, name="Chain", description="A chain of Dispatch work described in words.",
                      content="# Mission\nDo the chain.", design_status="ready",
                      design_json={"summary": "s", "steps": STEPS, "config": [], "connectors_needed": ["github"]})
        db.add(wf)
        db.commit()
        return wf.id


def test_run_page_shows_the_map_and_tracks_steps(client, fake, monkeypatch):
    monkeypatch.setattr(runs_routes, "SSE_MAX_SECONDS", 1)
    make_user("admin", admin=True)
    uid = make_user("bob")
    wf_id = _workflow(uid)
    login(client, "bob")
    token = csrf(client, "/workflows")
    r = client.post(f"/workflows/{wf_id}/run", data={"csrf_token": token}, follow_redirects=False)
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    with SessionLocal() as db:
        run = db.get(WorkflowRun, run_id)
        assert run.design_snapshot["steps"][1]["id"] == "S2"
        # Maestro is told the step ids and the marker format
        assert "[step: S1]" in run.initial_message and "| S2 | dispatch | Implement |" in run.initial_message

    fake.stream_script["*"] = [
        {"type": "agent.message", "content": [{"type": "text", "text": "[step: S1]\nWriting the issue."}]},
        {"type": "agent.mcp_tool_use", "name": "create_issue", "mcp_server_name": "github", "input": {},
         "evaluated_permission": "allow"},
        {"type": "agent.message", "content": [{"type": "text", "text": "[step: S9]\nUnknown step is ignored."}]},
        {"type": "agent.message", "content": [{"type": "text", "text": "[step: S2]\nDispatch is implementing."}]},
        {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
    ]
    RunDriver(run_id, threading.Event()).run()

    page = client.get(f"/runs/{run_id}").text
    assert 'id="run-map"' in page and page.count('class="ms-step"') == 3
    assert "Planned workflow" not in page  # the design map replaces the predicted plan
    steps = re.findall(r'class="ev-item" data-step="([^"]*)"', page)
    # initial user message (no step yet), S1 message, tool call (inherits S1), unknown marker (stays S1), S2
    assert "S1" in steps and steps[-2:] == ["S2", "S2"] or steps[-1] == "S2"
    assert "[step:" not in page.split('id="transcript"')[1].split('id="run-actions"')[0]
    assert '"S2"' in page  # current step handed to the highlight script

    with client.stream("GET", f"/runs/{run_id}/stream") as resp:
        body = "".join(resp.iter_text())
    assert 'data-step="S2"' in body


def test_runs_without_design_keep_the_predicted_plan(client, fake):
    from tests.conftest import start_run

    make_user("admin", admin=True)
    make_user("bob")
    login(client, "bob")
    run_id = start_run(client, "Do something")
    page = client.get(f"/runs/{run_id}").text
    assert 'id="run-map"' not in page and "Planned workflow" in page



def test_runs_started_before_snapshots_use_their_workflow_design(client, fake):
    make_user("admin", admin=True)
    uid = make_user("bob")
    wf_id = _workflow(uid)
    with SessionLocal() as db:
        run = WorkflowRun(user_id=uid, workflow_id=wf_id, status="completed", content_snapshot="x",
                          initial_message="x", pending_event_ids=[], design_snapshot=None)
        db.add(run)
        db.commit()
        run_id = run.id
    login(client, "bob")
    page = client.get(f"/runs/{run_id}").text
    assert 'id="run-map"' in page and page.count('class="ms-step"') == 3


def test_run_page_uses_the_swimlane_and_condenses_the_log(client, fake):
    make_user("admin", admin=True)
    uid = make_user("bob")
    wf_id = _workflow(uid)
    login(client, "bob")
    token = csrf(client, "/workflows")
    r = client.post(f"/workflows/{wf_id}/run", data={"csrf_token": token}, follow_redirects=False)
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    fake.stream_script["*"] = [
        {"type": "agent.message", "content": [{"type": "text", "text": "[step: S1]\nIssue #12 created."}]},
        {"type": "agent.mcp_tool_use", "name": "create_issue", "mcp_server_name": "github", "input": {},
         "evaluated_permission": "allow"},
        {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
    ]
    RunDriver(run_id, threading.Event()).run()
    page = client.get(f"/runs/{run_id}").text
    assert 'class="maestro-diagram"' in page and "maestro-map" not in page  # same columns as the design
    assert 'id="seg-dialog"' in page and "transcript condensed" in page
    items = re.findall(r'class="ev-item( is-detail)?" data-step="([^"]*)"', page)
    # the tool call goes to the detail window; the report stays in the log
    assert ("", "S1") in items and (" is-detail", "S1") in items


def test_runs_list_has_no_diagram(client, fake):
    make_user("admin", admin=True)
    uid = make_user("bob")
    wf_id = _workflow(uid)
    login(client, "bob")
    page = client.get(f"/workflows/{wf_id}/history").text
    assert "maestro-diagram" not in page and "maestro-map" not in page and "Planned workflow" not in page


def _run_with(client, fake, uid, wf_id, messages):  # type: ignore[no-untyped-def]
    token = csrf(client, "/workflows")
    r = client.post(f"/workflows/{wf_id}/run", data={"csrf_token": token}, follow_redirects=False)
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    fake.stream_script["*"] = [
        *({"type": "agent.message", "content": [{"type": "text", "text": m}]} for m in messages),
        {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
    ]
    RunDriver(run_id, threading.Event()).run()
    return run_id


def test_workflow_stays_ongoing_until_maestro_reports_it_done(client, fake):
    make_user("admin", admin=True)
    uid = make_user("bob")
    wf_id = _workflow(uid)
    login(client, "bob")
    first = _run_with(client, fake, uid, wf_id, ["[step: S2]\nDispatch is implementing issue #12.\n[workflow: ongoing]"])
    with SessionLocal() as db:
        run = db.get(WorkflowRun, first)
        assert run.status == "completed" and run.progress == "ongoing" and run.display_label == "ongoing"
    page = client.get(f"/runs/{first}").text
    assert 'badge ongoing' in page and "the workflow continues on the next run" in page
    assert "[workflow:" not in page.split('id="transcript"')[1].split('id="run-actions"')[0]
    assert "badge ongoing" in client.get("/workflows").text

    second = _run_with(client, fake, uid, wf_id, ["[step: S3]\nPR #13 merged. All steps are complete.\n[workflow: done]"])
    with SessionLocal() as db:
        assert db.get(WorkflowRun, second).progress == "done"
        older = db.get(WorkflowRun, first)
        assert older.progress == "continued" and older.display_label == f"continued in #{second}"
    history = client.get(f"/workflows/{wf_id}/history").text
    assert f"continued in #{second}" in history and "badge done" in history


def test_progress_without_marker_and_blocked_reports(client, fake):
    make_user("admin", admin=True)
    uid = make_user("bob")
    wf_id = _workflow(uid)
    login(client, "bob")
    # no marker, last design step reached: done; earlier step: ongoing
    run_id = _run_with(client, fake, uid, wf_id, ["[step: S2]\nWaiting for Dispatch."])
    with SessionLocal() as db:
        assert db.get(WorkflowRun, run_id).progress == "ongoing"
    run_id = _run_with(client, fake, uid, wf_id, ["[step: S3]\nMerged, finished."])
    with SessionLocal() as db:
        assert db.get(WorkflowRun, run_id).progress == "done"
    run_id = _run_with(client, fake, uid, wf_id, ["[step: S2]\nPR #13 was closed without merge.\n[workflow: blocked]"])
    with SessionLocal() as db:
        run = db.get(WorkflowRun, run_id)
        assert run.progress == "blocked" and run.display_label == "needs attention"
    # Maestro is told how to report the workflow status
    with SessionLocal() as db:
        assert "[workflow: ongoing]" in db.get(WorkflowRun, run_id).initial_message


def test_long_step_texts_can_be_read_in_full(client, fake):
    from app.maestro.render import render_design_svg

    long_detail = "Write the issue with the context, the acceptance criteria and the files to change. " * 2
    steps = [dict(STEPS[0], detail=long_detail.strip(), title="A very long step title " * 4)]
    svg = str(render_design_svg({"steps": steps}))
    assert "…" in svg  # clipped on the diagram
    assert "<title>" in svg and long_detail.strip() in svg  # full text in the tooltip

    make_user("admin", admin=True)
    uid = make_user("bob")
    wf_id = _workflow(uid)
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        wf.design_json = {**wf.design_json, "steps": [dict(STEPS[0], detail=long_detail.strip()), *STEPS[1:]]}
        db.commit()
    login(client, "bob")
    page = client.get(f"/workflows/{wf_id}/design").text
    assert 'id="step-dialog"' in page and "Click a step to read it in full." in page
    assert long_detail.strip() in page.split('id="step-dialog"')[1]  # step data handed to the dialog


def test_status_lines_count_as_workflow_markers():
    from app.runs.progress import workflow_marker

    assert workflow_marker("[step: S12]\nWorkflow: done\n\nThe chain is finished.") == "done"
    assert workflow_marker("[step: S5]\n**Workflow:** active\n\nDispatch is coding.") == "ongoing"
    assert workflow_marker("- Workflow status: blocked") == "blocked"
    assert workflow_marker("The workflow: done soon, maybe") is None  # not a status line
    assert workflow_marker("Workflow: active\n...\n[workflow: done]") == "done"


def test_finished_runs_are_closed_and_old_progress_is_refreshed(client, fake):
    from app.runs.progress import refresh_progress

    make_user("admin", admin=True)
    uid = make_user("bob")
    wf_id = _workflow(uid)
    login(client, "bob")
    run_id = _run_with(client, fake, uid, wf_id, ["[step: S2]\nWorkflow: done\n\nBoth PRs are merged."])
    with SessionLocal() as db:
        assert db.get(WorkflowRun, run_id).progress == "done"
        db.get(WorkflowRun, run_id).progress = "ongoing"  # as computed before status lines were read
        db.commit()
        assert refresh_progress(db) == 1 and db.get(WorkflowRun, run_id).progress == "done"

    page = client.get(f"/runs/{run_id}").text
    assert "Continue the conversation" not in page and f"/runs/{run_id}/message" not in page
    assert "Run finished." in page
    token = csrf(client, f"/runs/{run_id}")
    r = client.post(f"/runs/{run_id}/message", data={"text": "hi", "csrf_token": token})
    assert "This run has ended" in r.text
