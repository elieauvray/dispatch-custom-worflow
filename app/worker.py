"""Worker Upsun : pilote les sessions Managed Agents des exécutions actives.

Lancement : `python -m app.worker`.

Pour chaque exécution au statut `running`, un thread :
1. crée la session (agent + environnement + vault de l'utilisateur) si besoin ;
2. ouvre le flux d'événements, puis relit l'historique (consolidation, dédoublonnage par id) ;
3. envoie le message initial s'il ne l'a pas encore été ;
4. enregistre chaque événement dans `run_events` et met à jour le statut ;
5. rend la main dès que la session est en attente (validation, réponse, fin de tour).

Une exécution réservée par un worker disparu (heartbeat trop ancien) est reprise.
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
from app.models import RunEvent, RunStatus, User, WorkflowRun, utcnow
from app.runs.events import apply_event, last_agent_text, store_event
from app.settings.service import client_for, resources_ready, sync_user_resources

log = logging.getLogger("dispatch.worker")

POLL_SECONDS = 2.0
HEARTBEAT_SECONDS = 10.0
STALE_AFTER = timedelta(seconds=45)
MAX_CONCURRENT = 50
MAX_ATTEMPTS = 6
# Session idle sans nouvel événement : au bout de STALL_LIMIT reconnexions, on réapplique
# le dernier état idle connu (cas d'un message parti pendant une transition d'état).
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
        """Enregistre les événements ; retourne True si la session n'a plus besoin du worker."""
        for ev in events:
            if self.stop.is_set():
                return True
            db.refresh(run)  # le web a pu changer le statut (annulation, réponse)
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
                        f"Dispatch – {title}",
                    )
                    db.commit()
                    log.info("Run %s : session %s créée", run.id, run.session_id)
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
                log.warning("Run %s : erreur API (%s/%s) : %s", self.run_id, attempts, MAX_ATTEMPTS, exc)
                if attempts >= MAX_ATTEMPTS:
                    db.rollback()
                    run = self._load(db)
                    if run and run.status == RunStatus.RUNNING:
                        self._fail(db, run, str(exc))
                    return
                time.sleep(min(2**attempts, 30))
            except Exception:  # erreur réseau du flux, etc. : on se reconnecte
                attempts += 1
                log.exception("Run %s : erreur inattendue (%s/%s)", self.run_id, attempts, MAX_ATTEMPTS)
                if attempts >= MAX_ATTEMPTS:
                    db.rollback()
                    run = self._load(db)
                    if run and run.status == RunStatus.RUNNING:
                        self._fail(db, run, "Le suivi de la session a échoué à plusieurs reprises.")
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
            self._fail(db, run, "La session est inactive sans avoir produit de résultat.")
            return
        apply_event(run, last_idle.payload_json, last_agent_text(db, run))
        if run.status == RunStatus.RUNNING:  # ex. dernier état = message en file
            self._fail(db, run, "La session ne répond plus.")
            return
        db.commit()
        log.info("Run %s : état réappliqué depuis le dernier idle (%s)", run.id, run.status)

    def _drive(self, db: Session, client: ManagedAgentsAPI, run: WorkflowRun) -> bool:
        """Retourne True quand le worker peut relâcher l'exécution."""
        assert run.session_id
        with client.open_stream(run.session_id) as stream:
            # Consolidation : le flux est ouvert, on rejoue l'historique pour couvrir les trous.
            history = client.list_events(run.session_id)
            if not run.initial_sent:
                if any(ev.get("type") == "user.message" for ev in history):
                    run.initial_sent = True  # déjà envoyé avant un redémarrage
                else:
                    client.send_user_message(run.session_id, run.initial_message)
                    run.initial_sent = True
                    run.status = RunStatus.RUNNING
                db.commit()
                if self._ingest(db, run, history):
                    # L'historique se termine sur un état d'attente, mais le message vient d'être envoyé.
                    if run.status != RunStatus.CANCELLED:
                        run.status = RunStatus.RUNNING
                        db.commit()
            elif self._ingest(db, run, history):
                return True
            if self._ingest(db, run, stream):
                return True
        # Flux terminé sans état final : vérifier l'état réel avant de se reconnecter.
        db.refresh(run)
        if run.status != RunStatus.RUNNING:
            return True
        status = client.session_status(run.session_id)
        if status in {"idle", "terminated"}:
            if self._ingest(db, run, client.list_events(run.session_id)):
                return True
        return False


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

    def tick(self) -> None:
        db = SessionLocal()
        try:
            self.reap(db)
            for run_id in self.claim(db):
                t = threading.Thread(
                    target=RunDriver(run_id, self.stop).run, name=f"run-{run_id}", daemon=True
                )
                self.threads[run_id] = t
                t.start()
                log.info("Run %s pris en charge par %s", run_id, self.worker_id)
            if time.monotonic() - self._last_heartbeat >= HEARTBEAT_SECONDS:
                self.heartbeat(db)
                self._last_heartbeat = time.monotonic()
        finally:
            db.close()

    def serve(self) -> None:
        log.info("Worker %s démarré", self.worker_id)
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Erreur dans la boucle du worker")
            self.stop.wait(POLL_SECONDS)
        # Arrêt propre : libère les réservations pour une reprise immédiate.
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
        log.info("Worker %s arrêté", self.worker_id)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    worker = Worker()
    signal.signal(signal.SIGTERM, lambda *_: worker.stop.set())
    signal.signal(signal.SIGINT, lambda *_: worker.stop.set())
    worker.serve()


if __name__ == "__main__":
    main()
