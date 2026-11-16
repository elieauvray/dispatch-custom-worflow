"""Run creation and user actions relayed to the Anthropic API."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.anthropic_client import ManagedAgentsError
from app.deps import get_user_settings
from app.models import RunStatus, Skill, User, Workflow, WorkflowRun, utcnow
from app.settings.service import client_for, resources_ready, sync_user_resources
from app.workflows.compose import compose_workflow_message, snapshot_skills


def ensure_ready(db: Session, user: User) -> None:
    settings = get_user_settings(db, user)
    if not resources_ready(settings):
        sync_user_resources(db, user, settings)


def create_workflow_run(db: Session, user: User, workflow: Workflow) -> WorkflowRun:
    ensure_ready(db, user)
    if workflow.description:  # Maestro workflow: orchestration prompt + config + run context
        from app.maestro.service import maestro_message

        skills, message = maestro_message(db, workflow, "manual")
    else:
        skills = snapshot_skills(db.scalars(select(Skill)))
        message = compose_workflow_message(skills, workflow.name, workflow.content)
    run = WorkflowRun(
        user_id=user.id,
        workflow_id=workflow.id,
        status=RunStatus.RUNNING,
        trigger="manual",
        design_snapshot=workflow.design_json if workflow.description else None,
        content_snapshot=workflow.content,
        workflow_name_snapshot=workflow.name,
        skills_snapshot=skills,
        initial_message=message,
        pending_event_ids=[],
    )
    db.add(run)
    db.commit()
    return run


def _release(run: WorkflowRun) -> None:
    """Hands the run back to the worker."""
    run.status = RunStatus.RUNNING
    run.finished_at = None
    run.claimed_by = None


def send_message(db: Session, user: User, run: WorkflowRun, text: str) -> None:
    if not run.session_id:
        raise ManagedAgentsError("The session is not created yet. Wait a moment.")
    if run.status == RunStatus.WAITING_APPROVAL:
        raise ManagedAgentsError("Answer the pending approval requests first.")
    client_for(get_user_settings(db, user)).send_user_message(run.session_id, text)
    _release(run)
    run.stop_reason = None
    db.commit()


def confirm_tool(
    db: Session, user: User, run: WorkflowRun, event_id: str, allow: bool, deny_message: str | None
) -> None:
    if not run.session_id or event_id not in (run.pending_event_ids or []):
        raise ManagedAgentsError("This approval request is no longer pending.")
    client_for(get_user_settings(db, user)).send_tool_confirmations(
        run.session_id, [(event_id, allow, (deny_message or "").strip() or None)]
    )
    remaining = [e for e in run.pending_event_ids if e != event_id]
    run.pending_event_ids = remaining
    if not remaining:
        _release(run)
    db.commit()


def cancel_run(db: Session, user: User, run: WorkflowRun) -> None:
    if run.session_id:
        client_for(get_user_settings(db, user)).interrupt(run.session_id)
    run.status = RunStatus.CANCELLED
    run.pending_event_ids = []
    run.finished_at = utcnow()
    db.commit()
