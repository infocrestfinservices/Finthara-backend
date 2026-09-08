from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from database import get_db
from models.user_model import User
from models.project_model import Project
from services.auth_service import decode_access_token

security = HTTPBearer(auto_error=False)

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
) -> User:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )
    payload = decode_access_token(credentials.credentials)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token"
        )
    user_id = payload.get("user_id")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )
    return user

def get_optional_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
) -> User:
    if not credentials:
        return None
    payload = decode_access_token(credentials.credentials)
    if not payload:
        return None
    user_id = payload.get("user_id")
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id).first()


def _project_or_404(project_id: int, db: Session) -> Project:
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


def get_owned_project(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Project:
    """A project the current user may SEE — as its direct owner, or as an active member
    (any role, viewer included) of the team whose account owns it.

    Reusable guard for read/download routes with a {project_id} path param. Write access is
    gated separately by require_project_editor. Returns 404 (not 403) on a miss so we never
    reveal that a project owned by someone else exists.
    """
    project = _project_or_404(project_id, db)
    if project.user_id == current_user.id:
        return project
    from services.roles import role_in_company
    if project.user_id is not None and role_in_company(
            db, owner_user_id=project.user_id, user_id=current_user.id):
        return project
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")


def _require_project_role(min_role: str):
    """Dependency factory: the caller must be the project's direct owner, or a team member
    with at least `min_role` on the account that owns it."""
    def _dep(project_id: int, db: Session = Depends(get_db),
             current_user: User = Depends(get_current_user)) -> Project:
        project = _project_or_404(project_id, db)
        if project.user_id == current_user.id:
            return project
        from services.roles import role_in_company, meets
        role = (role_in_company(db, owner_user_id=project.user_id, user_id=current_user.id)
                if project.user_id is not None else None)
        if role and meets(role, min_role):
            return project
        if role:
            # They can see it (a viewer) but not do this — say so.
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail=f"Your role ({role}) can view this project but not change it.")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return _dep


require_project_editor = _require_project_role("editor")


def get_strictly_owned_project(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Project:
    """Only the account that owns the project — no team member, whatever their role. For
    irreversible actions (deleting the project and its report)."""
    project = _project_or_404(project_id, db)
    if project.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project

def get_admin_user(current_user: User = Depends(get_current_user)) -> User:
    """Staff only. Everything under /admin depends on this.

    404, not 403. A 403 confirms that /admin exists and that this account simply is not on
    the list, which tells an attacker their target and that the endpoint is worth attacking;
    a 404 says only that there is nothing at that address, which is what a stranger should
    be able to learn. Same reasoning as get_owned_project above.
    """
    if not getattr(current_user, "is_admin", False):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return current_user
