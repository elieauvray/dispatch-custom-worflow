from __future__ import annotations

from app.db import SessionLocal
from app.maestro.diagram import DIAGRAM_ONLY_DESCRIPTION, detect_media_type, with_diagram
from app.models import Workflow
from tests.conftest import csrf, login, make_user
from tests.test_maestro import _design, _setup

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _post(client, files, description=""):  # type: ignore[no-untyped-def]
    token = csrf(client, "/maestro/new")
    return client.post("/maestro/new", data={"name": "From a diagram", "description": description, "csrf_token": token},
                       files=files, follow_redirects=False)


def test_a_diagram_alone_is_enough_and_reaches_every_design_call(client, fake):
    _setup(client)
    r = _post(client, {"diagram": ("pasted-diagram.png", PNG, "image/png")})
    assert r.status_code == 303
    wf_id = int(r.headers["location"].split("/")[2])
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert wf.description == DIAGRAM_ONLY_DESCRIPTION and wf.diagram_media_type == "image/png"
    _design(wf_id)
    stages = {stage: d for stage, d in fake.diagrams_seen}
    for stage in ("tools", "clarify", "design"):
        assert stages[stage]["media_type"] == "image/png" and stages[stage]["data"] == PNG, stage
    page = client.get(f"/workflows/{wf_id}/design").text
    assert f'href="/workflows/{wf_id}/edit"' in page and "Revise the design" not in page
    edit = client.get(f"/workflows/{wf_id}/edit").text
    assert "Current diagram" in edit and f'src="/workflows/{wf_id}/diagram"' in edit
    img = client.get(f"/workflows/{wf_id}/diagram")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png" and img.content == PNG


def test_invalid_or_missing_input_is_refused(client, fake):
    _setup(client)
    r = _post(client, {"diagram": ("evil.png", b"<script>alert(1)</script>", "image/png")}, "x" * 50)
    assert r.status_code == 400 and "must be a PNG, JPEG, GIF or WebP image, or a PDF" in r.text
    r = _post(client, {"diagram": ("big.png", PNG + b"\x00" * 3_800_000, "image/png")}, "x" * 50)
    assert r.status_code == 400 and "too large" in r.text
    r = _post(client, {}, "too short")
    assert r.status_code == 400 and "or add a diagram" in r.text


def test_only_the_owner_sees_the_diagram(client, fake):
    _setup(client)
    r = _post(client, {"diagram": ("d.png", PNG, "image/png")})
    wf_id = int(r.headers["location"].split("/")[2])
    client.post("/logout", data={"csrf_token": csrf(client, "/workflows")})
    make_user("eve")
    login(client, "eve")
    assert client.get(f"/workflows/{wf_id}/diagram").status_code == 404


def test_diagram_content_blocks():
    assert detect_media_type(b"%PDF-1.7 ...") == "application/pdf"
    assert detect_media_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    blocks = with_diagram("text", {"media_type": "application/pdf", "data": b"%PDF-1.7"})
    assert blocks[0]["type"] == "document" and blocks[1]["type"] == "text" and blocks[1]["text"].endswith("text")
    assert with_diagram("text", None) == "text"


def test_text_overrides_the_diagram_in_every_prompt():
    from app.maestro.diagram import with_diagram
    from app.maestro.knowledge import TOOLS_SYSTEM

    content = with_diagram("Replace jira with linear", {"media_type": "image/png", "data": b"\x89PNG"})
    note = content[1]["text"]
    assert "takes precedence over the diagram" in note and note.endswith("Replace jira with linear")
    assert "A replaced tool is neither needed nor unsupported" in TOOLS_SYSTEM
    assert "comment that mentions them in the tracker" in TOOLS_SYSTEM


def test_edit_replaces_or_removes_the_diagram_and_redesigns(client, fake):
    from tests.conftest import csrf

    _setup(client)
    r = _post(client, {"diagram": ("d.png", PNG, "image/png")})
    wf_id = int(r.headers["location"].split("/")[2])
    _design(wf_id)
    token = csrf(client, f"/workflows/{wf_id}/edit")
    pdf = b"%PDF-1.4\n%fake"
    r = client.post(f"/workflows/{wf_id}/edit", data={"name": "Diagram flow", "description": DIAGRAM_ONLY_DESCRIPTION,
                                                      "csrf_token": token},
                    files={"diagram": ("new.pdf", pdf, "application/pdf")}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert wf.diagram_media_type == "application/pdf" and wf.diagram_data == pdf and wf.design_status == "queued"
    _design(wf_id)
    assert fake.diagrams_seen[-1][1]["media_type"] == "application/pdf"

    # Removing the only input is refused; removing it with a real description works.
    token = csrf(client, f"/workflows/{wf_id}/edit")
    r = client.post(f"/workflows/{wf_id}/edit", data={"name": "Diagram flow", "description": DIAGRAM_ONLY_DESCRIPTION,
                                                      "remove_diagram": "1", "csrf_token": token})
    assert r.status_code == 400 and "or add a diagram" in r.text
    text = "Fix bugs: Maestro files each bug as a GitHub issue and Dispatch implements it, the lead merges."
    client.post(f"/workflows/{wf_id}/edit", data={"name": "Diagram flow", "description": text,
                                                  "remove_diagram": "1", "csrf_token": token})
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert wf.diagram_data is None and wf.description == text and wf.design_status == "queued"


def test_edit_change_request_is_sent_as_feedback(client, fake):
    from tests.conftest import csrf

    _setup(client)
    r = _post(client, {"diagram": ("d.png", PNG, "image/png")})
    wf_id = int(r.headers["location"].split("/")[2])
    _design(wf_id)
    token = csrf(client, f"/workflows/{wf_id}/edit")
    client.post(f"/workflows/{wf_id}/edit", data={"name": "Diagram flow", "description": DIAGRAM_ONLY_DESCRIPTION,
                                                  "feedback": "Use Linear", "csrf_token": token})
    _design(wf_id)
    assert fake.design_calls[-1]["feedback"] == "Use Linear"
