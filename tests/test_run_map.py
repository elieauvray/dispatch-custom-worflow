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
