"""Traitement des événements Managed Agents : stockage et machine à états d'une exécution."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Connector, ConnectorStatus, RunEvent, RunStatus, WorkflowRun, utcnow

# Événements sans intérêt pour la transcription (on ne les stocke pas).
IGNORED_TYPES = {"agent.thinking", "event_start", "event_delta", "span.model_request_start"}

TERMINAL = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}


def event_key(ev: dict[str, Any]) -> str:
    eid = ev.get("id")
    if eid:
        return str(eid)
    # Certains événements (ex. user.interrupt) peuvent avoir un id vide.
    digest = hashlib.sha256(json.dumps(ev, sort_keys=True, default=str).encode()).hexdigest()
    return f"local_{digest[:40]}"


def agent_text(ev: dict[str, Any]) -> str:
    return "".join(
        b.get("text", "") for b in ev.get("content", []) or [] if b.get("type") == "text"
    )


_TRAILING = re.compile(r"[\s*_`)\]»\"']+$")


def is_question(text: str | None) -> bool:
    """Heuristique : le dernier paragraphe non vide se termine par un point d'interrogation."""
    if not text:
        return False
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return False
    return _TRAILING.sub("", lines[-1]).endswith("?")


def error_message(ev: dict[str, Any]) -> str:
    err = ev.get("error") or {}
    etype = err.get("type", "error")
    msg = err.get("message") or ""
    server = err.get("mcp_server_name")
    prefix = f"{etype}" + (f" ({server})" if server else "")
    return f"{prefix} : {msg}".strip(" :")


def last_agent_text(db: Session, run: WorkflowRun) -> str | None:
    ev = db.scalar(
        select(RunEvent)
        .where(RunEvent.run_id == run.id, RunEvent.type == "agent.message")
        .order_by(RunEvent.id.desc())
        .limit(1)
    )
    return agent_text(ev.payload_json) if ev else None


def apply_event(run: WorkflowRun, ev: dict[str, Any], last_text: str | None) -> None:
    """Met à jour le statut de l'exécution à partir d'un nouvel événement."""
    etype = ev.get("type")
    if etype == "session.error":
        run.error = error_message(ev)
    if run.status == RunStatus.CANCELLED:
        return
    if etype == "session.status_running":
        run.status = RunStatus.RUNNING
        run.stop_reason = None
        run.pending_event_ids = []
        run.finished_at = None
    elif etype in {"user.message", "user.tool_confirmation"}:
        if etype == "user.tool_confirmation":
            pending = [p for p in run.pending_event_ids or [] if p != ev.get("tool_use_id")]
            run.pending_event_ids = pending
            if pending:
                return
        # Un message ou une validation relance la session.
        if run.status != RunStatus.FAILED:
            run.status = RunStatus.RUNNING
            run.finished_at = None
    elif etype == "session.status_idle":
        stop = ev.get("stop_reason") or {}
        reason = stop.get("type")
        run.stop_reason = reason
        if reason == "requires_action":
            run.status = RunStatus.WAITING_APPROVAL
            run.pending_event_ids = list(stop.get("event_ids") or [])
        elif reason == "end_turn":
            run.pending_event_ids = []
            if is_question(last_text):
                run.status = RunStatus.WAITING_INPUT
                run.finished_at = None
            else:
                run.status = RunStatus.COMPLETED
                run.finished_at = utcnow()
        elif reason == "retries_exhausted":
            run.status = RunStatus.FAILED
            run.finished_at = utcnow()
            run.error = run.error or "The session ran out of retries (retries_exhausted)."
        elif reason == "budget_reached":
            run.status = RunStatus.FAILED
            run.finished_at = utcnow()
            run.error = "The session budget was reached."
    elif etype == "session.status_terminated":
        run.status = RunStatus.FAILED if run.error and run.status != RunStatus.COMPLETED else RunStatus.COMPLETED
        run.pending_event_ids = []
        run.finished_at = run.finished_at or utcnow()


def store_event(db: Session, run: WorkflowRun, ev: dict[str, Any]) -> bool:
    """Enregistre l'événement s'il est nouveau et met à jour l'exécution. Retourne True si nouveau."""
    etype = str(ev.get("type", ""))
    if etype in IGNORED_TYPES:
        return False
    key = event_key(ev)
    exists = db.scalar(
        select(RunEvent.id).where(RunEvent.run_id == run.id, RunEvent.anthropic_event_id == key)
    )
    if exists:
        return False
    last_text = agent_text(ev) if etype == "agent.message" else last_agent_text(db, run)
    try:
        with db.begin_nested():
            db.add(RunEvent(run_id=run.id, anthropic_event_id=key, type=etype, payload_json=ev))
    except IntegrityError:
        return False
    apply_event(run, ev, last_text)
    if etype == "session.error":
        _flag_connector(db, run, ev)
    return True


def _flag_connector(db: Session, run: WorkflowRun, ev: dict[str, Any]) -> None:
    err = ev.get("error") or {}
    if err.get("type") != "mcp_authentication_failed_error":
        return
    server = err.get("mcp_server_name")
    if not server:
        return
    connector = db.scalar(
        select(Connector).where(Connector.user_id == run.user_id, Connector.provider == server)
    )
    if connector:
        connector.status = ConnectorStatus.ERROR
        connector.status_message = "MCP authentication failed during a session: reconnect this connector."
