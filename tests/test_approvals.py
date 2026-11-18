from __future__ import annotations

import threading

from app.db import SessionLocal
from app.models import WorkflowRun
from app.runs.approvals import is_read_only
from app.worker import RunDriver
from tests.conftest import login, make_user, start_run


def test_read_only_names():
    def mcp(name: str) -> dict:
        return {"name": name, "mcp_server_name": "linear"}

    for name in ("get_issue", "list_issues", "search_documents", "list_pull_requests", "whoami"):
        assert is_read_only(mcp(name)), name
    for name in ("create_issue", "update_issue", "add_issue_comment", "merge_pull_request", "get_or_create_label",
                 "save_issue"):
        assert not is_read_only(mcp(name)), name
    assert not is_read_only({"name": "get_file"})  # built-in sandbox tool: not a connector call


def _tool(eid: str, name: str) -> dict:
    return {"id": eid, "type": "agent.mcp_tool_use", "name": name, "mcp_server_name": "linear", "input": {},
            "evaluated_permission": "ask"}


def test_read_only_calls_are_approved_and_writes_still_wait(client, fake):
    make_user("admin", admin=True)
    make_user("bob")
    login(client, "bob")
    run_id = start_run(client, "Check ELI-34")
    fake.stream_script["*"] = [
        _tool("tu_read", "get_issue"),
        {"type": "session.status_idle", "stop_reason": {"type": "requires_action", "event_ids": ["tu_read"]}},
        {"type": "agent.message", "content": [{"type": "text", "text": "ELI-34 is open."}]},
        _tool("tu_write", "save_comment"),
        {"type": "session.status_idle", "stop_reason": {"type": "requires_action", "event_ids": ["tu_write"]}},
    ]
    RunDriver(run_id, threading.Event()).run()
    with SessionLocal() as db:
        run = db.get(WorkflowRun, run_id)
        assert run.status == "waiting_approval" and run.pending_event_ids == ["tu_write"]
    sent = [c for c in fake.calls if c[0] == "send_tool_confirmations"]
    assert sent and sent[0][1][1] == [("tu_read", True, None)]
