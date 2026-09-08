from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from database import get_db
from models.project_model import Project
from models.report_model import Report
from models.user_model import User
from schemas.project_schema import ProjectCreate, ProjectResponse, ProjectUpdate
from schemas.report_schema import ReportCreate, ReportResponse
from dependencies import (get_current_user, get_owned_project, require_project_editor,
                          get_strictly_owned_project)
from services.roles import my_company_ids

router = APIRouter(prefix="/projects", tags=["Projects"])

@router.post("/", response_model=ProjectResponse)
def create_project(
    data: ProjectCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Ownership comes from the JWT, never from the request body.
    project = Project(**data.model_dump())
    project.user_id = current_user.id
    db.add(project)
    db.commit()
    db.refresh(project)
    return project

@router.get("/", response_model=List[ProjectResponse])
def get_projects(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The caller's own projects, plus every project owned by a team they are an active
    member of (any role). `user_id` on each row tells the frontend whose it is."""
    owner_ids = [current_user.id] + my_company_ids(db, current_user.id)
    return (db.query(Project)
              .filter(Project.user_id.in_(owner_ids))
              .order_by(Project.created_at.desc())
              .all())

@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(project: Project = Depends(get_owned_project)):
    return project


@router.patch("/{project_id}", response_model=ProjectResponse)
def update_project(
    data: ProjectUpdate,
    project: Project = Depends(require_project_editor),
    db: Session = Depends(get_db),
):
    """Edit a project's details. There was no update route at all, so "Edit Details" in
    the report view saved nothing — the browser said "Field updated!" and the old value
    came straight back on refresh.

    Only fields present in the request body are written (`exclude_unset`), so editing one
    figure leaves everything else exactly as it was. Ownership is enforced by
    get_owned_project, the same dependency the other routes use; user_id and id are not
    accepted from the body and so cannot be reassigned."""
    changes = data.model_dump(exclude_unset=True)
    # An omitted field means "leave alone"; an explicit null would otherwise wipe a value.
    changes = {k: v for k, v in changes.items() if v is not None}
    for field, value in changes.items():
        setattr(project, field, value)
    if changes:
        db.commit()
        db.refresh(project)
    return project

@router.post("/{project_id}/report", response_model=ReportResponse)
def save_report(
    data: ReportCreate,
    project: Project = Depends(require_project_editor),
    db: Session = Depends(get_db),
):
    existing = db.query(Report).filter(Report.project_id == project.id).first()
    if existing:
        existing.report_content = data.report_content
        existing.report_format = data.report_format
        existing.financial_format = data.financial_format
        existing.status = "completed"
        db.commit()
        db.refresh(existing)
        return existing
    report = Report(
        project_id=project.id,
        report_content=data.report_content,
        report_format=data.report_format,
        financial_format=data.financial_format,
        status="completed"
    )
    db.add(report)
    project.status = "completed"
    db.commit()
    db.refresh(report)
    return report

@router.get("/{project_id}/report", response_model=ReportResponse)
def get_report(
    project: Project = Depends(get_owned_project),
    db: Session = Depends(get_db),
):
    report = db.query(Report).filter(Report.project_id == project.id).first()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report

@router.delete("/{project_id}")
def delete_project(
    project: Project = Depends(get_strictly_owned_project),
    db: Session = Depends(get_db),
):
    db.delete(project)
    db.commit()
    return {"message": "Project deleted"}
