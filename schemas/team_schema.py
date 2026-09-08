from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr

# "owner" is the account holder — implicit, never assigned. Members are viewer or editor.
Role = Literal["viewer", "editor"]
InviteRole = Literal["viewer", "editor"]


class InviteRequest(BaseModel):
    email: EmailStr
    role: InviteRole = "viewer"


class AcceptInviteRequest(BaseModel):
    token: str


class SetRoleRequest(BaseModel):
    role: Role


class MemberOut(BaseModel):
    user_id: int
    email: str
    full_name: Optional[str] = None
    role: str
    joined_at: Optional[datetime] = None
    is_owner: bool = False


class InvitationOut(BaseModel):
    id: int
    email: str
    role: str
    status: str
    invited_by: Optional[str] = None
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    # Only populated on the create response when email delivery isn't configured, so the
    # link can still be tested locally. Never set when the invite email actually went out.
    accept_token: Optional[str] = None


class TeamOut(BaseModel):
    can_manage: bool           # is the caller an owner of this team
    team_enabled: bool         # does the plan include seats at all
    plan_label: str
    seats_used: int
    seats_limit: int
    members: list[MemberOut]
    pending_invites: list[InvitationOut]


class MembershipOut(BaseModel):
    owner_user_id: int
    owner_name: Optional[str] = None
    owner_email: str
    role: str


class PendingInviteOut(BaseModel):
    token: str
    team_name: str
    role: str
    invited_by: Optional[str] = None
    expires_at: Optional[datetime] = None


class AuditEntryOut(BaseModel):
    id: int
    action: str
    actor: Optional[str] = None
    target: Optional[str] = None
    meta: Optional[dict] = None
    created_at: datetime
