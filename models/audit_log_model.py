from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship
from datetime import datetime

from database import Base


class AuditLog(Base):
    """One row per meaningful write inside a company: a member added/removed, a role
    changed, an invite sent/accepted/revoked — anything the owner would want a record of.
    Written only through services/audit_service.py's log(), never constructed here directly,
    so the shape stays in one place.
    """
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Nullable: some events have no signed-in actor (a rejected invite, an auto-claim on
    # signup attributed to the original inviter but triggered by the new account).
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    action = Column(String, nullable=False, index=True)   # e.g. "member.role_changed"
    target = Column(String, nullable=True)                 # free text: an email, "user:12"
    meta = Column(String, nullable=True)                   # small JSON blob, extra detail
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    owner = relationship("User", foreign_keys=[owner_user_id])
    actor = relationship("User", foreign_keys=[actor_user_id])
