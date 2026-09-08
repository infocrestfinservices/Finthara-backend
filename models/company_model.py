"""Company-scoped team membership and invitations.

There is no separate `Company` table. In this app, billing and plan already live on `User`
(see models/user_model.py, services/entitlements.py) — a "company" IS the Enterprise-plan
account whose seats a team shares, so it is named by that account's own user id
(`owner_user_id`) rather than by a row of its own. The same person can be Owner of their own
account and Viewer on someone else's — that's why role is per (owner_user_id, user_id) pair,
not a single column on User.

Numeric rank (services/roles.py: VIEWER=0, EDITOR=1, OWNER=2), not a role-name switch —
inserting a role between two existing ones later is a new constant, not a rewrite of every
`if role == ...` across the codebase. "owner" is the account holder's implicit role and is
never stored in a row; a member is "viewer" or "editor".
"""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime

from database import Base


class CompanyUser(Base):
    """One row: this user's role in that company, right now.

    Removing someone is `is_active=False`, never a row delete — the row is the only record
    that they were ever on the team, and support/audit needs that to survive the removal.
    """
    __tablename__ = "company_users"
    __table_args__ = (UniqueConstraint("owner_user_id", "user_id", name="uq_company_member"),)

    id = Column(Integer, primary_key=True, index=True)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    role = Column(String, nullable=False, default="viewer")   # viewer | editor | owner
    is_active = Column(Boolean, nullable=False, default=True)
    invited_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    joined_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", foreign_keys=[owner_user_id])
    user = relationship("User", foreign_keys=[user_id])
    invited_by = relationship("User", foreign_keys=[invited_by_id])


class CompanyInvitation(Base):
    """Keyed by email, not by a User id — the whole point is being able to invite someone
    who doesn't have an account yet. `token` is what the accept link carries, but the token
    alone grants nothing: see services/team_service.py — accepting always re-enters
    add_member(), the same call every other membership grant goes through.
    """
    __tablename__ = "company_invitations"

    id = Column(Integer, primary_key=True, index=True)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    email = Column(String, nullable=False, index=True)
    role = Column(String, nullable=False, default="viewer")   # editor | viewer — never owner
    token = Column(String, nullable=False, unique=True, index=True)
    status = Column(String, nullable=False, default="pending", index=True)
    # pending | accepted | rejected | revoked
    invited_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    responded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", foreign_keys=[owner_user_id])
    invited_by = relationship("User", foreign_keys=[invited_by_id])
