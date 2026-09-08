"""Company audit log — one row per meaningful team write, so the owner has a record of who
did what. Everything goes through `log()`; nothing constructs an AuditLog directly.

Named `audit_service.py` to match email_service.py / totp_service.py; models/audit_log_model.py's
docstring calls it "services/audit.py" — same thing.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from models.audit_log_model import AuditLog

logger = logging.getLogger("audit")


def log(db: Session, *, owner_user_id: int, actor_user_id: int | None, action: str,
        target: str | None = None, meta: dict | None = None) -> None:
    """Record one event. Commits its own row so a caller that rolls back its main change
    still... actually no — it flushes but leaves the commit to the caller, so an event is
    only persisted if the action it describes was. Callers commit once at the end.
    """
    row = AuditLog(
        owner_user_id=owner_user_id,
        actor_user_id=actor_user_id,
        action=action,
        target=(target or None),
        meta=(json.dumps(meta, separators=(",", ":")) if meta else None),
    )
    db.add(row)


def recent(db: Session, owner_user_id: int, *, limit: int = 50, offset: int = 0) -> list[AuditLog]:
    return (db.query(AuditLog)
              .filter(AuditLog.owner_user_id == owner_user_id)
              .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
              .offset(max(0, offset)).limit(min(200, max(1, limit)))
              .all())
