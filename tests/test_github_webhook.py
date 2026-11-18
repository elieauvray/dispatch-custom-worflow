from __future__ import annotations

import hashlib
import hmac
import json
import re

from app.db import SessionLocal
from app.models import Connector, ConnectorStatus, UserSettings, Workflow, WorkflowRun
from tests.conftest import csrf, login, make_user


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _merged(repo: str, merged: bool = True) -> bytes:
    return json.dumps({"action": "closed", "repository": {"full_name": repo},
                       "pull_request": {"number": 12, "merged": merged}}).encode()


def _setup(client):  # type: ignore[no-untyped-def]
    make_user("admin", admin=True)
    uid = make_user("bob")
    with SessionLocal() as db:
        db.add(Connector(user_id=uid, provider="github", mcp_server_url="https://github.test/mcp",
                         auth_type="static_bearer", credential_id="cred_gh", last4="abcd",
                         permission_policy="auto", status=ConnectorStatus.CONFIGURED))
        design = {"summary": "s", "steps": [], "connectors_needed": ["github"],
                  "config": [{"key": "REPO", "description": "repo", "value": "Acme/Shop"}]}
        scheduled = Workflow(user_id=uid, name="By config", description="d" * 50, content="# Mission",
                             design_json=design, design_status="ready")
        manual = Workflow(user_id=uid, name="Manual", description="Work on acme/shop", content="# Mission",
                          design_json={**design, "config": []}, design_status="ready")
        other = Workflow(user_id=uid, name="Other", description="Work on acme/blog", content="# Mission",
                         design_json={**design, "config": []}, design_status="ready")
        db.add_all([scheduled, manual, other])
        db.commit()
        ids = (scheduled.id, manual.id, other.id)
    login(client, "bob")
    token = csrf(client, "/settings")
    r = client.post("/settings/github-webhook", data={"csrf_token": token}, follow_redirects=False)
    assert r.headers["location"] == "/settings#github-webhook"
    page = client.get("/settings").text
    url = re.search(r'value="(https://testserver/webhooks/github/[^"]+)"', page).group(1)
    secret = re.search(r'Secret \(shown once\) <input value="([^"]+)"', page).group(1)
    assert "Secret (shown once)" not in client.get("/settings").text  # never shown again
    with SessionLocal() as db:
        s = db.get(UserSettings, uid)
        assert s.github_hook_secret_encrypted and secret not in s.github_hook_secret_encrypted
    return url.replace("https://testserver", ""), secret, ids


def test_merged_pr_wakes_the_workflows_of_that_repo(client, fake):
    path, secret, (scheduled, manual, other) = _setup(client)
    body = _merged("acme/shop")
    r = client.post(path, content=body, headers={"X-GitHub-Event": "pull_request",
                                                 "X-Hub-Signature-256": _sign(secret, body)})
    assert r.status_code == 202 and sorted(r.json()["woken_workflows"]) == sorted([scheduled, manual])
    with SessionLocal() as db:
        for wf_id in (scheduled, manual):  # found by its config value or by its description
            runs = db.query(WorkflowRun).filter_by(workflow_id=wf_id).all()
            assert len(runs) == 1 and runs[0].trigger == "wake"
        assert db.query(WorkflowRun).filter_by(workflow_id=other).count() == 0

    # A second merge right after does not wake them again
    r = client.post(path, content=body, headers={"X-GitHub-Event": "pull_request",
                                                 "X-Hub-Signature-256": _sign(secret, body)})
    assert r.json()["woken_workflows"] == []


def test_webhook_security_and_ignored_events(client, fake):
    path, secret, _ = _setup(client)
    body = _merged("acme/shop")
    assert client.post(path, content=body, headers={"X-GitHub-Event": "pull_request",
                                                    "X-Hub-Signature-256": "sha256=bad"}).status_code == 401
    assert client.post("/webhooks/github/unknown", content=body,
                       headers={"X-Hub-Signature-256": _sign(secret, body)}).status_code == 404
    ping = b'{"zen": "hi"}'
    assert client.post(path, content=ping, headers={"X-GitHub-Event": "ping",
                                                    "X-Hub-Signature-256": _sign(secret, ping)}).status_code == 200
    closed = _merged("acme/shop", merged=False)
    r = client.post(path, content=closed, headers={"X-GitHub-Event": "pull_request",
                                                   "X-Hub-Signature-256": _sign(secret, closed)})
    assert "ignored" in r.json()

