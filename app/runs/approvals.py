"""Automatic approval of read-only tool calls.

With the "Always ask" policy, Managed Agents asks for every MCP call, reads included. Reading an issue
or listing pull requests changes nothing, so the worker approves those calls itself and only the calls
that change something (create, update, comment, merge...) wait for the user.

Managed Agents only takes per-tool permissions by exact tool name, and each connector names its tools
its own way, so read-only calls are recognized by their name.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RunEvent, RunStatus, WorkflowRun

READ_VERBS = frozenset({
    "get", "list", "search", "read", "fetch", "find", "query", "view", "show", "describe", "lookup", "check",
    "count", "whoami",
})
# A read-looking name that still changes something (e.g. "get_or_create_label") is never auto-approved.
# Matched on whole words only: "list_comments" or "get_issue_labels" are reads, the nouns must not count.
# Words that are mostly nouns in tool names (comment, post, label, link) are left out for the same reason.
WRITE_VERBS = frozenset({
    "create", "update", "upsert", "delete", "remove", "add", "set", "edit", "write", "save", "merge", "close",
    "assign", "unassign", "move", "archive", "upload", "send", "publish", "approve", "transition", "patch",
    "put", "insert", "mark", "reopen", "resolve", "submit", "share", "trigger", "execute", "or",
})
WORDS = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")


def is_read_only(tool: dict[str, Any]) -> bool:
    """Only MCP tools (connectors), never the sandbox's built-in tools."""
    name = str(tool.get("name") or "")
    words = [w.lower() for w in WORDS.findall(name)]
    if not tool.get("mcp_server_name") or not words or words[0] not in READ_VERBS:
        return False
    return not any(w in WRITE_VERBS for w in words[1:])


def auto_approve(db: Session, client: Any, run: WorkflowRun) -> bool:
    """Approve the pending read-only calls. Returns True when nothing is left for the user."""
    pending = list(run.pending_event_ids or [])
    if run.status != RunStatus.WAITING_APPROVAL or not run.session_id or not pending:
        return False
    rows = db.scalars(
        select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.anthropic_event_id.in_(pending))
    )
    reads = [r.anthropic_event_id for r in rows if is_read_only(r.payload_json)]
    if not reads:
        return False
    client.send_tool_confirmations(run.session_id, [(eid, True, None) for eid in reads])
    remaining = [e for e in pending if e not in reads]
    run.pending_event_ids = remaining
    if not remaining:
        run.status = RunStatus.RUNNING  # the session goes on by itself
        run.stop_reason = None
    db.commit()
    return not remaining
