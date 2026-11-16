"""Progress of a Maestro workflow across runs.

A run's `status` follows Maestro's session: it ends ("completed") as soon as Maestro has done its part
and handed off to Upsun Dispatch or to a person. The workflow itself is only finished when its last
step is. `progress` records where the workflow stands when the session ends:

- ongoing: waiting for Dispatch or a human gate; a later run (schedule, GitHub wake-up, Run now) continues it
- blocked: Maestro reported something it cannot fix (PR closed without merge, Dispatch run failed...)
- done: the last step is complete
- continued: a newer run of the same workflow took over (see `continued_by_run_id`)
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import RunEvent, RunStatus, WorkflowRun

WORKFLOW_MARKER = re.compile(r"\[workflow:\s*(ongoing|done|blocked)\s*\]", re.IGNORECASE)
# Orchestration prompts often ask for a status line instead ("Workflow: active", "**Workflow:** done").
STATUS_LINE = re.compile(
    r"^[\s>*_#-]*workflow\s*(?:status)?[*_]*\s*:[*_\s]*(ongoing|active|in progress|done|complete|completed|"
    r"finished|blocked)\b",
    re.IGNORECASE | re.MULTILINE,
)
STATUS_WORDS = {"active": "ongoing", "in progress": "ongoing", "complete": "done", "completed": "done",
                "finished": "done"}


def workflow_marker(text: str | None) -> str | None:
    """Workflow status Maestro reported in a message: its marker, else a "Workflow: ..." status line."""
    found = WORKFLOW_MARKER.findall(text or "") or STATUS_LINE.findall(text or "")
    if not found:
        return None
    word = re.sub(r"\s+", " ", found[-1].lower())
    return STATUS_WORDS.get(word, word)


def strip_workflow_markers(text: str | None) -> str:
    return WORKFLOW_MARKER.sub("", text or "")


def is_maestro_run(run: WorkflowRun) -> bool:
    return bool(run.design_snapshot or (run.workflow is not None and run.workflow.description))


def compute_progress(db: Session, run: WorkflowRun) -> str:
    """Where the workflow stands at the end of this run's session."""
    from app.runs.events import agent_text, step_marker

    design: dict[str, Any] = run.design_snapshot or (run.workflow.design_json if run.workflow else None) or {}
    step_ids = [str(s.get("id")) for s in design.get("steps") or []]
    last_step = None
    rows = db.scalars(
        select(RunEvent)
        .where(RunEvent.run_id == run.id, RunEvent.type == "agent.message")
        .order_by(RunEvent.id.desc())
    )
    for row in rows:
        text = agent_text(row.payload_json)
        marker = workflow_marker(text)
        if marker:
            return marker  # Maestro's own report wins
        if last_step is None:
            announced = step_marker(text)
            if announced in step_ids:
                last_step = announced
    # No report (for example a schedule created before the marker existed): done only when the last
    # step of the design was reached.
    return "done" if step_ids and last_step == step_ids[-1] else "ongoing"


def settle_progress(db: Session, run: WorkflowRun) -> None:
    """Called when a run's status changes: record the workflow progress once the session has ended."""
    if not is_maestro_run(run) or run.progress == "continued":
        return
    if run.status == RunStatus.COMPLETED:
        run.progress = compute_progress(db, run)
    elif run.status in {RunStatus.FAILED, RunStatus.CANCELLED}:
        run.progress = None
    else:
        run.progress = None  # session active again (for example you continued the conversation)


def mark_continued(db: Session, run: WorkflowRun) -> None:
    """A new run of a workflow takes over the previous ones that left it ongoing or blocked."""
    if run.workflow_id is None or run.id is None:
        return
    db.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.workflow_id == run.workflow_id,
            WorkflowRun.user_id == run.user_id,  # a shared workflow: each user's runs follow their own items
            WorkflowRun.id != run.id,
            WorkflowRun.progress.in_(["ongoing", "blocked"]),
        )
        .values(progress="continued", continued_by_run_id=run.id)
    )


def refresh_progress(db: Session) -> int:
    """Recompute the progress of ended Maestro runs still shown as ongoing (or never computed)."""
    runs = db.scalars(
        select(WorkflowRun).where(
            WorkflowRun.status == RunStatus.COMPLETED,
            (WorkflowRun.progress.is_(None)) | (WorkflowRun.progress == "ongoing"),
        )
    ).all()
    changed = 0
    for run in runs:
        if not is_maestro_run(run):
            continue
        progress = compute_progress(db, run)
        if progress != run.progress:
            run.progress, changed = progress, changed + 1
    db.commit()
    return changed
