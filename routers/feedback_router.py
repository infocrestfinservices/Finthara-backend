"""Feedback form (footer "Feedback" link → /feedback page).

POST /feedback        — signed-in users only. Saved to the `feedback` table, then a copy is
                        emailed to FEEDBACK_EMAIL (super admin). The email address stored is
                        the account's own (login requires a verified email), so it cannot be
                        a fake address typed into the form.
GET  /admin/feedback  — super admin only: every response, newest first, plus a summary.

Spam: login is required, and one account can send at most _MAX_PER_DAY responses in 24h
(counted in the database, so a restart or a second worker does not reset it).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from dependencies import get_admin_user, get_current_user
from models.feedback_model import Feedback
from models.user_model import User
from services.email_service import send_feedback_message

logger = logging.getLogger("feedback")

router = APIRouter(prefix="/feedback", tags=["Feedback"])
admin_router = APIRouter(prefix="/admin/feedback", tags=["Admin"])

_MAX_PER_DAY = 3

# Must match the option lists in frontend/src/components/FeedbackForm.jsx.
Purpose = Literal["CMA / Bank Loan", "Feasibility Study", "Short Project Report", "Just exploring"]
Quality = Literal["Excellent", "Good", "Average", "Poor"]
Recommend = Literal["Yes", "Maybe", "No"]


class FeedbackRequest(BaseModel):
    rating: int = Field(ge=1, le=5)
    purpose: Purpose
    quality: Quality
    recommend: Recommend
    name: str
    message: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Name is required.")
        if len(v) > 120:
            raise ValueError("Name is too long.")
        return v

    @field_validator("message")
    @classmethod
    def _length(cls, v: str) -> str:
        v = (v or "").strip()
        if len(v) > 5000:
            raise ValueError("Message is too long.")
        return v


@router.post("")
@router.post("/")
def submit_feedback(req: FeedbackRequest, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    if not user.is_verified:
        raise HTTPException(status_code=403, detail="Please verify your email first.")

    since = datetime.utcnow() - timedelta(hours=24)
    recent = (db.query(func.count(Feedback.id))
                .filter(Feedback.user_id == user.id, Feedback.created_at >= since).scalar())
    if recent >= _MAX_PER_DAY:
        raise HTTPException(status_code=429,
                            detail="You've already sent feedback today. Thank you — please try again tomorrow.")

    row = Feedback(user_id=user.id, name=req.name, email=user.email, rating=req.rating,
                   purpose=req.purpose, quality=req.quality, recommend=req.recommend,
                   message=req.message or None)
    db.add(row)
    db.commit()

    # The response is already saved; a mail failure must not lose it or fail the request.
    try:
        send_feedback_message(rating=req.rating, purpose=req.purpose, quality=req.quality,
                              recommend=req.recommend, message=req.message,
                              name=req.name, from_email=user.email)
    except Exception:
        logger.exception("feedback: saved #%s but could not email it", row.id)

    logger.info("feedback: #%s %s/5 (%s) from user %s", row.id, req.rating, req.purpose, user.id)
    return {"ok": True}


def _super_admin(admin: User = Depends(get_admin_user)) -> User:
    # 404 like the rest of /admin, so the endpoint's existence isn't confirmed to others.
    if not getattr(admin, "is_super_admin", False):
        raise HTTPException(status_code=404, detail="Not found")
    return admin


@admin_router.get("")
@admin_router.get("/")
def list_feedback(db: Session = Depends(get_db), _: User = Depends(_super_admin),
                  limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    total = db.query(func.count(Feedback.id)).scalar() or 0
    avg = db.query(func.avg(Feedback.rating)).scalar()
    rows = (db.query(Feedback).order_by(Feedback.created_at.desc(), Feedback.id.desc())
              .limit(limit).offset(offset).all())
    return {
        "total": total,
        "average_rating": round(float(avg), 2) if avg is not None else None,
        "items": [{
            "id": f.id, "name": f.name, "email": f.email, "rating": f.rating,
            "purpose": f.purpose, "quality": f.quality, "recommend": f.recommend,
            "message": f.message,
            "created_at": f.created_at.isoformat() if f.created_at else None,
        } for f in rows],
    }
