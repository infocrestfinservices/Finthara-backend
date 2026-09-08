"""Team roles — the one place that knows what viewer / editor / owner mean and who has
which, for a given company (see models/company_model.py).

A "company" is an Enterprise/Professional-plan account whose seats a team shares; it is
identified by that account's own user id (`owner_user_id`). The account holder is always an
implicit `owner` of their own company — there is no CompanyUser row for them.

Numeric rank, not a name switch: inserting a role between two later is a new constant here,
not an `if role == ...` rewrite across the codebase.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from models.company_model import CompanyUser

VIEWER, EDITOR, OWNER = "viewer", "editor", "owner"
ROLES = (VIEWER, EDITOR, OWNER)
RANK = {VIEWER: 0, EDITOR: 1, OWNER: 2}

# What each role can do to the team's projects/reports.
#   viewer — read a project, view and download its report
#   editor — the above, plus create / edit / generate
#   owner  — the above, plus manage the team (invite, change roles, remove)


def normalize_role(role: str | None) -> str | None:
    r = (role or "").strip().lower()
    return r if r in RANK else None


def meets(role: str | None, minimum: str) -> bool:
    """True if `role` is at least `minimum` in the rank order."""
    if role is None:
        return False
    return RANK.get(role, -1) >= RANK.get(minimum, 99)


def role_in_company(db: Session, *, owner_user_id: int, user_id: int) -> str | None:
    """The user's current role in that company, or None if they are not on the team.

    The account holder is their own company's owner without a row — every other member has
    an active CompanyUser row.
    """
    if owner_user_id == user_id:
        return OWNER
    row = (db.query(CompanyUser)
             .filter(CompanyUser.owner_user_id == owner_user_id,
                     CompanyUser.user_id == user_id,
                     CompanyUser.is_active.is_(True))
             .first())
    return normalize_role(row.role) if row else None


def my_company_ids(db: Session, user_id: int) -> list[int]:
    """owner_user_ids of every team this user is an active member of (their own account is
    not included — that is implicit)."""
    rows = (db.query(CompanyUser.owner_user_id)
              .filter(CompanyUser.user_id == user_id, CompanyUser.is_active.is_(True))
              .all())
    return [r[0] for r in rows]


def active_members(db: Session, owner_user_id: int) -> list[CompanyUser]:
    return (db.query(CompanyUser)
              .filter(CompanyUser.owner_user_id == owner_user_id,
                      CompanyUser.is_active.is_(True))
              .order_by(CompanyUser.joined_at.asc())
              .all())
