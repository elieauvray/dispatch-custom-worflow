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

READ_ONLY = re.compile(
    r"^(get|list|search|read|fetch|find|query|view|show|describe|lookup|check|count|whoami)(_|$)",
    re.IGNORECASE,
)
# A read-looking name that still changes something (e.g. "get_or_create_label") is never auto-approved.
WRITE_HINT = re.compile(
    r"(create|update|delete|remove|add|set|edit|write|post|comment|merge|close|assign|move|archive|"
    r"upload|send|publish|approve|transition|link|label)",
    re.IGNORECASE,
)


def is_read_only(tool: dict[str, Any]) -> bool:
    """Only MCP tools (connectors), never the sandbox's built-in tools."""
    name = str(tool.get("name") or "")
    if not tool.get("mcp_server_name") or not READ_ONLY.match(name):
        return False
    rest = READ_ONLY.sub("", name, count=1)
    return not WRITE_HINT.search(rest)


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
