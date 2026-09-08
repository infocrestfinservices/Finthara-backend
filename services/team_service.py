"""Team membership & invitations — the business logic behind routers/team_router.py.

Rules that live here, not in the router:
  * A person is added to a team exactly one way: `add_member()`. Accepting an invitation
    calls it; an owner adding an existing user directly calls it; the signup auto-claim
    calls it. One path, one set of checks, one audit line.
  * Seats are counted as owner + active members. The limit is the plan's (entitlements.py).
  * An invitation's token grants nothing on its own — accept always re-checks the email,
    the expiry and the seat count at accept time, not just at send time.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from models.company_model import CompanyUser, CompanyInvitation
from models.user_model import User
from services import audit_service, roles
from services.entitlements import seat_limit, team_enabled

logger = logging.getLogger("team")

INVITE_TTL_DAYS = 14


class TeamError(Exception):
    """Message is safe to show the user."""


# ── seats ────────────────────────────────────────────────────────────────────────────────
def seats_used(db: Session, owner: User) -> int:
    members = (db.query(CompanyUser)
                 .filter(CompanyUser.owner_user_id == owner.id,
                         CompanyUser.is_active.is_(True))
                 .count())
    return members + 1  # + the owner themselves


def _assert_seat_free(db: Session, owner: User) -> None:
    limit = seat_limit(owner)
    if seats_used(db, owner) >= limit:
        raise TeamError(
            f"Your {owner.plan.title()} plan allows {limit} seats and they are all taken. "
            f"Remove a member or upgrade to add more.")


# ── membership ───────────────────────────────────────────────────────────────────────────
def add_member(db: Session, *, owner: User, member: User, role: str,
               actor_user_id: int | None, invited_by_id: int | None = None) -> CompanyUser:
    role = roles.normalize_role(role) or roles.VIEWER
    if role == roles.OWNER:
        # A co-owner is a real thing, but never handed out through an invite or a self-serve
        # flow — only promoted from within by an existing owner (see set_role).
        role = roles.EDITOR
    if member.id == owner.id:
        raise TeamError("You are already the owner of this team.")

    existing = (db.query(CompanyUser)
                  .filter(CompanyUser.owner_user_id == owner.id,
                          CompanyUser.user_id == member.id)
                  .first())
    if existing and existing.is_active:
        raise TeamError(f"{member.email} is already on the team.")

    _assert_seat_free(db, owner)

    if existing:
        existing.is_active = True
        existing.role = role
        existing.invited_by_id = invited_by_id or existing.invited_by_id
        existing.joined_at = datetime.utcnow()
        row = existing
    else:
        row = CompanyUser(owner_user_id=owner.id, user_id=member.id, role=role,
                          is_active=True, invited_by_id=invited_by_id)
        db.add(row)

    audit_service.log(db, owner_user_id=owner.id, actor_user_id=actor_user_id,
                      action="member.added", target=member.email, meta={"role": role})
    return row


def set_role(db: Session, *, owner: User, member_user_id: int, role: str,
             actor_user_id: int) -> CompanyUser:
    new_role = roles.normalize_role(role)
    if new_role not in (roles.VIEWER, roles.EDITOR):
        raise TeamError("A member can be a viewer or an editor.")
    row = (db.query(CompanyUser)
             .filter(CompanyUser.owner_user_id == owner.id,
                     CompanyUser.user_id == member_user_id,
                     CompanyUser.is_active.is_(True))
             .first())
    if not row:
        raise TeamError("That person is not on the team.")
    old = row.role
    row.role = new_role
    audit_service.log(db, owner_user_id=owner.id, actor_user_id=actor_user_id,
                      action="member.role_changed", target=str(member_user_id),
                      meta={"from": old, "to": new_role})
    return row


def remove_member(db: Session, *, owner: User, member_user_id: int, actor_user_id: int) -> None:
    row = (db.query(CompanyUser)
             .filter(CompanyUser.owner_user_id == owner.id,
                     CompanyUser.user_id == member_user_id,
                     CompanyUser.is_active.is_(True))
             .first())
    if not row:
        raise TeamError("That person is not on the team.")
    # Never a row delete — the row is the only record they were ever on the team.
    row.is_active = False
    audit_service.log(db, owner_user_id=owner.id, actor_user_id=actor_user_id,
                      action="member.removed", target=str(member_user_id),
                      meta={"role": row.role})


# ── invitations ──────────────────────────────────────────────────────────────────────────
def create_invitation(db: Session, *, owner: User, email: str, role: str,
                      invited_by: User) -> CompanyInvitation:
    if not team_enabled(owner):
        raise TeamError("Team seats are available on the Professional and Enterprise plans.")

    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise TeamError("Enter a valid email address.")
    if email == owner.email.lower():
        raise TeamError("That is the owner's own address.")

    role = roles.normalize_role(role) or roles.VIEWER
    if role == roles.OWNER:
        raise TeamError("Invitations can only be sent as editor or viewer.")

    # Already a member?
    member = db.query(User).filter(User.email == email).first()
    if member:
        active = (db.query(CompanyUser)
                    .filter(CompanyUser.owner_user_id == owner.id,
                            CompanyUser.user_id == member.id,
                            CompanyUser.is_active.is_(True))
                    .first())
        if active:
            raise TeamError(f"{email} is already on the team.")

    _assert_seat_free(db, owner)

    # One live invitation per (owner, email): re-inviting refreshes the existing one.
    inv = (db.query(CompanyInvitation)
             .filter(CompanyInvitation.owner_user_id == owner.id,
                     CompanyInvitation.email == email,
                     CompanyInvitation.status == "pending")
             .first())
    now = datetime.utcnow()
    if inv:
        inv.role = role
        inv.token = secrets.token_urlsafe(32)
        inv.expires_at = now + timedelta(days=INVITE_TTL_DAYS)
        inv.invited_by_id = invited_by.id
    else:
        inv = CompanyInvitation(
            owner_user_id=owner.id, email=email, role=role,
            token=secrets.token_urlsafe(32), status="pending",
            invited_by_id=invited_by.id,
            expires_at=now + timedelta(days=INVITE_TTL_DAYS))
        db.add(inv)

    audit_service.log(db, owner_user_id=owner.id, actor_user_id=invited_by.id,
                      action="invite.sent", target=email, meta={"role": role})
    return inv


def revoke_invitation(db: Session, *, owner: User, invitation_id: int, actor_user_id: int) -> None:
    inv = (db.query(CompanyInvitation)
             .filter(CompanyInvitation.id == invitation_id,
                     CompanyInvitation.owner_user_id == owner.id)
             .first())
    if not inv or inv.status != "pending":
        raise TeamError("That invitation is no longer pending.")
    inv.status = "revoked"
    inv.responded_at = datetime.utcnow()
    audit_service.log(db, owner_user_id=owner.id, actor_user_id=actor_user_id,
                      action="invite.revoked", target=inv.email)


def _valid_pending(inv: CompanyInvitation) -> bool:
    return inv.status == "pending" and inv.expires_at and inv.expires_at > datetime.utcnow()


def accept_invitation(db: Session, *, token: str, user: User) -> CompanyUser:
    inv = db.query(CompanyInvitation).filter(CompanyInvitation.token == (token or "")).first()
    if not inv or not _valid_pending(inv):
        raise TeamError("This invitation link is invalid or has expired.")
    if inv.email.lower() != user.email.lower():
        raise TeamError("This invitation was sent to a different email address.")

    owner = db.query(User).filter(User.id == inv.owner_user_id).first()
    if not owner or owner.is_deleted:
        raise TeamError("The team that invited you is no longer active.")

    row = add_member(db, owner=owner, member=user, role=inv.role,
                     actor_user_id=user.id, invited_by_id=inv.invited_by_id)
    inv.status = "accepted"
    inv.responded_at = datetime.utcnow()
    audit_service.log(db, owner_user_id=owner.id, actor_user_id=user.id,
                      action="invite.accepted", target=user.email, meta={"role": row.role})
    return row


def claim_pending_for(db: Session, user: User) -> int:
    """Auto-accept every still-valid invitation addressed to this user's email. Called right
    after a new account is verified, so someone invited before they signed up lands on the
    team without having to click the link. Returns how many were claimed."""
    invs = (db.query(CompanyInvitation)
              .filter(CompanyInvitation.email == user.email.lower(),
                      CompanyInvitation.status == "pending")
              .all())
    claimed = 0
    for inv in invs:
        if not _valid_pending(inv):
            continue
        owner = db.query(User).filter(User.id == inv.owner_user_id).first()
        if not owner or owner.is_deleted:
            continue
        try:
            add_member(db, owner=owner, member=user, role=inv.role,
                       actor_user_id=None, invited_by_id=inv.invited_by_id)
        except TeamError:
            continue  # seat full etc. — leave the invite pending, owner can sort it out
        inv.status = "accepted"
        inv.responded_at = datetime.utcnow()
        audit_service.log(db, owner_user_id=owner.id, actor_user_id=user.id,
                          action="invite.accepted", target=user.email,
                          meta={"role": inv.role, "auto": True})
        claimed += 1
    return claimed


def leave_team(db: Session, *, owner_user_id: int, user: User) -> None:
    row = (db.query(CompanyUser)
             .filter(CompanyUser.owner_user_id == owner_user_id,
                     CompanyUser.user_id == user.id,
                     CompanyUser.is_active.is_(True))
             .first())
    if not row:
        raise TeamError("You are not on that team.")
    row.is_active = False
    audit_service.log(db, owner_user_id=owner_user_id, actor_user_id=user.id,
                      action="member.left", target=user.email)
