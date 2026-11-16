"""Upsun worker: drives the Managed Agents sessions of active runs.

Start: `python -m app.worker`.

For each run with the `running` status, a thread:
1. creates the session (the user's agent + environment + vault) if needed;
2. opens the event stream, then rereads the history (consolidation, deduplication by id);
3. sends the initial message if it has not been sent yet;
4. stores each event in `run_events` and updates the status;
5. hands back control as soon as the session is waiting (approval, reply, end of turn).

A run claimed by a worker that has disappeared (heartbeat too old) is resumed.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.anthropic_client import ManagedAgentsAPI, ManagedAgentsError
from app.db import SessionLocal
from app.models import RunEvent, RunStatus, User, Workflow, WorkflowRun, utcnow
from app.runs.events import apply_event, last_agent_text, store_event
from app.settings.service import client_for, resources_ready, sync_user_resources

log = logging.getLogger("dispatch.worker")

POLL_SECONDS = 2.0
HEARTBEAT_SECONDS = 10.0
STALE_AFTER = timedelta(seconds=45)
MAX_CONCURRENT = 50
MAX_ATTEMPTS = 6
# Idle session with no new event: after STALL_LIMIT reconnections, reapply
# the last known idle state (case of a message sent during a state transition).
STALL_LIMIT = 20
STALL_SLEEP = 1.5


class RunDriver:
    def __init__(self, run_id: int, stop: threading.Event) -> None:
        self.run_id = run_id
        self.stop = stop

    # -- helpers ----------------------------------------------------------------
    def _load(self, db: Session) -> WorkflowRun | None:
        return db.get(WorkflowRun, self.run_id)

    def _fail(self, db: Session, run: WorkflowRun, message: str) -> None:
        run.status = RunStatus.FAILED
        run.error = message
        run.finished_at = utcnow()
        db.commit()

    def _ingest(self, db: Session, run: WorkflowRun, events: Any) -> bool:
        """Stores the events; returns True if the session no longer needs the worker."""
        for ev in events:
            if self.stop.is_set():
                return True
            db.refresh(run)  # the web app may have changed the status (cancellation, reply)
            if run.status == RunStatus.CANCELLED:
                store_event(db, run, ev)
                db.commit()
                return True
            if store_event(db, run, ev):
                db.commit()
            if run.status != RunStatus.RUNNING:
                return True
        return False

    # -- cycle principal -----------------------------------------------------------
    def run(self) -> None:
        attempts = 0
        stalls = 0
        while not self.stop.is_set():
            db = SessionLocal()
            try:
                run = self._load(db)
                if run is None or run.status != RunStatus.RUNNING:
                    return
                user = db.get(User, run.user_id)
                assert user is not None and user.settings is not None
                settings = user.settings
                if not resources_ready(settings):
                    sync_user_resources(db, user, settings)
                client = client_for(settings)
                if not run.session_id:
                    title = run.workflow_name_snapshot or run.content_snapshot[:80]
                    run.session_id = client.create_session(
                        settings.agent_id or "",
                        settings.agent_version,
                        settings.environment_id or "",
                        [settings.vault_id or ""],
                        f"Maestro · {title}",
                    )
                    db.commit()
                    log.info("Run %s: session %s created", run.id, run.session_id)
                if self._drive(db, client, run):
                    return
                attempts = 0
                if run.session_id and client.session_status(run.session_id) in {"idle", "terminated"}:
                    stalls += 1
                    if stalls >= STALL_LIMIT:
                        self._settle_from_last_idle(db, run)
                        return
                    time.sleep(STALL_SLEEP)
                else:
                    stalls = 0
            except ManagedAgentsError as exc:
                attempts += 1
                log.warning("Run %s: API error (%s/%s): %s", self.run_id, attempts, MAX_ATTEMPTS, exc)
                if attempts >= MAX_ATTEMPTS:
                    db.rollback()
                    run = self._load(db)
                    if run and run.status == RunStatus.RUNNING:
                        self._fail(db, run, str(exc))
                    return
                time.sleep(min(2**attempts, 30))
            except Exception:  # stream network error, etc.: reconnect
                attempts += 1
                log.exception("Run %s: unexpected error (%s/%s)", self.run_id, attempts, MAX_ATTEMPTS)
                if attempts >= MAX_ATTEMPTS:
                    db.rollback()
                    run = self._load(db)
                    if run and run.status == RunStatus.RUNNING:
                        self._fail(db, run, "Tracking this session failed repeatedly.")
                    return
                time.sleep(min(2**attempts, 30))
            finally:
                db.close()

    def _settle_from_last_idle(self, db: Session, run: WorkflowRun) -> None:
        last_idle = db.scalar(
            select(RunEvent)
            .where(RunEvent.run_id == run.id, RunEvent.type.in_(
                ["session.status_idle", "session.status_terminated"]))
            .order_by(RunEvent.id.desc())
            .limit(1)
        )
        db.refresh(run)
        if run.status != RunStatus.RUNNING:
            return
        if last_idle is None:
            self._fail(db, run, "The session went idle without producing a result.")
            return
        apply_event(run, last_idle.payload_json, last_agent_text(db, run))
        if run.status == RunStatus.RUNNING:  # for example, last state = queued message
            self._fail(db, run, "The session stopped responding.")
            return
        db.commit()
        log.info("Run %s: state restored from the last idle event (%s)", run.id, run.status)

    def _drive(self, db: Session, client: ManagedAgentsAPI, run: WorkflowRun) -> bool:
        """Returns True when the worker can release the run."""
        assert run.session_id
        with client.open_stream(run.session_id) as stream:
            # Consolidation: the stream is open, replay the history to fill the gaps.
            history = client.list_events(run.session_id)
            if not run.initial_sent:
                if any(ev.get("type") == "user.message" for ev in history):
                    run.initial_sent = True  # already sent before a restart
                else:
                    client.send_user_message(run.session_id, run.initial_message)
                    run.initial_sent = True
                    run.status = RunStatus.RUNNING
                db.commit()
                if self._ingest(db, run, history):
                    # The history ends in a waiting state, but the message was just sent.
                    if run.status != RunStatus.CANCELLED:
                        run.status = RunStatus.RUNNING
                        db.commit()
            elif self._ingest(db, run, history):
                return True
            if self._ingest(db, run, stream):
                return True
        # Stream ended without a final state: check the actual state before reconnecting.
        db.refresh(run)
        if run.status != RunStatus.RUNNING:
            return True
        status = client.session_status(run.session_id)
        if status in {"idle", "terminated"}:
            if self._ingest(db, run, client.list_events(run.session_id)):
                return True
        return False


SCHEDULE_SYNC_SECONDS = 60.0


def _design_job(wf_id: int) -> None:
    from app.maestro.service import generate_design

    db = SessionLocal()
    try:
        generate_design(db, wf_id)
    except Exception:
        log.exception("Workflow %s: design generation crashed", wf_id)
    finally:
        db.close()


class Worker:
    def __init__(self) -> None:
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}"
        self.stop = threading.Event()
        self.threads: dict[int, threading.Thread] = {}
        self._last_heartbeat = 0.0

    def claim(self, db: Session) -> list[int]:
        now = utcnow()
        stale = now - STALE_AFTER
        candidates = db.scalars(
            select(WorkflowRun.id).where(
                WorkflowRun.status == RunStatus.RUNNING,
                or_(
                    WorkflowRun.claimed_by.is_(None),
                    WorkflowRun.claimed_by == self.worker_id,
                    WorkflowRun.heartbeat_at < stale,
                ),
            ).order_by(WorkflowRun.id)
        ).all()
        claimed: list[int] = []
        for run_id in candidates:
            if run_id in self.threads or len(self.threads) + len(claimed) >= MAX_CONCURRENT:
                continue
            res = db.execute(
                update(WorkflowRun)
                .where(
                    WorkflowRun.id == run_id,
                    WorkflowRun.status == RunStatus.RUNNING,
                    or_(
                        WorkflowRun.claimed_by.is_(None),
                        WorkflowRun.claimed_by == self.worker_id,
                        WorkflowRun.heartbeat_at < stale,
                    ),
                )
                .values(claimed_by=self.worker_id, heartbeat_at=now)
            )
            if res.rowcount == 1:  # type: ignore[attr-defined]
                claimed.append(run_id)
        db.commit()
        return claimed

    def heartbeat(self, db: Session) -> None:
        alive = [rid for rid, t in self.threads.items() if t.is_alive()]
        if alive:
            db.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id.in_(alive), WorkflowRun.claimed_by == self.worker_id)
                .values(heartbeat_at=utcnow())
            )
            db.commit()

    def reap(self, db: Session) -> None:
        done = [rid for rid, t in self.threads.items() if not t.is_alive()]
        if done:
            db.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id.in_(done), WorkflowRun.claimed_by == self.worker_id)
                .values(claimed_by=None)
            )
            db.commit()
            for rid in done:
                del self.threads[rid]

    def maestro_jobs(self, db: Session) -> None:
        """Generate queued Maestro designs and import runs fired by schedules."""
        from app.maestro import service as maestro

        self.design_threads = {k: t for k, t in getattr(self, "design_threads", {}).items() if t.is_alive()}
        for wf_id in maestro.claim_design_jobs(db):
            t = threading.Thread(target=_design_job, args=(wf_id,), name=f"design-{wf_id}", daemon=True)
            self.design_threads[wf_id] = t
            t.start()
            log.info("Workflow %s: design generation started", wf_id)
        if time.monotonic() - getattr(self, "_last_schedule_sync", 0.0) >= SCHEDULE_SYNC_SECONDS:
            self._last_schedule_sync = time.monotonic()
            for wf in db.scalars(select(Workflow).where(Workflow.deployment_id.is_not(None))):
                try:
                    if maestro.import_scheduled_runs(db, wf):
                        log.info("Workflow %s: scheduled run(s) imported", wf.id)
                except Exception:
                    db.rollback()
                    log.exception("Workflow %s: could not import scheduled runs", wf.id)

    def tick(self) -> None:
        db = SessionLocal()
        try:
            self.maestro_jobs(db)
            self.reap(db)
            for run_id in self.claim(db):
                t = threading.Thread(
                    target=RunDriver(run_id, self.stop).run, name=f"run-{run_id}", daemon=True
                )
                self.threads[run_id] = t
                t.start()
                log.info("Run %s picked up by %s", run_id, self.worker_id)
            if time.monotonic() - self._last_heartbeat >= HEARTBEAT_SECONDS:
                self.heartbeat(db)
                self._last_heartbeat = time.monotonic()
        finally:
            db.close()

    def serve(self) -> None:
        log.info("Worker %s started", self.worker_id)
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Error in the worker loop")
            self.stop.wait(POLL_SECONDS)
        # Clean shutdown: release the claims so runs resume immediately.
        db = SessionLocal()
        try:
            db.execute(
                update(WorkflowRun)
                .where(WorkflowRun.claimed_by == self.worker_id)
                .values(claimed_by=None)
            )
            db.commit()
        finally:
            db.close()
        log.info("Worker %s stopped", self.worker_id)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    from app.config import missing_secrets

    missing = missing_secrets()
    if missing:
        # Do not crash in a loop: wait for a redeploy with the correct variables.
        log.error("Incomplete configuration (%s): worker waiting.", ", ".join(missing))
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        while not stop.wait(60):
            pass
        return
    worker = Worker()
    signal.signal(signal.SIGTERM, lambda *_: worker.stop.set())
    signal.signal(signal.SIGINT, lambda *_: worker.stop.set())
    worker.serve()


if __name__ == "__main__":
    main()
