"""Maestro workflows: design from a description, compose each run, schedule with Managed Agents."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.anthropic_client import ManagedAgentsError
from app.maestro.knowledge import CONNECTOR_NAMES
from app.deps import get_user_settings
from app.models import RunStatus, Skill, User, Workflow, WorkflowRun, utcnow
from app.plans.service import connector_policies
from app.settings.service import client_for, resources_ready, sync_user_resources
from app.maestro.diagram import fingerprint
from app.maestro.render import tag_text
from app.runs.progress import mark_continued
from app.workflows.compose import compose_workflow_message, instructions_block, instructions_hash, snapshot_skills

log = logging.getLogger(__name__)
GENERATING_TIMEOUT = timedelta(minutes=20)
ACTORS = ("maestro", "dispatch", "human", "upsun", "external")
DONE_WHEN = {"pr_merged", "pr_approved", "issue_closed", "pr_opened"}


# --- Design -------------------------------------------------------------------


def create_maestro_workflow(db: Session, user: User, name: str, description: str,
                            diagram: tuple[str, bytes] | None = None) -> Workflow:
    wf = Workflow(user_id=user.id, name=name, description=description, content="", design_status="queued")
    if diagram:
        wf.diagram_media_type, wf.diagram_data = diagram
    db.add(wf)
    db.commit()
    return wf


def current_instructions(db: Session) -> list[dict[str, Any]]:
    """Active system instructions, in order (stored in the `skills` table)."""
    return snapshot_skills(db.scalars(select(Skill)))


def current_instructions_hash(db: Session) -> str:
    return instructions_hash(current_instructions(db))


def instructions_changed(wf: Workflow, current_hash: str) -> bool:
    """True when the system instructions changed since this workflow's design was generated."""
    return wf.design_status == "ready" and (wf.instructions_hash or "") != current_hash


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
            "repeat": tag_text(st.get("repeat")),
            "loop": tag_text(st.get("loop")),
            "done_when": st.get("done_when") if actor == "dispatch" and st.get("done_when") in DONE_WHEN else "none",
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
        "instruction_adjustments": [
            {"instruction": str(a.get("instruction", "")), "change": str(a.get("change", ""))}
            for a in raw.get("instruction_adjustments") or [] if isinstance(a, dict)
        ][:20],
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


def blocking_message(needed: list[str], connected: set[str], unsupported: list[dict[str, Any]]) -> str | None:
    """Why Maestro cannot run this workflow with the tools connected today (None when it can)."""
    problems = []
    missing = [CONNECTOR_NAMES[c] for c in needed if c not in connected]
    if missing:
        problems.append(
            "This workflow needs " + ", ".join(missing) + ", which " + ("is" if len(missing) == 1 else "are")
            + " not connected to Maestro. Connect " + ("it" if len(missing) == 1 else "them")
            + " in Settings, then submit your request again."
        )
    if unsupported:
        names = ", ".join(f"{u.get('name')} ({u.get('reason')})" for u in unsupported)
        problems.append(
            "It also mentions tools Maestro cannot connect to today: " + names
            + ". Remove them from the description or replace them with Linear, GitHub or Jira/Confluence."
            if missing else
            "This workflow mentions tools Maestro cannot connect to today: " + names
            + ". Remove them from the description or replace them with Linear, GitHub or Jira/Confluence, "
            "then submit your request again."
        )
    return " ".join(problems) or None


def missing_connectors(db: Session, user: User, wf: Workflow) -> list[str]:
    """Connectors the design relies on that are no longer connected."""
    needed = (wf.design_json or {}).get("connectors_needed") or []
    connected = set(connector_policies(db, user))
    return [CONNECTOR_NAMES[c] for c in needed if c in CONNECTOR_NAMES and c not in connected]


def decisions_block(clar: dict[str, Any] | None) -> str:
    decisions = (clar or {}).get("decisions") or []
    if not decisions:
        return ""
    lines = "\n".join(f"- {d['question']} -> {d['answer']}" for d in decisions)
    return f"\n\nDecisions made by the user (final, apply them):\n{lines}"


def normalize_questions(raw: list[Any]) -> list[dict[str, Any]]:
    questions, seen = [], set()
    for i, q in enumerate(raw[:6]):
        if not isinstance(q, dict):
            continue
        options = [str(o).strip()[:160] for o in q.get("options") or [] if str(o).strip()][:4]
        text = str(q.get("question") or "").strip()[:300]
        if not text or len(options) < 2:
            continue
        qid = re.sub(r"[^A-Za-z0-9_-]", "", str(q.get("id") or ""))[:16] or f"Q{i + 1}"
        if qid in seen:
            qid = f"Q{i + 1}"
        seen.add(qid)
        questions.append({"id": qid, "question": text, "why": str(q.get("why") or "").strip()[:240],
                          "options": options})
    return questions


def answer_questions(db: Session, wf: Workflow, answers: dict[str, str]) -> list[str]:
    """Record the answers as decisions and queue the design. Returns the ids still unanswered."""
    clar = dict(wf.clarifications or {})
    questions = clar.get("questions") or []
    missing = [q["id"] for q in questions if not (answers.get(q["id"]) or "").strip()]
    if missing:
        return missing
    decisions = list(clar.get("decisions") or [])
    decisions += [{"question": q["question"], "answer": answers[q["id"]].strip()[:500]} for q in questions]
    wf.clarifications = {**clar, "questions": [], "decisions": decisions}
    wf.design_status = "queued"
    db.commit()
    return []


def generate_design(db: Session, wf_id: int) -> None:
    """Called by the worker in a background thread."""
    wf = db.get(Workflow, wf_id)
    if wf is None or not wf.description:
        return
    user = db.get(User, wf.user_id)
    assert user is not None
    settings = get_user_settings(db, user)
    try:
        client = client_for(settings)
        # System instructions (admin) apply to every workflow and take precedence over the description.
        skills = current_instructions(db)
        instructions, ihash = instructions_block(skills), instructions_hash(skills)
        diagram = {"media_type": wf.diagram_media_type, "data": wf.diagram_data} if wf.diagram_data else None
        # 1. Tool check first (cheap): never write a prompt that needs a tool Maestro cannot use.
        text = wf.description + (f"\n\nRequested change: {wf.design_feedback}" if wf.design_feedback else "")
        check = client.check_tools(settings.model, text, instructions, diagram)
        needed = sorted({c for c in check.get("connectors_needed") or [] if c in CONNECTOR_NAMES})
        blocked = blocking_message(needed, set(connector_policies(db, user)), check.get("unsupported_tools") or [])
        if blocked:
            wf.design_status = "blocked"
            wf.design_error = blocked
            wf.design_updated_at = utcnow()
            db.commit()
            return
        # 2. Clarify: ask the user the decisions that make the workflow executable (once per request)
        services = sorted(connector_policies(db, user))
        clar = dict(wf.clarifications or {})
        request_hash = hashlib.sha256((text + ihash + fingerprint(wf.diagram_data)).encode()).hexdigest()
        if clar.get("for") != request_hash:
            asked = client.clarify_workflow(settings.model, text + decisions_block(clar), services, instructions, diagram)
            questions = normalize_questions(asked.get("questions") or [])
            clar = {"for": request_hash, "questions": questions, "decisions": clar.get("decisions") or []}
            wf.clarifications = clar
        if clar.get("questions"):
            wf.design_status = "needs_answers"
            wf.design_error = None
            wf.design_updated_at = utcnow()
            db.commit()
            return
        # 3. Full design, with the user's decisions
        raw = client.design_workflow(
            settings.model, wf.description + decisions_block(clar), services,
            wf.design_json, wf.design_feedback, instructions, diagram,
        )
        wf.design_json = {**_normalize_design(raw, wf.design_json), "connectors_needed": needed}
        wf.content = str(raw["orchestration_prompt"]).strip()
        if not wf.schedule_cron:
            wf.schedule_cron = wf.design_json["schedule"]["cron"]
        wf.design_status = "ready"
        wf.design_error = None
        wf.design_feedback = None
        wf.instructions_hash = ihash
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


REPORT_STEP = re.compile(r"\b(report|reporting|status|summary|escalat\w*)\b", re.IGNORECASE)


def report_step(steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The design step that holds the end-of-run report (the last step whose title is about reporting)."""
    return next((st for st in reversed(steps) if REPORT_STEP.search(str(st.get("title") or ""))), None)


def _report_marker_rule(steps: list[dict[str, Any]]) -> str:
    st = report_step(steps)
    if st is None:
        return ("Begin your final status report with the marker of the last step you worked on.")
    return (f"Your final status report, and any escalation to the user, belong to step {st['id']} "
            f"({st['title']}): begin that message with `[step: {st['id']}]`, even when the workflow is done "
            "or nothing else happened in this run.")


def run_context(wf: Workflow, trigger: str) -> str:
    """Block appended to the orchestration prompt on every Maestro run."""
    config = (wf.design_json or {}).get("config") or []
    rows = "\n".join(f"| {c['key']} | {c.get('value') or '<not set: ask the user>'} |" for c in config)
    table = f"| Key | Value |\n|-----|-------|\n{rows}\n" if rows else "No config values.\n"
    steps = (wf.design_json or {}).get("steps") or []
    step_rows = "\n".join(f"| {st['id']} | {st['actor']} | {st['title']} |" for st in steps)
    markers = (
        "## Step markers\n"
        "The person following this run sees the workflow diagram and the step you are on. Whenever you start "
        "working on a step, or report on one, begin that message with its marker on its own line, for example "
        f"`[step: {steps[0]['id']}]`. Use only these ids:\n\n| Id | Actor | Step |\n|----|-------|------|\n"
        f"{step_rows}\n\n{_report_marker_rule(steps)}\n\n"
        if steps else ""
    )
    return (
        "\n\n# This run\n"
        f"Trigger: {trigger}. You are Maestro. Read the ledgers first. For each Dispatch workflow you started, "
        "check its completion signal; never interact with it. Start what can be started, stop at human gates, "
        "do not wait for Dispatch, then end with the status report.\n\n"
        f"{markers}"
        "## Workflow status\n"
        "The run page shows whether the workflow is finished. End your final status report with exactly one "
        "of these markers on its own line, even if the prompt above asks for another status line:\n"
        "- `[workflow: ongoing]`: something still has to happen (a Dispatch run to finish, a person to act, "
        "a step to start on a later run);\n"
        "- `[workflow: done]`: every step of the workflow is complete;\n"
        "- `[workflow: blocked]`: it cannot go on without the user (a PR closed without merge, a failed "
        "Dispatch run, a missing value).\n\n"
        f"## Config values\n{table}"
    )


def maestro_message(db: Session, wf: Workflow, trigger: str) -> tuple[list[dict[str, Any]], str]:
    skills = current_instructions(db)
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
        context_type = (r.get("trigger_context") or {}).get("type")
        new_run = WorkflowRun(
            user_id=user.id, workflow_id=wf.id, deployment_run_id=rid,
            trigger="wake" if context_type == "manual" else "schedule",
            session_id=sid, initial_sent=True, initial_message=message,
            status=RunStatus.RUNNING if sid else RunStatus.FAILED,
            error=None if sid else f"Scheduled run failed: {error.get('type', 'unknown')} {error.get('message', '')}",
            content_snapshot=wf.content, workflow_name_snapshot=wf.name, skills_snapshot=skills,
            design_snapshot=wf.design_json,
            pending_event_ids=[], finished_at=None if sid else utcnow(),
        )
        db.add(new_run)
        db.flush()
        mark_continued(db, new_run)
        added += 1
    if added:
        db.commit()
    return added


# --- Wake-up (GitHub webhook) ---------------------------------------------------

WAKE_DEBOUNCE = timedelta(minutes=2)


def _aware(value: Any) -> Any:
    from datetime import UTC

    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _mentions_repo(wf: Workflow, repo: str) -> bool:
    values = " ".join(str(c.get("value") or "") for c in (wf.design_json or {}).get("config") or [])
    haystack = " ".join([wf.content or "", wf.description or "", values]).lower()
    return repo.lower() in haystack


def wake_for_repo(db: Session, user: User, repo: str) -> list[int]:
    """Start a Maestro run now for each ready workflow of the user that works on this repository."""
    woken: list[int] = []
    workflows = db.scalars(
        select(Workflow).where(Workflow.user_id == user.id, Workflow.description.is_not(None),
                               Workflow.design_status == "ready")
    )
    for wf in workflows:
        if not _mentions_repo(wf, repo) or missing_connectors(db, user, wf):
            continue
        if wf.deployment_id and wf.deployment_status != "active":
            continue  # schedule paused on purpose
        recent = db.scalar(
            select(WorkflowRun.id).where(
                WorkflowRun.workflow_id == wf.id, WorkflowRun.started_at >= utcnow() - WAKE_DEBOUNCE
            ).limit(1)
        )
        if recent or (wf.last_woken_at and _aware(wf.last_woken_at) >= utcnow() - WAKE_DEBOUNCE):
            continue  # a run just started or was just requested: it will see the merge
        try:
            if wf.deployment_id:
                client_for(get_user_settings(db, user)).run_deployment(wf.deployment_id)
            else:
                from app.runs.service import create_workflow_run

                run = create_workflow_run(db, user, wf)
                run.trigger = "wake"
                db.commit()
            wf.last_woken_at = utcnow()
            db.commit()
            woken.append(wf.id)
            log.info("Workflow %s woken by a merged pull request on %s", wf.id, repo)
        except ManagedAgentsError as exc:
            log.warning("Workflow %s: wake-up failed: %s", wf.id, exc)
    return woken
