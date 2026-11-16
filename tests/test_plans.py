from __future__ import annotations

from app.db import SessionLocal
from app.models import Connector, ConnectorStatus, Workflow, WorkflowRun
from app.plans.render import render_plan_svg
from app.plans.service import normalize
from tests.conftest import csrf, login, make_user


def _connect(user_id: int, provider: str, policy: str) -> None:
    with SessionLocal() as db:
        db.add(Connector(user_id=user_id, provider=provider, mcp_server_url=f"https://{provider}.test/mcp",
                         auth_type="static_bearer", credential_id=f"cred_{provider}", last4="abcd",
                         permission_policy=policy, status=ConnectorStatus.CONFIGURED))
        db.commit()


def _prompt_run(client, text: str) -> int:
    token = csrf(client, "/prompt")
    r = client.post("/prompt", data={"prompt": text, "csrf_token": token}, follow_redirects=False)
    return int(r.headers["location"].rsplit("/", 1)[1])


def test_approvals_and_missing_connectors_are_computed_by_the_app():
    raw = {"summary": "x", "steps": [
        {"title": "Read issues", "detail": "", "kind": "read", "service": "linear"},
        {"title": "Open a PR", "detail": "", "kind": "action", "service": "github"},
        {"title": "Comment", "detail": "", "kind": "action", "service": "atlassian"},
        {"title": "Think", "detail": "", "kind": "reasoning", "service": "none"},
    ]}
    plan = normalize(raw, {"linear": "always_ask", "github": "always_allow"})
    steps = plan["steps"]
    assert steps[0]["needs_approval"] is True        # Linear on "Always ask"
    assert steps[1]["needs_approval"] is False       # GitHub on "Always allow"
    assert steps[2]["missing_connector"] is True     # Atlassian not connected
    assert steps[-1]["kind"] == "result"             # a result step is always added


def test_svg_escapes_text_and_shows_interventions():
    plan = normalize({"summary": "<b>s</b>", "steps": [
        {"title": "<script>alert(1)</script>", "detail": "", "kind": "read", "service": "linear"},
        {"title": "Which team?", "detail": "", "kind": "question", "service": "none"},
    ]}, {"linear": "always_ask"})
    svg = str(render_plan_svg(plan))
    assert "<script>" not in svg and "&lt;script&gt;" in svg
    assert "Your approval" in svg and "Your reply" in svg
    assert svg.startswith("<svg") and 'role="img"' in svg


def test_prompt_run_shows_planned_workflow_once(client, fake):
    make_user("admin", admin=True)
    uid = make_user("bob")
    _connect(uid, "linear", "always_ask")
    login(client, "bob")
    run_id = _prompt_run(client, "Comment on one of my Linear issues")
    page = client.get(f"/runs/{run_id}").text
    assert f'hx-get="/runs/{run_id}/plan"' in page and "Planned workflow" in page

    frag = client.get(f"/runs/{run_id}/plan").text
    assert "<svg" in frag and "List my recent issues" in frag and "Your approval" in frag
    model, request_text, services = fake.plan_calls[0]
    assert request_text == "Comment on one of my Linear issues" and services == ["linear"]

    client.get(f"/runs/{run_id}/plan")  # cached: no second call
    assert len(fake.plan_calls) == 1
    token = csrf(client, f"/runs/{run_id}")
    client.post(f"/runs/{run_id}/plan/regenerate", data={"csrf_token": token})
    assert len(fake.plan_calls) == 2

    r = client.get(f"/runs/{run_id}/plan.svg")
    assert r.headers["content-type"].startswith("image/svg+xml") and "attachment" in r.headers["content-disposition"]


def test_plan_failure_is_shown_with_retry(client, fake):
    make_user("admin", admin=True)
    make_user("bob")
    login(client, "bob")
    run_id = _prompt_run(client, "Do something")
    fake.plan_error = "Claude declined to plan this request."
    frag = client.get(f"/runs/{run_id}/plan").text
    assert "could not be generated" in frag and "Try again" in frag
    client.get(f"/runs/{run_id}/plan")  # no automatic retry loop
    assert len(fake.plan_calls) == 1


def test_workflow_plan_is_reused_by_its_runs_and_follows_edits(client, fake):
    make_user("admin", admin=True)
    uid = make_user("bob")
    with SessionLocal() as db:
        wf = Workflow(user_id=uid, name="Weekly", content="Summarize my week.")
        db.add(wf)
        db.commit()
        wf_id = wf.id
    login(client, "bob")
    assert "<svg" in client.get(f"/workflows/{wf_id}/plan").text
    assert len(fake.plan_calls) == 1

    token = csrf(client, "/workflows")
    r = client.post(f"/workflows/{wf_id}/run", data={"csrf_token": token}, follow_redirects=False)
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert "<svg" in client.get(f"/runs/{run_id}/plan").text
    assert len(fake.plan_calls) == 1  # reused, same request text
    with SessionLocal() as db:
        assert db.get(WorkflowRun, run_id).plan_status == "ready"

    client.post(f"/workflows/{wf_id}/edit", data={"name": "Weekly", "content": "Summarize my month.",
                                                  "csrf_token": token})
    client.get(f"/workflows/{wf_id}/plan")
    assert len(fake.plan_calls) == 2  # content changed: new plan


def test_other_users_cannot_see_a_plan(client, fake):
    make_user("admin", admin=True)
    alice = make_user("alice")
    make_user("bob")
    with SessionLocal() as db:
        wf = Workflow(user_id=alice, name="Private", content="x")
        db.add(wf)
        db.commit()
        wf_id = wf.id
    login(client, "bob")
    assert client.get(f"/workflows/{wf_id}/plan").status_code == 404
    assert client.get(f"/workflows/{wf_id}/plan.svg").status_code == 404
    assert fake.plan_calls == []
