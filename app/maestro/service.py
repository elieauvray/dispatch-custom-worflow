"""Maestro workflows: design from a description, compose each run, schedule with Managed Agents."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.anthropic_client import ManagedAgentsError
from app.deps import get_user_settings
from app.models import RunStatus, Skill, User, Workflow, WorkflowRun, utcnow
from app.plans.service import connector_policies
from app.settings.service import client_for, resources_ready, sync_user_resources
from app.workflows.compose import compose_workflow_message, snapshot_skills

log = logging.getLogger(__name__)
GENERATING_TIMEOUT = timedelta(minutes=20)
ACTORS = ("maestro", "dispatch", "human", "upsun", "external")


# --- Design -------------------------------------------------------------------


def create_maestro_workflow(db: Session, user: User, name: str, description: str) -> Workflow:
    wf = Workflow(user_id=user.id, name=name, description=description, content="", design_status="queued")
    db.add(wf)
    db.commit()
    return wf


def request_design(db: Session, wf: Workflow, feedback: str | None = None) -> None:
    wf.design_feedback = (feedback or "").strip() or None
    wf.design_status = "queued"
    wf.design_error = None
    db.commit()


def claim_design_jobs(db: Session) -> list[int]:
    """Workflows whose design must be generated (queued, or stuck in generating)."""
    stale = utcnow() - GENERATING_TIMEOUT
    ids = db.scalars(
        select(Workflow.id).where(
            (Workflow.design_status == "queued")
            | ((Workflow.design_status == "generating") & (Workflow.design_updated_at < stale))
        )
    ).all()
    claimed = []
    for wf_id in ids:
        res = db.execute(
            update(Workflow)
            .where(Workflow.id == wf_id, Workflow.design_status.in_(["queued", "generating"]))
            .values(design_status="generating", design_updated_at=utcnow())
        )
        if res.rowcount == 1:  # type: ignore[attr-defined]
            claimed.append(wf_id)
    db.commit()
    return claimed


def _normalize_design(raw: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    old_values = {c.get("key"): c.get("value", "") for c in (previous or {}).get("config", [])}
    steps = []
    for i, st in enumerate(raw.get("steps") or []):
        actor = st.get("actor") if st.get("actor") in ACTORS else "maestro"
        steps.append({
            "id": str(st.get("id") or f"S{i + 1}")[:12],
            "phase": str(st.get("phase") or "").strip()[:60],
            "title": str(st.get("title") or "Step").strip()[:80],
            "detail": str(st.get("detail") or "").strip()[:160],
            "actor": actor,
            "dispatch_workflow": st.get("dispatch_workflow") if actor == "dispatch" else "none",
            "gate_owner": str(st.get("gate_owner") or "").strip()[:60] if actor == "human" else "",
            "repeat": str(st.get("repeat") or "").strip()[:60],
            "loop": str(st.get("loop") or "").strip()[:60],
        })
    config = [
        {"key": str(c.get("key", "")).strip()[:64], "description": str(c.get("description", "")).strip()[:200],
         "value": old_values.get(str(c.get("key", "")).strip()[:64], "")}
        for c in raw.get("config") or [] if str(c.get("key", "")).strip()
    ]
    return {
        "summary": str(raw.get("summary") or "").strip(),
        "steps": steps,
        "config": config,
        "assumptions": [str(a) for a in raw.get("assumptions") or []][:20],
        "not_possible": [
            {"need": str(n.get("need", "")), "alternative": str(n.get("alternative", ""))}
            for n in raw.get("not_possible") or []
        ][:20],
        "tools": [str(t) for t in raw.get("tools") or []][:20],
        "schedule": {
            "cron": str((raw.get("schedule") or {}).get("cron") or "0 * * * *")[:64],
            "reason": str((raw.get("schedule") or {}).get("reason") or "")[:240],
        },
    }


def generate_design(db: Session, wf_id: int) -> None:
    """Called by the worker in a background thread."""
    wf = db.get(Workflow, wf_id)
    if wf is None or not wf.description:
        return
    user = db.get(User, wf.user_id)
    assert user is not None
    settings = get_user_settings(db, user)
    try:
        raw = client_for(settings).design_workflow(
            settings.model, wf.description, sorted(connector_policies(db, user)),
            wf.design_json, wf.design_feedback,
        )
        wf.design_json = _normalize_design(raw, wf.design_json)
        wf.content = str(raw["orchestration_prompt"]).strip()
        if not wf.schedule_cron:
            wf.schedule_cron = wf.design_json["schedule"]["cron"]
        wf.design_status = "ready"
        wf.design_error = None
        wf.design_feedback = None
        # The predicted plan diagram follows the new content automatically (hash changes).
    except (ManagedAgentsError, RuntimeError) as exc:
        wf.design_status = "failed"
        wf.design_error = str(exc)
    wf.design_updated_at = utcnow()
    db.commit()
    if wf.design_status == "ready" and wf.deployment_id:
        try:
            push_schedule(db, user, wf)
        except ManagedAgentsError as exc:
            log.warning("Workflow %s: schedule not updated: %s", wf.id, exc)


def save_config(db: Session, wf: Workflow, values: dict[str, str]) -> None:
    design = dict(wf.design_json or {})
    design["config"] = [
        {**c, "value": values.get(c["key"], c.get("value", "")).strip()[:500]} for c in design.get("config", [])
    ]
    wf.design_json = design
    db.commit()


# --- Runs ---------------------------------------------------------------------


def run_context(wf: Workflow, trigger: str) -> str:
    """Block appended to the orchestration prompt on every Maestro run."""
    config = (wf.design_json or {}).get("config") or []
    rows = "\n".join(f"| {c['key']} | {c.get('value') or '<not set: ask the user>'} |" for c in config)
    table = f"| Key | Value |\n|-----|-------|\n{rows}\n" if rows else "No config values.\n"
    return (
        "\n\n# This run\n"
        f"Trigger: {trigger}. You are Maestro. Read the ledgers first, advance every active item as far as "
        "possible, stop at human gates, never wait more than 30 minutes in total in this run, then end "
        "with the status report.\n\n"
        f"## Config values\n{table}"
    )


def maestro_message(db: Session, wf: Workflow, trigger: str) -> tuple[list[dict[str, Any]], str]:
    skills = snapshot_skills(db.scalars(select(Skill)))
    return skills, compose_workflow_message(skills, wf.name, wf.content + run_context(wf, trigger))


# --- Schedule (Managed Agents deployment) -----------------------------------------


def _ensure_ready(db: Session, user: User) -> None:
    settings = get_user_settings(db, user)
    if not resources_ready(settings):
        sync_user_resources(db, user, settings)


def push_schedule(db: Session, user: User, wf: Workflow) -> None:
    """Create or update the deployment so scheduled runs use the current prompt, skills and config."""
    _ensure_ready(db, user)
    settings = get_user_settings(db, user)
    client = client_for(settings)
    _, message = maestro_message(db, wf, "schedule")
    cron = wf.schedule_cron or "0 * * * *"
    if wf.deployment_id:
        client.update_deployment(wf.deployment_id, message, cron, wf.schedule_timezone, wf.budget_cents)
    else:
        wf.deployment_id = client.create_deployment(
            f"Maestro · {wf.name}", settings.agent_id or "", settings.environment_id or "",
            [settings.vault_id or ""], message, cron, wf.schedule_timezone, wf.budget_cents,
        )
        wf.deployment_status = "active"
    db.commit()


def set_paused(db: Session, user: User, wf: Workflow, paused: bool) -> None:
    if not wf.deployment_id:
        return
    client_for(get_user_settings(db, user)).set_deployment_paused(wf.deployment_id, paused)
    wf.deployment_status = "paused" if paused else "active"
    db.commit()


def archive_schedule(db: Session, user: User, wf: Workflow) -> None:
    if wf.deployment_id:
        client_for(get_user_settings(db, user)).archive_deployment(wf.deployment_id)
        wf.deployment_id, wf.deployment_status = None, None
        db.commit()


def import_scheduled_runs(db: Session, wf: Workflow) -> int:
    """Register the sessions fired by the schedule so the worker follows them like any run."""
    user = db.get(User, wf.user_id)
    if user is None or not wf.deployment_id:
        return 0
    runs = client_for(get_user_settings(db, user)).list_deployment_runs(wf.deployment_id)
    known = set(db.scalars(select(WorkflowRun.deployment_run_id).where(WorkflowRun.workflow_id == wf.id)))
    added = 0
    for r in runs:
        rid, sid = r.get("id"), r.get("session_id")
        if not rid or rid in known:
            continue
        error = r.get("error") or {}
        skills, message = maestro_message(db, wf, "schedule")
        db.add(WorkflowRun(
            user_id=user.id, workflow_id=wf.id, trigger="schedule", deployment_run_id=rid,
            session_id=sid, initial_sent=True, initial_message=message,
            status=RunStatus.RUNNING if sid else RunStatus.FAILED,
            error=None if sid else f"Scheduled run failed: {error.get('type', 'unknown')} {error.get('message', '')}",
            content_snapshot=wf.content, workflow_name_snapshot=wf.name, skills_snapshot=skills,
            pending_event_ids=[], finished_at=None if sid else utcnow(),
        ))
        added += 1
    if added:
        db.commit()
    return added
