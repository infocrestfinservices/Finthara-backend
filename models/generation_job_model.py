"""A background report-generation job.

Report generation is a 1-3 minute pipeline (AI agents + a LibreOffice recalc + the Word
build). Run inside the HTTP request it outlives the platform's ~60s gateway timeout and the
client gets a 504 for a report that was actually still building. So the request now only
CREATES one of these rows and hands the work to a worker thread; the browser polls
GET /generate/jobs/{id} until status is "done" (result carried on the row) or "failed".

One row per run. A finished row is kept — it's the audit trail of what was generated and
when, and re-generating just makes a new row.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from database import Base


class GenerationJob(Base):
    __tablename__ = "generation_jobs"

    # A uuid hex, not an autoincrement int — the id travels to the browser and is polled
    # by anyone who has it, so it must not be guessable.
    id = Column(String, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    # Who started it. The status endpoint checks this (or team access to the project).
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    status = Column(String, nullable=False, default="queued")   # queued|running|done|failed
    stage = Column(String, nullable=True)                        # human label of the step
    progress = Column(Integer, nullable=False, default=0)        # 0-100

    # The exact payload the old synchronous endpoint returned, as JSON — so the frontend
    # consumes a finished job identically to the old direct response.
    result = Column(Text, nullable=True)
    error = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
