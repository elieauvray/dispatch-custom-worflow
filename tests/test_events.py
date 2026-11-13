from __future__ import annotations

import threading

from app.db import SessionLocal
from app.models import RunEvent, RunStatus, WorkflowRun
from app.runs import routes as runs_routes
from app.runs.events import apply_event, is_question
from app.worker import RunDriver, Worker
from tests.conftest import csrf, login, make_user


def _run(**kw) -> WorkflowRun:
    base = {"user_id": 1, "status": RunStatus.RUNNING, "content_snapshot": "x",
            "initial_message": "x", "pending_event_ids": []}
    return WorkflowRun(**{**base, **kw})


def test_is_question():
    assert is_question("Voici l'analyse.\n\nQuelle équipe dois-je cibler ?")
    assert is_question("Tu veux que je crée le ticket **?**")
    assert not is_question("Terminé. Le ticket ENG-12 est créé.")
    assert not is_question("Est-ce utile ? Voici le résultat final.")
    assert not is_question("")


def test_requires_action_sets_waiting_approval():
    run = _run()
    apply_event(run, {"type": "session.status_idle",
                      "stop_reason": {"type": "requires_action", "event_ids": ["sevt_1", "sevt_2"]}}, None)
    assert run.status == RunStatus.WAITING_APPROVAL
    assert run.pending_event_ids == ["sevt_1", "sevt_2"]
    apply_event(run, {"type": "user.tool_confirmation", "tool_use_id": "sevt_1", "result": "allow"}, None)
    assert run.status == RunStatus.WAITING_APPROVAL and run.pending_event_ids == ["sevt_2"]
    apply_event(run, {"type": "user.tool_confirmation", "tool_use_id": "sevt_2", "result": "deny"}, None)
    assert run.status == RunStatus.RUNNING and run.pending_event_ids == []


def test_end_turn_with_question_waits_for_answer():
    run = _run()
    apply_event(run, {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
                "Sur quel projet Linear dois-je travailler ?")
    assert run.status == RunStatus.WAITING_INPUT and run.finished_at is None


def test_end_turn_without_question_completes():
    run = _run()
    apply_event(run, {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}}, "C'est fait.")
    assert run.status == RunStatus.COMPLETED and run.finished_at is not None
    # L'utilisateur relance la conversation
    apply_event(run, {"type": "user.message", "content": []}, None)
    assert run.status == RunStatus.RUNNING


def test_cancelled_is_sticky_and_errors_are_recorded():
    run = _run(status=RunStatus.CANCELLED)
    apply_event(run, {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}}, "ok")
    assert run.status == RunStatus.CANCELLED
    run = _run()
    apply_event(run, {"type": "session.error", "error": {
        "type": "mcp_authentication_failed_error", "mcp_server_name": "linear", "message": "401"}}, None)
    assert "mcp_authentication_failed_error" in run.error and "linear" in run.error
    apply_event(run, {"type": "session.status_idle", "stop_reason": {"type": "retries_exhausted"}}, None)
    assert run.status == RunStatus.FAILED


def _prompt_run(client, text: str) -> int:
    token = csrf(client, "/prompt")
    r = client.post("/prompt", data={"prompt": text, "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    return int(r.headers["location"].rsplit("/", 1)[1])


def _drive(run_id: int) -> None:
    RunDriver(run_id, threading.Event()).run()


def test_worker_full_flow_with_tool_approval_and_question(client, fake):
    make_user("admin", admin=True)
    make_user("bob")
    login(client, "bob")
    run_id = _prompt_run(client, "Crée un ticket Linear pour le bug de login")

    tool_use = {"id": "sevt_tool", "type": "agent.mcp_tool_use", "name": "create_issue",
                "mcp_server_name": "linear", "input": {"title": "Bug de login"},
                "evaluated_permission": "ask"}
    fake.stream_script["*"] = [
        {"type": "session.status_running"},
        {"type": "agent.message", "content": [{"type": "text", "text": "Je crée le ticket."}]},
        tool_use,
        {"type": "session.status_idle",
         "stop_reason": {"type": "requires_action", "event_ids": ["sevt_tool"]}},
    ]
    _drive(run_id)
    with SessionLocal() as db:
        run = db.get(WorkflowRun, run_id)
        assert run.session_id and run.initial_sent
        assert ("send_user_message", (run.session_id, "Crée un ticket Linear pour le bug de login")) in fake.calls
        tool_event = db.query(RunEvent).filter_by(run_id=run_id, type="agent.mcp_tool_use").one()
        sid = run.session_id
        assert run.status == RunStatus.WAITING_APPROVAL
        assert run.pending_event_ids == ["sevt_tool"]

    page = client.get(f"/runs/{run_id}").text
    assert "Claude wants to run" in page and "create_issue" in page and "Bug de login" in page

    token = csrf(client, f"/runs/{run_id}")
    r = client.post(f"/runs/{run_id}/confirm", headers={"HX-Request": "true"}, data={
        "event_id": tool_event.anthropic_event_id, "decision": "deny",
        "deny_message": "Utilise le projet Web", "csrf_token": token})
    assert r.status_code == 200
    decisions = [d for c, d in fake.calls if c == "send_tool_confirmations"][-1]
    assert decisions == (sid, [(tool_event.anthropic_event_id, False, "Utilise le projet Web")])
    with SessionLocal() as db:
        assert db.get(WorkflowRun, run_id).status == RunStatus.RUNNING

    # Claude pose une question et termine son tour
    fake.stream_script[sid] = [
        {"type": "user.tool_confirmation", "tool_use_id": tool_event.anthropic_event_id, "result": "deny"},
        {"type": "agent.message", "content": [{"type": "text", "text": "Dans quelle équipe ?"}]},
        {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
    ]
    _drive(run_id)
    with SessionLocal() as db:
        assert db.get(WorkflowRun, run_id).status == RunStatus.WAITING_INPUT
    page = client.get(f"/runs/{run_id}").text
    assert "Claude is waiting for your reply" in page

    r = client.post(f"/runs/{run_id}/message", data={"text": "Équipe Web", "csrf_token": token},
                    follow_redirects=False)
    assert ("send_user_message", (sid, "Équipe Web")) in fake.calls
    fake.stream_script[sid] = [
        {"type": "agent.message", "content": [{"type": "text", "text": "Ticket WEB-42 créé."}]},
        {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
    ]
    _drive(run_id)
    with SessionLocal() as db:
        run = db.get(WorkflowRun, run_id)
        assert run.status == RunStatus.COMPLETED
        # Pas de doublon d'événements malgré les relectures d'historique
        ids = [e.anthropic_event_id for e in db.query(RunEvent).filter_by(run_id=run_id)]
        assert len(ids) == len(set(ids))
        # Le deuxième message utilisateur n'a pas été renvoyé par le worker
        assert sum(1 for c, _ in fake.calls if c == "send_user_message") == 2

    # Enregistrer comme workflow
    r = client.post(f"/runs/{run_id}/save-workflow", data={"name": "Ticket bug", "csrf_token": token},
                    follow_redirects=False)
    assert r.headers["location"] == "/workflows"
    page = client.get("/workflows").text
    assert "Ticket bug" in page
    r = client.post(f"/runs/{run_id}/save-workflow", data={"name": "Ticket bug", "csrf_token": token})
    assert "already have a workflow" in r.text


def test_cancel_interrupts_session(client, fake):
    from app.runs.events import store_event

    make_user("admin", admin=True)
    make_user("bob")
    login(client, "bob")
    run_id = _prompt_run(client, "Longue tâche")
    fake.stream_script["*"] = [
        {"type": "session.status_running"},
        {"type": "agent.message", "content": [{"type": "text", "text": "Je continue ?"}]},
        {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
    ]
    _drive(run_id)
    token = csrf(client, f"/runs/{run_id}")
    client.post(f"/runs/{run_id}/cancel", data={"csrf_token": token})
    with SessionLocal() as db:
        run = db.get(WorkflowRun, run_id)
        assert run.status == RunStatus.CANCELLED and run.finished_at is not None
        assert ("interrupt", run.session_id) in fake.calls
        # Les événements arrivant après l'annulation ne changent plus le statut
        store_event(db, run, {"id": "sevt_late", "type": "session.status_running"})
        db.commit()
        assert db.get(WorkflowRun, run_id).status == RunStatus.CANCELLED
    assert "Run canceled" in client.get(f"/runs/{run_id}").text


def test_worker_resumes_stale_claims(client, fake):
    make_user("admin", admin=True)
    make_user("bob")
    login(client, "bob")
    run_id = _prompt_run(client, "Reprends-moi")
    from datetime import timedelta

    from app.models import utcnow

    with SessionLocal() as db:
        run = db.get(WorkflowRun, run_id)
        run.claimed_by = "ancien-worker"
        run.heartbeat_at = utcnow() - timedelta(minutes=5)
        db.commit()
    w = Worker()
    with SessionLocal() as db:
        assert w.claim(db) == [run_id]
    with SessionLocal() as db:
        assert db.get(WorkflowRun, run_id).claimed_by == w.worker_id


def test_sse_stream_replays_stored_events(client, fake, monkeypatch):
    monkeypatch.setattr(runs_routes, "SSE_MAX_SECONDS", 1)
    make_user("admin", admin=True)
    make_user("bob")
    login(client, "bob")
    run_id = _prompt_run(client, "Bonjour")
    fake.stream_script["*"] = [
        {"type": "agent.message", "content": [{"type": "text", "text": "Salut **Bob**"}]},
        {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
    ]
    _drive(run_id)
    with client.stream("GET", f"/runs/{run_id}/stream") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert "event: transcript" in body and "<strong>Bob</strong>" in body
    assert "event: actions" in body and "event: status" in body
