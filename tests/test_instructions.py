from __future__ import annotations

from app.db import SessionLocal
from app.models import Skill, Workflow
from tests.conftest import csrf, login
from tests.test_maestro import _create, _design, _setup

RULE = "Every pull request opened by Dispatch goes through a Dispatch Code review before a person merges it."


def _add_instruction(name: str, content: str) -> int:
    with SessionLocal() as db:
        s = Skill(name=name, content=content, position=1, active=True)
        db.add(s)
        db.commit()
        return s.id


def test_system_instructions_take_precedence_in_the_design(client, fake):
    _setup(client)
    _add_instruction("Review gate", RULE)
    fake.design_adjustments = [{"instruction": "Review gate", "change": "Added a Dispatch Code review before each merge."}]
    wf_id = _create(client)
    _design(wf_id)
    # tool check, clarify and designer all receive the instructions, with the precedence rule
    seen = dict(fake.instructions_seen)
    for stage in ("tools", "clarify", "design"):
        assert RULE in seen[stage] and "take precedence" in seen[stage], stage
    page = client.get(f"/workflows/{wf_id}/design").text
    assert "Adjusted to system instructions" in page and "Added a Dispatch Code review" in page
    assert "instructions changed" not in client.get("/workflows").text


def test_changing_instructions_marks_designs_outdated_until_regenerated(client, fake):
    _setup(client)
    wf_id = _create(client)
    _design(wf_id)
    assert "instructions changed" not in client.get("/workflows").text
    _add_instruction("Language", "Write every issue in English.")
    assert "instructions changed" in client.get("/workflows").text
    page = client.get(f"/workflows/{wf_id}/design").text
    assert "The system instructions changed since this design was generated" in page

    token = csrf(client, f"/workflows/{wf_id}/design")
    client.post(f"/workflows/{wf_id}/design/revise", data={"csrf_token": token})
    _design(wf_id)
    with SessionLocal() as db:
        assert db.get(Workflow, wf_id).instructions_hash
    assert "instructions changed" not in client.get("/workflows").text


def test_users_read_instructions_and_only_admins_edit_them(client, fake):
    _setup(client)
    _add_instruction("Language", "Write every issue in English.")
    page = client.get("/instructions").text
    assert "System instructions" in page and "Write every issue in English." in page
    assert "New system instruction" not in page and "/delete" not in page
    assert client.get("/admin/instructions").status_code == 403

    client.post("/logout", data={"csrf_token": csrf(client, "/workflows")})
    login(client, "admin")
    assert client.get("/instructions", follow_redirects=False).headers["location"] == "/admin/instructions"
    assert client.get("/admin/skills", follow_redirects=False).headers["location"] == "/admin/instructions"
    admin_page = client.get("/admin/instructions").text
    assert "New system instruction" in admin_page and "<strong>precedence</strong>" in admin_page
