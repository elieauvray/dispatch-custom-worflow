from __future__ import annotations

from app.db import SessionLocal
from app.models import Skill, Workflow, WorkflowRun
from app.workflows.compose import SKILLS_INTRO, compose_workflow_message
from tests.conftest import csrf, login, make_user


def test_skills_come_before_workflow_content():
    msg = compose_workflow_message(
        [{"name": "Ton", "content": "Sois concis."}, {"name": "Format", "content": "Réponds en Markdown."}],
        'Rapport "hebdo"',
        "Liste les tickets en retard.",
    )
    assert msg.index("<instructions>") < msg.index("</instructions>") < msg.index("<workflow")
    assert SKILLS_INTRO in msg
    assert msg.index("Sois concis.") < msg.index("Réponds en Markdown.")
    assert '<workflow name="Rapport &quot;hebdo&quot;">' in msg
    assert msg.rstrip().endswith("Liste les tickets en retard.\n</workflow>")


def test_no_skills_means_only_workflow_block():
    msg = compose_workflow_message([], "W", "Fais X")
    assert "<instructions>" not in msg
    assert msg == '<workflow name="W">\nFais X\n</workflow>'


def test_workflow_run_freezes_active_skills_in_order(client, fake):
    make_user("admin", admin=True)
    uid = make_user("bob")
    with SessionLocal() as db:
        db.add_all([
            Skill(name="Second", content="Deuxième consigne", position=2, active=True),
            Skill(name="Inactif", content="NE PAS ENVOYER", position=0, active=False),
            Skill(name="Premier", content="Première consigne", position=1, active=True),
        ])
        wf = Workflow(user_id=uid, name="Hebdo", content="Résume la semaine.")
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
        assert [s["name"] for s in run.skills_snapshot] == ["Premier", "Second"]
        msg = run.initial_message
        assert "NE PAS ENVOYER" not in msg
        assert msg.index("Première consigne") < msg.index("Deuxième consigne") < msg.index("Résume la semaine.")
        # Modifier le skill après coup ne change pas l'historique
        skill = db.query(Skill).filter_by(name="Premier").one()
        skill.content = "Modifiée"
        db.commit()
    with SessionLocal() as db:
        run = db.get(WorkflowRun, run_id)
        assert run.skills_snapshot[0]["content"] == "Première consigne"
    page = client.get(f"/runs/{run_id}").text
    assert "Première consigne" in page and "Modifiée" not in page
