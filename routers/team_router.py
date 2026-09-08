"""Team seats — invite people onto your account, give them a role, manage them.

A "company" is your own account (identified by your user id); its members share your
Professional/Enterprise plan's seats and, per their role, your projects:
  viewer  — read your projects, view and download their reports
  editor  — the above, plus create / edit / generate
  owner   — the above, plus manage the team (this router)

Two audiences hit this router:
  * an OWNER managing their team               -> /team, /team/invites, /team/members
  * a MEMBER acting on teams they belong to    -> /team/memberships, /team/invites/pending
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from dependencies import get_current_user
from models.company_model import CompanyInvitation, CompanyUser
from models.user_model import User
from schemas.team_schema import (
    AcceptInviteRequest, AuditEntryOut, InvitationOut, InviteRequest, MemberOut,
    MembershipOut, PendingInviteOut, SetRoleRequest, TeamOut,
)
from services import audit_service, roles, team_service
from services.email_service import send_team_invite_email
from services.entitlements import effective_plan, plan_spec, seat_limit, team_enabled
from services.team_service import TeamError

logger = logging.getLogger("team")

router = APIRouter(prefix="/team", tags=["Team"])


def _team_name(owner: User) -> str:
    return (owner.full_name or owner.email.split("@")[0]) + "'s team"


def _member_out(db: Session, cu: CompanyUser) -> MemberOut:
    u = db.query(User).filter(User.id == cu.user_id).first()
    return MemberOut(user_id=cu.user_id, email=(u.email if u else "—"),
                     full_name=(u.full_name if u else None), role=cu.role,
                     joined_at=cu.joined_at, is_owner=False)


def _invite_out(db: Session, inv: CompanyInvitation) -> InvitationOut:
    by = db.query(User).filter(User.id == inv.invited_by_id).first()
    return InvitationOut(id=inv.id, email=inv.email, role=inv.role, status=inv.status,
                         invited_by=(by.full_name or by.email) if by else None,
                         created_at=inv.created_at, expires_at=inv.expires_at)


# Team management is the account holder's own — `current_user` IS the owner on every
# endpoint below. (The "owner" role exists only as the implicit role of the account holder;
# it is never assigned to a member, so there is no co-owner to authorise here.)

# ── the owner's own team ─────────────────────────────────────────────────────────────────
@router.get("", response_model=TeamOut)
@router.get("/", response_model=TeamOut)
def my_team(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    owner = current_user
    members = [_member_out(db, cu) for cu in roles.active_members(db, owner.id)]
    pending = [_invite_out(db, i) for i in
               db.query(CompanyInvitation)
                 .filter(CompanyInvitation.owner_user_id == owner.id,
                         CompanyInvitation.status == "pending")
                 .order_by(CompanyInvitation.created_at.desc()).all()]
    return TeamOut(
        can_manage=True,
        team_enabled=team_enabled(owner),
        plan_label=plan_spec(effective_plan(owner))["label"],
        seats_used=team_service.seats_used(db, owner),
        seats_limit=seat_limit(owner),
        members=members,
        pending_invites=pending,
    )


@router.post("/invites", response_model=InvitationOut)
def send_invite(req: InviteRequest, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    owner = current_user
    try:
        inv = team_service.create_invitation(db, owner=owner, email=str(req.email),
                                             role=req.role, invited_by=owner)
        db.commit()
    except TeamError as e:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(e))
    db.refresh(inv)

    sent = False
    try:
        sent = send_team_invite_email(inv.email, inviter_name=owner.full_name or owner.email,
                                      team_name=_team_name(owner), role=inv.role,
                                      token=inv.token)
    except Exception:
        logger.exception("team: invite email failed to send for %s", inv.email)

    out = _invite_out(db, inv)
    if not sent:
        # Dev / email-not-configured: hand the accept link back so it can still be tested.
        out.accept_token = inv.token
    return out


@router.delete("/invites/{invitation_id}")
def revoke_invite(invitation_id: int, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    try:
        team_service.revoke_invitation(db, owner=current_user, invitation_id=invitation_id,
                                       actor_user_id=current_user.id)
        db.commit()
    except TeamError as e:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True}


@router.patch("/members/{member_user_id}", response_model=MemberOut)
def change_member_role(member_user_id: int, req: SetRoleRequest,
                       db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    try:
        row = team_service.set_role(db, owner=current_user, member_user_id=member_user_id,
                                    role=req.role, actor_user_id=current_user.id)
        db.commit()
    except TeamError as e:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(e))
    db.refresh(row)
    return _member_out(db, row)


@router.delete("/members/{member_user_id}")
def remove_member(member_user_id: int, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    try:
        team_service.remove_member(db, owner=current_user, member_user_id=member_user_id,
                                   actor_user_id=current_user.id)
        db.commit()
    except TeamError as e:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True}


@router.get("/audit", response_model=list[AuditEntryOut])
def team_audit(limit: int = 50, offset: int = 0, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    rows = audit_service.recent(db, current_user.id, limit=limit, offset=offset)
    out = []
    for r in rows:
        actor = db.query(User).filter(User.id == r.actor_user_id).first() if r.actor_user_id else None
        out.append(AuditEntryOut(
            id=r.id, action=r.action,
            actor=(actor.full_name or actor.email) if actor else None,
            target=r.target,
            meta=(json.loads(r.meta) if r.meta else None),
            created_at=r.created_at))
    return out


# ── acting as a member of other teams ────────────────────────────────────────────────────
@router.get("/memberships", response_model=list[MembershipOut])
def my_memberships(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    rows = (db.query(CompanyUser)
              .filter(CompanyUser.user_id == current_user.id, CompanyUser.is_active.is_(True))
              .all())
    out = []
    for cu in rows:
        owner = db.query(User).filter(User.id == cu.owner_user_id).first()
        if not owner or owner.is_deleted:
            continue
        out.append(MembershipOut(owner_user_id=cu.owner_user_id,
                                 owner_name=owner.full_name, owner_email=owner.email,
                                 role=cu.role))
    return out


@router.delete("/memberships/{owner_user_id}")
def leave_team(owner_user_id: int, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    try:
        team_service.leave_team(db, owner_user_id=owner_user_id, user=current_user)
        db.commit()
    except TeamError as e:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True}


@router.get("/invites/pending", response_model=list[PendingInviteOut])
def invites_for_me(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    from datetime import datetime
    invs = (db.query(CompanyInvitation)
              .filter(CompanyInvitation.email == current_user.email.lower(),
                      CompanyInvitation.status == "pending")
              .all())
    out = []
    for inv in invs:
        if not inv.expires_at or inv.expires_at <= datetime.utcnow():
            continue
        owner = db.query(User).filter(User.id == inv.owner_user_id).first()
        by = db.query(User).filter(User.id == inv.invited_by_id).first()
        out.append(PendingInviteOut(
            token=inv.token, team_name=_team_name(owner) if owner else "a team",
            role=inv.role, invited_by=(by.full_name or by.email) if by else None,
            expires_at=inv.expires_at))
    return out


@router.post("/invites/accept", response_model=MembershipOut)
def accept_invite(req: AcceptInviteRequest, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    try:
        row = team_service.accept_invitation(db, token=req.token, user=current_user)
        db.commit()
    except TeamError as e:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(e))
    owner = db.query(User).filter(User.id == row.owner_user_id).first()
    return MembershipOut(owner_user_id=row.owner_user_id, owner_name=owner.full_name,
                         owner_email=owner.email, role=row.role)
