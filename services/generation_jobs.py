"""Runs report generation off the HTTP request, on a small worker-thread pool.

Why a thread and not FastAPI BackgroundTasks: BackgroundTasks run in the same event loop
after the response is sent, so a blocking 90-second job still ties up the (single) uvicorn
worker and every other request waits. A thread doesn't — the recalc is a subprocess (GIL
released) and the AI calls are I/O, so the event loop stays free to answer status polls.

Why a thread and not Celery/RQ: there is no Redis and no second service on this deploy.
A pool of 2 threads in-process is enough for the traffic here and adds no infrastructure.
State lives in the `generation_jobs` table so a poll from any worker sees the truth (there
is only one worker today, but this keeps it honest if that changes).

A job that is still "running" when the process restarts is orphaned — it will sit at
"running" forever. `sweep_stale()` (called on startup) fails anything left mid-flight so a
reload doesn't leave a browser polling a dead job.
"""
from __future__ import annotations

import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from sqlalchemy import or_

from database import SessionLocal
from models.generation_job_model import GenerationJob

# A job whose row hasn't been touched in this long is presumed dead (its worker wedged or
# the process it ran in is gone) — long enough that a legitimately slow step doesn't trip it.
_STALE_AFTER = timedelta(minutes=12)

logger = logging.getLogger("gen_jobs")

# 2 workers: generation is heavy (LibreOffice is the memory spike) and the deploy is small.
# A third concurrent report is far more likely to OOM the container than to finish sooner.
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="genjob")


def create(db, *, project_id: int, user_id: int) -> GenerationJob:
    job = GenerationJob(id=uuid.uuid4().hex, project_id=project_id, user_id=user_id,
                        status="queued", progress=0, stage="Queued")
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def find_active(db, project_id: int) -> GenerationJob | None:
    """A LIVE queued/running job for this project, if one exists — so a double-click or a
    retry latches onto the job already in flight instead of starting a second one. A job
    that hasn't updated in _STALE_AFTER is treated as dead so the user is never permanently
    stuck behind a wedged run."""
    fresh_since = datetime.utcnow() - _STALE_AFTER
    return (db.query(GenerationJob)
              .filter(GenerationJob.project_id == project_id,
                      GenerationJob.status.in_(("queued", "running")),
                      or_(GenerationJob.updated_at >= fresh_since,
                          GenerationJob.updated_at.is_(None)))
              .order_by(GenerationJob.created_at.desc())
              .first())


def submit(job_id: str, work) -> None:
    """`work(db, job)` runs in a worker thread with its OWN db session and must return the
    JSON-serialisable result payload. Progress is reported by calling `progress(db, job, pct,
    stage)` from inside it."""
    _POOL.submit(_run, job_id, work)


def progress(db, job: GenerationJob, pct: int, stage: str) -> None:
    job.progress = max(0, min(99, int(pct)))
    job.stage = stage[:120]
    db.commit()


def _run(job_id: str, work) -> None:
    db = SessionLocal()
    try:
        job = db.query(GenerationJob).filter(GenerationJob.id == job_id).first()
        if not job:
            return
        job.status, job.stage, job.progress = "running", "Starting", 1
        db.commit()
        try:
            result = work(db, job)
            job.result = json.dumps(result)
            job.status, job.progress, job.stage, job.error = "done", 100, "Done", None
        except Exception as exc:                       # noqa: BLE001 — surfaced to the user
            logger.exception("gen job %s failed", job_id)
            db.rollback()
            job = db.query(GenerationJob).filter(GenerationJob.id == job_id).first()
            if job:
                # FastAPI's HTTPException stringifies as "502: message" — the user wants the
                # message, not the status code.
                msg = getattr(exc, "detail", None) or str(exc) or exc.__class__.__name__
                job.status = "failed"
                job.error = str(msg)[:480]
                job.stage = "Failed"
        db.commit()
    finally:
        db.close()


def sweep_stale() -> int:
    """Fail every job still marked queued/running. Called once on startup: the worker pool
    lives in this process, so a fresh process means none of those jobs are actually being
    worked — they died with the old process. A browser polling one then gets a clear
    "restarted, try again" instead of spinning forever.
    """
    db = SessionLocal()
    try:
        stale = (db.query(GenerationJob)
                   .filter(GenerationJob.status.in_(("queued", "running")))
                   .all())
        for job in stale:
            job.status = "failed"
            job.error = "The server restarted while this report was generating. Please try again."
            job.stage = "Failed"
        db.commit()
        return len(stale)
    finally:
        db.close()
