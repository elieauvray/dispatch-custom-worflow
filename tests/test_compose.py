from __future__ import annotations

from app.db import SessionLocal
from app.models import Skill, Workflow, WorkflowRun
from app.workflows.compose import SKILLS_INTRO, compose_workflow_message
from tests.conftest import csrf, login, make_user


def test_skills_come_before_workflow_content():
    msg = compose_workflow_message(
        [{"name": "Tone", "content": "Be concise."}, {"name": "Format", "content": "Answer in Markdown."}],
        'Report "weekly"',
        "List the overdue tickets.",
    )
    assert msg.index("<instructions>") < msg.index("</instructions>") < msg.index("<workflow")
    assert SKILLS_INTRO in msg
    assert msg.index("Be concise.") < msg.index("Answer in Markdown.")
    assert '<workflow name="Report &quot;weekly&quot;">' in msg
    assert msg.rstrip().endswith("List the overdue tickets.\n</workflow>")


def test_no_skills_means_only_workflow_block():
    msg = compose_workflow_message([], "W", "Do X")
    assert "<instructions>" not in msg
    assert msg == '<workflow name="W">\nDo X\n</workflow>'


def test_workflow_run_freezes_active_skills_in_order(client, fake):
    make_user("admin", admin=True)
    uid = make_user("bob")
    with SessionLocal() as db:
        db.add_all([
            Skill(name="Second", content="Second instruction", position=2, active=True),
            Skill(name="Inactive", content="DO NOT SEND", position=0, active=False),
            Skill(name="First", content="First instruction", position=1, active=True),
        ])
        wf = Workflow(user_id=uid, name="Weekly", content="Summarize the week.")
        db.add(wf)
        db.commit()
        wf_id = wf.id
    login(client, "bob")
    token = csrf(client, "/workflows")
    r = client.post(f"/workflows/{wf_id}/run", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/runs/")
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    with SessionLocal() as db:
        run = db.get(WorkflowRun, run_id)
        assert run is not None
        assert [s["name"] for s in run.skills_snapshot] == ["First", "Second"]
        msg = run.initial_message
        assert "DO NOT SEND" not in msg
        assert msg.index("First instruction") < msg.index("Second instruction") < msg.index("Summarize the week.")
        # Editing the skill afterward does not change the history
        skill = db.query(Skill).filter_by(name="First").one()
        skill.content = "Edited"
        db.commit()
    with SessionLocal() as db:
        run = db.get(WorkflowRun, run_id)
        assert run.skills_snapshot[0]["content"] == "First instruction"
    page = client.get(f"/runs/{run_id}").text
    assert "First instruction" in page and "Edited" not in page
