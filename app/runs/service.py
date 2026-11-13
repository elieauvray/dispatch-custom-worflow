"""Création des exécutions et actions utilisateur relayées vers l'API Anthropic."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.anthropic_client import ManagedAgentsError
from app.deps import get_user_settings
from app.models import RunEvent, RunStatus, Skill, User, Workflow, WorkflowRun, utcnow
from app.runs.events import agent_text
from app.settings.service import client_for, resources_ready, sync_user_resources
from app.workflows.compose import compose_workflow_message, snapshot_skills


def ensure_ready(db: Session, user: User) -> None:
    settings = get_user_settings(db, user)
    if not resources_ready(settings):
        sync_user_resources(db, user, settings)


def create_prompt_run(db: Session, user: User, prompt: str) -> WorkflowRun:
    ensure_ready(db, user)
    run = WorkflowRun(
        user_id=user.id,
        workflow_id=None,
        status=RunStatus.RUNNING,
        content_snapshot=prompt,
        skills_snapshot=[],
        initial_message=prompt,
        pending_event_ids=[],
    )
    db.add(run)
    db.commit()
    return run


def create_workflow_run(db: Session, user: User, workflow: Workflow) -> WorkflowRun:
    ensure_ready(db, user)
    skills = snapshot_skills(db.scalars(select(Skill)))
    run = WorkflowRun(
        user_id=user.id,
        workflow_id=workflow.id,
        status=RunStatus.RUNNING,
        content_snapshot=workflow.content,
        workflow_name_snapshot=workflow.name,
        skills_snapshot=skills,
        initial_message=compose_workflow_message(skills, workflow.name, workflow.content),
        pending_event_ids=[],
    )
    db.add(run)
    db.commit()
    return run


def _release(run: WorkflowRun) -> None:
    """Remet l'exécution entre les mains du worker."""
    run.status = RunStatus.RUNNING
    run.finished_at = None
    run.claimed_by = None


def send_message(db: Session, user: User, run: WorkflowRun, text: str) -> None:
    if not run.session_id:
        raise ManagedAgentsError("La session n'est pas encore créée, patientez un instant.")
    if run.status == RunStatus.WAITING_APPROVAL:
        raise ManagedAgentsError("Répondez d'abord aux demandes de validation en attente.")
    client_for(get_user_settings(db, user)).send_user_message(run.session_id, text)
    _release(run)
    run.stop_reason = None
    db.commit()


def confirm_tool(
    db: Session, user: User, run: WorkflowRun, event_id: str, allow: bool, deny_message: str | None
) -> None:
    if not run.session_id or event_id not in (run.pending_event_ids or []):
        raise ManagedAgentsError("Cette demande de validation n'est plus en attente.")
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


def reference_result(db: Session, run: WorkflowRun) -> str:
    """Texte des messages de Claude du dernier tour (depuis le dernier message utilisateur)."""
    events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.id)))
    texts: list[str] = []
    for ev in events:
        if ev.type == "user.message":
            texts = []
        elif ev.type == "agent.message":
            texts.append(agent_text(ev.payload_json))
    return "\n\n".join(t for t in texts if t.strip())
